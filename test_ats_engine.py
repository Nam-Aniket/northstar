#!/usr/bin/env python3
"""Tests for the ATS engine v2: term extraction, bullet selection, linting."""

import os
os.environ.setdefault("JOBENGINE_SKILLS", "skills.example.json")
os.environ.setdefault("JOBENGINE_CONFIG", "config.example.json")
os.environ.setdefault("JOBENGINE_FACTS", "facts.example.json")

import unittest

from generate_accepted_resumes import (
    FACT_BANK,
    cover_letter_paragraphs,
    build_content,
    extract_terms,
    extract_terms_detailed,
    lint_text,
    select_bullets,
    subtitle_for,
    summary_for,
    validate_fact_bank,
)
from score_jobs import score_job, KEEP_THRESHOLD, STRONG_THRESHOLD


def _use_caps_config():
    """Monkeypatch config proxy so caps are ON for these tests."""
    import config
    config._proxy._load()  # ensure base data is populated first
    config._proxy._data["needs_sponsorship"] = True
    config._proxy._data["seniority_cap"] = 2


def _restore_default_config():
    """Restore caps to the example-file defaults."""
    import config
    config._proxy._data["needs_sponsorship"] = False
    config._proxy._data["seniority_cap"] = None


class TestExtractTerms(unittest.TestCase):
    def test_alias_dimensional_model_maps_to_data_modelling(self):
        hits, _ = extract_terms_detailed("Experience with dimensional models and star schema design.")
        self.assertIn("Data Modelling", hits)

    def test_business_partnering_maps_to_stakeholder_management(self):
        hits, _ = extract_terms_detailed("Strong business partnering with finance teams.")
        self.assertIn("Stakeholder Management", hits)

    def test_au_and_us_spellings_both_hit(self):
        au, _ = extract_terms_detailed("data visualisation and statistical modelling")
        us, _ = extract_terms_detailed("data visualization and statistical modeling")
        self.assertIn("Data Visualisation", au)
        self.assertIn("Data Visualisation", us)
        self.assertIn("Statistical Analysis", au)
        self.assertIn("Statistical Analysis", us)

    def test_bare_r_does_not_false_match(self):
        hits, _ = extract_terms_detailed("Required: 3 years experience. R&D team. Section 4(r).")
        self.assertNotIn("R", hits)

    def test_r_programming_matches(self):
        hits, _ = extract_terms_detailed("Proficiency in R programming required.")
        self.assertIn("R", hits)

    def test_unsupported_terms_reported_as_gaps(self):
        _, unsupported = extract_terms_detailed("Must have Snowflake and dbt experience.")
        self.assertIn("Snowflake", unsupported)
        self.assertIn("dbt", unsupported)

    def test_requirements_region_doubles_weight(self):
        jd = "We are a great company using many tools. Requirements: strong SQL skills."
        hits, _ = extract_terms_detailed(jd)
        self.assertEqual(hits["SQL"]["weight"], 2)
        jd2 = "Our team uses SQL daily. Benefits: free coffee."
        hits2, _ = extract_terms_detailed(jd2)
        self.assertEqual(hits2["SQL"]["weight"], 1)

    def test_data_storytelling_and_self_service_hit(self):
        hits, _ = extract_terms_detailed("self-service analytics and data storytelling skills")
        self.assertIn("Self-Service BI", hits)
        self.assertIn("Data Storytelling", hits)

    def test_v1_signature_compatible(self):
        supported, unsupported = extract_terms("SQL and Power BI, plus Alteryx.")
        self.assertIn("SQL", supported)
        self.assertIn("Power BI", supported)
        self.assertIn("Alteryx", unsupported)


class TestSelectBullets(unittest.TestCase):
    def test_dax_jd_selects_dax_bullet(self):
        hits, _ = extract_terms_detailed("Requirements: expert DAX and Power Query, self-service reporting.")
        chosen = select_bullets("role2", hits)
        texts = " ".join(e["text"] for e in chosen)
        self.assertIn("DAX", texts)
        self.assertIn("Power Query", texts)

    def test_forecasting_jd_selects_forecasting_bullet(self):
        hits, _ = extract_terms_detailed("Requirements: demand forecasting and predictive analytics.")
        chosen = select_bullets("role2", hits)
        texts = " ".join(e["text"] for e in chosen)
        self.assertIn("forecasting", texts.lower())

    def test_different_jds_get_different_bullets(self):
        bi_hits, _ = extract_terms_detailed("Requirements: DAX, Power Query, dashboards, self-service.")
        ba_hits, _ = extract_terms_detailed("Requirements: requirements gathering, process mapping, stakeholder workshops, UAT.")
        bi = [e["text"] for e in select_bullets("role2", bi_hits)]
        ba = [e["text"] for e in select_bullets("role2", ba_hits)]
        self.assertNotEqual(bi, ba)

    def test_budget_respected(self):
        hits, _ = extract_terms_detailed("SQL Python Power BI forecasting requirements stakeholder")
        self.assertEqual(len(select_bullets("role2", hits)), 4)
        self.assertEqual(len(select_bullets("role1", hits)), 3)
        self.assertEqual(len(select_bullets("role3", hits)), 3)

    def test_deterministic(self):
        hits, _ = extract_terms_detailed("Requirements: SQL, ETL, data quality.")
        a = [e["text"] for e in select_bullets("role2", hits)]
        b = [e["text"] for e in select_bullets("role2", hits)]
        self.assertEqual(a, b)


class TestContentSafety(unittest.TestCase):
    def test_fact_bank_validates(self):
        validate_fact_bank()  # raises on any structural/lint violation

    def test_slop_linter_catches_banned_phrases(self):
        with self.assertRaises(ValueError):
            lint_text("Leveraged synergies to spearhead initiatives.", "test")
        with self.assertRaises(ValueError):
            lint_text("I am excited to apply for this role.", "test")

    def test_summary_appends_at_most_one_clause(self):
        hits, _ = extract_terms_detailed(
            "Requirements: data governance, self-service, storytelling, forecasting, NLP, UAT."
        )
        s = summary_for("data_analyst", hits)
        self.assertLessEqual(s.count("Particular recent focus"), 1)

    def test_seniority_guard(self):
        self.assertEqual(subtitle_for("Senior Data Scientist", "data_scientist"), "Data Scientist")
        self.assertEqual(subtitle_for("Data Analyst", "data_analyst"), "Data Analyst")
        self.assertNotIn("Senior", subtitle_for("Senior Business Analyst, Data & AI", "business_analyst"))

    def test_cover_letter_varies_by_sector(self):
        target = {"role_title": "Data Analyst", "company": "X"}
        energy_jd = "Requirements: SQL. We are an energy utility managing the electricity grid."
        health_jd = "Requirements: SQL. We are an aged care provider supporting patient outcomes."
        c1 = build_content({"role_title": "Data Analyst"}, energy_jd)
        c2 = build_content({"role_title": "Data Analyst"}, health_jd)
        p_energy = cover_letter_paragraphs(c1, "EnergyCo", "Data Analyst", energy_jd)
        p_health = cover_letter_paragraphs(c2, "CareCo", "Data Analyst", health_jd)
        self.assertNotEqual(p_energy[0], p_health[0])


class TestAdversarialScorer(unittest.TestCase):
    """Adversarial regression tests for score_jobs.score_job."""

    def test_zero_skill_jd_scores_low(self):
        """Fluff JD with no recognised skills must score below 50 (old bug returned 77)."""
        jd = "We seek a wonderful person to join our team. Apply now."
        r = score_job("Data Analyst", jd)
        self.assertLess(r["fit"], 50, f"fluff JD scored {r['fit']} — expected < 50")

    def test_unsupported_stack_below_keep(self):
        """Requirements listing ONLY unsupported tools must fall below KEEP_THRESHOLD."""
        jd = "Requirements: expert in Snowflake, dbt and Looker. Must have 3+ years Snowflake."
        r = score_job("Data Analyst", jd)
        self.assertLess(r["fit"], KEEP_THRESHOLD,
                        f"unsupported-only JD scored {r['fit']} — expected < {KEEP_THRESHOLD}")

    def test_supported_stack_scores_high(self):
        """A realistic JD whose core requirements I fully match must score >= 70."""
        jd = (
            "Requirements: We are seeking a Data Analyst with strong SQL and Python skills "
            "to build dashboards and reports in Power BI. You will partner with stakeholders "
            "to deliver data-driven insights, perform ad hoc analysis, and maintain data quality."
        )
        r = score_job("Data Analyst", jd)
        self.assertGreaterEqual(r["fit"], 70, f"supported-stack JD scored {r['fit']} — expected >= 70")

    def test_thin_jd_not_inflated(self):
        """Laplace smoothing: a JD with very few requirements must not reach the strong band
        just because each requirement is matched — too little evidence to be confident."""
        jd = "Requirements: strong SQL, Python and Power BI experience."
        r = score_job("Data Analyst", jd)
        self.assertLess(r["fit"], STRONG_THRESHOLD, f"thin JD scored {r['fit']} — smoothing should keep it out of the strong band")
        self.assertGreater(r["fit"], 40, f"thin JD scored {r['fit']} — a fully-matched stack should still be a respectable fit")

    def test_citizenship_capped(self):
        """Citizenship/clearance requirement must hard-cap fit to <= 30."""
        _use_caps_config()
        try:
            jd = (
                "Requirements: strong SQL, Python, Power BI and Tableau. "
                "Australian citizenship and security clearance required."
            )
            r = score_job("Data Analyst", jd)
            self.assertLessEqual(r["fit"], 30, f"citizenship JD scored {r['fit']} — expected <= 30")
            # Citizenship is now expressed through the work-authorization knockout.
            self.assertTrue(any("work authorization" in c for c in r["caps"]),
                            "work-authorization cap not recorded")
            self.assertTrue(r["auto_reject_risk"], "work-auth knockout should flag auto-reject risk")
        finally:
            _restore_default_config()

    def test_seniority_soft_cap(self):
        """Same JD with a senior/7+ years requirement must score strictly lower than without it."""
        _use_caps_config()
        try:
            base_jd = "Requirements: strong SQL, Python, Power BI, data modelling and stakeholder management."
            senior_jd = base_jd + " Requirements: 7+ years as a senior lead analyst."
            base_r = score_job("Data Analyst", base_jd)
            senior_r = score_job("Data Analyst", senior_jd)
            self.assertLess(senior_r["fit"], base_r["fit"],
                            f"senior JD {senior_r['fit']} should be < base {base_r['fit']}")
        finally:
            _restore_default_config()

    def test_two_different_jds_spread(self):
        """Strong data-analyst JD and poor-fit JD must differ by > 15 points."""
        strong_jd = (
            "Requirements: strong SQL, Python, Power BI, DAX, data modelling, "
            "stakeholder management and data visualisation."
        )
        weak_jd = "We are looking for a great communicator to join our people team. Culture fit is essential."
        strong_r = score_job("Data Analyst", strong_jd)
        weak_r = score_job("Data Analyst", weak_jd)
        self.assertGreater(strong_r["fit"] - weak_r["fit"], 15,
                           f"spread too small: strong={strong_r['fit']} weak={weak_r['fit']}")

    def test_word_boundary(self):
        """Words containing 'sql' or bare 'r' as substrings must not be credited as SQL or R tools.

        The JD 'cooperation, operational' contains 'r' as a letter but must not
        match the R-language skill.  A JD with NO technical tool terms but some
        soft-skill matches may score any value — the guard is purely about false
        positive tool matches, not overall fit.
        """
        jd = (
            "We value cooperation, operational excellence, and organisational alignment. "
            "Requirements: great communication skills and stakeholder engagement."
        )
        r = score_job("Data Analyst", jd)
        # Word-boundary guards: tool labels must not be false-positively matched
        self.assertNotIn("SQL", r["supported"], "'SQL' falsely matched via 'cooperation'/'operational'")
        self.assertNotIn("R", r["supported"], "'R' falsely matched via word fragment in 'cooperation'")
        # Separately confirm a purely-fluff JD with NO soft skills also stays low
        fluff_jd = "We value cooperation and organisational alignment. No technical requirements."
        fluff_r = score_job("Data Analyst", fluff_jd)
        self.assertNotIn("SQL", fluff_r["supported"])
        self.assertNotIn("R", fluff_r["supported"])
        self.assertLess(fluff_r["fit"], 50, f"fluff+wordpart JD scored {fluff_r['fit']} — expected < 50")


class TestKnockouts(unittest.TestCase):
    """Knockout / prescreening detection (the only true ATS auto-reject gate)."""

    def _ko(self, jd, role="Data Analyst"):
        from score_jobs import detect_knockouts, _req_start
        return detect_knockouts(jd, _req_start(jd))

    def _types(self, jd):
        return {k["type"] for k in self._ko(jd)}

    def test_min_years_detected_with_value(self):
        kos = self._ko("Requirements: minimum 5 years of analytics experience.")
        years = [k for k in kos if k["type"] == "min_years"]
        self.assertEqual(len(years), 1)
        self.assertEqual(years[0]["required_value"], 5)

    def test_plus_years_form_detected(self):
        kos = self._ko("Requirements: 7+ years in a data role.")
        self.assertEqual([k["required_value"] for k in kos if k["type"] == "min_years"], [7])

    def test_bare_years_is_not_a_knockout(self):
        # Descriptive prose, not a stated minimum.
        self.assertNotIn("min_years", self._types(
            "Requirements: SQL and Python. Our team has grown over the last 5 years."))

    def test_degree_required_detected(self):
        self.assertIn("degree", self._types(
            "Requirements: Bachelor's degree in Statistics required."))

    def test_degree_preferred_is_not_a_knockout(self):
        self.assertNotIn("degree", self._types(
            "Requirements: Bachelor's degree preferred, or equivalent experience."))

    def test_certification_detected(self):
        self.assertIn("certification", self._types("Requirements: CPA required."))

    def test_eeo_citizenship_is_not_a_knockout(self):
        _use_caps_config()
        try:
            jd = ("Requirements: SQL and Python. We welcome all regardless of "
                  "citizenship, gender identity or sexual orientation.")
            self.assertNotIn("work_authorization", self._types(jd))
        finally:
            _restore_default_config()

    def test_onsite_detected_as_verify_only(self):
        kos = self._ko("Requirements: SQL. This role is 4 days per week in the office.")
        onsite = [k for k in kos if k["type"] == "onsite"]
        self.assertEqual(len(onsite), 1)
        self.assertEqual(onsite[0]["status"], "unknown")  # never an auto-fail

    def test_years_unmet_flags_auto_reject(self):
        import config
        config._proxy._load()
        config._proxy._data["years_experience"] = 2
        try:
            r = score_job("Data Analyst",
                          "Requirements: strong SQL and Python. Minimum 8 years required.")
            self.assertTrue(r["auto_reject_risk"])
            self.assertLess(r["fit"], KEEP_THRESHOLD)
        finally:
            config._proxy._data.pop("years_experience", None)

    def test_years_unknown_does_not_fail(self):
        # No candidate years in the example config -> status unknown -> no cap, no flag.
        r = score_job("Data Analyst",
                      "Requirements: strong SQL and Python. Minimum 8 years required.")
        self.assertFalse(r["auto_reject_risk"])


class TestSoftWeighting(unittest.TestCase):
    """Hard/soft weighted coverage: soft skills count at a lower, role-dependent
    weight, and a manager/senior role penalises unevidenced soft skills more."""

    def test_senior_role_penalises_unevidenced_soft_more(self):
        # "Leadership" is a soft skill the example bank does NOT evidence (a gap);
        # SQL/Python are hard skills it has. Only the role seniority differs, so
        # the score gap is purely the soft weight (IC 0.15 vs senior 0.40).
        jd = ("Requirements: strong SQL and Python for analysis. "
              "Proven leadership of a team is essential.")
        ic = score_job("Data Analyst", jd)
        snr = score_job("Senior Data Analyst", jd)
        self.assertLess(snr["fit"], ic["fit"],
                        f"senior {snr['fit']} should be < IC {ic['fit']} when leadership unevidenced")

    def test_soft_weighting_does_not_inflate_pure_hard_jd(self):
        # A JD of only hard skills the bank evidences must still clear the keep bar;
        # the soft term must not change a JD with no soft requirements.
        jd = "Requirements: SQL, Python, Power BI, Tableau and data modelling."
        r = score_job("Data Analyst", jd)
        self.assertGreaterEqual(r["fit"], KEEP_THRESHOLD)


if __name__ == "__main__":
    unittest.main()
