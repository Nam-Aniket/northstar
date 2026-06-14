#!/usr/bin/env python3
"""build_esco.py — Transform the ESCO tabiya CSV into esco/skills_en.csv.

Usage:
    python scripts/build_esco.py
    python scripts/build_esco.py --input /tmp/tabiya_skills.csv --output esco/skills_en.csv
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Denylist — generic standalone words that would over-match in résumés / JDs
# ---------------------------------------------------------------------------

DENYLIST: set[str] = {
    "management", "research", "support", "planning", "development",
    "communication", "analysis", "design", "training", "monitoring",
    "reporting", "coordination", "leadership", "documentation", "assessment",
    "evaluation", "plan", "develop", "manage", "supervise", "organise",
    "organize", "maintain", "prepare", "review", "ensure", "provide",
    "deliver", "implement", "conduct", "perform", "process", "service",
    "services", "system", "systems", "team", "work", "quality", "control",
    "operations", "administration",
    # Abstract heads that must never stand alone (also rejected as derived
    # phrase tails via GENERIC_HEADS below).
    "procedures", "principles", "practices", "activities", "standards",
    "requirements", "compliance", "regulations", "information", "data",
    "staff", "duties", "tasks", "processes",
    # Whole-phrase FP-control denylist — generic boilerplate compounds that
    # slip through verb-strip / single-token guards and fire on ordinary prose.
    "human resources", "financial reports", "customer service",
    "data analysis", "project work", "team support", "daily operations",
    "general administration", "best practices",
}

# ---------------------------------------------------------------------------
# VERB-STRIP — derive a bare-noun alias from occupational verb phrases.
#
# ESCO labels are written as occupational VERB phrases ("provide patient care",
# "ensure pharmacovigilance"). Résumés use the BARE noun ("patient care",
# "pharmacovigilance"). At build time we strip a leading skill-verb (and an
# optional following particle) to emit the bare-noun phrase as an extra alias.
# Pure stdlib, no NLP libraries.
# ---------------------------------------------------------------------------

# Closed set of leading verbs we strip. Conservative on purpose.
SKILL_VERBS: frozenset[str] = frozenset({
    "ensure", "manage", "provide", "perform", "administer", "conduct",
    "develop", "maintain", "operate", "apply", "monitor", "coordinate",
    "use", "supervise", "oversee", "prepare", "deliver", "implement",
    "plan", "design", "organise", "organize", "review", "assess",
    "evaluate", "control", "carry", "make", "identify", "support",
    "build", "create", "establish", "lead", "direct", "handle",
    "process", "utilise", "utilize",
})

# Drop ONE of these if it immediately follows the stripped verb
# (e.g. "carry out maintenance", "provide for safety").
PARTICLES: frozenset[str] = frozenset({
    "out", "up", "of", "the", "a", "an", "with", "for", "in", "on",
})

# Reject a derived phrase whose LAST token is one of these abstract heads —
# the residue would over-match generic prose.
GENERIC_HEADS: frozenset[str] = frozenset({
    "procedures", "principles", "practices", "activities", "standards",
    "requirements", "compliance", "regulations", "information", "data",
    "staff", "duties", "services", "systems", "processes", "operations",
    "management", "work", "tasks", "support",
    # FP-control additions — over-matching tails that fire on ordinary prose.
    "needs", "performance", "facilities", "strategy", "strategies",
    "plan", "plans", "resources",
})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Import ontology from the repo root (this script lives in scripts/)
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import ontology  # noqa: E402


def keep_alias(a: str) -> bool:
    n = ontology.normalize(a)
    return len(n) >= 3 and not n.isdigit() and n not in DENYLIST


def _deinflect_gerund(token: str) -> str | None:
    """Map a gerund to its base verb form, or None if not a clean gerund.

    'managing' -> 'manage', 'monitoring' -> 'monitor'. We only need to recover
    enough to test membership in SKILL_VERBS, so we try both the bare stem and
    the stem + 'e'.
    """
    if not token.endswith("ing") or len(token) <= 4:
        return None
    stem = token[:-3]            # 'manag', 'monitor'
    if stem in SKILL_VERBS:      # 'monitor'
        return stem
    if (stem + "e") in SKILL_VERBS:  # 'manag' -> 'manage'
        return stem + "e"
    return None


def derive_bare_noun(phrase: str, existing: set[str]) -> str | None:
    """Strip a leading skill-verb (+ optional particle) to a bare-noun alias.

    Returns the derived alias (already normalized) or None when nothing safe
    can be derived. `existing` is the set of normalized aliases already emitted
    for this row, used to avoid duplicates.
    """
    original = ontology.normalize(phrase)
    if not original:
        return None
    tokens = original.split()
    if not tokens:
        return None

    lead = tokens[0]
    if lead in SKILL_VERBS:
        tokens = tokens[1:]
    else:
        base = _deinflect_gerund(lead)
        if base is None:
            return None  # no verb lead -> nothing to strip
        tokens = tokens[1:]

    # Drop one trailing particle directly after the stripped verb.
    if tokens and tokens[0] in PARTICLES:
        tokens = tokens[1:]

    # A residue that still leads with a bare article ("for a refund" ->
    # "a refund", "use of a microscope" -> "a microscope") is not a clean
    # bare-noun phrase; drop the dangling article so the alias starts on a
    # content token.
    if tokens and tokens[0] in {"a", "an", "the"}:
        tokens = tokens[1:]

    # Guards.
    if len(tokens) < 2:                    # need >= 2 content tokens
        return None
    if tokens[-1] in GENERIC_HEADS:        # abstract head -> reject
        return None

    derived = " ".join(tokens)
    if derived == original:                # verb must actually have been stripped
        return None
    if len(derived) < 6:                   # too short
        return None
    if derived in existing:                # already an alias
        return None
    if not keep_alias(derived):            # respect min-length / digit / DENYLIST
        return None
    return derived


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build(input_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_read = 0
    skills_written = 0
    derived_count = 0
    # All distinct derived aliases (for the report). Set dedupes across rows;
    # input file order + deterministic guards keep the CSV byte-identical.
    derived_aliases: set[str] = set()
    samples: list[tuple[str, str, str]] = []

    with (
        input_path.open(encoding="utf-8", newline="") as fin,
        output_path.open("w", encoding="utf-8", newline="") as fout,
    ):
        reader = csv.DictReader(fin)
        writer = csv.writer(fout, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
        writer.writerow(["preferredLabel", "altLabels", "skillType"])

        for row in reader:
            rows_read += 1
            pref = row.get("PREFERREDLABEL", "").strip()
            if not pref or not keep_alias(pref):
                continue

            raw_alts = row.get("ALTLABELS", "") or ""
            alts = [
                a.strip()
                for a in raw_alts.split("\n")
                if a.strip() and keep_alias(a.strip())
            ]

            skill_type = row.get("SKILLTYPE", "").strip()

            # VERB-STRIP: derive a bare-noun alias from each candidate phrase
            # (preferredLabel + each surviving altLabel). Track the normalized
            # aliases already present so we never emit a duplicate for this row.
            present = {ontology.normalize(a) for a in [pref, *alts]}
            for candidate in [pref, *alts]:
                derived = derive_bare_noun(candidate, present)
                if derived is None:
                    continue
                alts.append(derived)
                present.add(derived)
                derived_count += 1
                derived_aliases.add(derived)

            writer.writerow([pref, "\n".join(alts), skill_type])
            skills_written += 1

            if len(samples) < 3:
                samples.append((pref, "\n".join(alts[:2]), skill_type))

    # Report: every distinct derived alias, sorted by token-count ascending then
    # alphabetically, so the shortest / most-generic phrases sit at the top for
    # false-positive review.
    report_path = Path(__file__).resolve().parent / "derived_aliases_report.txt"
    report_lines = sorted(
        derived_aliases, key=lambda s: (len(s.split()), s)
    )
    with report_path.open("w", encoding="utf-8", newline="\n") as rf:
        for line in report_lines:
            rf.write(line + "\n")

    print(f"Rows read:      {rows_read}")
    print(f"Skills written: {skills_written}")
    print(f"Derived aliases (total emitted):  {derived_count}")
    print(f"Derived aliases (distinct):       {len(derived_aliases)}")
    print(f"Report:         {report_path}")
    print(f"Output:         {output_path}")
    print()
    print("Sample rows:")
    for pref, alts_preview, stype in samples:
        alts_short = alts_preview.replace("\n", " | ")
        print(f"  [{stype}] {pref!r} -> {alts_short!r}")

    size_bytes = output_path.stat().st_size
    size_mb = size_bytes / 1_048_576
    print(f"\nFile size: {size_bytes:,} bytes ({size_mb:.2f} MB)")

    if size_bytes > 8 * 1_048_576:
        import gzip
        gz_path = output_path.with_suffix(".csv.gz")
        with output_path.open("rb") as f_in, gzip.open(gz_path, "wb") as f_out:
            f_out.write(f_in.read())
        print(f"WARNING: plain file exceeds 8 MB — also wrote {gz_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build esco/skills_en.csv from tabiya ESCO export.")
    parser.add_argument(
        "--input",
        default="/tmp/tabiya_skills.csv",
        help="Path to the raw tabiya/ESCO CSV (default: /tmp/tabiya_skills.csv)",
    )
    parser.add_argument(
        "--output",
        default=str(ROOT / "esco" / "skills_en.csv"),
        help="Destination CSV path (default: esco/skills_en.csv in repo root)",
    )
    args = parser.parse_args()
    build(Path(args.input), Path(args.output))


if __name__ == "__main__":
    main()
