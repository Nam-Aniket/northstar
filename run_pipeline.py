#!/usr/bin/env python3
"""Chain pipeline stages: discover → fill_missing_jds → prepare_job_posts → score_jobs → generate_accepted_resumes.

Usage:
  python3 run_pipeline.py                    # run all stages (default: from discover)
  python3 run_pipeline.py --from fetch       # skip discover, run fetch + prepare + score + generate
  python3 run_pipeline.py --from prepare     # skip discover+fetch, run prepare + score + generate
  python3 run_pipeline.py --from score       # skip fetch+prepare, run score + generate
  python3 run_pipeline.py --from generate    # run only generate (needs matched_jobs.csv)

Prerequisites by stage:
  discover — LinkedIn guest scrape; non-fatal (pipeline continues on failure)
  fetch    — needs job_posts_enriched.csv; non-fatal (pipeline continues on failure)
  prepare  — needs job_posts_enriched.csv (from merge_fetched_jds.py or jd_resolver.py)
  score    — needs job_posts.csv (from prepare)
  generate — needs matched_jobs.csv (from score) and job_posts.csv
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

STAGES = ["discover", "fetch", "prepare", "score", "generate"]
SCRIPTS = {
    "discover": ROOT / "00_search_linkedin_guest.py",
    "fetch": ROOT / "fill_missing_jds.py",
    "prepare": ROOT / "prepare_job_posts.py",
    "score": ROOT / "score_jobs.py",
    "generate": ROOT / "generate_accepted_resumes.py",
}

# Stages that are non-fatal: pipeline continues even if they exit non-zero
# (discovery = LinkedIn scrape, fetch = JD backfill -- both network-dependent).
_NON_FATAL = {"discover", "fetch"}


def run_stage(name: str) -> None:
    script = SCRIPTS[name]
    print(f"\n{'='*60}")
    print(f"  Stage: {name}  ({script.name})")
    print("="*60)
    result = subprocess.run([sys.executable, str(script)], cwd=str(ROOT))
    if result.returncode != 0:
        if name in _NON_FATAL:
            print(f"\n[!] Stage '{name}' exited {result.returncode} — continuing (non-fatal).")
            return
        sys.exit(f"\n[!] Stage '{name}' failed (exit {result.returncode}) — pipeline stopped.")
    print(f"[✓] {name} done")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run job pipeline stages in order.")
    ap.add_argument(
        "--from", dest="from_stage", choices=STAGES, default="discover",
        metavar="STAGE",
        help=f"Start from this stage. Choices: {', '.join(STAGES)}. Default: discover",
    )
    args = ap.parse_args()

    start_idx = STAGES.index(args.from_stage)
    stages_to_run = STAGES[start_idx:]

    print(f"Pipeline: {' → '.join(stages_to_run)}")
    for stage in stages_to_run:
        run_stage(stage)
    print("\nPipeline complete.")


if __name__ == "__main__":
    main()
