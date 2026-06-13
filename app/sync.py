"""app/sync.py — Zone 1 read-model loader.

Zone 1 tables are fully rebuilt on every sync (DROP → CREATE → INSERT).
Zone 2 tables (app_state, application_status, app_events) are never touched here.

Run:
    python app/sync.py          (from project root)
    python -m app.sync          (from project root)
"""
import csv
import json
import sqlite3
import glob
import os
import re
import datetime
import sys

# Ensure project root is on sys.path so `import csv_merge` works regardless of
# whether this file is run as a script or as a module.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

import csv_merge
from app import db


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


def normalize_company(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def infer_sector(text: str) -> str:
    t = (text or "").lower()
    rules = [
        ("energy",               ["energy", "electricity", "gas", "utilities", "renewables", "solar"]),
        ("health",               ["health", "hospital", "medical", "clinical", "aged care", "pharma", "ndis"]),
        ("government",           ["government", "council", "department of", "agency", "public sector", "defence"]),
        ("finance",              ["bank", "finance", "financial", "insurance", "super", "wealth", "fintech", "payments"]),
        ("retail",               ["retail", "ecommerce", "e-commerce", "consumer", "fmcg", "supermarket"]),
        ("technology",           ["software", "saas", "tech", "it services", "cloud", "data", "ai"]),
        ("telecom",              ["telecom", "telco", "network", "broadband", "mobile"]),
        ("transport_logistics",  ["logistics", "transport", "freight", "supply chain", "airline", "rail"]),
        ("education",            ["university", "education", "training", "school", "rto", "edtech"]),
        ("professional_services",["consulting", "advisory", "accounting", "legal", "recruitment"]),
    ]
    for sector, keywords in rules:
        if any(kw in t for kw in keywords):
            return sector
    return "other"


def _drop_create(con: sqlite3.Connection, ddl: str, table: str) -> None:
    con.execute(f"DROP TABLE IF EXISTS {table}")
    con.execute(ddl)


# ---------------------------------------------------------------------------
# Zone 1 DDL
# ---------------------------------------------------------------------------

_DDL_JOBS = """
CREATE TABLE jobs (
    row_key              TEXT PRIMARY KEY,
    company              TEXT,
    role_title           TEXT,
    location             TEXT,
    job_url              TEXT,
    match_score          INT,
    match_band           TEXT,
    matched_evidence     TEXT,
    gaps                 TEXT,
    why_keep             TEXT,
    job_text             TEXT,
    company_domain       TEXT,
    required_skills      TEXT,
    preferred_skills     TEXT,
    jd_posted_date       TEXT,
    jd_salary            TEXT,
    jd_employment_type   TEXT,
    authenticity_status  TEXT,
    authenticity_score   TEXT,
    duplicate_group_id   TEXT,
    duplicate_count      TEXT,
    source_platform      TEXT,
    sector               TEXT,
    apply_type           TEXT DEFAULT 'unknown',
    confidence           TEXT,
    unclassified_requirements TEXT,
    last_synced_at       TEXT
)
"""

_DDL_RESUME_PACKAGES = """
CREATE TABLE resume_packages (
    row_key                TEXT PRIMARY KEY,
    role_family            TEXT,
    resume_file            TEXT,
    cover_letter_file      TEXT,
    change_log_file        TEXT,
    match_report_file      TEXT,
    self_check_match_rate  INT,
    self_check_passes_target INT,
    hard_coverage_pct      INT,
    quantified_bullets     INT,
    word_count             INT,
    jd_terms_missing       TEXT,
    genuine_gaps           TEXT,
    generated_date         TEXT
)
"""

_DDL_COMPANIES = """
CREATE TABLE companies (
    company_key   TEXT PRIMARY KEY,
    company_name  TEXT,
    domain        TEXT,
    email_pattern TEXT,
    sector        TEXT
)
"""

_DDL_PEOPLE = """
CREATE TABLE people (
    person_key          TEXT PRIMARY KEY,
    company_key         TEXT,
    name                TEXT,
    role                TEXT,
    role_target         TEXT,
    verified_email      TEXT,
    verification_source TEXT,
    guessed_emails      TEXT,
    linkedin_url        TEXT
)
"""

_DDL_DRAFTS = """
CREATE TABLE drafts (
    draft_key     TEXT PRIMARY KEY,
    company       TEXT,
    person_slug   TEXT,
    filepath      TEXT,
    state         TEXT,
    followup_n    INT,
    last_modified TEXT
)
"""

_DDL_OUTREACH_LOG = """
CREATE TABLE outreach_log (
    id                  INTEGER PRIMARY KEY,
    company             TEXT,
    email               TEXT,
    person_name         TEXT,
    status              TEXT,
    sent_at             TEXT,
    replied_at          TEXT,
    followup_1_sent_at  TEXT,
    followup_2_sent_at  TEXT
)
"""


# ---------------------------------------------------------------------------
# Sync steps
# ---------------------------------------------------------------------------

def _sync_jobs(con: sqlite3.Connection, now: str) -> int:
    """Load job_posts.csv into jobs, then overlay match scores from matched_jobs.csv."""
    jp_path = os.path.join(_PROJECT_ROOT, "job_posts.csv")
    try:
        with open(jp_path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        print(f"  [SKIP] jobs: could not read job_posts.csv: {e}")
        return 0

    # Build jobs dict keyed by row_key
    jobs: dict[str, dict] = {}
    for row in rows:
        key = csv_merge.row_key(row)
        jobs[key] = {
            "row_key":             key,
            "company":             row.get("company", ""),
            "role_title":          row.get("role_title", ""),
            "location":            row.get("location", ""),
            "job_url":             row.get("job_url", ""),
            "match_score":         None,
            "match_band":          None,
            "matched_evidence":    None,
            "gaps":                None,
            "why_keep":            None,
            "job_text":            row.get("job_text", ""),
            "company_domain":      row.get("company_domain", ""),
            "required_skills":     row.get("required_skills", ""),
            "preferred_skills":    row.get("preferred_skills", ""),
            "jd_posted_date":      row.get("jd_posted_date", ""),
            "jd_salary":           row.get("jd_salary", ""),
            "jd_employment_type":  row.get("jd_employment_type", ""),
            "authenticity_status": row.get("authenticity_status", ""),
            "authenticity_score":  row.get("authenticity_score", ""),
            "duplicate_group_id":  row.get("duplicate_group_id", ""),
            "duplicate_count":     row.get("duplicate_count", ""),
            "source_platform":     row.get("source_platform", ""),
            "sector":              infer_sector(row.get("company", "") + " " + row.get("job_text", "")),
            "apply_type":          "unknown",
            "confidence":          "",
            "unclassified_requirements": "",
            "last_synced_at":      now,
        }

    # Overlay matched_jobs.csv
    mj_path = os.path.join(_PROJECT_ROOT, "matched_jobs.csv")
    try:
        with open(mj_path, newline="", encoding="utf-8") as f:
            matched_rows = list(csv.DictReader(f))
        for row in matched_rows:
            key = csv_merge.row_key(row)
            if key in jobs:
                jobs[key]["match_score"]      = row.get("match_score")
                jobs[key]["match_band"]       = row.get("match_band")
                jobs[key]["matched_evidence"] = row.get("matched_evidence")
                jobs[key]["gaps"]             = row.get("gaps")
                jobs[key]["why_keep"]         = row.get("why_keep")
                jobs[key]["confidence"]       = row.get("confidence", "")
                jobs[key]["unclassified_requirements"] = row.get("unclassified_requirements", "")
            else:
                # Thin insert for a matched job not in job_posts.csv
                jobs[key] = {
                    "row_key":             key,
                    "company":             row.get("company", ""),
                    "role_title":          row.get("role_title", ""),
                    "location":            row.get("location", ""),
                    "job_url":             row.get("job_url", ""),
                    "match_score":         row.get("match_score"),
                    "match_band":          row.get("match_band"),
                    "matched_evidence":    row.get("matched_evidence"),
                    "gaps":                row.get("gaps"),
                    "why_keep":            row.get("why_keep"),
                    "job_text":            "",
                    "company_domain":      "",
                    "required_skills":     "",
                    "preferred_skills":    "",
                    "jd_posted_date":      "",
                    "jd_salary":           "",
                    "jd_employment_type":  "",
                    "authenticity_status": "",
                    "authenticity_score":  "",
                    "duplicate_group_id":  "",
                    "duplicate_count":     "",
                    "source_platform":     "",
                    "sector":              infer_sector(row.get("company", "")),
                    "apply_type":          "unknown",
                    "confidence":          row.get("confidence", ""),
                    "unclassified_requirements": row.get("unclassified_requirements", ""),
                    "last_synced_at":      now,
                }
    except Exception as e:
        print(f"  [WARN] jobs: could not read matched_jobs.csv (match scores not applied): {e}")

    # Swap
    _drop_create(con, _DDL_JOBS, "jobs")
    con.executemany(
        """INSERT INTO jobs VALUES (
            :row_key,:company,:role_title,:location,:job_url,
            :match_score,:match_band,:matched_evidence,:gaps,:why_keep,
            :job_text,:company_domain,:required_skills,:preferred_skills,
            :jd_posted_date,:jd_salary,:jd_employment_type,
            :authenticity_status,:authenticity_score,
            :duplicate_group_id,:duplicate_count,
            :source_platform,:sector,:apply_type,:confidence,:unclassified_requirements,:last_synced_at
        )""",
        jobs.values(),
    )
    con.commit()

    # Populate job_seen: INSERT OR IGNORE preserves existing first_seen_date.
    today = now[:10]
    con.executemany(
        "INSERT OR IGNORE INTO job_seen(row_key, first_seen_date) VALUES(?, ?)",
        [(key, today) for key in jobs],
    )
    con.commit()
    return len(jobs)


def _sync_resume_packages(con: sqlite3.Connection) -> int:
    pattern = os.path.join(_PROJECT_ROOT, "resumes", "*_match_report.json")
    files = glob.glob(pattern)
    if not files:
        print("  [SKIP] resume_packages: no match report JSON files found")
        return 0

    packages: list[dict] = []
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:
            print(f"  [WARN] resume_packages: could not read {fpath}: {e}")
            continue

        job_url = d.get("job_url", "")
        key = csv_merge.canonical_url(job_url) if job_url else csv_merge.row_key(d)

        stem = os.path.basename(fpath)                          # e.g. acme_analyst_match_report.json
        base = stem.replace("_match_report.json", "")

        ats = d.get("ats_self_check", {})
        # jd_terms_missing may be a list
        missing = ats.get("hard_terms_missing") or d.get("jd_terms_missing") or []
        if isinstance(missing, list):
            missing = ", ".join(missing)
        gaps = ats.get("genuine_gaps_unsupported") or d.get("genuine_gaps") or []
        if isinstance(gaps, list):
            gaps = ", ".join(gaps)

        packages.append({
            "row_key":                   key,
            "role_family":               d.get("role_family", ""),
            "resume_file":               base + "_resume.docx",
            "cover_letter_file":         base + "_cover_letter.docx",
            "change_log_file":           base + "_change_log.md",
            "match_report_file":         stem,
            "self_check_match_rate":     ats.get("match_rate"),
            "self_check_passes_target":  1 if ats.get("passes_target") else 0,
            "hard_coverage_pct":         ats.get("hard_coverage_pct"),
            "quantified_bullets":        ats.get("quantified_bullets"),
            "word_count":                ats.get("word_count"),
            "jd_terms_missing":          missing,
            "genuine_gaps":              gaps,
            "generated_date":            d.get("generated_date", ""),
        })

    _drop_create(con, _DDL_RESUME_PACKAGES, "resume_packages")
    con.executemany(
        """INSERT INTO resume_packages VALUES (
            :row_key,:role_family,:resume_file,:cover_letter_file,
            :change_log_file,:match_report_file,
            :self_check_match_rate,:self_check_passes_target,
            :hard_coverage_pct,:quantified_bullets,:word_count,
            :jd_terms_missing,:genuine_gaps,:generated_date
        )""",
        packages,
    )
    con.commit()
    return len(packages)


def _sync_companies(con: sqlite3.Connection) -> int:
    cd_path = os.path.join(_PROJECT_ROOT, "company_domains.json")
    try:
        with open(cd_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"  [SKIP] companies: could not read company_domains.json: {e}")
        return 0

    rows: list[dict] = []
    for name, info in data.items():
        key = normalize_company(name)
        rows.append({
            "company_key":   key,
            "company_name":  name,
            "domain":        info.get("domain", ""),
            "email_pattern": info.get("pattern", ""),
            "sector":        infer_sector(name),
        })

    _drop_create(con, _DDL_COMPANIES, "companies")
    con.executemany(
        "INSERT INTO companies VALUES (:company_key,:company_name,:domain,:email_pattern,:sector)",
        rows,
    )
    con.commit()
    return len(rows)


def _sync_people(con: sqlite3.Connection) -> int:
    sp_path = os.path.join(_PROJECT_ROOT, "data", "seed_people_with_emails.csv")
    try:
        with open(sp_path, newline="", encoding="utf-8") as f:
            rows_csv = list(csv.DictReader(f))
    except Exception as e:
        print(f"  [SKIP] people: could not read seed_people_with_emails.csv: {e}")
        return 0

    rows: list[dict] = []
    for row in rows_csv:
        company_key = normalize_company(row.get("company_name", ""))
        name = row.get("name", "")
        person_key = company_key + "|" + slugify(name)
        rows.append({
            "person_key":          person_key,
            "company_key":         company_key,
            "name":                name,
            "role":                row.get("role", ""),
            "role_target":         row.get("role_target", ""),
            "verified_email":      row.get("verified_email", ""),
            "verification_source": row.get("verification_source", ""),
            "guessed_emails":      row.get("guessed_emails", ""),
            "linkedin_url":        row.get("linkedin_url", ""),
        })

    _drop_create(con, _DDL_PEOPLE, "people")
    con.executemany(
        """INSERT INTO people VALUES (
            :person_key,:company_key,:name,:role,:role_target,
            :verified_email,:verification_source,:guessed_emails,:linkedin_url
        )""",
        rows,
    )
    con.commit()
    return len(rows)


def _sync_drafts(con: sqlite3.Connection) -> int:
    draft_dir = os.path.join(_PROJECT_ROOT, "drafts")
    try:
        files = glob.glob(os.path.join(draft_dir, "*.md"))
    except Exception as e:
        print(f"  [SKIP] drafts: could not glob drafts/: {e}")
        return 0

    _STATE_PREFIXES = ("pending_review__", "ready_to_send__", "sent__", "skipped__")

    rows: list[dict] = []
    for fpath in files:
        fname = os.path.basename(fpath)

        state = "unknown"
        rest = fname
        for prefix in _STATE_PREFIXES:
            if fname.startswith(prefix):
                state = prefix.rstrip("_")
                rest = fname[len(prefix):]
                break

        # followup_n from 'followup{N}' in filename
        followup_n = None
        m = re.search(r"followup(\d+)", rest)
        if m:
            followup_n = int(m.group(1))

        # draft_key = rest (filename with state prefix removed)
        draft_key = rest

        # company and person_slug from rest: company__person_slug.md
        parts = rest.rstrip(".md").split("__", 1)
        company   = parts[0] if parts else ""
        person_slug = parts[1].replace(".md", "") if len(parts) > 1 else ""

        try:
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fpath)).isoformat(timespec="seconds")
        except Exception:
            mtime = ""

        rows.append({
            "draft_key":    draft_key,
            "company":      company,
            "person_slug":  person_slug,
            "filepath":     fpath,
            "state":        state,
            "followup_n":   followup_n,
            "last_modified": mtime,
        })

    _drop_create(con, _DDL_DRAFTS, "drafts")
    con.executemany(
        """INSERT INTO drafts VALUES (
            :draft_key,:company,:person_slug,:filepath,:state,:followup_n,:last_modified
        )""",
        rows,
    )
    con.commit()
    return len(rows)


def _sync_outreach_log(con: sqlite3.Connection) -> int:
    db_path = os.path.join(_PROJECT_ROOT, "outreach.db")
    if not os.path.exists(db_path):
        print("  [SKIP] outreach_log: outreach.db not found")
        _drop_create(con, _DDL_OUTREACH_LOG, "outreach_log")
        con.commit()
        return 0

    try:
        src = sqlite3.connect(db_path)
        src.row_factory = sqlite3.Row
        # Check the table exists
        tbl_check = src.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='outreach'"
        ).fetchone()
        if not tbl_check:
            print("  [SKIP] outreach_log: outreach.db has no 'outreach' table yet")
            src.close()
            _drop_create(con, _DDL_OUTREACH_LOG, "outreach_log")
            con.commit()
            return 0

        # Read all columns that exist in the source table
        src_cols_raw = src.execute("PRAGMA table_info(outreach)").fetchall()
        src_col_names = {r["name"] for r in src_cols_raw}

        target_cols = ["company", "email", "person_name", "status", "sent_at",
                       "replied_at", "followup_1_sent_at", "followup_2_sent_at"]
        select_cols = [c if c in src_col_names else f"NULL AS {c}" for c in target_cols]
        query = f"SELECT {', '.join(select_cols)} FROM outreach"

        source_rows = src.execute(query).fetchall()
        src.close()
    except Exception as e:
        print(f"  [SKIP] outreach_log: error reading outreach.db: {e}")
        _drop_create(con, _DDL_OUTREACH_LOG, "outreach_log")
        con.commit()
        return 0

    rows = [dict(r) for r in source_rows]

    _drop_create(con, _DDL_OUTREACH_LOG, "outreach_log")
    con.executemany(
        """INSERT INTO outreach_log
           (company, email, person_name, status, sent_at, replied_at,
            followup_1_sent_at, followup_2_sent_at)
           VALUES (:company,:email,:person_name,:status,:sent_at,:replied_at,
                   :followup_1_sent_at,:followup_2_sent_at)""",
        rows,
    )
    con.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def sync(con: sqlite3.Connection, now: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    counts["jobs"]            = _sync_jobs(con, now)
    counts["resume_packages"] = _sync_resume_packages(con)
    counts["companies"]       = _sync_companies(con)
    counts["people"]          = _sync_people(con)
    counts["drafts"]          = _sync_drafts(con)
    counts["outreach_log"]    = _sync_outreach_log(con)
    return counts


def main() -> None:
    con = db.connect()
    db.init_schema(con)
    now = datetime.datetime.now().isoformat(timespec="seconds")
    counts = sync(con, now)
    con.close()
    print("Sync complete:")
    for table, n in counts.items():
        print(f"  {table:<22} {n} rows")


if __name__ == "__main__":
    main()
