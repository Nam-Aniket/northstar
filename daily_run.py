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

_NON_FATAL = {"fetch"}


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _run_stage(name: str) -> tuple[bool, str]:
    r = subprocess.run(
        [sys.executable, str(SCRIPTS[name])],
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
        choices=["prepare", "score", "generate"],
        default="prepare",
        metavar="STAGE",
        help="Start from this stage (default: prepare).",
    )
    ap.add_argument(
        "--with-fetch", action="store_true",
        help="Prepend the non-fatal 'fetch' stage before prepare.",
    )
    args = ap.parse_args()

    base_order = ["prepare", "score", "generate"]
    start_idx = base_order.index(args.from_stage)
    order = base_order[start_idx:]
    if args.with_fetch:
        order = ["fetch"] + order

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
                message=f"Running {name}...",
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
