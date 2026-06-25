"""app/db.py — SQLite connection + schema initialisation."""
import sqlite3
import os

# Resolved at import time so it works from any cwd.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(_PROJECT_ROOT, "app", "control_panel.db")


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA foreign_keys=OFF")
    return con


def init_schema(con: sqlite3.Connection) -> None:
    """Create Zone 2 (app-state) tables. Never dropped — safe to call repeatedly."""
    con.executescript("""
        -- Zone-1 safety stubs: created empty so a fresh-install GET / never 500s.
        -- The real sync.py DROPs and re-creates these with data; the IF NOT EXISTS
        -- means sync can still do that freely.
        CREATE TABLE IF NOT EXISTS jobs (
            row_key TEXT PRIMARY KEY,
            company TEXT, role_title TEXT, location TEXT, job_url TEXT,
            match_score REAL, sector TEXT, jd_text TEXT, jd_posted_date TEXT,
            matched_evidence TEXT, gaps TEXT, confidence TEXT,
            unclassified_requirements TEXT
        );
        CREATE TABLE IF NOT EXISTS resume_packages (
            row_key TEXT PRIMARY KEY,
            resume_file TEXT, cover_letter_file TEXT,
            self_check_match_rate REAL, self_check_passes_target INTEGER,
            hard_coverage_pct REAL, quantified_bullets INTEGER,
            word_count INTEGER, jd_terms_missing TEXT, genuine_gaps TEXT,
            role_family TEXT
        );
        CREATE TABLE IF NOT EXISTS companies (
            company_key TEXT PRIMARY KEY, company_name TEXT, domain TEXT
        );
        CREATE TABLE IF NOT EXISTS people (
            person_key TEXT PRIMARY KEY, company_key TEXT, name TEXT,
            role TEXT, verified_email TEXT, linkedin_url TEXT
        );
        CREATE TABLE IF NOT EXISTS drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person_slug TEXT, state TEXT
        );
        CREATE TABLE IF NOT EXISTS outreach_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT, replied_at TEXT
        );

        -- Zone-2: onboarding singleton
        CREATE TABLE IF NOT EXISTS onboarding (
            id INTEGER PRIMARY KEY CHECK (id=1),
            resume_filename TEXT,
            resume_uploaded_at TEXT,
            resume_summary TEXT,
            skills_built INTEGER DEFAULT 0,
            location TEXT DEFAULT '',
            recency_tpr TEXT DEFAULT 'r86400',
            created_at TEXT,
            updated_at TEXT
        );

        -- Zone-2: tracked positions (role cards the user wants to search)
        CREATE TABLE IF NOT EXISTS tracked_positions (
            title TEXT PRIMARY KEY,
            display TEXT,
            created_at TEXT
        );

        -- Zone-2: tracked locations (location cards the user wants to search)
        CREATE TABLE IF NOT EXISTS tracked_locations (
            name TEXT PRIMARY KEY,
            display TEXT,
            created_at TEXT
        );

        CREATE TABLE IF NOT EXISTS app_state (
            row_key       TEXT PRIMARY KEY,
            starred       INT  DEFAULT 0,
            dismissed     INT  DEFAULT 0,
            notes         TEXT DEFAULT '',
            applied_at    TEXT,
            created_at    TEXT,
            updated_at    TEXT
        );

        CREATE TABLE IF NOT EXISTS application_status (
            row_key           TEXT PRIMARY KEY,
            status            TEXT,
            status_changed_at TEXT,
            source            TEXT DEFAULT 'manual'
        );

        CREATE TABLE IF NOT EXISTS app_events (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            row_key      TEXT,
            event_type   TEXT,
            payload_json TEXT,
            created_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS job_seen (
            row_key        TEXT PRIMARY KEY,
            first_seen_date TEXT
        );

        CREATE TABLE IF NOT EXISTS person_state (
            person_key       TEXT PRIMARY KEY,
            outreach_status  TEXT,
            contacted_at     TEXT,
            notes            TEXT,
            updated_at       TEXT
        );

        CREATE TABLE IF NOT EXISTS manual_people (
            person_key     TEXT PRIMARY KEY,
            company_key    TEXT,
            name           TEXT,
            role           TEXT,
            verified_email TEXT,
            linkedin_url   TEXT,
            created_at     TEXT
        );

        CREATE TABLE IF NOT EXISTS manual_company (
            company_key  TEXT PRIMARY KEY,
            company_name TEXT,
            domain       TEXT,
            created_at   TEXT
        );

        CREATE TABLE IF NOT EXISTS tracker_people (
            person_key      TEXT PRIMARY KEY,
            company_key     TEXT NOT NULL,
            company_name    TEXT,
            name            TEXT,
            title           TEXT,
            email           TEXT,
            pattern         TEXT,
            outreach_status TEXT DEFAULT 'not_contacted',
            notes           TEXT DEFAULT '',
            needs_review    INT  DEFAULT 0,
            created_at      TEXT,
            updated_at      TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_tracker_people_company ON tracker_people(company_key);

        -- Business mode (B2B outreach): isolated from the job-hunt contact store.
        CREATE TABLE IF NOT EXISTS biz_companies (
            company_key  TEXT PRIMARY KEY,
            company_name TEXT,
            domain       TEXT,
            website      TEXT,
            priority     INTEGER DEFAULT 0,
            notes        TEXT DEFAULT '',
            created_at   TEXT,
            updated_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS biz_prospects (
            prospect_key TEXT PRIMARY KEY,
            company_key  TEXT,
            company_name TEXT,
            name         TEXT,
            title        TEXT,
            email        TEXT,
            pattern      TEXT,
            stage        TEXT DEFAULT 'lead',
            notes        TEXT DEFAULT '',
            needs_review INTEGER DEFAULT 0,
            created_at   TEXT,
            updated_at   TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_biz_prospects_company ON biz_prospects(company_key);
    """)
    con.commit()


def migrate_tracker_people(con: sqlite3.Connection) -> int:
    """
    Idempotent one-shot migration: if tracker_people is empty and manual_people has rows,
    copy each manual_people row + its person_state status/notes + manual_company display name
    into tracker_people. Preserves person_key stability.
    
    Returns count of rows migrated.
    """
    # Check if tracker_people already has data
    count = con.execute("SELECT COUNT(*) FROM tracker_people").fetchone()[0]
    if count > 0:
        return 0  # Already migrated
    
    # Get all manual_people and join with person_state and manual_company
    rows = con.execute("""
        SELECT
            mp.person_key,
            mp.company_key,
            COALESCE(mc.company_name, mp.company_key) AS company_name,
            mp.name,
            mp.role,
            mp.verified_email,
            ps.outreach_status,
            ps.notes,
            ps.updated_at,
            mp.created_at
        FROM manual_people mp
        LEFT JOIN person_state ps ON mp.person_key = ps.person_key
        LEFT JOIN manual_company mc ON mp.company_key = mc.company_key
    """).fetchall()
    
    migrated = 0
    for row in rows:
        con.execute("""
            INSERT OR IGNORE INTO tracker_people
            (person_key, company_key, company_name, name, title, email,
             outreach_status, notes, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            row['person_key'],
            row['company_key'],
            row['company_name'],
            row['name'],
            row['role'],
            row['verified_email'],
            row['outreach_status'] or 'not_contacted',
            row['notes'] or '',
            row['created_at'],
            row['updated_at']
        ))
        migrated += 1
    
    con.commit()
    return migrated
