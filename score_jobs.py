#!/usr/bin/env python3
"""Score job_posts.csv against resume evidence and write matched_jobs.csv.

Deterministic, rule-based Requirement-Coverage Scorer (RCS) — no LLM.
Same inputs -> identical output.

Fit % = the share of THIS JD's recognised requirements that the resume evidences.

  coverage = supported_weight / (supported_weight + lacked_weight + unclassified_weight)
  Fit      = round(100 * coverage), then multiplicative hard/soft caps applied.

  - supported_weight   : importance of must-have terms the owner CAN evidence
  - lacked_weight      : importance of required tools the owner CANNOT do (UNSUPPORTED_BANK)
  - unclassified_weight: required tech acronyms in neither bank (surfaced for review)

Caps (multiplicative, never additive floors):
  - citizenship / security clearance demanded -> hard cap (owner on a 485 visa)
  - senior/lead/5+/7+ years in the requirements region -> soft 0.85 cap (owner is early-career)

There are NO floors: a JD with no recognised requirements scores LOW, not 77.
'Fit %' is an expected-fit estimate, NOT an interview probability.

Rows scoring >= KEEP_THRESHOLD are kept and written to matched_jobs.csv.
"""

from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

import config as _config

from generate_accepted_resumes import (
    SUPPORTED_TERMS,
    UNSUPPORTED_TERMS,
    UNSUPPORTED_BANK,
    extract_terms_detailed,
    importance,
    role_family,
    _REQ_REGION,
)

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "job_posts.csv"
MATCHED = ROOT / "matched_jobs.csv"

# Tuned against the corpus (see re-score note). Coverage-based scale, not the old
# inflated 70-point-floor scale.
KEEP_THRESHOLD = 45
STRONG_THRESHOLD = 68

# Laplace smoothing on the coverage denominator. A JD that surfaces only a few
# requirements gives an unreliable 100% (e.g. 3 soft skills -> perfect fit). Adding
# SMOOTH pseudo-requirements dampens thin-JD inflation while leaving evidence-rich
# matches near the top.
SMOOTH = 2

_CITIZENSHIP_REQUIRED = re.compile(
    r"\b(australian citizen|citizenship|security clearance|agsva|baseline clearance|"
    r"nv1|nv2|must be a citizen|permanent resident only|pr or citizen)\b",
    re.IGNORECASE,
)
# Senior/over-qualified signals — only counted inside the requirements region.
_SENIORITY_SENIOR = re.compile(
    r"\b(senior|lead|principal|head of|10\+\s*years|7\+\s*years|5\+\s*years|"
    r"minimum (of )?[5-9] years|at least [5-9] years)\b",
    re.IGNORECASE,
)
_ACRONYM = re.compile(r"\b([A-Z]{2,5})\b")
# Common non-tool acronyms that must never count as a missing tech requirement.
_ACRONYM_STOP = frozenset({
    "AU", "NZ", "US", "USA", "UK", "EU", "AUS", "ANZ", "APAC", "EMEA",
    "CEO", "CFO", "CIO", "CTO", "COO", "HR", "PR", "IT", "QA", "RD",
    "KPI", "KPIS", "OKR", "OKRS", "SLA", "ROI", "EOI", "FAQ", "ASAP", "FYI",
    "PDF", "DOC", "URL", "WFH", "FTE", "EOD", "TBC", "TBD", "AKA", "ETC",
    "AND", "OR", "THE", "FOR", "YOU", "OUR", "WE", "ALL", "ANY", "NEW",
    "VIC", "NSW", "QLD", "WA", "SA", "ACT", "NT", "CBD", "GMT", "AEST", "AEDT",
    "DEI", "EEO", "LGBT", "LGBTQ", "ESG", "CSR", "NDIS", "TAFE",
})


def _req_start(jd: str):
    m = _REQ_REGION.search(jd)
    return m.start() if m else None


def _lacked_weight(jd_low: str, req_start, unsupported_labels) -> int:
    """Weight required-but-lacked tools higher when they sit in the requirements region."""
    w = 0
    for label in unsupported_labels:
        aliases = UNSUPPORTED_BANK.get(label, [label.lower()])
        positions = [jd_low.find(a) for a in aliases if jd_low.find(a) >= 0]
        pos = min(positions) if positions else -1
        in_region = req_start is not None and pos >= req_start
        w += 4 if in_region else 2
    return w


def _unclassified_acronyms(jd: str, req_start) -> list[str]:
    """Required tech-looking acronyms in neither bank — the blind-spot tripwire."""
    region = jd[req_start:] if req_start is not None else jd
    out = []
    seen = set()
    for m in _ACRONYM.finditer(region):
        tok = m.group(1)
        low = tok.lower()
        if low in SUPPORTED_TERMS or low in UNSUPPORTED_TERMS or tok in _ACRONYM_STOP:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def score_job(role: str, jd: str) -> dict:
    jd_low = jd.lower()
    req_start = _req_start(jd)
    hits, unsupported = extract_terms_detailed(jd)

    supported_w = sum(importance(h) for h in hits.values())
    lacked_w = _lacked_weight(jd_low, req_start, unsupported)
    unclassified = _unclassified_acronyms(jd, req_start)
    unclassified_w = 2 * len(unclassified)

    total_w = supported_w + lacked_w + unclassified_w
    if total_w == 0:
        fit = 12.0
        confidence = "low"
    else:
        fit = 100.0 * supported_w / (total_w + SMOOTH)
        confidence = "ok" if (req_start is not None and total_w >= 4) else "low"

    caps = []
    if _config.needs_sponsorship and _CITIZENSHIP_REQUIRED.search(jd):
        fit = min(fit, 25.0)
        caps.append("citizenship/clearance required")
    if _config.seniority_cap is not None and req_start is not None and _SENIORITY_SENIOR.search(jd[req_start:]):
        fit *= 0.85
        caps.append("senior/lead level")

    fit = round(max(0.0, min(100.0, fit)))
    return {
        "fit": fit,
        "band": _band(fit),
        "supported": sorted(hits),
        "lacked": sorted(unsupported),
        "unclassified": unclassified,
        "confidence": confidence,
        "caps": caps,
        "coverage_pct": round(100 * supported_w / total_w) if total_w else 0,
    }


def _band(fit: int) -> str:
    if fit >= STRONG_THRESHOLD:
        return "strong"
    if fit >= KEEP_THRESHOLD:
        return "fair"
    return "drop"


def _why_keep(r: dict, family: str) -> str:
    skills = ", ".join(r["supported"][:3]) if r["supported"] else "no evidenced must-haves"
    msg = f"{r['band']} fit {r['fit']}% — {family.replace('_', ' ')}; evidences {skills}"
    if r["lacked"]:
        msg += f"; missing {', '.join(r['lacked'][:3])}"
    if r["caps"]:
        msg += f"; capped: {', '.join(r['caps'])}"
    return msg


def _read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames:
            sys.exit(f"[!] No header in {path}")
        return list(reader)


def main() -> None:
    if not JOBS.exists():
        sys.exit(f"[!] {JOBS} not found — run prepare_job_posts.py first")

    rows = _read_csv(JOBS)
    kept: list[dict] = []
    dropped = 0

    for row in rows:
        jd = row.get("job_text", "").strip()
        role = row.get("role_title", "").strip()
        if not jd:
            dropped += 1
            continue

        family = role_family(role, jd)
        r = score_job(role, jd)
        if r["band"] == "drop":
            dropped += 1
            continue

        kept.append({
            "company": row.get("company", ""),
            "role_title": role,
            "location": row.get("location", ""),
            "job_url": row.get("job_url", ""),
            "match_score": str(r["fit"]),
            "match_band": r["band"],
            "matched_evidence": "; ".join(r["supported"]),
            "gaps": "; ".join(r["lacked"]),
            "why_keep": _why_keep(r, family),
            "confidence": r["confidence"],
            "unclassified_requirements": "; ".join(r["unclassified"]),
        })

    kept.sort(key=lambda x: (-int(x["match_score"]), x["company"], x["role_title"]))

    fieldnames = [
        "company", "role_title", "location", "job_url",
        "match_score", "match_band", "matched_evidence", "gaps", "why_keep",
        "confidence", "unclassified_requirements",
    ]
    with MATCHED.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    scores = [int(x["match_score"]) for x in kept]
    rng = f"{min(scores)}-{max(scores)}" if scores else "n/a"
    print(f"Scored {len(rows)} jobs: {len(kept)} kept (Fit >= {KEEP_THRESHOLD}), {dropped} dropped")
    print(f"Fit range: {rng}")
    print(f"Written to {MATCHED}")


if __name__ == "__main__":
    main()
