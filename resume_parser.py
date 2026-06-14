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
    "summary": "", "skills": "", "experiences": [], "education": [],
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

# A header line is short (<=4 words) and matches one of the above patterns.
def _is_section_header(line: str) -> str | None:
    """Return section key ('summary'|'experience'|'education'|'skills') or None."""
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
_ROLE_SEP_RE = re.compile(r"(?:\s+[-–|]\s+|\s*,\s*)")


def _is_date_only_line(line: str) -> bool:
    """True if line is purely a date range (no role/company text beside the date)."""
    stripped = line.strip()
    # Remove the date range; if nothing meaningful remains, it's a date-only line
    rest = _DATE_RANGE_RE.sub("", stripped).strip(" |–-,•")
    if _DATE_RANGE_RE.search(stripped) and len(rest) < 8:
        return True
    return False


# A Role - Company line: no date, has a separator between two non-empty parts
_ROLE_COMPANY_RE = re.compile(r".+\s+[-–|]\s+.+")


def _is_role_company_line(line: str) -> bool:
    """True if line looks like 'Role - Company' without a date."""
    stripped = line.strip()
    if not stripped or _is_section_header(stripped):
        return False
    if _DATE_RE.search(stripped):
        return False
    return bool(_ROLE_COMPANY_RE.match(stripped))


def _is_entry_header(line: str) -> bool:
    """True if line looks like an experience entry header (has dates or Role-Company pattern)."""
    stripped = line.strip()
    if not stripped:
        return False
    if _is_section_header(stripped):
        return False
    # Has a year or date range
    if _DATE_RE.search(stripped):
        return True
    # Role – Company with no date (two-line format)
    if _is_role_company_line(stripped):
        return True
    return False


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

    return entries


# ---------------------------------------------------------------------------
# Education parsing
# ---------------------------------------------------------------------------

_GPA_RE = re.compile(r"\b(?:GPA|WAM|CGPA)\b[\s:]*([0-9.]+(?:/[0-9.]+)?)", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")


def _extract_education(block: list[str]) -> list[dict]:
    """Parse an EDUCATION block into a list of education dicts."""
    entries: list[dict] = []
    current: dict | None = None

    for line in block:
        stripped = line.strip()
        if not stripped:
            continue

        gpa_m = _GPA_RE.search(stripped)
        year_m = _YEAR_RE.search(stripped)

        # If this line looks like a degree header (contains degree keywords or is a new entry)
        degree_keywords = re.compile(
            r"\b(Bachelor|Master|Doctor|PhD|MBA|BEng|BSc|BE|BA|BComm|BCom|MCom|MEng|MSc|"
            r"Diploma|Certificate|Associate|Honours|Hons|Grad\s?Dip|PostGrad|Graduate)\b",
            re.IGNORECASE,
        )
        is_degree_line = bool(degree_keywords.search(stripped))

        if is_degree_line:
            if current is not None:
                entries.append(current)
            current = {"degree": stripped, "school": "", "year": "", "gpa": ""}
            if year_m:
                current["year"] = year_m.group()
            if gpa_m:
                current["gpa"] = gpa_m.group(1)
        elif current is not None:
            # Subsequent lines: school name, year, gpa
            if not current["school"] and not year_m and not gpa_m:
                current["school"] = stripped
            if year_m and not current["year"]:
                current["year"] = year_m.group()
            if gpa_m and not current["gpa"]:
                current["gpa"] = gpa_m.group(1)
        else:
            # No current entry yet — start a bare entry
            current = {"degree": stripped, "school": "", "year": "", "gpa": ""}
            if year_m:
                current["year"] = year_m.group()
            if gpa_m:
                current["gpa"] = gpa_m.group(1)

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
    skills_block = sections.get("skills", [])
    skill_items: list[str] = []
    if skills_block:
        skill_items = _extract_skills_from_block(skills_block)

    # Union with ontology match_text so skills populate even with no SKILLS header
    try:
        from ontology import match_text as _onto_match
        onto_skills = _onto_match(text)
        # Merge: ontology labels that aren't already in the explicit list
        existing_lower = {s.lower() for s in skill_items}
        for s in sorted(onto_skills):
            if s.lower() not in existing_lower:
                skill_items.append(s)
                existing_lower.add(s.lower())
    except Exception:
        pass

    skills_str = ", ".join(s for s in skill_items if s)

    # --- Experiences ---
    exp_block = sections.get("experience", [])
    experiences = _extract_experiences(exp_block)

    # --- Education ---
    edu_block = sections.get("education", [])
    education = _extract_education(edu_block)

    det: dict = {
        "name": contact["name"],
        "email": contact["email"],
        "phone": contact["phone"],
        "location": contact["location"],
        "linkedin": contact["linkedin"],
        "summary": summary,
        "skills": skills_str,
        "experiences": experiences,
        "education": education,
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
