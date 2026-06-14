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
}

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


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def build(input_path: Path, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows_read = 0
    skills_written = 0
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

            writer.writerow([pref, "\n".join(alts), skill_type])
            skills_written += 1

            if len(samples) < 3:
                samples.append((pref, "\n".join(alts[:2]), skill_type))

    print(f"Rows read:      {rows_read}")
    print(f"Skills written: {skills_written}")
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
