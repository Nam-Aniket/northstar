#!/usr/bin/env python3
"""
Send Outreach.

Reads drafts/ for files whose frontmatter status is 'ready_to_send' (you
flip them by hand after review). Sends each via Gmail API with OAuth,
throttled to a human cadence, business hours only. Logs everything to
outreach.db (SQLite).

First-time setup:
  1. Go to console.cloud.google.com -> new project
  2. Enable "Gmail API"
  3. OAuth consent screen: External, your email as test user
  4. Credentials -> OAuth client ID -> Desktop app -> download JSON
  5. Save it as gmail_oauth_client.json in this folder
  6. pip install google-auth google-auth-oauthlib google-api-python-client
  7. Run this script once - it'll open a browser for consent, then cache
     the token in gmail_oauth_token.json

Each run:
  python3 send_outreach.py --drafts-dir drafts --max-per-run 20
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import os
import random
import re
import sqlite3
import time
from email.mime.text import MIMEText
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.send",
          "https://www.googleapis.com/auth/gmail.readonly"]

DB_PATH = "outreach.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  company TEXT,
  person_name TEXT,
  email TEXT,
  role TEXT,
  role_bucket TEXT,
  linkedin_url TEXT,
  signal_source TEXT,
  subject TEXT,
  draft_path TEXT,
  status TEXT,
  sent_at TEXT,
  thread_id TEXT,
  message_id TEXT,
  replied_at TEXT,
  followup_1_sent_at TEXT,
  followup_2_sent_at TEXT,
  bounce_at TEXT,
  notes TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_email_company ON outreach(email, company);
"""

FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def parse_draft(path: Path) -> tuple[dict, str]:
    """Return (frontmatter_dict, body)."""
    text = path.read_text(encoding="utf-8")
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    fm_block, body = m.group(1), m.group(2)
    fm = {}
    for line in fm_block.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm, body.strip()


def gmail_service():
    """Returns an authenticated Gmail API client."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    creds = None
    token_path = "gmail_oauth_token.json"
    client_secrets = "gmail_oauth_client.json"

    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(client_secrets):
                raise SystemExit(
                    f"Missing {client_secrets}. Download it from Google Cloud Console "
                    "(OAuth Client ID, Desktop app) and save it here."
                )
            flow = InstalledAppFlow.from_client_secrets_file(client_secrets, SCOPES)
            creds = flow.run_local_server(port=0)
        Path(token_path).write_text(creds.to_json(), encoding="utf-8")

    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def build_message(to_email: str, subject: str, body: str, sender_email: str | None = None) -> dict:
    msg = MIMEText(body, "plain", "utf-8")
    msg["to"] = to_email
    if sender_email:
        msg["from"] = sender_email
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
    return {"raw": raw}


def is_business_hours(now: dt.datetime, start: int = 9, end: int = 17) -> bool:
    if now.weekday() >= 5:  # Sat/Sun
        return False
    return start <= now.hour < end


def db_connect(path: str) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    con.row_factory = sqlite3.Row
    return con


def already_sent(con: sqlite3.Connection, email: str, company: str) -> bool:
    cur = con.execute(
        "SELECT 1 FROM outreach WHERE email=? AND company=? AND status IN ('sent','replied','bounced')",
        (email, company),
    )
    return cur.fetchone() is not None


def main():
    ap = argparse.ArgumentParser(description="Send reviewed outreach drafts via Gmail API.")
    ap.add_argument("--drafts-dir", default="drafts")
    ap.add_argument("--max-per-run", type=int, default=20)
    ap.add_argument("--min-gap", type=int, default=90, help="Min seconds between sends")
    ap.add_argument("--max-gap", type=int, default=180, help="Max seconds between sends")
    ap.add_argument("--bh-start", type=int, default=9, help="Earliest hour to send (local)")
    ap.add_argument("--bh-end", type=int, default=17, help="Latest hour to send (local)")
    ap.add_argument("--ignore-business-hours", action="store_true")
    ap.add_argument("--db", default=DB_PATH)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    drafts_dir = Path(args.drafts_dir)
    if not drafts_dir.exists():
        raise SystemExit(f"No drafts dir: {drafts_dir}")

    # Discover ready_to_send drafts.
    candidates = []
    for p in sorted(drafts_dir.iterdir()):
        if not p.is_file() or p.suffix != ".md":
            continue
        if not p.name.startswith("ready_to_send__"):
            continue
        candidates.append(p)

    if not candidates:
        print("No drafts marked 'ready_to_send'. Rename pending_review__*.md -> ready_to_send__*.md "
              "after you've edited each one.")
        return
    print(f"[plan] {len(candidates)} ready, will send up to {args.max_per_run}")

    con = db_connect(args.db)
    service = None if args.dry_run else gmail_service()
    sent_count = 0

    for path in candidates:
        if sent_count >= args.max_per_run:
            print(f"[stop] hit max-per-run={args.max_per_run}")
            break

        fm, body = parse_draft(path)
        to_email = fm.get("to_email", "")
        subject = fm.get("subject", "Hello")
        company = fm.get("company", "")
        name = fm.get("to_name", "")

        if not to_email or "@" not in to_email or to_email.lower().startswith(("invalid", "unverified")):
            print(f"  [skip:no_email] {path.name}")
            continue

        # Follow-ups intentionally target an already-contacted person, so the
        # already_sent guard (initial-outreach dedup) must not block them.
        is_followup = bool(fm.get("followup_of_id", "").strip())
        if not is_followup and already_sent(con, to_email, company):
            print(f"  [skip:already_sent] {path.name}")
            continue

        # Business hours gate
        now = dt.datetime.now()
        if not args.ignore_business_hours and not is_business_hours(now, args.bh_start, args.bh_end):
            print(f"  [stop:outside_hours] {now:%a %H:%M} - run between {args.bh_start}-{args.bh_end} weekdays")
            break

        if args.dry_run:
            print(f"  [DRY] would send to {to_email} ({name} @ {company}) subj={subject!r}")
            sent_count += 1
            continue

        msg = build_message(to_email, subject, body)
        try:
            resp = service.users().messages().send(userId="me", body=msg).execute()
        except Exception as exc:  # noqa: BLE001
            print(f"  [send-error] {to_email}: {type(exc).__name__}: {exc}")
            continue

        message_id = resp.get("id", "")
        thread_id = resp.get("threadId", "")
        now_iso = dt.datetime.utcnow().isoformat()

        # If this is a follow-up, update the parent row's followup_X_sent_at
        # rather than creating a new row.
        followup_of = fm.get("followup_of_id", "").strip()
        if followup_of:
            # decide which column to set based on what's already filled
            parent = con.execute(
                "SELECT followup_1_sent_at, followup_2_sent_at FROM outreach WHERE id=?",
                (followup_of,),
            ).fetchone()
            if parent and not parent["followup_1_sent_at"]:
                con.execute("UPDATE outreach SET followup_1_sent_at=? WHERE id=?", (now_iso, followup_of))
            elif parent and not parent["followup_2_sent_at"]:
                con.execute("UPDATE outreach SET followup_2_sent_at=? WHERE id=?", (now_iso, followup_of))
        else:
            con.execute("""
                INSERT INTO outreach (
                  company, person_name, email, role, role_bucket, linkedin_url,
                  signal_source, subject, draft_path, status, sent_at, thread_id, message_id
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                company, name, to_email, fm.get("role", ""), fm.get("role_bucket", ""),
                fm.get("linkedin", ""), fm.get("signal_source", ""),
                subject, str(path), "sent",
                now_iso, thread_id, message_id,
            ))
        con.commit()

        # Mark file as sent
        new_name = path.name.replace("ready_to_send__", "sent__", 1)
        path.rename(drafts_dir / new_name)

        sent_count += 1
        print(f"  [sent {sent_count}/{args.max_per_run}] {to_email} <- {company}")

        # Throttle
        if sent_count < args.max_per_run:
            gap = random.randint(args.min_gap, args.max_gap)
            print(f"    waiting {gap}s before next send...")
            time.sleep(gap)

    print(f"\n[done] sent={sent_count}")
    con.close()


if __name__ == "__main__":
    main()
