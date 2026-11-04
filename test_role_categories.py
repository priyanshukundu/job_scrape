"""Offline tests for the category chips on the queue page (ROLE_CATEGORIES in job_scout.py).

The page decides category membership in JavaScript (see _EXTRA_SCRIPT): the role text is
lowercased and tested against one regex per category, '\\b(?:term1|term2|...)\\b' with each term
regex-escaped. A row may match several categories; a row matching none gets the key 'other'.
categories_for() below is a Python reimplementation of exactly that logic. It reads the category
list from _extra_script() (the same JSON the page embeds), so the terms under test are the
terms the page ships.
"""
import json
import re

import job_scout
from role_categories_premerge import OLD_ROLE_CATEGORIES as _ALL_OLD_ROLE_CATEGORIES, OLD_ROLE_CATEGORY_EXCLUDE

# Legal/HR/Marketing/Admin was dropped on purpose: its roles are now excluded by title_exclude instead of filed in a chip.
REMOVED_CHIPS = {"Legal/HR/Marketing/Admin"}
OLD_ROLE_CATEGORIES = {k: v for k, v in _ALL_OLD_ROLE_CATEGORIES.items() if k not in REMOVED_CHIPS}


def _page_categories():
    script = job_scout._extra_script()
    start = script.index("var CATEGORIES = ") + len("var CATEGORIES = ")
    end = script.index(";\n", start)
    return json.loads(script[start:end])


_CATS = _page_categories()
_RES = {
    c["label"]: re.compile(r"\b(?:" + "|".join(re.escape(t.lower()) for t in c["terms"]) + r")\b")
    for c in _CATS if c["terms"]
}
# Optional per-category exclusion phrases (same matching): a title containing one never joins the category.
_EXCL = {
    c["label"]: re.compile(r"\b(?:" + "|".join(re.escape(t.lower()) for t in c["exclude"]) + r")\b")
    for c in _CATS if c.get("exclude")
}


# Consolidation of the chips: merged label -> the former chips it absorbed. Sample tests below are keyed by
# the former labels; _n() maps one to the label now on the page. Quantitative Risk/Analytics and
# Quant/Modeling/Pricing stay separate on purpose: Quant/Modeling/Pricing carries exclusions ("valuation
# control", "controllers") that would wrongly drop a "Quantitative Analyst, Valuation Control" title that
# Quantitative Risk/Analytics used to keep. Quantitative Risk/Analytics and Model Risk/Validation share one chip.
MERGES = {
    "Model & Quantitative Risk": ["Quantitative Risk/Analytics", "Model Risk/Validation"],
    "Risk Management (General)": ["Risk & Controls (General)", "Risk Management (General)"],
    "Compliance/Audit": ["Compliance/Financial Crime", "Audit/Assurance"],
    "Fixed Income/Trading/Markets": ["Fixed Income", "Trading/Markets/Derivatives"],
    "Investment Banking/M&A": ["Capital Markets/IB", "M&A/Advisory", "Corporate Finance/Development"],
    "Portfolio/Asset Management & Research": [
        "Portfolio/Asset Management", "Portfolio Manager/Analyst", "Research/Investment Analysis", "Equity Research"],
    "Private Markets/Real Estate/Structured Finance": [
        "Private Equity/Credit", "Real Estate Finance", "Structured Finance/Securitization",
        "Energy/Construction/Infrastructure"],
    "Banking/Lending/Underwriting": ["Commercial Banking/Lending", "Retail/Consumer Banking", "Underwriting"],
    "Wealth/Trust": ["Wealth Management", "Wealth/Trust/Advisory"],
    "FP&A/Financial Analysis": ["FP&A", "Financial Analysis/Business Finance"],
    "Operations/Payments": ["Operations/Servicing", "Payments/Cash Management"],
    "Product/Program/Strategy": ["Product Management", "Program/Project/Business Analysis", "Strategy/Consulting"],
}
_TO_NEW = {old: new for new, olds in MERGES.items() for old in olds}
# Second consolidation (CHIP_MERGES in job_scout.py): neighbouring chips merged again for a shorter row.
_TO_FINAL = {member: new for new, members in job_scout.CHIP_MERGES.items() for member in members}


def _n(old_label):
    first = _TO_NEW.get(old_label, old_label)
    return _TO_FINAL.get(first, first)


def _old_regexes():
    out = {}
    for label, terms in OLD_ROLE_CATEGORIES.items():
        rx = re.compile(r"\b(?:" + "|".join(re.escape(t.lower()) for t in terms) + r")\b")
        ex = OLD_ROLE_CATEGORY_EXCLUDE.get(label)
        exrx = re.compile(r"\b(?:" + "|".join(re.escape(t.lower()) for t in ex) + r")\b") if ex else None
        out[label] = (rx, exrx)
    return out


_OLD = _old_regexes()


def old_categories_for(role):
    """Membership under the pre-merge chips (frozen in role_categories_premerge.py)."""
    text = role.lower()
    hits = [label for label, (rx, ex) in _OLD.items() if rx.search(text) and not (ex and ex.search(text))]
    return hits or ["Other (no category)"]


def mapped_old_categories_for(role):
    return sorted({_n(l) for l in old_categories_for(role)})


def categories_for(role):
    """Labels matched by a role title, or ['Other (no category)'] when none match (mirrors the JS)."""
    text = role.lower()
    hits = [label for label, rx in _RES.items()
            if rx.search(text) and not (label in _EXCL and _EXCL[label].search(text))]
    return hits or ["Other (no category)"]


NEW_CATEGORY_SAMPLES = {
    "Commercial Banking/Lending": (
        ["Relationship Banking Associate", "Special Assets Associate I", "Commercial Credit Senior Associate, CRE"],
        ["Financial Analyst", "Associate Actuary", "Software Developer"],
    ),
    "Portfolio Manager/Analyst": (
        ["Commercial Portfolio Manager II", "Associate Portfolio Manager - Parametric", "Portfolio Analyst"],
        ["Project Manager", "Product Manager", "Senior Accountant"],
    ),
    "Accounting/Controllership/Tax": (
        ["Senior Accountant", "External Reporting Controller Senior Associate", "Tax Manager"],
        ["Financial Analyst", "Budget Analyst", "Account Manager"],
    ),
    "Financial Analysis/Business Finance": (
        ["Senior Financial Analyst", "Finance Business Partner", "Budget Analyst Associate"],
        ["Senior Accountant", "Credit Analyst", "Business Analyst"],
    ),
    "Compliance/Financial Crime": (
        ["AML and Sanction Associate", "Senior Compliance Associate", "KYC Analyst"],
        ["Operating Risk Manager", "Credit Analyst", "Financial Planner"],
    ),
    "Audit/Assurance": (
        ["Associate, Internal Audit", "Risk Assurance Senior Associate", "Senior Audit Manager"],
        ["Quality Assurance Engineer", "Accounting Manager", "Compliance Associate"],
    ),
    "Risk Management (General)": (
        ["Risk Manager", "Senior Risk Analyst", "First Line Risk and Control Associate Director"],
        ["Portfolio Manager", "Credit Analyst", "Software Developer"],
    ),
    "Trading/Markets/Derivatives": (
        ["Associate Trader, CMO Derivatives Desk", "Trade Support Analyst", "Equity Derivatives Structuring Associate"],
        ["Financial Analyst", "Relationship Manager", "Accounting Associate"],
    ),
    "Structured Finance/Securitization": (
        ["Structured Finance - Asset-Backed Securities, Analyst", "Structured Credit & CLOs, Senior Analyst",
         "Associate, Securitization"],
        ["Financial Analyst", "Commercial Portfolio Manager", "Equity Research Analyst"],
    ),
    "M&A/Advisory": (
        ["Mergers & Acquisitions Senior Associate", "IB Associate - Energy", "Associate (Financial Restructuring)"],
        ["Internal Audit Associate", "Product Manager", "Credit Analyst"],
    ),
    "Research/Investment Analysis": (
        ["Senior Research Analyst", "Investment Analyst, Public Credit", "Credit Research Analyst"],
        ["Financial Analyst", "Risk Analyst", "Operations Associate"],
    ),
    "Quant/Modeling/Pricing": (
        ["Quantitative Research Analyst", "Senior Associate - Valuation", "Quant Developer"],
        ["Financial Analyst", "Software Developer", "Credit Analyst"],
    ),
    "Operations/Servicing": (
        ["Derivatives Operations Analyst", "Associate, FX Settlements", "Lead Client Servicing Associate"],
        ["Financial Analyst", "Strategy Associate", "Operating Risk Manager"],
    ),
    "Technology/Data": (
        ["Software Engineer", "Data Engineer II", "Cyber Security Manager"],
        ["Financial Analyst", "Credit Officer", "Wealth Advisor"],
    ),
    "Product Management": (
        ["Senior Product Manager - Travel", "Associate, Product Management", "Product Owner, Payments Platform"],
        ["Product Controller", "Project Manager", "Marketing Associate"],
    ),
    "Strategy/Consulting": (
        ["Corporate Strategy Associate", "Senior Consultant", "Management Consulting Analyst"],
        ["Financial Analyst", "Operations Associate", "Tax Manager"],
    ),
    "Program/Project/Business Analysis": (
        ["Senior Business Analyst", "Program Manager, Transformation", "Project Management Office Lead (PMO)"],
        ["Financial Analyst", "Product Manager", "Project Finance Associate"],
    ),
    "Sales/Business Development": (
        ["Business Development Representative", "Institutional Sales Associate", "Investor Relations Analyst"],
        ["Financial Analyst", "Credit Analyst", "Salesforce Administrator"],
    ),
    "Retail/Consumer Banking": (
        ["Associate Community Banker", "Branch Manager", "Retail Management Associate"],
        ["Financial Analyst", "Commercial Credit Analyst", "Portfolio Manager"],
    ),
    "Wealth/Trust/Advisory": (
        ["Wealth Advisor", "Trust Administrator Associate", "Private Bank Associate"],
        ["Financial Analyst", "Operations Associate", "Commercial Banker"],
    ),
    "Payments/Cash Management": (
        ["Payments Sales Senior Associate", "Cash Management Officer", "Merchant Services Analyst"],
        ["Financial Analyst", "Credit Analyst", "Treasury Analyst"],
    ),
    "Insurance/Actuarial": (
        ["Associate Actuary", "Property Claims Manager", "Insurance Associate"],
        ["Financial Analyst", "Credit Analyst", "Risk Manager"],
    ),
    "Early Careers/Internships": (
        ["Summer Analyst Program", "2027 Associate Development Program", "Software Engineering Intern"],
        ["Senior Financial Analyst", "Vice President, Credit", "Director of Operations"],
    ),
    "Energy/Construction/Infrastructure": (
        ["Associate, Construction Finance", "IB Associate - Energy", "Project Finance Associate Lead"],
        ["Financial Analyst", "Credit Analyst", "Wealth Advisor"],
    ),
}

# Existing categories: their terms are unchanged, sample titles must keep mapping to them.
EXISTING_SAMPLES = {
    "Treasury/ALM/Liquidity": "Treasury Analyst",
    "FP&A": "Senior Associate, FP&A",
    "Credit Risk": "Credit Risk Analyst",
    "Fixed Income": "Fixed Income Sales Associate",
    "Operational Risk": "Operational Risk Manager",
    "Quantitative Risk/Analytics": "Quantitative Analyst",
    "Capital Planning": "CCAR Capital Planning Lead",
    "Model Risk/Validation": "Model Validation Associate",
    "Market Risk": "Market Risk Analyst",
    "Capital Markets/IB": "Capital Markets Associate",
    "Risk & Controls (General)": "Technology Risk Manager",
    "Portfolio/Asset Management": "Asset Management Analyst",
    "Underwriting": "Underwriting Associate",
    "Real Estate Finance": "Commercial Real Estate Analyst",
    "Private Equity/Credit": "Private Credit Associate",
    "Wealth Management": "Wealth Management Associate",
    "Equity Research": "Equity Research Associate",
    "Corporate Finance/Development": "Corporate Development Associate",
}


def test_every_category_is_covered_by_a_sample():
    assert set(EXISTING_SAMPLES) | set(NEW_CATEGORY_SAMPLES) == set(OLD_ROLE_CATEGORIES)
    assert set(EXISTING_SAMPLES).isdisjoint(NEW_CATEGORY_SAMPLES)


def test_chip_list_is_the_merged_list_in_first_member_order_then_other():
    labels = [c["label"] for c in _CATS]
    assert labels[-1] == "Other (no category)"
    assert labels[:-1] == list(dict.fromkeys(_n(l) for l in OLD_ROLE_CATEGORIES))
    assert len(labels) == 15


def _expected_present(old_label, title):
    """The merged chip holds the title iff any former member chip did."""
    return _n(old_label) in mapped_old_categories_for(title)


def test_category_representative_titles_match():
    for label, (hits, _misses) in NEW_CATEGORY_SAMPLES.items():
        for title in hits:
            assert _n(label) in categories_for(title), (label, title, categories_for(title))


def test_category_near_misses_do_not_match():
    # A near miss for one former chip may legitimately belong to a sibling that was merged into the same
    # chip; so the merged chip must hold it exactly when some former member did.
    for label, (_hits, misses) in NEW_CATEGORY_SAMPLES.items():
        for title in misses:
            assert label not in old_categories_for(title), (label, title)
            assert (_n(label) in categories_for(title)) == _expected_present(label, title), (label, title)


def test_existing_category_samples_still_map():
    for label, title in EXISTING_SAMPLES.items():
        assert _n(label) in categories_for(title), (label, title, categories_for(title))


def test_no_term_was_lost_in_the_merge():
    for old_label, old_terms in OLD_ROLE_CATEGORIES.items():
        new_terms = job_scout.ROLE_CATEGORIES[_n(old_label)]
        assert set(old_terms) <= set(new_terms), old_label
    unions = {}
    for old_label, old_terms in OLD_ROLE_CATEGORIES.items():
        unions.setdefault(_n(old_label), set()).update(old_terms)
    for new_label, union in unions.items():
        assert set(job_scout.ROLE_CATEGORIES[new_label]) == union, new_label
        assert len(job_scout.ROLE_CATEGORIES[new_label]) == len(union)  # deduped


def test_exclusions_moved_to_merged_labels_and_only_cover_one_member():
    # An exclusion must not be able to hide another member's terms: it is only moved onto a merged chip
    # when that chip's other members share no title with the excluded phrase.
    assert job_scout.ROLE_CATEGORY_EXCLUDE == {
        "Treasury/ALM/Liquidity": ["sales"],
        "Portfolio/Asset Mgmt/Wealth": ["operations research"],
        "Quant/Modeling/Pricing": ["valuation controller", "valuation control", "controllers", "valuation advisory"],
    }
    assert set(job_scout.ROLE_CATEGORY_EXCLUDE) <= set(job_scout.ROLE_CATEGORIES)


def test_merge_table_reproduces_old_membership_on_sample_titles():
    titles = set()
    for hits, misses in NEW_CATEGORY_SAMPLES.values():
        titles.update(hits)
        titles.update(misses)
    titles.update(EXISTING_SAMPLES.values())
    for _label, hits, misses in TIGHTENED:
        titles.update(hits)
        titles.update(misses)
    titles.update([
        "Operations Research Analyst, Senior", "Valuation Controller - Vice President - Rates",
        "Senior Associate - Valuation", "Treasury Management Sales Officer Senior", "Treasury Analyst",
        "Equities Electronic Trading - J.P. Morgan NYSE Floor Broker - Associate", "Credit Risk Manager",
        "Zamboni Driver", "Quantitative Analyst, Valuation Control",
    ])
    assert len(titles) > 100
    for title in sorted(titles):
        assert sorted(categories_for(title)) == mapped_old_categories_for(title), (title, categories_for(title), mapped_old_categories_for(title))


def test_quantitative_title_with_valuation_control_keeps_quantitative_risk_chip():
    assert "Model & Quantitative Risk" in categories_for("Quantitative Analyst, Valuation Control")


def test_unmatched_title_stays_in_other():
    assert categories_for("Zamboni Driver") == ["Other (no category)"]
    assert categories_for("Chief Happiness Wizard") == ["Other (no category)"]


def test_multiple_categories_allowed():
    cats = categories_for("Credit Risk Manager")
    assert "Credit Risk/Lending" in cats and "Risk/Compliance/Op Risk" in cats


# Terms tightened from bare words to multi-word terms (or excluded): each entry is
# (label, titles that must still match, near-miss titles that must not match that label).
TIGHTENED = [
    ("Technology/Data",
     ["Software Engineer", "Applied AI/ML Senior Associate", "Associate Director, Lead Cloud Architect",
      "Staff IT Analyst II", "Business Intelligence Developer", "Associate Director Engineering"],
     ["IT Risk Associate II", "AI Risk Management Manager", "Equity Research Associate - Medical Technology",
      "Technology Risk Management Specialist", "Credit Risk Strategy and Analytics", "Senior FP&A Analyst - Public Cloud",
      "Portfolio Construction & Risk, Senior Analyst", "Senior Risk Engineer", "Investment Banking Associate - Technology M&A"]),
    ("Strategy/Consulting",
     ["Corporate Strategy Associate", "Associate Manager, Strategy & Operations", "Oliver Wyman Senior Consultant",
      "Senior Associate, Strategic Initiatives", "Associate, Growth Strategy"],
     ["Credit Risk Strategy Manager I", "Portfolio Strategy Analyst", "Treasury Management Consultant III",
      "Trading Strategy Associate", "Regional Consultant - US Wealth", "Strategic Analytics - Senior Associate"]),
    ("Commercial Banking/Lending",
     ["Corporate Banker", "Commercial Banking Associate", "Portfolio Banker - Middle Market"],
     ["Investment Banking Associate - Equity Capital Markets", "Investment Banking and Capital Markets-Banker-Healthcare Analyst",
      "Global Lead, Commercial Risk Strategy"]),
    ("Retail/Consumer Banking",
     ["Retail Management Associate", "Deposit Operations Associate", "Retail Loan Credit Risk Reviewer"],
     ["Investment & Corporate Banking - Consumer & Retail, Associate", "Equity Research Associate, Food Retail",
      "Deposit & Liquidity Product Management", "Senior Lead Product Manager, Deposit Pricing"]),
    ("Wealth/Trust/Advisory",
     ["Wealth Advisor", "Private Wealth Associate", "Trust Administrator Associate", "Trust Portfolio Manager"],
     ["Asset & Wealth Management, Investment Risk Management, Equities, Associate", "Quant Analytics Senior Associate - Trust & Security",
      "Equity Research Associate - Trust Banks"]),
    ("Energy/Construction/Infrastructure",
     ["Associate, Construction Lending", "Power & Utilities Investment Banking Associate", "Coordinator Capital Projects I"],
     ["Portfolio Construction Analyst", "Air Power Defense Portfolio Director"]),
    ("Payments/Cash Management",
     ["Merchant Services Analyst", "Card Solutions Product Manager"],
     ["Senior Business Analyst - Card Loss Forecasting", "Merchant Banking Associate", "Merchant Strategy & Operations"]),
    ("Program/Project/Business Analysis",
     ["Implementation Manager", "Senior Implementation Consultant"],
     ["Analyst, Portfolio Implementation", "Director, Implementation Risk"]),
    ("Sales/Business Development",
     ["Distribution Sales Associate"],
     ["Distribution Center Associate", "Associate Director, Warehousing & Distribution"]),
    ("Trading/Markets/Derivatives",
     ["Equity Derivatives Structuring Associate", "Equity Structuring Associate"],
     ["Corporate Structuring Associate", "Alternatives Fund Structuring, Associate"]),
    ("M&A/Advisory",
     ["Leveraged Finance Associate", "Capital Solutions Group Vice President", "Debt Capital Market Analyst"],
     ["Leveraged Finance Portfolio Manager", "Credit Analyst - Leveraged Finance Portfolio Management"]),
]

# Known false positives fixed with the exclusion list or rewording.
def test_operations_research_is_not_research_analysis():
    assert "Portfolio/Asset Mgmt/Wealth" not in categories_for("Operations Research Analyst, Senior")
    assert "Portfolio/Asset Mgmt/Wealth" in categories_for("Senior Research Analyst")


def test_valuation_controller_is_not_quant():
    assert "Quant/Modeling/Pricing" not in categories_for("Valuation Controller - Vice President - Rates")
    assert "Accounting/Controllership/Tax" in categories_for("Valuation Controller - Vice President - Rates")
    assert "Quant/Modeling/Pricing" in categories_for("Senior Associate - Valuation")


def test_floor_broker_stays_in_trading():
    assert "Fixed Income/Trading/Markets" in categories_for("Equities Electronic Trading - J.P. Morgan NYSE Floor Broker - Associate")


def test_tightened_terms_keep_positives_and_reject_near_misses():
    for label, hits, misses in TIGHTENED:
        for title in hits:
            assert _n(label) in categories_for(title), (label, title, categories_for(title))
        for title in misses:
            assert label not in old_categories_for(title), (label, title)
            assert (_n(label) in categories_for(title)) == _expected_present(label, title), (label, title)


def test_bare_ambiguous_terms_are_gone():
    # These bare words matched far too broadly; only multi-word forms are allowed now.
    bare = {
        "Technology/Data": {"it", "ai", "analytics", "technology", "engineering", "cloud", "architect", "engineer"},
        "Strategy/Consulting": {"strategy", "strategic", "consultant"},
        "Retail/Consumer Banking": {"retail", "deposit", "deposits"},
        "Wealth/Trust/Advisory": {"wealth", "trust"},
        "Energy/Construction/Infrastructure": {"power", "construction"},
        "Payments/Cash Management": {"card", "merchant"},
        "Commercial Banking/Lending": {"banker", "banking associate", "commercial risk"},
        "Program/Project/Business Analysis": {"implementation"},
        "Sales/Business Development": {"distribution"},
        "Trading/Markets/Derivatives": {"structuring"},
        "M&A/Advisory": {"leveraged finance"},
    }
    for label, words in bare.items():
        assert not words & set(job_scout.ROLE_CATEGORIES[_n(label)]), label


def test_new_family_terms_catch_leftover_titles():
    cases = {
        "Portfolio Manager/Analyst": ["Portfolio Construction Analyst", "Portfolio Strategy Analyst"],
        "Risk Management (General)": ["Risk Analysis Specialist II", "Vice President, Risk / Policy Management", "Capital Risk Vice President"],
        "Operations/Servicing": ["Associate, Derivative Operations", "Loan Funding Specialist", "Associate - Investor Services"],
        "Financial Analysis/Business Finance": ["Director, Capital Controls & Planning", "Finance Control Manager - Senior Associate"],
        "M&A/Advisory": ["Vice President, Capital Solutions"],
        "Insurance/Actuarial": ["Health & Benefits Consultant, Senior Associate"],
        "Technology/Data": ["Data Protection Associate", "Data Visualization Senior Associate"],
    }
    for label, titles in cases.items():
        for title in titles:
            assert _n(label) in categories_for(title), (label, title, categories_for(title))


def test_treasury_sales_roles_are_sales_not_treasury():
    for title in ["Treasury Management Sales Officer Senior", "Sales Executive, Treasury Solutions",
                  "Pre-Sales Solution Specialist, Treasury & Payments (Quantum)", "Director, Treasury Management Sales"]:
        hits = categories_for(title)
        assert "Other Business Roles" in hits and "Treasury/ALM/Liquidity" not in hits, (title, hits)
    # real treasury roles keep their chip
    for title in ["Treasury Analyst", "Treasury Manager", "Liquidity Risk Manager, Treasury"]:
        assert "Treasury/ALM/Liquidity" in categories_for(title), title


def test_admin_hr_marketing_roles_are_excluded_by_title_not_filed_in_a_chip():
    import json
    from keyword_matching import contains_term
    exc = json.load(open("data/config.json"))["filters"]["title_exclude"]
    for title in ["Executive Assistant, Capital Markets", "Administrative Assistant III- Risk", "Corporate Communications Associate",
                  "Senior Associate, Global Rewards", "Consultant, Pay Equity", "Associate, Legal Operations",
                  "Social Media Coordinator", "Training and Development Associate", "Senior Associate - Recruiting Events"]:
        assert any(contains_term(title, t) for t in exc), title
    for title in ["Treasury Analyst", "Credit Risk Associate", "Legal Entity Controller Associate", "Quantitative Analyst"]:
        assert not any(contains_term(title, t) for t in exc), title
    assert "Legal/HR/Marketing/Admin" not in job_scout.ROLE_CATEGORIES
