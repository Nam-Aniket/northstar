"""test_tracker_queries.py — Phase 2 query layer tests."""
import unittest
import sqlite3
from app.db import init_schema
from app import queries


class TestCompanyCore(unittest.TestCase):
    """Test company_core function for fuzzy matching."""

    def test_basic_core(self):
        """Test basic company name normalization."""
        self.assertEqual(queries.company_core("Acme Corp"), "acme")

    def test_strips_pty_ltd(self):
        """Test that 'Pty Ltd' is stripped."""
        self.assertEqual(queries.company_core("Calrom Pty Ltd"), "calrom")

    def test_strips_limited(self):
        """Test that 'Limited' is stripped."""
        self.assertEqual(queries.company_core("Acme Limited"), "acme")

    def test_strips_inc(self):
        """Test that 'Inc' is stripped."""
        self.assertEqual(queries.company_core("Microsoft Inc"), "microsoft")

    def test_strips_llc(self):
        """Test that 'LLC' is stripped."""
        self.assertEqual(queries.company_core("Tech Solutions LLC"), "tech")

    def test_strips_multiple_suffixes(self):
        """Test stripping multiple combined suffixes."""
        self.assertEqual(queries.company_core("Acme Corp Ltd"), "acme")

    def test_punctuation_removed(self):
        """Test that punctuation is removed."""
        self.assertEqual(queries.company_core("Acme, Inc."), "acme")

    def test_normalizes_whitespace(self):
        """Test whitespace normalization."""
        self.assertEqual(queries.company_core("Acme  Corp"), "acme")

    def test_case_insensitive(self):
        """Test case insensitivity."""
        self.assertEqual(queries.company_core("ACME CORP"), "acme")


class TestResolveCompany(unittest.TestCase):
    """Test resolve_company fuzzy matching."""

    def setUp(self):
        """Create a fresh in-memory DB for each test."""
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        init_schema(self.con)

    def tearDown(self):
        """Close the connection."""
        self.con.close()

    def test_exact_match(self):
        """Test exact match (normalize_company match)."""
        self.con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, name, email) VALUES (?, ?, ?, ?, ?)",
            ("acme|alice", "acme", "Acme Corp", "Alice", "alice@acme.com")
        )
        self.con.commit()

        candidates = [{"company_key": "acme", "company_name": "Acme Corp"}]
        key, name, match_type = queries.resolve_company("acme corp", candidates)
        self.assertEqual(key, "acme")
        self.assertEqual(match_type, "exact")

    def test_suffix_match(self):
        """Test suffix match (company_core match)."""
        candidates = [{"company_key": "calrom", "company_name": "Calrom"}]
        key, name, match_type = queries.resolve_company("Calrom Pty Ltd", candidates)
        self.assertEqual(key, "calrom")
        self.assertEqual(match_type, "suffix")

    def test_no_false_merge_different_cores(self):
        """Test that Calcom does NOT merge into Calrom (different cores)."""
        candidates = [{"company_key": "calrom", "company_name": "Calrom"}]
        key, name, match_type = queries.resolve_company("Calcom", candidates)
        # Should NOT match (different cores), so create new
        self.assertNotEqual(key, "calrom")
        self.assertEqual(match_type, "new")

    def test_fuzzy_match_high_threshold(self):
        """Test fuzzy match above 0.90 threshold."""
        candidates = [{"company_key": "acme", "company_name": "Acme Inc"}]
        # Very similar name (just "Inc" diff, but that's a suffix)
        key, name, match_type = queries.resolve_company("Acme Incorporated", candidates)
        # Should be suffix match (both strip to same core) or fuzzy
        self.assertIn(match_type, ["suffix", "fuzzy"])

    def test_fuzzy_no_match_low_threshold(self):
        """Test that very different names don't fuzzy match."""
        candidates = [{"company_key": "google", "company_name": "Google"}]
        key, name, match_type = queries.resolve_company("Apple", candidates)
        # Should be new (no match at all)
        self.assertEqual(match_type, "new")

    def test_new_company(self):
        """Test creating a new company when no match."""
        candidates = []
        key, name, match_type = queries.resolve_company("Brand New Corp", candidates)
        self.assertEqual(match_type, "new")
        self.assertIsNotNone(key)


class TestIngestPeople(unittest.TestCase):
    """Test ingest_people upsert logic."""

    def setUp(self):
        """Create a fresh in-memory DB for each test."""
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        init_schema(self.con)

    def tearDown(self):
        """Close the connection."""
        self.con.close()

    def test_ingest_new_people(self):
        """Test ingesting new people."""
        people = [
            {"name": "Alice Smith", "title": "Engineer", "email": "alice@acme.com"},
            {"name": "Bob Jones", "title": "Manager", "email": "bob@acme.com"},
        ]
        result = queries.ingest_people(self.con, "acme", "Acme Corp", people)
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["updated"], 0)

        # Verify rows exist
        rows = self.con.execute("SELECT COUNT(*) FROM tracker_people").fetchone()[0]
        self.assertEqual(rows, 2)

    def test_ingest_preserves_status_on_reupload(self):
        """Test that re-upload preserves outreach_status and notes."""
        # Initial ingest
        people = [
            {"name": "Alice Smith", "title": "Engineer", "email": "alice@acme.com"},
        ]
        queries.ingest_people(self.con, "acme", "Acme Corp", people)

        # Update status manually
        self.con.execute(
            "UPDATE tracker_people SET outreach_status = 'contacted', notes = 'Very interested' WHERE email = 'alice@acme.com'"
        )
        self.con.commit()

        # Re-ingest same person with different title
        people_v2 = [
            {"name": "Alice Smith", "title": "Senior Engineer", "email": "alice@acme.com"},
        ]
        result = queries.ingest_people(self.con, "acme", "Acme Corp", people_v2)
        self.assertEqual(result["updated"], 1)

        # Verify status and notes are preserved, but title is updated
        row = self.con.execute(
            "SELECT title, outreach_status, notes FROM tracker_people WHERE email = 'alice@acme.com'"
        ).fetchone()
        self.assertEqual(row["title"], "Senior Engineer")  # Updated
        self.assertEqual(row["outreach_status"], "contacted")  # Preserved
        self.assertEqual(row["notes"], "Very interested")  # Preserved

    def test_ingest_dedup_within_paste_on_email(self):
        """Test that duplicates on same email are skipped."""
        people = [
            {"name": "Alice Smith", "title": "Engineer", "email": "alice@acme.com"},
            {"name": "Alice Smith", "title": "Engineer", "email": "alice@acme.com"},
        ]
        result = queries.ingest_people(self.con, "acme", "Acme Corp", people)
        self.assertEqual(result["added"], 1)
        self.assertEqual(result["skipped"], 1)

        rows = self.con.execute("SELECT COUNT(*) FROM tracker_people").fetchone()[0]
        self.assertEqual(rows, 1)

    def test_ingest_with_needs_review(self):
        """Test ingesting people marked as needs_review."""
        people = [
            {"name": "M. Tariq", "title": "Engineer", "email": "m.tariq@acme.com", "needs_review": 1},
        ]
        result = queries.ingest_people(self.con, "acme", "Acme Corp", people)
        self.assertEqual(result["needs_review"], 1)

        row = self.con.execute("SELECT needs_review FROM tracker_people WHERE email = 'm.tariq@acme.com'").fetchone()
        self.assertEqual(row["needs_review"], 1)


class TestTrackerTable(unittest.TestCase):
    """Test tracker_table query with jobs and company placeholders."""

    def setUp(self):
        """Create a fresh in-memory DB for each test."""
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        init_schema(self.con)

    def tearDown(self):
        """Close the connection."""
        self.con.close()

    def test_tracker_table_people_only(self):
        """Test loading tracker_people into table."""
        self.con.execute("""
            INSERT INTO tracker_people (person_key, company_key, company_name, name, title, email, outreach_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("acme|alice", "acme", "Acme Corp", "Alice Smith", "Engineer", "alice@acme.com", "contacted"))
        self.con.commit()

        rows = queries.tracker_table(self.con, q=None, company=None, status=None, sort=None, dir=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Alice Smith")
        self.assertEqual(rows[0]["outreach_status"], "contacted")

    def test_tracker_table_with_jobs(self):
        """Test that jobs are attached to people."""
        self.con.execute("""
            INSERT INTO tracker_people (person_key, company_key, company_name, name, title, email)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("acme|alice", "acme", "Acme Corp", "Alice Smith", "Engineer", "alice@acme.com"))
        
        self.con.execute("""
            INSERT INTO jobs (row_key, company, role_title)
            VALUES (?, ?, ?)
        """, ("job1", "acme", "Senior Engineer"))
        
        self.con.execute("""
            INSERT INTO application_status (row_key, status)
            VALUES (?, ?)
        """, ("job1", "applied"))
        
        self.con.commit()

        rows = queries.tracker_table(self.con, q=None, company=None, status=None, sort=None, dir=None)
        self.assertEqual(len(rows), 1)
        # Jobs should be attached (implementation will show structure)
        self.assertIn("jobs", rows[0])

    def test_placeholder_for_company_with_jobs_no_people(self):
        """Test that a company with jobs but no people gets a placeholder row."""
        # Add a job
        self.con.execute("""
            INSERT INTO jobs (row_key, company, role_title)
            VALUES (?, ?, ?)
        """, ("job1", "acme", "Senior Engineer"))
        
        self.con.commit()

        rows = queries.tracker_table(self.con, q=None, company=None, status=None, sort=None, dir=None)
        
        # Should have one placeholder row
        self.assertEqual(len(rows), 1)
        self.assertIn("is_placeholder", rows[0])
        self.assertTrue(rows[0]["is_placeholder"])

    def test_no_placeholder_for_company_no_jobs(self):
        """Test that a company with no people and no jobs gets no row."""
        # Add a company to tracker_people (will have people)
        self.con.execute("""
            INSERT INTO tracker_people (person_key, company_key, company_name, name, title, email)
            VALUES (?, ?, ?, ?, ?, ?)
        """, ("acme|alice", "acme", "Acme Corp", "Alice Smith", "Engineer", "alice@acme.com"))
        self.con.commit()

        rows = queries.tracker_table(self.con, q=None, company=None, status=None, sort=None, dir=None)
        
        # Should only have the one person, no placeholder
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0].get("is_placeholder", False))

    def test_filter_by_company(self):
        """Test filtering by company."""
        self.con.execute("""
            INSERT INTO tracker_people (person_key, company_key, company_name, name, title, email)
            VALUES 
                (?, ?, ?, ?, ?, ?),
                (?, ?, ?, ?, ?, ?)
        """, 
        ("acme|alice", "acme", "Acme Corp", "Alice Smith", "Engineer", "alice@acme.com",
         "google|bob", "google", "Google Inc", "Bob Jones", "Manager", "bob@google.com"))
        self.con.commit()

        rows = queries.tracker_table(self.con, q=None, company="acme", status=None, sort=None, dir=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["company_key"], "acme")

    def test_filter_by_status(self):
        """Test filtering by outreach status."""
        self.con.execute("""
            INSERT INTO tracker_people (person_key, company_key, company_name, name, title, email, outreach_status)
            VALUES 
                (?, ?, ?, ?, ?, ?, ?),
                (?, ?, ?, ?, ?, ?, ?)
        """, 
        ("acme|alice", "acme", "Acme Corp", "Alice Smith", "Engineer", "alice@acme.com", "contacted",
         "acme|bob", "acme", "Acme Corp", "Bob Jones", "Manager", "bob@acme.com", "not_contacted"))
        self.con.commit()

        rows = queries.tracker_table(self.con, q=None, company=None, status="contacted", sort=None, dir=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Alice Smith")

    def test_search_by_name(self):
        """Test searching by name."""
        self.con.execute("""
            INSERT INTO tracker_people (person_key, company_key, company_name, name, title, email)
            VALUES 
                (?, ?, ?, ?, ?, ?),
                (?, ?, ?, ?, ?, ?)
        """, 
        ("acme|alice", "acme", "Acme Corp", "Alice Smith", "Engineer", "alice@acme.com",
         "acme|bob", "acme", "Acme Corp", "Bob Jones", "Manager", "bob@acme.com"))
        self.con.commit()

        rows = queries.tracker_table(self.con, q="Alice", company=None, status=None, sort=None, dir=None)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "Alice Smith")


class TestSetTrackerPersonStatus(unittest.TestCase):
    """Test updating person outreach status."""

    def setUp(self):
        """Create a fresh in-memory DB for each test."""
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        init_schema(self.con)

    def tearDown(self):
        """Close the connection."""
        self.con.close()

    def test_set_person_status(self):
        """Test updating person status."""
        self.con.execute("""
            INSERT INTO tracker_people (person_key, company_key, company_name, name, title, email, outreach_status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, ("acme|alice", "acme", "Acme Corp", "Alice Smith", "Engineer", "alice@acme.com", "not_contacted"))
        self.con.commit()

        queries.set_tracker_person_status(self.con, "acme|alice", "contacted")

        row = self.con.execute("SELECT outreach_status FROM tracker_people WHERE person_key = 'acme|alice'").fetchone()
        self.assertEqual(row["outreach_status"], "contacted")


class TestCompanySuggestions(unittest.TestCase):
    """Test company suggestions endpoint."""

    def setUp(self):
        """Create a fresh in-memory DB for each test."""
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        init_schema(self.con)

    def tearDown(self):
        """Close the connection."""
        self.con.close()

    def test_company_suggestions_basic(self):
        """Test getting company suggestions."""
        self.con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, name, email) VALUES (?, ?, ?, ?, ?)",
            ("acme|alice", "acme", "Acme Corp", "Alice", "alice@acme.com")
        )
        self.con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, name, email) VALUES (?, ?, ?, ?, ?)",
            ("acme|bob", "acme", "Acme Corp", "Bob", "bob@acme.com")
        )
        self.con.commit()

        suggestions = queries.company_suggestions(self.con, "acm")
        # Should find Acme Corp
        self.assertTrue(any(s["company_key"] == "acme" for s in suggestions))

    def test_company_suggestions_deduplicated(self):
        """Test that duplicate company keys are deduplicated."""
        self.con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, name, email) VALUES (?, ?, ?, ?, ?)",
            ("acme|alice", "acme", "Acme Corp", "Alice", "alice@acme.com")
        )
        self.con.execute(
            "INSERT INTO tracker_people (person_key, company_key, company_name, name, email) VALUES (?, ?, ?, ?, ?)",
            ("acme|bob", "acme", "Acme Corp", "Bob", "bob@acme.com")
        )
        self.con.commit()

        suggestions = queries.company_suggestions(self.con, "acm")
        acme_count = sum(1 for s in suggestions if s["company_key"] == "acme")
        self.assertEqual(acme_count, 1, "Acme should appear only once")


class TestTrackerGroups(unittest.TestCase):
    """Tests for queries.tracker_groups — the grouped Tracker view."""

    def setUp(self):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        init_schema(self.con)

    def tearDown(self):
        self.con.close()

    def _add_person(self, person_key, company_key, company_name, name,
                    outreach_status="not_contacted", title="", email=""):
        self.con.execute("""
            INSERT INTO tracker_people
              (person_key, company_key, company_name, name, title, email,
               outreach_status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (person_key, company_key, company_name, name, title, email,
              outreach_status, "2026-06-01T10:00:00", "2026-06-10T10:00:00"))
        self.con.commit()

    def _add_job(self, row_key, company, role_title, app_status=None,
                 match_score=None, status_changed_at=None):
        self.con.execute("""
            INSERT INTO jobs (row_key, company, role_title, match_score)
            VALUES (?,?,?,?)
        """, (row_key, company, role_title, match_score))
        if app_status:
            self.con.execute("""
                INSERT INTO application_status (row_key, status, status_changed_at, source)
                VALUES (?,?,?,?)
            """, (row_key, app_status, status_changed_at or "2026-06-12T10:00:00", "manual"))
        self.con.commit()

    # ── grouping ────────────────────────────────────────────────────────────

    def test_groups_people_into_company_cards(self):
        """Two people at same company → one group."""
        self._add_person("acme|alice", "acme", "Acme Corp", "Alice")
        self._add_person("acme|bob",   "acme", "Acme Corp", "Bob")
        groups, stats = queries.tracker_groups(self.con)
        acme = [g for g in groups if g["company_key"] == "acme"]
        self.assertEqual(len(acme), 1)
        self.assertEqual(acme[0]["people_count"], 2)

    def test_separate_groups_for_different_companies(self):
        """People at different companies → separate groups."""
        self._add_person("acme|alice",   "acme",   "Acme Corp", "Alice")
        self._add_person("google|carol", "google", "Google",    "Carol")
        groups, stats = queries.tracker_groups(self.con)
        keys = {g["company_key"] for g in groups}
        self.assertIn("acme",   keys)
        self.assertIn("google", keys)
        self.assertEqual(len(groups), 2)

    # ── applied rollup ───────────────────────────────────────────────────────

    def test_group_status_applied_when_job_applied(self):
        """group_status == 'applied' when a job is in applied stage."""
        self._add_job("job1", "Acme Corp", "Engineer", app_status="applied")
        groups, _ = queries.tracker_groups(self.con)
        acme_key = queries.normalize_company("Acme Corp")
        g = next(g for g in groups if g["company_key"] == acme_key)
        self.assertEqual(g["group_status"], "applied")
        self.assertEqual(g["applied_count"], 1)

    def test_group_status_offer_beats_applied(self):
        """offer stage takes priority over applied in group_status rollup."""
        self._add_job("job1", "Acme Corp", "Engineer", app_status="applied")
        self._add_job("job2", "Acme Corp", "Director", app_status="offer")
        groups, _ = queries.tracker_groups(self.con)
        acme_key = queries.normalize_company("Acme Corp")
        g = next(g for g in groups if g["company_key"] == acme_key)
        self.assertEqual(g["group_status"], "offer")

    # ── needs_contact for jobs-with-no-people ────────────────────────────────

    def test_group_status_needs_contact_for_jobs_no_people(self):
        """Company with jobs but no tracker people → group_status = needs_contact."""
        self._add_job("job1", "Orphan Corp", "Analyst")
        # jobs-no-people with no recent activity is archived by default; reveal it.
        groups, _ = queries.tracker_groups(self.con, show_all=True)
        orphan_key = queries.normalize_company("Orphan Corp")
        g = next((g for g in groups if g["company_key"] == orphan_key), None)
        self.assertIsNotNone(g)
        self.assertEqual(g["group_status"], "needs_contact")
        self.assertTrue(g["is_placeholder"])

    # ── contacted rollup ─────────────────────────────────────────────────────

    def test_contacted_rollup(self):
        """contacted_count counts people with contacted/followup_due/replied."""
        self._add_person("acme|alice", "acme", "Acme", "Alice", outreach_status="contacted")
        self._add_person("acme|bob",   "acme", "Acme", "Bob",   outreach_status="followup_due")
        self._add_person("acme|carol", "acme", "Acme", "Carol", outreach_status="not_contacted")
        groups, _ = queries.tracker_groups(self.con)
        g = next(g for g in groups if g["company_key"] == "acme")
        self.assertEqual(g["contacted_count"], 2)

    def test_group_status_contacted_from_people(self):
        """group_status == 'contacted' when people are contacted but no jobs applied."""
        self._add_person("acme|alice", "acme", "Acme", "Alice", outreach_status="contacted")
        groups, _ = queries.tracker_groups(self.con)
        g = next(g for g in groups if g["company_key"] == "acme")
        self.assertEqual(g["group_status"], "contacted")

    # ── needs_contacts_only filter ───────────────────────────────────────────

    def test_needs_contacts_only_filter(self):
        """needs_contacts_only=True keeps only groups with group_status == needs_contact."""
        self._add_person("acme|alice", "acme", "Acme Corp", "Alice")
        self._add_job("job1", "Orphan Corp", "Analyst")
        groups, _ = queries.tracker_groups(self.con, needs_contacts_only=True)
        self.assertTrue(all(g["group_status"] == "needs_contact" for g in groups))
        orphan_key = queries.normalize_company("Orphan Corp")
        self.assertTrue(any(g["company_key"] == orphan_key for g in groups))

    # ── search ───────────────────────────────────────────────────────────────

    def test_search_matches_person_name(self):
        """q matches on person name."""
        self._add_person("acme|alice", "acme", "Acme Corp", "Alice Smith")
        self._add_person("goog|bob",   "goog", "Google",    "Bob Jones")
        groups, _ = queries.tracker_groups(self.con, q="Alice")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["company_key"], "acme")

    def test_search_matches_company_name(self):
        """q matches on company name."""
        self._add_person("acme|alice", "acme", "Acme Corp", "Alice")
        self._add_person("goog|bob",   "goog", "Google",    "Bob")
        groups, _ = queries.tracker_groups(self.con, q="Google")
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["company_key"], "goog")

    # ── stats ────────────────────────────────────────────────────────────────

    def test_stats_shape(self):
        """stats dict has all required keys."""
        _, stats = queries.tracker_groups(self.con)
        for key in ("total_companies", "companies_contacted",
                    "companies_applied", "people_total", "added_this_week"):
            self.assertIn(key, stats)

    def test_stats_total_companies_includes_jobs_only(self):
        """total_companies counts companies with jobs but no people."""
        self._add_job("job1", "Orphan Corp", "Analyst")
        _, stats = queries.tracker_groups(self.con)
        self.assertGreaterEqual(stats["total_companies"], 1)

    # ── sort ─────────────────────────────────────────────────────────────────

    def test_sort_company_az(self):
        """sort=company gives alphabetical order."""
        self._add_person("zoo|p",  "zoo",  "Zzz Corp", "P")
        self._add_person("aaa|q",  "aaa",  "Aaa Corp", "Q")
        groups, _ = queries.tracker_groups(self.con, sort="company", dir="asc")
        names = [g["company_name"] for g in groups]
        self.assertEqual(names, sorted(names, key=str.lower))


if __name__ == '__main__':
    unittest.main()
