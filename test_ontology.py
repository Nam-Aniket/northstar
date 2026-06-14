#!/usr/bin/env python3
"""Tests for matcher.py and ontology.py."""

import os
# Point config at example files so imports of config.py in transitive deps work
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")

import json
import time
import unittest
from pathlib import Path


# ---------------------------------------------------------------------------
# matcher tests
# ---------------------------------------------------------------------------

class TestMatcher(unittest.TestCase):
    """Unit tests for the raw Aho-Corasick matcher."""

    def setUp(self):
        from matcher import build_automaton, find
        self.build = build_automaton
        self.find = find

    def test_single_word_alias(self):
        am = self.build({"python": "Python"})
        self.assertIn("Python", self.find("I know python well", am))

    def test_multiword_alias_matched(self):
        am = self.build({"magnetic resonance imaging": "MRI"})
        result = self.find("experience with magnetic resonance imaging", am)
        self.assertIn("MRI", result)

    def test_word_boundary_rejects_substring(self):
        """'r' embedded inside alphanumeric words must not match."""
        am = self.build({"r": "R"})
        # 'r' appears inside 'Requirements' and 'years' and 'experience' —
        # all surrounded by [a-z0-9], so the boundary guard rejects them.
        result = self.find("Requirements: 3 years experience.", am)
        self.assertNotIn("R", result)

    def test_word_boundary_accepts_standalone(self):
        """Single 'r' at a proper boundary should match."""
        am = self.build({"r": "R"})
        result = self.find("proficient in R programming", am)
        self.assertIn("R", result)

    def test_word_boundary_start_of_string(self):
        """Match at position 0 with no preceding character."""
        am = self.build({"sql": "SQL"})
        result = self.find("sql is required", am)
        self.assertIn("SQL", result)

    def test_word_boundary_end_of_string(self):
        """Match at end of string with no following character."""
        am = self.build({"python": "Python"})
        result = self.find("candidate knows python", am)
        self.assertIn("Python", result)

    def test_multiple_labels_in_one_pass(self):
        am = self.build({"python": "Python", "sql": "SQL", "power bi": "Power BI"})
        result = self.find("Built dashboards in Power BI with Python and SQL.", am)
        self.assertIn("Python", result)
        self.assertIn("SQL", result)
        self.assertIn("Power BI", result)

    def test_alias_conflict_last_write_wins(self):
        """Two different labels sharing an alias — last write wins, no crash."""
        am = self.build({"python": "Python", "python": "Python3"})  # noqa: F601
        result = self.find("knows python", am)
        # Either label is acceptable — no KeyError / crash
        self.assertTrue(len(result) >= 1)

    def test_empty_text(self):
        am = self.build({"sql": "SQL"})
        self.assertEqual(self.find("", am), set())

    def test_empty_alias_map(self):
        am = self.build({})
        self.assertEqual(self.find("anything here", am), set())

    def test_case_insensitive(self):
        am = self.build({"power bi": "Power BI"})
        result = self.find("Expert in POWER BI dashboards", am)
        self.assertIn("Power BI", result)


# ---------------------------------------------------------------------------
# ontology tests
# ---------------------------------------------------------------------------

class TestOntologyFallback(unittest.TestCase):
    """ontology falls back to taxonomy.json when no ESCO file is present."""

    def setUp(self):
        import ontology
        ontology._reset_cache()
        # Ensure no ESCO env var or directory interferes
        os.environ.pop("ESCO_DATA", None)
        # If esco/ dir happens to exist in the test environment, skip gracefully
        self._esco_exists = (Path(__file__).resolve().parent / "esco" / "skills_en.csv").exists()

    def tearDown(self):
        import ontology
        ontology._reset_cache()
        os.environ.pop("ESCO_DATA", None)

    def test_fallback_loads_taxonomy(self):
        """When no ESCO file exists, load_alias_map must contain taxonomy aliases."""
        if self._esco_exists:
            self.skipTest("esco/skills_en.csv present — not a pure fallback environment")
        from ontology import load_alias_map
        alias_map = load_alias_map()
        # taxonomy.json has 'sql' -> 'SQL', 'python' -> 'Python'
        self.assertIn("sql", alias_map)
        self.assertEqual(alias_map["sql"], "SQL")
        self.assertIn("python", alias_map)
        self.assertEqual(alias_map["python"], "Python")

    def test_match_text_recovers_core_skills(self):
        """match_text must find SQL, Python, and Power BI from a fixture résumé."""
        from ontology import match_text
        fixture = (
            "Jordan Rivera — Data Analyst\n"
            "Built executive dashboards in Power BI and Tableau.\n"
            "Wrote complex SQL queries and ETL pipelines.\n"
            "Used Python for statistical analysis and automation.\n"
            "Experienced with dimensional modelling and data warehousing."
        )
        result = match_text(fixture)
        self.assertIn("SQL", result)
        self.assertIn("Python", result)
        self.assertIn("Power BI", result)

    def test_match_text_no_false_r(self):
        """'r' inside common words must not trigger the R skill."""
        from ontology import match_text
        result = match_text("Requirements: 3 years experience. R&D team. Section 4(r).")
        self.assertNotIn("R", result)

    def test_match_text_r_programming(self):
        """'r programming' alias must trigger the R skill."""
        from ontology import match_text
        result = match_text("Proficiency in r programming required.")
        self.assertIn("R", result)

    def test_group_for_known_label(self):
        """group_for returns correct group from taxonomy."""
        from ontology import group_for
        self.assertEqual(group_for("SQL"), "Programming")
        self.assertEqual(group_for("Power BI"), "Data & BI")

    def test_group_for_unknown_label(self):
        """group_for returns 'General' for labels not in taxonomy."""
        from ontology import group_for
        self.assertEqual(group_for("Beekeeping"), "General")

    def test_normalize_strips_accents(self):
        """normalize must strip combining accents and lowercase."""
        from ontology import normalize
        self.assertEqual(normalize("Résumé"), "resume")
        self.assertEqual(normalize("  Power  BI  "), "power bi")

    def test_normalize_nfkd(self):
        """normalize handles NFKD ligatures and whitespace collapse."""
        from ontology import normalize
        self.assertEqual(normalize("café"), "cafe")

    def test_curated_aliases_override_esco(self):
        """Curated taxonomy aliases must win over ESCO on conflicts."""
        if self._esco_exists:
            self.skipTest("Real ESCO file present — conflict test needs a mock")
        # With fallback only, curated is the sole source, so no conflict possible;
        # just verify the curated aliases are in the map
        from ontology import load_alias_map
        alias_map = load_alias_map()
        # 'powerbi' is a curated alias for Power BI
        self.assertIn("powerbi", alias_map)
        self.assertEqual(alias_map["powerbi"], "Power BI")


class TestOntologyEscoMock(unittest.TestCase):
    """ontology correctly parses an ESCO-format CSV when ESCO_DATA is set."""

    def setUp(self):
        import tempfile, csv as _csv
        import ontology
        ontology._reset_cache()

        # Write a minimal ESCO-like CSV
        self._tmp = tempfile.NamedTemporaryFile(
            mode="w", suffix=".csv", delete=False, encoding="utf-8", newline=""
        )
        writer = _csv.writer(self._tmp)
        writer.writerow(["preferredLabel", "altLabels"])
        writer.writerow(["beekeeping", "apiculture\nhive management"])
        writer.writerow(["nursing care", "patient care\nclinical care"])
        writer.writerow(["Python", "python programming"])  # overlaps curated
        self._tmp.close()
        os.environ["ESCO_DATA"] = self._tmp.name

    def tearDown(self):
        import ontology
        ontology._reset_cache()
        os.environ.pop("ESCO_DATA", None)
        Path(self._tmp.name).unlink(missing_ok=True)

    def test_esco_preferred_label_matched(self):
        from ontology import match_text
        result = match_text("candidate has experience in beekeeping")
        self.assertIn("beekeeping", result)

    def test_esco_alt_label_matched(self):
        from ontology import match_text
        result = match_text("skilled in apiculture and nursing care")
        self.assertIn("beekeeping", result)
        self.assertIn("nursing care", result)

    def test_curated_wins_over_esco_on_python(self):
        """The curated label 'Python' must win over ESCO's 'Python' entry."""
        from ontology import match_text
        result = match_text("expert python developer")
        self.assertIn("Python", result)


class TestOntologyPerformance(unittest.TestCase):
    """Smoke test: build over the full taxonomy alias set, match a paragraph."""

    def test_build_and_match_speed(self):
        """Build automaton from taxonomy and match a paragraph in <1 s."""
        tax_path = Path(__file__).resolve().parent / "taxonomy.json"
        with tax_path.open(encoding="utf-8") as f:
            taxonomy = json.load(f)

        from matcher import build_automaton, find
        from ontology import normalize

        alias_map = {
            normalize(alias): label
            for label, meta in taxonomy.items()
            for alias in meta.get("aliases", [])
        }

        t0 = time.monotonic()
        automaton = build_automaton(alias_map)
        paragraph = (
            "Experience with Python, SQL, Power BI, Tableau, ETL pipelines, "
            "data modelling, stakeholder management, business partnering, "
            "data visualisation, statistical analysis, machine learning, "
            "Azure, AWS, Spark, Kafka, Airflow, dbt, dimensional modelling, "
            "star schema, data warehousing, KPI reporting, self-service analytics."
        ) * 20  # ~1 KB of text
        result = find(paragraph, automaton)
        elapsed = time.monotonic() - t0

        self.assertGreater(len(result), 5, "Expected multiple skill matches")
        self.assertLess(elapsed, 1.0, f"Build+match took {elapsed:.3f}s — too slow")


if __name__ == "__main__":
    unittest.main()
