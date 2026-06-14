"""run_status.py — atomic JSON status file + lock for the daily pipeline run."""
from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).parent
STATUS_PATH = ROOT / "app" / "run_status.json"
LOCK_PATH = ROOT / "app" / "run.lock"

PCT: dict[str, int] = {
    "prepare": 10,
    "score": 40,
    "generate": 70,
    "sync": 92,
    "done": 100,
}

_DEFAULTS: dict = {
    "stage": "idle",
    "pct": 0,
    "message": "",
    "started_at": None,
    "finished_at": None,
    "ok": None,
    "error_stage": None,
    "error_detail": None,
}


def read() -> dict:
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # Ensure all keys present (fill missing with defaults)
        return {**_DEFAULTS, **data}
    except Exception:
        return dict(_DEFAULTS)


def write(**fields) -> None:
    current = read()
    current.update(fields)
    tmp = STATUS_PATH.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f)
    os.replace(tmp, STATUS_PATH)


def is_running() -> bool:
    if not LOCK_PATH.exists():
        return False
    try:
        pid = int(LOCK_PATH.read_text().strip())
        os.kill(pid, 0)  # signal 0 = existence check only
        return True
    except Exception:
        # Stale lock (pid dead or unreadable) — treat as not running
        return False


def acquire_lock() -> bool:
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def release_lock() -> None:
    try:
        LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
