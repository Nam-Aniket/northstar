#!/usr/bin/env python3
"""
Follow-up.

Runs daily. For each contact in outreach.db with:
  - status = 'sent'
  - replied_at IS NULL
  - sent_at >= N days ago
  - followup_1_sent_at IS NULL (for bump 1) OR followup_2_sent_at IS NULL (for bump 2)

It also:
  - Checks Gmail for replies on each open thread and marks replied_at
  - Drafts a short follow-up email per eligible contact into drafts/ as
    pending_review__followup{N}__<company>__<person>.md  (you review, then
    rename to ready_to_send__... and run send_outreach.py)

Same LLM env vars as draft_emails.py.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path

from dotenv_loader import load_env
load_env()

import importlib.util


def _load_module(modname: str, filename: str):
    """Load a digit-prefixed sibling script (e.g. 04_draft_emails.py) by path."""
    spec = importlib.util.spec_from_file_location(
        modname, Path(__file__).resolve().parent / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_draft = _load_module("draft_emails", "04_draft_emails.py")
call_llm, slugify = _draft.call_llm, _draft.slugify


SYSTEM_PROMPT_FU = """You write short, polite follow-up emails for job-search outreach.

Rules:
- Output ONLY the email body. No subject, no markdown, no quotes.
- 40-70 words MAX. Shorter than the original.
- Acknowledge that the recipient is busy, don't apologise excessively.
- Refer to the previous note briefly without repeating it.
- Make a softer, smaller ask than the original (one sentence, one question).
- No emojis, no exclamation marks, no "just wanted to bump this".
- Sign off with the sender's first name only. No signature block.
"""

USER_TEMPLATE_FU = """Write follow-up #{n} for an outreach email that got no reply after {days} days.

Recipient: {name}, {role} at {company}
Original ask was about: {role_target}
Why this company: {rationale}
Sender first name: {sender_first}

Keep it short. End with a one-line, low-stakes question.
"""


def fetch_recent_reply(service, thread_id: str, sender_email_lower: str) -> str | None:
    """Returns ISO timestamp if the recipient has replied on this thread; else None."""
    try:
        thread = service.users().threads().get(userId="me", id=thread_id, format="metadata",
                                                metadataHeaders=["From", "Date"]).execute()
    except Exception:
        return None
    msgs = thread.get("messages", [])
    if len(msgs) < 2:
        return None
    # Look at any message NOT from the sender (our own outbound)
    for m in msgs[1:]:
        headers = {h["name"].lower(): h["value"] for h in m.get("payload", {}).get("headers", [])}
        from_h = headers.get("from", "").lower()
        if sender_email_lower and sender_email_lower in from_h:
            continue
        date = headers.get("date")
        return date or dt.datetime.utcnow().isoformat()
    return None


def days_since(iso: str) -> float:
    try:
        t = dt.datetime.fromisoformat(iso.replace("Z", ""))
    except Exception:
        return 0
    return (dt.datetime.utcnow() - t).total_seconds() / 86400.0


def main():
    ap = argparse.ArgumentParser(description="Check for replies and draft follow-ups.")
    ap.add_argument("--db", default="outreach.db")
    ap.add_argument("--drafts-dir", default="drafts")
    ap.add_argument("--fu1-days", type=int, default=4)
    ap.add_argument("--fu2-days", type=int, default=10)
    ap.add_argument("--max-drafts", type=int, default=20)
    ap.add_argument("--check-replies", action="store_true",
                    help="Also poll Gmail for replies on open threads.")
    ap.add_argument("--sender-email", default=os.environ.get("SENDER_EMAIL", ""),
                    help="Your gmail address (for reply detection)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row

    # 1. Optionally check for replies
    if args.check_replies:
        try:
            gmail_service = _load_module("send_outreach", "05_send_outreach.py").gmail_service
            service = gmail_service()
        except Exception as exc:
            print(f"[gmail] couldn't init service: {exc}")
            service = None

        if service:
            cur = con.execute("SELECT id, thread_id, email FROM outreach WHERE status='sent' AND replied_at IS NULL")
            for row in cur.fetchall():
                if not row["thread_id"]:
                    continue
                replied = fetch_recent_reply(service, row["thread_id"], args.sender_email.lower())
                if replied:
                    con.execute(
                        "UPDATE outreach SET status='replied', replied_at=? WHERE id=?",
                        (replied, row["id"]),
                    )
                    print(f"  [reply] {row['email']} replied at {replied}")
            con.commit()

    # 2. Find follow-up candidates
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    sender_name = os.environ.get("SENDER_NAME", "Your Name")
    sender_first = sender_name.split()[0]

    drafts_dir = Path(args.drafts_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)
    drafted = 0

    # Follow-up 1
    cur = con.execute("""
        SELECT * FROM outreach
        WHERE status='sent' AND replied_at IS NULL AND followup_1_sent_at IS NULL
    """)
    candidates_fu1 = [r for r in cur.fetchall() if r["sent_at"] and days_since(r["sent_at"]) >= args.fu1_days]
    # Follow-up 2
    cur = con.execute("""
        SELECT * FROM outreach
        WHERE status='sent' AND replied_at IS NULL
              AND followup_1_sent_at IS NOT NULL AND followup_2_sent_at IS NULL
    """)
    candidates_fu2 = [r for r in cur.fetchall() if r["followup_1_sent_at"] and days_since(r["followup_1_sent_at"]) >= (args.fu2_days - args.fu1_days)]

    plan = [("fu1", r) for r in candidates_fu1] + [("fu2", r) for r in candidates_fu2]
    plan = plan[: args.max_drafts]
    print(f"[plan] {len(candidates_fu1)} fu1 + {len(candidates_fu2)} fu2 (drafting up to {args.max_drafts})")

    for kind, row in plan:
        n = 1 if kind == "fu1" else 2
        days = round(days_since(row["sent_at"]))
        user_msg = USER_TEMPLATE_FU.format(
            n=n, days=days,
            name=row["person_name"], role=row["role"] or "", company=row["company"],
            role_target="Data / BI roles",
            rationale="(see original draft)",
            sender_first=sender_first,
        )
        if args.dry_run or not api_key:
            body = f"[DRY RUN follow-up #{n}, {days} days since send]"
        else:
            try:
                body = call_llm(
                    [{"role": "system", "content": SYSTEM_PROMPT_FU},
                     {"role": "user", "content": user_msg}],
                    model=model, base_url=base_url, api_key=api_key,
                )
            except Exception as exc:
                print(f"  [llm-error] id={row['id']}: {exc}")
                continue

        fname = f"pending_review__followup{n}__{slugify(row['company'])}__{slugify(row['person_name'])}.md"
        path = drafts_dir / fname
        subject = f"Re: {row['subject']}" if row["subject"] else "Quick follow-up"
        front = (
            "---\n"
            "status: pending_review\n"
            f"followup_of_id: {row['id']}\n"
            f"to_name: {row['person_name']}\n"
            f"to_email: {row['email']}\n"
            f"company: {row['company']}\n"
            f"role: {row['role'] or ''}\n"
            f"role_bucket: {row['role_bucket'] or ''}\n"
            f"linkedin: {row['linkedin_url'] or ''}\n"
            f"thread_id: {row['thread_id'] or ''}\n"
            f"subject: {subject}\n"
            "---\n\n"
        )
        path.write_text(front + body + f"\n\n{sender_first}\n", encoding="utf-8")
        drafted += 1
        print(f"  [fu{n}] {row['person_name']} @ {row['company']} -> {path.name}")

    print(f"\n[done] drafted {drafted} follow-ups -> {drafts_dir}/")
    con.close()


if __name__ == "__main__":
    main()
