#!/usr/bin/env python3
"""
Seed from Reachout.

One-shot bridge: reads the "Job Hunt - The Pipeline - Reachout Data.csv" and
emits a people CSV (one row per person from the Person 1/2/3 columns) in the
shape personalization_scraper.py and draft_emails.py expect.

No scraping. No search engines. Just LinkedIn slug -> name conversion using
the same helper job_outreach_pipeline.py uses, so we can verify the
personalization + drafting stages on real seed data even while x-ray is broken.
"""

from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

# Inlined from 02_find_people.py (that module is digit-prefixed and not importable,
# and pulls in the crawler stack we don't need here).
LINKEDIN_SLUG_RE = re.compile(r"linkedin\.com/in/([^/?#]+)")


def name_from_linkedin_slug(url: str) -> str:
    """'linkedin.com/in/jordan-rivera/' -> 'Jordan Rivera'.

    Strips numeric/hash suffixes ('-a19984171') LinkedIn appends on slug collision.
    """
    m = LINKEDIN_SLUG_RE.search(url or "")
    if not m:
        return ""
    slug = m.group(1)
    parts = [p for p in slug.split("-")
             if not (len(p) >= 6 and any(c.isdigit() for c in p) and any(c.isalpha() for c in p))]
    parts = [p for p in parts if not p.isdigit()]
    if not parts:
        parts = slug.split("-")
    return " ".join(p.capitalize() for p in parts)


COLS = [
    "company_name", "role_target", "rationale",
    "name", "role", "role_bucket",
    "verified_email", "verification_source",
    "guessed_emails", "linkedin_url",
    "domain", "accepted_website", "source",
]


def main():
    ap = argparse.ArgumentParser(description="Convert reachout CSV -> people CSV using seed URLs only.")
    ap.add_argument("--input", required=True,
                    help='The "Reachout Data" CSV (Company + Person 1/2/3 columns).')
    ap.add_argument("--output", default="seed_people.csv",
                    help="Output people CSV.")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    people: list[dict] = []
    for r in rows:
        company = (r.get("Company") or "").strip()
        if not company:
            continue
        role_target = (r.get("Role Type") or "").strip()
        rationale = (r.get("Why this Company?") or "").strip()
        for col in ("Person 1", "Person 2", "Person 3"):
            url = (r.get(col) or "").strip()
            if not url or "linkedin.com/in/" not in url:
                continue
            name = name_from_linkedin_slug(url)
            if not name:
                continue
            people.append({
                "company_name": company,
                "role_target": role_target,
                "rationale": rationale,
                "name": name,
                "role": "(seed - role unknown)",
                "role_bucket": "seed",
                "verified_email": "",
                "verification_source": "",
                "guessed_emails": "",
                "linkedin_url": url,
                "domain": "",
                "accepted_website": "",
                "source": "seed_csv",
            })

    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for p in people:
            w.writerow(p)

    print(f"[done] {len(people)} people from {len(rows)} companies -> {args.output}")
    if people:
        print(f"[sample] first 3:")
        for p in people[:3]:
            print(f"  {p['name']} @ {p['company_name']}  ({p['linkedin_url']})")


if __name__ == "__main__":
    main()
