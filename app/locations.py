"""app/locations.py — Country/State/City tree helpers for the location selector."""
from __future__ import annotations

import json
import os

_DATA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "locations.json")
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        with open(_DATA_PATH, encoding="utf-8") as f:
            _cache = json.load(f)
    return _cache


def countries() -> list[str]:
    """Return country names with synthetic 'Remote' prepended."""
    data = _load()
    return ["Remote"] + [c["name"] for c in data["countries"]]


def states(country: str) -> list[str]:
    """Return state names for the given country, or [] for city-states / Remote."""
    data = _load()
    for c in data["countries"]:
        if c["name"] == country:
            return [s["name"] for s in c.get("states", [])]
    return []


def cities(country: str, state: str) -> list[str]:
    """Return city names for the given country+state."""
    data = _load()
    for c in data["countries"]:
        if c["name"] == country:
            for s in c.get("states", []):
                if s["name"] == state:
                    return list(s.get("cities", []))
    return []


def build_query(country: str, state: str, city: str) -> str:
    """Build the LinkedIn search string from the deepest non-empty level."""
    country = (country or "").strip()
    state = (state or "").strip()
    city = (city or "").strip()
    if city:
        return f"{city}, {state}, {country}"
    if state:
        return f"{state}, {country}"
    if country == "Remote":
        return "Remote"
    return country


def build_key(country: str, state: str, city: str) -> str:
    """Return a pipe-joined lowercase key of non-empty segments."""
    parts = [s.strip().lower() for s in (country, state, city) if s and s.strip()]
    return "|".join(parts)


def validate(country: str, state: str, city: str) -> bool:
    """Return True if the selection exists in the tree (or is Remote with no state/city)."""
    country = (country or "").strip()
    state = (state or "").strip()
    city = (city or "").strip()

    if country == "Remote":
        return not state and not city

    data = _load()
    country_data = None
    for c in data["countries"]:
        if c["name"] == country:
            country_data = c
            break
    if country_data is None:
        return False

    # City-states (e.g. Singapore) have no states list — country-only is valid.
    if not country_data.get("states"):
        return not state and not city

    # Country-only selection is valid even when states exist.
    if not state:
        return not city

    state_data = None
    for s in country_data["states"]:
        if s["name"] == state:
            state_data = s
            break
    if state_data is None:
        return False

    if not city:
        return True

    return city in state_data.get("cities", [])


def location_breadcrumb(name: str) -> list[str]:
    """Return split segments if name uses the pipe format, else []."""
    if "|" in name:
        return name.split("|")
    return []
