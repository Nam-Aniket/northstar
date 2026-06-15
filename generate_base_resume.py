#!/usr/bin/env python3
"""Generate a single generic resume for direct ATS submission.

Writes:  resumes/<name_slug>_<Label>.docx
         resumes/<name_slug>_<Label>_Cover_Letter.docx

Use this when you need a clean base document — for ATS upload portals or a
role you haven't scored yet. With no JD to tailor against, bullets fall back
to the authored-priority order of the fact bank.

For tailored per-company resumes run generate_accepted_resumes.py instead.
"""

import argparse
import re

import config
from generate_accepted_resumes import (
    EXPERIENCE_SLOTS,
    FACT_BANK,
    OUT_ROOT,
    _facts_override,
    compact_skills,
    projects_for,
    select_bullets,
    summary_for,
    write_cover_letter_docx,
    write_resume,
)


def generate_base_resume(label: str, family: str) -> None:
    if not config.generation_enabled:
        print("Resume generation is OFF for this profile. Enable it in config.json (matching.generation_enabled).")
        return
    if _facts_override is None:
        print("Resume generation is ON but no resume data found. Upload your resume in onboarding "
              "to generate resumes (skipping - will not build from placeholder data).")
        return

    OUT_ROOT.mkdir(exist_ok=True)

    no_hits: dict = {}  # no JD -> selection falls back to authored order
    content = {
        "family": family,
        "jd_hits": no_hits,
        "unsupported": [],
        "subtitle": label,
        "summary": summary_for(family, no_hits),
        "skills_lines": compact_skills(family, no_hits),
        "bullets_by_slot": {
            slot: [e["text"] for e in select_bullets(slot, no_hits)]
            for _, slot in EXPERIENCE_SLOTS
        },
        "projects": projects_for(family),
    }

    slug = label.replace(" ", "_")
    slug_name = re.sub(r"[^a-z0-9]+", "_", config.NAME.lower()).strip("_")
    resume_path = OUT_ROOT / f"{slug_name}_{slug}.docx"
    cl_path = OUT_ROOT / f"{slug_name}_{slug}_Cover_Letter.docx"

    write_resume(content, resume_path)
    write_cover_letter_docx(content, "your organisation", label, "", cl_path)

    print(f"Resume:       {resume_path}")
    print(f"Cover letter: {cl_path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Generate a generic base resume for direct ATS submission.")
    ap.add_argument("--family", default="data_analyst",
                    choices=["data_analyst", "reporting_bi", "business_analyst",
                             "engineering_bi", "commercial_analytics", "data_scientist"],
                    help="Role family for tailoring (default: data_analyst)")
    ap.add_argument("--label", default="Data Analyst",
                    help="Role label for the resume title (default: 'Data Analyst')")
    args = ap.parse_args()
    generate_base_resume(args.label, args.family)
