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
    """)
    con.commit()
