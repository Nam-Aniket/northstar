#!/usr/bin/env python3
"""
LinkedIn guest job SEARCH -> job_alerts_raw.csv  (no login, no Comet).

Replaces the manual "tell Comet to search for jobs" step. Hits LinkedIn's
public guest endpoint
  /jobs-guest/jobs/api/seeMoreJobPostings/search
which returns job cards with no authentication, paginates them fully, and
writes the pipeline's intake CSV. JD text is left blank on purpose — the
downstream fill_missing_jds.py fetches and caches the JDs idempotently.

Design notes (verified 2026-06-13):
- The endpoint returns 10 cards per page; paginate via &start=0,10,20,...
- Pagination is flaky: an empty page can appear before more results, so we
  keep going until N consecutive empties (not the first one).
- sortBy=DD = newest first. f_TPR=r86400 (24h) / r172800 (48h) / r604800 (7d).
- Keyword search is fuzzy and roles overlap, so we dedupe by job id.
- Requires curl_cffi for a real browser fingerprint (run via .venv/bin/python).

Usage:
  .venv/bin/python 00_search_linkedin_guest.py                       # 4 roles, Australia, 24h
  .venv/bin/python 00_search_linkedin_guest.py --tpr r172800         # last 48h
  .venv/bin/python 00_search_linkedin_guest.py --location "Melbourne, Victoria, Australia"
  .venv/bin/python 00_search_linkedin_guest.py --keywords "data analyst" "data engineer"
"""

from __future__ import annotations

import argparse
import csv
import html
import re
import time
from pathlib import Path

import csv_merge
from superpowered_crawler_final import SuperpoweredCrawlerFinal

SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"

DEFAULT_KEYWORDS = ["data analyst", "business analyst", "data engineer", "data scientist"]

OUT_COLUMNS = [
    "company", "role_title", "location", "job_url", "job_text",
    "required_skills", "preferred_skills", "notes",
    "posted_date", "search_keyword", "source",
]


def _clean(s: str) -> str:
    """Strip HTML tags + unescape entities -> plain text (full title, no truncation)."""
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()


def _grab(pattern: str, chunk: str) -> str:
    m = re.search(pattern, chunk, re.S)
    return _clean(m.group(1)) if m else ""


def parse_cards(html_text: str, keyword: str) -> list[dict]:
    """Parse each <li> job-card block as a unit so fields stay aligned."""
    cards = []
    for chunk in html_text.split("<li>")[1:]:
        m = re.search(r"jobPosting:(\d+)", chunk)
        if not m:
            continue
        jid = m.group(1)
        # Capture a time component when LinkedIn provides one (finer freshness);
        # date-only postings still match and behave exactly as before.
        date_m = re.search(r'datetime="([0-9]{4}-[0-9]{2}-[0-9]{2}(?:[T ][0-9:]{5,8})?)"', chunk)
        cards.append({
            "company": _grab(r'base-search-card__subtitle"[^>]*>(.*?)</h4', chunk),
            "role_title": _grab(r'base-search-card__title"[^>]*>(.*?)</h3', chunk),
            "location": _grab(r'job-search-card__location"[^>]*>(.*?)</span', chunk),
            "job_url": f"https://www.linkedin.com/jobs/view/{jid}",
            "job_text": "",
            "required_skills": "",
            "preferred_skills": "",
            "notes": "",
            "posted_date": (date_m.group(1).replace(" ", "T") if date_m else ""),
            "search_keyword": keyword,
            "source": "linkedin_guest_search",
        })
    return cards


def search_keyword(crawler, keyword, location, tpr, max_start, empties_stop, delay) -> list[dict]:
    found, start, consecutive_empty = [], 0, 0
    while start <= max_start:
        from urllib.parse import urlencode
        q = urlencode({"keywords": keyword, "location": location,
                       "f_TPR": tpr, "sortBy": "DD", "start": start})
        r = crawler.fetch(f"{SEARCH_URL}?{q}")
        text = getattr(r, "text", "") or ""
        cards = parse_cards(text, keyword)
        if cards:
            found += cards
            consecutive_empty = 0
        else:
            consecutive_empty += 1
            if consecutive_empty >= empties_stop:
                break
        start += 10
        time.sleep(delay)
    return found


def main():
    ap = argparse.ArgumentParser(description="LinkedIn guest job search -> job_alerts_raw.csv")
    ap.add_argument("--keywords", nargs="+", default=DEFAULT_KEYWORDS)
    ap.add_argument("--location", nargs="+", default=["Australia"],
                    help='One or more location strings (default ["Australia"] — scoring filters relevance).')
    ap.add_argument("--tpr", default="r86400",
                    help="Recency seconds: r900=15m r3600=1h r14400=4h r86400=24h r172800=48h r604800=7d.")
    ap.add_argument("--out", default="job_alerts_raw.csv")
    ap.add_argument("--max-start", type=int, default=250,
                    help="Max pagination offset per keyword (250 = ~26 pages).")
    ap.add_argument("--empties-stop", type=int, default=3,
                    help="Stop a keyword after this many consecutive empty pages.")
    ap.add_argument("--delay", type=float, default=1.3, help="Seconds between page requests.")
    ap.add_argument("--no-append", action="store_true",
                    help="Overwrite --out instead of merging into existing rows.")
    args = ap.parse_args()

    # Search result pages are recency-windowed (f_TPR) and the URL is byte-identical
    # run-to-run — caching them replays stale jobs and hides genuinely new roles.
    # Always fetch the search endpoint live. (Belt-and-suspenders: fetch() also
    # refuses to cache this endpoint regardless of this flag.)
    crawler = SuperpoweredCrawlerFinal(respect_robots=False, polite_delay=args.delay, rps=0.6,
                                       cache_enabled=False)

    all_cards = []
    for loc in args.location:
        for kw in args.keywords:
            cards = search_keyword(crawler, kw, loc, args.tpr,
                                   args.max_start, args.empties_stop, args.delay)
            print(f"[{loc} | {kw}] {len(cards)} cards")
            all_cards += cards

    # dedupe by canonical job id (roles overlap)
    deduped = {}
    for cd in all_cards:
        deduped.setdefault(csv_merge.row_key(cd), cd)
    new_rows = list(deduped.values())
    print(f"[total] {len(all_cards)} cards -> {len(new_rows)} unique jobs")

    out = Path(args.out)
    if out.exists() and not args.no_append:
        with out.open("r", encoding="utf-8-sig") as f:
            existing = list(csv.DictReader(f))
        before = len(existing)
        rows = csv_merge.merge_csv_on_key(existing, new_rows, csv_merge.row_key)
        print(f"[append] merged into {out} ({before} existing -> {len(rows)} total)")
    else:
        before = 0
        rows = new_rows

    # Machine-readable delta so the orchestrator can detect (and loudly surface)
    # a run that scraped jobs but added nothing new — never a silent SUCCESS again.
    appended = len(rows) - before
    print(f"[discover-delta] scraped_unique={len(new_rows)} existing={before} appended={appended}")
    if new_rows and appended == 0:
        print("[discover-warning] 0 NEW jobs added: every scraped job was already known. "
              "The search returned nothing new this run (no fresh postings in the recency window).")

    fieldnames = list(dict.fromkeys(OUT_COLUMNS + [k for r in rows for k in r.keys()]))
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    print(f"[done] wrote {len(rows)} rows -> {out}")
    print("Next: fill_missing_jds.py --input %s  ->  prepare -> score -> generate" % args.out)


if __name__ == "__main__":
    main()
