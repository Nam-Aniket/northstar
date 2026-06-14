"""Regression guard for the core onboarding->search wiring.

The Run pipeline's discover stage MUST search using the user's saved tracked
roles, locations, and recency -- not 00_search_linkedin_guest.py's hardcoded
defaults. This silently regressed once (the form was saved but ignored); these
tests fail loudly if the wiring breaks again.

Run:  python -m unittest test_onboarding_search_wiring   (from project root)
"""
import os
import sqlite3
import sys
import tempfile
import unittest

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app import db, queries
import daily_run


class OnboardingSearchWiring(unittest.TestCase):
    def setUp(self):
        # Isolated temp-file DB (file-based so it survives across connections,
        # the way _discover_args opens its own connection).
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        def _connect():
            c = sqlite3.connect(self.path)
            c.row_factory = sqlite3.Row
            return c

        self._orig_connect = db.connect
        self._orig_write_config = queries.write_config
        db.connect = _connect
        queries.write_config = lambda con: None  # isolate from the real config.json

        con = db.connect()
        db.init_schema(con)
        con.close()

    def tearDown(self):
        db.connect = self._orig_connect
        queries.write_config = self._orig_write_config
        os.unlink(self.path)

    def test_locations_add_dedup_remove(self):
        con = db.connect()
        queries.add_locations(con, "Melbourne, Sydney, Melbourne")
        self.assertEqual(
            [l["display"] for l in queries.list_locations(con)], ["Melbourne", "Sydney"]
        )
        queries.remove_location(con, "sydney")
        self.assertEqual([l["display"] for l in queries.list_locations(con)], ["Melbourne"])
        con.close()

    def test_discover_args_uses_saved_form(self):
        con = db.connect()
        queries.get_onboarding(con)  # page load creates the singleton row (as the app does)
        queries.add_positions(con, "Data Analyst, Business Analyst")
        queries.add_locations(con, "Melbourne, Sydney")
        queries.set_recency(con, "r604800")
        con.close()

        args = daily_run._discover_args()
        # roles
        self.assertIn("--keywords", args)
        self.assertIn("Data Analyst", args)
        self.assertIn("Business Analyst", args)
        # locations
        self.assertIn("--location", args)
        self.assertIn("Melbourne", args)
        self.assertIn("Sydney", args)
        # recency carried through, not the r86400 default
        tpr_idx = args.index("--tpr")
        self.assertEqual(args[tpr_idx + 1], "r604800")
        # >1 location triggers the pagination volume cap
        self.assertIn("--max-start", args)

    def test_discover_args_empty_db_falls_back_to_defaults(self):
        # No roles/locations saved -> no --keywords/--location flags, so
        # 00_search uses its own defaults and the run still works.
        args = daily_run._discover_args()
        self.assertNotIn("--keywords", args)
        self.assertNotIn("--location", args)
        self.assertEqual(args, ["--tpr", "r86400"])

    def test_single_location_has_no_max_start_cap(self):
        con = db.connect()
        queries.add_positions(con, "Data Analyst")
        queries.add_locations(con, "Melbourne")
        con.close()
        args = daily_run._discover_args()
        self.assertNotIn("--max-start", args)


if __name__ == "__main__":
    unittest.main()
