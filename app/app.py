"""Job-Hunt Console — local FastAPI app over the pipeline outputs."""
from __future__ import annotations

import html as _html
import json
import os
import re
import subprocess
import sys
import urllib.parse
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, locations as app_locations, queries, relevance

import config

# Override dropdown values -> seniority cap rank. "" / "auto" = derive from tracked roles.
_LEVEL_OVERRIDE = {"entry": 1, "mid": 2, "senior": 3}

ROOT = Path(__file__).resolve().parent.parent
RESUME_DIR = ROOT / "resumes"
APP_DIR = Path(__file__).resolve().parent
UPLOADS_DIR = ROOT / "uploads"

# Resting state for run_status.json. Used to "consume" a finished/failed run so
# it can't replay a page refresh on the next load.
_IDLE = {"stage": "idle", "pct": 0, "message": "", "started_at": None,
         "finished_at": None, "ok": None, "error_stage": None, "error_detail": None}

app = FastAPI(title="Job-Hunt Console")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["highlight"] = queries.highlight
# Bold the numbers in an insight caption - preattentive emphasis on the "so what".
# quote=False keeps apostrophes literal; escaping them to &#x27; would let the
# digit-bolding regex mangle the entity ("&#x<b>27</b>;").
templates.env.filters["swd"] = lambda t: re.sub(r"(\d+)", r"<b>\1</b>", _html.escape(t or "", quote=False))
templates.env.filters["urlk"] = lambda k: urllib.parse.quote(k or "", safe="")
templates.env.filters["domid"] = lambda k: re.sub(r"[^A-Za-z0-9_-]", "-", k or "")
templates.env.filters["normco"] = queries.normalize_company
# Relative "posted X ago" + just-posted highlight on job cards.
from datetime import datetime as _dt, timezone as _tz
from app import freshness as _freshness
templates.env.filters["ago"] = lambda iso: _freshness.humanize_ago(iso or "", _dt.now(_tz.utc))
templates.env.filters["isfresh"] = lambda iso: _freshness.is_just_posted(iso or "", _dt.now(_tz.utc))


def _reldate(s):
    """Render an ISO date/datetime string as a friendly relative label."""
    if not s:
        return ""
    from datetime import datetime
    try:
        d = datetime.fromisoformat(str(s)[:19])
    except Exception:
        return str(s)[:10]
    days = (datetime.now().date() - d.date()).days
    if days < 0:
        return d.strftime("%b %d")
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 7:
        return "%dd ago" % days
    if days < 30:
        return "%dw ago" % (days // 7)
    if days < 365:
        return "%dmo ago" % (days // 30)
    return d.strftime("%b %Y")


templates.env.filters["reldate"] = _reldate
templates.env.globals["status_flow"] = queries.STATUS_FLOW
templates.env.globals["person_status_flow"] = queries.PERSON_STATUS_FLOW


def conn():
    return db.connect()


# Ensure all tables (Zone-1 + Zone-2 incl. onboarding/tracked_positions) exist on
# startup, so a fresh install — or any DB not yet initialised — never 500s.
_init_con = db.connect()
db.init_schema(_init_con)
_init_con.close()


def _card(request, job):
    return templates.TemplateResponse(request, "_job_card.html", {"j": job})


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", sector: str = "", min_score: int = 0,
         status: str = "", show_dismissed: int = 0, starred: int = 0,
         view: str = "", day: str = "", everything: int = 0, level: str = "",
         fresh: str = ""):
    con = conn()
    days = queries.available_days(con)
    # Default to the newest day so the board lands on today's batch after a Run;
    # prior days stay selectable via the day nav and "All days".
    current_day = day if day else (days[0] if days else "all")
    effective_day = None if (not current_day or current_day == "all") else current_day

    jobs = queries.get_jobs(con, q=q or None, sector=sector or None, min_score=min_score,
                            status=status or None, show_dismissed=bool(show_dismissed),
                            starred_only=bool(starred), view=view or None, day=effective_day,
                            fresh=fresh or None)
    # Auto-defaulted to the newest day but nothing visible there -> fall back to all.
    if not day and effective_day and not jobs:
        current_day, effective_day = "all", None
        jobs = queries.get_jobs(con, q=q or None, sector=sector or None, min_score=min_score,
                                status=status or None, show_dismissed=bool(show_dismissed),
                                starred_only=bool(starred), view=view or None, day=None,
                                fresh=fresh or None)

    # "For me" smart view: default board only, unless the user clicked "Show everything".
    for_me_active = (view in ("", None)) and not everything
    hidden_count = 0
    if for_me_active:
        profile = relevance.target_profile(con)
        override = _LEVEL_OVERRIDE.get(level)  # None when level is "" / "auto" / unknown
        before = len(jobs)
        jobs = relevance.apply_for_me_view(jobs, profile, override_rank=override,
                                           cand_years=config.years_experience)
        hidden_count = before - len(jobs)

    # prev/next day navigation (days is descending: index 0 = newest)
    if current_day and current_day != "all" and current_day in days:
        idx = days.index(current_day)
        prev_day = days[idx + 1] if idx + 1 < len(days) else ""  # older
        next_day = days[idx - 1] if idx - 1 >= 0 else ""         # newer
    else:
        prev_day = ""
        next_day = ""

    onb_state = queries.onboarding_state(con)
    if onb_state != "READY":
        con.close()
        return RedirectResponse("/onboarding", status_code=302)
    onb = queries.get_onboarding(con)
    positions = queries.list_positions(con)
    ctx = {
        "request": request, "jobs": jobs, "stats": queries.stats(con),
        "sectors": queries.sectors(con),
        "f": {"q": q, "sector": sector, "min_score": min_score, "status": status,
              "show_dismissed": show_dismissed, "starred": starred,
              "view": view, "day": current_day, "everything": everything, "level": level,
              "fresh": fresh},
        "view": view,
        "for_me_active": for_me_active,
        "hidden_count": hidden_count,
        "everything": everything,
        "level": level,
        "days": days,
        "current_day": current_day,
        "prev_day": prev_day,
        "next_day": next_day,
        "onb_state": onb_state,
        "onb": onb,
        "positions": positions,
    }
    con.close()
    # HTMX partial refresh of just the list when filtering
    tpl = "_job_list.html" if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "joblist" else "jobs.html"
    return templates.TemplateResponse(request, tpl, ctx)


@app.get("/onboarding", response_class=HTMLResponse)
def onboarding_page(request: Request):
    con = conn()
    state = queries.onboarding_state(con)
    onb = queries.get_onboarding(con)
    positions = queries.list_positions(con)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(request, "onboarding.html",
                                      {"state": state, "onb": onb, "positions": positions,
                                       "locations": locations,
                                       "countries": app_locations.countries()})


@app.post("/onboarding/resume", response_class=HTMLResponse)
async def onboarding_resume_upload(request: Request, file: UploadFile = File(...)):
    ext = Path(file.filename).suffix.lower() if file.filename else ""
    if ext not in (".docx", ".pdf"):
        return Response(
            status_code=200,
            headers={"HX-Reswap": "none",
                     "HX-Trigger": json.dumps({"toast": "Only .docx or .pdf files are accepted."})},
        )
    UPLOADS_DIR.mkdir(exist_ok=True)
    # Remove any existing base_resume.* with a different extension
    for old in UPLOADS_DIR.glob("base_resume.*"):
        old.unlink(missing_ok=True)
    dest = UPLOADS_DIR / f"base_resume{ext}"
    dest.write_bytes(await file.read())

    # Build skills in-process
    import config as _config
    from build_profile import _load_taxonomy, build_skills_json, extract_text, match_resume
    if ext == ".pdf":
        try:
            import pypdf  # noqa: F401
        except ImportError:
            dest.unlink(missing_ok=True)
            return Response(
                status_code=200,
                headers={"HX-Reswap": "none",
                         "HX-Trigger": json.dumps({"toast": "pypdf is not installed. Run pip install pypdf or upload a .docx file."})},
            )
    taxonomy_path = _config.ROOT / "taxonomy.json"
    resume_text = extract_text(dest)
    taxonomy = _load_taxonomy(taxonomy_path)
    present = match_resume(resume_text, taxonomy)
    skills = build_skills_json(present, taxonomy)
    import json as _json
    skills_path = _config._SKILLS_PATH
    # Write to real skills.json (not the example)
    real_skills = _config.ROOT / "skills.json"
    tmp_path = str(real_skills) + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        _json.dump(skills, f, indent=2)
        f.write("\n")
    os.replace(tmp_path, str(real_skills))

    n_skills = len(skills.get("supported_skills", {}))
    import collections
    by_group: collections.Counter = collections.Counter(
        v["group"] for v in skills.get("supported_skills", {}).values()
    )
    n_groups = len(by_group)
    summary = f"Found {n_skills} skills across {n_groups} groups"
    if n_skills < _config.LOW_SKILL_FLOOR:
        summary += (f". Only {n_skills} detected - add more detail (tools, "
                    "technologies, methods) so more jobs can match.")

    # Parse résumé into the Builder schema and cache for prefill
    try:
        from resume_parser import parse_resume, save_parsed
        # Deterministic parse only. The LLM overlay made a slow (up to 60s) network
        # call that blocked the entire upload response and caused the "takes minutes"
        # lag. Onboarding only needs name/contact + skills + facts, all derivable
        # without the LLM; Builder AI-write still uses it on demand.
        parsed = parse_resume(resume_text, use_llm=False)
        save_parsed(parsed, UPLOADS_DIR)
        n_roles = len(parsed.get("experiences") or [])
        if n_roles:
            summary += f"; {n_roles} role{'s' if n_roles != 1 else ''} parsed"
    except Exception:
        pass  # parse failure must never break the skills flow

    # Build facts.json for per-job tailored resume generation.
    try:
        from facts_bridge import build_facts, save_facts
        facts = build_facts(parsed)
        save_facts(facts, ROOT)
    except Exception:
        pass  # facts build failure must never break onboarding

    # Copy the parsed identity into config.json so generated resumes carry the real
    # name/contact instead of the shipped "Jordan Rivera" placeholder.
    try:
        _contact = " | ".join(p for p in [
            (parsed.get("location") or "").strip(),
            (parsed.get("email") or "").strip(),
            (parsed.get("phone") or "").strip(),
            (parsed.get("linkedin") or "").strip(),
        ] if p)
        queries.set_identity((parsed.get("name") or "").strip(), _contact)
    except Exception:
        pass  # identity write must never break onboarding

    try:
        def _edu_line(e):
            parts = [e.get("degree"), e.get("school"), e.get("year")]
            gpa = (e.get("gpa") or "").strip()
            if gpa:
                label = (e.get("gpa_label") or "").strip()
                parts.append(f"{label} {gpa}".strip())  # unlabeled score renders as-is
            return ", ".join(p.strip() for p in parts if p and p.strip())
        _edu_lines = [_edu_line(e) for e in (parsed.get("education") or [])]
        queries.set_education(_edu_lines)
    except Exception:
        pass  # education write must never break onboarding

    try:
        _projects = [
            [p.get("name", ""), list(p.get("bullets") or [])]
            for p in (parsed.get("projects") or []) if p.get("name")
        ]
        if _projects:
            queries.set_projects(_projects)
    except Exception:
        pass  # projects write must never break onboarding

    con = conn()
    queries.set_resume(con, dest.name, summary)
    state = queries.onboarding_state(con)
    onb = queries.get_onboarding(con)
    positions = queries.list_positions(con)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(request, "_ob_steps.html",
                                      {"state": state, "onb": onb,
                                       "positions": positions, "locations": locations,
                                       "countries": app_locations.countries()})


@app.post("/onboarding/resume/delete", response_class=HTMLResponse)
def onboarding_resume_delete(request: Request):
    for f in UPLOADS_DIR.glob("base_resume.*"):
        f.unlink(missing_ok=True)
    (UPLOADS_DIR / "parsed_resume.json").unlink(missing_ok=True)
    import config as _config
    real_skills = _config.ROOT / "skills.json"
    real_skills.unlink(missing_ok=True)
    (_config.ROOT / "facts.json").unlink(missing_ok=True)
    con = conn()
    queries.clear_resume(con)
    state = queries.onboarding_state(con)
    onb = queries.get_onboarding(con)
    positions = queries.list_positions(con)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(request, "_ob_steps.html",
                                      {"state": state, "onb": onb,
                                       "positions": positions, "locations": locations,
                                       "countries": app_locations.countries()})


@app.get("/onboarding/resume/download")
def onboarding_resume_download():
    matches = sorted(UPLOADS_DIR.glob("base_resume.*"))
    if not matches:
        return Response("Resume not found", status_code=404)
    con = conn()
    onb = queries.get_onboarding(con)
    con.close()
    name = onb.get("resume_filename") or matches[0].name
    return FileResponse(matches[0], filename=name)


@app.post("/onboarding/positions", response_class=HTMLResponse)
def onboarding_add_positions(request: Request, titles: str = Form("")):
    con = conn()
    state = queries.onboarding_state(con)
    if state == "NEEDS_RESUME":
        con.close()
        return Response(
            status_code=409,
            headers={"HX-Trigger": json.dumps({"toast": "Add a resume first"})},
        )
    queries.add_positions(con, titles)
    state = queries.onboarding_state(con)
    positions = queries.list_positions(con)
    onb = queries.get_onboarding(con)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(request, "_ob_steps.html",
                                      {"state": state, "onb": onb,
                                       "positions": positions, "locations": locations,
                                       "countries": app_locations.countries()})


@app.post("/onboarding/positions/delete", response_class=HTMLResponse)
def onboarding_delete_position(request: Request, title: str = Form(...)):
    con = conn()
    queries.remove_position(con, title)
    state = queries.onboarding_state(con)
    positions = queries.list_positions(con)
    onb = queries.get_onboarding(con)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(request, "_ob_steps.html",
                                      {"state": state, "onb": onb,
                                       "positions": positions, "locations": locations,
                                       "countries": app_locations.countries()})


@app.post("/onboarding/location", response_class=HTMLResponse)
def onboarding_add_locations(
    request: Request,
    country: str = Form(""),
    state: str = Form(""),
    city: str = Form(""),
):
    con = conn()
    queries.add_structured_location(con, country, state, city)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(
        request, "_locations.html",
        {"locations": locations, "countries": app_locations.countries()},
    )


@app.get("/onboarding/location/states", response_class=HTMLResponse)
def onboarding_location_states(request: Request, country: str = ""):
    return templates.TemplateResponse(
        request, "_loc_state_select.html",
        {"states": app_locations.states(country), "country": country},
    )


@app.get("/onboarding/location/cities", response_class=HTMLResponse)
def onboarding_location_cities(request: Request, country: str = "", state: str = ""):
    return templates.TemplateResponse(
        request, "_loc_city_select.html",
        {"cities": app_locations.cities(country, state), "state": state},
    )


@app.post("/onboarding/location/delete", response_class=HTMLResponse)
def onboarding_delete_location(request: Request, name: str = Form(...)):
    con = conn()
    queries.remove_location(con, name)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(
        request, "_locations.html",
        {"locations": locations, "countries": app_locations.countries()},
    )


@app.post("/onboarding/recency")
def onboarding_recency(recency_tpr: str = Form("r86400")):
    con = conn()
    queries.set_recency(con, recency_tpr)
    con.close()
    return Response(status_code=204)


@app.get("/jobs/{row_key:path}/resume")
def resume(row_key: str):
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job or not job.get("resume_file"):
        return HTMLResponse("No resume", status_code=404)
    p = RESUME_DIR / os.path.basename(job["resume_file"])
    return FileResponse(p, filename=p.name) if p.exists() else HTMLResponse("File missing", status_code=404)


@app.get("/jobs/{row_key:path}/cover")
def cover(row_key: str):
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job or not job.get("cover_letter_file"):
        return HTMLResponse("No cover letter", status_code=404)
    p = RESUME_DIR / os.path.basename(job["cover_letter_file"])
    return FileResponse(p, filename=p.name) if p.exists() else HTMLResponse("File missing", status_code=404)


@app.post("/jobs/{row_key:path}/apply", response_class=HTMLResponse)
def apply(request: Request, row_key: str, view: str = Form("card")):
    con = conn(); queries.set_applied(con, row_key); job = queries.get_job(con, row_key); con.close()
    # Fire-and-forget: generate a tailored resume for this job in the background.
    # Bypasses the generation_enabled gate via --row-key. Fails silently if inputs
    # are missing (the subprocess exits cleanly via sys.exit(0)).
    _gen_script = ROOT / "generate_accepted_resumes.py"
    if _gen_script.exists():
        _kwargs: dict = {}
        if os.name == "posix":
            _kwargs["start_new_session"] = True
        else:
            _kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008
        subprocess.Popen(
            [sys.executable, str(_gen_script), "--row-key", row_key],
            cwd=str(ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **_kwargs,
        )
    tpl = "_actionbar.html" if view == "detail" else "_job_card.html"
    return templates.TemplateResponse(request, tpl, {"j": job})


@app.post("/jobs/{row_key:path}/unapply", response_class=HTMLResponse)
def unapply(request: Request, row_key: str, view: str = Form("card")):
    con = conn(); queries.unapply(con, row_key); job = queries.get_job(con, row_key); con.close()
    tpl = "_actionbar.html" if view == "detail" else "_job_card.html"
    return templates.TemplateResponse(request, tpl, {"j": job})


@app.post("/jobs/{row_key:path}/star", response_class=HTMLResponse)
def star(request: Request, row_key: str, view: str = Form("card")):
    con = conn(); queries.toggle_star(con, row_key); job = queries.get_job(con, row_key); con.close()
    tpl = "_actionbar.html" if view == "detail" else "_job_card.html"
    return templates.TemplateResponse(request, tpl, {"j": job})


@app.post("/jobs/{row_key:path}/dismiss", response_class=HTMLResponse)
def dismiss(request: Request, row_key: str, view: str = Form("card")):
    con = conn(); queries.set_dismissed(con, row_key, 1)
    if view == "detail":
        job = queries.get_job(con, row_key); con.close()
        return templates.TemplateResponse(request, "_actionbar.html", {"j": job})
    con.close()
    return HTMLResponse("")  # card removed from board


@app.post("/jobs/{row_key:path}/restore", response_class=HTMLResponse)
def restore(request: Request, row_key: str, view: str = Form("card")):
    con = conn(); queries.set_dismissed(con, row_key, 0); job = queries.get_job(con, row_key); con.close()
    tpl = "_actionbar.html" if view == "detail" else "_job_card.html"
    return templates.TemplateResponse(request, tpl, {"j": job})


@app.post("/jobs/{row_key:path}/status", response_class=HTMLResponse)
def status(request: Request, row_key: str, value: str = Form(...)):
    con = conn(); queries.set_status(con, row_key, value); job = queries.get_job(con, row_key); con.close()
    return templates.TemplateResponse(request, "_actionbar.html", {"j": job})


@app.post("/jobs/{row_key:path}/notes", response_class=HTMLResponse)
def notes(row_key: str, notes: str = Form("")):
    con = conn(); queries.save_notes(con, row_key, notes); con.close()
    return HTMLResponse('<span class="saved-tag">Saved</span>')


@app.get("/insights", response_class=HTMLResponse)
def insights(request: Request, sector: str = "", day: str = ""):
    con = conn()
    ctx = {"d": queries.insights(con, sector or None, day or None),
           "sectors": queries.sectors(con), "days": queries.available_days(con),
           "f": {"sector": sector, "day": day}}
    con.close()
    return templates.TemplateResponse(request, "insights.html", ctx)


@app.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request, q: str = "", status: str = "", app_status: str = "",
            needs_contacts_only: int = 0, sort: str = "activity", dir: str = "",
            show_all: int = 0):
    con = conn()
    groups, stats = queries.tracker_groups(
        con,
        q=q,
        status=status,
        app_status=app_status,
        needs_contacts_only=bool(needs_contacts_only),
        sort=sort or "activity",
        dir=dir,
        show_all=bool(show_all),
    )
    all_companies = queries.company_suggestions(con, "")
    ctx = {
        "groups": groups,
        "stats": stats,
        "all_companies": all_companies,
        "expand_all": bool(q or status or app_status or needs_contacts_only),
        "f": {
            "q": q,
            "status": status,
            "app_status": app_status,
            "needs_contacts_only": needs_contacts_only,
            "sort": sort or "activity",
            "dir": dir,
            "show_all": show_all,
        },
        "person_status_flow": queries.PERSON_STATUS_FLOW,
        "status_flow": queries.STATUS_FLOW,
    }
    con.close()
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_groups.html", ctx)
    return templates.TemplateResponse(request, "contacts.html", ctx)


@app.post("/tracker/ingest", response_class=HTMLResponse)
def tracker_ingest(request: Request, paste: str = Form(""), company: str = Form(""),
                   pattern: str = Form("")):
    from linkedin_people_parser import parse_people
    result = parse_people(paste, company, pattern)
    con = conn()
    candidates = queries.company_suggestions(con, company)
    company_key, company_name, match_type = queries.resolve_company(company, candidates)
    # Merge people + needs_review (flag needs_review=1 for review items)
    all_people = list(result.people)
    for p in result.needs_review:
        p2 = dict(p)
        p2["needs_review"] = 1
        all_people.append(p2)
    counts = queries.ingest_people(con, company_key, company_name, all_people)
    # Build toast
    parts = []
    if counts["added"]:
        parts.append(f"Added {counts['added']}")
    if counts["updated"]:
        parts.append(f"updated {counts['updated']}")
    if counts["needs_review"]:
        parts.append(f"{counts['needs_review']} need review")
    if counts["skipped"]:
        parts.append(f"{counts['skipped']} skipped")
    if match_type == "suffix" or match_type == "exact":
        parts.append(f"linked to existing {company_name}")
    elif match_type == "fuzzy":
        parts.append(f"similar existing: {company_name}")
    toast_msg = (", ".join(parts) or "No new people found") + "."
    groups, stats = queries.tracker_groups(con)
    all_companies = queries.company_suggestions(con, "")
    ctx = {
        "groups": groups,
        "stats": stats,
        "all_companies": all_companies,
        "f": {
            "q": "", "status": "", "app_status": "",
            "needs_contacts_only": 0, "sort": "activity", "dir": "desc",
        },
        "person_status_flow": queries.PERSON_STATUS_FLOW,
        "status_flow": queries.STATUS_FLOW,
    }
    con.close()
    return templates.TemplateResponse(
        request, "_groups.html", ctx,
        headers={"HX-Trigger": json.dumps({"toast": toast_msg})}
    )


@app.post("/tracker/person/{person_key:path}/status", response_class=HTMLResponse)
def tracker_person_status(request: Request, person_key: str, status: str = Form(...)):
    con = conn()
    queries.set_tracker_person_status(con, person_key, status)
    person = queries.get_tracker_person(con, person_key)
    con.close()
    if not person:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_tperson.html",
                                      {"p": person,
                                       "person_status_flow": queries.PERSON_STATUS_FLOW})


@app.post("/tracker/person/{person_key:path}/notes", response_class=HTMLResponse)
def tracker_person_notes(request: Request, person_key: str, notes: str = Form("")):
    con = conn()
    queries.set_tracker_person_notes(con, person_key, notes)
    con.close()
    return HTMLResponse('<span class="saved-tag">Saved</span>')


@app.post("/tracker/job/{row_key:path}/status", response_class=HTMLResponse)
def tracker_job_status(request: Request, row_key: str, status: str = Form(...)):
    con = conn()
    # Reuse set_status (same path as Board's "mark applied"); it writes
    # application_status.status + status_changed_at = now, source = 'manual'.
    queries.set_status(con, row_key, status)
    job = queries.get_tracker_job(con, row_key)
    con.close()
    if not job:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_tjob.html",
                                      {"j": job, "status_flow": queries.STATUS_FLOW})


@app.get("/tracker/companies", response_class=HTMLResponse)
def tracker_companies(request: Request, q: str = ""):
    con = conn()
    suggestions = queries.company_suggestions(con, q)
    con.close()
    parts = [f'<option value="{s["company_name"]}">' for s in suggestions[:20]]
    return HTMLResponse("\n".join(parts))


@app.get("/builder", response_class=HTMLResponse)
def builder(request: Request):
    from dotenv_loader import load_env
    load_env()
    ai_enabled = bool(os.environ.get("LLM_API_KEY"))
    return templates.TemplateResponse(request, "builder.html", {"ai_enabled": ai_enabled})


@app.get("/builder/prefill")
def builder_prefill():
    """Return parsed_resume.json as JSON for Builder prefill, or {} if absent."""
    p = UPLOADS_DIR / "parsed_resume.json"
    if not p.exists():
        return JSONResponse({})
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        return JSONResponse(data)
    except Exception:
        return JSONResponse({})


@app.post("/builder/download")
def builder_download(payload: str = Form(...)):
    data = json.loads(payload)
    from resume_docx import build_resume_docx  # imported here to keep startup fast
    doc = build_resume_docx(data)
    buf = BytesIO()
    doc.save(buf)
    fname = (data.get("name") or "resume").strip().replace(" ", "_") + "_resume.docx"
    return Response(
        content=buf.getvalue(),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/tracker/export")
def tracker_export():
    import csv
    import io
    con = conn()
    rows = queries.tracker_export_rows(con)
    con.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Company", "Name", "Title", "Email", "Outreach Status", "Jobs", "Notes"])
    for r in rows:
        writer.writerow([
            r.get("company_name", ""),
            r.get("name", ""),
            r.get("title", ""),
            r.get("email", ""),
            r.get("outreach_status", ""),
            r.get("jobs_summary", ""),
            r.get("notes", ""),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=northstar_contacts.csv"},
    )


# ── Business mode (B2B outreach) ───────────────────────────────────────────────

@app.get("/business", response_class=HTMLResponse)
def business(request: Request, q: str = "", stage: str = ""):
    con = conn()
    groups, summary = queries.biz_groups(con)
    con.close()
    if q or stage:
        ql = q.lower()
        for g in groups:
            g["prospects"] = [
                p for p in g["prospects"]
                if (not ql or ql in (p.get("name") or "").lower()
                    or ql in (g["company_name"] or "").lower())
                and (not stage or p.get("stage") == stage)
            ]
            g["prospect_count"] = len(g["prospects"])
        groups = [g for g in groups if g["prospects"]]
    ctx = {
        "groups": groups, "summary": summary,
        "f": {"q": q, "stage": stage},
        "biz_stage_flow": queries.BIZ_STAGE_FLOW,
    }
    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(request, "_biz_groups.html", ctx)
    return templates.TemplateResponse(request, "business.html", ctx)


@app.post("/business/ingest", response_class=HTMLResponse)
def business_ingest(request: Request, paste: str = Form(""), company: str = Form(""),
                    pattern: str = Form("")):
    from linkedin_people_parser import parse_people
    result = parse_people(paste, company, pattern)
    con = conn()
    company_key = queries.slugify(company)
    all_people = list(result.people)
    for p in result.needs_review:
        p2 = dict(p); p2["needs_review"] = 1; all_people.append(p2)
    for p in all_people:
        p.setdefault("pattern", pattern)
    counts = queries.ingest_biz_prospects(con, company_key, company, all_people)
    groups, summary = queries.biz_groups(con)
    con.close()
    ctx = {"groups": groups, "summary": summary, "f": {"q": "", "stage": ""},
           "biz_stage_flow": queries.BIZ_STAGE_FLOW}
    toast = (f"Added {counts['added']}, {counts['needs_review']} need review."
             if counts["added"] else "No new prospects found.")
    return templates.TemplateResponse(request, "_biz_groups.html", ctx,
        headers={"HX-Trigger": json.dumps({"toast": toast})})


@app.post("/business/upload-csv", response_class=HTMLResponse)
def business_upload_csv(request: Request, csv_text: str = Form("")):
    con = conn()
    counts = queries.import_biz_csv(con, csv_text)
    groups, summary = queries.biz_groups(con)
    con.close()
    ctx = {"groups": groups, "summary": summary, "f": {"q": "", "stage": ""},
           "biz_stage_flow": queries.BIZ_STAGE_FLOW}
    toast = f"Imported {counts['added']} prospects ({counts['needs_review']} need review)."
    return templates.TemplateResponse(request, "_biz_groups.html", ctx,
        headers={"HX-Trigger": json.dumps({"toast": toast})})


@app.post("/business/prospect/{prospect_key:path}/stage", response_class=HTMLResponse)
def business_set_stage(request: Request, prospect_key: str, stage: str = Form(...)):
    con = conn()
    try:
        queries.set_biz_stage(con, prospect_key, stage)
    except ValueError:
        con.close()
        return HTMLResponse("bad stage", status_code=400)
    p = queries.get_biz_prospect(con, prospect_key)
    con.close()
    if not p:
        return HTMLResponse("", status_code=404)
    return templates.TemplateResponse(request, "_biz_prospect.html",
        {"p": p, "biz_stage_flow": queries.BIZ_STAGE_FLOW})


@app.post("/business/prospect/{prospect_key:path}/notes", response_class=HTMLResponse)
def business_set_notes(request: Request, prospect_key: str, notes: str = Form("")):
    con = conn()
    queries.set_biz_prospect_notes(con, prospect_key, notes)
    con.close()
    return HTMLResponse('<span class="saved-tag">Saved</span>')


@app.post("/business/company/{company_key:path}/priority", response_class=HTMLResponse)
def business_set_priority(request: Request, company_key: str, on: int = Form(1)):
    con = conn()
    queries.set_biz_priority(con, company_key, bool(on))
    con.close()
    return HTMLResponse('<span class="saved-tag">Saved</span>')


@app.post("/builder/ai/summary")
def builder_ai_summary(role: str = Form(""), details: str = Form(""), skills: str = Form("")):
    try:
        from resume_ai import generate_summary
        return JSONResponse({"text": generate_summary(role, details, skills)})
    except RuntimeError as e:
        return JSONResponse({"error": str(e)})
    except Exception as e:
        return JSONResponse({"error": "AI request failed: " + str(e)})


@app.post("/builder/ai/bullets")
def builder_ai_bullets(role: str = Form(""), raw: str = Form("")):
    try:
        from resume_ai import improve_bullets
        return JSONResponse({"text": improve_bullets(raw, role)})
    except RuntimeError as e:
        return JSONResponse({"error": str(e)})
    except Exception as e:
        return JSONResponse({"error": "AI request failed: " + str(e)})


# ---------------------------------------------------------------------------
# Per-job live editor — tailor the resume + cover letter for one job, mark
# missing skills as "have it" (persists + re-scores live), download both.
# These MUST be declared before the catch-all GET /jobs/{row_key:path} below,
# or that route would swallow /edit, /edit/prefill and the POST sub-paths.
# ---------------------------------------------------------------------------

def _parsed_resume() -> dict:
    """Load the user's parsed résumé (clean structured contact + education),
    or {} if absent. Used to seed the editor's static fields."""
    p = UPLOADS_DIR / "parsed_resume.json"
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _flatten_skill_lines(lines) -> str:
    """Turn grouped 'Group: A, B, C' skill lines into one deduped comma list
    for the editor's skills field (and build_resume_docx)."""
    out: list[str] = []
    seen: set[str] = set()
    for line in lines or []:
        part = line.split(":", 1)[1] if ":" in line else line
        for s in part.split(","):
            s = s.strip()
            if s and s.lower() not in seen:
                seen.add(s.lower())
                out.append(s)
    return ", ".join(out)


_DATE_HINT = re.compile(r"\b(19|20)\d{2}\b|present|current", re.IGNORECASE)


def _split_slot_header(header: str) -> tuple[str, str, str]:
    """Recover (role, company, dates) from a facts.json EXPERIENCE_SLOTS header.

    facts_bridge builds the header as ' | '.join([role, company, dates]) dropping
    blanks, so the editor's dedicated Role/Company/Dates fields (and the download
    docx) get structured data instead of one pipe-joined blob in the role field."""
    parts = [p.strip() for p in header.split(" | ") if p.strip()]
    if not parts:
        return header, "", ""
    role = parts[0]
    rest = parts[1:]
    dates = ""
    if rest and _DATE_HINT.search(rest[-1]):
        dates = rest[-1]
        rest = rest[:-1]
    company = ", ".join(rest)
    return role, company, dates


def _editor_prefill(job: dict) -> dict:
    """Build the JD-tailored editable draft for one job: tailored summary,
    JD-prioritised skills, JD-selected experience bullets, real projects and
    education, plus the cover-letter paragraphs and the JD terms (for the live
    coverage meter). Pure read; generation happens on open, no /apply needed."""
    import generate_accepted_resumes as gen

    role = job.get("role_title", "")
    company = job.get("company", "")
    jd = job.get("job_text", "") or ""

    content = gen.build_content({"role_title": role, "company": company}, jd)

    experiences = []
    for title_text, slot in gen.EXPERIENCE_SLOTS:
        bullets = content["bullets_by_slot"].get(slot, [])
        if title_text or bullets:
            exp_role, exp_company, exp_dates = _split_slot_header(title_text)
            experiences.append({"role": exp_role, "company": exp_company,
                                "dates": exp_dates, "bullets": bullets, "slot": slot})

    projects = [{"name": n, "bullets": list(bs)} for n, bs in content.get("projects", [])]

    parsed = _parsed_resume()
    education = [e for e in (parsed.get("education") or []) if isinstance(e, dict)]

    name = config.NAME
    # Prefer the parsed résumé's structured contact fields; fall back to splitting
    # the pre-joined config.CONTACT string on its separators.
    email = parsed.get("email") or ""
    phone = parsed.get("phone") or ""
    location = parsed.get("location") or ""
    linkedin = parsed.get("linkedin") or ""
    if not any([email, phone, location, linkedin]) and config.CONTACT:
        parts = [p.strip() for p in re.split(r"\s*\|\s*", config.CONTACT) if p.strip()]
        for part in parts:
            low = part.lower()
            if "@" in part and not email:
                email = part
            elif "linkedin" in low and not linkedin:
                linkedin = part
            elif re.search(r"\d", part) and "linkedin" not in low and not phone:
                phone = part
            elif not location:
                location = part

    cover_paragraphs = gen.cover_letter_paragraphs(content, company, role, jd)

    # JD terms for the live coverage meter: evidenced + gap labels for this job.
    gaps = job.get("gaps_list", [])
    jd_terms = sorted(set(job.get("evidence_list", []) + gaps))

    # Don't seed the résumé skills line with skills the candidate doesn't yet
    # evidence (the job's current gaps): listing an unclaimed skill is both
    # dishonest and confusing (it would show as covered here while the Match-fit
    # panel lists it as missing). Claiming a gap adds it back via the editor.
    gap_lc = {g.strip().lower() for g in gaps}
    skills = ", ".join(
        s for s in _flatten_skill_lines(content["skills_lines"]).split(", ")
        if s and s.strip().lower() not in gap_lc
    )

    return {
        "name": name,
        "email": email,
        "phone": phone,
        "location": location,
        "linkedin": linkedin,
        "summary": content["summary"],
        "skills": skills,
        "experiences": experiences,
        "projects": projects,
        "education": education,
        "cover_paragraphs": cover_paragraphs,
        "jd_terms": jd_terms,
    }


@app.get("/jobs/{row_key:path}/edit", response_class=HTMLResponse)
def job_editor(request: Request, row_key: str):
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job:
        return HTMLResponse("Job not found", status_code=404)
    return templates.TemplateResponse(request, "job_editor.html", {"j": job})


@app.get("/jobs/{row_key:path}/edit/prefill")
def job_editor_prefill(row_key: str):
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        return JSONResponse(_editor_prefill(job))
    except Exception as e:
        return JSONResponse({"error": "prefill failed: " + str(e)}, status_code=500)


@app.post("/jobs/{row_key:path}/skills/add", response_class=HTMLResponse)
def job_editor_add_skill(request: Request, row_key: str, label: str = Form(...)):
    con = conn()
    job = queries.get_job(con, row_key)
    if not job:
        con.close()
        return HTMLResponse("Job not found", status_code=404)

    label = (label or "").strip()
    # Only allow adding a skill the scorer actually flagged as a gap for THIS job,
    # so the button can't be used to inject arbitrary skills.
    if label and label in job.get("gaps_list", []):
        import score_jobs
        # Serialise the read-modify-write of skills.json + the in-process bank
        # reset against any concurrent claim (see queries.SKILLS_LOCK).
        with queries.SKILLS_LOCK:
            queries.add_supported_skill(label)
            config.reset_banks()
            score_jobs._reset_caches()
            queries.rescore_job(con, row_key)
        job = queries.get_job(con, row_key)

    con.close()
    return templates.TemplateResponse(request, "_job_fit.html",
                                      {"j": job, "claimed_skill": label})


def _job_target(job: dict) -> dict:
    return {"role_title": job.get("role_title", ""), "company": job.get("company", "")}


def _job_skill_ok(job: dict, skill: str) -> bool:
    """A skill may only be acted on if the scorer flagged it for THIS job
    (a gap or already evidenced) - the same honesty guard as claim-skill."""
    allowed = {s.strip().lower() for s in
               (job.get("gaps_list", []) + job.get("evidence_list", []))}
    return (skill or "").strip().lower() in allowed


@app.get("/jobs/{row_key:path}/bullets", response_class=HTMLResponse)
def job_editor_bullets(request: Request, row_key: str, skill: str = ""):
    import generate_accepted_resumes as gen
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job:
        return HTMLResponse("Job not found", status_code=404)
    skill = (skill or "").strip()
    bullets = []
    suggested_slot = ""
    if skill and _job_skill_ok(job, skill):
        bullets = gen.placeable_bullets_for_skill(
            _job_target(job), job.get("job_text", "") or "", skill)
        suggested_slot = gen.best_slot_for_skill(skill)
    return templates.TemplateResponse(request, "_bullet_suggestions.html", {
        "skill": skill, "bullets": bullets, "row_key": row_key,
        "slots": gen.EXPERIENCE_SLOTS, "suggested_slot": suggested_slot,
    })


@app.post("/jobs/{row_key:path}/bullets/add")
def job_editor_add_bullet(row_key: str, skill: str = Form(...),
                          slot: str = Form(...), text: str = Form(...)):
    import generate_accepted_resumes as gen
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job:
        return JSONResponse({"error": "not found"}, status_code=404)
    skill = (skill or "").strip()
    if not _job_skill_ok(job, skill):
        return JSONResponse({"error": "skill not part of this job"}, status_code=400)
    if slot not in {s for _, s in gen.EXPERIENCE_SLOTS}:
        return JSONResponse({"error": "unknown role"}, status_code=400)
    try:
        gen.add_fact_bullet(slot, text, skill)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    header = next((h for h, s in gen.EXPERIENCE_SLOTS if s == slot), "")
    return JSONResponse({"slot": slot, "text": text.strip(), "role_header": header})


@app.post("/jobs/{row_key:path}/download")
def job_editor_download(row_key: str, payload: str = Form(...), cover: str = Form("")):
    con = conn(); job = queries.get_job(con, row_key); con.close()
    if not job:
        return HTMLResponse("Job not found", status_code=404)

    import zipfile
    from resume_docx import build_resume_docx
    import generate_accepted_resumes as gen

    data = json.loads(payload)

    resume_doc = build_resume_docx(data)
    resume_buf = BytesIO()
    resume_doc.save(resume_buf)

    # Split the edited cover text on blank lines into body paragraphs.
    cover_paras = [p.strip() for p in re.split(r"\n\s*\n", cover or "") if p.strip()]
    cover_buf = BytesIO()
    if cover_paras:
        gen.write_cover_letter_docx_from_paragraphs(cover_paras, cover_buf)

    base = (job.get("company") or "resume").strip().replace(" ", "_").lower()
    base = re.sub(r"[^a-z0-9_]+", "", base) or "application"

    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{base}_resume.docx", resume_buf.getvalue())
        if cover_paras:
            zf.writestr(f"{base}_cover_letter.docx", cover_buf.getvalue())

    return Response(
        content=zip_buf.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{base}_application.zip"'},
    )


@app.get("/jobs/{row_key:path}", response_class=HTMLResponse)
def detail(request: Request, row_key: str):
    con = conn(); job = queries.get_job(con, row_key)
    if not job:
        con.close()
        return HTMLResponse("Job not found", status_code=404)
    company = queries.company_detail(con, queries.normalize_company(job["company"]))
    con.close()
    return templates.TemplateResponse(request, "job_detail.html",
                                       {"j": job, "status_flow": queries.STATUS_FLOW, "company": company})


@app.get("/company/{company_key:path}", response_class=HTMLResponse)
def company(request: Request, company_key: str):
    con = conn()
    ctx = {"c": queries.company_detail(con, company_key)}
    con.close()
    return templates.TemplateResponse(request, "company.html", ctx)


@app.post("/person/{person_key:path}/status", response_class=HTMLResponse)
def person_status(request: Request, person_key: str, value: str = Form(...)):
    con = conn()
    queries.set_person_status(con, person_key, value)
    p = queries.get_person(con, person_key)
    con.close()
    return templates.TemplateResponse(request, "_person_row.html", {"p": p})


@app.post("/company/{company_key:path}/people", response_class=HTMLResponse)
def add_person(request: Request, company_key: str,
               name: str = Form(...), role: str = Form(""),
               email: str = Form(""), linkedin_url: str = Form("")):
    if not name.strip():
        return HTMLResponse('<div class="text-xs text-rose-500 px-3 py-1">Name is required</div>')
    con = conn()
    pk = queries.add_manual_person(con, company_key, name.strip(), role.strip(), email.strip(), linkedin_url.strip())
    p = queries.get_person(con, pk)
    con.close()
    return templates.TemplateResponse(request, "_person_row.html", {"p": p})


@app.post("/company", response_class=HTMLResponse)
def add_company(request: Request,
                company_name: str = Form(...),
                domain: str = Form(""),
                name: list[str] = Form([]),
                role: list[str] = Form([]),
                email: list[str] = Form([]),
                linkedin_url: list[str] = Form([])):
    con = conn()
    ck = queries.add_manual_company(con, company_name.strip(), domain.strip())
    for i, nm in enumerate(name):
        if nm.strip():
            queries.add_manual_person(
                con, ck, nm.strip(),
                role[i].strip() if i < len(role) else "",
                email[i].strip() if i < len(email) else "",
                linkedin_url[i].strip() if i < len(linkedin_url) else "",
            )
    con.close()
    return Response(status_code=204, headers={"HX-Redirect": f"/company/{urllib.parse.quote(ck, safe='')}"})


@app.post("/person/{person_key:path}/delete", response_class=HTMLResponse)
def delete_person(person_key: str):
    con = conn()
    if con.execute("SELECT 1 FROM people WHERE person_key=?", (person_key,)).fetchone():
        con.close()
        return HTMLResponse("", status_code=409)
    queries.delete_manual_person(con, person_key)
    con.close()
    return HTMLResponse("")


@app.post("/sync")
def sync():
    subprocess.run([sys.executable, str(APP_DIR / "sync.py")], cwd=str(ROOT))
    # HX-Redirect makes HTMX do a full client-side navigation instead of swapping
    # the whole page into the Sync button (which produced a duplicate header).
    return Response(status_code=204, headers={"HX-Redirect": "/"})


@app.get("/run/log")
def run_log_view():
    import run_log
    if not run_log.LATEST_PATH.exists():
        return HTMLResponse("No run log yet. Click Run to start a pipeline run.", status_code=404)
    return FileResponse(
        run_log.LATEST_PATH.resolve(),
        media_type="text/plain",
        headers={"Content-Disposition": "inline; filename=latest.log"},
    )


@app.post("/run")
def run_start(request: Request):
    import run_status
    con = conn()
    ob_state = queries.onboarding_state(con)
    con.close()
    if ob_state != "READY":
        return Response(
            status_code=409,
            headers={"HX-Trigger": json.dumps({"toast": "Add a resume and at least one role first"})},
        )
    if run_status.is_running():
        return Response(
            status_code=409,
            headers={"HX-Trigger": json.dumps({"toast": "Pipeline already running"})},
        )
    # Capture this run's output to a persistent log file (no more silent DEVNULL).
    import run_log
    log_path = run_log.new_run_log_path()
    run_log.update_latest(log_path)
    logf = open(log_path, "ab")
    env = {**os.environ, run_log.ENV_VAR: str(log_path)}
    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    try:
        subprocess.Popen(
            [sys.executable, str(ROOT / "daily_run.py")],
            cwd=str(ROOT),
            stdout=logf,
            stderr=subprocess.STDOUT,
            env=env,
            **kwargs,
        )
    finally:
        logf.close()  # the child holds its own dup of the fd
    return templates.TemplateResponse(
        request, "_run_progress.html", {"st": {"stage": "discover", "pct": 2, "message": "Starting…"}}
    )


@app.get("/run/status", response_class=HTMLResponse)
def run_status_view(request: Request):
    """Progress widget endpoint.

    Two callers, distinguished by ?watch=1:
      * cold page load (base.html, no marker) — only starts watching if a run is
        actually live; a stale "done" left by a background run is cleared
        silently with NO reload.
      * active poll (the progress fragment, watch=1) — when the run it is
        watching finishes, the page hard-reloads exactly once via HX-Refresh.
    """
    import run_status
    watch = request.query_params.get("watch") == "1"
    st = run_status.read()
    stage = st.get("stage")

    # Run in progress -> live fragment (the template keeps polling with ?watch=1).
    if stage in ("discover", "fetch", "prepare", "score", "generate", "sync"):
        if run_status.is_running():
            return templates.TemplateResponse(request, "_run_progress.html", {"st": st})
        # Stale in-progress stage with no live process (a crashed run): clear it so
        # the widget never polls forever against a run that will never finish.
        run_status.write(**_IDLE)
        if watch:
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        return templates.TemplateResponse(request, "_run_progress.html", {"st": _IDLE})

    # Failed run -> show the error; fragment carries no poll trigger so polling stops.
    if stage == "error":
        return templates.TemplateResponse(request, "_run_progress.html", {"st": st})

    # Finished run -> consume it so it can never replay on a later load.
    if stage == "done":
        run_status.write(**_IDLE)
        if watch:
            # Only the tab that watched the run reloads, once, with a fresh document.
            return Response(status_code=200, headers={"HX-Refresh": "true"})
        return templates.TemplateResponse(request, "_run_progress.html", {"st": _IDLE})

    # Idle: a cold load that caught a run mid-flight (lock held but no stage yet)
    # becomes a watcher; otherwise just render the quiet idle widget.
    if not watch and run_status.is_running():
        return templates.TemplateResponse(
            request, "_run_progress.html",
            {"st": {**_IDLE, "stage": "discover", "pct": 2, "message": "Starting…"}},
        )
    return templates.TemplateResponse(request, "_run_progress.html", {"st": st})
