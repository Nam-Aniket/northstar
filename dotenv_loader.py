"""
Tiny stdlib .env loader. No python-dotenv dependency.

Scripts that need secrets import this at the top:

    from dotenv_loader import load_env
    load_env()

It reads a `.env` file in the current working directory (or this folder)
and populates os.environ for any keys not already set in the shell.
Lines starting with '#' are ignored. Values can be quoted or unquoted.
Existing env vars take precedence over .env values.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(path: str | Path | None = None) -> int:
    """Load KEY=VALUE pairs from .env into os.environ. Returns count loaded."""
    if path is None:
        # Try CWD first, then the directory this module lives in
        for candidate in (Path.cwd() / ".env", Path(__file__).parent / ".env"):
            if candidate.exists():
                path = candidate
                break
        else:
            return 0
    p = Path(path)
    if not p.exists():
        return 0

    count = 0
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip()
        # strip optional surrounding quotes
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        # don't overwrite vars already set in the shell
        if key and key not in os.environ:
            os.environ[key] = val
            count += 1
    return count
