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
import ontology

from generate_accepted_resumes import (
    UNSUPPORTED_BANK,
    role_family,
    _REQ_REGION,
    _SENIORITY_WORDS,
)

ROOT = Path(__file__).resolve().parent
JOBS = ROOT / "job_posts.csv"
MATCHED = ROOT / "matched_jobs.csv"

# Requirement-coverage thresholds. Calibrated 2026-06-15 against the local
# corpus (job_posts.csv) under the full-taxonomy requirement model below.
KEEP_THRESHOLD = 45
STRONG_THRESHOLD = 68

# Laplace smoothing on the coverage denominator. A JD that surfaces only a few
# requirements gives an unreliable 100% (e.g. 3 soft skills -> perfect fit). Adding
# SMOOTH pseudo-requirements dampens thin-JD inflation while leaving evidence-rich
# matches near the top.
SMOOTH = 2

# Soft-skill weight: soft requirements (Communication, Stakeholder Management, ...)
# enter the coverage ratio at a FRACTION of a hard skill, because real ATS weight
# hard technical skills well above soft ones and soft skills cannot be reliably
# dis-evidenced from a résumé. The weight is role/seniority-dependent (see
# _soft_weight): an IC analyst is judged mostly on tooling; a manager/lead role
# legitimately up-weights communication/leadership. These are calibratable knobs
# (ranges defended in the calibration note), NOT an asserted hard:soft ratio.
W_SOFT_IC = 0.15        # individual-contributor baseline (range 0.10-0.20)
W_SOFT_BA = 0.25        # business-analyst family — soft is core (range 0.20-0.30)
W_SOFT_MANAGER = 0.40   # manager/lead/senior framing (range 0.30-0.50)


def _soft_weight(role: str, jd: str, req_start) -> float:
    """How much soft-skill coverage counts for THIS role. Manager/lead/senior >
    business-analyst > individual contributor."""
    region = jd[req_start:] if req_start is not None else jd
    if _SENIORITY_WORDS.search(role) or _SENIORITY_SENIOR.search(region):
        return W_SOFT_MANAGER
    if role_family(role, jd) == "business_analyst":
        return W_SOFT_BA
    return W_SOFT_IC

# Weight of an unmet requirement (a skill THIS JD asks for that the résumé does
# not evidence) relative to evidence the candidate actually has. < 1.0 because
# requirement lists are noisy and partly redundant (a JD naming both Tableau and
# Power BI does not make a Power BI user half-unfit). Calibrated, not arbitrary —
# see the calibration note; chosen from {0.5, 0.6, 0.7, 1.0}.
GAP_DISCOUNT = 0.6

# FIX B1: removed bare "citizenship" — it matched EEO boilerplate.
# Keep specific requirement phrasings only.
_CITIZENSHIP_REQUIRED = re.compile(
    r"\b(australian citizen|security clearance|agsva|baseline clearance|"
    r"nv1|nv2|must be a citizen|permanent resident only|pr or citizen)\b",
    re.IGNORECASE,
)
# EEO context markers: if a citizenship-ish match sits within ~60 chars of these,
# the cap is suppressed (it's just a diversity/EEO statement, not a requirement).
_EEO_CONTEXT = re.compile(
    r"\b(gender identity|sexual orientation|veteran|protected characteristic|marital status)\b",
    re.IGNORECASE,
)
# Over-qualification signal — TITLE-LEVEL seniority words only. The years-based
# alternations were removed: a stated minimum-years requirement is now a hard
# knockout (see detect_knockouts), not a soft 0.85 cap, so the two no longer
# double-count. This stays as a gentle cap for "senior/lead/principal" framing.
_SENIORITY_SENIOR = re.compile(
    r"\b(senior|lead|principal|head of)\b",
    re.IGNORECASE,
)

# --- Knockout / prescreening requirements -------------------------------------
# The only requirements real ATS auto-reject on (answered on the application
# FORM, overriding resume quality). Detected in the requirements region; a
# detected-but-unmet knockout flags "will auto-reject", an unmet work-auth keeps
# the hard 25 cap, and unknown candidate data degrades to "verify" (never fails).
_MIN_YEARS_PLUS = re.compile(r"\b(\d{1,2})\s*\+\s*years?\b", re.IGNORECASE)
_MIN_YEARS_QUAL = re.compile(
    r"\b(?:minimum|min\.?|at least)\s+(?:of\s+)?(\d{1,2})\s*\+?\s*years?\b",
    re.IGNORECASE,
)
# A degree is a knockout only when an explicit requirement word sits within ~50
# chars AND no softener does — "Bachelor's degree required" yes, "degree
# preferred / or equivalent experience" no. Conservative: when in doubt, skip.
_DEGREE_TERM = re.compile(
    r"\b(bachelor'?s?|master'?s?|phd|doctorate|degree|tertiary qualification)\b",
    re.IGNORECASE,
)
_DEGREE_REQUIRED = re.compile(r"\b(required|essential|must have|mandatory)\b", re.IGNORECASE)
_DEGREE_SOFTENER = re.compile(
    r"\b(preferred|desirable|advantageous|nice to have|a plus|"
    r"or equivalent|equivalent experience)\b",
    re.IGNORECASE,
)
_CERT_REQ = re.compile(
    r"\b(cpa|cfa|pmp|cscp|aws certified|azure (?:certified|administrator)|"
    r"cissp|prince2|itil|six sigma|comptia|security\+)\b",
    re.IGNORECASE,
)
_ONSITE_REQ = re.compile(
    r"\b(on-?site|in[-\s]office|in person|relocat\w+|"
    r"\d\s*days?\s*(?:per week\s*)?(?:in (?:the )?office|on-?site))\b",
    re.IGNORECASE,
)
# FIX A2: off-target role soft cap helpers.
# Stopwords stripped before token-overlap check (seniority/level words + common English).
_TITLE_STOPWORDS = frozenset({
    "senior", "junior", "lead", "principal", "head", "associate", "staff",
    "the", "and", "of", "in", "a", "an", "to", "for", "with", "at",
    "i", "ii", "iii", "iv", "v",
})


def _title_tokens(text: str) -> frozenset:
    """Lowercase significant tokens from a job title or keyword phrase."""
    return frozenset(
        t for t in re.split(r"[^a-z]+", text.lower()) if t and t not in _TITLE_STOPWORDS
    )


def _is_off_target(role: str) -> bool:
    """Return True only if the job title shares NO significant token with any tracked keyword.

    Conservative: if config has no keywords, or if there is ANY token overlap,
    do NOT cap.
    """
    keywords = _config.get_target_keywords()
    if not keywords:
        return False
    role_toks = _title_tokens(role)
    if not role_toks:
        return False
    for kw in keywords:
        if role_toks & _title_tokens(kw):
            return False  # overlap found — not off-target
    return True


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


_taxonomy_alias_set: frozenset | None = None
_soft_label_set: frozenset | None = None


def _reset_caches() -> None:
    """Drop the module-level taxonomy caches so a live re-score (after the user
    edits skills.json) rebuilds them from the current ontology state."""
    global _taxonomy_alias_set, _soft_label_set
    _taxonomy_alias_set = None
    _soft_label_set = None


def _taxonomy_aliases() -> frozenset:
    """Lowercased set of every taxonomy alias — used so a known skill the
    candidate lacks is scored as a gap, never double-counted as 'unclassified'."""
    global _taxonomy_alias_set
    if _taxonomy_alias_set is None:
        _taxonomy_alias_set = frozenset(ontology.load_alias_map().keys())
    return _taxonomy_alias_set


def _soft_labels() -> frozenset:
    """Taxonomy labels flagged soft (Communication, Problem Solving, ...). FIT is
    HARD-skill requirement coverage: soft skills are near-universal and cannot be
    reliably dis-evidenced from a résumé, so counting a soft 'gap' against fit is
    noise. Excluded from both numerator and denominator (surfaced elsewhere)."""
    global _soft_label_set
    if _soft_label_set is None:
        tax = ontology._load_taxonomy()
        _soft_label_set = frozenset(l for l, m in tax.items() if m.get("soft"))
    return _soft_label_set


def _unclassified_acronyms(jd: str, req_start) -> list[str]:
    """Tech-looking acronyms the taxonomy does not know at all — the blind-spot
    tripwire. Taxonomy skills (whether the candidate has them or not) are excluded
    here; an unevidenced known skill is a gap, not an unknown."""
    region = jd[req_start:] if req_start is not None else jd
    known = _taxonomy_aliases()
    out = []
    seen = set()
    for m in _ACRONYM.finditer(region):
        tok = m.group(1)
        low = tok.lower()
        if low in known or tok in _ACRONYM_STOP:
            continue
        if tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _candidate_years():
    """Owner's years of experience from config, or None if not supplied."""
    yrs = getattr(_config, "years_experience", None)
    try:
        return int(yrs) if yrs is not None else None
    except (TypeError, ValueError):
        return None


def _candidate_certs() -> list[str]:
    return [str(c) for c in getattr(_config, "certifications", []) or []]


def _kb(ktype: str, detail: str, required_value, status: str) -> dict:
    return {"type": ktype, "detail": detail,
            "required_value": required_value, "status": status}


def _cmp_years(req_y: int) -> str:
    cand = _candidate_years()
    if cand is None:
        return "unknown"
    return "met" if cand >= req_y else "unmet"


def _cmp_degree() -> str:
    edu = " ".join(getattr(_config, "EDUCATION", []) or []).lower()
    return "met" if re.search(r"bachelor|master|phd|doctorate|degree", edu) else "unknown"


def _cmp_cert(name: str) -> str:
    certs = _candidate_certs()
    if not certs:
        return "unknown"
    return "met" if any(name.lower() in c.lower() for c in certs) else "unmet"


def detect_knockouts(jd: str, req_start) -> list[dict]:
    """Hard prescreening requirements in the JD's requirements region. Each is a
    {type, detail, required_value, status} where status is met/unmet/unknown
    relative to the owner's config. Unknown candidate data -> "unknown", never a
    false "unmet"."""
    region = jd[req_start:] if req_start is not None else jd
    out: list[dict] = []

    # Minimum years of experience — only "N+ years" or "minimum/at least N years"
    # (bare "5 years" is descriptive prose, not a stated minimum).
    yrs = [int(m.group(1)) for m in _MIN_YEARS_PLUS.finditer(region)]
    yrs += [int(m.group(1)) for m in _MIN_YEARS_QUAL.finditer(region)]
    if yrs:
        req_y = max(yrs)
        out.append(_kb("min_years", f"{req_y}+ years", req_y, _cmp_years(req_y)))

    # Degree REQUIRED (explicit requirement word near, no softener).
    for m in _DEGREE_TERM.finditer(region):
        win = region[m.start():min(len(region), m.end() + 50)]
        if _DEGREE_REQUIRED.search(win) and not _DEGREE_SOFTENER.search(win):
            out.append(_kb("degree", m.group(1).lower(), None, _cmp_degree()))
            break

    # Required certification.
    seen_cert = set()
    for m in _CERT_REQ.finditer(region):
        name = m.group(1).lower()
        if name in seen_cert:
            continue
        seen_cert.add(name)
        out.append(_kb("certification", name, None, _cmp_cert(name)))

    # Work authorization / citizenship (subsumes the old citizenship cap). Only a
    # real gate when the owner needs sponsorship and it is not EEO boilerplate.
    if _config.needs_sponsorship:
        cm = _CITIZENSHIP_REQUIRED.search(jd)
        if cm:
            window = jd[max(0, cm.start() - 60):min(len(jd), cm.end() + 60)]
            if not _EEO_CONTEXT.search(window):
                out.append(_kb("work_authorization", cm.group(1).lower(), None, "unmet"))

    # Onsite / relocation — we cannot know the owner's willingness, so "unknown"
    # (surfaced for the user to verify), never an auto-fail.
    if _ONSITE_REQ.search(region):
        out.append(_kb("onsite", "onsite/relocation", None, "unknown"))

    return out


def score_job(role: str, jd: str) -> dict:
    jd_low = jd.lower()
    req_start = _req_start(jd)

    # Requirement set = the FULL taxonomy run over the JD. This is what the job
    # actually asks for — not just skills the candidate happens to have. Each
    # requirement is then split into evidenced / gap / curated-exclusion.
    req = ontology.match_text_detailed(jd)

    supported_w = 0.0   # evidenced HARD requirements (candidate HAS them)
    gap_w = 0.0         # known HARD requirements the candidate does NOT evidence
    supported_soft_w = 0.0   # evidenced soft requirements
    gap_soft_w = 0.0         # soft requirements not evidenced
    supported_labels: list[str] = []
    gap_labels: list[str] = []
    curated_lacked: list[str] = []
    soft = _soft_labels()

    for label, info in req.items():
        in_region = req_start is not None and info["first_pos"] >= req_start
        imp = (2 if in_region else 1) * min(info["freq"], 3)
        if label in soft:
            # Soft requirements enter the ratio at a fraction (see _soft_weight),
            # role/seniority-weighted below.
            if label in _config.TERM_BANK:
                supported_soft_w += imp
                supported_labels.append(label)
            else:
                gap_soft_w += imp * GAP_DISCOUNT
            continue
        if label in _config.TERM_BANK:
            supported_w += imp
            supported_labels.append(label)
        elif label in UNSUPPORTED_BANK:
            # Curated hard exclusion — counted by _lacked_weight (heavier), not here.
            curated_lacked.append(label)
        else:
            gap_w += imp * GAP_DISCOUNT
            gap_labels.append(label)

    lacked_w = _lacked_weight(jd_low, req_start, curated_lacked)
    unclassified = _unclassified_acronyms(jd, req_start)
    unclassified_w = 2 * len(unclassified)

    # Blend hard + role-weighted soft coverage. Hard skills count at full weight;
    # soft skills at w_soft (in both numerator and denominator) so a soft-heavy JD
    # raises soft's share and a candidate with no soft evidence loses more of it on
    # a manager role (high w_soft) than an IC role (low w_soft).
    w_soft = _soft_weight(role, jd, req_start)
    hard_w = supported_w + gap_w + lacked_w + unclassified_w
    soft_total_w = supported_soft_w + gap_soft_w
    total_w = hard_w + w_soft * soft_total_w
    num = supported_w + w_soft * supported_soft_w
    if total_w == 0:
        # JD surfaced no recognised requirements at all — no basis to match on.
        fit = 0.0
        confidence = "low"
    else:
        fit = 100.0 * num / (total_w + SMOOTH)
        confidence = "ok" if (req_start is not None and total_w >= 4) else "low"

    caps = []
    # Knockout gates — the requirements real ATS auto-reject on. A confirmed-unmet
    # work-auth keeps the hard 25 cap; any other confirmed-unmet knockout caps
    # below KEEP_THRESHOLD (the job will auto-reject on the form). "unknown"
    # knockouts never cap — they are surfaced for the user to verify.
    knockouts = detect_knockouts(jd, req_start)
    unmet = [k for k in knockouts if k["status"] == "unmet"]
    auto_reject_risk = bool(unmet)
    if any(k["type"] == "work_authorization" for k in unmet):
        fit = min(fit, 25.0)
        caps.append("work authorization required")
    elif unmet:
        fit = min(fit, 35.0)
        caps.append("auto-reject risk: " + "; ".join(k["detail"] for k in unmet))
    if _config.seniority_cap is not None and req_start is not None and _SENIORITY_SENIOR.search(jd[req_start:]):
        fit *= 0.85
        caps.append("senior/lead level")
    # FIX A2: off-target role soft cap (0.7×) when the job title shares no significant
    # token with ANY of the user's tracked role keywords.  Skip if no keywords configured.
    if _is_off_target(role):
        fit *= 0.7
        caps.append("off-target role")

    fit = round(max(0.0, min(100.0, fit)))
    return {
        "fit": fit,
        "band": _band(fit),
        "supported": sorted(supported_labels),
        "lacked": sorted(set(gap_labels) | set(curated_lacked)),
        "unclassified": unclassified,
        "confidence": confidence,
        "caps": caps,
        "coverage_pct": round(100 * num / total_w) if total_w else 0,
        "knockouts": knockouts,
        "auto_reject_risk": auto_reject_risk,
    }


def _band(fit: int) -> str:
    if fit >= STRONG_THRESHOLD:
        return "strong"
    if fit >= KEEP_THRESHOLD:
        return "fair"
    return "drop"


def _why_keep(r: dict, family: str) -> str:
    skills = ", ".join(r["supported"][:3]) if r["supported"] else "no evidenced must-haves"
    msg = f"{r['band']} fit {r['fit']}% - {family.replace('_', ' ')}; evidences {skills}"
    if r["lacked"]:
        msg += f"; missing {', '.join(r['lacked'][:3])}"
    if r["caps"]:
        msg += f"; capped: {', '.join(r['caps'])}"
    if r.get("auto_reject_risk"):
        unmet = [k["detail"] for k in r["knockouts"] if k["status"] == "unmet"]
        msg = f"AUTO-REJECT RISK (needs {', '.join(unmet)}); " + msg
    verify = [k["detail"] for k in r.get("knockouts", []) if k["status"] == "unknown"]
    if verify:
        msg += f"; verify: {', '.join(verify)}"
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
            "auto_reject_risk": "yes" if r["auto_reject_risk"] else "",
            "knockouts": "; ".join(
                f"{k['type']}:{k['detail']}({k['status']})" for k in r["knockouts"]
            ),
        })

    kept.sort(key=lambda x: (-int(x["match_score"]), x["company"], x["role_title"]))

    fieldnames = [
        "company", "role_title", "location", "job_url",
        "match_score", "match_band", "matched_evidence", "gaps", "why_keep",
        "confidence", "unclassified_requirements",
        "auto_reject_risk", "knockouts",
    ]
    with MATCHED.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)

    scores = [int(x["match_score"]) for x in kept]
    rng = f"{min(scores)}-{max(scores)}" if scores else "n/a"
    print(f"Scored {len(rows)} jobs: {len(kept)} kept (Fit >= {KEEP_THRESHOLD}), {dropped} dropped")
    print(f"Fit range: {rng}")
    if not scores:
        n_supported = len(_config.TERM_BANK)
        print(f"[!] 0 jobs cleared Fit >= {KEEP_THRESHOLD}. Your profile evidences {n_supported} skill(s).")
        if n_supported < _config.LOW_SKILL_FLOOR:
            print("    Likely cause: too few skills detected - add more detail to your résumé "
                  "(tools, technologies, methods) and re-upload.")
        else:
            print("    Your profile looks fine; none of these postings matched. "
                  "Try broader search keywords or check back tomorrow.")
    print(f"Written to {MATCHED}")


if __name__ == "__main__":
    main()
