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

from app import db, queries

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
         view: str = "", day: str = ""):
    con = conn()
    days = queries.available_days(con)
    # Default to the newest day so the board lands on today's batch after a Run;
    # prior days stay selectable via the day nav and "All days".
    current_day = day if day else (days[0] if days else "all")
    effective_day = None if (not current_day or current_day == "all") else current_day

    jobs = queries.get_jobs(con, q=q or None, sector=sector or None, min_score=min_score,
                            status=status or None, show_dismissed=bool(show_dismissed),
                            starred_only=bool(starred), view=view or None, day=effective_day)
    # Auto-defaulted to the newest day but nothing visible there -> fall back to all.
    if not day and effective_day and not jobs:
        current_day, effective_day = "all", None
        jobs = queries.get_jobs(con, q=q or None, sector=sector or None, min_score=min_score,
                                status=status or None, show_dismissed=bool(show_dismissed),
                                starred_only=bool(starred), view=view or None, day=None)

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
              "view": view, "day": current_day},
        "view": view,
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
                                       "locations": locations})


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

    # Parse résumé into the Builder schema and cache for prefill
    try:
        from resume_parser import parse_resume, save_parsed
        parsed = parse_resume(resume_text, use_llm=bool(os.environ.get("LLM_API_KEY")))
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

    con = conn()
    queries.set_resume(con, dest.name, summary)
    state = queries.onboarding_state(con)
    onb = queries.get_onboarding(con)
    positions = queries.list_positions(con)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(request, "_ob_steps.html",
                                      {"state": state, "onb": onb,
                                       "positions": positions, "locations": locations})


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
                                       "positions": positions, "locations": locations})


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
                                       "positions": positions, "locations": locations})


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
                                       "positions": positions, "locations": locations})


@app.post("/onboarding/location", response_class=HTMLResponse)
def onboarding_add_locations(request: Request, names: str = Form("")):
    con = conn()
    queries.add_locations(con, names)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(request, "_locations.html", {"locations": locations})


@app.post("/onboarding/location/delete", response_class=HTMLResponse)
def onboarding_delete_location(request: Request, name: str = Form(...)):
    con = conn()
    queries.remove_location(con, name)
    locations = queries.list_locations(con)
    con.close()
    return templates.TemplateResponse(request, "_locations.html", {"locations": locations})


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


@app.get("/people", response_class=HTMLResponse)
def people(request: Request):
    con = conn(); ctx = {"groups": queries.people(con)}; con.close()
    return templates.TemplateResponse(request, "people.html", ctx)


@app.get("/tracker", response_class=HTMLResponse)
def tracker(request: Request):
    con = conn()
    ctx = {
        "rows": queries.tracker_rows(con),
        "summary": queries.tracker_summary(con),
    }
    con.close()
    return templates.TemplateResponse(request, "tracker.html", ctx)


@app.post("/tracker/{row_key:path}/status", response_class=HTMLResponse)
def tracker_status(request: Request, row_key: str, value: str = Form(...)):
    con = conn()
    if value == "applied":
        queries.set_applied(con, row_key)
    else:
        queries.set_status(con, row_key, value)
    r = queries.get_tracker_row(con, row_key)
    con.close()
    return templates.TemplateResponse(request, "_tracker_row.html", {"r": r})


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
    rows = queries.tracker_rows(con)
    con.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Company", "Role", "Fit %", "Status", "Applied date", "Contacts", "Job URL", "Notes"])
    for r in rows:
        writer.writerow([
            r.get("company", ""),
            r.get("role_title", ""),
            r.get("match_score", ""),
            r.get("status", ""),
            r.get("applied_at", ""),
            r.get("contacts", 0),
            r.get("job_url", ""),
            r.get("notes", ""),
        ])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=northstar_tracker.csv"},
    )


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
