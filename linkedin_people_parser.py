"""LinkedIn People tab parser and email generation engine.

Pure parser with no DB/FastAPI imports. Parses raw LinkedIn "People" tab copy,
cleans names/titles, generates emails from patterns, and flags need-review cases.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass
class ParseResult:
    """Result of parse_people()."""
    people: list[dict]  # [{name, title, email}, ...]
    needs_review: list[dict]  # [{name, title, email, reason}, ...]
    dropped: list[dict]  # [{name, title, email, reason}, ...]


def _normalize_name(s: str) -> str:
    """Strip whitespace, collapse internal whitespace, handle quotes."""
    s = (s or "").strip()
    s = re.sub(r"\s+", " ", s)
    return s


def _is_noise_line(line: str) -> bool:
    """True if line is noise (followers, connections, services, etc.)."""
    s = line.strip().lower()
    if not s:
        return True
    # Followers/following
    if re.match(r"^\d[\d,]*\s+followers?$", s):
        return True
    if re.match(r"^\d[\d,]*\s+following$", s):
        return True
    # Connections
    if "mutual connection" in s or "degree connection" in s:
        return True
    # Services
    if s.startswith("provides services"):
        return True
    # Action buttons
    if s in ("message", "follow", "connect", "pending"):
        return True
    return False


def _strip_diacritics(s: str) -> str:
    """Remove diacritics using NFKD normalization."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", s)
        if unicodedata.category(c) != "Mn"
    )


def _clean_name(name: str) -> str:
    """Clean: normalize, strip diacritics, title-case all-upper/all-lower."""
    name = _normalize_name(name)
    # Strip "is open to work" suffix
    name = re.sub(r"\s+is open to work\s*$", "", name, flags=re.IGNORECASE)
    name = _normalize_name(name)
    
    # Title case if all-upper or all-lower
    if name and (name.isupper() or name.islower()):
        name = name.title()
    
    # Strip diacritics
    name = _strip_diacritics(name)
    return name


def _dedupe_doubled_name(name: str) -> str:
    """Handle doubled name lines like 'John Doe John Doe'."""
    parts = name.split()
    if not parts:
        return name
    
    # If second half is identical to first half, keep just first
    mid = len(parts) // 2
    if mid > 0 and parts[:mid] == parts[mid:]:
        return " ".join(parts[:mid])
    
    return name


def _is_abbreviated_first_name(name: str) -> bool:
    """True if first token is a single initial or initial-dot pattern."""
    parts = name.split()
    if not parts:
        return False
    first = parts[0]
    # Single letter or single letter with dot (M., A., etc.)
    if len(first) == 1 and first.isalpha():
        return True
    if len(first) == 2 and first[0].isalpha() and first[1] == ".":
        return True
    return False


def _has_lone_initial_surname(name: str) -> bool:
    """True if surname (last token) is a single letter."""
    parts = name.split()
    if len(parts) < 2:
        return False
    last = parts[-1]
    # Single letter or initial+dot
    if len(last) == 1 and last.isalpha():
        return True
    if len(last) == 2 and last[0].isalpha() and last[1] == ".":
        return True
    return False


def _is_linkedin_member_only(name: str) -> bool:
    """True if name is exactly 'LinkedIn Member'."""
    return name.strip().lower() == "linkedin member"


def _has_no_surname(name: str) -> bool:
    """True if name is single token (no surname)."""
    return len(name.split()) < 2


def slugify(s: str) -> str:
    """Lowercase, ASCII, replace non-alphanumeric with underscore."""
    s = _strip_diacritics((s or "").lower())
    return re.sub(r"[^a-z0-9]+", "_", s).strip("_")


def make_email(name: str, pattern: str, domain: str = "") -> str:
    """Generate email from name and pattern.
    
    Args:
        name: Full name (e.g., "John Doe")
        pattern: Email pattern with tokens: {first}, {last}, {f}, {l}
        domain: Email domain (e.g., "example.com"). If empty, extracted from pattern.
    
    Returns:
        Lowercase ASCII email address.
    """
    # Extract domain from pattern if not provided
    if not domain and "@" in pattern:
        domain = pattern.split("@", 1)[1].strip()
    
    # Normalize name and strip diacritics
    name = _normalize_name(name)
    name = _strip_diacritics(name)
    
    # Remove special characters from name tokens (hyphens, apostrophes, etc.)
    name = re.sub(r"[^a-zA-Z0-9\s.]", "", name)
    name = _normalize_name(name)
    
    # Split into tokens
    parts = name.lower().split()
    first = parts[0] if parts else ""
    last = parts[-1] if len(parts) > 1 else ""
    f = first[0] if first else ""
    l = last[0] if last else ""
    
    # Replace tokens in pattern
    local = pattern.split("@")[0] if "@" in pattern else pattern
    local = local.replace("{first}", first)
    local = local.replace("{last}", last)
    local = local.replace("{f}", f)
    local = local.replace("{l}", l)
    
    # Clean: lowercase, ASCII only, remove special chars but collapse consecutive dots
    local = re.sub(r"[^a-z0-9._-]", "", local.lower())
    # Collapse consecutive dots into single dot
    local = re.sub(r"\.+", ".", local)
    
    if domain:
        return f"{local}@{domain}"
    return local


def parse_people(raw_text: str, company: str, pattern: str, *, domain: str = "") -> ParseResult:
    """Parse raw LinkedIn People tab copy.
    
    Args:
        raw_text: Raw pasted LinkedIn text
        company: Company name (stored as-is)
        pattern: Email pattern (e.g., "{first}.{last}@company.com")
        domain: Email domain override (optional)
    
    Returns:
        ParseResult with people, needs_review, dropped lists.
    """
    lines = raw_text.split("\n")
    
    people_list = []
    needs_review_list = []
    dropped_list = []
    seen_slugs = set()  # For dedup within paste
    
    # Find degree anchors (·?\s*1st|2nd|3rd)
    degree_re = re.compile(r"·?\s*(?:1st|2nd|3rd)\b")
    
    i = 0
    while i < len(lines):
        line = lines[i]
        
        # Look for degree anchor
        if not degree_re.search(line):
            i += 1
            continue
        
        # NAME: first non-noise line above anchor
        name_line = None
        for j in range(i - 1, -1, -1):
            candidate = lines[j].strip()
            if not _is_noise_line(candidate) and candidate:
                name_line = candidate
                break
        
        if not name_line:
            i += 1
            continue
        
        # Clean the name
        name = _clean_name(name_line)
        name = _dedupe_doubled_name(name)
        name = _normalize_name(name)
        
        # TITLE: first non-noise line below anchor
        title = ""
        for j in range(i + 1, len(lines)):
            candidate = lines[j].strip()
            if _is_noise_line(candidate):
                continue
            if candidate and not degree_re.search(candidate):
                title = _normalize_name(candidate)
                break
        
        # Check discard conditions (in order of priority)
        
        # 1. LinkedIn Member only
        if _is_linkedin_member_only(name):
            dropped_list.append({
                "name": name,
                "title": title,
                "email": "",
                "reason": "anonymized (LinkedIn Member)"
            })
            i += 1
            continue
        
        # 2. Lone-initial surname
        if _has_lone_initial_surname(name):
            dropped_list.append({
                "name": name,
                "title": title,
                "email": "",
                "reason": "lone-initial surname"
            })
            i += 1
            continue
        
        # 3. No surname
        if _has_no_surname(name):
            dropped_list.append({
                "name": name,
                "title": title,
                "email": "",
                "reason": "no surname"
            })
            i += 1
            continue
        
        # Generate email
        email = make_email(name, pattern, domain)
        
        # 4. Dedup by slug
        slug = slugify(name)
        if slug in seen_slugs:
            dropped_list.append({
                "name": name,
                "title": title,
                "email": email,
                "reason": "duplicate within paste"
            })
            i += 1
            continue
        seen_slugs.add(slug)
        
        # 5. Abbreviated first name → needs review
        if _is_abbreviated_first_name(name):
            needs_review_list.append({
                "name": name,
                "title": title,
                "email": email,
                "reason": "abbreviated first name"
            })
            i += 1
            continue
        
        # Accept
        people_list.append({
            "name": name,
            "title": title,
            "email": email
        })
        
        i += 1
    
    return ParseResult(
        people=people_list,
        needs_review=needs_review_list,
        dropped=dropped_list
    )
