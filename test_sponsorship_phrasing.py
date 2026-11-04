"""Sponsorship classifier: wording that excludes OPT too is Blocked, plain no-sponsorship wording is "OPT Only", benign look-alikes stay Unclear."""
import pytest

import job_scout as js

BLOCKED = [
    "Toyota does not offer support or sponsorship of job applicants for employment-based visas or any other work authorization for this role now or in the future. You must have the right to work in the United States and not require Toyota support or sponsorship for immigration-related employment (e.g., H-1B, O-1, E-3, H-1B1, TN, F-1 OPT, F-1 STEM OPT, F-1 CPT, etc.) now or in the future.",
    "This role is limited to persons with an indefinite right to work in the United States.",
]
OPT_ONLY = [
    "Sponsorship Offered: No",
    "Visa Sponsorship? No",
    "We hire without current or future sponsorship needs.",
    "USAA does not provide visa sponsorship for this role. Please do not apply for this role if at any time (now or in the future) you will need immigration support (i.e., H-1B, TN, STEM OPT Training Plans, etc.).",
    "Candidates requiring immigration support, such as H-1B or STEM OPT, will not be considered.",
    "We do not provide visa sponsorship for this role.",
    "We are unable to provide immigration support for this role.",
    "This role is not eligible for visa assistance.",
    "No immigration support is available for this position.",
    "Bachelor's degree in Finance, STEM, or a related field. We do not provide visa sponsorship for this role.",
    "Applicants must be authorized to work in the U.S. without current or future employer support.",
    "We will not sponsor now or in the future.",
]
HARD_BLOCKED = [
    "We do not offer any type of employment-based immigration sponsorship for this role. Likewise, the firm will not provide any assistance or sign any documentation in support of any other form of immigration sponsorship or benefit, including optional practical training (OPT) or curricular practical training (CPT).",
    "Candidates must hold an active US Secret clearance.",
    "Access is limited to U.S. persons only under export control rules.",
    "This role is open to U.S. citizens or lawful permanent residents.",
    "The position requires U.S. Citizenship.",
    "Must have permanent or ongoing work authorization in the U.S.",
    "Unrestricted work authorization is needed. CPT and OPT are restricted.",
    "Secret clearance is required for this position.",
    "Benefits include STEM support. We will not sponsor now or in the future. F-1 OPT and CPT candidates are not eligible.",
    "To be eligible for a VIE: Be less than 28 years old. Be graduated of Master Degree. Be EU citizen",
    "Must be a U.S. citizen. We do not sponsor visas.",
    "Active secret clearance required. No sponsorship.",
    "This position is subject to ITAR. We cannot sponsor.",
    "No sponsorship available. Candidates on STEM OPT or CPT are not considered.",
]
UNCLEAR = [
    "Our members are not sponsored by agencies of the United States.",
    "We will not sponsor new industry registrations for this fund.",
    "We offer visa sponsorship and immigration support for qualified candidates.",
    "Immigration support is available for the right candidate.",
    "Do not apply unless you are passionate about risk management.",
    "Please do not apply through third parties; use our careers site.",
    "Candidates must be eligible to work in the United States. We will sponsor H-1B visas where appropriate.",
    "The role supports our immigration law practice clients and does not require travel.",
    "Authorized to work in the U.S. is required; we provide relocation assistance.",
]


@pytest.mark.parametrize("text", BLOCKED)
def test_blocks_immigration_support_phrasing(text):
    assert js.classify_sponsorship(text)[0] == "Blocked"


@pytest.mark.parametrize("text", HARD_BLOCKED)
def test_hard_blocks_stay_blocked(text):
    assert js.classify_sponsorship(text)[0] == "Blocked"


@pytest.mark.parametrize("text", OPT_ONLY + [
    "Please do not apply for this role if you will need immigration support (H-1B, TN).",
    "We do not offer relocation or immigration assistance.",
])
def test_plain_no_sponsorship_is_opt_only(text):
    assert js.classify_sponsorship(text)[0] == "OPT Only"


@pytest.mark.parametrize("text", UNCLEAR)
def test_benign_lookalikes_stay_unclear(text):
    assert js.classify_sponsorship(text)[0] == "Unclear"


def test_us_abbreviation_before_space_is_normalized():
    assert js.classify_sponsorship("Candidates must be authorized to work in the U.S. without sponsorship now or in the future.")[0] == "OPT Only"


def test_tags():
    assert js.classify_sponsorship("We will not sponsor now or in the future.")[1].startswith("[now or future]")
    assert js.classify_sponsorship("No sponsorship. Do not apply if you need H-1B or STEM OPT.")[1].startswith("[no STEM OPT]")
    assert js.classify_sponsorship("We do not provide sponsorship; we will not sign any documentation for OPT or CPT.")[0] == "Blocked"


def test_mizuho_refuses_stem_opt_paperwork_is_blocked():
    text = ("Applicants must be currently authorized to work in the United States on a full-time basis. Mizuho does not sponsor "
            "applicants for work visas or offer any employment-based immigration sponsorship for this position. This includes, "
            "but is not limited to, any sponsorship or administrative assistance or execution of any documentation related to "
            "F-1 (CPT/OPT/STEM OPT), TN, O-1, E-3, H-1B1 or H-1B application.")
    assert js.classify_sponsorship(text)[0] == "Blocked"


def test_will_not_participate_in_stem_opt_is_blocked():
    text = "PNC will not provide sponsorship for employment visas or participate in STEM OPT for this position."
    assert js.classify_sponsorship(text)[0] == "Blocked"
