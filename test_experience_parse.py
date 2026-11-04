"""Offline regression tests for classify_experience: stated-years forms the original regexes missed, and the
boilerplate that must never be read as a requirement (None = Unclear beats a wrong value)."""
import pytest

from job_scout import classify_experience


def years(text, title="Analyst"):
    return classify_experience(title, text)[0]


@pytest.mark.parametrize("text, expected", [
    ("Minimum five years of recent experience with a public accounting firm", 5),
    ("Qualifications: Two years of financial analysis experience or an equivalent", 2),
    ("You'll need to have: 10 or more years of credit analysis experience", 10),
    ("Five or more years of related IT experience.", 5),
    ("8 years or more of experience in lending", 8),
    ("Has 10 plus years of banking experience with portfolio management", 10),
    ("Experience At least three (3) to five (5) years of experience in data analysis", 3),
    ("Five (5) to ten (10) years of experience in agricultural lending", 5),
    ("One to two years of related experience in a bank", 1),
    ("Minimum of one (1) year of work experience in a financial services company", 1),
    ("High School Diploma or GED and 1 years' experience in Escrow", 1),
    ("Bachelor's degree plus 4 years of related experience or demonstrated equivalency", 4),
    ("Master's degree plus two (2) years of experience in the job offered", 2),
    ("5 years of experience in business development, commercial lending", 5),
    ("Bachelor's degree or equivalent experience and three years' experience in regulatory risk", 3),
    ("P&C License required 5 years commercial insurance underwriting experience", 5),
    ("Experience: More than 10 years Life Underwriting experience.", 10),
    ("Experience Required: 2-3 years of clerical experience", 2),
    ("Minimum Years of Work Experience: 3 Preferred Years of Work Experience: No additional", 3),
    ("MBA with 3 years of relevant experience in consulting", 3),
    ("Required Qualifications: 4 yrs of progressive professional experience in finance", 4),
])
def test_new_stated_forms(text, expected):
    assert years(text) == expected


@pytest.mark.parametrize("text", [
    # employer history / tenure
    "Founded 20 years ago, we are a growing firm with great experience serving clients.",
    "Building on more than 30 years of investing experience, Point72 seeks to deliver returns.",
    "As a premier asset management organization with more than 85 years of experience, we invest globally.",
    "Over 30 years of history and experience in the market.",
    "A bank with 120 years of experience in community banking.",
    # service, age, fiscal, calendar, contract terms
    "Years of service award recognizes experience at the firm after 5 years of service.",
    "Must be eighteen years of age or older and have prior work experience.",
    "Applicants must be 18 years of age. Experience is a plus.",
    "Compare results to the prior 3 fiscal years and share experience with leadership.",
    "Completion within three years of hire (required) experience building the book",
    "A calendar of holidays; one break is at year end. Experience matters.",
    "Four-year college degree in related field and a good attitude toward experience",
    # preferred / desired only
    "Preferred qualifications: five years of experience in fintech.",
    "One to two years of related experience preferred.",
    "Minimum two-year banking experience preferred.",
    "Desired Skills & Competencies: College degree. Has 8 or more years of experience of credit analysis.",
    "Bonus points for 6 years of experience in private equity.",
    # degree OR experience alternatives
    "Associate's degree or three years of banking, retail fraud detection, or investigative experience",
    "Education from a bachelor's degree or have four or more years of insurance-related work experience",
    "Bachelor's degree in finance and/or 3 years of related work experience",
    "Degree from an accredited university; or at least 5 or more years of related experience and/or training.",
    # no requirement at all
    "We offer 20 days of vacation per year. Experience with Excel is helpful.",
    "",
])
def test_not_a_requirement(text):
    assert years(text) is None


def test_old_forms_unchanged():
    assert years("Requires 3-5 years of experience") == 3
    assert years("5+ years of experience") == 5
    assert years("at least 4 years in banking") == 4
    assert years("entry-level role, no experience required") == 0
    # an explicit entry phrase still wins over years (original ordering)
    assert years("New grads welcome. 5+ years preferred") == 0


def test_old_regex_does_not_read_tail_of_longer_number():
    assert years("We have served clients for 150+ years with great experience") is None
    assert years("with 1,000+ years of combined experience") is None


def test_entry_boilerplate_not_entry_level():
    # "from entry-level employees to senior leaders" is culture copy, not a requirement
    assert years("From entry-level employees to senior leaders, we grow talent. Required: 1+ year of experience.") == 1
    assert years("From entry-level employees to senior leaders, we grow talent.") is None


def test_stated_years_are_read_and_the_title_is_never_used():
    val, ev = classify_experience("Senior Associate, Audit", "Qualifications: Minimum three years of recent experience")
    assert val == 3 and "three years" in ev


@pytest.mark.parametrize("title", [
    "Summer Analyst Intern", "Jr Asset Liability Management Analyst", "Underwriting Trainee June 2027",
    "Graduate Program, Markets", "Managing Director, Credit", "Director, Underwriting", "Senior Analyst",
    "Associate Director, Ops", "Credit Analyst",
])
def test_title_alone_never_yields_a_value(title):
    # never guess: seniority words in the title say nothing about a stated requirement
    val, ev = classify_experience(title, "No years mentioned.")
    assert val is None and "inferred" not in ev


def test_maximum_years_is_not_a_requirement():
    assert classify_experience("Analyst", "If you have no more than three years of professional experience and fit this profile, apply.")[0] is None
    assert classify_experience("Analyst", "Candidates have at most 2 years of relevant experience.")[0] is None
    assert classify_experience("Analyst", "Requires three years of professional experience.")[0] == 3
