#!/usr/bin/env python3
"""
Validate Active Hirers.

Reads active_hirers.csv (from job_posting_scraper.py) and:

  1. Re-fetches each posting URL to confirm it's still live.
  2. Pulls JSON-LD JobPosting structured data when present, using its
     datePosted as the authoritative date (overrides 'X days ago' parse).
  3. Filters to postings within --max-age-days.
  4. Drops or flags recruiter agencies (--exclude-recruiters or --flag-recruiters).
  5. Writes a per-company rollup CSV (one row per company, with the
     freshest posting + role count) ready to feed into job_outreach_pipeline.
  6. Prints an audit summary AND samples 5 postings for eyeball verification.

Outputs:
  <prefix>_validated.csv          one row per validated posting
  <prefix>_companies.csv          one row per company (the file you feed to job_outreach_pipeline)
  <prefix>_audit.json             machine-readable QA report
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import random
import re
from collections import defaultdict
from pathlib import Path

from superpowered_crawler_final import SuperpoweredCrawlerFinal


def parse_iso_date(s: str) -> dt.date | None:
    if not s:
        return None
    try:
        return dt.date.fromisoformat(s[:10])
    except Exception:
        return None


def days_old(iso: str, today: dt.date) -> int | None:
    d = parse_iso_date(iso)
    return (today - d).days if d else None


def main():
    ap = argparse.ArgumentParser(description="Validate freshness + liveness of scraped postings.")
    ap.add_argument("--input", default="active_hirers.csv")
    ap.add_argument("--output-prefix", default="active_hirers")
    ap.add_argument("--max-age-days", type=int, default=14)
    ap.add_argument("--exclude-recruiters", action="store_true",
                    help="Drop postings from known recruiter agencies entirely.")
    ap.add_argument("--flag-recruiters", action="store_true",
                    help="Keep recruiters but mark them separately in the company rollup.")
    ap.add_argument("--no-refetch", action="store_true",
                    help="Skip refetching individual posting URLs (faster, less accurate).")
    ap.add_argument("--sample-size", type=int, default=5)
    ap.add_argument("--rps", type=float, default=0.5)
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))

    print(f"[plan] {len(rows)} raw postings to validate")
    today = dt.date.today()

    crawler = None
    if not args.no_refetch:
        crawler = SuperpoweredCrawlerFinal(
            max_pages=1, use_search=False,
            rps=args.rps, polite_delay=0.5,
            respect_robots=True, identify=False,
        )

    validated: list[dict] = []
    dropped_old = 0
    dropped_dead = 0
    dropped_recruiter = 0
    fixed_dates = 0
    still_no_date = 0

    for i, row in enumerate(rows, 1):
        company = row.get("company_name", "").strip()
        url = row.get("posting_url", "").strip()
        is_recruiter = (row.get("is_recruiter", "") or "").strip().lower() in ("true", "1", "yes")

        if args.exclude_recruiters and is_recruiter:
            dropped_recruiter += 1
            continue

        posted_iso = row.get("posted_at_iso", "").strip()
        canonical_source = row.get("source", "")

        # 1. Try to re-validate via JSON-LD on the posting page
        live = True
        if crawler and url and i <= len(rows):  # avoid futile checks
            try:
                fetch = crawler.fetch(url, max_bytes=600_000)
                if not fetch.text or fetch.status >= 400:
                    live = False
                else:
                    sd = crawler.extract_structured_data(fetch.text)
                    for j in sd.get("jobs", []):
                        dp = (j.get("date_posted") or "")[:10]
                        if dp:
                            if dp != posted_iso:
                                fixed_dates += 1
                            posted_iso = dp
                            break
            except Exception as exc:
                print(f"  [{i}/{len(rows)}] refetch error: {exc}")

        if not live:
            dropped_dead += 1
            continue

        # 2. Freshness gate
        age = days_old(posted_iso, today)
        if age is None:
            still_no_date += 1
            # Be lenient: keep it but flag
        elif age > args.max_age_days:
            dropped_old += 1
            continue

        row["posted_at_iso"] = posted_iso
        row["age_days"] = age if age is not None else ""
        row["date_confidence"] = "structured" if posted_iso else "unparsed"
        validated.append(row)

    print(f"\n[validation] kept {len(validated)} / {len(rows)}")
    print(f"  dropped (too old):    {dropped_old}")
    print(f"  dropped (dead url):   {dropped_dead}")
    print(f"  dropped (recruiter):  {dropped_recruiter}")
    print(f"  fixed posting dates:  {fixed_dates}")
    print(f"  no parseable date:    {still_no_date}")

    # ----- write validated postings CSV --------------------------------------
    val_csv = f"{args.output_prefix}_validated.csv"
    cols = list(rows[0].keys()) + ["age_days", "date_confidence"] if rows else []
    cols = list(dict.fromkeys(cols))
    with open(val_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in validated:
            w.writerow({k: r.get(k, "") for k in cols})
    print(f"\n  wrote {val_csv}")

    # ----- build per-company rollup ------------------------------------------
    per_company: dict[str, dict] = {}
    for p in validated:
        key = p["company_name"].strip()
        if not key:
            continue
        d = per_company.setdefault(key, {
            "company_name": key,
            "open_roles": [],
            "freshest_post_iso": "",
            "freshest_post_url": "",
            "is_recruiter": False,
            "sources": set(),
        })
        d["open_roles"].append(p.get("role_title", ""))
        if p.get("posted_at_iso") and (not d["freshest_post_iso"] or p["posted_at_iso"] > d["freshest_post_iso"]):
            d["freshest_post_iso"] = p["posted_at_iso"]
            d["freshest_post_url"] = p["posting_url"]
        if (p.get("is_recruiter", "") or "").lower() in ("true", "1", "yes"):
            d["is_recruiter"] = True
        d["sources"].add(p.get("source", ""))

    rollup_csv = f"{args.output_prefix}_companies.csv"
    rollup_cols = ["company_name", "open_role_count", "open_roles_sample",
                   "freshest_post_iso", "freshest_post_url",
                   "is_recruiter", "sources"]
    with open(rollup_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=rollup_cols)
        w.writeheader()
        for d in sorted(per_company.values(), key=lambda x: x["freshest_post_iso"], reverse=True):
            w.writerow({
                "company_name": d["company_name"],
                "open_role_count": len(d["open_roles"]),
                "open_roles_sample": " | ".join(sorted(set(r for r in d["open_roles"] if r))[:3]),
                "freshest_post_iso": d["freshest_post_iso"],
                "freshest_post_url": d["freshest_post_url"],
                "is_recruiter": d["is_recruiter"],
                "sources": ",".join(sorted(d["sources"])),
            })
    print(f"  wrote {rollup_csv}  ({len(per_company)} unique companies)")

    # ----- audit summary -----------------------------------------------------
    by_age = defaultdict(int)
    for p in validated:
        a = p.get("age_days", "")
        bucket = "unknown" if a == "" else ("0-3d" if a <= 3 else "4-7d" if a <= 7 else "8-14d")
        by_age[bucket] += 1

    audit = {
        "input_count": len(rows),
        "validated_count": len(validated),
        "dropped_too_old": dropped_old,
        "dropped_dead_url": dropped_dead,
        "dropped_recruiter": dropped_recruiter,
        "fixed_posting_dates": fixed_dates,
        "no_parseable_date": still_no_date,
        "unique_companies": len(per_company),
        "recruiter_companies": sum(1 for d in per_company.values() if d["is_recruiter"]),
        "age_buckets": dict(by_age),
    }
    audit_path = f"{args.output_prefix}_audit.json"
    Path(audit_path).write_text(json.dumps(audit, indent=2), encoding="utf-8")
    print(f"  wrote {audit_path}")

    # ----- sample for eyeball check -----------------------------------------
    print(f"\n[sample] {args.sample_size} random validated postings for manual QA:")
    for p in random.sample(validated, min(args.sample_size, len(validated))):
        print(f"  - {p.get('company_name','?')} | {p.get('role_title','?')}")
        print(f"    posted: {p.get('posted_at_iso','?')} ({p.get('posted_relative','')})")
        print(f"    url:    {p.get('posting_url','')}")
        print(f"    recruiter: {p.get('is_recruiter','')}, source: {p.get('source','')}")

    print("\n[next] feed the companies CSV into job_outreach_pipeline.py:")
    print(f"  python3 job_outreach_pipeline.py \\")
    print(f"    --input {rollup_csv} \\")
    print(f"    --output-prefix job_outreach \\")
    print(f"    --config job_outreach_config.json \\")
    print(f"    --workers 4 --resume --identify")
    print("\n  (the pipeline reads 'company_name' column; rationale/role_target will be blank")
    print("   for these companies - either edit them in or use freshest_post_url as the rationale.)")


if __name__ == "__main__":
    main()
