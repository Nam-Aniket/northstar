"""test_biz_mode.py — Business mode (B2B) + job-side contact fixes.

CRITICAL ISOLATION: this points db.DB_PATH at a throwaway temp database BEFORE
importing the app, so the owner's real control_panel.db (and its contacts) are
never read or mutated by the suite. Example fixtures are forced for the banks.
"""
import os
import tempfile

os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")
os.environ.setdefault("JOBENGINE_FACTS", "facts.example.json")

import app.db as db

# Redirect to a temp DB *before* anything opens a connection.
_TMP = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_TMP.close()
db.DB_PATH = _TMP.name
db.init_schema(db.connect())

import unittest
import app.queries as q


def tearDownModule():
    try:
        os.unlink(_TMP.name)
    except OSError:
        pass


def _fresh_con():
    """Temp-DB connection with the suite's tables cleared. Tolerates tables that
    don't exist yet (so it works before/after the biz tables are added)."""
    con = db.connect()
    for t in ("tracker_people", "people", "manual_people",
              "biz_companies", "biz_prospects"):
        try:
            con.execute(f"DELETE FROM {t}")
        except Exception:
            pass
    con.commit()
    return con


# ── PART 2 — job-side fixes ────────────────────────────────────────────────────

class CompanyDetailTrackerPeopleTest(unittest.TestCase):
    def test_company_detail_includes_tracker_people(self):
        con = _fresh_con()
        now = q._now()
        con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, "
            "name, title, email, pattern, outreach_status, notes, needs_review, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("prophix|jane_doe", "prophix", "Prophix", "Jane Doe", "VP Data",
             "jane.doe@prophix.com", "{first}.{last}@prophix.com",
             "not_contacted", "", 0, now, now))
        con.commit()
        detail = q.company_detail(con, "prophix")
        names = [p["name"] for p in detail["people"]]
        emails = [p["email"] for p in detail["people"]]
        con.close()
        self.assertIn("Jane Doe", names)
        self.assertIn("jane.doe@prophix.com", emails)


class CompaniesWithContactsTest(unittest.TestCase):
    def test_stats_counts_companies_with_people(self):
        con = _fresh_con()
        now = q._now()
        con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, "
            "name, title, email, pattern, outreach_status, notes, needs_review, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("acme|sam_lee", "acme", "Acme", "Sam Lee", "Eng",
             "sam.lee@acme.com", "{first}.{last}@acme.com",
             "not_contacted", "", 0, now, now))
        con.commit()
        groups, stats = q.tracker_groups(con)
        con.close()
        self.assertEqual(stats["companies_with_people"], 1)


# ── PART 1 — business mode ─────────────────────────────────────────────────────

class BizSchemaTest(unittest.TestCase):
    def test_biz_tables_exist(self):
        con = db.connect()
        cols_c = {r[1] for r in con.execute("PRAGMA table_info(biz_companies)")}
        cols_p = {r[1] for r in con.execute("PRAGMA table_info(biz_prospects)")}
        con.close()
        self.assertTrue({"company_key", "company_name", "priority"} <= cols_c)
        self.assertTrue({"prospect_key", "company_key", "name", "email", "stage"} <= cols_p)


class IngestBizTest(unittest.TestCase):
    def test_ingest_creates_company_and_prospects(self):
        con = _fresh_con()
        people = [{"name": "Mia Stone", "title": "CFO",
                   "email": "mia.stone@acme.com", "pattern": "{first}.{last}@acme.com"}]
        counts = q.ingest_biz_prospects(con, "acme", "Acme Inc", people)
        comp = con.execute("SELECT * FROM biz_companies WHERE company_key='acme'").fetchone()
        pros = con.execute("SELECT stage, name FROM biz_prospects WHERE company_key='acme'").fetchone()
        con.close()
        self.assertEqual(counts["added"], 1)
        self.assertIsNotNone(comp)
        self.assertEqual(pros["stage"], "lead")
        self.assertEqual(pros["name"], "Mia Stone")


class BizGroupsTest(unittest.TestCase):
    def test_groups_and_summary(self):
        con = _fresh_con()
        q.ingest_biz_prospects(con, "acme", "Acme", [
            {"name": "Mia Stone", "title": "CFO", "email": "m@acme.com", "pattern": ""},
            {"name": "Ned Park", "title": "CTO", "email": "n@acme.com", "pattern": ""}])
        q.set_biz_stage(con, "acme|ned_park", "contacted")
        groups, summary = q.biz_groups(con)
        con.close()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["prospect_count"], 2)
        self.assertEqual(summary["lead"], 1)
        self.assertEqual(summary["contacted"], 1)


class BizMutationsTest(unittest.TestCase):
    def setUp(self):
        self.con = _fresh_con()
        q.ingest_biz_prospects(self.con, "acme", "Acme",
            [{"name": "Mia Stone", "title": "CFO", "email": "m@acme.com", "pattern": ""}])

    def tearDown(self):
        self.con.close()

    def test_set_stage(self):
        q.set_biz_stage(self.con, "acme|mia_stone", "meeting")
        self.assertEqual(q.get_biz_prospect(self.con, "acme|mia_stone")["stage"], "meeting")

    def test_set_stage_rejects_unknown(self):
        with self.assertRaises(ValueError):
            q.set_biz_stage(self.con, "acme|mia_stone", "bogus")

    def test_priority_toggle(self):
        q.set_biz_priority(self.con, "acme", True)
        g = {x["company_key"]: x for x in q.biz_groups(self.con)[0]}
        self.assertEqual(g["acme"]["priority"], 1)

    def test_notes(self):
        q.set_biz_prospect_notes(self.con, "acme|mia_stone", "warm intro via Sam")
        self.assertEqual(q.get_biz_prospect(self.con, "acme|mia_stone")["notes"], "warm intro via Sam")


class BizCsvTest(unittest.TestCase):
    def test_csv_import(self):
        con = _fresh_con()
        csv_text = (
            "company,name,title,email\n"
            "Acme Inc,Mia Stone,CFO,mia@acme.com\n"
            "Acme Inc,Ned Park,CTO,\n"
            "Globex,Ada Byte,CEO,ada@globex.com\n")
        counts = q.import_biz_csv(con, csv_text)
        n_companies = con.execute("SELECT COUNT(*) FROM biz_companies").fetchone()[0]
        n_prospects = con.execute("SELECT COUNT(*) FROM biz_prospects").fetchone()[0]
        con.close()
        self.assertEqual(n_companies, 2)
        self.assertEqual(n_prospects, 3)
        self.assertEqual(counts["needs_review"], 1)


class ModeIsolationTest(unittest.TestCase):
    def test_business_data_never_leaks_into_job_tracker(self):
        con = _fresh_con()
        q.ingest_biz_prospects(con, "globex", "Globex",
            [{"name": "Ada Byte", "title": "CEO", "email": "ada@globex.com", "pattern": ""}])
        groups, stats = q.tracker_groups(con)
        job_names = {p["name"] for g in groups for p in g.get("people", [])}
        detail = q.company_detail(con, "globex")
        con.close()
        self.assertNotIn("Ada Byte", job_names)
        self.assertNotIn("Ada Byte", [p["name"] for p in detail["people"]])


if __name__ == "__main__":
    unittest.main()
