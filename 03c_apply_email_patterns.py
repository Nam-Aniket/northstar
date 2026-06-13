#!/usr/bin/env python3
"""
Apply Email Patterns.

Email verification by SMTP is impossible on networks that block port 25
(most home ISPs) and web-search verification is bot-blocked. So instead we
GENERATE high-confidence emails from a per-company domain + pattern map.

Workflow:
  1. Fill company_domains.json with each company's domain + email pattern.
     Get the pattern free from hunter.io Domain Search (25/mo free) or guess.
  2. Run this script: it applies the right pattern to every person at that
     company and writes emails into the people CSV.

Pattern tokens:
  {first}        -> jordan
  {last}         -> rivera
  {f}            -> j        (first initial)
  {l}            -> r        (last initial)
  {first[0]}     -> j        (index form written by 03b's canonical patterns)
  {last[0]}      -> r
  {first}.{last} -> jordan.rivera
  {f}{last}      -> jrivera

Example company_domains.json entry:
  "Employment Hero": {"domain": "employmenthero.com", "pattern": "{first}@{domain}"}
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


def split_name(full: str) -> tuple[str, str]:
    parts = [re.sub(r"[^a-z]", "", p.lower()) for p in full.split() if p.strip()]
    parts = [p for p in parts if p]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[-1]


def build_email(full_name: str, domain: str, pattern: str) -> str:
    first, last = split_name(full_name)
    if not first or not domain:
        return ""
    # Guard: patterns needing a last name when we only have one name.
    # Covers both {last} and the index forms {last[0]} written by 03b.
    if ("{last}" in pattern or "{last[" in pattern) and not last:
        return ""
    # {first[0]} / {last[0]} index syntax (the 7 canonical patterns in 03b use this).
    def _index(m):
        token, idx = m.group(1), int(m.group(2))
        src = first if token == "first" else last
        return src[idx] if 0 <= idx < len(src) else ""
    email = re.sub(r"\{(first|last)\[(\d+)\]\}", _index, pattern)
    repl = {
        "{first}": first,
        "{last}": last,
        "{f}": first[0] if first else "",
        "{l}": last[0] if last else "",
        "{domain}": domain,
    }
    for k, v in repl.items():
        email = email.replace(k, v)
    # if the pattern omitted @domain, append it
    if "@" not in email:
        email = f"{email}@{domain}"
    return email.lower()


def main():
    ap = argparse.ArgumentParser(description="Generate emails from per-company domain + pattern.")
    ap.add_argument("--input", default="seed_people.csv")
    ap.add_argument("--output", default="seed_people_with_emails.csv")
    ap.add_argument("--domains", default="company_domains.json")
    ap.add_argument("--default-pattern", default="{first}.{last}@{domain}",
                    help="Used when a company has a domain but no explicit pattern.")
    args = ap.parse_args()

    domains_path = Path(args.domains)
    if not domains_path.exists():
        raise SystemExit(f"Missing {args.domains}. Create it (see --help for format).")
    domain_map = json.loads(domains_path.read_text("utf-8"))

    with open(args.input, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    fieldnames = list(rows[0].keys()) if rows else []
    for extra in ("verified_email", "verification_source", "domain"):
        if extra not in fieldnames:
            fieldnames.append(extra)

    generated = 0
    no_domain = set()
    for r in rows:
        company = r.get("company_name", "").strip()
        entry = domain_map.get(company)
        if not entry:
            no_domain.add(company)
            continue
        domain = entry.get("domain", "").strip()
        pattern = entry.get("pattern", "").strip() or args.default_pattern
        email = build_email(r.get("name", ""), domain, pattern)
        if email:
            r["verified_email"] = email
            r["verification_source"] = f"pattern:{pattern}"
            r["domain"] = domain
            generated += 1

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})

    print(f"[done] generated {generated}/{len(rows)} emails -> {args.output}")
    if no_domain:
        print(f"[missing] {len(no_domain)} companies have no entry in {args.domains}:")
        for c in sorted(no_domain):
            print(f"    {c}")


if __name__ == "__main__":
    main()
