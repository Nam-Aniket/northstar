"""test_tracker_migration.py — Phase 0 schema + migration tests."""
import unittest
import sqlite3
import tempfile
import os
from app.db import init_schema, migrate_tracker_people


class TestTrackerMigration(unittest.TestCase):
    """Test tracker_people schema and idempotent migration."""

    def setUp(self):
        """Create a fresh in-memory DB for each test."""
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        init_schema(self.con)

    def tearDown(self):
        """Close the connection."""
        self.con.close()

    def test_schema_created(self):
        """tracker_people table exists with correct schema."""
        # Check table exists
        tables = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tracker_people'"
        ).fetchall()
        self.assertEqual(len(tables), 1, "tracker_people table should exist")

        # Check columns
        columns = self.con.execute("PRAGMA table_info(tracker_people)").fetchall()
        col_names = [col['name'] for col in columns]
        expected = [
            'person_key', 'company_key', 'company_name', 'name', 'title',
            'email', 'pattern', 'outreach_status', 'notes', 'needs_review',
            'created_at', 'updated_at'
        ]
        for col in expected:
            self.assertIn(col, col_names, f"Column {col} should exist")

    def test_index_created(self):
        """Index on company_key exists."""
        indexes = self.con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_tracker_people_company'"
        ).fetchall()
        self.assertEqual(len(indexes), 1, "Index idx_tracker_people_company should exist")

    def test_init_schema_idempotent(self):
        """Calling init_schema twice does not fail."""
        # Should not raise
        init_schema(self.con)
        init_schema(self.con)

    def test_migrate_empty_when_no_manual_people(self):
        """If manual_people is empty, migration returns 0."""
        result = migrate_tracker_people(self.con)
        self.assertEqual(result, 0)

    def test_migrate_copies_manual_people_row(self):
        """Migration copies manual_people + person_state into tracker_people."""
        # Insert test data
        self.con.execute("""
            INSERT INTO manual_company (company_key, company_name)
            VALUES ('acme', 'ACME Corp')
        """)
        self.con.execute("""
            INSERT INTO manual_people (person_key, company_key, name, role, verified_email, created_at)
            VALUES ('acme|alice', 'acme', 'Alice Smith', 'Engineer', 'alice@example.com', '2026-06-01T00:00:00Z')
        """)
        self.con.execute("""
            INSERT INTO person_state (person_key, outreach_status, notes, updated_at)
            VALUES ('acme|alice', 'contacted', 'Interested in role', '2026-06-10T00:00:00Z')
        """)
        self.con.commit()

        # Migrate
        result = migrate_tracker_people(self.con)
        self.assertEqual(result, 1)

        # Verify row in tracker_people
        row = self.con.execute(
            "SELECT * FROM tracker_people WHERE person_key = 'acme|alice'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['company_key'], 'acme')
        self.assertEqual(row['company_name'], 'ACME Corp')
        self.assertEqual(row['name'], 'Alice Smith')
        self.assertEqual(row['title'], 'Engineer')
        self.assertEqual(row['email'], 'alice@example.com')
        self.assertEqual(row['outreach_status'], 'contacted')
        self.assertEqual(row['notes'], 'Interested in role')

    def test_migrate_idempotent(self):
        """Running migration twice does not duplicate rows."""
        # Setup
        self.con.execute("""
            INSERT INTO manual_company (company_key, company_name)
            VALUES ('acme', 'ACME Corp')
        """)
        self.con.execute("""
            INSERT INTO manual_people (person_key, company_key, name, role, verified_email, created_at)
            VALUES ('acme|alice', 'acme', 'Alice Smith', 'Engineer', 'alice@example.com', '2026-06-01T00:00:00Z')
        """)
        self.con.commit()

        # First migrate
        result1 = migrate_tracker_people(self.con)
        self.assertEqual(result1, 1)

        # Second migrate (should return 0)
        result2 = migrate_tracker_people(self.con)
        self.assertEqual(result2, 0)

        # Verify still only one row
        count = self.con.execute("SELECT COUNT(*) FROM tracker_people").fetchone()[0]
        self.assertEqual(count, 1)

    def test_migrate_preserves_row_across_schema_reinit(self):
        """After migration, calling init_schema again does not drop tracker_people data."""
        # Setup and migrate
        self.con.execute("""
            INSERT INTO manual_company (company_key, company_name)
            VALUES ('acme', 'ACME Corp')
        """)
        self.con.execute("""
            INSERT INTO manual_people (person_key, company_key, name, role, verified_email, created_at)
            VALUES ('acme|bob', 'acme', 'Bob Jones', 'Designer', 'bob@example.com', '2026-06-01T00:00:00Z')
        """)
        self.con.commit()

        migrate_tracker_people(self.con)
        count_before = self.con.execute("SELECT COUNT(*) FROM tracker_people").fetchone()[0]
        self.assertEqual(count_before, 1)

        # Re-init schema (should not drop data)
        init_schema(self.con)

        # Verify row still exists
        count_after = self.con.execute("SELECT COUNT(*) FROM tracker_people").fetchone()[0]
        self.assertEqual(count_after, 1, "tracker_people row should survive init_schema reinit")

        row = self.con.execute(
            "SELECT * FROM tracker_people WHERE person_key = 'acme|bob'"
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row['name'], 'Bob Jones')


if __name__ == '__main__':
    unittest.main()
