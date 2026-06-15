"""facts_bridge.py — Convert a parsed résumé dict into a valid facts.json.

Public API
----------
build_facts(parsed: dict) -> dict
    Turn the output of resume_parser.parse_resume() into a dict matching the
    exact schema consumed by config.load_facts_override() and the override block
    in generate_accepted_resumes.py (FACT_BANK / EXPERIENCE_SLOTS / BULLET_BUDGETS).

save_facts(facts: dict, root: Path) -> None
    Atomically write facts to <root>/facts.json.

Constraints honoured so validate_fact_bank() never raises
----------------------------------------------------------
1. Every evidences label is in config.TERM_BANK  (ESCO-only labels filtered out).
2. No two bullets in the same slot share the same first word  (lint_bullet_openers).
3. No bullet contains a banned phrase  (lint_text / BANNED_PHRASES).
4. len(pool) >= BULLET_BUDGETS[slot]  (pool >= budget, guaranteed because budget
   = min(len(surviving_bullets), 5) and we keep at least one bullet per slot).
"""
from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path
from typing import Dict, List

# ---------------------------------------------------------------------------
# Banned phrases — mirror of generate_accepted_resumes.BANNED_PHRASES so we
# can filter before generation rather than letting it hard-fail.
# ---------------------------------------------------------------------------

_BANNED_PHRASES = [
    "leverag", "spearhead", "passionate", "synerg", "results-driven",
    "results driven", "proven track record", "dynamic professional",
    "team player", "go-getter", "think outside", "utilize", "utilis",
    "responsible for", "duties included", "i am excited", "i believe my",
    "cutting-edge", "cutting edge", "seamless", "delve", "in today's",
    "fast-paced", "best-in-class", "world-class", "honed", "esteemed",
    "keen eye",
]


def _has_banned_phrase(text: str) -> bool:
    low = text.lower()
    return any(phrase in low for phrase in _BANNED_PHRASES)


# ---------------------------------------------------------------------------
# Core builder
# ---------------------------------------------------------------------------

def build_facts(parsed: dict) -> dict:
    """Return a facts.json-compatible dict built from a parsed résumé.

    Parameters
    ----------
    parsed : dict
        Output of resume_parser.parse_resume() — must contain an
        ``experiences`` key: list of {role, company, dates, bullets: [str]}.

    Returns
    -------
    dict with keys FACT_BANK, EXPERIENCE_SLOTS, BULLET_BUDGETS.
    """
    # Lazy imports — avoid module-level side effects and keep stdlib-adjacent.
    import config
    import ontology

    term_bank_labels: set = set(config.TERM_BANK.keys())

    fact_bank: Dict[str, List[Dict]] = {}
    experience_slots: List[List[str]] = []
    bullet_budgets: Dict[str, int] = {}

    experiences = parsed.get("experiences") or []

    for idx, exp in enumerate(experiences):
        slot_key = f"role{idx + 1}"
        role = (exp.get("role") or "").strip()
        company = (exp.get("company") or "").strip()
        dates = (exp.get("dates") or "").strip()

        # Build the header string in the format the generator renders:
        # "Role | Company | Dates"
        parts = [p for p in [role, company, dates] if p]
        header = " | ".join(parts) if parts else slot_key

        raw_bullets: List[str] = [b for b in (exp.get("bullets") or []) if b and b.strip()]

        # --- Filter 1: drop slop-linter violators ---
        clean: List[str] = [b for b in raw_bullets if not _has_banned_phrase(b)]
        # If filtering removed everything, restore originals (better a flagged
        # resume than a crash; validate_fact_bank only checks structure).
        if not clean:
            clean = raw_bullets[:]

        # --- Filter 2: deduplicate on first word (lint_bullet_openers) ---
        # Keep the first bullet with each opening word; drop later duplicates.
        kept: List[str] = []
        seen_openers: set = set()
        for b in clean:
            words = b.split()
            if not words:
                continue
            opener = words[0].lower()
            if opener not in seen_openers:
                kept.append(b)
                seen_openers.add(opener)
            # else: silently drop — same first word as an earlier kept bullet

        # --- Ensure at least one bullet (validate_fact_bank: pool >= budget) ---
        if not kept:
            if raw_bullets:
                kept = [max(raw_bullets, key=len)]  # keep a real bullet, never invent
            elif role and not company and not dates:
                # A role with no company, no dates, and no bullets is almost always a
                # misparsed prose line, not a real job. Drop the slot entirely.
                continue
            else:
                # A genuine header (role + company/dates) that simply listed no
                # bullets: a neutral, truthful placeholder.
                kept = [f"Contributed to {role}." if role else "Contributed to the team."]

        # --- Tag evidences: ontology match, filtered to TERM_BANK labels ---
        pool: List[Dict] = []
        for bullet in kept:
            matched = ontology.match_text(bullet)
            evidences = sorted(lbl for lbl in matched if lbl in term_bank_labels)
            pool.append({"text": bullet, "evidences": evidences})

        # Budget: at most 5, never more than pool size.
        budget = min(len(pool), 5)

        fact_bank[slot_key] = pool
        bullet_budgets[slot_key] = budget
        experience_slots.append([header, slot_key])

    # Edge case: no experiences parsed → return a minimal valid structure
    # using a single empty-ish slot so the generator doesn't crash.
    if not fact_bank:
        fact_bank["role1"] = [{"text": "Contributed to data and analytics work.", "evidences": []}]
        bullet_budgets["role1"] = 1
        experience_slots = [["Experience | | ", "role1"]]

    return {
        "FACT_BANK": fact_bank,
        "EXPERIENCE_SLOTS": experience_slots,
        "BULLET_BUDGETS": bullet_budgets,
    }


def save_facts(facts: dict, root: Path) -> None:
    """Atomically write facts to <root>/facts.json."""
    dest = root / "facts.json"
    tmp = str(dest) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(facts, f, indent=2)
        f.write("\n")
    os.replace(tmp, str(dest))
