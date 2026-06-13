#!/usr/bin/env python3
"""
Personalization Scraper.

Reads job_outreach_people.csv (output of job_outreach_pipeline.py) and adds
ONE strong personalization signal per person. Re-uses the SuperpoweredCrawlerFinal
for politeness, caching, disguise, and PDF/JSON-LD parsing.

Signal priority (first hit wins):
  1. recent LinkedIn post:  site:linkedin.com/posts "Name"
  2. talk / podcast / panel: "Name" speaker OR talk OR podcast
  3. blog / Medium / Substack: "Name" "Company" (blog OR medium OR substack)
  4. news mention:           "Name" "Company" (announced OR launched OR promoted)

Output: <prefix>_people_personalised.csv with two new columns:
  personalization_signal       short paraphrase ready to drop into an email
  signal_source                URL the signal came from
"""

from __future__ import annotations

import argparse
import csv
import re
import urllib.parse
from pathlib import Path

from superpowered_crawler_final import SuperpoweredCrawlerFinal


SEARCH_ENGINES = [
    ("brave", "https://search.brave.com/search?"),
    ("bing", "https://www.bing.com/search?"),
]

# Stop-words that mean the snippet is generic "X works at Y" boilerplate, not a real signal.
GENERIC = {
    "view profile", "see who you know", "join to see", "join now", "log in",
    "sign in", "create your free", "click here", "linkedin", "located in",
}


def _ddg_search(crawler: SuperpoweredCrawlerFinal, query: str, max_results: int = 5) -> list[tuple[str, str]]:
    """
    Returns a list of (href, snippet) from a web search.
    Queries crawler.search_web which handles API keys (Google/Brave) or HTML fallback.
    """
    try:
        results = crawler.search_web(query, max_results=max_results)
    except Exception as e:
        print(f"[personalization] search_web failed for query '{query}': {e}")
        return []

    out: list[tuple[str, str]] = []
    for item in results:
        href = item.get("link", "")
        title = item.get("title", "").strip()
        snippet = item.get("snippet", "").strip()
        combined = f"{title}. {snippet}" if snippet else title
        if href and combined.strip():
            out.append((href, combined))
    return out


def _is_real_signal(snippet: str) -> bool:
    if not snippet or len(snippet) < 30:
        return False
    low = snippet.lower()
    return not any(g in low for g in GENERIC)


def _condense(snippet: str, max_chars: int = 220) -> str:
    s = snippet.strip()
    if len(s) <= max_chars:
        return s
    cut = s[:max_chars]
    # try to end on a sentence boundary
    for sep in (". ", "! ", "? "):
        i = cut.rfind(sep)
        if i > max_chars * 0.6:
            return cut[: i + 1].strip()
    return cut.rstrip() + "..."


def find_personalization_signal(
    crawler: SuperpoweredCrawlerFinal, name: str, company: str
) -> tuple[str, str]:
    """Return (signal, source_url) or ('', '') if nothing useful."""
    if not name:
        return "", ""

    queries = [
        # 1. Recent LinkedIn posts (high signal)
        f'site:linkedin.com/posts "{name}"',
        # 2. Speaking / podcasts
        f'"{name}" ({company}) (speaker OR talk OR podcast OR panel)',
        # 3. Blog / Medium / Substack
        f'"{name}" {company} (medium.com OR substack.com OR blog)',
        # 4. News / promotions / launches
        f'"{name}" {company} (announced OR launched OR promoted OR joined OR appointed)',
    ]

    for q in queries:
        try:
            results = _ddg_search(crawler, q, max_results=4)
        except Exception:
            results = []
        for href, snippet in results:
            if _is_real_signal(snippet):
                return _condense(snippet), href
    return "", ""


def main():
    ap = argparse.ArgumentParser(description="Adds one personalization signal per person.")
    ap.add_argument("--input", required=True, help="job_outreach_people.csv")
    ap.add_argument("--output", required=True, help="Output CSV (people + signal columns)")
    ap.add_argument("--rps", type=float, default=1.0)
    ap.add_argument("--workers", type=int, default=1, help="Keep at 1 - DDG bans on parallel hits.")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N rows (0 = all)")
    args = ap.parse_args()

    crawler = SuperpoweredCrawlerFinal(
        max_pages=1,
        use_search=True,
        rps=args.rps,
        polite_delay=0.7,
        respect_robots=True,
        identify=False,  # DDG is fine with rotating UAs
    )

    with open(args.input, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[: args.limit]

    out_rows = []
    print(f"[plan] {len(rows)} people")
    for i, row in enumerate(rows, 1):
        name = row.get("name", "").strip()
        company = row.get("company_name", "").strip()
        signal, source = find_personalization_signal(crawler, name, company)
        row["personalization_signal"] = signal
        row["signal_source"] = source
        print(f"  [{i}/{len(rows)}] {name} @ {company}: {'OK' if signal else 'no signal'}")
        out_rows.append(row)

    # Preserve original columns + add the two new ones at the end if missing.
    fieldnames = list(rows[0].keys()) if rows else []
    for extra in ("personalization_signal", "signal_source"):
        if extra not in fieldnames:
            fieldnames.append(extra)

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in out_rows:
            w.writerow(r)

    hit = sum(1 for r in out_rows if r.get("personalization_signal"))
    print(f"\n[done] {hit}/{len(out_rows)} got a signal -> {args.output}")


if __name__ == "__main__":
    main()
