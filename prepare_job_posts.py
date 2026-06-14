"""Prepare raw job-alert rows for resume/JD scoring.

This is the intake layer for LinkedIn/SEEK/Gmail alerts:
raw alert rows -> deduped + authenticity-flagged job_posts.csv.

Standard library only. No scraping, no mailbox access, no company lookups.
"""

import argparse
import csv
import re
import sys
import unicodedata
import urllib.parse
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path


BASE_COLUMNS = [
    "company",
    "company_domain",
    "role_title",
    "location",
    "job_url",
    "job_text",
    "required_skills",
    "preferred_skills",
    "notes",
]

INTAKE_COLUMNS = [
    "source_platform",
    "posted_date",
    "alert_query",
    "authenticity_status",
    "authenticity_score",
    "authenticity_notes",
    "duplicate_group_id",
    "duplicate_count",
    "duplicate_sources",
]

REVIEW_COLUMNS = BASE_COLUMNS + [
    "source_platform",
    "posted_date",
    "alert_query",
    "canonical_url",
    "dedupe_key",
    "duplicate_group_id",
    "duplicate_status",
    "duplicate_of",
    "duplicate_reason",
    "authenticity_status",
    "authenticity_score",
    "authenticity_notes",
    "keep_for_scoring",
]

FIELD_ALIASES = {
    "company": ["company", "company_name", "employer", "organisation", "organization"],
    "company_domain": ["company_domain", "domain", "company_website", "website"],
    "role_title": ["role_title", "job_title", "title", "position"],
    "location": ["location", "job_location", "work_location"],
    "job_url": ["job_url", "url", "link", "apply_url", "posting_url"],
    "job_text": ["job_text", "job_description", "description", "jd", "summary"],
    "required_skills": ["required_skills", "requirements", "must_have", "required"],
    "preferred_skills": ["preferred_skills", "nice_to_have", "preferred"],
    "notes": ["notes", "note"],
    "source_platform": ["source_platform", "source", "job_source", "alert_source"],
    "posted_date": ["posted_date", "date_posted", "email_date", "alert_date"],
    "alert_query": ["alert_query", "saved_search", "search_query", "gmail_subject"],
}

TRUSTED_JOB_BOARD_HOSTS = {
    "linkedin.com",
    "seek.com.au",
    "indeed.com",
    "au.indeed.com",
    "jora.com",
    "ethicaljobs.com.au",
    "apsjobs.gov.au",
    "careers.vic.gov.au",
    "iworkfor.nsw.gov.au",
    "smartjobs.qld.gov.au",
}

TRUSTED_ATS_HOSTS = {
    "myworkdayjobs.com",
    "workdayjobs.com",
    "greenhouse.io",
    "boards.greenhouse.io",
    "lever.co",
    "jobs.lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "workable.com",
    "bamboohr.com",
    "jobvite.com",
    "icims.com",
    "successfactors.com",
    "taleo.net",
    "oraclecloud.com",
    "pageuppeople.com",
    "recruitee.com",
    "comeet.com",
    "breezy.hr",
    "pinpointhq.com",
    "jobadder.com",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "hotmail.com",
    "outlook.com",
    "yahoo.com",
    "icloud.com",
    "live.com",
    "proton.me",
    "protonmail.com",
}

AGENCY_TERMS = {
    "recruitment",
    "recruiter",
    "recruiting",
    "staffing",
    "talent",
    "hays",
    "randstad",
    "michael page",
    "peoplebank",
    "finite",
    "paxus",
    "robert half",
    "hudson",
    "ignite",
    "ambition",
    "halcyon knights",
    "six degrees",
    "sharp & carter",
    "sharp and carter",
    "corestaff",
    "mind recruitment",
}

END_EMPLOYER_UNKNOWN_PATTERNS = [
    r"\bour client\b",
    r"\bconfidential\b",
    r"\bundisclosed\b",
    r"\bclient is\b",
    r"\bon behalf of\b",
    r"\bleading client\b",
]

SCAM_PATTERNS = [
    r"\bwhatsapp\b",
    r"\btelegram\b",
    r"\bsignal app\b",
    r"\bpay (a|the)?\s*(fee|deposit|registration)\b",
    r"\btraining fee\b",
    r"\bcrypto\b",
    r"\binvestment opportunity\b",
    r"\bno experience required\b.*\bhigh income\b",
    r"\bwork from home\b.*\bweekly pay\b",
    r"\babn only\b",
]

CORPORATE_SUFFIXES = {
    "pty",
    "ltd",
    "limited",
    "inc",
    "llc",
    "plc",
    "corp",
    "corporation",
    "co",
    "company",
    "australia",
    "australian",
    "group",
}

LOCATION_STOPWORDS = {
    "australia",
    "vic",
    "victoria",
    "nsw",
    "qld",
    "wa",
    "sa",
    "tas",
    "act",
    "nt",
    "hybrid",
    "remote",
    "onsite",
    "on",
    "site",
    "full",
    "time",
}


@dataclass
class Authenticity:
    status: str
    score: int
    notes: list

    @property
    def keep_for_scoring(self):
        return not self.status.startswith("reject")


def clean_text(value):
    value = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", " ", value).strip()


def lower_ascii(value):
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    return value.lower()


def first_value(row, names):
    low = {str(k).strip().lower(): v for k, v in row.items() if k is not None}
    for name in names:
        if name in low and clean_text(low[name]):
            return clean_text(low[name])
    return ""


def standardize_row(row, idx):
    out = {col: first_value(row, FIELD_ALIASES[col]) for col in FIELD_ALIASES}
    alias_names = {name for names in FIELD_ALIASES.values() for name in names}
    for key, value in row.items():
        if key is None:
            continue
        clean_key = str(key).strip()
        low_key = clean_key.lower()
        if not clean_key or clean_key.startswith("_"):
            continue
        if clean_key in out or low_key in alias_names:
            continue
        out[clean_key] = clean_text(value)
    for col in BASE_COLUMNS + ["source_platform", "posted_date", "alert_query"]:
        out.setdefault(col, "")
    out["_row_id"] = str(idx)
    out["_raw"] = row
    out["company_domain"] = normalize_domain(out["company_domain"])
    return out


def normalize_domain(value):
    value = lower_ascii(value).strip()
    value = re.sub(r"^https?://", "", value)
    value = value.split("/", 1)[0].split("@")[-1].strip(".")
    if value.startswith("www."):
        value = value[4:]
    if not value or " " in value or "." not in value:
        return ""
    if not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", value):
        return ""
    return value


def url_host(url):
    if not url:
        return ""
    raw = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.I):
        raw = "https://" + raw
    try:
        host = urllib.parse.urlsplit(raw).netloc.lower()
    except ValueError:
        return ""
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def registrable_domain(host):
    host = normalize_domain(host)
    if not host:
        return ""
    labels = host.split(".")
    while labels and labels[0] in {"www", "careers", "career", "jobs", "job"}:
        labels = labels[1:]
    if len(labels) >= 3 and labels[-2] in {"com", "net", "org", "gov", "edu", "co"} and len(labels[-1]) == 2:
        return ".".join(labels[-3:])
    if len(labels) >= 2:
        return ".".join(labels[-2:])
    return host


def host_matches_domain(host, domain):
    if not host or not domain:
        return False
    host_root = registrable_domain(host)
    domain_root = registrable_domain(domain)
    return bool(host_root and domain_root and host_root == domain_root)


def trusted_host_kind(host):
    root = registrable_domain(host)
    all_hosts = {host, root}
    if all_hosts & TRUSTED_JOB_BOARD_HOSTS:
        if "linkedin.com" in all_hosts:
            return "linkedin"
        if "seek.com.au" in all_hosts:
            return "seek"
        return "trusted_job_board"
    if all_hosts & TRUSTED_ATS_HOSTS:
        return "trusted_ats"
    return ""


def infer_source_platform(row):
    existing = clean_text(row.get("source_platform", ""))
    if existing:
        return existing
    host = url_host(row.get("job_url", ""))
    kind = trusted_host_kind(host)
    if kind == "linkedin":
        return "LinkedIn"
    if kind == "seek":
        return "SEEK"
    if kind == "trusted_ats":
        return "Trusted ATS"
    if kind == "trusted_job_board":
        return "Trusted job board"
    if host and host_matches_domain(host, row.get("company_domain", "")):
        return "Company careers site"
    if host:
        return host
    return ""


def canonicalize_url(url):
    if not url:
        return ""
    raw = url.strip()
    if not re.match(r"^[a-z][a-z0-9+.-]*://", raw, flags=re.I):
        raw = "https://" + raw
    try:
        parts = urllib.parse.urlsplit(raw)
    except ValueError:
        return ""
    host = (parts.netloc or "").lower().split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    path = re.sub(r"/+", "/", parts.path or "/").rstrip("/")
    query = urllib.parse.parse_qs(parts.query)

    if host.endswith("linkedin.com"):
        m = re.search(r"/jobs/view/(\d+)", path)
        jid = m.group(1) if m else (query.get("currentJobId") or query.get("jobId") or [""])[0]
        return f"linkedin:{jid}" if jid else f"{host}{path}".lower()
    if host.endswith("seek.com.au"):
        m = re.search(r"/job/(\d+)", path)
        return f"seek:{m.group(1)}" if m else f"{host}{path}".lower()
    if "indeed." in host:
        jk = (query.get("jk") or [""])[0]
        return f"indeed:{jk}" if jk else f"{host}{path}".lower()

    keep_params = {}
    for key in ("gh_jid", "jobId", "job_id", "req_id", "requisitionId", "id"):
        if query.get(key):
            keep_params[key] = query[key][0]
    q = urllib.parse.urlencode(sorted(keep_params.items()))
    return f"{host}{path}?{q}".lower() if q else f"{host}{path}".lower()


def token_set(value):
    return set(re.findall(r"[a-z0-9]+", lower_ascii(value)))


def normalized_company(value):
    toks = [t for t in token_set(value) if t not in CORPORATE_SUFFIXES]
    return " ".join(sorted(toks))


def normalized_title(value):
    value = lower_ascii(value).replace("&", " and ")
    toks = re.findall(r"[a-z0-9]+", value)
    drop = {"full", "time", "part", "contract", "temporary", "permanent"}
    return " ".join(t for t in toks if t not in drop)


def normalized_location(value):
    toks = sorted(t for t in token_set(value) if t not in LOCATION_STOPWORDS)
    return " ".join(toks[:4])


def text_signature(row):
    text = " ".join([
        row.get("role_title", ""),
        row.get("job_text", ""),
        row.get("required_skills", ""),
        row.get("preferred_skills", ""),
    ])
    toks = [t for t in re.findall(r"[a-z0-9]+", lower_ascii(text)) if len(t) > 2]
    return set(toks)


def similarity(a, b):
    a, b = a or "", b or ""
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def jaccard(a, b):
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def row_identity(row):
    domain = row.get("company_domain") or ""
    company = normalized_company(row.get("company", ""))
    title = normalized_title(row.get("role_title", ""))
    location = normalized_location(row.get("location", ""))
    return {
        "url": canonicalize_url(row.get("job_url", "")),
        "domain_or_company": registrable_domain(domain) or company,
        "company": company,
        "title": title,
        "location": location,
        "sig": text_signature(row),
    }


def duplicate_reason(candidate, primary):
    c, p = candidate["_identity"], primary["_identity"]
    if c["url"] and p["url"] and c["url"] == p["url"]:
        return "same canonical job URL"

    same_company = c["domain_or_company"] and c["domain_or_company"] == p["domain_or_company"]
    title_sim = similarity(c["title"], p["title"])
    loc_same = (not c["location"] or not p["location"] or c["location"] == p["location"])
    text_sim = jaccard(c["sig"], p["sig"])

    if same_company and loc_same and title_sim >= 0.92:
        return f"same company/location and near-identical title ({title_sim:.2f})"
    if same_company and title_sim >= 0.86 and text_sim >= 0.45:
        return f"same company/title with overlapping JD text ({text_sim:.2f})"
    return ""


def source_quality(row):
    auth = row.get("_auth")
    auth_score = auth.score if auth else 0
    text_len = len(row.get("job_text", ""))
    has_domain = 10 if row.get("company_domain") else 0
    has_url = 8 if row.get("job_url") else 0
    official = 12 if host_matches_domain(url_host(row.get("job_url", "")), row.get("company_domain", "")) else 0
    return auth_score + min(text_len // 300, 12) + has_domain + has_url + official


def merge_rows(rows):
    primary = max(rows, key=source_quality).copy()
    for col in BASE_COLUMNS:
        values = [clean_text(r.get(col, "")) for r in rows if clean_text(r.get(col, ""))]
        if not values:
            continue
        if col == "job_text":
            primary[col] = max(values, key=len)
        elif col in {"required_skills", "preferred_skills", "notes"}:
            seen, merged = set(), []
            for value in values:
                for part in re.split(r"\s*(?:;|\|\|)\s*", value):
                    part = clean_text(part)
                    key = part.lower()
                    if part and key not in seen:
                        seen.add(key)
                        merged.append(part)
            primary[col] = "; ".join(merged)
        elif not primary.get(col):
            primary[col] = values[0]

    platforms = sorted({infer_source_platform(r) for r in rows if infer_source_platform(r)})
    primary["source_platform"] = "; ".join(platforms)
    for row in rows:
        for key, value in row.items():
            if key.startswith("_") or key in BASE_COLUMNS or key in INTAKE_COLUMNS:
                continue
            if not primary.get(key) and clean_text(value):
                primary[key] = clean_text(value)
    primary["duplicate_count"] = str(len(rows))
    primary["duplicate_sources"] = "; ".join(sorted({
        infer_source_platform(r) or url_host(r.get("job_url", "")) or "unknown"
        for r in rows
    }))
    return primary


def has_any_pattern(text, patterns):
    return [p for p in patterns if re.search(p, text, flags=re.I)]


def is_agency_listing(row):
    haystack = lower_ascii(" ".join([
        row.get("company", ""),
        row.get("source_platform", ""),
        row.get("notes", ""),
        row.get("job_text", "")[:1200],
    ]))
    return any(term in haystack for term in AGENCY_TERMS) or bool(has_any_pattern(haystack, END_EMPLOYER_UNKNOWN_PATTERNS))


def assess_authenticity(row):
    notes = []
    score = 50
    company = row.get("company", "")
    role = row.get("role_title", "")
    url = row.get("job_url", "")
    domain = row.get("company_domain", "")
    host = url_host(url)
    root = registrable_domain(host)
    kind = trusted_host_kind(host)
    text = lower_ascii(" ".join([
        company,
        role,
        row.get("job_text", ""),
        row.get("required_skills", ""),
        row.get("notes", ""),
    ]))

    if company:
        score += 10
    else:
        score -= 25
        notes.append("missing company name")
    if role:
        score += 10
    else:
        score -= 25
        notes.append("missing role title")
    if url:
        score += 10
    else:
        score -= 15
        notes.append("missing job URL")
    if len(row.get("job_text", "")) >= 350 or row.get("required_skills"):
        score += 10
    else:
        notes.append("short or missing JD text")
    if row.get("location"):
        score += 5

    if domain:
        if domain in FREE_EMAIL_DOMAINS:
            score -= 35
            notes.append(f"company_domain is a free email domain ({domain})")
        else:
            score += 8
    elif host and not kind:
        inferred = root
        if inferred and inferred not in FREE_EMAIL_DOMAINS:
            row["company_domain"] = row.get("company_domain") or inferred
            score += 5
            notes.append(f"inferred company_domain from job URL ({inferred})")
    else:
        score -= 5
        notes.append("company domain not provided")

    if host and host_matches_domain(host, row.get("company_domain", "")):
        score += 20
        notes.append("job URL host matches company domain")
    elif kind in {"linkedin", "seek", "trusted_job_board"}:
        score += 22
        notes.append(f"trusted job-board source ({kind.replace('_', ' ')})")
    elif kind == "trusted_ats":
        score += 18
        notes.append("trusted ATS source")
    elif host:
        score -= 8
        notes.append(f"unrecognized job URL host ({host})")

    agency = is_agency_listing(row)
    if agency:
        score -= 12
        notes.append("agency/recruiter or end-employer-unclear listing")

    scam_hits = has_any_pattern(text, SCAM_PATTERNS)
    if scam_hits:
        score -= 55
        notes.append("suspicious wording: " + ", ".join(scam_hits[:3]))

    score = max(0, min(100, score))
    if scam_hits or score < 30 or not company or not role:
        status = "reject_suspicious"
    elif agency:
        status = "agency_or_recruiter_listing"
    elif host and host_matches_domain(host, row.get("company_domain", "")) and score >= 80:
        status = "verified_company_source"
    elif kind in {"linkedin", "seek", "trusted_job_board", "trusted_ats"} and score >= 70:
        status = "trusted_alert_source"
    elif score >= 45:
        status = "needs_company_confirmation"
    else:
        status = "reject_low_authenticity"

    if not notes:
        notes.append("no authenticity issues found")
    return Authenticity(status, score, notes)


def group_duplicates(rows):
    groups = []
    review = []
    for row in rows:
        row["_identity"] = row_identity(row)
        row["_auth"] = assess_authenticity(row)
        row["source_platform"] = infer_source_platform(row)

        matched = None
        reason = ""
        for group in groups:
            for existing in group:
                reason = duplicate_reason(row, existing)
                if reason:
                    matched = group
                    break
            if matched is not None:
                break
        if matched is None:
            groups.append([row])
        else:
            matched.append(row)

    out_rows = []
    for idx, group in enumerate(groups, start=1):
        group_id = f"J{idx:04d}"
        primary = merge_rows(group)
        primary["_auth"] = assess_authenticity(primary)
        primary["_identity"] = row_identity(primary)
        primary["duplicate_group_id"] = group_id
        primary["authenticity_status"] = primary["_auth"].status
        primary["authenticity_score"] = str(primary["_auth"].score)
        primary["authenticity_notes"] = "; ".join(primary["_auth"].notes)
        out_rows.append(primary)

        primary_key = canonicalize_url(primary.get("job_url", "")) or (
            f"{primary.get('company', '')} | {primary.get('role_title', '')}"
        )
        for original in group:
            reason = "primary row"
            duplicate_status = "primary"
            duplicate_of = ""
            if original is not max(group, key=source_quality):
                duplicate_status = "duplicate"
                duplicate_of = primary_key
                reason = duplicate_reason(original, primary) or "lower-quality duplicate in same group"
            auth = original["_auth"]
            review.append(review_row(
                original,
                group_id,
                duplicate_status,
                duplicate_of,
                reason,
                auth,
            ))
    return out_rows, review


def review_row(row, group_id, duplicate_status, duplicate_of, reason, auth):
    data = {
        key: value
        for key, value in row.items()
        if not key.startswith("_")
    }
    for col in BASE_COLUMNS:
        data.setdefault(col, "")
    data.update({
        "source_platform": infer_source_platform(row),
        "posted_date": row.get("posted_date", ""),
        "alert_query": row.get("alert_query", ""),
        "canonical_url": canonicalize_url(row.get("job_url", "")),
        "dedupe_key": " | ".join([
            row["_identity"]["domain_or_company"],
            row["_identity"]["title"],
            row["_identity"]["location"],
        ]).strip(" |"),
        "duplicate_group_id": group_id,
        "duplicate_status": duplicate_status,
        "duplicate_of": duplicate_of,
        "duplicate_reason": reason,
        "authenticity_status": auth.status,
        "authenticity_score": str(auth.score),
        "authenticity_notes": "; ".join(auth.notes),
        "keep_for_scoring": "yes" if auth.keep_for_scoring and duplicate_status == "primary" else "no",
    })
    return data


def read_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit(f"[!] No header row found in {path}.")
        return [standardize_row(row, idx) for idx, row in enumerate(reader, start=1)]


def write_csv(path, rows, fieldnames):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def output_fieldnames(rows):
    extras = []
    for row in rows:
        for key in row:
            if key.startswith("_") or key in BASE_COLUMNS or key in INTAKE_COLUMNS:
                continue
            if key not in extras:
                extras.append(key)
    return BASE_COLUMNS + extras + INTAKE_COLUMNS


def review_fieldnames(rows):
    extras = []
    for row in rows:
        for key in row:
            if key.startswith("_") or key in REVIEW_COLUMNS:
                continue
            if key not in extras:
                extras.append(key)
    return REVIEW_COLUMNS + extras


def prepare(input_path, out_path, review_path, rejected_path):
    rows = read_rows(input_path)
    deduped, review = group_duplicates(rows)
    kept = [r for r in deduped if r["_auth"].keep_for_scoring]
    rejected = [r for r in deduped if not r["_auth"].keep_for_scoring]

    write_csv(out_path, kept, output_fieldnames(kept))
    write_csv(review_path, review, review_fieldnames(review))
    if rejected_path:
        rejected_rows = []
        for row in rejected:
            rejected_rows.append({
                **{col: row.get(col, "") for col in BASE_COLUMNS},
                "source_platform": row.get("source_platform", ""),
                "authenticity_status": row["_auth"].status,
                "authenticity_score": str(row["_auth"].score),
                "authenticity_notes": "; ".join(row["_auth"].notes),
                "duplicate_group_id": row.get("duplicate_group_id", ""),
                "duplicate_sources": row.get("duplicate_sources", ""),
            })
        write_csv(rejected_path, rejected_rows, BASE_COLUMNS + [
            "source_platform",
            "authenticity_status",
            "authenticity_score",
            "authenticity_notes",
            "duplicate_group_id",
            "duplicate_sources",
        ])

    duplicate_rows = sum(1 for row in review if row["duplicate_status"] == "duplicate")
    statuses = defaultdict(int)
    for row in deduped:
        statuses[row["_auth"].status] += 1
    return {
        "raw": len(rows),
        "deduped": len(deduped),
        "kept": len(kept),
        "rejected": len(rejected),
        "duplicates": duplicate_rows,
        "statuses": dict(sorted(statuses.items())),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Deduplicate and authenticity-flag LinkedIn/SEEK/Gmail job alert rows before scoring."
    )
    parser.add_argument("--input", default=None, help="Alert/enriched CSV to prepare (default: job_posts_enriched.csv if present, else job_alerts_raw.csv)")
    parser.add_argument("--out", default="job_posts.csv", help="Deduped CSV passed into the match scoring step")
    parser.add_argument("--review", default="job_post_intake_review.csv", help="Full duplicate/authenticity review CSV")
    parser.add_argument("--rejected", default="job_posts_rejected_authenticity.csv", help="Rejected suspicious or unusable rows")
    args = parser.parse_args(argv)

    # The pipeline (run_pipeline.py / daily_run.py) invokes this with no args; prefer the
    # JD-enriched file so fetched job descriptions aren't dropped. Raw alerts are the
    # fallback for a first run before any JD enrichment exists.
    if args.input is None:
        args.input = "job_posts_enriched.csv" if Path("job_posts_enriched.csv").exists() else "job_alerts_raw.csv"

    summary = prepare(Path(args.input), Path(args.out), Path(args.review), Path(args.rejected))
    print(f"[*] Raw rows: {summary['raw']}")
    print(f"[*] Unique job groups: {summary['deduped']} ({summary['duplicates']} duplicate row(s) removed)")
    print(f"[*] Kept for scoring: {summary['kept']} | rejected before scoring: {summary['rejected']}")
    print("[*] Authenticity statuses: " + ", ".join(
        f"{k}:{v}" for k, v in summary["statuses"].items()
    ))
    print(f"[*] Wrote {args.out}")
    print(f"[*] Review: {args.review}")
    if args.rejected:
        print(f"[*] Rejected: {args.rejected}")


if __name__ == "__main__":
    main()
