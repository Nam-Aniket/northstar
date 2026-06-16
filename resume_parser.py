"""resume_parser.py — Parse résumé text into the Builder ns-resume schema.

Entry point:
    parse_resume(text: str, use_llm: bool = False) -> dict

Returned schema (matches Builder collect()):
    {
        name:        str,
        email:       str,
        phone:       str,
        location:    str,
        linkedin:    str,
        summary:     str,
        skills:      str,   # comma-joined string
        experiences: [{ role, company, dates, bullets: [...] }],
        education:   [{ degree, school, year, gpa }],
    }

Stdlib only (no third-party imports in the deterministic path).
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY: dict = {
    "name": "", "email": "", "phone": "", "location": "", "linkedin": "",
    "summary": "", "skills": "", "inferred_skills": "",
    "experiences": [], "education": [], "projects": [],
}


def _empty() -> dict:
    return {k: (list(v) if isinstance(v, list) else v) for k, v in _EMPTY.items()}


# ---------------------------------------------------------------------------
# Section-header detection
# ---------------------------------------------------------------------------

_SUMMARY_RE = re.compile(
    r"^(SUMMARY|PROFILE|OBJECTIVE|ABOUT|PROFESSIONAL SUMMARY|CAREER SUMMARY)$",
    re.IGNORECASE,
)
_EXPERIENCE_RE = re.compile(
    r"^(EXPERIENCE|EMPLOYMENT|WORK HISTORY|PROFESSIONAL EXPERIENCE|WORK EXPERIENCE|CAREER HISTORY)$",
    re.IGNORECASE,
)
_EDUCATION_RE = re.compile(
    r"^(EDUCATION|ACADEMIC|ACADEMIC BACKGROUND|EDUCATIONAL BACKGROUND|QUALIFICATIONS)$",
    re.IGNORECASE,
)
_SKILLS_RE = re.compile(
    r"^(SKILLS|TECHNICAL SKILLS|COMPETENCIES|KEY SKILLS|CORE SKILLS|SKILLS & TOOLS|TOOLS & TECHNOLOGIES)$",
    re.IGNORECASE,
)
_PROJECTS_RE = re.compile(
    r"^(PROJECTS|PERSONAL PROJECTS|KEY PROJECTS|SELECTED PROJECTS|SIDE PROJECTS)$",
    re.IGNORECASE,
)
# Any other recognised section heading. Recognising these gives a hard boundary
# so their content cannot leak into education/experience (the school="PROJECTS" bug).
_OTHER_SECTION_RE = re.compile(
    r"^(CERTIFICATIONS?|CERTIFICATES?|AWARDS?|HON(?:ORS|OURS)|ACHIEVEMENTS?|"
    r"PUBLICATIONS?|VOLUNTEER(?:ING)?|INTERESTS|HOBBIES|REFERENCES|LANGUAGES|"
    r"ACTIVITIES|MEMBERSHIPS?|AFFILIATIONS?)$",
    re.IGNORECASE,
)

# A header line is short (<=4 words) and matches one of the above patterns.
def _is_section_header(line: str) -> str | None:
    """Return a section key ('summary'|'experience'|'education'|'skills'|
    'projects'|'other') or None."""
    stripped = line.strip()
    if not stripped or len(stripped.split()) > 4:
        return None
    if _SUMMARY_RE.match(stripped):
        return "summary"
    if _EXPERIENCE_RE.match(stripped):
        return "experience"
    if _EDUCATION_RE.match(stripped):
        return "education"
    if _SKILLS_RE.match(stripped):
        return "skills"
    if _PROJECTS_RE.match(stripped):
        return "projects"
    if _OTHER_SECTION_RE.match(stripped):
        return "other"
    return None


# ---------------------------------------------------------------------------
# Contact extraction (first ~15 lines)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    # AU landline/mobile: +61 X XXXX XXXX  or  0X XXXX XXXX  or  +61 4XX XXX XXX
    r"(?:\+61|0061)[\s\-]?[2-9][\s\-]?\d{4}[\s\-]?\d{4}"
    r"|(?:\+61|0061)[\s\-]?4\d{2}[\s\-]?\d{3}[\s\-]?\d{3}"
    r"|0[2-9][\s\-]?\d{4}[\s\-]?\d{4}"
    r"|04\d{2}[\s\-]?\d{3}[\s\-]?\d{3}"
    # US/CA: (555) 123-4567 / 555-123-4567
    r"|(?:\+?1[\s\-]?)?\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4}"
)
_LINKEDIN_RE = re.compile(r"(?:https?://)?(?:www\.)?linkedin\.com/in/[A-Za-z0-9_\-]+/?", re.IGNORECASE)
# City, ST  or  City, Country  pattern
_LOCATION_RE = re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)*),\s*([A-Z]{2,3}|[A-Z][a-z]+(?: [A-Z][a-z]+)*)\b")


def _extract_contact(lines: list[str]) -> dict:
    """Extract email, phone, linkedin, location, name from the first 15 lines."""
    result: dict = {"name": "", "email": "", "phone": "", "location": "", "linkedin": ""}
    head = lines[:15]
    head_text = "\n".join(head)

    m = _EMAIL_RE.search(head_text)
    if m:
        result["email"] = m.group()

    m = _PHONE_RE.search(head_text)
    if m:
        result["phone"] = m.group().strip()

    m = _LINKEDIN_RE.search(head_text)
    if m:
        result["linkedin"] = m.group().strip()

    m = _LOCATION_RE.search(head_text)
    if m:
        result["location"] = m.group().strip()

    # Name = first non-empty line that:
    #   - has <= 5 words
    #   - contains no digits
    #   - does not look like a section header
    #   - is title-case-ish (first char uppercase)
    for line in head:
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(r"\d", stripped):
            continue
        if _EMAIL_RE.search(stripped) or _PHONE_RE.search(stripped) or _LINKEDIN_RE.search(stripped):
            continue
        words = stripped.split()
        if len(words) > 5:
            continue
        if _is_section_header(stripped):
            continue
        if stripped[0].isupper():
            result["name"] = stripped
            break

    return result


# ---------------------------------------------------------------------------
# Slice text into section blocks
# ---------------------------------------------------------------------------

def _slice_sections(lines: list[str]) -> dict[str, list[str]]:
    """Return {section_key: [lines]} mapping. Unlabelled leading lines -> 'header'."""
    sections: dict[str, list[str]] = {"header": []}
    current = "header"
    for line in lines:
        key = _is_section_header(line)
        if key:
            current = key
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return sections


# ---------------------------------------------------------------------------
# Skills extraction
# ---------------------------------------------------------------------------

_BULLET_RE = re.compile(r"^[•‣◦⁃∙\-\*–—•]\s*")


def _extract_skills_from_block(block: list[str]) -> list[str]:
    """Split a SKILLS block on commas, bullets, newlines, pipes."""
    raw_text = " | ".join(block)
    # Normalise bullets/pipes to commas then split
    normalised = re.sub(r"[|•\-\*–‣\n]", ",", raw_text)
    # Strip leading bullet chars from each item
    items = [_BULLET_RE.sub("", s).strip() for s in normalised.split(",")]
    return [s for s in items if len(s) > 1]


# ---------------------------------------------------------------------------
# Experience parsing
# ---------------------------------------------------------------------------

# A date range contains a 4-digit year or common date tokens
_DATE_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b|\b\d{4}\b|Present|Current|Now",
    re.IGNORECASE,
)
_DATE_RANGE_RE = re.compile(
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Q[1-4])[\w\s,]*\d{0,4}\s*(?:–|-|to)\s*"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|Q[1-4]|Present|Current|Now)[\w\s,]*\d{0,4}"
    r"|\b\d{4}\s*(?:–|-|to)\s*(?:\d{4}|Present|Current|Now)\b",
    re.IGNORECASE,
)
# Role – Company  or  Role, Company  in the first portion of a line
_ROLE_SEP_RE = re.compile(r"(?:\s+[-–|·•∙│]\s+|\s*,\s*)")


def _is_date_only_line(line: str) -> bool:
    """True if line is purely a date range (no role/company text beside the date)."""
    stripped = line.strip()
    # Remove the date range; if nothing meaningful remains, it's a date-only line
    rest = _DATE_RANGE_RE.sub("", stripped).strip(" |–-,•")
    if _DATE_RANGE_RE.search(stripped) and len(rest) < 8:
        return True
    return False


# Role/company separator: a spaced dash/pipe or the word "at". NOT a bare comma
# (prose has commas) and NOT any dash (hyphenated words like "cross-functional").
_HEADER_SEP_RE = re.compile(r"\s+[-–|·•∙│]\s+|\s+\bat\b\s+")
# Common bullet-opener verbs (past/gerund). A header's company side never starts
# with one of these; a line like "...| assigned Jira tickets..." is prose.
_VERB_LEXICON = frozenset({
    "mentored", "assigned", "reviewed", "built", "led", "managed", "developed",
    "designed", "created", "analysed", "analyzed", "reduced", "increased",
    "improved", "delivered", "implemented", "coordinated", "supported",
    "maintained", "automated", "unblocked", "drove", "owned", "shipped",
    "launched", "migrated", "optimised", "optimized", "engineered", "deployed",
    "collaborated", "partnered", "presented", "researched", "tested", "wrote",
    "processed", "validated", "reconciled", "configured", "trained", "achieved",
})


def _titleish(s: str) -> bool:
    """True if a phrase looks like a proper name/title (most words capitalised),
    not a prose clause."""
    words = s.split()
    if not words:
        return False
    caps = sum(1 for w in words if w[:1].isupper() or not w[:1].isalpha())
    return caps >= max(1, len(words) // 2)


def _is_entry_header(line: str) -> bool:
    """True only when a line is SHAPED like an experience header (Role | Company [| Dates]),
    not a bullet or a prose sentence. A real date range or two title-ish sides is required."""
    stripped = line.strip()
    if not stripped or _is_section_header(stripped):
        return False
    if _BULLET_RE.match(stripped):
        return False  # explicit bullet marker -> never a header
    if _is_date_only_line(stripped):
        return True   # two-line layout: a date line under a role/company line
    has_daterange = bool(_DATE_RANGE_RE.search(stripped))
    # Reject sentence/prose shapes.
    if stripped.endswith((".", ":", ";")):
        return False
    if stripped.count(",") >= 2:
        return False
    if len(stripped.split()) > 14:
        return False
    if not _HEADER_SEP_RE.search(stripped):
        return False
    # Split role / company once, after removing any date span.
    core = _DATE_RANGE_RE.sub("", stripped).strip(" -–|,")
    parts = _HEADER_SEP_RE.split(core, maxsplit=1)
    left = parts[0].strip() if parts else ""
    right = parts[1].strip() if len(parts) > 1 else ""
    if not left or not right:
        return has_daterange  # one-sided header carrying a date is acceptable
    if len(left.split()) > 6 or len(right.split()) > 6:
        return False
    if right.split()[0].lower().strip(".,") in _VERB_LEXICON:
        return False
    # Without a date, require both sides to look like proper names/titles.
    if not has_daterange and not (_titleish(left) and _titleish(right)):
        return False
    return True


def _parse_entry_header(line: str) -> tuple[str, str, str]:
    """Return (role, company, dates) from an entry header line."""
    stripped = line.strip()
    # Extract date portion first
    dates = ""
    m = _DATE_RANGE_RE.search(stripped)
    if m:
        dates = m.group().strip()
        rest = (stripped[:m.start()] + " " + stripped[m.end():]).strip()
    else:
        # Find any lone year
        ym = re.search(r"\b\d{4}\b", stripped)
        if ym:
            dates = ym.group()
            rest = (stripped[:ym.start()] + " " + stripped[ym.end():]).strip()
        else:
            rest = stripped

    rest = rest.strip(" |–-,")
    # Split on separator to get role / company
    parts = _ROLE_SEP_RE.split(rest, maxsplit=1)
    role = parts[0].strip() if parts else ""
    company = parts[1].strip() if len(parts) > 1 else ""
    return role, company, dates


def _is_bullet(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    return bool(_BULLET_RE.match(stripped)) or stripped[0].isupper()


def _looks_like_company_line(text: str) -> bool:
    """True if a line looks like a 'Company, City[, Country]' header line rather
    than a résumé bullet. Used to recover the company when a comma-separated
    company line (no header separator) was absorbed as a title entry's first
    'bullet'. Deliberately conservative: real bullets open with an action verb
    and tend to be longer, so we require a title-ish, comma-bearing, short,
    verb-free, non-sentence line."""
    words = text.split()
    if not words or len(words) > 6:
        return False
    if "," not in text:
        return False
    if text.rstrip().endswith((".", ":", ";")):
        return False
    if words[0].lower().strip(".,") in _VERB_LEXICON:
        return False
    return _titleish(text)


def _merge_two_line_headers(entries: list[dict]) -> list[dict]:
    """Merge a 'title-line then company-line' job split into one entry.

    Some résumés put the job title + dates on one line and the company +
    location on the next, e.g.

        Data Analytics Intern                 Feb 2026 - Present
        Cultural Infusion, Melbourne, Australia
          - bullet ...

    Left split, the bullet-less title entry later gets a 'Contributed to {role}.'
    filler bullet downstream. We collapse the pair into one entry. The company
    line reaches us one of two ways depending on its separator:

      Mode 1 - the company line carried a header separator (pipe/dash/middot/
      "at"), so it parsed as its OWN entry B (role + company, no dates) and the
      real bullets attached to B. We fold B into the preceding title entry A.

      Mode 2 - the company line was plain "Company, City" (no header separator),
      so the header detector did NOT recognise it and it was absorbed as A's
      first "bullet". We promote that leading line to A's company.

    Trigger in both modes: a title entry A that has dates, no company, and (Mode
    1) no bullets / (Mode 2) a leading company-shaped bullet.

    Known limitation: an undated company-line is structurally indistinguishable
    from a genuinely separate undated next job. So a dated, company-less,
    bullet-less role immediately followed by an undated separate job can be
    mis-merged. This is inherent to the two-line layout; the user reviews and can
    correct the parsed result in the editor.
    """
    out: list[dict] = []
    i = 0
    n = len(entries)
    while i < n:
        a = entries[i]
        b = entries[i + 1] if i + 1 < n else None
        # Mode 1: the company line parsed as its own (undated) entry B.
        if (b is not None
                and a["dates"] and not a["company"] and not a["bullets"]
                and not b["dates"] and b["role"]):
            company = f'{b["role"]}, {b["company"]}' if b["company"] else b["role"]
            out.append({
                "role": a["role"],
                "company": company,
                "dates": a["dates"],
                "bullets": b["bullets"],
            })
            i += 2
            continue
        # Mode 2: the company line was absorbed as A's leading "bullet".
        if (a["dates"] and not a["company"] and a["bullets"]
                and _looks_like_company_line(a["bullets"][0])):
            out.append({
                "role": a["role"],
                "company": a["bullets"][0],
                "dates": a["dates"],
                "bullets": a["bullets"][1:],
            })
            i += 1
            continue
        out.append(a)
        i += 1
    return out


def _extract_experiences(block: list[str]) -> list[dict]:
    """Parse an EXPERIENCE block into a list of experience dicts.

    Handles two common layouts:
      1. Single-line:  Role - Company   Jan 2021 – Present
      2. Two-line:     Role - Company
                       Jan 2021 – Present
    """
    entries: list[dict] = []
    current: dict | None = None

    for line in block:
        stripped = line.strip()
        if not stripped:
            continue

        if _is_entry_header(stripped):
            if _is_date_only_line(stripped) and current is not None and not current["dates"]:
                # Two-line format: date line following a role-company line
                m = _DATE_RANGE_RE.search(stripped)
                current["dates"] = m.group().strip() if m else stripped
                continue

            # New entry
            if current is not None:
                entries.append(current)
            role, company, dates = _parse_entry_header(stripped)
            current = {"role": role, "company": company, "dates": dates, "bullets": []}
        elif current is not None:
            clean = _BULLET_RE.sub("", stripped).strip()
            if clean:
                current["bullets"].append(clean)
        # Lines before any entry header are ignored

    if current is not None:
        entries.append(current)

    return _merge_two_line_headers(entries)


def _extract_projects(block: list[str]) -> list[dict]:
    """Parse a PROJECTS block into [{name, bullets}]. A short, non-bulleted,
    non-prose line starts a new project; bullets and prose attach to it."""
    projects: list[dict] = []
    current: dict | None = None
    for line in block:
        stripped = line.strip()
        if not stripped:
            continue
        if _BULLET_RE.match(stripped):
            if current is not None:
                current["bullets"].append(_BULLET_RE.sub("", stripped).strip())
            continue
        # A project title: short and not a sentence. Otherwise treat as a bullet.
        is_title = len(stripped.split()) <= 8 and not stripped.endswith((".", ":", ";"))
        if is_title:
            if current is not None:
                projects.append(current)
            current = {"name": stripped, "bullets": []}
        elif current is not None:
            current["bullets"].append(stripped)
    if current is not None:
        projects.append(current)
    return projects


# ---------------------------------------------------------------------------
# Education parsing
# ---------------------------------------------------------------------------

_GPA_RE = re.compile(r"\b(GPA|WAM|CGPA)\b[\s:]*([0-9.]+(?:/[0-9.]+)?)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
# Unlabeled score, e.g. "79/100" or "8.7/10" (years like 2019/2020 excluded by the
# 1-3 digit numerator). Used only when no GPA/WAM/CGPA label is present.
_SCORE_RE = re.compile(r"\b(\d{1,3}(?:\.\d+)?)\s*/\s*(100|10|7|4)\b")
# Field separators inside one résumé line: comma, middot variants, pipe, tab. Real
# résumés use any of these interchangeably; splitting on all of them keeps
# degree/school/score from collapsing into one segment.
_FIELD_SEP_RE = re.compile(r"\s*[,·•∙│|]\s*|\t+")

# Education field separators — used to split a single combined line into
# degree / school / dates cleanly so the date is never duplicated at render.
_DEGREE_KEYWORDS = re.compile(
    r"\b(Bachelor|Master|Doctor|PhD|MBA|BEng|BSc|BE|BA|BComm|BCom|MCom|MEng|MSc|"
    r"Diploma|Certificate|Associate|Honours|Hons|Grad\s?Dip|PostGrad|Graduate)\b",
    re.IGNORECASE,
)
_INSTITUTION_RE = re.compile(
    r"\b(University|College|Institute|School|TAFE|Polytechnic|Academy)\b", re.IGNORECASE
)
_MONTH_RE = re.compile(
    r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b", re.IGNORECASE
)


def _normalize_edu_dates(text: str) -> str:
    """Return a clean 'YYYY - YYYY' (or single 'YYYY', or 'YYYY - Present') from a
    date span in text, else ''. Strips months so the render shows years only."""
    span = _DATE_RANGE_RE.search(text)
    chunk = span.group() if span else text
    years = re.findall(r"(?:19|20)\d{2}", chunk)
    has_present = bool(re.search(r"present|current|now", chunk, re.IGNORECASE))
    if len(years) >= 2:
        return f"{years[0]} - {years[-1]}"
    if years and (has_present or span):
        return f"{years[0]} - Present" if has_present else years[0]
    if years:
        return years[0]
    return "Present" if (span and has_present) else ""


def _score_from(text: str) -> tuple[str, str]:
    """Return (gpa, label) from a line: labeled GPA/WAM/CGPA first, else a bare
    score like 79/100 (no label). ('', '') if none."""
    gm = _GPA_RE.search(text)
    if gm:
        return gm.group(2), gm.group(1).upper()
    sm = _SCORE_RE.search(text)
    if sm:
        return sm.group(0).replace(" ", ""), ""  # unlabeled score
    return "", ""


def _split_education_line(line: str) -> tuple[str, str, str, str, str]:
    """Split a combined education line into (degree, school, dates, gpa, gpa_label).
    The degree string never contains the school, dates, or score."""
    gpa, gpa_label = _score_from(line)
    work = _GPA_RE.sub("", line)
    work = _SCORE_RE.sub("", work).strip()
    dates = _normalize_edu_dates(work)
    # Remove date tokens so they don't pollute degree/school segments.
    work = _DATE_RANGE_RE.sub("", work)
    work = _MONTH_RE.sub("", work)
    work = re.sub(r"\b(19|20)\d{2}\b", "", work)
    segments = [s.strip(" -–|·•∙") for s in _FIELD_SEP_RE.split(work)]
    segments = [s for s in segments if s]
    degree = next((s for s in segments if _DEGREE_KEYWORDS.search(s)), "")
    # school must be a DIFFERENT segment than degree (avoids degree==school dupes).
    school = next((s for s in segments if s != degree and _INSTITUTION_RE.search(s)), "")
    leftovers = [s for s in segments if s not in (degree, school)]
    if degree and leftovers:
        degree = degree + ", " + ", ".join(leftovers)  # e.g. a major: "..., Computer Science"
    elif not degree:
        degree = segments[0] if segments else ""
    return degree, school, dates, gpa, gpa_label


def _extract_education(block: list[str]) -> list[dict]:
    """Parse an EDUCATION block into a list of education dicts."""
    entries: list[dict] = []
    current: dict | None = None

    def _new_entry(degree, school, dates, gpa, gpa_label):
        return {"degree": degree, "school": school, "year": dates,
                "gpa": gpa, "gpa_label": gpa_label}

    for line in block:
        stripped = line.strip()
        if not stripped:
            continue

        is_degree_line = bool(_DEGREE_KEYWORDS.search(stripped))

        if is_degree_line:
            if current is not None:
                entries.append(current)
            current = _new_entry(*_split_education_line(stripped))
        elif current is not None:
            # Subsequent lines of a two-line layout: school, then dates, then score.
            gpa, gpa_label = _score_from(stripped)
            dates = _normalize_edu_dates(stripped)
            if gpa and not current["gpa"]:
                current["gpa"], current["gpa_label"] = gpa, gpa_label
            elif dates and not current["year"]:
                current["year"] = dates
            elif not current["school"] and not dates:
                current["school"] = stripped
        else:
            # No current entry yet — start a bare entry from the line.
            degree, school, dates, gpa, gpa_label = _split_education_line(stripped)
            current = _new_entry(degree or stripped, school, dates, gpa, gpa_label)

    if current is not None:
        entries.append(current)

    return entries


# ---------------------------------------------------------------------------
# Truthfulness guard for LLM bullets
# ---------------------------------------------------------------------------

def _token_overlap(bullet: str, source: str) -> float:
    """Fraction of bullet tokens present in source text (case-insensitive)."""
    b_tokens = set(re.findall(r"[a-z0-9]+", bullet.lower()))
    if not b_tokens:
        return 1.0
    s_tokens = set(re.findall(r"[a-z0-9]+", source.lower()))
    overlap = b_tokens & s_tokens
    return len(overlap) / len(b_tokens)


def _guard_bullets(experiences: list[dict], source_text: str, threshold: float = 0.80) -> list[dict]:
    """Drop any bullet whose token-overlap with source_text is below threshold."""
    result = []
    for exp in experiences:
        safe_bullets = [
            b for b in (exp.get("bullets") or [])
            if _token_overlap(b, source_text) >= threshold
        ]
        result.append({**exp, "bullets": safe_bullets})
    return result


# ---------------------------------------------------------------------------
# LLM overlay
# ---------------------------------------------------------------------------

_PARSER_SYSTEM = (
    "You are a résumé parser. "
    'Return JSON EXACTLY matching this schema: '
    '{"name":"","email":"","phone":"","location":"","linkedin":"",'
    '"summary":"","skills":"","experiences":[{"role":"","company":"","dates":"",'
    '"bullets":[]}],"education":[{"degree":"","school":"","year":"","gpa":""}]}. '
    "Use ONLY text present in the résumé — never invent employers, dates, numbers, "
    "skills, or bullets; copy bullet wording verbatim, do not rewrite or add metrics. "
    "skills must be a comma-separated string. Absent field -> '' or []. "
    "Output ONLY the JSON object."
)


def _llm_parse(text: str) -> dict | None:
    """Call the LLM and return a parsed dict, or None on any failure."""
    try:
        import urllib.request
        import urllib.error
        from dotenv_loader import load_env
        load_env()
    except Exception:
        return None

    api_key = os.environ.get("LLM_API_KEY", "")
    if not api_key:
        return None

    base_url = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    model = os.environ.get("LLM_MODEL", "deepseek-chat")

    messages = [
        {"role": "system", "content": _PARSER_SYSTEM},
        {"role": "user", "content": text[:12000]},
    ]
    payload = json.dumps({
        "model": model,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 2000,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        raw = data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None

    # Extract first {...} via brace matching
    start = raw.find("{")
    if start == -1:
        return None
    depth = 0
    end = -1
    for i, ch in enumerate(raw[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    if end == -1:
        return None

    try:
        parsed = json.loads(raw[start:end])
    except Exception:
        return None

    # Validate / coerce to schema
    coerced: dict = _empty()
    for field in ("name", "email", "phone", "location", "linkedin", "summary"):
        v = parsed.get(field, "")
        coerced[field] = str(v) if v else ""

    # skills must be a comma-string
    skills_raw = parsed.get("skills", "")
    if isinstance(skills_raw, list):
        coerced["skills"] = ", ".join(str(s) for s in skills_raw)
    else:
        coerced["skills"] = str(skills_raw) if skills_raw else ""

    # experiences
    exps = parsed.get("experiences", [])
    if isinstance(exps, list):
        coerced["experiences"] = [
            {
                "role": str(e.get("role", "")),
                "company": str(e.get("company", "")),
                "dates": str(e.get("dates", "")),
                "bullets": [str(b) for b in (e.get("bullets") or []) if b],
            }
            for e in exps if isinstance(e, dict)
        ]

    # education
    edus = parsed.get("education", [])
    if isinstance(edus, list):
        coerced["education"] = [
            {
                "degree": str(e.get("degree", "")),
                "school": str(e.get("school", "")),
                "year": str(e.get("year", "")),
                "gpa": str(e.get("gpa", "")),
            }
            for e in edus if isinstance(e, dict)
        ]

    return coerced


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_resume(text: str, use_llm: bool = False) -> dict:
    """Parse résumé plain text into the Builder ns-resume schema dict.

    Always runs a deterministic baseline. When use_llm=True and LLM_API_KEY is
    set, runs an LLM overlay and merges: deterministic contact/skills/summary
    as base; LLM experiences/education only when non-empty.

    Never raises — returns a partial (possibly empty) dict on any error.
    """
    try:
        return _parse_resume_inner(text, use_llm)
    except Exception:
        return _empty()


def _parse_resume_inner(text: str, use_llm: bool) -> dict:
    if not text or not text.strip():
        return _empty()

    lines = text.splitlines()

    # --- Contact block (first 15 lines) ---
    contact = _extract_contact(lines)

    # --- Section slicing ---
    sections = _slice_sections(lines)

    # --- Summary ---
    summary_block = sections.get("summary", [])
    summary = " ".join(l.strip() for l in summary_block if l.strip())

    # --- Skills ---
    # Declared skills (from the SKILLS section) are the only ones rendered on the
    # résumé / written to the profile — they are what the candidate actually claims.
    # Ontology-inferred skills (matched anywhere in the résumé) are kept SEPARATE
    # for the matcher, never asserted as declared, to avoid misrepresentation.
    skills_block = sections.get("skills", [])
    declared_items = _extract_skills_from_block(skills_block) if skills_block else []

    inferred_items: list[str] = []
    try:
        from ontology import match_text as _onto_match
        declared_lower = {s.lower() for s in declared_items}
        for s in sorted(_onto_match(text)):
            if s.lower() not in declared_lower:
                inferred_items.append(s)
    except Exception:
        pass

    if declared_items:
        skill_items = declared_items
    else:
        # No SKILLS section: fall back to inferred so skills still populate (back-compat).
        skill_items, inferred_items = inferred_items, []

    skills_str = ", ".join(s for s in skill_items if s)
    inferred_str = ", ".join(inferred_items)

    # --- Experiences ---
    exp_block = sections.get("experience", [])
    experiences = _extract_experiences(exp_block)

    # --- Education ---
    edu_block = sections.get("education", [])
    education = _extract_education(edu_block)

    # --- Projects ---
    projects = _extract_projects(sections.get("projects", []))

    det: dict = {
        "name": contact["name"],
        "email": contact["email"],
        "phone": contact["phone"],
        "location": contact["location"],
        "linkedin": contact["linkedin"],
        "summary": summary,
        "skills": skills_str,
        "inferred_skills": inferred_str,
        "experiences": experiences,
        "education": education,
        "projects": projects,
    }

    # --- Optional LLM overlay ---
    if use_llm and os.environ.get("LLM_API_KEY"):
        llm = _llm_parse(text)
        if llm is not None:
            # Deterministic wins for contact/skills/summary
            for field in ("name", "email", "phone", "location", "linkedin", "summary", "skills"):
                if det[field]:
                    llm[field] = det[field]
            # LLM wins for experiences/education only when non-empty
            if llm.get("experiences"):
                llm["experiences"] = _guard_bullets(llm["experiences"], text)
            else:
                llm["experiences"] = det["experiences"]
            if not llm.get("education"):
                llm["education"] = det["education"]
            return llm

    return det


# ---------------------------------------------------------------------------
# Storage helper
# ---------------------------------------------------------------------------

def save_parsed(parsed: dict, uploads_dir: Path) -> None:
    """Atomically write parsed dict to uploads_dir/parsed_resume.json."""
    uploads_dir.mkdir(exist_ok=True)
    dest = uploads_dir / "parsed_resume.json"
    tmp = str(dest) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(parsed, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(dest))
