"""ontology.py — Ontology-backed skill recognition layer.

Loads an alias map from either:
  1. An ESCO skills CSV (set env ESCO_DATA or drop esco/skills_en.csv in the
     repo root).  ESCO format: columns 'preferredLabel' and one of
     'altLabels' / 'alternativeLabels' (newline-separated values).
  2. Fallback: taxonomy.json (supported_skills section).

In both cases, the curated taxonomy.json aliases are unioned ON TOP (curated
wins on conflicts) so built-in labels like 'powerbi' / 'dimensional model'
always match regardless of the source.

The compiled Aho-Corasick automaton is cached in-process.

Public API
----------
normalize(s: str) -> str
    NFKD strip-accents, lower, collapse whitespace.

load_alias_map() -> dict[normalized_alias, canonical_label]

match_text(text: str) -> set[str]
    Build (or reuse) the automaton and run find() over text.

group_for(label: str) -> str
    Best-effort group from taxonomy.json; falls back to 'General'.
"""
from __future__ import annotations

import csv
import json
import os
import unicodedata
from pathlib import Path
from typing import Dict, Optional, Set

from matcher import build_automaton, find, _Automaton

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

def normalize(s: str) -> str:
    """NFKD strip-accents, lowercase, collapse whitespace."""
    # NFKD decomposition followed by dropping combining characters
    nfkd = unicodedata.normalize("NFKD", s)
    stripped = "".join(ch for ch in nfkd if unicodedata.category(ch) != "Mn")
    return " ".join(stripped.lower().split())


# ---------------------------------------------------------------------------
# Taxonomy helpers
# ---------------------------------------------------------------------------

_taxonomy_cache: Optional[dict] = None


def _load_taxonomy() -> dict:
    global _taxonomy_cache
    if _taxonomy_cache is None:
        tax_path = ROOT / "taxonomy.json"
        if tax_path.exists():
            with tax_path.open(encoding="utf-8") as f:
                _taxonomy_cache = json.load(f)
        else:
            _taxonomy_cache = {}
    return _taxonomy_cache


def group_for(label: str) -> str:
    """Return the taxonomy group for label, or 'General' if unknown."""
    taxonomy = _load_taxonomy()
    entry = taxonomy.get(label)
    if entry and "group" in entry:
        return entry["group"]
    return "General"


# ---------------------------------------------------------------------------
# Alias map construction
# ---------------------------------------------------------------------------

def _aliases_from_taxonomy() -> Dict[str, str]:
    """Return {normalized_alias: label} from taxonomy.json supported_skills."""
    taxonomy = _load_taxonomy()
    alias_map: Dict[str, str] = {}
    for label, meta in taxonomy.items():
        for alias in meta.get("aliases", []):
            alias_map[normalize(alias)] = label
    return alias_map


def _aliases_from_esco(esco_path: Path) -> Dict[str, str]:
    """Parse an ESCO skills CSV into {normalized_alias: preferredLabel}."""
    alias_map: Dict[str, str] = {}
    with esco_path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        # Support both 'altLabels' and 'alternativeLabels'
        alt_col = None
        for candidate in ("altLabels", "alternativeLabels"):
            if candidate in headers:
                alt_col = candidate
                break

        for row in reader:
            preferred = (row.get("preferredLabel") or "").strip()
            if not preferred:
                continue
            label = preferred  # use preferred label as canonical

            # preferred label is itself an alias
            alias_map[normalize(preferred)] = label

            # alternative labels (newline-separated)
            if alt_col:
                for alt in (row.get(alt_col) or "").splitlines():
                    alt = alt.strip()
                    if alt:
                        alias_map[normalize(alt)] = label

    return alias_map


def load_alias_map() -> Dict[str, str]:
    """Return the merged alias map (ESCO if available, else taxonomy fallback).

    Curated taxonomy aliases are always overlaid last so they win on conflicts.
    """
    # Check for ESCO data source
    esco_path: Optional[Path] = None
    env_path = os.environ.get("ESCO_DATA")
    if env_path:
        p = Path(env_path)
        if p.exists():
            esco_path = p
    if esco_path is None:
        candidate = ROOT / "esco" / "skills_en.csv"
        if candidate.exists():
            esco_path = candidate

    if esco_path is not None:
        alias_map = _aliases_from_esco(esco_path)
    else:
        alias_map = _aliases_from_taxonomy()

    # Union the curated taxonomy on top (curated wins on conflicts)
    alias_map.update(_aliases_from_taxonomy())

    return alias_map


# ---------------------------------------------------------------------------
# In-process cache for the compiled automaton
# ---------------------------------------------------------------------------

_cached_automaton: Optional[_Automaton] = None
_cached_alias_map: Optional[Dict[str, str]] = None


def _get_automaton() -> _Automaton:
    global _cached_automaton, _cached_alias_map
    if _cached_automaton is None:
        _cached_alias_map = load_alias_map()
        _cached_automaton = build_automaton(_cached_alias_map)
    return _cached_automaton


# ---------------------------------------------------------------------------
# match_text
# ---------------------------------------------------------------------------

def match_text(text: str) -> Set[str]:
    """Return the set of canonical skill labels matched in text."""
    return find(text, _get_automaton())


# ---------------------------------------------------------------------------
# Cache invalidation helper (useful for tests that swap ESCO_DATA)
# ---------------------------------------------------------------------------

def aliases_for(labels: list[str]) -> Dict[str, list[str]]:
    """Return {label: [normalized_alias, ...]} for each requested label.

    Inverts the cached alias map.  Only aliases that pass a basic quality
    guard (length >= 3, not purely digits) are returned.
    """
    alias_map = load_alias_map()
    result: Dict[str, list[str]] = {lbl: [] for lbl in labels}
    label_set = set(labels)
    for norm_alias, canonical in alias_map.items():
        if canonical in label_set:
            if len(norm_alias) >= 3 and not norm_alias.isdigit():
                result[canonical].append(norm_alias)
    return result


def _reset_cache() -> None:
    """Clear in-process caches.  Intended for tests only."""
    global _cached_automaton, _cached_alias_map, _taxonomy_cache
    _cached_automaton = None
    _cached_alias_map = None
    _taxonomy_cache = None
