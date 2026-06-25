"""Tests for LinkedIn People parser and email generation engine."""

import unittest

from linkedin_people_parser import (
    parse_people,
    make_email,
    slugify,
    _is_abbreviated_first_name,
    _has_lone_initial_surname,
    _clean_name,
)


class TestEmailGeneration(unittest.TestCase):
    """Test email generation from name and pattern."""
    
    def test_basic_pattern_first_last(self):
        """Test {first}.{last} pattern."""
        email = make_email("John Doe", "{first}.{last}@company.com")
        self.assertEqual(email, "john.doe@company.com")
    
    def test_basic_pattern_f_l(self):
        """Test {f}.{l} pattern (initials)."""
        email = make_email("John Doe", "{f}.{l}@company.com")
        self.assertEqual(email, "j.d@company.com")
    
    def test_mixed_case_input(self):
        """Test that mixed-case names are lowercased."""
        email = make_email("JOHN DoE", "{first}.{last}@company.com")
        self.assertEqual(email, "john.doe@company.com")
    
    def test_diacritics_stripped(self):
        """Test that diacritics are removed."""
        email = make_email("François Müller", "{first}.{last}@company.com")
        self.assertEqual(email, "francois.muller@company.com")
    
    def test_domain_from_pattern(self):
        """Test extracting domain from pattern."""
        email = make_email("John Doe", "{first}.{last}@example.org")
        self.assertEqual(email, "john.doe@example.org")
    
    def test_domain_override(self):
        """Test domain parameter overrides pattern."""
        email = make_email("John Doe", "{first}.{last}@pattern.com", domain="override.com")
        self.assertEqual(email, "john.doe@override.com")
    
    def test_single_name_no_domain(self):
        """Test single name without domain."""
        email = make_email("John", "{first}")
        self.assertEqual(email, "john")
    
    def test_special_chars_removed(self):
        """Test that special characters are removed."""
        email = make_email("John-Paul O'Reilly", "{first}{last}@company.com")
        self.assertEqual(email, "johnpauloreilly@company.com")


class TestNameCleaning(unittest.TestCase):
    """Test name cleaning and normalization."""
    
    def test_all_lowercase_title_cased(self):
        """Test all-lowercase names are title-cased."""
        name = _clean_name("bradley gooding")
        self.assertEqual(name, "Bradley Gooding")
    
    def test_all_uppercase_title_cased(self):
        """Test all-uppercase names are title-cased."""
        name = _clean_name("BRADLEY GOODING")
        self.assertEqual(name, "Bradley Gooding")
    
    def test_mixed_case_preserved(self):
        """Test mixed-case names are preserved."""
        name = _clean_name("John Doe")
        self.assertEqual(name, "John Doe")
    
    def test_is_open_to_work_stripped(self):
        """Test 'is open to work' suffix is removed."""
        name = _clean_name("John Doe is open to work")
        self.assertEqual(name, "John Doe")
    
    def test_whitespace_normalized(self):
        """Test internal whitespace is normalized."""
        name = _clean_name("John  Doe")
        self.assertEqual(name, "John Doe")
    
    def test_diacritics_removed(self):
        """Test diacritics are removed."""
        name = _clean_name("François")
        self.assertEqual(name, "Francois")


class TestAbbreviatedFirstName(unittest.TestCase):
    """Test detection of abbreviated first names."""
    
    def test_single_initial(self):
        """Test single initial (M.)."""
        self.assertTrue(_is_abbreviated_first_name("M. Uzair Tariq"))
    
    def test_single_letter(self):
        """Test single letter without dot (M)."""
        self.assertTrue(_is_abbreviated_first_name("M Uzair Tariq"))
    
    def test_full_first_name(self):
        """Test full first name."""
        self.assertFalse(_is_abbreviated_first_name("Muhammad Uzair Tariq"))
    
    def test_two_letter_word(self):
        """Test two-letter word is not abbreviated."""
        self.assertFalse(_is_abbreviated_first_name("Al Johnson"))


class TestLoneInitialSurname(unittest.TestCase):
    """Test detection of lone-initial surnames."""
    
    def test_lone_initial_surname(self):
        """Test single-letter surname."""
        self.assertTrue(_has_lone_initial_surname("Ayaz M."))
    
    def test_lone_letter_no_dot(self):
        """Test single letter surname without dot."""
        self.assertTrue(_has_lone_initial_surname("Ayaz M"))
    
    def test_full_surname(self):
        """Test full surname."""
        self.assertFalse(_has_lone_initial_surname("Ayaz Muhammad"))
    
    def test_single_name(self):
        """Test single name has no surname."""
        self.assertFalse(_has_lone_initial_surname("Ayaz"))


class TestParseIntegration(unittest.TestCase):
    """Integration tests for the full parser."""
    
    def test_basic_parse(self):
        """Test parsing basic LinkedIn text."""
        raw = """John Doe
· 1st
Software Engineer
Jane Smith
· 2nd
Product Manager"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        self.assertEqual(len(result.people), 2)
        self.assertEqual(result.people[0]["name"], "John Doe")
        self.assertEqual(result.people[0]["title"], "Software Engineer")
        self.assertEqual(result.people[0]["email"], "john.doe@company.com")
        self.assertEqual(result.people[1]["name"], "Jane Smith")
        self.assertEqual(result.people[1]["title"], "Product Manager")
    
    def test_dedup_within_paste(self):
        """Test that duplicate names within same paste are deduplicated."""
        raw = """John Doe
· 1st
Software Engineer
John Doe
· 2nd
Senior Engineer"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        self.assertEqual(len(result.people), 1)
        self.assertEqual(len(result.dropped), 1)
        self.assertEqual(result.dropped[0]["reason"], "duplicate within paste")
    
    def test_is_open_to_work_dedup(self):
        """Test that 'is open to work' is stripped before dedup."""
        raw = """John Doe is open to work
· 1st
Software Engineer
John Doe
· 2nd
Senior Engineer"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        self.assertEqual(len(result.people), 1)
        self.assertEqual(len(result.dropped), 1)
    
    def test_linkedin_member_dropped(self):
        """Test that 'LinkedIn Member' is dropped."""
        raw = """LinkedIn Member
· 1st
Some Title
John Doe
· 2nd
Engineer"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        self.assertEqual(len(result.people), 1)
        self.assertEqual(len(result.dropped), 1)
        self.assertEqual(result.dropped[0]["reason"], "anonymized (LinkedIn Member)")
    
    def test_lone_initial_surname_dropped(self):
        """Test that lone-initial surnames are dropped."""
        raw = """Ayaz M.
· 1st
Engineer
John Doe
· 2nd
Software Engineer"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        self.assertEqual(len(result.people), 1)
        self.assertEqual(len(result.dropped), 1)
        self.assertEqual(result.dropped[0]["reason"], "lone-initial surname")
    
    def test_no_surname_dropped(self):
        """Test that single-token names are dropped."""
        raw = """Madonna
· 1st
Singer
John Doe
· 2nd
Engineer"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        self.assertEqual(len(result.people), 1)
        self.assertEqual(len(result.dropped), 1)
        self.assertEqual(result.dropped[0]["reason"], "no surname")
    
    def test_abbreviated_first_name_needs_review(self):
        """Test that abbreviated first names go to needs_review."""
        raw = """M. Uzair Tariq
· 1st
Engineer
John Doe
· 2nd
Software Engineer"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        self.assertEqual(len(result.people), 1)
        self.assertEqual(len(result.needs_review), 1)
        self.assertEqual(result.needs_review[0]["name"], "M. Uzair Tariq")
        self.assertEqual(result.needs_review[0]["email"], "m.tariq@company.com")
        self.assertEqual(result.needs_review[0]["reason"], "abbreviated first name")
    
    def test_noise_lines_excluded(self):
        """Test that noise lines are excluded from title."""
        raw = """John Doe
· 1st
150 followers
Software Engineer
Jane Smith
· 2nd
Product Manager"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        # First person should have title "Software Engineer" (noise skipped)
        self.assertEqual(len(result.people), 2)
        self.assertEqual(result.people[0]["title"], "Software Engineer")
    
    def test_diacritics_in_names(self):
        """Test that diacritics are removed from names and emails."""
        raw = """François Müller
· 1st
Engineer"""
        result = parse_people(raw, "Acme Corp", "{first}.{last}@company.com")
        self.assertEqual(len(result.people), 1)
        self.assertEqual(result.people[0]["name"], "Francois Muller")
        self.assertEqual(result.people[0]["email"], "francois.muller@company.com")
    
    def test_pattern_variants(self):
        """Test different email pattern formats."""
        raw = """John Doe
· 1st
Engineer"""
        
        # Test {f}.{l} pattern
        result = parse_people(raw, "Acme Corp", "{f}.{l}@company.com")
        self.assertEqual(result.people[0]["email"], "j.d@company.com")
        
        # Test {first}{last} pattern
        result = parse_people(raw, "Acme Corp", "{first}{last}@company.com")
        self.assertEqual(result.people[0]["email"], "johndoe@company.com")
        
        # Test plain pattern (no tokens)
        result = parse_people(raw, "Acme Corp", "firstname.lastname@company.com")
        self.assertEqual(result.people[0]["email"], "firstname.lastname@company.com")


class TestSlugify(unittest.TestCase):
    """Test slugify function for deduplication."""
    
    def test_basic_slugify(self):
        """Test basic name slugification."""
        self.assertEqual(slugify("John Doe"), "john_doe")
    
    def test_special_chars_removed(self):
        """Test special characters are replaced."""
        self.assertEqual(slugify("John-Paul O'Reilly"), "john_paul_o_reilly")
    
    def test_case_insensitive(self):
        """Test slugify is case-insensitive."""
        self.assertEqual(slugify("John Doe"), slugify("john doe"))
    
    def test_diacritics_removed(self):
        """Test diacritics are removed."""
        self.assertEqual(slugify("François"), slugify("francois"))


if __name__ == "__main__":
    unittest.main()
