"""Job-Hunt Console — local FastAPI app over the pipeline outputs."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
from io import BytesIO
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import db, queries

ROOT = Path(__file__).resolve().parent.parent
RESUME_DIR = ROOT / "resumes"
APP_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Job-Hunt Console")
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.filters["highlight"] = queries.highlight
templates.env.filters["urlk"] = lambda k: urllib.parse.quote(k or "", safe="")
templates.env.filters["domid"] = lambda k: re.sub(r"[^A-Za-z0-9_-]", "-", k or "")
templates.env.filters["normco"] = queries.normalize_company
templates.env.globals["status_flow"] = queries.STATUS_FLOW
templates.env.globals["person_status_flow"] = queries.PERSON_STATUS_FLOW


def conn():
    return db.connect()


def _card(request, job):
    return templates.TemplateResponse(request, "_job_card.html", {"j": job})


@app.get("/", response_class=HTMLResponse)
def home(request: Request, q: str = "", sector: str = "", min_score: int = 0,
         status: str = "", show_dismissed: int = 0, starred: int = 0,
         view: str = "", day: str = ""):
    con = conn()
    days = queries.available_days(con)
    current_day = day if day else "all"
    effective_day = None if (not current_day or current_day == "all") else current_day

    jobs = queries.get_jobs(con, q=q or None, sector=sector or None, min_score=min_score,
                            status=status or None, show_dismissed=bool(show_dismissed),
                            starred_only=bool(starred), view=view or None, day=effective_day)

    # prev/next day navigation (days is descending: index 0 = newest)
    if current_day and current_day != "all" and current_day in days:
        idx = days.index(current_day)
        prev_day = days[idx + 1] if idx + 1 < len(days) else ""  # older
        next_day = days[idx - 1] if idx - 1 >= 0 else ""         # newer
    else:
        prev_day = ""
        next_day = ""

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
    }
    con.close()
    # HTMX partial refresh of just the list when filtering
    tpl = "_job_list.html" if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "joblist" else "jobs.html"
    return templates.TemplateResponse(request, tpl, ctx)


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
    return templates.TemplateResponse(request, "builder.html", {})


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


@app.post("/sync")
def sync():
    subprocess.run([sys.executable, str(APP_DIR / "sync.py")], cwd=str(ROOT))
    # HX-Redirect makes HTMX do a full client-side navigation instead of swapping
    # the whole page into the Sync button (which produced a duplicate header).
    return Response(status_code=204, headers={"HX-Redirect": "/"})
