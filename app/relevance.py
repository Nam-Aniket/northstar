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


# Role-type words that appear across many domains. They show up in the user's own
# targets too (e.g. "Data Engineer"), but on their own they must NOT qualify an
# off-domain role like "Software Engineer" as on-target. So they are excluded from
# the relevance ANCHOR set. (Seniority/filler are already stripped by title_tokens.)
_GENERIC_ROLE_WORDS = {
    "engineer", "engineering", "developer", "programmer",
    "consultant", "specialist", "architect", "advisor", "technician",
}


def anchor_tokens(title: str) -> set[str]:
    """Domain-anchor tokens of a title: title_tokens minus generic role words.

    This is what relevance matching compares, so 'Software Engineer' ({software})
    can't match a 'Data Engineer' target on the shared generic word 'engineer'
    alone — it must share a real domain anchor (data / analyst / scientist / ...).
    """
    return title_tokens(title) - _GENERIC_ROLE_WORDS


# Stated minimum years-of-experience. Only "N+ years" or "minimum/at least N
# years" count; bare "5 years" is descriptive prose, not a stated requirement.
_YEARS_PLUS_RE = re.compile(r"\b(\d{1,2})\s*\+\s*years?\b", re.IGNORECASE)
_YEARS_MIN_RE = re.compile(
    r"\b(?:minimum|min\.?|at least)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*years?\b",
    re.IGNORECASE,
)


def required_years(text: str) -> int | None:
    """Highest stated minimum years-of-experience in the text, or None."""
    if not text:
        return None
    yrs = [int(m.group(1)) for m in _YEARS_PLUS_RE.finditer(text)]
    yrs += [int(m.group(1)) for m in _YEARS_MIN_RE.finditer(text)]
    return max(yrs) if yrs else None


def target_profile(con) -> dict:
    """Derive the user's relevance profile from tracked_positions.

    Returns {title_tokens, tracked_titles, max_seniority_rank}.
    Empty title_tokens signals 'unconfigured' -> the view hides nothing.
    """
    rows = con.execute("SELECT title FROM tracked_positions").fetchall()
    titles = [r["title"] for r in rows if r["title"]]
    tokens: set[str] = set()
    anchors: set[str] = set()
    max_rank = MID_RANK
    for t in titles:
        tokens |= title_tokens(t)
        anchors |= anchor_tokens(t)
        max_rank = max(max_rank, parse_seniority(t)[1])
    return {
        "title_tokens": tokens,
        "anchor_tokens": anchors,
        "tracked_titles": titles,
        "max_seniority_rank": max_rank,
    }


def classify_job(job: dict, profile: dict, cap: int, cand_years: int | None = None) -> dict:
    """Merge relevance + seniority + experience flags onto a shaped job and return it.

    on_target: title shares >=1 domain ANCHOR token with the tracked titles.
               If the profile is unconfigured (no tokens), everything is on_target.
    over_level: seniority rank exceeds `cap`.
    over_experience: the JD asks for more years than `cand_years` (None -> never).
    """
    title = job.get("role_title", "")
    label, rank = parse_seniority(title)
    # Match on domain anchors (generic role words excluded). Fall back to deriving
    # anchors from title_tokens when a caller passes a profile without the key.
    targets = profile.get("anchor_tokens")
    if targets is None:
        targets = profile.get("title_tokens", set()) - _GENERIC_ROLE_WORDS
    if targets:
        on_target = bool(anchor_tokens(title) & targets)
    elif profile.get("title_tokens"):
        # Tracked titles were ALL generic role words (rare) -> raw token overlap.
        on_target = bool(title_tokens(title) & profile["title_tokens"])
    else:
        on_target = True  # unconfigured profile hides nothing
    req_y = required_years(job.get("job_text") or "")
    job["on_target"] = on_target
    job["seniority_label"] = label
    job["seniority_rank"] = rank
    job["over_level"] = rank > cap
    job["req_years"] = req_y
    job["over_experience"] = bool(
        cand_years is not None and req_y is not None and req_y > cand_years)
    return job


def apply_for_me_view(jobs: list[dict], profile: dict, override_rank: int | None = None,
                      cand_years: int | None = None) -> list[dict]:
    """Hide off-target jobs, sink over-qualified jobs, sort for the For-me board.

    Sort key: well-matched before over-level/over-experience, then freshest
    (posted_at) first, then match_score descending. `override_rank` (None = auto)
    replaces the seniority cap; `cand_years` enables the experience flag.
    """
    cap = override_rank if override_rank is not None else profile.get("max_seniority_rank", MID_RANK)
    classified = [classify_job(j, profile, cap, cand_years) for j in jobs]
    kept = [j for j in classified if j["on_target"]]
    kept.sort(key=lambda j: (1 if (j["over_level"] or j["over_experience"]) else 0,  # in-reach first
                             _Desc(j.get("posted_at")),            # then freshest
                             -(j.get("match_score") or 0)))        # then Fit
    return kept
