#!/usr/bin/env python3
"""
job_description_fetcher.py

Given a list of LinkedIn job URLs, fetches the full job description and
structured fields then writes a tracker-compatible CSV + JSON sidecar.

Input formats:
  --input paste.txt    pipe-delimited lines: "Company | Role | Location | URL"
                       (numbered lines like "1. Company | ..." also work)
  --input jobs.csv     CSV — auto-detects the column containing linkedin.com/jobs/view

Output:
  --output fetched_jobs.csv   25-column tracker CSV (appends if file already exists)
  fetched_jobs.json           JSON sidecar with full untruncated descriptions

Strategy (in order, first non-empty description wins):
  1. LinkedIn guest API:  /jobs-guest/jobs/api/jobPosting/{id}   (no login needed)
  2. Public SEO page:     /jobs/view/{id}  + JSON-LD extraction  (fallback)
  3. Direct fetch:        for non-LinkedIn URLs (e.g. gradconnection.com)

Uses the existing SuperpoweredCrawlerFinal so curl_cffi impersonation,
per-host rate limiting, disk cache, and Playwright fallback are all free.
respect_robots=False is required — LinkedIn's robots.txt disallows /jobs-guest/.
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import re
import sys
from pathlib import Path

from superpowered_crawler_final import SuperpoweredCrawlerFinal, clean_text


# ---------------------------------------------------------------------------
# Skills keyword list (case-insensitive match, returned title-cased)
# ---------------------------------------------------------------------------
_SKILLS = [
    "sql", "python", "r", "power bi", "powerbi", "tableau", "excel",
    "dbt", "azure", "aws", "gcp", "spark", "hadoop", "snowflake", "databricks",
    "machine learning", "deep learning", "nlp", "statistics",
    "data modelling", "data modeling", "data warehouse", "etl", "elt",
    "looker", "qlik", "scikit-learn", "tensorflow", "pytorch",
    "pandas", "numpy", "matplotlib", "seaborn", "plotly",
    "git", "github", "gitlab", "jira", "confluence", "agile", "scrum",
    "salesforce", "sap", "oracle", "mongodb", "postgresql", "mysql",
    "bigquery", "redshift", "sharepoint", "alteryx", "ssis",
]

_SALARY_RE = re.compile(
    r"\$[\d,]+(?:\.\d+)?(?:\s*[kKmM])?(?:\s*[-–—]\s*\$?[\d,]+(?:\.\d+)?(?:\s*[kKmM])?)?",
)
_WORK_ARR_RE = re.compile(r"\b(remote|hybrid|on[-\s]?site|in[-\s]?office)\b", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(?:amp|lt|gt|nbsp|quot|#\d+);")


def _strip_html(html: str) -> str:
    text = _HTML_TAG_RE.sub(" ", html)
    text = _HTML_ENTITY_RE.sub(
        lambda m: {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ", "&quot;": '"'}.get(m.group(0), " "),
        text,
    )
    return re.sub(r"\s{2,}", " ", text).strip()


def _extract_skills(text: str) -> str:
    tl = text.lower()
    found = []
    for skill in _SKILLS:
        if re.search(r"\b" + re.escape(skill) + r"\b", tl):
            found.append(skill.upper() if len(skill) <= 3 else skill.title())
    return ", ".join(found)


def _extract_salary(text: str) -> str:
    m = _SALARY_RE.search(text)
    return m.group(0).strip() if m else "Not listed"


def _extract_work_arr(text: str, location: str = "") -> str:
    m = _WORK_ARR_RE.search(f"{text} {location}")
    if not m:
        return "Unknown"
    w = m.group(1).lower()
    if "remote" in w:
        return "Remote"
    if "hybrid" in w:
        return "Hybrid"
    return "On-site"


# ---------------------------------------------------------------------------
# LinkedIn guest API HTML parser
# ---------------------------------------------------------------------------

def _parse_guest_fragment(html: str) -> dict:
    """Parse the focused HTML fragment returned by the LinkedIn guest endpoint."""
    out = {
        "title": "", "company": "", "description": "",
        "employment_type": "", "seniority": "",
    }

    m = re.search(r'class="[^"]*topcard__title[^"]*"[^>]*>([^<]+)<', html)
    if not m:
        m = re.search(r'<h2[^>]*>([^<]+)</h2>', html)
    if m:
        out["title"] = m.group(1).strip()

    m = re.search(r'class="[^"]*topcard__org-name-link[^"]*"[^>]*>([^<]+)<', html)
    if not m:
        m = re.search(r'class="[^"]*topcard__flavor[^"]*"[^>]*>([^<]+)<', html)
    if m:
        out["company"] = m.group(1).strip()

    # Description block — two known class name patterns
    desc_m = re.search(
        r'class="[^"]*show-more-less-html__markup[^"]*"[^>]*>(.*?)</(?:section|div)>',
        html, flags=re.DOTALL | re.IGNORECASE,
    )
    if not desc_m:
        desc_m = re.search(
            r'class="[^"]*description__text[^"]*"[^>]*>(.*?)</(?:section|div)>',
            html, flags=re.DOTALL | re.IGNORECASE,
        )
    if desc_m:
        out["description"] = _strip_html(desc_m.group(1))

    # Criteria rows: Employment type, Seniority level, etc.
    for heading, value in re.findall(
        r'class="[^"]*description__job-criteria-subheader[^"]*"[^>]*>\s*([^<]+?)\s*</\w+>'
        r'.*?class="[^"]*description__job-criteria-text[^"]*"[^>]*>\s*([^<]+?)\s*</\w+>',
        html, flags=re.DOTALL | re.IGNORECASE,
    ):
        h = heading.strip().lower()
        v = value.strip()
        if "employment" in h:
            out["employment_type"] = v
        elif "seniority" in h:
            out["seniority"] = v

    return out


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------

def _location_and_arr(raw: str) -> tuple[str, str]:
    """Split 'Brisbane, QLD (Hybrid)' → ('Brisbane, QLD', 'Hybrid')."""
    arr_m = re.search(r"\(?(Remote|Hybrid|On[-\s]?site|In[-\s]?office)\)?", raw, re.IGNORECASE)
    arr = arr_m.group(1).title() if arr_m else ""
    location = re.sub(r"\s*\((?:Remote|Hybrid|On[-\s]?site|In[-\s]?office)\)", "", raw, flags=re.I).strip()
    return location, arr


def parse_input(path: str) -> list[dict]:
    p = Path(path)
    if p.suffix.lower() == ".csv":
        return _parse_csv(p)
    return _parse_text(p)


def _parse_text(p: Path) -> list[dict]:
    jobs = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = re.sub(r"^\d+\.\s*", "", line.strip())
        if not line:
            continue
        parts = [x.strip() for x in line.split("|")]
        url = next((x for x in parts if re.search(r"https?://", x)), None)
        if not url:
            continue
        location_raw = parts[2] if len(parts) > 2 else ""
        location, arr = _location_and_arr(location_raw)
        jobs.append({
            "Company": parts[0] if parts else "",
            "Job Title": parts[1] if len(parts) > 1 else "",
            "Job Location": location,
            "Work Arrangement": arr,
            "LinkedIn URL": url.strip(),
        })
    return jobs


def _parse_csv(p: Path) -> list[dict]:
    jobs = []
    with open(p, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            url_col = next(
                (k for k in row if "url" in k.lower() or "linkedin" in k.lower()),
                None,
            )
            if not url_col or not row[url_col]:
                continue
            url = row[url_col].strip()
            location_raw = row.get("Location", row.get("Job Location", ""))
            location, arr = _location_and_arr(location_raw)
            jobs.append({
                "Company": row.get("Company", ""),
                "Job Title": row.get("Job Title", row.get("Role", "")),
                "Job Location": location,
                "Work Arrangement": arr or row.get("Work Arrangement", ""),
                "LinkedIn URL": url,
            })
    return jobs


# ---------------------------------------------------------------------------
# 25-column tracker schema (matches the existing cleaned CSV)
# ---------------------------------------------------------------------------
TRACKER_COLS = [
    "Job Title", "Application Status", "Recruiter / Contact Name",
    "Application Stage", "Resume Used (Link)", "Resume Summary",
    "Company", "Job Description", "Key Skills", "Contact Details",
    "Date Posted", "LinkedIn URL", "Employment Type", "Match %",
    "Apply Link", "Industry Type", "Company Size", "Company Type",
    "Company Location (HQ)", "Company LinkedIn", "Job Location",
    "Work Arrangement", "Salary", "Source", "Citizenship Required",
]


def _to_row(job: dict, enriched: dict) -> dict:
    row = {col: "" for col in TRACKER_COLS}
    row["Job Title"] = enriched.get("title") or job.get("Job Title", "")
    row["Company"] = enriched.get("company") or job.get("Company", "")
    row["Job Location"] = job.get("Job Location", "")
    row["Work Arrangement"] = (
        job.get("Work Arrangement")
        or _extract_work_arr(enriched.get("description", ""), job.get("Job Location", ""))
    )
    row["LinkedIn URL"] = job.get("LinkedIn URL", "")
    row["Apply Link"] = job.get("LinkedIn URL", "")
    row["Source"] = "LinkedIn"
    row["Job Description"] = enriched.get("description", "")[:500]
    row["Key Skills"] = enriched.get("skills", "")
    row["Employment Type"] = enriched.get("employment_type", "")
    row["Salary"] = enriched.get("salary", "Not listed")
    row["Application Status"] = "Not Applied"
    row["Citizenship Required"] = "Not Stated"
    row["Date Posted"] = datetime.date.today().isoformat()
    return row


# ---------------------------------------------------------------------------
# Fetch loop
# ---------------------------------------------------------------------------

def fetch_all(jobs: list[dict], crawler: SuperpoweredCrawlerFinal, progress_cb=None) -> list[dict]:
    results = []
    total = len(jobs)

    for i, job in enumerate(jobs, 1):
        if progress_cb is not None:
            try:
                progress_cb(i, total)
            except Exception:
                pass  # progress reporting must never break fetching
        url = job["LinkedIn URL"]
        label = f"{job.get('Company', '?')} — {job.get('Job Title', '?')}"
        print(f"[{i}/{total}] {label}")

        jid_m = re.search(r"jobs/view/(\d+)", url)
        enriched: dict = {}
        description = ""
        method = "none"

        if jid_m:
            jid = jid_m.group(1)

            # --- Attempt 1: guest API ---
            guest_url = f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"
            r = crawler.fetch(guest_url)
            if r.status == 200 and r.text and len(r.text) > 400:
                parsed = _parse_guest_fragment(r.text)
                if parsed["description"]:
                    description = parsed["description"]
                    method = "guest_api"
                    enriched = {
                        "title": parsed["title"] or job.get("Job Title", ""),
                        "company": parsed["company"] or job.get("Company", ""),
                        "description": description,
                        "employment_type": parsed["employment_type"],
                        "skills": _extract_skills(description),
                        "salary": _extract_salary(description),
                    }
                    print(f"  [guest_api] {len(description)} chars ✓")

            # --- Attempt 2: public page JSON-LD ---
            if not description:
                pub_url = f"https://www.linkedin.com/jobs/view/{jid}"
                r2 = crawler.fetch(pub_url)
                if r2.status == 200 and r2.text:
                    sd = crawler.extract_structured_data(r2.text)
                    # Read raw jsonld list — NOT sd["jobs"] which truncates to 300 chars
                    jp = next(
                        (b for b in sd["jsonld"] if str(b.get("@type", "")).lower() == "jobposting"),
                        {},
                    )
                    raw_desc = jp.get("description", "")
                    if raw_desc:
                        description = _strip_html(raw_desc)
                        method = "public_jsonld"
                        org = jp.get("hiringOrganization") or {}
                        enriched = {
                            "title": jp.get("title") or job.get("Job Title", ""),
                            "company": (org.get("name") if isinstance(org, dict) else "") or job.get("Company", ""),
                            "description": description,
                            "employment_type": jp.get("employmentType", ""),
                            "skills": _extract_skills(description),
                            "salary": _extract_salary(description),
                        }
                        print(f"  [public_jsonld] {len(description)} chars ✓")

        else:
            # Non-LinkedIn URL (e.g. gradconnection.com) — fetch directly
            r = crawler.fetch(url)
            if r.status == 200 and r.text:
                sd = crawler.extract_structured_data(r.text)
                jp = next(
                    (b for b in sd["jsonld"] if str(b.get("@type", "")).lower() == "jobposting"),
                    {},
                )
                raw_desc = jp.get("description", "")
                if raw_desc:
                    description = _strip_html(raw_desc)
                else:
                    # Fallback: grab main text from page
                    description = clean_text(r.text)[:2000]
                method = "direct_fetch"
                enriched = {
                    "title": jp.get("title") or job.get("Job Title", ""),
                    "company": job.get("Company", ""),
                    "description": description,
                    "employment_type": jp.get("employmentType", ""),
                    "skills": _extract_skills(description),
                    "salary": _extract_salary(description),
                }
                print(f"  [direct_fetch] {len(description)} chars ✓")

        if not description:
            print(f"  [warn] no description retrieved (status={r.status if 'r' in dir() else '?'})")
            enriched = {
                "title": job.get("Job Title", ""),
                "company": job.get("Company", ""),
                "description": "",
                "employment_type": "",
                "skills": "",
                "salary": "Not listed",
            }

        results.append({"job": job, "enriched": enriched, "method": method})

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_output(results: list[dict], output_csv: str) -> None:
    out_path = Path(output_csv)
    json_path = out_path.with_suffix(".json")

    write_header = not out_path.exists()
    with open(out_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRACKER_COLS)
        if write_header:
            writer.writeheader()
        for r in results:
            writer.writerow(_to_row(r["job"], r["enriched"]))

    # JSON sidecar — full untruncated descriptions
    existing: list = []
    if json_path.exists():
        try:
            existing = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    sidecar = existing + [
        {
            "url": r["job"]["LinkedIn URL"],
            "company": r["enriched"].get("company", ""),
            "title": r["enriched"].get("title", ""),
            "method": r.get("method", ""),
            "description_full": r["enriched"].get("description", ""),
            "skills": r["enriched"].get("skills", ""),
            "employment_type": r["enriched"].get("employment_type", ""),
            "salary": r["enriched"].get("salary", ""),
        }
        for r in results
    ]
    json_path.write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")

    ok = sum(1 for r in results if r["enriched"].get("description"))
    print(f"\n[done] {ok}/{len(results)} descriptions fetched → {out_path} + {json_path}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch LinkedIn job descriptions from a URL list.")
    ap.add_argument("--input", required=True, help="Input file: .txt (pipe-delimited paste) or .csv")
    ap.add_argument("--output", default="fetched_jobs.csv", help="Output CSV (appends if exists)")
    ap.add_argument("--rps", type=float, default=0.2,
                    help="Requests/sec per host (default 0.2 = 1 req/5s, safe for LinkedIn)")
    ap.add_argument("--delay", type=float, default=4.0,
                    help="Polite delay between requests in seconds (default 4.0)")
    args = ap.parse_args()

    jobs = parse_input(args.input)
    if not jobs:
        print("No jobs found in input — check file format.", file=sys.stderr)
        sys.exit(1)
    print(f"Loaded {len(jobs)} jobs from {args.input}\n")

    crawler = SuperpoweredCrawlerFinal(
        respect_robots=False,   # LinkedIn's robots.txt blocks /jobs-guest/ — must override
        rps=args.rps,
        polite_delay=args.delay,
    )

    results = fetch_all(jobs, crawler)
    write_output(results, args.output)


if __name__ == "__main__":
    main()
