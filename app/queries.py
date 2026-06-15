"""Read/write helpers over control_panel.db for the web app."""
from __future__ import annotations

import datetime
import html as _html
import json
import os
import re

from app import db


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
    return d


def get_jobs(con, q=None, sector=None, min_score=0, status=None,
             show_dismissed=False, starred_only=False, view=None, day=None) -> list[dict]:
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
        if sector and d.get("sector") != sector:
            continue
        if status and d["status"] != status:
            continue
        if q:
            hay = f"{d.get('company','')} {d.get('role_title','')} {d.get('location','')} {d.get('sector','')}".lower()
            if q.lower() not in hay:
                continue
        out.append(d)
    out.sort(key=lambda d: (d["match_score"] or 0, d["starred"]), reverse=True)
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


def people(con) -> list[dict]:
    comp = {r["company_key"]: dict(r) for r in con.execute("SELECT * FROM companies")}
    # Overlay manual_company for companies not in Zone-1
    for r in con.execute("SELECT * FROM manual_company"):
        ck = r["company_key"]
        comp.setdefault(ck, {"company_key": ck, "company_name": r["company_name"], "domain": r["domain"] or ""})
    drafts = [dict(r) for r in con.execute("SELECT * FROM drafts")]
    outr = [dict(r) for r in con.execute("SELECT * FROM outreach_log")]
    ps_map = {r["person_key"]: dict(r) for r in con.execute("SELECT * FROM person_state")}
    groups = {}
    for p in con.execute("SELECT * FROM people"):
        ck = p["company_key"]
        g = groups.setdefault(ck, {
            "company":     (comp.get(ck, {}).get("company_name") or ck),
            "company_key": ck,
            "domain":      comp.get(ck, {}).get("domain", ""),
            "people":      [],
        })
        g["people"].append(_person_row(p, ps_map.get(p["person_key"]), drafts, outr))
    # Append manual people whose key is not already present
    seen_keys = {pp["person_key"] for g in groups.values() for pp in g["people"]}
    for p in con.execute("SELECT * FROM manual_people"):
        pk = p["person_key"]
        if pk in seen_keys:
            continue
        ck = p["company_key"]
        comp_info = comp.get(ck, {})
        g = groups.setdefault(ck, {
            "company":     (comp_info.get("company_name") or ck),
            "company_key": ck,
            "domain":      comp_info.get("domain", ""),
            "people":      [],
        })
        g["people"].append(_person_row(p, ps_map.get(pk), drafts, outr, is_manual=True))
    out = sorted(groups.values(), key=lambda g: (-len(g["people"]), g["company"].lower()))
    for g in out:
        g["count"] = len(g["people"])
        g["emails"] = sum(1 for pp in g["people"] if pp["has_email"])
        g["contacted"] = sum(1 for pp in g["people"] if pp["outreach_status"] in ("contacted", "followup_due", "replied"))
    return out


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


def tracker_rows(con) -> list[dict]:
    """All scored jobs with application state + contact count, sorted by match_score desc."""
    # Build contact count by normalize_company(company_key) -> count (Zone-1 + manual)
    contact_counts: dict[str, int] = {}
    for r in con.execute(
        "SELECT company_key, COUNT(*) AS cnt FROM people GROUP BY company_key"
    ).fetchall():
        contact_counts[normalize_company(r["company_key"])] = r["cnt"]
    for r in con.execute(
        "SELECT company_key, COUNT(*) AS cnt FROM manual_people GROUP BY company_key"
    ).fetchall():
        nk = normalize_company(r["company_key"])
        contact_counts[nk] = contact_counts.get(nk, 0) + r["cnt"]

    rows = []
    for r in con.execute(
        "SELECT j.row_key, j.company, j.role_title, j.job_url, j.match_score, j.sector, "
        "       s.applied_at, s.notes, a.status AS app_status "
        "FROM jobs j "
        "LEFT JOIN app_state s ON j.row_key = s.row_key "
        "LEFT JOIN application_status a ON j.row_key = a.row_key "
        "WHERE j.match_score IS NOT NULL "
        "ORDER BY j.match_score DESC"
    ).fetchall():
        d = dict(r)
        score = d.get("match_score") or 0
        applied = bool(d.get("applied_at"))
        applied_at_raw = d.get("applied_at") or ""
        d["score_tone"] = "high" if score >= 75 else ("mid" if score >= 55 else "low")
        d["applied"] = applied
        d["applied_at"] = applied_at_raw[:10] if applied_at_raw else ""
        d["notes"] = d.get("notes") or ""
        d["status"] = d.get("app_status") or ("applied" if applied else "new")
        d["contacts"] = contact_counts.get(normalize_company(d.get("company") or ""), 0)
        rows.append(d)
    return rows


def get_tracker_row(con, row_key: str) -> dict | None:
    """Single tracker row for HTMX swap after a status update."""
    contact_counts: dict[str, int] = {}
    for r in con.execute(
        "SELECT company_key, COUNT(*) AS cnt FROM people GROUP BY company_key"
    ).fetchall():
        contact_counts[normalize_company(r["company_key"])] = r["cnt"]
    for r in con.execute(
        "SELECT company_key, COUNT(*) AS cnt FROM manual_people GROUP BY company_key"
    ).fetchall():
        nk = normalize_company(r["company_key"])
        contact_counts[nk] = contact_counts.get(nk, 0) + r["cnt"]

    r = con.execute(
        "SELECT j.row_key, j.company, j.role_title, j.job_url, j.match_score, j.sector, "
        "       s.applied_at, s.notes, a.status AS app_status "
        "FROM jobs j "
        "LEFT JOIN app_state s ON j.row_key = s.row_key "
        "LEFT JOIN application_status a ON j.row_key = a.row_key "
        "WHERE j.row_key = ?",
        (row_key,),
    ).fetchone()
    if not r:
        return None
    d = dict(r)
    score = d.get("match_score") or 0
    applied = bool(d.get("applied_at"))
    applied_at_raw = d.get("applied_at") or ""
    d["score_tone"] = "high" if score >= 75 else ("mid" if score >= 55 else "low")
    d["applied"] = applied
    d["applied_at"] = applied_at_raw[:10] if applied_at_raw else ""
    d["notes"] = d.get("notes") or ""
    d["status"] = d.get("app_status") or ("applied" if applied else "new")
    d["contacts"] = contact_counts.get(normalize_company(d.get("company") or ""), 0)
    return d


def tracker_summary(con) -> dict:
    g = lambda s, *a: con.execute(s, a).fetchone()[0]
    total = g("SELECT COUNT(*) FROM jobs WHERE match_score IS NOT NULL")
    applied = g("SELECT COUNT(*) FROM app_state WHERE applied_at IS NOT NULL")
    interviewing = g(
        "SELECT COUNT(*) FROM application_status WHERE status IN ('phone_screen','interview')"
    )
    offers = g("SELECT COUNT(*) FROM application_status WHERE status = 'offer'")
    return {"total": total, "applied": applied, "interviewing": interviewing, "offers": offers}


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
