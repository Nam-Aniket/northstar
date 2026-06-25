"""app/relevance.py — read-time relevance + seniority classification for the board.

Pure functions, stdlib only. No DB writes. The scorer is untouched; these signals
are an orthogonal presentation layer used by the "For me" board view.
"""
from __future__ import annotations

import re

MID_RANK = 2


class _Desc:
    """Wrap a value so ascending sort yields descending order of the original.

    Lets a single sort key mix ascending fields (over_level) with a descending
    string field (posted_at, an ISO timestamp that can't be numerically negated).
    """
    __slots__ = ("v",)

    def __init__(self, v):
        self.v = v or ""

    def __lt__(self, other):
        return self.v > other.v

    def __eq__(self, other):
        return self.v == other.v

# Checked most-senior first so a multi-word title resolves to its highest level.
# Each entry: (label, rank, [trigger phrases]). Phrases are matched on word
# boundaries against the lowercased title.
_SENIORITY_RULES = [
    ("exec",      6, ["vp", "vice president", "chief", "ceo", "cto", "cfo", "c-level"]),
    ("director",  5, ["director", "head of"]),
    ("manager",   4, ["manager", "mgr"]),
    ("principal", 4, ["principal", "staff"]),
    ("senior",    3, ["senior", "snr", "sr"]),
    ("lead",      3, ["lead"]),
    ("entry",     1, ["junior", "jnr", "entry", "associate"]),
    ("graduate",  1, ["graduate", "grad", "trainee"]),
    ("intern",    0, ["intern", "internship"]),
]


def parse_seniority(title: str) -> tuple[str, int]:
    """Return (label, rank) parsed from a job title. Default ('mid', 2)."""
    t = (title or "").lower()
    for label, rank, phrases in _SENIORITY_RULES:
        for p in phrases:
            if re.search(r"\b" + re.escape(p) + r"\b", t):
                return (label, rank)
    return ("mid", MID_RANK)


# Words removed before comparing titles: seniority words + generic filler.
_SENIORITY_WORDS = {
    "vp", "vice", "president", "chief", "ceo", "cto", "cfo",
    "director", "head", "manager", "mgr", "principal", "staff",
    "senior", "snr", "sr", "lead", "junior", "jnr", "entry",
    "associate", "graduate", "grad", "trainee", "intern", "internship",
}
_FILLER_WORDS = {
    "the", "a", "an", "of", "and", "or", "to", "for", "in", "at", "with",
    "remote", "hybrid", "onsite", "contract", "permanent", "fulltime", "parttime",
    "full", "part", "time", "new", "role", "job", "position", "officer",
    "i", "ii", "iii", "iv",
}
_STOP = _SENIORITY_WORDS | _FILLER_WORDS
_TOKEN_RE = re.compile(r"[a-z0-9&]+")


def title_tokens(title: str) -> set[str]:
    """Meaningful lowercase tokens of a title, with seniority + filler removed."""
    return {w for w in _TOKEN_RE.findall((title or "").lower()) if w not in _STOP}


def target_profile(con) -> dict:
    """Derive the user's relevance profile from tracked_positions.

    Returns {title_tokens, tracked_titles, max_seniority_rank}.
    Empty title_tokens signals 'unconfigured' -> the view hides nothing.
    """
    rows = con.execute("SELECT title FROM tracked_positions").fetchall()
    titles = [r["title"] for r in rows if r["title"]]
    tokens: set[str] = set()
    max_rank = MID_RANK
    for t in titles:
        tokens |= title_tokens(t)
        max_rank = max(max_rank, parse_seniority(t)[1])
    return {
        "title_tokens": tokens,
        "tracked_titles": titles,
        "max_seniority_rank": max_rank,
    }


def classify_job(job: dict, profile: dict, cap: int) -> dict:
    """Merge relevance + seniority flags onto a shaped job dict and return it.

    on_target: title shares >=1 meaningful token with the tracked titles.
               If the profile is unconfigured (no tokens), everything is on_target.
    over_level: seniority rank exceeds `cap`.
    """
    title = job.get("role_title", "")
    label, rank = parse_seniority(title)
    targets = profile.get("title_tokens") or set()
    if targets:
        on_target = bool(title_tokens(title) & targets)
    else:
        on_target = True
    job["on_target"] = on_target
    job["seniority_label"] = label
    job["seniority_rank"] = rank
    job["over_level"] = rank > cap
    return job


def apply_for_me_view(jobs: list[dict], profile: dict, override_rank: int | None = None) -> list[dict]:
    """Hide off-target jobs, sink over-level jobs, sort for the For-me board.

    Sort key: in-level before over-level, then freshest (posted_at) first, then
    match_score descending. `override_rank` (None = auto) replaces the cap.
    """
    cap = override_rank if override_rank is not None else profile.get("max_seniority_rank", MID_RANK)
    classified = [classify_job(j, profile, cap) for j in jobs]
    kept = [j for j in classified if j["on_target"]]
    kept.sort(key=lambda j: (1 if j["over_level"] else 0,          # in-level group first
                             _Desc(j.get("posted_at")),            # then freshest
                             -(j.get("match_score") or 0)))        # then Fit
    return kept
