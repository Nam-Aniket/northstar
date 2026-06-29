"""test_seek_source.py — Seek -> job_alerts_raw mapping + best-effort contract."""
import unittest

import csv_merge
import seek_source

_SAMPLE = {
    "company_name": "Acme Analytics",
    "role_title": "Data Analyst",
    "posting_url": "https://www.seek.com.au/job/12345678",
    "posted_at_iso": "2026-06-28",
    "posted_relative": "1d ago",
    "location": "Melbourne VIC",
    "source": "seek",
    "is_recruiter": False,
    "raw_snippet": "Some description",
    "_keyword": "data analyst",
}


class TestSeekMapping(unittest.TestCase):
    def test_maps_to_canonical_columns(self):
        r = seek_source.seek_to_canonical(_SAMPLE)
        self.assertEqual(r["company"], "Acme Analytics")
        self.assertEqual(r["role_title"], "Data Analyst")
        self.assertEqual(r["location"], "Melbourne VIC")
        self.assertEqual(r["job_url"], "https://www.seek.com.au/job/12345678")
        self.assertEqual(r["posted_date"], "2026-06-28")
        self.assertEqual(r["search_keyword"], "data analyst")
        self.assertEqual(r["source"], "seek")
        self.assertEqual(r["job_text"], "")  # backfilled later by the fetch stage

    def test_row_key_is_stable_for_merge(self):
        r = seek_source.seek_to_canonical(_SAMPLE)
        self.assertTrue(csv_merge.row_key(r))  # non-empty -> dedups correctly

    def test_decodes_html_entities(self):
        r = seek_source.seek_to_canonical({
            "role_title": "BI Reporting &amp; Analytics",
            "company_name": "Smith &amp; Co",
            "posting_url": "https://www.seek.com.au/job/42?type=standard&amp;ref=x",
        })
        self.assertEqual(r["role_title"], "BI Reporting & Analytics")
        self.assertEqual(r["company"], "Smith & Co")
        self.assertEqual(r["job_url"], "https://www.seek.com.au/job/42?type=standard&ref=x")

    def test_handles_missing_fields(self):
        r = seek_source.seek_to_canonical({"role_title": "BI Analyst"})
        self.assertEqual(r["role_title"], "BI Analyst")
        self.assertEqual(r["company"], "")
        self.assertEqual(r["source"], "seek")


class _FakeScraper:
    def __init__(self, postings):
        self._postings = postings
        self.calls = []

    def scrape_seek(self, crawler, kw, loc, max_age_days, max_pages):
        self.calls.append((kw, loc))
        return list(self._postings)


class TestFetchBestEffort(unittest.TestCase):
    def test_maps_scraped_rows_with_injected_scraper(self):
        fake = _FakeScraper([_SAMPLE])
        rows = seek_source.fetch_seek_rows(
            ["data analyst"], ["Melbourne"], _scraper=fake, _crawler=object())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "seek")
        self.assertEqual(fake.calls, [("data analyst", "Melbourne")])

    def test_no_keywords_returns_empty_without_scraping(self):
        # No keywords -> the scrape loop never runs, so no network, no rows.
        fake = _FakeScraper([_SAMPLE])
        rows = seek_source.fetch_seek_rows([], [], _scraper=fake, _crawler=object())
        self.assertEqual(rows, [])
        self.assertEqual(fake.calls, [])

    def test_one_query_failing_is_non_fatal(self):
        class Boom:
            def scrape_seek(self, *a):
                raise RuntimeError("blocked")
        logs = []
        rows = seek_source.fetch_seek_rows(
            ["x"], ["y"], log=logs.append, _scraper=Boom(), _crawler=object())
        self.assertEqual(rows, [])
        self.assertTrue(any("seek-warning" in m for m in logs))


if __name__ == "__main__":
    unittest.main()
