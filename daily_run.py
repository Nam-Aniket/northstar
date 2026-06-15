"""daily_run.py — single entrypoint for the frictionless daily pipeline run.

Called by: in-app Run button, cron, launchd.
Runs: prepare → score → generate → sync (in-process).
"""
from __future__ import annotations

import argparse
import datetime
import os
import subprocess
import sys
from pathlib import Path

import run_log
import run_status
from run_pipeline import SCRIPTS

ROOT = Path(__file__).parent

# Open run-log handle for this run (set in main); _log() mirrors output into it.
_LOG = None
_ECHO = True  # also print to stdout for terminal visibility; off when app-launched

# Network-dependent stages: a non-zero exit is logged but never stops the run.
_NON_FATAL = {"discover", "fetch"}

# Extra CLI args per stage. The JD-backfill (fetch) must target the freshly
# scraped intake, not its default job_posts_enriched.csv.
_STAGE_ARGS = {"fetch": ["--input", "job_alerts_raw.csv"]}

# Human-readable progress messages shown in the in-app pulse.
_STAGE_MSG = {
    "discover": "Searching LinkedIn for new roles",
    "fetch": "Fetching job descriptions",
    "prepare": "Preparing postings",
    "score": "Scoring how well each role fits you",
    "generate": "Building tailored resumes",
}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _log(line: str = "") -> None:
    """Write a line to the run log, and echo to stdout unless app-launched."""
    if _ECHO:
        print(line, flush=True)
    if _LOG is not None:
        _LOG.write(line + "\n")
        _LOG.flush()


def _discover_args() -> list[str]:
    """Build discover-stage CLI args from the user's saved onboarding settings,
    so the LinkedIn search uses their tracked roles, locations, and recency
    instead of 00_search's hardcoded defaults. Empty -> 00_search falls back to
    its own defaults, so the run still works."""
    from app import db, queries
    con = db.connect()
    db.init_schema(con)
    keywords = [p["display"] for p in queries.list_positions(con)]
    locations = [l["display"] for l in queries.list_locations(con)]
    onb = queries.get_onboarding(con)
    tpr = onb.get("recency_tpr") or "r86400"
    con.close()
    a: list[str] = []
    if keywords:
        a += ["--keywords", *keywords]
    if locations:
        a += ["--location", *locations]
    a += ["--tpr", tpr]
    if len(locations) > 1:
        a += ["--max-start", "100"]  # cap pagination when searching many locations (volume control)
    return a


def _run_stage(name: str) -> tuple[bool, str]:
    extra = _discover_args() if name == "discover" else _STAGE_ARGS.get(name, [])
    start = datetime.datetime.now()
    _log(f"\n----- stage {name} START {start.isoformat(timespec='seconds')} -----")
    r = subprocess.run(
        [sys.executable, str(SCRIPTS[name]), *extra],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    dur = (datetime.datetime.now() - start).total_seconds()
    if r.stdout:
        _log(r.stdout.rstrip())
    if r.stderr:
        _log("[stderr]\n" + r.stderr.rstrip())
    _log(f"----- stage {name} END rc={r.returncode} dur={dur:.1f}s -----")
    ok = r.returncode == 0
    err = (r.stderr or "")[-500:]  # short tail still feeds the status UI
    return ok, err


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the full pipeline (prepare→score→generate→sync).")
    ap.add_argument(
        "--from", dest="from_stage",
        choices=["discover", "fetch", "prepare", "score", "generate"],
        default="discover",
        metavar="STAGE",
        help="Start from this stage (default: discover -- scrapes LinkedIn first).",
    )
    ap.add_argument(
        "--with-fetch", action="store_true",
        help="(deprecated no-op; discovery + JD fetch now run by default).",
    )
    args = ap.parse_args()

    global _LOG, _ECHO
    app_launched = bool(os.environ.get(run_log.ENV_VAR))
    log_path = Path(os.environ[run_log.ENV_VAR]) if app_launched else run_log.new_run_log_path()
    _LOG = open(log_path, "a", encoding="utf-8", buffering=1)
    _ECHO = not app_launched  # app redirects stdout into this same file; don't double-write

    base_order = ["discover", "fetch", "prepare", "score", "generate"]
    start_idx = base_order.index(args.from_stage)
    order = base_order[start_idx:]

    _log(f"=== Northstar run {_now()} ===")
    _log(f"launched: {'app' if app_launched else 'terminal'}  stages={order}")
    try:
        from app import db as _db, queries as _q
        _con = _db.connect()
        _db.init_schema(_con)
        roles = [p["display"] for p in _q.list_positions(_con)]
        locs = [l["display"] for l in _q.list_locations(_con)]
        tpr = (_q.get_onboarding(_con).get("recency_tpr") or "r86400")
        _con.close()
        _log(f"config: roles={roles or '(defaults)'} locations={locs or '(defaults)'} recency={tpr}")
    except Exception as e:
        _log(f"config: (unavailable: {e})")

    # Fresh discovery merges into job_alerts_raw.csv; a stale job_posts_enriched.csv
    # would shadow it at the prepare step, so drop it whenever we re-discover.
    if "discover" in order:
        (ROOT / "job_posts_enriched.csv").unlink(missing_ok=True)

    if not run_status.acquire_lock():
        _log("another run holds the lock; exiting without starting")
        _LOG.close()
        sys.exit(0)

    run_log.update_latest(log_path)

    try:
        run_status.write(
            stage="prepare",
            pct=0,
            message="Starting...",
            started_at=_now(),
            finished_at=None,
            ok=None,
            error_stage=None,
            error_detail=None,
        )

        for name in order:
            run_status.write(
                stage=name,
                pct=run_status.PCT.get(name, 0),
                message=_STAGE_MSG.get(name, f"Running {name}"),
            )
            ok, err = _run_stage(name)
            if not ok and name not in _NON_FATAL:
                _log(f"=== FAILED at {name} (full stage output above) ===")
                run_status.write(
                    stage="error",
                    ok=False,
                    error_stage=name,
                    error_detail=err,
                    finished_at=_now(),
                    message=f"Failed at {name}",
                )
                return

        # Sync step — in-process, transactional
        run_status.write(stage="sync", pct=92, message="Updating dashboard...")
        _log("\n----- stage sync START -----")
        try:
            from app import db
            from app import sync as appsync

            con = db.connect()
            db.init_schema(con)
            appsync.sync(con, _now())
            total = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
            scored = con.execute("SELECT COUNT(*) FROM jobs WHERE match_score IS NOT NULL").fetchone()[0]
            con.close()
            _log("----- stage sync END -----")
        except Exception as e:
            import traceback
            _log("[stderr]\n" + traceback.format_exc())
            _log(f"=== FAILED at sync : {e} ===")
            run_status.write(
                stage="error",
                ok=False,
                error_stage="sync",
                error_detail=str(e)[-500:],
                finished_at=_now(),
                message="Failed at sync",
            )
            return

        # Resume-generation visibility. The generate stage silently no-ops in two
        # cases, which previously hid "no resumes were created" behind a SUCCESS
        # line — non-technical users never noticed. Surface the reason.
        import config as _cfg
        no_resume_reason = None
        if not _cfg.generation_enabled:
            no_resume_reason = (
                "resume generation is OFF",
                ['Set  "generation_enabled": true  under "matching" in config.json,',
                 "then run again."],
            )
        elif _cfg.load_facts_override() is None:
            no_resume_reason = (
                "no resume data found",
                ["Upload your resume in onboarding so tailored resumes can be built",
                 "from your real experience, then run again."],
            )
        done_msg = "Up to date (no resumes - see log)" if no_resume_reason else "Up to date"
        run_status.write(stage="done", pct=100, message=done_msg, ok=True, finished_at=_now())
        if no_resume_reason:
            why, how = no_resume_reason
            _log("")
            _log("!" * 60)
            _log(f"[!] NO RESUMES GENERATED - {why}.")
            _log("    No tailored resume or cover letter was created for any job.")
            for line in how:
                _log("    " + line)
            _log("!" * 60)
        tail = " | NO resumes (see above)" if no_resume_reason else ""
        _log(f"=== SUCCESS : {total} postings synced, {scored} scored onto the board{tail} ===")

    finally:
        run_status.release_lock()
        if _LOG is not None:
            _LOG.close()


if __name__ == "__main__":
    main()
