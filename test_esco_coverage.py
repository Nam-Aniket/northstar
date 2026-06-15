#!/usr/bin/env python3
"""test_esco_coverage.py — Per-profession skill-recognition coverage + FP control.

Exercises ontology.match_text against six short, profession-specific résumé
fixtures and one skill-free generic-prose fixture. Each résumé asserts a
per-profession recognition FLOOR (set from measured baseline numbers), and the
generic prose asserts a false-positive ceiling (< 4 labels).

The floors are deliberately set at the pre-verb-strip baseline so this test:
  * passes against the CURRENT esco/skills_en.csv, and
  * keeps passing after the build-time VERB-STRIP only ADDS coverage.

Run:  python -m unittest test_esco_coverage
"""
import os

# Point config/skills at example files so transitive imports resolve.
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")

import unittest
from pathlib import Path

import ontology

FIXTURES = Path(__file__).resolve().parent / "tests" / "fixtures"
RESUMES = FIXTURES / "resumes"

# Per-profession recognition floors, measured against the pre-verb-strip
# esco/skills_en.csv. The build only adds bare-noun aliases, so post-build
# counts must be >= these floors.
FLOORS = {
    "data": 12,
    "electrician": 5,
    "finance": 8,
    "law": 1,
    "nursing": 5,
    "pharma": 9,
}

# Generic prose must stay essentially skill-free.
FP_CEILING = 4


def recognized(path: Path):
    """Return the set of labels ontology recognizes in a fixture file."""
    return ontology.match_text(path.read_text(encoding="utf-8"))


class TestEscoCoverage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # ESCO is opt-in now; this suite validates the ESCO-enabled path.
        cls._prev_esco = os.environ.get("ESCO_DATA")
        os.environ["ESCO_DATA"] = str(Path(__file__).resolve().parent / "esco" / "skills_en.csv")
        ontology._reset_cache()

    @classmethod
    def tearDownClass(cls):
        if cls._prev_esco is None:
            os.environ.pop("ESCO_DATA", None)
        else:
            os.environ["ESCO_DATA"] = cls._prev_esco
        ontology._reset_cache()

    def test_per_profession_floor(self):
        for prof, floor in sorted(FLOORS.items()):
            with self.subTest(profession=prof):
                labels = recognized(RESUMES / f"{prof}.txt")
                # Visibility: print what was recognized for this profession.
                print(
                    f"[{prof:12s}] recognized={len(labels):3d} "
                    f"floor={floor:3d} :: {sorted(labels)}"
                )
                self.assertGreaterEqual(
                    len(labels),
                    floor,
                    f"{prof}: recognized {len(labels)} < floor {floor}",
                )

    def test_generic_prose_false_positive_control(self):
        labels = recognized(FIXTURES / "generic_prose.txt")
        print(f"[generic_prose] false-positives={len(labels)} :: {sorted(labels)}")
        self.assertLess(
            len(labels),
            FP_CEILING,
            f"generic prose produced {len(labels)} labels "
            f"(>= {FP_CEILING}): {sorted(labels)}",
        )


if __name__ == "__main__":
    unittest.main()
