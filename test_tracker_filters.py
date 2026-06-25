"""Regression tests for tracker_groups filters + sorts + archiving.

Mirrors the manual filter audit: exercises every control alone and the
behaviours that were inconsistent (empty sort keys, A-Z direction,
needs-contacts meaning, auto-hide of closed/stale).
"""
import unittest, sqlite3, datetime
from app import db, queries


def iso(days_ago):
    return (datetime.datetime.now() - datetime.timedelta(days=days_ago)).isoformat(timespec="seconds")


class TestTrackerFilters(unittest.TestCase):
    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        db.init_schema(self.con)
        self._seed()

    def _job(self, company, role, posted_days, status=None, status_days=0, score=50):
        rk = f"{company}:{role}"
        self.con.execute(
            "INSERT INTO jobs(row_key,company,role_title,location,job_url,match_score,jd_posted_date)"
            " VALUES(?,?,?,?,?,?,?)",
            (rk, company, role, "Remote", "", score, iso(posted_days)[:10]))
        if status:
            self.con.execute(
                "INSERT INTO application_status(row_key,status,status_changed_at,source)"
                " VALUES(?,?,?,?)",
                (rk, status, iso(status_days), "manual"))
        return rk

    def _person(self, company, name, st, days):
        k = queries.normalize_company(company)
        self.con.execute(
            "INSERT INTO tracker_people(person_key,company_key,company_name,name,title,email,"
            "outreach_status,notes,needs_review,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (k + "/" + name, k, company, name, "Eng", name + "@x.com", st, "", 0, iso(days), iso(days)))

    def _seed(self):
        self._job("Alpha", "Eng", 2, "applied", 1)        # applied + people  -> active
        self._person("Alpha", "Ann", "contacted", 1)
        self._job("Bravo", "Eng", 2, "applied", 1)        # applied, no people -> active
        self._job("Cold Corp", "Eng", 2)                  # recent, no app, no people -> visible
        self._job("Stale Inc", "Eng", 30)                 # stale cold -> archived
        self._job("Closed Co", "Eng", 10, "closed", 5)    # all closed -> archived
        self._person("People Only", "Pat", "replied", 1)  # people, no jobs
        self.con.commit()

    def names(self, **kw):
        groups, stats = queries.tracker_groups(self.con, **kw)
        return [g["company_name"] for g in groups], stats

    def test_default_hides_stale_and_closed(self):
        names, stats = self.names()
        self.assertEqual(set(names), {"Alpha", "Bravo", "Cold Corp", "People Only"})
        self.assertEqual(stats["archived_count"], 2)

    def test_show_all_reveals_archived(self):
        names, _ = self.names(show_all=True)
        self.assertEqual(set(names),
                         {"Alpha", "Bravo", "Cold Corp", "Stale Inc", "Closed Co", "People Only"})

    def test_needs_contacts_is_any_jobs_no_people(self):
        names, _ = self.names(needs_contacts_only=True)
        self.assertEqual(set(names), {"Bravo", "Cold Corp", "Stale Inc"})

    def test_search_matches_company(self):
        names, _ = self.names(q="alpha")
        self.assertEqual(names, ["Alpha"])

    def test_search_matches_person(self):
        names, _ = self.names(q="pat")
        self.assertEqual(names, ["People Only"])

    def test_status_filter(self):
        names, _ = self.names(status="contacted")
        self.assertEqual(names, ["Alpha"])

    def test_app_status_applied(self):
        names, _ = self.names(app_status="applied")
        self.assertEqual(set(names), {"Alpha", "Bravo"})

    def test_app_status_closed_visible_when_filtered(self):
        names, _ = self.names(app_status="closed")
        self.assertIn("Closed Co", names)

    def test_company_sort_is_ascending(self):
        names, _ = self.names(show_all=True, sort="company")
        self.assertEqual(names, sorted(names, key=str.lower))

    def test_every_company_has_sort_keys(self):
        groups, _ = queries.tracker_groups(self.con, show_all=True, sort="added")
        self.assertTrue(all(g["added_key"] for g in groups), "added_key empty for some company")
        self.assertTrue(all(g["last_activity"] for g in groups), "last_activity empty for some company")

    def test_added_sort_recent_first(self):
        groups, _ = queries.tracker_groups(self.con, show_all=True, sort="added")
        keys = [g["added_key"] for g in groups]
        self.assertEqual(keys, sorted(keys, reverse=True))

    def test_combo_app_status_with_sort_added(self):
        # the originally-reported "applied only works with date added" combo
        names, _ = self.names(app_status="applied", sort="added")
        self.assertEqual(set(names), {"Alpha", "Bravo"})


if __name__ == "__main__":
    unittest.main()
