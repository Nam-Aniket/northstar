"""run_log.py — per-run log file paths + a small helper for the latest pointer.

Lives alongside run_status.py. Both the web app and daily_run.py import this so
they agree on where a run's log goes. Log files are gitignored runtime data; only
this module is committed.
"""
from __future__ import annotations

import datetime
import shutil
from pathlib import Path

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
LATEST_PATH = LOG_DIR / "latest.log"

# The app sets this env var when it launches daily_run.py, so the child writes to
# the exact same file the parent opened (instead of minting a second timestamp).
ENV_VAR = "NORTHSTAR_RUN_LOG"


def new_run_log_path() -> Path:
    """Return a fresh timestamped log path, creating logs/ if needed."""
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOG_DIR / f"run_{ts}.log"


def update_latest(target: Path) -> None:
    """Point logs/latest.log at the most recent run log (symlink, copy fallback)."""
    LOG_DIR.mkdir(exist_ok=True)
    try:
        if LATEST_PATH.exists() or LATEST_PATH.is_symlink():
            LATEST_PATH.unlink()
        LATEST_PATH.symlink_to(target.name)  # relative, stays valid inside logs/
    except OSError:
        shutil.copyfile(target, LATEST_PATH)
