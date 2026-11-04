"""Branch-side banking titles are excluded; adjacent finance roles stay."""
import json
import pytest
import job_scout as js

FILTERS = json.load(open("data/config.json", encoding="utf-8"))["filters"]


def _job(title):
    return {"title": title, "location": "New York, NY"}


@pytest.mark.parametrize("title", [
    "Branch Operations Associate Manager Palms",
    "Senior Branch Premier Banker Capital District",
    "Associate Branch Manager, Ameriprise Financial Advisors - Richmond, VA",
    "CA WA Private Mortgage Banking Associate Manager",
    "Client Experience Associate - Piedmont Market",
    "Registered Client Associate",
    "J.P. Morgan Wealth Management - Private Client Investment Associate - Bethesda",
    "Client Associate",
    "Associate Roadway Engineer",
    "Souscripteur adjoint ou souscriptrice adjointe, Équipe rationalisation de portefeuille",
    "Associate Auto Claims Adjuster Upskill",
    "Associate Territory Manager, Neurovascular - Dallas, TX",
    "Associate Relationship  Banker - Austin Central Market - Austin ,TX",
])
def test_branch_titles_excluded(title):
    assert not js.matches(_job(title), FILTERS)


@pytest.mark.parametrize("title", [
    "Mortgage Quantitative Analyst, VP",
    "Corporate Banker - Global Capital Management - Vice President",
    "Retail Credit Risk Expert",
    "Treasury Associate",
    "Credit Risk Review Analyst",
])
def test_adjacent_roles_kept(title):
    f = dict(FILTERS, title_include=[])
    assert js.matches(_job(title), f)


@pytest.mark.parametrize("title", [
    "Bilingual Jr Relationship Manager, Associate",
    "Credit Associate (Bilingual Japanese/English Required)",
    "Senior Treasury Analyst (Japanese Speaking)",
    "Surveillance Associate, Japanese Language Position",
    "Credit Risk Management Consultant with German",
    "Associate, Core Service (Mandarin/Cantonese required)",
])
def test_language_required_titles_excluded(title):
    assert not js.matches(_job(title), FILTERS)


@pytest.mark.parametrize("title", [
    "Environmental Services Associate (Housekeeping-PT Evening) West Penn Hospital",
    "Transport Services Associate - Full Time - Wexford Hospital",
    "Materials Management Associate, AGH, Full Time",
    "Materials Handler Associate, Full Time, Forbes",
    "Maintenance Mechanic Associate (Evenings) I Grove City Hospital",
    "Cross Functional Associate I Wexford Hospital",
])
def test_hospital_support_titles_excluded(title):
    assert not js.matches(_job(title), FILTERS)


@pytest.mark.parametrize("title", [
    "Product Manager: Capital One Shopping (Remote-Eligible)",
    "Senior Product Delivery Associate - Data Platforms",
    "Broker Dealer Regional Consultant - Michigan, VP II",
    "Asset Management - Internal Client Advisor - Retirement Plan Specialist - Associate",
    "J. P. Morgan Advisor- Investment Associate - Dallas, TX",
    "AWM, Private Bank, Regional Lending Advisor, Associate - Los Angeles",
    "Dedicated Lending Associate SAFE Act - B10 - GARDENA",
    "Communications Senior Associate",
    "Senior Associate HRBP- Premium Products/Shopping",
    "Payments Sales Senior Associate",
    "Alternative Investments Sales Desk Associate",
    "Full- stack Engineer 5 - Capital One Shopping Tech",
    "Android Mobile Engineer - Marcus - Associate",
    "Senior Java Developer - Rates Derivatives Algo & E-Trading",
    "Production Engineer, Global Banking & Markets",
    "Process Automation Engineer - Investment Management, VP-I",
    "Associate Information Security Specialist III",
])
def test_2026_10_23_wrong_category_excluded(title):
    f = dict(FILTERS, title_include=[])
    assert not js.matches(_job(title), f)


@pytest.mark.parametrize("title", [
    "Treasury Management & Payments Consultant",
    "Wholesale Credit Risk Product Owner",
    "Information Security Risk Oversight Professional",
    "Quantitative Analytics Associate - Co-brand Card Data Analytics",
])
def test_2026_10_23_adjacent_kept(title):
    f = dict(FILTERS, title_include=[])
    assert js.matches(_job(title), f)


@pytest.mark.parametrize("title", [
    "Associate Technician, Operations",
    "Associate Maintenance Technician(Electrician)",
    "Pharmacy Technician Associate Representative",
    "Associate Biomed Technician",
    "Associate Application Developer",
])
def test_2026_10_26_trades_and_app_dev_excluded(title):
    f = dict(FILTERS, title_include=[])
    assert not js.matches(_job(title), f)


@pytest.mark.parametrize("title", [
    "Quantitative/Data Developer | Fixed Income",
    "Portfolio Management Administrative Support Associate",
    "Client Quant Developer, BQuant Desktop",
])
def test_2026_10_26_adjacent_kept(title):
    f = dict(FILTERS, title_include=[])
    assert js.matches(_job(title), f)
