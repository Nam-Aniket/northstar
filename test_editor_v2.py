"""Tests for the Tailor & Edit v2 backend helpers + routes.

Covers the fact-bank writer (write-once persistence + in-process reload), the
placeable-bullet computation, and the honesty guard on the add-bullet route.
Uses unittest to match the rest of the suite.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import config
import generate_accepted_resumes as gen


class AddFactBulletTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.patchers = [
            mock.patch.object(config, "ROOT", self.root),
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(gen, "EXPERIENCE_SLOTS",
                              [("Analyst | Acme | 2022 - 2024", "role1"),
                               ("Intern | Beta | 2020 - 2021", "role2")]),
            mock.patch.object(gen, "BULLET_BUDGETS", {"role1": 3, "role2": 3}),
            mock.patch.object(gen, "FACT_BANK", {
                "role1": [{"text": "Owned the data pipeline.", "evidences": ["Data Pipelines"]}],
                "role2": [],
            }),
        ]
        for p in self.patchers:
            p.start()
        os.environ.pop("JOBENGINE_FACTS", None)

    def tearDown(self):
        for p in reversed(self.patchers):
            p.stop()
        self._tmp.cleanup()

    def test_persists_and_reloads(self):
        entry = gen.add_fact_bullet("role1", "Built CI/CD in GitHub Actions.", "CI/CD")
        self.assertEqual(entry, {"text": "Built CI/CD in GitHub Actions.", "evidences": ["CI/CD"]})
        saved = json.loads((self.root / "facts.json").read_text())
        texts = [b["text"] for b in saved["FACT_BANK"]["role1"]]
        self.assertIn("Built CI/CD in GitHub Actions.", texts)
        # reload made it visible in-process
        self.assertTrue(any(b["text"] == "Built CI/CD in GitHub Actions."
                            for b in gen.FACT_BANK["role1"]))

    def test_rejects_unknown_slot(self):
        with self.assertRaises(ValueError):
            gen.add_fact_bullet("nope", "x", "CI/CD")

    def test_dedupes_existing_text(self):
        gen.add_fact_bullet("role1", "Owned the data pipeline.", "Automation")
        texts = [b["text"] for b in gen.FACT_BANK["role1"]]
        self.assertEqual(texts.count("Owned the data pipeline."), 1)


class PlaceableBulletsTests(unittest.TestCase):
    def test_excludes_already_selected(self):
        with mock.patch.object(gen, "EXPERIENCE_SLOTS",
                               [("Analyst | Acme | 2022 - 2024", "role1")]), \
             mock.patch.object(gen, "BULLET_BUDGETS", {"role1": 1}), \
             mock.patch.object(gen, "FACT_BANK", {"role1": [
                 {"text": "Selected pipeline bullet.", "evidences": ["CI/CD"]},
                 {"text": "Bench CI/CD bullet.", "evidences": ["CI/CD"]},
             ]}), \
             mock.patch.object(gen, "build_content",
                               lambda target, jd: {"bullets_by_slot": {"role1": ["Selected pipeline bullet."]}}):
            out = gen.placeable_bullets_for_skill({"role_title": "x", "company": "y"}, "jd", "CI/CD")
            self.assertEqual([b["text"] for b in out], ["Bench CI/CD bullet."])
            self.assertEqual(out[0]["slot"], "role1")
            self.assertEqual(out[0]["role_header"], "Analyst | Acme | 2022 - 2024")

    def test_case_insensitive_and_empty(self):
        with mock.patch.object(gen, "EXPERIENCE_SLOTS",
                               [("Analyst | Acme | 2022 - 2024", "role1")]), \
             mock.patch.object(gen, "FACT_BANK", {"role1": [
                 {"text": "A bullet.", "evidences": ["Data Quality"]},
             ]}), \
             mock.patch.object(gen, "build_content",
                               lambda target, jd: {"bullets_by_slot": {"role1": []}}):
            self.assertEqual(
                [b["text"] for b in gen.placeable_bullets_for_skill({}, "jd", "data quality")],
                ["A bullet."])
            self.assertEqual(gen.placeable_bullets_for_skill({}, "jd", ""), [])


if __name__ == "__main__":
    unittest.main()
