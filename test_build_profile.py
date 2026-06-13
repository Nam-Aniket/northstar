#!/usr/bin/env python3
"""Deterministic tests for build_profile.py."""

import os
# Point config at example files so this module is safe to import standalone
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")

import json
import tempfile
import unittest
from pathlib import Path

from build_profile import match_resume, build_skills_json, _load_taxonomy

TAXONOMY_PATH = Path(__file__).resolve().parent / "taxonomy.json"


class TestMatchResume(unittest.TestCase):
    """Deterministic matching against the shipped taxonomy."""

    def setUp(self):
        self.taxonomy = _load_taxonomy(TAXONOMY_PATH)

    def test_core_tools_detected(self):
        """Power BI, Tableau, SQL, Python must all be found."""
        text = "Built dashboards in Power BI and Tableau, strong SQL and Python"
        present = match_resume(text, self.taxonomy)
        self.assertIn("Power BI", present)
        self.assertIn("Tableau", present)
        self.assertIn("SQL", present)
        self.assertIn("Python", present)

    def test_absent_skill_not_detected(self):
        """Kafka must not appear when it is not mentioned."""
        text = "Built dashboards in Power BI and Tableau, strong SQL and Python"
        present = match_resume(text, self.taxonomy)
        self.assertNotIn("Kafka", present)

    def test_au_spelling_data_visualisation(self):
        """Australian spelling 'data visualisation' must map to Data Visualisation."""
        text = "Produced data visualisation reports for the executive team."
        present = match_resume(text, self.taxonomy)
        self.assertIn("Data Visualisation", present)

    def test_us_spelling_data_visualization(self):
        """US spelling 'data visualization' must also map to Data Visualisation."""
        text = "Expert in data visualization using Tableau."
        present = match_resume(text, self.taxonomy)
        self.assertIn("Data Visualisation", present)

    def test_business_partnering_alias(self):
        """'business partnering' must map to Stakeholder Management."""
        text = "Extensive business partnering with finance and operations."
        present = match_resume(text, self.taxonomy)
        self.assertIn("Stakeholder Management", present)

    def test_dimensional_model_alias(self):
        """'dimensional model' must map to Data Modelling."""
        text = "Designed a dimensional model for the enterprise warehouse."
        present = match_resume(text, self.taxonomy)
        self.assertIn("Data Modelling", present)

    def test_word_boundary_r_not_false_matched(self):
        """Single 'r' inside words must not match the R skill."""
        text = "Requirements: 3 years experience. R&D team. Section 4(r)."
        present = match_resume(text, self.taxonomy)
        self.assertNotIn("R", present)

    def test_r_programming_matched(self):
        """'r programming' must match the R skill."""
        text = "Proficiency in R programming required."
        present = match_resume(text, self.taxonomy)
        self.assertIn("R", present)


class TestBuildSkillsJson(unittest.TestCase):
    """Output shape tests."""

    def setUp(self):
        self.taxonomy = _load_taxonomy(TAXONOMY_PATH)

    def test_present_in_supported(self):
        """Labels in present set appear in supported_skills."""
        present = {"SQL", "Python"}
        result = build_skills_json(present, self.taxonomy)
        self.assertIn("SQL", result["supported_skills"])
        self.assertIn("Python", result["supported_skills"])

    def test_absent_in_unsupported(self):
        """Labels not in present appear in unsupported_skills."""
        present = {"SQL"}
        result = build_skills_json(present, self.taxonomy)
        self.assertIn("Kafka", result["unsupported_skills"])
        self.assertNotIn("Kafka", result["supported_skills"])

    def test_supported_shape(self):
        """supported_skills entries must have aliases and group keys."""
        present = {"Power BI"}
        result = build_skills_json(present, self.taxonomy)
        entry = result["supported_skills"]["Power BI"]
        self.assertIn("aliases", entry)
        self.assertIn("group", entry)

    def test_unsupported_shape(self):
        """unsupported_skills entries must be lists of alias strings."""
        present = set()
        result = build_skills_json(present, self.taxonomy)
        kafka_entry = result["unsupported_skills"]["Kafka"]
        self.assertIsInstance(kafka_entry, list)
        self.assertTrue(all(isinstance(a, str) for a in kafka_entry))

    def test_soft_flag_preserved(self):
        """soft:true must be preserved in supported_skills when present in taxonomy."""
        present = {"Stakeholder Management"}
        result = build_skills_json(present, self.taxonomy)
        entry = result["supported_skills"]["Stakeholder Management"]
        self.assertTrue(entry.get("soft"), "soft flag should be True for Stakeholder Management")

    def test_no_label_in_both_sections(self):
        """No label may appear in both supported and unsupported."""
        present = {"SQL", "Python", "Power BI"}
        result = build_skills_json(present, self.taxonomy)
        overlap = set(result["supported_skills"]) & set(result["unsupported_skills"])
        self.assertEqual(overlap, set(), f"Labels in both sections: {overlap}")

    def test_every_taxonomy_label_in_output(self):
        """Every taxonomy label must appear in exactly one section."""
        present = {"SQL", "Python"}
        result = build_skills_json(present, self.taxonomy)
        covered = set(result["supported_skills"]) | set(result["unsupported_skills"])
        self.assertEqual(covered, set(self.taxonomy.keys()))

    def test_output_round_trips_as_json(self):
        """Output must be JSON-serialisable."""
        present = {"SQL", "Tableau"}
        result = build_skills_json(present, self.taxonomy)
        serialised = json.dumps(result)
        parsed = json.loads(serialised)
        self.assertEqual(set(parsed["supported_skills"]), present)


class TestCLI(unittest.TestCase):
    """Integration test: run the CLI against a fixture string and check the written file."""

    def test_cli_text_flag_writes_correct_file(self):
        """--text flag produces a file with the right supported/unsupported split."""
        import subprocess, sys
        fixture = "Built dashboards in Power BI and Tableau, strong SQL and Python"
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            out_path = tmp.name

        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parent / "build_profile.py"),
                    "--text", fixture,
                    "--out", out_path,
                ],
                capture_output=True,
                text=True,
                cwd=str(Path(__file__).resolve().parent),
            )
            self.assertEqual(result.returncode, 0, f"CLI exited non-zero:\n{result.stderr}")
            with open(out_path) as f:
                skills = json.load(f)
            self.assertIn("Power BI", skills["supported_skills"])
            self.assertIn("Tableau", skills["supported_skills"])
            self.assertIn("SQL", skills["supported_skills"])
            self.assertIn("Python", skills["supported_skills"])
            self.assertNotIn("Kafka", skills["supported_skills"])
            self.assertIn("Kafka", skills["unsupported_skills"])
        finally:
            Path(out_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
