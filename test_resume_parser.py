#!/usr/bin/env python3
"""Tests for the deterministic résumé parser (use_llm=False), covering the
parse->render defects fixed 2026-06-15: experience-header over-matching,
education field separation + GPA/WAM, section-header leakage, projects, and the
declared-vs-inferred skills split."""

import os
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")

import unittest

from resume_parser import (
    parse_resume,
    _is_entry_header,
    _extract_education,
    _extract_projects,
)


def _edu_line(e):
    """Mirror of app.app's education-line composition (kept in sync)."""
    parts = [e.get("degree"), e.get("school"), e.get("year")]
    gpa = (e.get("gpa") or "").strip()
    if gpa:
        parts.append(f"{(e.get('gpa_label') or 'GPA').strip()} {gpa}")
    return ", ".join(p.strip() for p in parts if p and p.strip())


class TestExperienceHeader(unittest.TestCase):
    def test_pipe_bullet_not_header(self):
        resume = (
            "EXPERIENCE\n"
            "Data Analyst | Acme Corp | Jan 2021 - Dec 2022\n"
            "- Automated billing reports with Python.\n"
            "Mentored three analyst trainees across six sprint cycles | assigned Jira "
            "tickets, reviewed deliverables, and unblocked SQL/Python issues\n"
            "- Built Power BI dashboards.\n"
        )
        exps = parse_resume(resume)["experiences"]
        self.assertEqual(len(exps), 1, "pipe bullet should not start a new experience")
        self.assertEqual(exps[0]["role"], "Data Analyst")
        self.assertEqual(exps[0]["company"], "Acme Corp")
        self.assertTrue(any("Mentored three analyst" in b for b in exps[0]["bullets"]))

    def test_prose_with_hyphen_is_bullet(self):
        resume = (
            "EXPERIENCE\n"
            "Data Analyst | Acme Corp | Jan 2021 - Dec 2022\n"
            "Worked cross-functionally - delivered the migration on time.\n"
        )
        exps = parse_resume(resume)["experiences"]
        self.assertEqual(len(exps), 1)
        self.assertTrue(any("cross-functionally" in b for b in exps[0]["bullets"]))

    def test_real_header_with_date_still_detected(self):
        self.assertTrue(_is_entry_header("Senior Data Analyst | Telstra | Mar 2019 - Jan 2021"))
        self.assertFalse(_is_entry_header("Reduced costs by 30% in 2023 across the team."))


class TestEducation(unittest.TestCase):
    def test_single_line_split_with_wam(self):
        edu = _extract_education([
            "Master of Data Science, Monash University, Feb 2024 - Dec 2025",
            "WAM: 80.5",
        ])
        self.assertEqual(len(edu), 1)
        e = edu[0]
        self.assertEqual(e["degree"], "Master of Data Science")
        self.assertEqual(e["school"], "Monash University")
        self.assertEqual(e["year"], "2024 - 2025")
        self.assertEqual(e["gpa"], "80.5")
        self.assertEqual(e["gpa_label"], "WAM")
        self.assertEqual(_edu_line(e), "Master of Data Science, Monash University, 2024 - 2025, WAM 80.5")

    def test_two_line_layout(self):
        edu = _extract_education(["Master of Data Science", "Monash University", "2024 - 2025"])
        e = edu[0]
        self.assertEqual(e["degree"], "Master of Data Science")
        self.assertEqual(e["school"], "Monash University")
        self.assertEqual(e["year"], "2024 - 2025")

    def test_gpa_label_variants(self):
        for label, raw, num in [("GPA", "GPA 3.8", "3.8"), ("CGPA", "CGPA: 8.9/10", "8.9/10"), ("WAM", "WAM 75", "75")]:
            edu = _extract_education(["Bachelor of Science, City University, 2016 - 2019", raw])
            self.assertEqual(edu[0]["gpa"], num)
            self.assertEqual(edu[0]["gpa_label"], label)

    def test_no_em_dash_in_composed_date(self):
        edu = _extract_education(["Master of Data Science, Monash University, Feb 2024 – Dec 2025"])
        line = _edu_line(edu[0])
        self.assertNotIn("–", line)
        self.assertNotIn("—", line)


class TestSectionBoundaries(unittest.TestCase):
    def test_trailing_section_does_not_leak_into_education(self):
        resume = (
            "EDUCATION\n"
            "Bachelor of Engineering, Computer Science, Mumbai University, Jul 2016 - Dec 2020\n"
            "CERTIFICATIONS\n"
            "AWS Certified Data Analytics\n"
        )
        p = parse_resume(resume)
        self.assertEqual(len(p["education"]), 1)
        self.assertNotIn("CERTIF", " ".join(
            f"{e['degree']} {e['school']}" for e in p["education"]).upper())

    def test_projects_section_parsed_and_separate(self):
        resume = (
            "EXPERIENCE\n"
            "Data Analyst | Acme | Jan 2021 - Dec 2022\n"
            "- Built reports.\n"
            "PROJECTS\n"
            "Hybrid Recommendation Engine\n"
            "- Built a hybrid recommender combining NCF with TF-IDF.\n"
            "EDUCATION\n"
            "Master of Data Science, Monash University, 2024 - 2025\n"
        )
        p = parse_resume(resume)
        self.assertEqual([q["name"] for q in p["projects"]], ["Hybrid Recommendation Engine"])
        self.assertTrue(p["projects"][0]["bullets"])
        # project content must not bleed into experience or education
        self.assertFalse(any("recommender" in b for e in p["experiences"] for b in e["bullets"]))
        self.assertTrue(all(e["school"] != "PROJECTS" for e in p["education"]))


class TestSkillsSplit(unittest.TestCase):
    def test_declared_vs_inferred(self):
        resume = (
            "SKILLS\n"
            "Python, SQL\n"
            "EXPERIENCE\n"
            "Data Analyst | Acme | Jan 2021 - Dec 2022\n"
            "- Built TensorFlow pipelines and Power BI dashboards.\n"
        )
        p = parse_resume(resume)
        self.assertEqual(p["skills"], "Python, SQL")
        self.assertNotIn("TensorFlow", p["skills"])
        self.assertIn("TensorFlow", p["inferred_skills"])

    def test_no_skills_section_falls_back_to_inferred(self):
        resume = (
            "EXPERIENCE\n"
            "Data Analyst | Acme | Jan 2021 - Dec 2022\n"
            "- Built Power BI dashboards and SQL pipelines.\n"
        )
        p = parse_resume(resume)
        self.assertTrue(p["skills"], "skills should populate from ontology when no SKILLS section")


if __name__ == "__main__":
    unittest.main()
