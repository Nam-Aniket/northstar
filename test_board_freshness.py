"""Board-level freshness behavior: _shape posted_at + freshest-first sort."""
import os
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")
os.environ.setdefault("JOBENGINE_FACTS", "facts.example.json")

import unittest

import app.queries as Q


class ShapePostedAt(unittest.TestCase):
    def test_shape_sets_posted_at_from_jd_posted_date(self):
        row = {"match_score": 60, "jd_posted_date": "2026-06-25T10:00:00",
               "first_seen_date": "2026-06-20"}
        self.assertEqual(Q._shape(row)["posted_at"], "2026-06-25T10:00:00")

    def test_shape_posted_at_falls_back_to_first_seen(self):
        row = {"match_score": 60, "jd_posted_date": "", "first_seen_date": "2026-06-20"}
        self.assertEqual(Q._shape(row)["posted_at"], "2026-06-20")


class SortBoard(unittest.TestCase):
    def test_freshest_first_beats_higher_score(self):
        jobs = [
            {"row_key": "a", "match_score": 90, "posted_at": "2026-06-20", "starred": 0},
            {"row_key": "b", "match_score": 50, "posted_at": "2026-06-25", "starred": 0},
        ]
        Q._sort_board(jobs)
        self.assertEqual([j["row_key"] for j in jobs], ["b", "a"])

    def test_same_day_breaks_tie_on_score(self):
        jobs = [
            {"row_key": "a", "match_score": 50, "posted_at": "2026-06-25", "starred": 0},
            {"row_key": "b", "match_score": 90, "posted_at": "2026-06-25", "starred": 0},
        ]
        Q._sort_board(jobs)
        self.assertEqual([j["row_key"] for j in jobs], ["b", "a"])


if __name__ == "__main__":
    unittest.main()
