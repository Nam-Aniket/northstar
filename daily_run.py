"""daily_run.py — single entrypoint for the frictionless daily pipeline run.

Called by: in-app Run button, cron, launchd.
Runs: prepare → score → generate → sync (in-process).
"""
from __future__ import annotations

import argparse
import datetime
import subprocess
import sys
from pathlib import Path

import run_status
from run_pipeline import SCRIPTS

ROOT = Path(__file__).parent

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
    r = subprocess.run(
        [sys.executable, str(SCRIPTS[name]), *extra],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
    )
    ok = r.returncode == 0
    err = (r.stderr or "")[-500:]
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

    base_order = ["discover", "fetch", "prepare", "score", "generate"]
    start_idx = base_order.index(args.from_stage)
    order = base_order[start_idx:]

    # Fresh discovery merges into job_alerts_raw.csv; a stale job_posts_enriched.csv
    # would shadow it at the prepare step, so drop it whenever we re-discover.
    if "discover" in order:
        (ROOT / "job_posts_enriched.csv").unlink(missing_ok=True)

    if not run_status.acquire_lock():
        print("already running")
        sys.exit(0)

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
        try:
            from app import db
            from app import sync as appsync

            con = db.connect()
            db.init_schema(con)
            appsync.sync(con, _now())
            con.close()
        except Exception as e:
            run_status.write(
                stage="error",
                ok=False,
                error_stage="sync",
                error_detail=str(e)[-500:],
                finished_at=_now(),
                message="Failed at sync",
            )
            return

        run_status.write(stage="done", pct=100, message="Up to date", ok=True, finished_at=_now())

    finally:
        run_status.release_lock()


if __name__ == "__main__":
    main()
