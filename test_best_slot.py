"""test_best_slot.py — unit tests for the editor's best-fit experience picker.

best_slot_for_skill(skill, fact_bank, experience_slots, group_for) picks which
work-experience slot a newly claimed skill's bullet should default into:
  1. evidence overlap (exact skill, or same taxonomy group),
  2. tie-break to the most recent role (first slot),
  3. fallback to the most recent role when nothing overlaps.

Tests inject a fake `group_for` so they stay deterministic and independent of
taxonomy.json.
"""
import os
# Load example fixtures, not the owner's real (gitignored) facts.json. Must run
# before importing generate_accepted_resumes so the module-level facts override
# reads the example file — and so this test never pollutes the shared import for
# tests that run after it.
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")
os.environ.setdefault("JOBENGINE_FACTS", "facts.example.json")

import unittest

import generate_accepted_resumes as gen

SLOTS = [("Recent Role | A | 2025", "role1"),
         ("Middle Role | B | 2022", "role2"),
         ("Oldest Role | C | 2020", "role3")]

# Fake taxonomy grouping for deterministic tests.
GROUPS = {
    "machine learning": "AI/ML", "deep learning": "AI/ML", "nlp": "AI/ML",
    "sql": "Data", "power bi": "BI", "welding": "Trades",
}


def fake_group_for(label):
    return GROUPS.get((label or "").strip().lower(), "General")


def pick(skill, bank):
    return gen.best_slot_for_skill(skill, bank, SLOTS, group_for=fake_group_for)


class BestSlotTests(unittest.TestCase):
    def test_exact_evidence_overlap_wins(self):
        bank = {
            "role1": [{"text": "x", "evidences": ["SQL"]}],
            "role2": [{"text": "y", "evidences": ["Machine Learning"]}],
            "role3": [{"text": "z", "evidences": ["Power BI"]}],
        }
        self.assertEqual(pick("Machine Learning", bank), "role2")

    def test_group_overlap_when_no_exact(self):
        # "Deep Learning" isn't evidenced anywhere, but shares the AI/ML group
        # with role2's "NLP" bullet.
        bank = {
            "role1": [{"text": "x", "evidences": ["SQL"]}],
            "role2": [{"text": "y", "evidences": ["NLP"]}],
            "role3": [{"text": "z", "evidences": ["Power BI"]}],
        }
        self.assertEqual(pick("Deep Learning", bank), "role2")

    def test_tiebreak_prefers_most_recent_role(self):
        # Both role1 and role2 have one AI/ML-group evidence -> most recent (role1).
        bank = {
            "role1": [{"text": "x", "evidences": ["Machine Learning"]}],
            "role2": [{"text": "y", "evidences": ["NLP"]}],
            "role3": [{"text": "z", "evidences": ["Power BI"]}],
        }
        self.assertEqual(pick("Deep Learning", bank), "role1")

    def test_no_overlap_falls_back_to_most_recent(self):
        bank = {
            "role1": [{"text": "x", "evidences": ["SQL"]}],
            "role2": [{"text": "y", "evidences": ["NLP"]}],
            "role3": [{"text": "z", "evidences": ["Power BI"]}],
        }
        self.assertEqual(pick("Welding", bank), "role1")

    def test_empty_bank_falls_back_to_most_recent(self):
        self.assertEqual(pick("Machine Learning", {}), "role1")

    def test_no_slots_returns_empty(self):
        self.assertEqual(
            gen.best_slot_for_skill("SQL", {}, [], group_for=fake_group_for), "")

    def test_general_group_skill_uses_exact_only(self):
        # An unknown-group skill must not match every other "General" evidence;
        # with no exact hit it falls back to the most recent role.
        bank = {
            "role1": [{"text": "x", "evidences": ["Some Unknown Thing"]}],
            "role2": [{"text": "y", "evidences": ["Another Unknown"]}],
        }
        self.assertEqual(pick("Mystery Skill", bank), "role1")


if __name__ == "__main__":
    unittest.main()
