"""Centralised runtime config for the Northstar job engine.

Loads skills.json + config.json from the repo root.
Environment overrides: JOBENGINE_SKILLS, JOBENGINE_CONFIG.
Falls back to skills.example.json / config.example.json so --help and tests
work without the real (gitignored) files.

Exposes:
  TERM_BANK, UNSUPPORTED_BANK  — dicts loaded from skills.json
  SUPPORTED_TERMS, UNSUPPORTED_TERMS, _TERM_PATTERNS, _UNSUPPORTED_PATTERNS
  _alias_pattern                — re-export (moved here from generate_accepted_resumes)
  NAME, CONTACT, WORK_RIGHTS   — identity strings from config.json
  get_target_keywords()
  get_target_location()
  get_recency_tpr()
  needs_sponsorship (bool)
  seniority_cap (int | None)
  generation_enabled (bool)
  keep_threshold (int)
  strong_threshold (int)
  exclude_companies (list)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# File resolution — env override > real file > example fallback
# ---------------------------------------------------------------------------

def _resolve(env_var: str, real_name: str, example_name: str) -> Path:
    override = os.environ.get(env_var)
    if override:
        return Path(override)
    real = ROOT / real_name
    if real.exists():
        return real
    return ROOT / example_name


_SKILLS_PATH = _resolve("JOBENGINE_SKILLS", "skills.json", "skills.example.json")
_CONFIG_PATH = _resolve("JOBENGINE_CONFIG", "config.json", "config.example.json")

# ---------------------------------------------------------------------------
# Lazy-load: only fail when the bank is actually accessed
# ---------------------------------------------------------------------------

_skills_raw: dict | None = None
_config_raw: dict | None = None


def _load_skills() -> dict:
    global _skills_raw
    if _skills_raw is None:
        if not _SKILLS_PATH.exists():
            raise FileNotFoundError(
                f"skills file not found at {_SKILLS_PATH}. "
                "Copy skills.example.json to skills.json and fill it in."
            )
        with _SKILLS_PATH.open(encoding="utf-8") as f:
            _skills_raw = json.load(f)
    return _skills_raw


def _load_config() -> dict:
    global _config_raw
    if _config_raw is None:
        if not _CONFIG_PATH.exists():
            raise FileNotFoundError(
                f"config file not found at {_CONFIG_PATH}. "
                "Copy config.example.json to config.json and fill it in."
            )
        with _CONFIG_PATH.open(encoding="utf-8") as f:
            _config_raw = json.load(f)
    return _config_raw


# ---------------------------------------------------------------------------
# TERM_BANK / UNSUPPORTED_BANK — lazy proxies
# ---------------------------------------------------------------------------

class _LazyBank(dict):
    """Dict that loads from file on first access."""

    def __init__(self, key: str):
        super().__init__()
        self._key = key
        self._loaded = False

    def _ensure(self):
        if not self._loaded:
            data = _load_skills().get(self._key, {})
            self.update(data)
            self._loaded = True

    def __getitem__(self, k):
        self._ensure()
        return super().__getitem__(k)

    def __contains__(self, k):
        self._ensure()
        return super().__contains__(k)

    def __iter__(self):
        self._ensure()
        return super().__iter__()

    def items(self):  # type: ignore[override]
        self._ensure()
        return super().items()

    def keys(self):  # type: ignore[override]
        self._ensure()
        return super().keys()

    def values(self):  # type: ignore[override]
        self._ensure()
        return super().values()

    def get(self, k, default=None):
        self._ensure()
        return super().get(k, default)

    def __len__(self):
        self._ensure()
        return super().__len__()


TERM_BANK: Dict[str, Dict] = _LazyBank("supported_skills")
UNSUPPORTED_BANK: Dict[str, List[str]] = _LazyBank("unsupported_skills")

# ---------------------------------------------------------------------------
# _alias_pattern — moved verbatim from generate_accepted_resumes.py
# ---------------------------------------------------------------------------

def _alias_pattern(alias: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(alias) + r"(?![a-z0-9])")


# ---------------------------------------------------------------------------
# Derived lookup structures — lazy (built on first import access)
# These match the exact comprehensions from generate_accepted_resumes.py L209-229
# ---------------------------------------------------------------------------

class _LazySupportedTerms(dict):
    _loaded = False

    def _ensure(self):
        if not self._loaded:
            self.update({a: lbl for lbl, meta in TERM_BANK.items() for a in meta["aliases"]})
            self._loaded = True

    def __getitem__(self, k):
        self._ensure(); return super().__getitem__(k)
    def __contains__(self, k):
        self._ensure(); return super().__contains__(k)
    def __iter__(self):
        self._ensure(); return super().__iter__()
    def items(self):  # type: ignore[override]
        self._ensure(); return super().items()
    def keys(self):  # type: ignore[override]
        self._ensure(); return super().keys()
    def values(self):  # type: ignore[override]
        self._ensure(); return super().values()
    def get(self, k, default=None):
        self._ensure(); return super().get(k, default)
    def __len__(self):
        self._ensure(); return super().__len__()


class _LazyUnsupportedTerms(dict):
    _loaded = False

    def _ensure(self):
        if not self._loaded:
            self.update({a: lbl for lbl, aliases in UNSUPPORTED_BANK.items() for a in aliases})
            self._loaded = True

    def __getitem__(self, k):
        self._ensure(); return super().__getitem__(k)
    def __contains__(self, k):
        self._ensure(); return super().__contains__(k)
    def __iter__(self):
        self._ensure(); return super().__iter__()
    def items(self):  # type: ignore[override]
        self._ensure(); return super().items()
    def keys(self):  # type: ignore[override]
        self._ensure(); return super().keys()
    def values(self):  # type: ignore[override]
        self._ensure(); return super().values()
    def get(self, k, default=None):
        self._ensure(); return super().get(k, default)
    def __len__(self):
        self._ensure(); return super().__len__()


class _LazyTermPatterns(dict):
    _loaded = False

    def _ensure(self):
        if not self._loaded:
            self.update({
                lbl: [_alias_pattern(a) for a in meta["aliases"]]
                for lbl, meta in TERM_BANK.items()
            })
            self._loaded = True

    def __getitem__(self, k):
        self._ensure(); return super().__getitem__(k)
    def __contains__(self, k):
        self._ensure(); return super().__contains__(k)
    def __iter__(self):
        self._ensure(); return super().__iter__()
    def items(self):  # type: ignore[override]
        self._ensure(); return super().items()
    def keys(self):  # type: ignore[override]
        self._ensure(); return super().keys()
    def values(self):  # type: ignore[override]
        self._ensure(); return super().values()
    def get(self, k, default=None):
        self._ensure(); return super().get(k, default)
    def __len__(self):
        self._ensure(); return super().__len__()


class _LazyUnsupportedPatterns(dict):
    _loaded = False

    def _ensure(self):
        if not self._loaded:
            self.update({
                lbl: [_alias_pattern(a) for a in aliases]
                for lbl, aliases in UNSUPPORTED_BANK.items()
            })
            self._loaded = True

    def __getitem__(self, k):
        self._ensure(); return super().__getitem__(k)
    def __contains__(self, k):
        self._ensure(); return super().__contains__(k)
    def __iter__(self):
        self._ensure(); return super().__iter__()
    def items(self):  # type: ignore[override]
        self._ensure(); return super().items()
    def keys(self):  # type: ignore[override]
        self._ensure(); return super().keys()
    def values(self):  # type: ignore[override]
        self._ensure(); return super().values()
    def get(self, k, default=None):
        self._ensure(); return super().get(k, default)
    def __len__(self):
        self._ensure(); return super().__len__()


SUPPORTED_TERMS = _LazySupportedTerms()
UNSUPPORTED_TERMS = _LazyUnsupportedTerms()
_TERM_PATTERNS = _LazyTermPatterns()
_UNSUPPORTED_PATTERNS = _LazyUnsupportedPatterns()

# ---------------------------------------------------------------------------
# Identity from config.json
# ---------------------------------------------------------------------------

def _cfg() -> dict:
    return _load_config()


def _identity() -> dict:
    return _cfg().get("identity", {})


def _get_name() -> str:
    return _identity().get("name", "Candidate Name")


def _get_contact() -> str:
    return _identity().get("contact", "")


def _get_work_rights() -> str:
    return _identity().get("work_rights", "")


class _LazyStr(str):
    """String subclass that defers config load until first string operation."""
    pass


# Expose as module-level names; evaluated lazily via property-style accessors
# but used as plain strings in the importing modules.

class _ConfigProxy:
    """Namespace for config values, loaded on first attribute access."""
    _loaded = False
    _data: dict = {}

    def _load(self):
        if not self._loaded:
            cfg = _cfg()
            ident = cfg.get("identity", {})
            matching = cfg.get("matching", {})
            search = cfg.get("search", {})
            self._data = {
                "NAME": ident.get("name", "Candidate Name"),
                "CONTACT": ident.get("contact", ""),
                "WORK_RIGHTS": ident.get("work_rights", ""),
                "needs_sponsorship": bool(ident.get("needs_sponsorship", False)),
                "seniority_cap": matching.get("seniority_cap", None),
                "generation_enabled": bool(matching.get("generation_enabled", False)),
                "keep_threshold": int(matching.get("keep_threshold", 45)),
                "strong_threshold": int(matching.get("strong_threshold", 68)),
                "exclude_companies": list(matching.get("exclude_companies", [])),
                "target_keywords": list(search.get("target_keywords", ["analyst"])),
                "target_location": str(search.get("target_location", "Remote")),
                "recency_tpr": str(search.get("recency_tpr", "r86400")),
            }
            self._loaded = True

    def __getattr__(self, name: str):
        self._load()
        if name in self._data:
            return self._data[name]
        raise AttributeError(name)


_proxy = _ConfigProxy()


# ---------------------------------------------------------------------------
# Module-level attribute shims — these make `from config import NAME` work.
# We use a module __getattr__ for lazy resolution.
# ---------------------------------------------------------------------------

def __getattr__(name: str):
    _proxy._load()
    if name in _proxy._data:
        return _proxy._data[name]
    raise AttributeError(f"module 'config' has no attribute {name!r}")


# These are set eagerly only if accessed at import time via direct reference.
# Importing code uses `from config import NAME` which triggers __getattr__.

# ---------------------------------------------------------------------------
# Config accessor functions
# ---------------------------------------------------------------------------

def get_target_keywords() -> List[str]:
    return _proxy._load() or None or list(_cfg().get("search", {}).get("target_keywords", ["analyst"]))


def get_target_location() -> str:
    return _cfg().get("search", {}).get("target_location", "Remote")


def get_recency_tpr() -> str:
    return _cfg().get("search", {}).get("recency_tpr", "r86400")


# Fix get_target_keywords to not return None
def get_target_keywords() -> List[str]:  # noqa: F811
    _proxy._load()
    return list(_proxy._data.get("target_keywords", ["analyst"]))


# ---------------------------------------------------------------------------
# Validation (called lazily — only when banks are first used)
# ---------------------------------------------------------------------------

def _validate_banks() -> None:
    """Structural validation of the loaded skill banks."""
    for label, meta in TERM_BANK.items():
        if not meta.get("aliases"):
            raise ValueError(f"TERM_BANK[{label!r}] has empty aliases")
        if not meta.get("group"):
            raise ValueError(f"TERM_BANK[{label!r}] has no group")

    supported_keys = set(SUPPORTED_TERMS.keys())
    unsupported_keys = set(UNSUPPORTED_TERMS.keys())
    overlap = supported_keys & unsupported_keys
    if overlap:
        raise ValueError(
            f"SUPPORTED_TERMS and UNSUPPORTED_TERMS share keys: {sorted(overlap)[:5]}"
        )
