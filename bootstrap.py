#!/usr/bin/env python3
"""
bootstrap.py — Zero-to-running installer for Northstar.

Usage:
    python3 bootstrap.py            # install deps (if needed) + launch the app
    python3 bootstrap.py --no-launch  # install only (CI / testing)
    python3 bootstrap.py --dry-run  # print what would happen, do nothing
"""

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
SENTINEL = VENV / ".installed"

if os.name == "nt":
    VENV_PYTHON = VENV / "Scripts" / "python.exe"
else:
    VENV_PYTHON = VENV / "bin" / "python"

LAUNCH_CMD = [
    str(VENV_PYTHON),
    "-m", "uvicorn",
    "app.app:app",
    "--host", "127.0.0.1",
    "--port", "8765",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _req_hash() -> str:
    """SHA-256 of requirements.txt."""
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def _venv_exists() -> bool:
    return VENV_PYTHON.exists()


def _install_needed() -> bool:
    if not _venv_exists():
        return True
    if not SENTINEL.exists():
        return True
    return SENTINEL.read_text().strip() != _req_hash()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install dependencies and launch Northstar.",
    )
    parser.add_argument(
        "--no-launch",
        action="store_true",
        help="Set up the venv and install deps, then exit without launching.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without executing anything.",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------ dry run
    if args.dry_run:
        print("=== Northstar bootstrap dry-run ===")
        print(f"  Project root : {ROOT}")
        print(f"  venv path    : {VENV}")
        print(f"  venv python  : {VENV_PYTHON}")
        print(f"  venv exists  : {_venv_exists()}")
        if not REQUIREMENTS.exists():
            print("  requirements : MISSING — cannot install")
        else:
            needs = _install_needed()
            print(f"  install needed: {needs}")
            if needs and _venv_exists() and not SENTINEL.exists():
                print("    reason: sentinel missing")
            elif needs and _venv_exists():
                print("    reason: requirements.txt changed since last install")
            elif needs:
                print("    reason: venv not found")
        print(f"  launch command: {' '.join(LAUNCH_CMD)}")
        if args.no_launch:
            print("  --no-launch set: would exit after install")
        return

    # ------------------------------------------------------------------ venv
    if not _venv_exists():
        print("Setting up Northstar (first run, ~1-2 min)…")
        print(f"  Creating virtual environment at {VENV} …")
        try:
            subprocess.check_call(
                [sys.executable, "-m", "venv", str(VENV)]
            )
        except subprocess.CalledProcessError:
            _die(
                "Failed to create a virtual environment.\n"
                "On Debian/Ubuntu: sudo apt install python3-venv\n"
                "On Fedora/RHEL:   sudo dnf install python3\n"
                "On macOS:         use the python.org installer (includes venv)."
            )

    # ------------------------------------------------------------------ pip install
    if _install_needed():
        if _venv_exists() and not REQUIREMENTS.exists():
            _die(f"requirements.txt not found at {REQUIREMENTS}.")

        print("  Installing dependencies (this may take a minute on first run)…")
        try:
            subprocess.check_call(
                [str(VENV_PYTHON), "-m", "pip", "install", "--upgrade", "pip"],
                stdout=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            _die("pip upgrade failed. Check that your internet connection is active.")

        try:
            subprocess.check_call(
                [str(VENV_PYTHON), "-m", "pip", "install", "-r", str(REQUIREMENTS)]
            )
        except subprocess.CalledProcessError:
            _die(
                "pip install failed.\n"
                "Check your internet connection and that requirements.txt is valid."
            )

        # Write sentinel so we skip install on the next run
        SENTINEL.write_text(_req_hash())
        print("  Dependencies installed.")

    # ------------------------------------------------------------------ launch
    if args.no_launch:
        print("Northstar is ready. (--no-launch: not starting the server.)")
        return

    print(f"Northstar -> http://127.0.0.1:8765")
    print("(Press Ctrl+C to stop.)")

    if os.name == "nt":
        # os.execv is unreliable on Windows; subprocess.call keeps the console alive
        sys.exit(subprocess.call(LAUNCH_CMD))
    else:
        os.execv(str(VENV_PYTHON), LAUNCH_CMD)


def _die(msg: str) -> None:
    print(f"\nError: {msg}", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
