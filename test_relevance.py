"""test_relevance.py — For-me board view relevance/seniority classification."""
import unittest
import sqlite3

from app import relevance
from app.db import init_schema


class TestParseSeniority(unittest.TestCase):
    def test_plain_title_is_mid(self):
        self.assertEqual(relevance.parse_seniority("Data Analyst"), ("mid", 2))

    def test_senior(self):
        self.assertEqual(relevance.parse_seniority("Senior Data Analyst"), ("senior", 3))

    def test_lead(self):
        self.assertEqual(relevance.parse_seniority("Lead BI Developer"), ("lead", 3))

    def test_manager(self):
        self.assertEqual(relevance.parse_seniority("Analytics Manager"), ("manager", 4))

    def test_principal_staff(self):
        self.assertEqual(relevance.parse_seniority("Principal Data Engineer"), ("principal", 4))
        self.assertEqual(relevance.parse_seniority("Staff Analyst"), ("principal", 4))

    def test_director_head_of(self):
        self.assertEqual(relevance.parse_seniority("Director of Analytics"), ("director", 5))
        self.assertEqual(relevance.parse_seniority("Head of Data"), ("director", 5))

    def test_exec(self):
        self.assertEqual(relevance.parse_seniority("VP of Engineering"), ("exec", 6))

    def test_graduate_and_intern(self):
        self.assertEqual(relevance.parse_seniority("Graduate Analyst"), ("graduate", 1))
        self.assertEqual(relevance.parse_seniority("Data Analyst Intern"), ("intern", 0))

    def test_junior_is_entry(self):
        self.assertEqual(relevance.parse_seniority("Junior Data Analyst"), ("entry", 1))

    def test_senior_word_not_matched_inside_other_word(self):
        self.assertEqual(relevance.parse_seniority("Software Engineer"), ("mid", 2))


class TestTitleTokens(unittest.TestCase):
    def test_drops_seniority_and_filler(self):
        self.assertEqual(relevance.title_tokens("Senior Data Analyst (Remote)"), {"data", "analyst"})

    def test_software_engineer_tokens(self):
        self.assertEqual(relevance.title_tokens("Software Engineer II"), {"software", "engineer"})


def _seed_positions(titles):
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    init_schema(con)
    for t in titles:
        con.execute(
            "INSERT INTO tracked_positions (title, display, created_at) VALUES (?,?,?)",
            (t, t, "2026-06-23"),
        )
    con.commit()
    return con


class TestTargetProfile(unittest.TestCase):
    def test_tokens_union_across_positions(self):
        con = _seed_positions(["Data Analyst", "Business Analyst"])
        p = relevance.target_profile(con)
        self.assertEqual(p["title_tokens"], {"data", "analyst", "business"})

    def test_max_rank_mid_when_no_senior_word(self):
        con = _seed_positions(["Data Analyst", "Business Analyst"])
        p = relevance.target_profile(con)
        self.assertEqual(p["max_seniority_rank"], 2)

    def test_max_rank_lifts_with_senior_title(self):
        con = _seed_positions(["Senior Data Analyst"])
        p = relevance.target_profile(con)
        self.assertEqual(p["max_seniority_rank"], 3)

    def test_empty_profile_when_no_positions(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        init_schema(con)
        p = relevance.target_profile(con)
        self.assertEqual(p["title_tokens"], set())
        self.assertEqual(p["max_seniority_rank"], 2)


class TestClassifyJob(unittest.TestCase):
    def setUp(self):
        self.profile = {"title_tokens": {"data", "analyst", "business"},
                        "tracked_titles": ["Data Analyst", "Business Analyst"],
                        "max_seniority_rank": 2}

    def test_off_target_software_engineer(self):
        j = relevance.classify_job({"role_title": "Software Engineer", "match_score": 60},
                                   self.profile, cap=2)
        self.assertFalse(j["on_target"])

    def test_on_target_and_over_level_senior(self):
        j = relevance.classify_job({"role_title": "Senior Data Analyst", "match_score": 70},
                                   self.profile, cap=2)
        self.assertTrue(j["on_target"])
        self.assertEqual(j["seniority_label"], "senior")
        self.assertTrue(j["over_level"])

    def test_on_target_in_level(self):
        j = relevance.classify_job({"role_title": "Business Intelligence Analyst", "match_score": 65},
                                   self.profile, cap=2)
        self.assertTrue(j["on_target"])
        self.assertFalse(j["over_level"])

    def test_unconfigured_profile_marks_all_on_target(self):
        empty = {"title_tokens": set(), "tracked_titles": [], "max_seniority_rank": 2}
        j = relevance.classify_job({"role_title": "Software Engineer", "match_score": 60},
                                   empty, cap=2)
        self.assertTrue(j["on_target"])


class TestAnchorMatching(unittest.TestCase):
    """Real-world leak: tracking 'Data Engineer' must NOT admit 'Software Engineer'
    via the shared generic word 'engineer'."""

    def setUp(self):
        self.con = _seed_positions(
            ["Data Analyst", "Business Analyst", "Data Engineer", "Data Scientist"])
        self.profile = relevance.target_profile(self.con)

    def test_anchor_tokens_drop_generic_role_words(self):
        self.assertEqual(relevance.anchor_tokens("Software Engineer"), {"software"})
        self.assertEqual(relevance.anchor_tokens("Data Engineer"), {"data"})
        self.assertEqual(relevance.anchor_tokens("Backend Developer"), {"backend"})

    def test_profile_anchors_exclude_engineer(self):
        self.assertIn("data", self.profile["anchor_tokens"])
        self.assertIn("scientist", self.profile["anchor_tokens"])
        self.assertNotIn("engineer", self.profile["anchor_tokens"])

    def test_software_engineer_off_target_even_when_tracking_data_engineer(self):
        j = relevance.classify_job(
            {"role_title": "Software Engineer", "match_score": 90}, self.profile, cap=2)
        self.assertFalse(j["on_target"])

    def test_machine_learning_engineer_off_target(self):
        j = relevance.classify_job(
            {"role_title": "Machine Learning Engineer", "match_score": 90}, self.profile, cap=2)
        self.assertFalse(j["on_target"])

    def test_backend_developer_off_target(self):
        j = relevance.classify_job(
            {"role_title": "Backend Developer", "match_score": 90}, self.profile, cap=2)
        self.assertFalse(j["on_target"])

    def test_data_engineer_still_on_target(self):
        j = relevance.classify_job(
            {"role_title": "Senior Data Engineer", "match_score": 90}, self.profile, cap=2)
        self.assertTrue(j["on_target"])

    def test_data_analyst_still_on_target(self):
        j = relevance.classify_job(
            {"role_title": "Data Analyst", "match_score": 90}, self.profile, cap=2)
        self.assertTrue(j["on_target"])


class TestApplyForMeView(unittest.TestCase):
    def setUp(self):
        self.profile = {"title_tokens": {"data", "analyst"},
                        "tracked_titles": ["Data Analyst"], "max_seniority_rank": 2}
        self.jobs = [
            {"role_title": "Software Engineer", "match_score": 90},
            {"role_title": "Senior Data Analyst", "match_score": 80},
            {"role_title": "Data Analyst", "match_score": 60},
            {"role_title": "Junior Data Analyst", "match_score": 55},
        ]

    def test_hides_off_target_and_sinks_over_level(self):
        out = relevance.apply_for_me_view(self.jobs, self.profile)
        titles = [j["role_title"] for j in out]
        self.assertNotIn("Software Engineer", titles)
        self.assertEqual(titles[-1], "Senior Data Analyst")
        self.assertEqual(titles[0], "Data Analyst")

    def test_empty_profile_hides_nothing(self):
        empty = {"title_tokens": set(), "tracked_titles": [], "max_seniority_rank": 2}
        out = relevance.apply_for_me_view(self.jobs, empty)
        self.assertEqual(len(out), 4)

    def test_override_rank_lifts_cap(self):
        out = relevance.apply_for_me_view(self.jobs, self.profile, override_rank=3)
        senior = next(j for j in out if j["role_title"] == "Senior Data Analyst")
        self.assertFalse(senior["over_level"])

    def test_in_level_sorted_by_freshness_then_score(self):
        # Among in-level on-target jobs, the fresher posted_at wins even with a
        # lower Fit score.
        jobs = [
            {"role_title": "Data Analyst", "match_score": 90, "posted_at": "2026-06-20"},
            {"role_title": "Data Analyst", "match_score": 50, "posted_at": "2026-06-25"},
        ]
        out = relevance.apply_for_me_view(jobs, self.profile)
        self.assertEqual([j["match_score"] for j in out], [50, 90])


class TestExperienceFlag(unittest.TestCase):
    """Flag roles that ask for more years than the candidate has."""

    profile = {"title_tokens": {"data", "analyst"}, "anchor_tokens": {"data", "analyst"},
               "tracked_titles": ["Data Analyst"], "max_seniority_rank": 2}

    def test_required_years_plus_form(self):
        self.assertEqual(relevance.required_years("We need 5+ years of SQL."), 5)

    def test_required_years_minimum_phrase(self):
        self.assertEqual(relevance.required_years("Minimum of 7 years experience required."), 7)

    def test_required_years_takes_the_max(self):
        self.assertEqual(relevance.required_years("3+ years here; at least 8 years there."), 8)

    def test_bare_years_prose_ignored(self):
        self.assertIsNone(relevance.required_years("Over the years we grew; founded 2 years ago."))

    def test_none_when_absent(self):
        self.assertIsNone(relevance.required_years("No experience requirement stated."))

    def test_over_experience_flagged(self):
        j = relevance.classify_job(
            {"role_title": "Data Analyst", "job_text": "Requires 6+ years.", "match_score": 70},
            self.profile, cap=2, cand_years=3)
        self.assertEqual(j["req_years"], 6)
        self.assertTrue(j["over_experience"])

    def test_within_experience_not_flagged(self):
        j = relevance.classify_job(
            {"role_title": "Data Analyst", "job_text": "Requires 2+ years.", "match_score": 70},
            self.profile, cap=2, cand_years=3)
        self.assertFalse(j["over_experience"])

    def test_no_candidate_years_never_flags(self):
        j = relevance.classify_job(
            {"role_title": "Data Analyst", "job_text": "Requires 9+ years.", "match_score": 70},
            self.profile, cap=2, cand_years=None)
        self.assertFalse(j["over_experience"])

    def test_apply_view_sinks_over_experience(self):
        jobs = [
            {"role_title": "Data Analyst", "job_text": "10+ years required", "match_score": 99},
            {"role_title": "Data Analyst", "job_text": "entry friendly", "match_score": 50},
        ]
        out = relevance.apply_for_me_view(jobs, self.profile, cand_years=3)
        self.assertEqual(out[-1]["job_text"], "10+ years required")  # sunk despite top score


if __name__ == "__main__":
    unittest.main()
