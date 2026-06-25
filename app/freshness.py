"""Read-time freshness helpers for the board. Pure, stdlib only.

Derives a sortable posted timestamp from the existing jd_posted_date (which may
be a date or a full ISO datetime) with fallback to first_seen_date - so no DB
column or migration is needed. `now` is always injected for deterministic tests.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

_ISO_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2}")


def posted_at_of(job: dict) -> str:
    """Best sortable posted timestamp (ISO string) for a shaped job, or "".

    Prefers jd_posted_date (date or full ISO), falls back to first_seen_date.
    ISO strings sort lexicographically in chronological order.
    """
    jd = (job.get("jd_posted_date") or "").strip()
    if jd and _ISO_PREFIX.match(jd):
        return jd
    fs = (job.get("first_seen_date") or "").strip()
    return fs if _ISO_PREFIX.match(fs) else ""


def _parse(iso: str):
    """Parse an ISO date or datetime to an aware UTC datetime, or None."""
    if not iso:
        return None
    s = iso.strip().replace("Z", "+00:00")
    dt = None
    for candidate in (s, s[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
            break
        except ValueError:
            dt = None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def humanize_ago(iso: str, now: datetime) -> str:
    """'12m ago' / '3h ago' (timestamps) or 'today' / '3d ago' (date-only)."""
    dt = _parse(iso)
    if dt is None:
        return ""
    secs = max(0.0, (now - dt).total_seconds())
    if "T" in iso:
        if secs < 60:
            return "just now"
        if secs < 3600:
            return f"{int(secs // 60)}m ago"
        if secs < 86400:
            return f"{int(secs // 3600)}h ago"
    days = int(secs // 86400)
    if days <= 0:
        return "today"
    if days == 1:
        return "yesterday"
    return f"{days}d ago"


def is_just_posted(iso: str, now: datetime) -> bool:
    """True if posted < 30 min ago. Requires a sub-day timestamp."""
    if "T" not in (iso or ""):
        return False
    dt = _parse(iso)
    return dt is not None and 0 <= (now - dt).total_seconds() < 1800


def fresh_ok(iso: str, now: datetime, bucket: str) -> bool:
    """bucket in {'1h','4h','24h'} (else passes). Date-only rows pass only '24h'."""
    limits = {"1h": 3600, "4h": 14400, "24h": 86400}
    lim = limits.get(bucket)
    if lim is None:
        return True
    dt = _parse(iso)
    if dt is None:
        return False
    secs = (now - dt).total_seconds()
    if "T" not in iso:
        return bucket == "24h" and secs < 2 * 86400
    return 0 <= secs < lim
