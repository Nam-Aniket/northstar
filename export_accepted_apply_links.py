#!/usr/bin/env python3
"""Export a compact application-link CSV from matched_jobs.csv."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


DEFAULT_INPUT = Path("matched_jobs.csv")
DEFAULT_OUTPUT = Path("accepted_jobs_apply_links.csv")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Write company, role and apply link columns for accepted jobs."
    )
    parser.add_argument("--input", default=str(DEFAULT_INPUT), help="Accepted jobs CSV.")
    parser.add_argument(
        "--out",
        default=str(DEFAULT_OUTPUT),
        help="Output CSV with company_name, job_role and apply_link.",
    )
    parser.add_argument(
        "--exclude-company",
        action="append",
        default=[],
        help="Company name to exclude. Can be provided multiple times.",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.out)

    with input_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    output_rows = []
    excluded = {company.strip() for company in args.exclude_company if company.strip()}
    for row in rows:
        if row.get("company", "").strip() in excluded:
            continue
        output_rows.append(
            {
                "company_name": row.get("company", "").strip(),
                "job_role": row.get("role_title", "").strip(),
                "apply_link": row.get("job_url", "").strip(),
            }
        )

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["company_name", "job_role", "apply_link"])
        writer.writeheader()
        writer.writerows(output_rows)

    print(f"Wrote {len(output_rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
