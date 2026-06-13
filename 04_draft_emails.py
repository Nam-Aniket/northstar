#!/usr/bin/env python3
"""
Draft Emails.

Reads the personalised people CSV and produces ONE outreach email per person
into drafts/. Uses any OpenAI-compatible LLM endpoint (DeepSeek by default,
swap via env vars).

Env vars:
  LLM_API_KEY      required.  DeepSeek API key (https://platform.deepseek.com/)
                              or OPENAI_API_KEY (sk-...)
  LLM_BASE_URL     default: https://api.deepseek.com/v1
                   for OpenAI:        https://api.openai.com/v1
                   for local Ollama:  http://localhost:11434/v1
  LLM_MODEL        default: deepseek-chat
                   for OpenAI:        gpt-4o-mini
                   for Ollama:        llama3.1:8b
  SENDER_NAME      your name (used in signature). default 'Your Name'
  SENDER_HEADLINE  one-line credential. default '(set SENDER_HEADLINE env var)'
  SENDER_LINKEDIN  your LinkedIn URL.

Output: drafts/<status>__<company>__<person>.md with frontmatter the sender
script can read. All drafts start as status: pending_review.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import urllib.request
import urllib.error
from pathlib import Path

from dotenv_loader import load_env
load_env()  # picks up LLM_API_KEY, SENDER_*, etc. from .env if present


SYSTEM_PROMPT = """You write warm, short, low-friction job-search outreach emails on behalf of a candidate.

Rules:
- Output ONLY the email body. No subject line, no markdown, no quotes, no preamble.
- 90-130 words total. Short paragraphs.
- Open with ONE specific sentence about the recipient or their company, using the personalization signal if provided. Never start with "I hope this finds you well" or similar filler.
- Briefly say who the sender is (1 sentence, credential-focused, not begging).
- Make ONE low-friction ask using TIARA framing: Trends, Insights, Resources, Advice. Never "can you refer me", "can you get me a job", or "please review my resume".
- Sign off with the sender's name only. Do NOT add a signature block, LinkedIn URL, or unsubscribe text - the sender script appends those.
- No emojis. No exclamation marks. No phrases like "I came across your profile" or "I would love to pick your brain".
"""

USER_TEMPLATE = """Write the outreach email.

Recipient:
- Name: {name}
- Role: {role}
- Company: {company}

Why I'm reaching out to THIS company:
{rationale}

Personalization signal (use this in the first sentence, paraphrased - do not quote verbatim):
{signal}

The role I'm pursuing: {role_target}

About me (1 sentence credential):
{sender_headline}

Sender first name to sign off with: {sender_first_name}
"""


def call_llm(messages: list[dict], model: str, base_url: str, api_key: str, timeout: int = 60) -> str:
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.6,
        "max_tokens": 400,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        raise RuntimeError(f"LLM HTTP {exc.code}: {body}") from None
    return data["choices"][0]["message"]["content"].strip()


def slugify(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return s[:60] or "x"


def write_draft(
    drafts_dir: Path, company: str, name: str, role: str, role_bucket: str,
    email: str, linkedin: str, signal: str, signal_source: str,
    body: str, sender_name: str, sender_linkedin: str,
) -> Path:
    fname = f"pending_review__{slugify(company)}__{slugify(name)}.md"
    path = drafts_dir / fname
    subject = _suggest_subject(role_bucket, role)
    signature = f"\n\n{sender_name}\n{sender_linkedin}" if sender_linkedin else f"\n\n{sender_name}"
    front = (
        "---\n"
        f"status: pending_review\n"
        f"to_name: {name}\n"
        f"to_email: {email}\n"
        f"company: {company}\n"
        f"role: {role}\n"
        f"role_bucket: {role_bucket}\n"
        f"linkedin: {linkedin}\n"
        f"signal_source: {signal_source}\n"
        f"subject: {subject}\n"
        "---\n\n"
    )
    path.write_text(front + body + signature + "\n", encoding="utf-8")
    return path


def _suggest_subject(role_bucket: str, role: str) -> str:
    if role_bucket == "talent":
        return "Quick question about your data hiring"
    if role_bucket == "hiring_leadership":
        return "Brief note from a Data/BI candidate"
    return "Quick question about working in data at your team"


def main():
    ap = argparse.ArgumentParser(description="Draft personalised outreach emails per person.")
    ap.add_argument("--input", required=True, help="Personalised people CSV")
    ap.add_argument("--drafts-dir", default="drafts", help="Where to write draft .md files")
    ap.add_argument("--limit", type=int, default=0, help="Process at most N rows (0 = all)")
    ap.add_argument("--skip-no-email", action="store_true",
                    help="Skip people whose verified_email is missing/invalid.")
    ap.add_argument("--dry-run", action="store_true", help="Don't call the LLM; show what would be drafted.")
    args = ap.parse_args()

    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not api_key and not args.dry_run:
        raise SystemExit("Set LLM_API_KEY (DeepSeek key) or run with --dry-run.")
    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")
    sender_name = os.environ.get("SENDER_NAME", "Your Name")
    sender_first = sender_name.split()[0]
    sender_headline = os.environ.get("SENDER_HEADLINE",
        "Analyst | open to new roles.")
    sender_linkedin = os.environ.get("SENDER_LINKEDIN", "")

    drafts_dir = Path(args.drafts_dir)
    drafts_dir.mkdir(parents=True, exist_ok=True)

    with open(args.input, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    if args.limit:
        rows = rows[: args.limit]

    print(f"[mode] base_url={base_url} model={model} sender={sender_name}")
    print(f"[plan] {len(rows)} candidates")

    drafted = 0
    skipped = 0
    for i, row in enumerate(rows, 1):
        name = row.get("name", "").strip()
        company = row.get("company_name", "").strip()
        email = row.get("verified_email", "").strip()
        role = row.get("role", "").strip()
        role_bucket = row.get("role_bucket", "").strip()
        role_target = row.get("role_target", "").strip()
        rationale = row.get("rationale", "").strip()
        signal = row.get("personalization_signal", "").strip()
        signal_source = row.get("signal_source", "").strip()
        linkedin = row.get("linkedin_url", "").strip()

        if not name or not company:
            skipped += 1
            continue
        if args.skip_no_email:
            invalid = (not email) or "@" not in email or email.lower().startswith(("invalid", "unverified"))
            if invalid:
                skipped += 1
                continue

        user_msg = USER_TEMPLATE.format(
            name=name, role=role or "(unknown role)", company=company,
            rationale=rationale or "(no specific rationale provided)",
            signal=signal or "(no personalization signal found - keep the opener generic-but-specific to the company)",
            role_target=role_target or "Data / BI roles",
            sender_headline=sender_headline,
            sender_first_name=sender_first,
        )

        if args.dry_run:
            body = "[DRY RUN - would call LLM with the above prompt]"
        else:
            try:
                body = call_llm(
                    [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": user_msg}],
                    model=model, base_url=base_url, api_key=api_key,
                )
            except Exception as exc:
                print(f"  [{i}/{len(rows)}] {name}: LLM error: {exc}")
                skipped += 1
                continue

        path = write_draft(
            drafts_dir, company, name, role, role_bucket, email, linkedin,
            signal, signal_source, body, sender_name, sender_linkedin,
        )
        drafted += 1
        print(f"  [{i}/{len(rows)}] {name} @ {company} -> {path.name}")

    print(f"\n[done] drafted {drafted}, skipped {skipped} -> {drafts_dir}/")


if __name__ == "__main__":
    main()
