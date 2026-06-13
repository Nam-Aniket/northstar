"""Merge fetched JD sidecar data into People Finder alert CSVs.

This is the bridge between a JD fetcher output such as
`fetched_jobs_YYYYMMDD.json` and the normal People Finder pipeline:

    job_alerts_raw.csv + fetched_jobs_YYYYMMDD.json
      -> job_posts_enriched.csv -> prepare_job_posts.py -> scoring

The sidecar must contain one object per job with at least:
`url`, `description_full`, and optionally `company`, `title`, `method`,
`skills`, `employment_type`, and `salary`.
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
from pathlib import Path

import csv_merge


ENRICH_COLUMNS = [
    "jd_source_url",
    "jd_enrichment_status",
    "jd_confidence",
    "jd_confidence_notes",
    "jd_extracted_title",
    "jd_extracted_company",
    "jd_extracted_location",
    "jd_posted_date",
    "jd_employment_type",
    "jd_salary",
    "jd_fetch_status",
]

RESOLVER_COLUMNS = [
    "jd_resolver_stage",
    "jd_resolver_source_kind",
    "jd_search_queries",
    "jd_candidate_urls",
    "jd_attempted_urls",
    "jd_rejected_urls",
]


def clean_text(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def linkedin_job_id(url: str) -> str:
    m = re.search(r"/jobs/view/(\d+)", url or "")
    return m.group(1) if m else ""


def norm_url(url: str) -> str:
    url = clean_text(url)
    if not url:
        return ""
    jid = linkedin_job_id(url)
    if jid:
        return f"linkedin:{jid}"
    return url.split("#", 1)[0].rstrip("/")


def status_for_method(method: str, description: str) -> tuple[str, str, str]:
    if len(description) < 350:
        return "jd_unavailable_no_description", "none", "external fetch returned no full JD"
    method = (method or "").lower()
    if method == "guest_api":
        return (
            "jd_full_linkedin_guest_api",
            "medium",
            "Full JD fetched from LinkedIn public guest job endpoint; verify against official/ATS page before final application.",
        )
    if method == "public_jsonld":
        return "jd_full_official_jsonld", "high", "Full JD fetched from public JobPosting JSON-LD."
    if method == "direct_fetch":
        return "jd_full_public_direct_fetch", "medium", "Full/usable JD fetched from public non-LinkedIn page."
    return "jd_full_external_sidecar", "medium", f"Full JD imported from external sidecar method={method or 'unknown'}."


def read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit(f"[!] No header row found in {path}")
        return list(reader.fieldnames), [dict(row) for row in reader]


def write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_sidecar(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        sys.exit("[!] Sidecar JSON must be a list of job objects")
    by_key: dict[str, dict] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        key = norm_url(item.get("url", ""))
        if key:
            by_key[key] = item
    return by_key


def output_fieldnames(input_fields: list[str]) -> list[str]:
    fields = list(input_fields)
    for col in ENRICH_COLUMNS + RESOLVER_COLUMNS:
        if col not in fields:
            fields.append(col)
    return fields


def merge(input_csv: Path, sidecar_json: Path, out_csv: Path, review_csv: Path, append: bool = True) -> dict:
    fields, rows = read_csv(input_csv)
    sidecar = load_sidecar(sidecar_json)
    out_rows = []
    matched_keys = set()
    statuses: dict[str, int] = {}

    for row in rows:
        key = norm_url(row.get("job_url", ""))
        item = sidecar.get(key)
        out = dict(row)
        if item:
            matched_keys.add(key)
            description = clean_text(item.get("description_full", ""))
            status, confidence, notes = status_for_method(item.get("method", ""), description)
            if description and len(description) > len(clean_text(out.get("job_text", ""))):
                out["job_text"] = description
            if item.get("skills") and not out.get("required_skills"):
                out["required_skills"] = clean_text(item.get("skills", ""))
            out.update({
                "jd_source_url": clean_text(item.get("url", "")),
                "jd_enrichment_status": status,
                "jd_confidence": confidence,
                "jd_confidence_notes": notes,
                "jd_extracted_title": clean_text(item.get("title", "")),
                "jd_extracted_company": clean_text(item.get("company", "")),
                "jd_extracted_location": clean_text(item.get("location", "")),
                "jd_posted_date": clean_text(item.get("posted_date", "")),
                "jd_employment_type": clean_text(item.get("employment_type", "")),
                "jd_salary": clean_text(item.get("salary", "")),
                "jd_fetch_status": f"sidecar:{clean_text(item.get('method', 'unknown'))}",
                "jd_resolver_stage": "external_sidecar",
                "jd_resolver_source_kind": clean_text(item.get("method", "external")),
                "jd_search_queries": "",
                "jd_candidate_urls": clean_text(item.get("url", "")),
                "jd_attempted_urls": clean_text(item.get("url", "")),
                "jd_rejected_urls": "",
            })
            statuses[status] = statuses.get(status, 0) + 1
        else:
            status = "jd_unavailable_no_sidecar_match"
            out.update({
                "jd_source_url": "",
                "jd_enrichment_status": status,
                "jd_confidence": "none",
                "jd_confidence_notes": "No matching fetched JD sidecar row.",
                "jd_resolver_stage": "external_sidecar",
                "jd_resolver_source_kind": "",
                "jd_search_queries": "",
                "jd_candidate_urls": "",
                "jd_attempted_urls": "",
                "jd_rejected_urls": "",
            })
            statuses[status] = statuses.get(status, 0) + 1
        out_rows.append(out)

    fieldnames = output_fieldnames(fields)

    if append and out_csv.exists():
        try:
            existing_fields, existing_rows = read_csv(out_csv)
            # Ensure all output fieldnames are present
            for col in fieldnames:
                if col not in existing_fields:
                    existing_fields.append(col)
            out_rows = csv_merge.merge_csv_on_key(existing_rows, out_rows, csv_merge.row_key)
            fieldnames = existing_fields
        except Exception:
            pass  # fall through to normal write if read fails

    write_csv(out_csv, fieldnames, out_rows)
    write_csv(review_csv, fieldnames, out_rows)

    return {
        "input_rows": len(rows),
        "sidecar_rows": len(sidecar),
        "matched_rows": len(matched_keys),
        "unmatched_sidecar_rows": len(set(sidecar) - matched_keys),
        "statuses": dict(sorted(statuses.items())),
    }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Merge fetched JD sidecar JSON into job alert CSV rows.")
    parser.add_argument("--input", default="job_alerts_raw.csv", help="Raw alert CSV")
    parser.add_argument("--sidecar", required=True, help="fetched_jobs_YYYYMMDD.json sidecar")
    parser.add_argument("--out", default="job_posts_enriched.csv", help="Enriched CSV output")
    parser.add_argument("--review", default="jd_resolution_review.csv", help="Review/provenance CSV output")
    parser.add_argument("--append", default=True, action=argparse.BooleanOptionalAction,
                        help="Merge into existing output file (default: on)")
    args = parser.parse_args(argv)
    summary = merge(Path(args.input), Path(args.sidecar), Path(args.out), Path(args.review), append=args.append)
    print(f"[*] Input rows: {summary['input_rows']} | sidecar rows: {summary['sidecar_rows']} | matched: {summary['matched_rows']}")
    print(f"[*] Unmatched sidecar rows: {summary['unmatched_sidecar_rows']}")
    print("[*] JD merge statuses: " + ", ".join(f"{k}:{v}" for k, v in summary["statuses"].items()))
    print(f"[*] Wrote {args.out}")
    print(f"[*] Review: {args.review}")


if __name__ == "__main__":
    main()
