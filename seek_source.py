"""seek_source.py — best-effort Seek.com.au discovery for the discover stage.

Maps Seek postings into the job_alerts_raw.csv column schema so they merge
alongside LinkedIn results (deduped by csv_merge.row_key, the fetch stage later
backfills job_text). Scraping is STRICTLY best-effort: any failure (robots-
disallowed, anti-bot block, changed markup, zero cards) logs a [seek-warning] and
returns what it has (often []), so the discover stage never fails because of Seek.

The pure mapping (seek_to_canonical) is unit-tested; the live scrape is not.

Note: Seek scraping respects robots.txt by default (respect_robots=True). If Seek
disallows crawling, this returns 0 — that is intended, not a bug.
"""
from __future__ import annotations

import html
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# job_alerts_raw.csv columns a Seek row must populate (others default to "").
_CANONICAL_KEYS = (
    "company", "role_title", "location", "job_url", "job_text",
    "required_skills", "preferred_skills", "notes",
    "posted_date", "search_keyword", "source",
)


def seek_to_canonical(s: dict) -> dict:
    """Map one scrape_seek() posting to the job_alerts_raw.csv column schema."""
    # Card scraping leaves HTML entities (e.g. "&amp;") in text and URLs; decode
    # them so titles read cleanly and the job_url is fetchable downstream.
    def _clean(v):
        return html.unescape((v or "").strip())

    row = {k: "" for k in _CANONICAL_KEYS}
    row["company"] = _clean(s.get("company_name"))
    row["role_title"] = _clean(s.get("role_title"))
    row["location"] = _clean(s.get("location"))
    row["job_url"] = _clean(s.get("posting_url"))
    row["posted_date"] = (s.get("posted_at_iso") or "").strip()
    row["search_keyword"] = (s.get("_keyword") or "").strip()
    row["source"] = "seek"
    return row


def _load_scraper():
    """Import the numeric-named 00a module (can't `import 00a...`)."""
    path = ROOT / "00a_scrape_job_postings.py"
    spec = importlib.util.spec_from_file_location("scrape_postings", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def fetch_seek_rows(keywords, locations, max_age_days: int = 3, max_pages: int = 2,
                    log=print, _scraper=None, _crawler=None) -> list[dict]:
    """Best-effort: scrape Seek for each keyword x location, mapped to canonical
    rows. Any error logs [seek-warning] and is swallowed. `_scraper`/`_crawler`
    are injection seams for tests."""
    rows: list[dict] = []
    try:
        mod = _scraper or _load_scraper()
        if _crawler is not None:
            crawler = _crawler
        else:
            from superpowered_crawler_final import SuperpoweredCrawlerFinal
            crawler = SuperpoweredCrawlerFinal(
                max_pages=1, use_search=False, rps=0.5, polite_delay=1.0,
                respect_robots=True, identify=False,
            )
        locs = list(locations) or ["Australia"]
        for kw in (keywords or []):
            for loc in locs:
                try:
                    for s in mod.scrape_seek(crawler, kw, loc, max_age_days, max_pages):
                        s = dict(s)
                        s["_keyword"] = kw
                        rows.append(seek_to_canonical(s))
                except Exception as e:  # one keyword/location failing is non-fatal
                    log(f"[seek-warning] {kw!r}@{loc!r}: {e}")
    except Exception as e:  # the whole Seek source being unavailable is non-fatal
        log(f"[seek-warning] Seek source unavailable: {e}")
    return rows
