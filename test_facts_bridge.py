#!/usr/bin/env python3
"""Tests for facts_bridge.build_facts().

Gate conditions (all must pass):
  (a) build_facts output passes config.validate_fact_bank() without raising.
  (b) No slot has duplicate first-word openers.
  (c) All evidences labels are in config.TERM_BANK.
  (d) A one-JD dry run of the generator produces a non-empty résumé body.

Run with:
  python -m unittest test_facts_bridge
"""

import os
import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

# Point to the example files so the test is self-contained and deterministic.
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")

# facts.json must NOT be active during test setup (we build our own).
# We'll use JOBENGINE_FACTS to redirect the generator to our temp file.

import config
from facts_bridge import build_facts, save_facts


# ---------------------------------------------------------------------------
# Synthetic parsed résumé
# ---------------------------------------------------------------------------

_PARSED = {
    "name": "Test Candidate",
    "email": "test@example.com",
    "phone": "0400 000 000",
    "location": "Melbourne, VIC",
    "linkedin": "",
    "summary": "Data analyst with SQL and Python experience.",
    "skills": "SQL, Python, Power BI",
    "experiences": [
        {
            "role": "Data Analyst",
            "company": "Acme Corp",
            "dates": "2022 - 2024",
            "bullets": [
                # Two bullets starting with the same word — second must be dropped.
                "Built SQL dashboards for operational reporting across 10 business units.",
                "Built Python pipelines that automated monthly reconciliation reports.",
                # One bullet with an ESCO-only skill label that is NOT in TERM_BANK.
                # 'data warehousing' maps to 'Data Warehousing' which IS in TERM_BANK;
                # use a term that only exists in ESCO and not in skills.example.json.
                "Analysed trends in customer data using statistical methods and R programming.",
            ],
        },
        {
            "role": "Junior Analyst",
            "company": "Beta Ltd",
            "dates": "2020 - 2022",
            "bullets": [
                "Developed Power BI dashboards used by 5 teams for weekly KPI reporting.",
                "Conducted ETL processes to clean and transform raw data from multiple sources.",
                "Presented findings to stakeholders and documented data-quality exceptions.",
            ],
        },
        {
            "role": "Intern",
            "company": "Gamma Inc",
            "dates": "2019 - 2020",
            "bullets": [
                "Supported ad hoc data analysis requests using Excel and SQL queries.",
                "Prepared weekly reports on data quality metrics for the data governance team.",
            ],
        },
    ],
    "education": [
        {"degree": "Bachelor of Commerce", "school": "University of Melbourne", "year": "2019", "gpa": ""}
    ],
}


class TestBuildFacts(unittest.TestCase):

    def setUp(self):
        self.facts = build_facts(_PARSED)

    # ------------------------------------------------------------------
    # (a) validate_fact_bank passes without raising
    # ------------------------------------------------------------------

    def test_a_validate_fact_bank_passes(self):
        """build_facts output must satisfy the generator's structural guard."""
        import generate_accepted_resumes as gar

        # Temporarily override the module-level FACT_BANK / BULLET_BUDGETS /
        # EXPERIENCE_SLOTS so validate_fact_bank() inspects OUR facts.
        orig_fb = gar.FACT_BANK
        orig_bb = gar.BULLET_BUDGETS
        orig_es = gar.EXPERIENCE_SLOTS

        try:
            gar.FACT_BANK = self.facts["FACT_BANK"]
            gar.BULLET_BUDGETS = self.facts["BULLET_BUDGETS"]
            gar.EXPERIENCE_SLOTS = [tuple(s) for s in self.facts["EXPERIENCE_SLOTS"]]
            # Should not raise.
            gar.validate_fact_bank()
        finally:
            gar.FACT_BANK = orig_fb
            gar.BULLET_BUDGETS = orig_bb
            gar.EXPERIENCE_SLOTS = orig_es

    # ------------------------------------------------------------------
    # (b) No slot has duplicate first-word openers
    # ------------------------------------------------------------------

    def test_b_no_duplicate_openers(self):
        """No slot may contain two bullets sharing the same first word."""
        for slot_key, pool in self.facts["FACT_BANK"].items():
            openers = [e["text"].split()[0].lower() for e in pool if e["text"].split()]
            dupes = [w for w, n in Counter(openers).items() if n > 1]
            self.assertEqual(
                dupes, [],
                f"Slot {slot_key!r} has duplicate openers: {dupes}",
            )

    # ------------------------------------------------------------------
    # (c) All evidences labels are in TERM_BANK
    # ------------------------------------------------------------------

    def test_c_evidences_in_term_bank(self):
        """Every evidences label must be a key in config.TERM_BANK."""
        term_bank_labels = set(config.TERM_BANK.keys())
        for slot_key, pool in self.facts["FACT_BANK"].items():
            for entry in pool:
                unknown = [lbl for lbl in entry["evidences"] if lbl not in term_bank_labels]
                self.assertEqual(
                    unknown, [],
                    f"Slot {slot_key!r} bullet has unknown evidence labels: {unknown}",
                )

    # ------------------------------------------------------------------
    # (d) One-JD dry run produces a non-empty résumé body
    # ------------------------------------------------------------------

    def test_d_generator_dry_run_produces_nonempty_body(self):
        """A single-job generation call using these facts must produce a docx."""
        import generate_accepted_resumes as gar

        tmp_dir = Path(tempfile.mkdtemp(prefix="facts_bridge_test_"))
        facts_file = tmp_dir / "facts.json"
        save_facts(self.facts, tmp_dir)

        # Also need matched_jobs.csv and job_posts.csv in a temp root.
        import csv
        matched_path = tmp_dir / "matched_jobs.csv"
        jobs_path = tmp_dir / "job_posts.csv"
        resumes_dir = tmp_dir / "resumes"
        resumes_dir.mkdir()

        tiny_jd = (
            "Requirements: strong SQL and Python skills to build Power BI dashboards "
            "and perform data quality checks. Stakeholder management and ETL experience valued."
        )
        row = {
            "company": "TestCo",
            "role_title": "Data Analyst",
            "match_score": "80",
            "match_band": "strong",
            "job_url": "https://example.com/job/1",
            "location": "Melbourne",
            "matched_evidence": "SQL, Python",
            "gaps": "",
            "why_keep": "strong match",
            "job_text": tiny_jd,
        }
        fieldnames = list(row.keys())
        for path, rows in [(matched_path, [row]), (jobs_path, [row])]:
            with path.open("w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

        # Monkeypatch the generator to use our temp root and facts.
        orig_fb = gar.FACT_BANK
        orig_bb = gar.BULLET_BUDGETS
        orig_es = gar.EXPERIENCE_SLOTS
        orig_matched = gar.MATCHED
        orig_jobs = gar.JOBS
        orig_out_root = gar.OUT_ROOT

        try:
            gar.FACT_BANK = self.facts["FACT_BANK"]
            gar.BULLET_BUDGETS = self.facts["BULLET_BUDGETS"]
            gar.EXPERIENCE_SLOTS = [tuple(s) for s in self.facts["EXPERIENCE_SLOTS"]]
            gar.MATCHED = matched_path
            gar.JOBS = jobs_path
            gar.OUT_ROOT = resumes_dir

            job_posts = gar.read_csv(jobs_path)
            priors = gar.family_skill_priors(job_posts)
            tracking_row, body_str = gar.generate_one(row, job_posts, priors)

            self.assertTrue(body_str.strip(), "Generator returned an empty body string")
            resume_file = resumes_dir / tracking_row["resume_file"]
            self.assertTrue(resume_file.exists(), f"Resume docx not created: {resume_file}")

        finally:
            gar.FACT_BANK = orig_fb
            gar.BULLET_BUDGETS = orig_bb
            gar.EXPERIENCE_SLOTS = orig_es
            gar.MATCHED = orig_matched
            gar.JOBS = orig_jobs
            gar.OUT_ROOT = orig_out_root
            shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
