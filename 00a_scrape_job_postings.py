#!/usr/bin/env python3
"""
Job Posting Scraper.

Finds companies that have posted Data/BI/Analytics roles in the last N days.
Two free sources: Seek.com.au and Indeed.com.au. Re-uses
SuperpoweredCrawlerFinal so we keep rate limits, robots, cache, and disguise.

Output: active_hirers.csv with one row per posting:
  company_name, role_title, posting_url, posted_at_iso, posted_relative,
  location, source, is_recruiter, raw_snippet
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import time
import urllib.parse
from pathlib import Path

from superpowered_crawler_final import SuperpoweredCrawlerFinal


# Known AU recruiter agencies - flag postings from them, don't treat as direct hirers.
RECRUITERS = {
    "hays", "robert half", "michael page", "hudson",
    "talenza", "bluefin", "clicks it", "halcyon knights",
    "randstad", "kelly services", "hudson rpo", "morgan mckinley",
    "the onset", "the recruitment company", "people group", "u&u",
    "people2people", "balance recruitment", "ignite", "paxus",
    "candle", "finite group", "ambit", "perigon",
}


def _normalize_company(name: str) -> str:
    """Cheap canonicalization for dedup. Keeps the printable name but normalizes for keys."""
    if not name:
        return ""
    n = name.strip()
    # Remove common suffixes for the dedup KEY only
    n = re.sub(r"\s+(pty\s*ltd|pty|ltd|limited|inc|llc|p/l)\.?$", "", n, flags=re.IGNORECASE)
    return n.strip()


def _is_recruiter(company_name: str) -> bool:
    if not company_name:
        return False
    low = company_name.lower()
    return any(r in low for r in RECRUITERS)


def _parse_relative_date(text: str, now: dt.datetime | None = None) -> str | None:
    """
    Convert 'Listed three days ago', '2d ago', 'Posted 14 days ago' to an ISO date.
    Returns None if no match.
    """
    if not text:
        return None
    now = now or dt.datetime.now()
    t = text.lower()
    # word-number → digit
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14}
    for w, n in words.items():
        t = re.sub(rf"\b{w}\b", str(n), t)

    m = re.search(r"(\d{1,3})\s*(d|day|days|h|hour|hours|m|min|minute|minutes)\b", t)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith("h"):
            delta = dt.timedelta(hours=n)
        elif unit.startswith("m"):
            delta = dt.timedelta(minutes=n)
        else:
            delta = dt.timedelta(days=n)
        return (now - delta).date().isoformat()
    if "just posted" in t or "moments ago" in t or "today" in t:
        return now.date().isoformat()
    if "yesterday" in t:
        return (now - dt.timedelta(days=1)).date().isoformat()
    return None


# ---------------------------------------------------------------------------
# Seek scraper
# ---------------------------------------------------------------------------

def scrape_seek(
    crawler: SuperpoweredCrawlerFinal,
    query: str,
    location: str,
    max_age_days: int,
    max_pages: int,
) -> list[dict]:
    """Scrape Seek search results pages. Returns one dict per posting."""
    out: list[dict] = []
    q = urllib.parse.quote_plus(query.replace(" ", "-"))
    loc = urllib.parse.quote_plus(location.replace(" ", "-"))
    daterange = min(max(max_age_days, 1), 31)

    for page in range(1, max_pages + 1):
        url = (
            f"https://www.seek.com.au/{q}-jobs/in-{loc}"
            f"?daterange={daterange}&page={page}"
        )
        print(f"  [seek] page {page}: {url}")
        fetch = crawler.fetch(url, max_bytes=1_500_000)
        if not fetch.text or fetch.status >= 400:
            print(f"    [seek] no content ({fetch.status})")
            break

        # Seek embeds a big JSON blob with the results - find the JobPosting JSON-LD if present
        structured = crawler.extract_structured_data(fetch.text)
        if structured.get("jobs"):
            for j in structured["jobs"]:
                out.append({
                    "company_name": "",  # JSON-LD JobPosting often omits company at search level
                    "role_title": j.get("title", ""),
                    "posting_url": j.get("url", "") or url,
                    "posted_at_iso": (j.get("date_posted") or "")[:10],
                    "posted_relative": "",
                    "location": location,
                    "source": "seek:jsonld",
                    "is_recruiter": False,
                    "raw_snippet": (j.get("description") or "")[:200],
                })

        # Fallback: scrape HTML cards (more reliable for company name + relative date)
        # Seek job cards: <article data-automation="normalJob"> ... </article>
        cards = re.findall(
            r'<article[^>]*data-automation="normalJob"[^>]*>(.*?)</article>',
            fetch.text, flags=re.DOTALL | re.IGNORECASE,
        )
        if not cards:
            # newer markup: data-card-type="JobCard"
            cards = re.findall(
                r'<div[^>]*data-card-type="JobCard"[^>]*>(.*?)</div>\s*</div>\s*</div>',
                fetch.text, flags=re.DOTALL | re.IGNORECASE,
            )
        for card in cards:
            title_m = re.search(r'data-automation="jobTitle"[^>]*>([^<]+)<', card)
            company_m = re.search(r'data-automation="jobCompany"[^>]*>([^<]+)<', card)
            location_m = re.search(r'data-automation="jobLocation"[^>]*>([^<]+)<', card)
            href_m = re.search(r'href="(/job/[^"]+)"', card)
            listed_m = re.search(r'data-automation="jobListingDate"[^>]*>([^<]+)<', card)

            company = (company_m.group(1) if company_m else "").strip()
            if not company:
                continue

            relative = listed_m.group(1).strip() if listed_m else ""
            posted_iso = _parse_relative_date(relative) or ""
            full_url = "https://www.seek.com.au" + href_m.group(1) if href_m else url

            out.append({
                "company_name": company,
                "role_title": (title_m.group(1) if title_m else "").strip(),
                "posting_url": full_url,
                "posted_at_iso": posted_iso,
                "posted_relative": relative,
                "location": (location_m.group(1) if location_m else location).strip(),
                "source": "seek",
                "is_recruiter": _is_recruiter(company),
                "raw_snippet": "",
            })

        if not cards:
            print(f"    [seek] no cards parsed (markup may have changed)")
            break

    return out


# ---------------------------------------------------------------------------
# Indeed scraper
# ---------------------------------------------------------------------------

def scrape_indeed(
    crawler: SuperpoweredCrawlerFinal,
    query: str,
    location: str,
    max_age_days: int,
    max_pages: int,
) -> list[dict]:
    """Scrape Indeed AU search pages. Returns one dict per posting."""
    out: list[dict] = []
    q = urllib.parse.quote_plus(query)
    loc = urllib.parse.quote_plus(location)
    fromage = min(max(max_age_days, 1), 30)

    for page in range(0, max_pages):
        start = page * 10
        url = f"https://au.indeed.com/jobs?q={q}&l={loc}&fromage={fromage}&start={start}"
        print(f"  [indeed] page {page+1}: {url}")
        fetch = crawler.fetch(url, max_bytes=1_500_000)
        if not fetch.text or fetch.status >= 400:
            print(f"    [indeed] no content ({fetch.status})")
            break

        # Indeed job cards contain a JSON blob with detailed posting info
        json_blob_m = re.search(r'window\.mosaic\.providerData\["mosaic-provider-jobcards"\]\s*=\s*({.*?});\s*</script>',
                                fetch.text, flags=re.DOTALL)
        if json_blob_m:
            try:
                blob = json.loads(json_blob_m.group(1))
                results = blob.get("metaData", {}).get("mosaicProviderJobCardsModel", {}).get("results", [])
                for r in results:
                    company = (r.get("company") or "").strip()
                    if not company:
                        continue
                    job_key = r.get("jobkey") or ""
                    posted_iso = ""
                    age_s = r.get("formattedRelativeTime") or ""
                    posted_iso = _parse_relative_date(age_s) or ""
                    out.append({
                        "company_name": company,
                        "role_title": r.get("title", ""),
                        "posting_url": f"https://au.indeed.com/viewjob?jk={job_key}" if job_key else url,
                        "posted_at_iso": posted_iso,
                        "posted_relative": age_s,
                        "location": r.get("formattedLocation") or location,
                        "source": "indeed",
                        "is_recruiter": _is_recruiter(company),
                        "raw_snippet": (r.get("snippet") or "")[:200],
                    })
                continue
            except Exception as exc:
                print(f"    [indeed] JSON parse failed: {exc}")

        # Fallback: scrape HTML cards
        cards = re.findall(r'<a[^>]*class="[^"]*tapItem[^"]*"[^>]*>(.*?)</a>', fetch.text, flags=re.DOTALL)
        for card in cards:
            company_m = re.search(r'companyName[^>]*>([^<]+)<', card)
            title_m = re.search(r'jobTitle[^>]*>([^<]+)<', card)
            if not company_m:
                continue
            company = company_m.group(1).strip()
            out.append({
                "company_name": company,
                "role_title": (title_m.group(1) if title_m else "").strip(),
                "posting_url": url,
                "posted_at_iso": "",
                "posted_relative": "",
                "location": location,
                "source": "indeed:html",
                "is_recruiter": _is_recruiter(company),
                "raw_snippet": "",
            })

    return out


# ---------------------------------------------------------------------------
# Merge + dedup
# ---------------------------------------------------------------------------

def dedupe_postings(postings: list[dict]) -> list[dict]:
    """Dedup by (normalized_company, role_title). Keep richest record."""
    by_key: dict[tuple[str, str], dict] = {}
    for p in postings:
        key = (_normalize_company(p["company_name"]).lower(), (p.get("role_title") or "").lower())
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = dict(p)
            continue
        # Merge - prefer the one with a parsed date
        if not existing.get("posted_at_iso") and p.get("posted_at_iso"):
            existing["posted_at_iso"] = p["posted_at_iso"]
            existing["posted_relative"] = p["posted_relative"]
        if not existing.get("posting_url") and p.get("posting_url"):
            existing["posting_url"] = p["posting_url"]
        existing["source"] = ",".join(sorted(set([existing["source"], p["source"]])))
    return list(by_key.values())


def main():
    ap = argparse.ArgumentParser(description="Find companies with fresh Data/BI postings.")
    ap.add_argument("--queries", nargs="+", default=[
        "data analyst", "data scientist", "data engineer",
        "bi analyst", "power bi", "analytics engineer",
        "business intelligence", "insights analyst",
    ], help="Search queries (one per call).")
    ap.add_argument("--location", default="Melbourne")
    ap.add_argument("--max-age-days", type=int, default=14)
    ap.add_argument("--max-pages", type=int, default=3, help="Pages per query per source.")
    ap.add_argument("--sources", default="seek,indeed", help="Comma-separated: seek,indeed.")
    ap.add_argument("--output", default="active_hirers.csv")
    ap.add_argument("--rps", type=float, default=0.5,
                    help="Job boards are sensitive; default is half-rate.")
    args = ap.parse_args()

    crawler = SuperpoweredCrawlerFinal(
        max_pages=1, use_search=False,
        rps=args.rps, polite_delay=1.0,
        respect_robots=True, identify=False,
    )
    sources = [s.strip().lower() for s in args.sources.split(",") if s.strip()]

    all_postings: list[dict] = []
    for query in args.queries:
        print(f"\n=== query: {query!r} in {args.location} (last {args.max_age_days}d) ===")
        if "seek" in sources:
            all_postings.extend(scrape_seek(crawler, query, args.location, args.max_age_days, args.max_pages))
        if "indeed" in sources:
            all_postings.extend(scrape_indeed(crawler, query, args.location, args.max_age_days, args.max_pages))

    print(f"\n[merge] {len(all_postings)} raw postings")
    deduped = dedupe_postings(all_postings)
    print(f"[merge] {len(deduped)} after dedup")

    cols = ["company_name", "role_title", "posting_url", "posted_at_iso",
            "posted_relative", "location", "source", "is_recruiter", "raw_snippet"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for p in deduped:
            w.writerow({k: p.get(k, "") for k in cols})

    by_source = {}
    recruiter_count = sum(1 for p in deduped if p.get("is_recruiter"))
    for p in deduped:
        by_source[p["source"]] = by_source.get(p["source"], 0) + 1

    print(f"\n[done] {len(deduped)} postings -> {args.output}")
    print(f"  by source: {by_source}")
    print(f"  recruiters: {recruiter_count} ({recruiter_count*100//max(len(deduped),1)}%)")


if __name__ == "__main__":
    main()
