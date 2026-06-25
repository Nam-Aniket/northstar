"""Read/write helpers over control_panel.db for the web app."""
from __future__ import annotations

import datetime
import html as _html
import json
import os
import re
import difflib
import threading
from pathlib import Path

from app import db
from app import freshness

# Serialises the editor's claim-a-skill path (add_supported_skill -> reset_banks
# -> rescore_job). FastAPI runs sync routes in a threadpool, so two near-
# simultaneous claims could otherwise interleave their read-modify-write of
# skills.json (lost update) or clear the lazy skill banks mid-score. Single-user
# app, so contention is effectively zero and a coarse lock is free.
SKILLS_LOCK = threading.Lock()


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


JOB_SELECT = """
SELECT j.*,
       s.starred, s.dismissed, s.notes, s.applied_at,
       a.status AS app_status,
       r.resume_file, r.cover_letter_file, r.self_check_match_rate,
       r.self_check_passes_target, r.hard_coverage_pct, r.quantified_bullets,
       r.word_count, r.jd_terms_missing, r.genuine_gaps, r.role_family,
       js.first_seen_date
FROM jobs j
LEFT JOIN app_state s          ON j.row_key = s.row_key
LEFT JOIN application_status a ON j.row_key = a.row_key
LEFT JOIN resume_packages r    ON j.row_key = r.row_key
LEFT JOIN job_seen js          ON j.row_key = js.row_key
"""

STATUS_FLOW = ["new", "applied", "phone_screen", "interview", "offer", "rejected", "closed"]
PERSON_STATUS_FLOW = ["not_contacted", "contacted", "followup_due", "replied", "closed"]


def normalize_company(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")




def company_core(name: str) -> str:
    """
    Normalize company name to a core for fuzzy matching.
    Strips punctuation, normalizes whitespace, and removes legal entity suffixes.
    """
    # Normalize: lowercase + strip punctuation + normalize whitespace
    s = (name or "").lower()
    # Remove punctuation
    s = re.sub(r"[.,&]", " ", s)
    # Normalize whitespace
    s = re.sub(r"\s+", " ", s).strip()
    
    # Strip legal/entity suffix tokens
    suffix_tokens = {
        "pty", "ltd", "limited", "pvt", "private", "inc", "incorporated",
        "llc", "corp", "corporation", "co", "company", "gmbh", "ag", "sa",
        "plc", "group", "holdings", "technologies", "technology", "labs",
        "software", "solutions", "systems"
    }
    
    tokens = s.split()
    # Remove trailing suffix tokens
    while tokens and tokens[-1] in suffix_tokens:
        tokens.pop()
    
    result = " ".join(tokens)
    # Convert to slugify form (spaces -> underscores)
    return slugify(result)


def resolve_company(typed: str, candidates: list) -> tuple:
    """
    Resolve a typed company name to the best matching company key.
    Returns (company_key, company_name, match_type).
    
    Match types: "exact", "suffix", "fuzzy", "new"
    """
    if not candidates:
        return (slugify(typed), typed, "new")
    
    typed_normalized = normalize_company(typed)
    typed_core = company_core(typed)
    
    # 1. Exact match
    for cand in candidates:
        if normalize_company(cand.get("company_name", "")) == typed_normalized:
            return (cand["company_key"], cand["company_name"], "exact")
    
    # 2. Suffix match (core equality)
    if typed_core:  # Only if non-empty core
        for cand in candidates:
            cand_core = company_core(cand.get("company_name", ""))
            if typed_core == cand_core and cand_core:
                return (cand["company_key"], cand["company_name"], "suffix")
    
    # 3. Fuzzy match (difflib >= 0.90)
    if typed_core:
        best_ratio = 0
        best_match = None
        for cand in candidates:
            cand_core = company_core(cand.get("company_name", ""))
            if not cand_core:
                continue
            ratio = difflib.SequenceMatcher(None, typed_core, cand_core).ratio()
            if ratio >= 0.90 and ratio > best_ratio:
                best_ratio = ratio
                best_match = cand
        
        if best_match:
            # Fuzzy match: suggestion but not auto-linked
            return (best_match["company_key"], best_match["company_name"], "fuzzy")
    
    # 4. New company
    return (slugify(typed), typed, "new")


def ingest_people(con, company_key: str, company_name: str, people: list) -> dict:
    """
    Upsert people into tracker_people.
    ON CONFLICT omits outreach_status and notes (preserves workflow state).
    
    Returns {added, updated, needs_review, skipped}.
    """
    now = _now()
    added = 0
    updated = 0
    needs_review = 0
    skipped = 0
    
    seen_emails = set()
    
    for person in people:
        name = person.get("name", "")
        title = person.get("title", "")
        email = person.get("email", "")
        is_review = person.get("needs_review", 0)
        pattern = person.get("pattern", "")
        
        if not name or not email:
            continue
        
        # Dedup within paste on email
        if email in seen_emails:
            skipped += 1
            continue
        seen_emails.add(email)
        
        # Check if email already exists under same company
        existing = con.execute(
            "SELECT person_key FROM tracker_people WHERE email = ? AND company_key = ?",
            (email, company_key)
        ).fetchone()
        
        if existing:
            # Update: refresh title/email/pattern but preserve status/notes
            con.execute("""
                UPDATE tracker_people
                SET title = ?, pattern = ?, updated_at = ?
                WHERE email = ? AND company_key = ?
            """, (title, pattern, now, email, company_key))
            updated += 1
        else:
            # Insert new
            person_key = f"{company_key}|{slugify(name)}"
            con.execute("""
                INSERT OR IGNORE INTO tracker_people
                (person_key, company_key, company_name, name, title, email, pattern,
                 outreach_status, notes, needs_review, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                person_key, company_key, company_name, name, title, email, pattern,
                "not_contacted", "", is_review, now, now
            ))
            added += 1
            if is_review:
                needs_review += 1
    
    con.commit()
    return {"added": added, "updated": updated, "needs_review": needs_review, "skipped": skipped}


def tracker_table(con, q=None, company=None, status=None, sort=None, dir=None) -> list[dict]:
    """
    Load tracker_people with jobs attached. Filter and sort.
    Emit one placeholder row per company with jobs but no people.
    """
    # Load all tracker_people
    rows = con.execute("""
        SELECT person_key, company_key, company_name, name, title, email,
               outreach_status, notes, needs_review
        FROM tracker_people
        ORDER BY company_key, name
    """).fetchall()
    
    people_rows = [dict(r) for r in rows]
    
    # Build company -> jobs map
    jobs_by_company = {}
    jobs_rows = con.execute("""
        SELECT j.row_key, j.company, j.role_title,
               a.status AS app_status
        FROM jobs j
        LEFT JOIN application_status a ON j.row_key = a.row_key
    """).fetchall()
    
    for job in jobs_rows:
        company_name = job["company"]
        company_key = normalize_company(company_name)
        if company_key not in jobs_by_company:
            jobs_by_company[company_key] = []
        jobs_by_company[company_key].append({
            "row_key": job["row_key"],
            "company": company_name,
            "role_title": job["role_title"],
            "app_status": job["app_status"] or "new"
        })
    
    # Attach jobs to people and apply filters
    result = []
    companies_with_people = set()
    
    for person in people_rows:
        person["is_placeholder"] = False
        person["jobs"] = jobs_by_company.get(person["company_key"], [])
        companies_with_people.add(person["company_key"])
        
        # Apply filters
        if q and q.lower() not in person["name"].lower():
            continue
        if company and person["company_key"] != company:
            continue
        if status and person["outreach_status"] != status:
            continue
        
        result.append(person)
    
    # Add placeholder rows for companies with jobs but no people
    for company_key, jobs in jobs_by_company.items():
        if company_key not in companies_with_people:
            # Get first job to extract display name
            display_name = jobs[0].get("company", company_key) if jobs else company_key
            placeholder = {
                "person_key": f"{company_key}|_placeholder",
                "company_key": company_key,
                "company_name": display_name,
                "name": f"[{len(jobs)} job{'s' if len(jobs) > 1 else ''}]",
                "title": "",
                "email": "",
                "outreach_status": "not_contacted",
                "notes": "",
                "needs_review": 0,
                "is_placeholder": True,
                "jobs": jobs
            }
            
            # Apply company filter to placeholder too
            if company and company_key != company:
                continue
            
            result.append(placeholder)
    
    # Sort (basic: by name or company)
    if sort == "company":
        result.sort(key=lambda x: x["company_key"], reverse=(dir == "desc"))
    else:
        result.sort(key=lambda x: x["name"], reverse=(dir == "desc"))
    
    return result


def tracker_groups(con, q='', status='', app_status='', needs_contacts_only=False,
                   sort='activity', dir='', show_all=False):
    """Return (groups, stats) for the grouped Tracker view.

    Each group:
      company_key, company_name, people[], jobs[],
      people_count, jobs_count, applied_count, contacted_count,
      group_status, last_activity, is_placeholder

    stats:
      total_companies, companies_contacted, companies_applied,
      people_total, added_this_week
    """
    import datetime as _dt

    now_str = _now()
    week_ago = (_dt.datetime.now() - _dt.timedelta(days=7)).isoformat(timespec="seconds")

    # ── 1. Collect all companies ─────────────────────────────────────────────
    # Company universe = tracker_people OR jobs (same as company_suggestions)
    companies: dict[str, dict] = {}  # company_key -> {company_key, company_name}

    for r in con.execute(
        "SELECT DISTINCT company_key, company_name FROM tracker_people"
    ).fetchall():
        k = r["company_key"]
        if k and k not in companies:
            companies[k] = {"company_key": k, "company_name": r["company_name"] or k}

    for r in con.execute(
        "SELECT DISTINCT company FROM jobs WHERE company IS NOT NULL AND trim(company) != ''"
    ).fetchall():
        name = r["company"]
        k = normalize_company(name)
        if k and k not in companies:
            companies[k] = {"company_key": k, "company_name": name}

    # ── 2. Load all tracker_people ───────────────────────────────────────────
    people_by_company: dict[str, list] = {}
    for r in con.execute("""
        SELECT person_key, company_key, company_name, name, title, email,
               outreach_status, notes, needs_review, created_at, updated_at
        FROM tracker_people
        ORDER BY company_key, name
    """).fetchall():
        p = dict(r)
        st = p.get("outreach_status", "not_contacted") or "not_contacted"
        p["color"] = ("green" if st in ("contacted", "replied")
                      else ("blue" if st == "followup_due" else "gray"))
        k = p["company_key"]
        people_by_company.setdefault(k, []).append(p)

    # ── 3. Load all jobs ─────────────────────────────────────────────────────
    jobs_by_company: dict[str, list] = {}
    for r in con.execute("""
        SELECT j.row_key, j.company, j.role_title, j.job_url, j.location,
               j.match_score, j.jd_posted_date,
               a.status AS app_status, a.status_changed_at
        FROM jobs j
        LEFT JOIN application_status a ON j.row_key = a.row_key
    """).fetchall():
        jd = dict(r)
        jd["app_status"] = jd["app_status"] or "new"
        k = normalize_company(jd["company"] or "")
        if k:
            jobs_by_company.setdefault(k, []).append(jd)

    # ── 4. Build groups ──────────────────────────────────────────────────────
    APPLIED_STAGES = {"applied", "phone_screen", "interview", "offer"}
    CONTACTED_STATUSES = {"contacted", "followup_due", "replied"}
    DEAD_STAGES = {"closed", "rejected"}

    # Archiving only declutters the default browse view; any explicit narrowing
    # (search / a filter / show-all) searches the full set instead.
    explicit = bool(q or status or app_status or needs_contacts_only or show_all)
    stale_cutoff = (_dt.datetime.now() - _dt.timedelta(days=7)).isoformat(timespec="seconds")

    groups = []
    archived_count = 0
    for ck, cmeta in companies.items():
        people = people_by_company.get(ck, [])
        jobs = jobs_by_company.get(ck, [])

        if not people and not jobs:
            continue

        # ── filters ──────────────────────────────────────────────────────────
        # q: matches company name OR person name/title
        if q:
            ql = q.lower()
            co_match = ql in (cmeta["company_name"] or "").lower()
            person_match = any(
                ql in (p.get("name", "") or "").lower() or
                ql in (p.get("title", "") or "").lower()
                for p in people
            )
            if not co_match and not person_match:
                continue

        # status: filter people
        filtered_people = people
        if status:
            filtered_people = [p for p in people if p.get("outreach_status") == status]
            if not filtered_people:
                continue

        # app_status: keep group if any job has this stage
        if app_status:
            if not any(j.get("app_status") == app_status for j in jobs):
                continue

        # ── counts ───────────────────────────────────────────────────────────
        applied_count = sum(1 for j in jobs if j.get("app_status") in APPLIED_STAGES)
        contacted_count = sum(
            1 for p in filtered_people if p.get("outreach_status") in CONTACTED_STATUSES
        )

        # ── group_status rollup ───────────────────────────────────────────────
        # offer > interview > applied > contacted > needs_contact > neutral
        statuses = {j.get("app_status") for j in jobs}
        person_statuses = {p.get("outreach_status") for p in filtered_people}

        if "offer" in statuses:
            group_status = "offer"
        elif "interview" in statuses:
            group_status = "interview"
        elif "phone_screen" in statuses or applied_count > 0:
            group_status = "applied"
        elif contacted_count > 0:
            group_status = "contacted"
        elif jobs and not filtered_people:
            group_status = "needs_contact"
        else:
            group_status = "neutral"

        # needs_contacts_only is applied below, once lifecycle signals are known.

        # ── sort keys (robust: fall back to job posted dates so EVERY company orders) ──
        people_touch = [p.get("updated_at") or "" for p in filtered_people]
        people_add   = [p.get("created_at") or "" for p in filtered_people]
        job_changed  = [j.get("status_changed_at") or "" for j in jobs]
        job_posted   = [(j.get("jd_posted_date") or "")[:19] for j in jobs]
        added_key     = max([d for d in (people_add + job_posted) if d] or [""])
        last_activity = max([d for d in (people_touch + job_changed + job_posted + people_add) if d] or [""])

        # ── lifecycle signals ─────────────────────────────────────────────────
        active_app = any(j.get("app_status") in APPLIED_STAGES for j in jobs)
        tracked = [j.get("app_status") for j in jobs
                   if j.get("app_status") and j.get("app_status") != "new"]
        all_closed = bool(tracked) and all(s in DEAD_STAGES for s in tracked)
        no_people = len(filtered_people) == 0

        # needs_contacts_only: any company with jobs but no contacts yet (excl. dead)
        if needs_contacts_only and not (jobs and no_people and not all_closed):
            continue

        # ── archive (hidden in default browse view): dead, or cold & stale ─────
        is_archived = all_closed or (no_people and not active_app and last_activity < stale_cutoff)
        if is_archived and not explicit:
            archived_count += 1
            continue

        # ── max match_score (for "fit" sort) ──────────────────────────────────
        scores = [j.get("match_score") or 0 for j in jobs]
        max_score = max(scores) if scores else 0

        groups.append({
            "company_key": ck,
            "company_name": cmeta["company_name"],
            "people": filtered_people,
            "jobs": jobs,
            "people_count": len(filtered_people),
            "jobs_count": len(jobs),
            "applied_count": applied_count,
            "contacted_count": contacted_count,
            "group_status": group_status,
            "last_activity": last_activity,
            "added_key": added_key,
            "max_score": max_score,
            "is_archived": is_archived,
            "is_placeholder": len(filtered_people) == 0,
        })

    # ── 5. Sort (sensible default direction per key) ───────────────────────────
    if sort == "company":
        groups.sort(key=lambda g: (g["company_name"] or "").lower(),
                    reverse=(dir == "desc"))
    elif sort == "added":
        groups.sort(key=lambda g: g["added_key"], reverse=(dir != "asc"))
    elif sort == "fit":
        groups.sort(key=lambda g: g["max_score"], reverse=(dir != "asc"))
    elif sort == "applied":
        groups.sort(key=lambda g: g["applied_count"], reverse=(dir != "asc"))
    else:  # activity (default)
        groups.sort(key=lambda g: g["last_activity"], reverse=(dir != "asc"))

    # ── 6. Stats (scoreboard) ─────────────────────────────────────────────────
    # Use the full (unfiltered) universe for stats so the scoreboard reflects
    # overall progress, not just what the current filter shows.
    all_people_flat = [p for ps in people_by_company.values() for p in ps]
    all_groups_full = list(companies.keys())
    total_companies = len(all_groups_full)

    companies_contacted = sum(
        1 for ck in companies
        if any(
            p.get("outreach_status") in CONTACTED_STATUSES
            for p in people_by_company.get(ck, [])
        )
    )
    companies_applied = sum(
        1 for ck in companies
        if any(
            j.get("app_status") in APPLIED_STAGES
            for j in jobs_by_company.get(ck, [])
        )
    )
    people_total = sum(len(ps) for ps in people_by_company.values())
    added_this_week = sum(
        1 for p in all_people_flat
        if (p.get("created_at") or "") >= week_ago
    )

    stats = {
        "total_companies": total_companies,
        "companies_contacted": companies_contacted,
        "companies_applied": companies_applied,
        "people_total": people_total,
        "added_this_week": added_this_week,
        "archived_count": archived_count,
    }

    return groups, stats


def set_tracker_person_status(con, person_key: str, status: str) -> None:
    """Update outreach_status for a person."""
    con.execute(
        "UPDATE tracker_people SET outreach_status = ?, updated_at = ? WHERE person_key = ?",
        (status, _now(), person_key)
    )
    con.commit()


def set_tracker_person_notes(con, person_key: str, notes: str) -> None:
    """Update notes for a tracker person."""
    con.execute(
        "UPDATE tracker_people SET notes = ?, updated_at = ? WHERE person_key = ?",
        (notes, _now(), person_key)
    )
    con.commit()


def get_contact_row(con, person_key: str) -> dict | None:
    """Single tracker person row for HTMX swap after a status/notes update."""
    r = con.execute("""
        SELECT person_key, company_key, company_name, name, title, email,
               outreach_status, notes, needs_review
        FROM tracker_people WHERE person_key = ?
    """, (person_key,)).fetchone()
    if not r:
        return None
    person = dict(r)
    person["is_placeholder"] = False
    jobs_rows = con.execute("""
        SELECT j.row_key, j.role_title, a.status AS app_status
        FROM jobs j
        LEFT JOIN application_status a ON j.row_key = a.row_key
        WHERE lower(trim(j.company)) = ?
    """, (person["company_key"],)).fetchall()
    person["jobs"] = [
        {"row_key": jr["row_key"], "role_title": jr["role_title"], "app_status": jr["app_status"] or "new"}
        for jr in jobs_rows
    ]
    # Compute color
    st = person.get("outreach_status", "not_contacted")
    person["color"] = "green" if st in ("contacted", "replied") else ("blue" if st == "followup_due" else "gray")
    return person


def get_tracker_person(con, person_key: str) -> dict | None:
    """Return a single tracker_people row with color computed."""
    r = con.execute("""
        SELECT person_key, company_key, company_name, name, title, email,
               outreach_status, notes, needs_review, created_at, updated_at
        FROM tracker_people WHERE person_key = ?
    """, (person_key,)).fetchone()
    if not r:
        return None
    p = dict(r)
    st = p.get("outreach_status", "not_contacted") or "not_contacted"
    p["color"] = ("green" if st in ("contacted", "replied")
                  else ("blue" if st == "followup_due" else "gray"))
    return p


def get_tracker_job(con, row_key: str) -> dict | None:
    """Return a single job row for the tracker (with app_status)."""
    r = con.execute("""
        SELECT j.row_key, j.company, j.role_title, j.job_url, j.location,
               j.match_score, j.jd_posted_date,
               a.status AS app_status, a.status_changed_at
        FROM jobs j
        LEFT JOIN application_status a ON j.row_key = a.row_key
        WHERE j.row_key = ?
    """, (row_key,)).fetchone()
    if not r:
        return None
    jd = dict(r)
    jd["app_status"] = jd["app_status"] or "new"
    return jd


def tracker_export_rows(con) -> list[dict]:
    """Flatten tracker_people + jobs for CSV export."""
    rows = con.execute("""
        SELECT person_key, company_key, company_name, name, title, email,
               outreach_status, notes
        FROM tracker_people
        ORDER BY company_key, name
    """).fetchall()

    jobs_by_company: dict[str, list] = {}
    for job in con.execute("""
        SELECT j.row_key, j.company, j.role_title, a.status AS app_status
        FROM jobs j
        LEFT JOIN application_status a ON j.row_key = a.row_key
    """).fetchall():
        ck = normalize_company(job["company"])
        jobs_by_company.setdefault(ck, []).append({
            "role_title": job["role_title"],
            "app_status": job["app_status"] or "new",
        })

    result = []
    for r in rows:
        d = dict(r)
        jobs = jobs_by_company.get(d["company_key"], [])
        d["jobs_summary"] = "; ".join(
            f"{j['role_title']} ({j['app_status']})" for j in jobs
        )
        result.append(d)
    return result


def company_suggestions(con, q: str) -> list[dict]:
    """
    Return fuzzy-ranked company suggestions for the type-ahead.
    Union of tracker_people, companies, and manual_company.
    """
    # Collect all unique companies
    companies_dict = {}
    
    # From tracker_people
    for row in con.execute("""
        SELECT DISTINCT company_key, company_name FROM tracker_people
    """).fetchall():
        key = row["company_key"]
        if key not in companies_dict:
            companies_dict[key] = {
                "company_key": key,
                "company_name": row["company_name"]
            }
    
    # From companies
    for row in con.execute("""
        SELECT company_key, company_name FROM companies
    """).fetchall():
        key = row["company_key"]
        if key not in companies_dict:
            companies_dict[key] = {
                "company_key": key,
                "company_name": row["company_name"]
            }
    
    # From manual_company
    for row in con.execute("""
        SELECT company_key, company_name FROM manual_company
    """).fetchall():
        key = row["company_key"]
        if key not in companies_dict:
            companies_dict[key] = {
                "company_key": key,
                "company_name": row["company_name"]
            }

    # From jobs — postings whose company has no company record yet. These are
    # exactly the "needs contacts" placeholder rows, so they must be selectable
    # in the Add-people drawer and the company filter.
    for row in con.execute("""
        SELECT DISTINCT company FROM jobs
        WHERE company IS NOT NULL AND trim(company) != ''
    """).fetchall():
        name = row["company"]
        key = normalize_company(name)
        if key and key not in companies_dict:
            companies_dict[key] = {
                "company_key": key,
                "company_name": name
            }

    # Rank by fuzzy match against q
    candidates = list(companies_dict.values())
    if not q:
        return candidates
    
    q_core = company_core(q)
    scored = []
    for cand in candidates:
        cand_core = company_core(cand["company_name"])
        ratio = difflib.SequenceMatcher(None, q_core, cand_core).ratio() if q_core and cand_core else 0
        scored.append((ratio, cand))
    
    scored.sort(key=lambda x: x[0], reverse=True)
    return [cand for _, cand in scored]

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _shape(r) -> dict:
    d = dict(r)
    score = d.get("match_score") or 0
    d["applied"] = bool(d.get("applied_at"))
    d["starred"] = bool(d.get("starred"))
    d["dismissed"] = bool(d.get("dismissed"))
    d["has_resume"] = bool(d.get("resume_file"))
    d["notes"] = d.get("notes") or ""
    d["score_tone"] = "high" if score >= 75 else ("mid" if score >= 55 else "low")
    d["status"] = d.get("app_status") or ("applied" if d["applied"] else "new")
    d["evidence_list"] = [t.strip() for t in (d.get("matched_evidence") or "").split(";") if t.strip()]
    d["gaps_list"] = [t.strip() for t in (d.get("gaps") or "").split(";") if t.strip()]
    d["missing_list"] = [t.strip() for t in (d.get("jd_terms_missing") or "").split(",") if t.strip()]
    d["low_confidence"] = (d.get("confidence") == "low")
    d["unclassified_list"] = [t.strip() for t in (d.get("unclassified_requirements") or "").split(";") if t.strip()]
    # job_day: prefer jd_posted_date if it's a valid YYYY-MM-DD, else first_seen_date, else ""
    jd_posted = d.get("jd_posted_date") or ""
    first_seen = d.get("first_seen_date") or ""
    if jd_posted and _DATE_RE.match(jd_posted):
        d["job_day"] = jd_posted[:10]
    elif first_seen:
        d["job_day"] = first_seen[:10]
    else:
        d["job_day"] = ""
    d["posted_at"] = freshness.posted_at_of(d)
    return d


def _sort_board(jobs: list[dict]) -> list[dict]:
    """Default board order: freshest first, then Fit, then starred."""
    jobs.sort(key=lambda d: (d.get("posted_at") or "",
                             d.get("match_score") or 0,
                             1 if d.get("starred") else 0), reverse=True)
    return jobs


def get_jobs(con, q=None, sector=None, min_score=0, status=None,
             show_dismissed=False, starred_only=False, view=None, day=None,
             fresh=None) -> list[dict]:
    _now = datetime.datetime.now(datetime.timezone.utc)
    rows = [_shape(r) for r in con.execute(JOB_SELECT).fetchall()]
    out = []
    for d in rows:
        if d.get("match_score") is None:
            continue  # unscored = dropped by the scorer (Fit below keep threshold)
        # view filter
        if view == "to_review":
            if d["applied"] or d["dismissed"]:
                continue
        elif view == "applied":
            if not d["applied"]:
                continue
        elif view == "starred":
            if not d["starred"]:
                continue
        else:
            # Default board (view is None or empty): exclude applied jobs
            if d["applied"]:
                continue
        # dismissed filter (applies unless view already handled it)
        if view not in ("applied", "starred"):
            if not show_dismissed and d["dismissed"]:
                continue
        else:
            if not show_dismissed and d["dismissed"]:
                continue
        # day filter
        if day and day != "all":
            if d["job_day"] != day:
                continue
        # other filters
        if starred_only and not d["starred"]:
            continue
        if min_score and (d["match_score"] or 0) < int(min_score):
            continue
        if fresh and not freshness.fresh_ok(d.get("posted_at") or "", _now, fresh):
            continue
        if sector and d.get("sector") != sector:
            continue
        if status and d["status"] != status:
            continue
        if q:
            hay = f"{d.get('company','')} {d.get('role_title','')} {d.get('location','')} {d.get('sector','')}".lower()
            if q.lower() not in hay:
                continue
        out.append(d)
    _sort_board(out)
    return out


def available_days(con) -> list[str]:
    """Return distinct non-empty job_day values across all jobs, sorted descending."""
    rows = [_shape(r) for r in con.execute(JOB_SELECT).fetchall()]
    seen = set()
    for d in rows:
        day = d.get("job_day", "")
        if day:
            seen.add(day)
    return sorted(seen, reverse=True)


def unapply(con, row_key):
    _ensure(con, row_key)
    now = _now()
    con.execute("UPDATE app_state SET applied_at=NULL, updated_at=? WHERE row_key=?", (now, row_key))
    con.execute("DELETE FROM application_status WHERE row_key=?", (row_key,))
    _event(con, row_key, "unapplied")
    con.commit()


def get_job(con, row_key) -> dict | None:
    r = con.execute(JOB_SELECT + " WHERE j.row_key = ?", (row_key,)).fetchone()
    return _shape(r) if r else None


def sectors(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT sector, COUNT(*) c FROM jobs GROUP BY sector ORDER BY c DESC")]


def stats(con) -> dict:
    g = lambda s, *a: con.execute(s, a).fetchone()[0]
    total = g("SELECT COUNT(*) FROM jobs WHERE match_score IS NOT NULL")
    # Count app-state marks only among currently-scored jobs, so stale marks left by
    # a previous dataset (different row_keys) can't skew the counters - otherwise
    # To review clamps to 0 when old applied/dismissed totals exceed today's scored set.
    applied = g("""SELECT COUNT(*) FROM jobs j JOIN app_state s ON j.row_key = s.row_key
                   WHERE j.match_score IS NOT NULL AND s.applied_at IS NOT NULL""")
    dismissed = g("""SELECT COUNT(*) FROM jobs j JOIN app_state s ON j.row_key = s.row_key
                     WHERE j.match_score IS NOT NULL AND s.dismissed = 1""")
    starred = g("""SELECT COUNT(*) FROM jobs j JOIN app_state s ON j.row_key = s.row_key
                   WHERE j.match_score IS NOT NULL AND s.starred = 1""")
    to_review = g("""SELECT COUNT(*) FROM jobs j
                     LEFT JOIN app_state s ON j.row_key = s.row_key
                     WHERE j.match_score IS NOT NULL
                       AND COALESCE(s.applied_at, '') = '' AND COALESCE(s.dismissed, 0) = 0""")
    return {"total": total, "applied": applied, "dismissed": dismissed,
            "starred": starred, "to_review": to_review}


def _ensure(con, row_key):
    con.execute("INSERT OR IGNORE INTO app_state(row_key, created_at, updated_at) VALUES(?,?,?)",
                (row_key, _now(), _now()))


def _event(con, row_key, ev, payload=""):
    con.execute("INSERT INTO app_events(row_key, event_type, payload_json, created_at) VALUES(?,?,?,?)",
                (row_key, ev, payload, _now()))


def set_applied(con, row_key):
    _ensure(con, row_key)
    con.execute("UPDATE app_state SET applied_at=?, updated_at=? WHERE row_key=?", (_now(), _now(), row_key))
    set_status(con, row_key, "applied", commit=False)
    _event(con, row_key, "applied")
    con.commit()


def toggle_star(con, row_key):
    _ensure(con, row_key)
    con.execute("UPDATE app_state SET starred = 1 - starred, updated_at=? WHERE row_key=?", (_now(), row_key))
    con.commit()


def set_dismissed(con, row_key, val=1):
    _ensure(con, row_key)
    con.execute("UPDATE app_state SET dismissed=?, updated_at=? WHERE row_key=?", (val, _now(), row_key))
    _event(con, row_key, "dismissed" if val else "restored")
    con.commit()


def save_notes(con, row_key, notes):
    _ensure(con, row_key)
    con.execute("UPDATE app_state SET notes=?, updated_at=? WHERE row_key=?", (notes, _now(), row_key))
    con.commit()


def set_status(con, row_key, status, commit=True):
    con.execute(
        "INSERT INTO application_status(row_key, status, status_changed_at, source) VALUES(?,?,?, 'manual') "
        "ON CONFLICT(row_key) DO UPDATE SET status=excluded.status, status_changed_at=excluded.status_changed_at",
        (row_key, status, _now()))
    if commit:
        con.commit()


def _norm_skill(s: str) -> str:
    """Casefold + collapse whitespace so 'SQL'/'sql', 'Power  BI'/'Power BI' group.
    Terms come from the scorer's controlled term bank, so they're already fairly
    canonical - this is a light touch, not NLP."""
    return re.sub(r"\s+", " ", (s or "").strip()).casefold()


def _fit_tone(f: int) -> str:
    """Strategic colour: green = strong fit, amber = borderline, gray = weak."""
    return "good" if f >= 75 else ("warn" if f >= 68 else "muted")


def insights(con, sector=None, day=None) -> dict:
    from collections import Counter, defaultdict
    from statistics import mean

    rows = [d for d in (_shape(r) for r in con.execute(JOB_SELECT).fetchall())
            if not d["dismissed"] and d["match_score"] is not None]
    if sector:
        rows = [d for d in rows if d.get("sector") == sector]
    if day and day != "all":
        rows = [d for d in rows if d["job_day"] == day]

    # ── single pass: scores, sectors, skills ────────────────────────────────
    ev_count, gp_count = Counter(), Counter()
    ev_disp, gp_disp = {}, {}
    ev_roles, gp_roles = defaultdict(list), defaultdict(list)
    sec_scores = defaultdict(list)
    for d in rows:
        sec_scores[d.get("sector") or "other"].append(d["match_score"] or 0)
        for s in d["evidence_list"]:
            k = _norm_skill(s)
            if not k:
                continue
            ev_count[k] += 1
            ev_disp.setdefault(k, s.strip())
            if d.get("company"):
                ev_roles[k].append(d["company"])
        for s in d["gaps_list"]:
            k = _norm_skill(s)
            if not k:
                continue
            gp_count[k] += 1
            gp_disp.setdefault(k, s.strip())
            if d.get("company"):
                gp_roles[k].append(d["company"])

    def _skills(counter, disp, roles, n):
        out = []
        for k, c in counter.most_common(n):
            seen, uniq = set(), []
            for co in roles[k]:
                if co not in seen:
                    seen.add(co)
                    uniq.append(co)
            out.append({"label": disp[k], "count": c,
                        "roles": uniq[:6], "more": max(0, len(uniq) - 6)})
        return out

    strengths = _skills(ev_count, ev_disp, ev_roles, 10)
    gaps_top = _skills(gp_count, gp_disp, gp_roles, 8)
    strength_max = max((s["count"] for s in strengths), default=1)
    gap_max = max((s["count"] for s in gaps_top), default=1)

    # ── score histogram (honest: include a sub-70 bucket) ───────────────────
    buckets = [("<70", 0, 69), ("70-74", 70, 74), ("75-79", 75, 79),
               ("80-84", 80, 84), ("85-89", 85, 89), ("90+", 90, 100)]
    hist = [{"label": lab, "count": sum(1 for d in rows if lo <= (d["match_score"] or 0) <= hi)}
            for lab, lo, hi in buckets]
    mode = max(hist, key=lambda b: b["count"]) if any(b["count"] for b in hist) else None

    # ── sectors: count + avg fit + tone (dual-encoded bar) ──────────────────
    sectors = sorted(
        ({"sector": k, "count": len(v), "avg_fit": round(mean(v)), "tone": _fit_tone(round(mean(v)))}
         for k, v in sec_scores.items()),
        key=lambda x: -x["count"])
    sec_max = max((s["count"] for s in sectors), default=1)

    applied = sum(1 for d in rows if d["applied"])
    funnel = [
        {"label": "To review", "count": sum(1 for d in rows if not d["applied"])},
        {"label": "Applied", "count": applied},
        {"label": "Interviewing", "count": sum(1 for d in rows if d["status"] in ("phone_screen", "interview"))},
        {"label": "Offer", "count": sum(1 for d in rows if d["status"] == "offer")},
    ]
    with_resume = sum(1 for d in rows if d["has_resume"])
    passes = sum(1 for d in rows if d.get("self_check_passes_target"))
    scores = [d["match_score"] for d in rows if d["match_score"]]

    # ── insight-led captions (the "so what", with the key number to bold) ───
    n = len(rows)
    top_s = strengths[0] if strengths else None
    top_g = gaps_top[0] if gaps_top else None
    best_sec = max(sectors, key=lambda s: s["avg_fit"]) if sectors else None
    vol_sec = sectors[0] if sectors else None
    cap = {
        "score": (f"Most roles land in the {mode['label']} fit band - {n} active in total." if mode else ""),
        "sector": ((f"{best_sec['sector'].replace('_', ' ').title()} fits you best (avg {best_sec['avg_fit']})"
                    + (f", while {vol_sec['sector'].replace('_', ' ').title()} brings the most roles ({vol_sec['count']})."
                       if vol_sec and vol_sec['sector'] != best_sec['sector'] else "."))
                   if best_sec else ""),
        "strengths": (f"{top_s['label']} is your most in-demand strength - evidenced in {top_s['count']} of {n} roles."
                      if top_s else "No matched skills yet - run the pipeline to populate this."),
        "gaps": (f"{top_g['label']} is the gap that recurs most - wanted in {top_g['count']} roles you don't fully cover."
                 if top_g else "No recurring skill gaps surfaced - nice."),
    }

    return {"total": n, "score_hist": hist, "score_mode": mode["label"] if mode else None,
            "sectors": sectors, "sec_max": sec_max, "funnel": funnel,
            "strengths": strengths, "strength_max": strength_max,
            "gaps_top": gaps_top, "gap_max": gap_max, "cap": cap,
            "ats": {"with_resume": with_resume, "passes": passes, "below": with_resume - passes},
            "applied": applied, "score_avg": round(sum(scores) / len(scores), 1) if scores else 0}


def _person_row(p, ps, drafts, outr, is_manual: bool = False) -> dict:
    """Build a person dict from a people row, person_state row, drafts list, outreach list."""
    email = p["verified_email"] or ""
    nm = (p["name"] or "").lower()
    first = nm.split()[0] if nm else ""
    dstate = ""
    for d in drafts:
        slug = (d.get("person_slug") or "").lower()
        if first and (first in slug or slug.replace("-", " ") in nm):
            dstate = d.get("state") or ""
            break
    replied = any(o.get("email") == email and email and o.get("replied_at") for o in outr)
    outreach_status = (ps or {}).get("outreach_status") or "not_contacted"
    contacted_at = (ps or {}).get("contacted_at") or ""
    return {
        "person_key":     p["person_key"],
        "company_key":    p["company_key"],
        "name":           p["name"],
        "role":           p["role"] or "",
        "email":          email,
        "has_email":      bool(email),
        "linkedin_url":   p["linkedin_url"] or "",
        "draft_state":    dstate,
        "replied":        replied,
        "outreach_status": outreach_status,
        "contacted_at":   contacted_at,
        "is_manual":      is_manual,
    }


def company_detail(con, company_key: str) -> dict:
    comp_row = con.execute("SELECT * FROM companies WHERE company_key=?", (company_key,)).fetchone()
    if comp_row:
        company_name = dict(comp_row)["company_name"] or company_key.title()
        domain = dict(comp_row).get("domain", "") or ""
    else:
        # Fall back to manual_company before using the key itself
        manual_comp = con.execute(
            "SELECT * FROM manual_company WHERE company_key=?", (company_key,)
        ).fetchone()
        if manual_comp:
            company_name = manual_comp["company_name"] or company_key.title()
            domain = manual_comp["domain"] or ""
        else:
            company_name = company_key.title()
            domain = ""
    drafts = [dict(r) for r in con.execute("SELECT * FROM drafts")]
    outr = [dict(r) for r in con.execute("SELECT * FROM outreach_log")]
    ps_map = {r["person_key"]: dict(r) for r in con.execute("SELECT * FROM person_state")}
    people_rows = [
        _person_row(p, ps_map.get(p["person_key"]), drafts, outr)
        for p in con.execute("SELECT * FROM people WHERE company_key=?", (company_key,))
    ]
    seen_keys = {pr["person_key"] for pr in people_rows}
    for p in con.execute("SELECT * FROM manual_people WHERE company_key=?", (company_key,)):
        if p["person_key"] not in seen_keys:
            people_rows.append(_person_row(p, ps_map.get(p["person_key"]), drafts, outr, is_manual=True))
    # Fetch scored jobs whose normalized company matches this company_key
    job_rows = [
        _shape(r) for r in con.execute(JOB_SELECT).fetchall()
        if r["match_score"] is not None and normalize_company(r["company"] or "") == company_key
    ]
    job_rows.sort(key=lambda d: (d["match_score"] or 0), reverse=True)

    return {
        "company":     company_name,
        "company_key": company_key,
        "domain":      domain,
        "people":      people_rows,
        "jobs":        job_rows,
    }


def get_person(con, person_key: str) -> dict | None:
    p = con.execute("SELECT * FROM people WHERE person_key=?", (person_key,)).fetchone()
    is_manual = False
    if not p:
        p = con.execute("SELECT * FROM manual_people WHERE person_key=?", (person_key,)).fetchone()
        if not p:
            return None
        is_manual = True
    drafts = [dict(r) for r in con.execute("SELECT * FROM drafts")]
    outr = [dict(r) for r in con.execute("SELECT * FROM outreach_log")]
    ps = con.execute("SELECT * FROM person_state WHERE person_key=?", (person_key,)).fetchone()
    return _person_row(p, dict(ps) if ps else None, drafts, outr, is_manual=is_manual)


def set_person_status(con, person_key: str, status: str) -> None:
    now = _now()
    con.execute(
        "INSERT OR IGNORE INTO person_state(person_key, outreach_status, updated_at) VALUES(?,?,?)",
        (person_key, status, now),
    )
    if status == "contacted":
        con.execute(
            "UPDATE person_state SET outreach_status=?, updated_at=?, "
            "contacted_at=COALESCE(contacted_at,?) WHERE person_key=?",
            (status, now, now, person_key),
        )
    else:
        con.execute(
            "UPDATE person_state SET outreach_status=?, updated_at=? WHERE person_key=?",
            (status, now, person_key),
        )
    con.commit()


def add_manual_company(con, company_name: str, domain: str = "") -> str:
    """INSERT OR REPLACE into manual_company; return company_key."""
    ck = normalize_company(company_name)
    con.execute(
        "INSERT OR REPLACE INTO manual_company(company_key, company_name, domain, created_at) VALUES(?,?,?,?)",
        (ck, company_name, domain or "", _now()),
    )
    con.commit()
    return ck


def add_manual_person(con, company_key: str, name: str, role: str = "",
                      email: str = "", linkedin_url: str = "") -> str:
    """INSERT OR REPLACE into manual_people; return person_key."""
    pk = company_key + "|" + slugify(name)
    con.execute(
        "INSERT OR REPLACE INTO manual_people"
        "(person_key, company_key, name, role, verified_email, linkedin_url, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (pk, company_key, name, role or "", email or "", linkedin_url or "", _now()),
    )
    con.commit()
    return pk


def delete_manual_person(con, person_key: str) -> None:
    """Delete from manual_people AND person_state."""
    con.execute("DELETE FROM manual_people WHERE person_key=?", (person_key,))
    con.execute("DELETE FROM person_state WHERE person_key=?", (person_key,))
    con.commit()


# ---------------------------------------------------------------------------
# Onboarding helpers (Zone-2 tables: onboarding + tracked_positions)
# ---------------------------------------------------------------------------

def get_onboarding(con) -> dict:
    """Return the singleton onboarding row, creating it (id=1) if absent."""
    con.execute(
        "INSERT OR IGNORE INTO onboarding(id, created_at, updated_at) VALUES(1,?,?)",
        (_now(), _now()),
    )
    con.commit()
    r = con.execute("SELECT * FROM onboarding WHERE id=1").fetchone()
    return dict(r)


def onboarding_state(con) -> str:
    """Return 'NEEDS_RESUME', 'NEEDS_POSITIONS', or 'READY'."""
    onb = get_onboarding(con)
    if not onb.get("skills_built"):
        return "NEEDS_RESUME"
    count = con.execute("SELECT COUNT(*) FROM tracked_positions").fetchone()[0]
    if count == 0:
        return "NEEDS_POSITIONS"
    return "READY"


def set_resume(con, filename: str, summary: str) -> None:
    now = _now()
    # Ensure the singleton row exists, else the UPDATE below is a no-op and
    # skills_built never flips (the upload would appear to do nothing).
    con.execute(
        "INSERT OR IGNORE INTO onboarding(id, created_at, updated_at) VALUES(1,?,?)",
        (now, now),
    )
    con.execute(
        "UPDATE onboarding SET resume_filename=?, resume_uploaded_at=?, resume_summary=?, "
        "skills_built=1, updated_at=? WHERE id=1",
        (filename, now, summary, now),
    )
    con.commit()


def clear_resume(con) -> None:
    now = _now()
    con.execute(
        "UPDATE onboarding SET skills_built=0, resume_filename=NULL, resume_summary=NULL, updated_at=? WHERE id=1",
        (now,),
    )
    con.commit()


def _migrate_legacy_location(con) -> None:
    """Seed tracked_locations from the old singleton onboarding.location once,
    so users onboarded before multi-location keep their saved location."""
    have = con.execute("SELECT COUNT(*) FROM tracked_locations").fetchone()[0]
    if have:
        return
    row = con.execute("SELECT location FROM onboarding WHERE id=1").fetchone()
    loc = (row["location"] if row else "") or ""
    loc = loc.strip()
    if loc:
        con.execute(
            "INSERT OR IGNORE INTO tracked_locations(name, display, created_at) VALUES(?,?,?)",
            (loc.lower(), loc, _now()),
        )
        con.commit()


def add_positions(con, raw: str) -> None:
    """Split raw on commas, strip, dedupe, insert each. Then write config."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    seen: set[str] = set()
    now = _now()
    for display in parts:
        title = display.lower()
        if title in seen:
            continue
        seen.add(title)
        con.execute(
            "INSERT OR IGNORE INTO tracked_positions(title, display, created_at) VALUES(?,?,?)",
            (title, display, now),
        )
    con.commit()
    write_config(con)


def remove_position(con, title: str) -> None:
    con.execute("DELETE FROM tracked_positions WHERE title=?", (title,))
    con.commit()
    write_config(con)


def set_location(con, loc: str) -> None:
    con.execute("UPDATE onboarding SET location=?, updated_at=? WHERE id=1", (loc, _now()))
    con.commit()
    write_config(con)


def set_recency(con, tpr: str) -> None:
    now = _now()
    # Ensure the singleton exists, else this UPDATE is a silent no-op when
    # recency is the first setting touched on a fresh DB.
    con.execute(
        "INSERT OR IGNORE INTO onboarding(id, created_at, updated_at) VALUES(1,?,?)",
        (now, now),
    )
    con.execute("UPDATE onboarding SET recency_tpr=?, updated_at=? WHERE id=1", (tpr, now))
    con.commit()
    write_config(con)


def list_positions(con) -> list[dict]:
    return [dict(r) for r in con.execute(
        "SELECT title, display FROM tracked_positions ORDER BY created_at"
    ).fetchall()]


def add_locations(con, raw: str) -> None:
    """Split raw on commas, strip, dedupe, insert each tracked location, then write config."""
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    seen: set[str] = set()
    now = _now()
    for display in parts:
        name = display.lower()
        if name in seen:
            continue
        seen.add(name)
        con.execute(
            "INSERT OR IGNORE INTO tracked_locations(name, display, created_at) VALUES(?,?,?)",
            (name, display, now),
        )
    con.commit()
    write_config(con)


def add_structured_location(con, country: str, state: str, city: str) -> None:
    """Insert a structured country/state/city location if valid, then write config."""
    import app.locations as _loc
    if not _loc.validate(country, state, city):
        return
    name = _loc.build_key(country, state, city)
    display = _loc.build_query(country, state, city)
    now = _now()
    con.execute(
        "INSERT OR IGNORE INTO tracked_locations(name, display, created_at) VALUES(?,?,?)",
        (name, display, now),
    )
    con.commit()
    write_config(con)


def remove_location(con, name: str) -> None:
    con.execute("DELETE FROM tracked_locations WHERE name=?", (name,))
    con.commit()
    write_config(con)


def list_locations(con) -> list[dict]:
    _migrate_legacy_location(con)
    return [dict(r) for r in con.execute(
        "SELECT name, display FROM tracked_locations ORDER BY created_at"
    ).fetchall()]


def write_config(con) -> None:
    """Atomically update config.json: set search.target_keywords/location/recency_tpr."""
    import config as _config

    # Always WRITE to config.json (never the shipped template). Read the existing
    # config.json as the base, falling back to config.example.json on first run.
    cfg_path = _config.ROOT / "config.json"
    example_path = _config.ROOT / "config.example.json"
    base_path = cfg_path if cfg_path.exists() else example_path
    if base_path.exists():
        with base_path.open(encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {}

    positions = list_positions(con)
    keywords = [p["display"] for p in positions]

    onb = get_onboarding(con)
    tpr = onb.get("recency_tpr") or "r86400"
    locations = [l["display"] for l in list_locations(con)]

    search = data.setdefault("search", {})
    search["target_keywords"] = keywords
    search["target_locations"] = locations
    if locations:
        search["target_location"] = locations[0]
    search["recency_tpr"] = tpr

    tmp = str(cfg_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(cfg_path))


def set_identity(name: str, contact: str) -> None:
    """Atomically update config.json identity.name / identity.contact from the parsed
    resume, so generated resumes carry the real candidate details instead of the
    shipped placeholder. Reads config.json (or config.example.json on first run)."""
    import config as _config

    cfg_path = _config.ROOT / "config.json"
    example_path = _config.ROOT / "config.example.json"
    base_path = cfg_path if cfg_path.exists() else example_path
    data = {}
    if base_path.exists():
        with base_path.open(encoding="utf-8") as f:
            data = json.load(f)

    ident = data.setdefault("identity", {})
    if name:
        ident["name"] = name
    if contact:
        ident["contact"] = contact

    tmp = str(cfg_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(cfg_path))


def set_education(lines: list) -> None:
    """Atomically update config.json identity.education from the parsed resume."""
    import config as _config

    cfg_path = _config.ROOT / "config.json"
    example_path = _config.ROOT / "config.example.json"
    base_path = cfg_path if cfg_path.exists() else example_path
    data = {}
    if base_path.exists():
        with base_path.open(encoding="utf-8") as f:
            data = json.load(f)

    ident = data.setdefault("identity", {})
    if lines:
        ident["education"] = lines

    tmp = str(cfg_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(cfg_path))


def set_projects(projects: list) -> None:
    """Atomically update config.json identity.projects from the parsed resume.
    projects: [[name, [bullets]], ...]."""
    import config as _config

    cfg_path = _config.ROOT / "config.json"
    example_path = _config.ROOT / "config.example.json"
    base_path = cfg_path if cfg_path.exists() else example_path
    data = {}
    if base_path.exists():
        with base_path.open(encoding="utf-8") as f:
            data = json.load(f)

    ident = data.setdefault("identity", {})
    if projects:
        ident["projects"] = projects

    tmp = str(cfg_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(cfg_path))


def add_supported_skill(label: str) -> bool:
    """Add one skill the user says they have to skills.json supported_skills.

    Atomic write. Aliases/group come from the taxonomy (ontology) so the new
    entry matches the same wording the scorer recognises; if the taxonomy knows
    no usable alias for the label we fall back to the normalised label itself so
    the entry never has empty aliases. The label is also removed from
    unsupported_skills so the two banks never share a key. (config._validate_banks
    encodes both invariants but is not wired into the runtime scoring path, so we
    enforce them here at write time rather than relying on a load-time guard.)

    Always WRITES to skills.json (never the shipped skills.example.json). Returns
    True if anything changed, False if the skill was already supported.
    Caller must invalidate the in-process caches (config.reset_banks etc.).
    """
    import config as _config
    import ontology

    label = (label or "").strip()
    if not label:
        return False

    # Write to the env-pointed file if set (keeps tests isolated), else the real
    # skills.json. Never the shipped skills.example.json.
    override = os.environ.get("JOBENGINE_SKILLS")
    skills_path = Path(override) if override else (_config.ROOT / "skills.json")
    example_path = _config.ROOT / "skills.example.json"
    base_path = skills_path if skills_path.exists() else example_path
    data: dict = {}
    if base_path.exists():
        with base_path.open(encoding="utf-8") as f:
            data = json.load(f)

    supported = data.setdefault("supported_skills", {})
    unsupported = data.setdefault("unsupported_skills", {})

    already = label in supported
    removed = unsupported.pop(label, None) is not None

    if not already:
        aliases = ontology.aliases_for([label]).get(label, [])
        if not aliases:
            aliases = [ontology.normalize(label)]
        # Deduplicate while preserving order; guarantee the label's own
        # normalised form is present so an exact JD mention always matches.
        seen: set = set()
        ordered = []
        for a in [ontology.normalize(label)] + aliases:
            if a and a not in seen:
                seen.add(a)
                ordered.append(a)
        supported[label] = {"aliases": ordered, "group": ontology.group_for(label)}

    if already and not removed:
        return False

    tmp = str(skills_path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(skills_path))
    return True


def rescore_job(con, row_key: str) -> dict | None:
    """Re-run the FIT scorer for one job and persist the new result.

    Updates the live DB row AND patches matched_jobs.csv (the source app/sync.py
    rebuilds from) so the new score survives the next sync instead of reverting.
    Returns the score_jobs.score_job result dict, or None if the job/JD is absent.

    Caller is responsible for invalidating the skill caches first
    (config.reset_banks + score_jobs._reset_caches) so the re-score reflects any
    skills just added; otherwise it re-computes the same (stale) result.
    """
    import score_jobs

    r = con.execute(
        "SELECT job_text, role_title FROM jobs WHERE row_key = ?", (row_key,)
    ).fetchone()
    if not r:
        return None
    jd = (r["job_text"] or "").strip()
    role = (r["role_title"] or "").strip()
    if not jd:
        return None

    result = score_jobs.score_job(role, jd)
    family = score_jobs.role_family(role, jd)
    why = score_jobs._why_keep(result, family)
    evidence = "; ".join(result["supported"])
    gaps = "; ".join(result["lacked"])
    unclassified = "; ".join(result["unclassified"])

    con.execute(
        """UPDATE jobs SET match_score = ?, match_band = ?, matched_evidence = ?,
                           gaps = ?, why_keep = ?, confidence = ?,
                           unclassified_requirements = ?
           WHERE row_key = ?""",
        (str(result["fit"]), result["band"], evidence, gaps, why,
         result["confidence"], unclassified, row_key),
    )
    con.commit()

    _patch_matched_csv(row_key, result, why)
    return result


def _patch_matched_csv(row_key: str, result: dict, why: str) -> None:
    """Rewrite the matched_jobs.csv row for row_key with the fresh score so the
    next app/sync.py run keeps the live re-score instead of reverting it.

    No-op if the CSV or the row is missing (the DB already holds the new score;
    the CSV only matters for sync persistence)."""
    import csv
    import csv_merge
    import score_jobs

    path = score_jobs.MATCHED
    if not path.exists():
        return

    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames or []
        rows = list(reader)

    patched = False
    for row in rows:
        if csv_merge.row_key(row) == row_key:
            row["match_score"] = str(result["fit"])
            row["match_band"] = result["band"]
            row["matched_evidence"] = "; ".join(result["supported"])
            row["gaps"] = "; ".join(result["lacked"])
            row["why_keep"] = why
            if "confidence" in fieldnames:
                row["confidence"] = result["confidence"]
            if "unclassified_requirements" in fieldnames:
                row["unclassified_requirements"] = "; ".join(result["unclassified"])
            if "auto_reject_risk" in fieldnames:
                row["auto_reject_risk"] = "yes" if result["auto_reject_risk"] else ""
            if "knockouts" in fieldnames:
                row["knockouts"] = "; ".join(
                    f"{k['type']}:{k['detail']}({k['status']})" for k in result["knockouts"]
                )
            patched = True
            break

    if not patched:
        return

    tmp = str(path) + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, str(path))


def highlight(text, evidence) -> str:
    """Escape JD text and wrap matched-skill terms in <mark>."""
    text = text or ""
    terms = set()
    for chunk in (evidence or "").split(";"):
        t = chunk.strip()
        if len(t) >= 2:
            terms.add(t)
        for part in re.split(r"[/&,]", t):
            part = part.strip()
            if len(part) >= 2:
                terms.add(part)
    esc = _html.escape(text, quote=False)
    if not terms:
        return esc.replace("\n", "<br>")
    pats = sorted((re.escape(t) for t in terms), key=len, reverse=True)
    rx = re.compile(r"(?<!\w)(" + "|".join(pats) + r")(?!\w)", re.I)
    return rx.sub(lambda m: f'<mark class="kw">{m.group(0)}</mark>', esc).replace("\n", "<br>")
