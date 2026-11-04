"""Offline regression tests for classify_salary and the structured-pay helpers (snippets saved from live
postings, trimmed). Positives pin each newly supported shape; negatives pin the false-positive guards."""
import json

import pytest

import job_scout as js


def sal(text):
    return js.classify_salary(text)[0]


# positives

@pytest.mark.parametrize("text,expected", [
    # legacy shapes keep their exact output
    ("The salary range is $120,000 - $150,000 per year.", "$120,000 - $150,000"),
    ("Base salary: $95k to $120k", "$95,000 - $120,000"),
    ("Compensation $180,000", "$180,000"),
    # dashes and separators
    ("Annual base salary range (excluding equity and bonus): $167,280 \u2014 $196,800 USD Application Limit", "$167,280 - $196,800"),
    ("Salary range: $120,000 \u2013 $150,000", "$120,000 - $150,000"),
    ("Salary range: $120,000 ‒ $150,000", "$120,000 - $150,000"),
    ("The pay range for this position is expected to be between $68,000 and $107,000/year, with", "$68,000 - $107,000"),
    ("Expected base pay rates will be between 140,000 USD and 180,000 USD. Actual salaries vary", "$140,000 - $180,000"),
    ("Salary Range = 165,000 - 260,000 USD Annual + Benefits + Bonus", "$165,000 - $260,000"),
    ("The salary range for this role is 157,200 USD - 298,800 USD. Good faith estimate", "$157,200 - $298,800"),
    ("Salary Range 80,044.50USD -108,295.50 USD", "$80,044 - $108,295"),
    ("the salary range for this position is USD$137,500.00 - USD$150,000.00 . Additionally", "$137,500 - $150,000"),
    ("The base salary range is USD 80,000 - 150,000 This range takes into account", "$80,000 - $150,000"),
    ("salary: $73150 - $99750 / year", "$73,150 - $99,750"),
    ("Salary $ 159-193K depending on job-related knowledge", "$159,000 - $193,000"),
    ("salary of 120K-150K plus bonus", "$120,000 - $150,000"),
    ("The salary range is 90,000 to 110,000 USD", "$90,000 - $110,000"),
    ("A reasonable estimate of the current range is $180,000 to $355,500. You may also", "$180,000 - $355,500"),
    ("OTHER DETAILS $90K \u2013 $125K / year Pay determined based on job-related knowledge", "$90,000 - $125,000"),
    ("Min USD $82,400.00/Yr. Max USD $109,867.00/Yr. Job Summary This position", "$82,400 - $109,867"),
    ("Minimum Salary: $155,000.00 per year Maximum Salary: $225,000.00 per year The minimum and", "$155,000 - $225,000"),
    ("CO/FL/TX: Minimum Salary $114,200, Maximum Salary $207,200 IL/PA: Minimum Salary $120,000", "$114,200 - $207,200"),
    ("Base Pay Range: Minimum: $78,750.00 USD Maximum: $130,000.00 USD We Value", "$78,750 - $130,000"),
    ("Pay Range Minimum: $129,100.00 Pay Range Maximum: $214,500.00 Base pay is", "$129,100 - $214,500"),
    ("The base salary for this position ranges from $135,000/year in our lowest geographic market up to $200,000/year in our highest", "$135,000 - $200,000"),
    ("The base salary for this position ranges from $138,771 and $208,156 USD Annual. However", "$138,771 - $208,156"),
    ("Salary range is expected to be between $113,000.00 CAD - $163,000.00 CAD Employees", "C$113,000 - C$163,000"),
    # currencies other than USD keep their own symbol
    ("Salary: £55,000 - £65,000 per annum", "£55,000 - £65,000"),
    ("Salary range: €70,000 - €85,000", "€70,000 - €85,000"),
    ("Salary range: CAD $90,000 - $110,000", "C$90,000 - C$110,000"),
    # up to / from single values
    ("Salary up to $140,000 depending on experience", "up to $140,000"),
    ("Starting salary: minimum of $85,000", "from $85,000"),
])
def test_annual_shapes(text, expected):
    assert sal(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("the base pay for this position is generally between $33.77 and $40.87 per hour. Actual", "$33.77 - $40.87/hr (~$70.2k - $85k/yr)"),
    ("comparable skill set. Salary $28.01 - $38.75 USD Hourly Travel Percentage: 100%", "$28.01 - $38.75/hr (~$58.3k - $80.6k/yr)"),
    ("The pay rate for this position is $33.65 hourly. The hourly pay rate will convert", "$33.65/hr (~$70k/yr)"),
    ("Compensation Starting base salary: $41.83 - $66.81 per hour. Exact", "$41.83 - $66.81/hr (~$87k - $139k/yr)"),
    ("an hourly wage in the range of $37.98hr - $43.00hr , depending on", "$37.98 - $43/hr (~$79k - $89.4k/yr)"),
    ("Pay rate: $45/hr", "$45/hr (~$93.6k/yr)"),
    ("salary of $8,000 - $10,000 per month", "$8,000 - $10,000/mo (~$96k - $120k/yr)"),
    ("Base salary $2,000 - $2,500 per week", "$2,000 - $2,500/wk (~$104k - $130k/yr)"),
])
def test_period_conversion_is_labelled(text, expected):
    assert sal(text) == expected
    assert "converted from" in js.classify_salary(text)[1] or "assumed" in sal(text)


def test_multiple_location_ranges_report_widest():
    text = ("Base Salary Ranges Please review the job posting for the location of this specific opportunity. "
            "$122,000.00 - $209,000.00 for the location of: Maryland, Colorado and remote workers "
            "$135,000.00 - $230,000.00 for the location of: Washington, D.C. "
            "$153,000.00 - $261,000.00 for the location of: New York, California")
    s, ev = js.classify_salary(text)
    assert s == "$122,000 - $261,000" and "3 ranges listed" in ev


def test_repeated_identical_range_is_not_a_table():
    text = "Salary range $100,000 - $120,000. Compensation: USD 100,000 - 120,000 per year."
    s, ev = js.classify_salary(text)
    assert s == "$100,000 - $120,000" and "ranges listed" not in ev


def test_base_range_beats_ote_range():
    text = ("the pay range for this position is expected to be between $68,000 and $107,000/year, with an expected "
            "On Target Earnings (OTE) between $77,000 and $121,000/year (including bonus or commission).")
    assert sal(text) == "$68,000 - $107,000"


def test_far_apart_ranges_are_not_merged():
    text = "Salary range $100,000 - $120,000. " + "x " * 400 + "Compensation $200,000 - $250,000"
    assert sal(text) == "$100,000 - $120,000"


# negatives

@pytest.mark.parametrize("text", [
    "Manages a $500M portfolio and a salary review process",
    "Compensation committee oversees $2 billion in assets under management",
    "We manage assets of $30 million - $50 million; salary is competitive",
    "salary: our funds returned $250,000 - $300,000 million",
    "Annual revenue of $120,000,000 and competitive compensation",
    "Competitive compensation including a sign-on bonus of $25,000",
    "Compensation package includes a $25,000 sign-on bonus for qualified candidates",
    "401(k) match up to $5,000 per year; competitive salary",
    "Compensation: bonus range $30,000 - $50,000 annually",
    "Compensation includes retention bonus of $40,000 - $60,000",
    "Equity grant valued at $200,000 as part of total compensation",
    "Relocation assistance of $30,000 is provided; salary discussed at interview",
    "Total compensation includes On Target Earnings (OTE) of $150,000 - $200,000",
    "Tuition reimbursement up to $50,000 as part of compensation",
    "A competitive salary. " + "word " * 120 + "The fund holds positions of $120,000 - $150,000.",
    "We offer a great salary and benefits. 5 to 10 years of experience, 3 - 5 reports",
    "salary history is not requested; the role spans 2020 - 2024 projects",
    "Minimum wage in Seattle is $19.97 per hour; compensation is discussed during the interview",
    "Hourly staff earn overtime. Pay transparency: $4,000 per hour consulting fees were reviewed",
    "A compensation consultant said $30 per week, or $12 - $14 gift cards, are common",
    "salary $45 - $55",  # no unit and no pay-range label: too ambiguous to call hourly
    "Our CEO's salary of $5 million was disclosed",
    "Salary: $1,000,000 - $2,000,000",
])
def test_negatives(text):
    assert sal(text) == "Unclear", js.classify_salary(text)


def test_excluding_bonus_does_not_reject_the_base_range():
    assert sal("Annual base salary range (excluding equity and bonus): $150,000 - $180,000") == "$150,000 - $180,000"


def test_plus_bonus_after_range_still_counts():
    assert sal("Salary range $150,000 - $180,000 + bonus and equity") == "$150,000 - $180,000"


# structured fields

def test_pay_text_shapes():
    assert js._pay_text("USD", 150000, 225000) == " Compensation: USD 150,000 - 225,000 per year."
    assert js._pay_text("usd", 31.5, 36, "HOUR") == " Compensation: USD 31.5 - 36 per hour."
    assert js._pay_text("USD", 0, 0) == ""
    assert js._pay_text("", 100000, 120000) == ""
    assert js._pay_text("USD", 100000, 120000, "DAY") == ""
    assert sal(js._pay_text("USD", 150000, 225000)) == "$150,000 - $225,000"
    assert sal(js._pay_text("USD", 31.5, 36, "HOUR")) == "$31.50 - $36/hr (~$65.5k - $74.9k/yr)"
    assert sal(js._pay_text("USD", 125000, 125000)) == "$125,000"


def test_jsonld_base_salary():
    page = ('<script type="application/ld+json">{"@type":"JobPosting","title":"x","baseSalary":{"@type":"MonetaryAmount",'
            '"currency":"USD","value":{"@type":"QuantitativeValue","minValue":120000.00,"maxValue":135000.00,"unitText":"YEAR"}}}</script>')
    assert sal(js._jsonld_pay(page)) == "$120,000 - $135,000"
    hourly = page.replace("120000.00", "30").replace("135000.00", "40").replace("YEAR", "HOUR")
    assert sal(js._jsonld_pay(hourly)) == "$30 - $40/hr (~$62.4k - $83.2k/yr)"
    zeros = page.replace("120000.00", "0").replace("135000.00", "0")
    assert js._jsonld_pay(zeros) == ""  # Jibe boards publish 0/0 placeholders
    graph = '<script type="application/ld+json">{"@graph":[' + json.dumps(json.loads(page.split(">", 1)[1].rsplit("<", 1)[0])) + ']}</script>'
    assert sal(js._jsonld_pay(graph)) == "$120,000 - $135,000"
    assert js._jsonld_pay("<html>no ld</html>") == ""


def test_greenhouse_pay_input_ranges_reach_describe(monkeypatch):
    payload = {"content": "&lt;p&gt;Build things.&lt;/p&gt;", "pay_input_ranges": [
        {"min_cents": 16728000, "max_cents": 19680000, "currency_type": "USD", "title": "Annual base salary range:"}]}
    monkeypatch.setattr(js, "http", lambda url, **kw: payload)
    text, _, _ = js.describe(dict(ats="greenhouse", slug="x"), dict(id="1"))
    assert sal(text) == "$167,280 - $196,800"
    payload["pay_input_ranges"] = [{"min_cents": 3100, "max_cents": 3600, "currency_type": "USD"}]
    text, _, _ = js.describe(dict(ats="greenhouse", slug="x"), dict(id="1"))
    assert sal(text).startswith("$31 - $36/hr")
    payload["pay_input_ranges"] = [{"min_cents": 300000, "max_cents": 900000, "currency_type": "USD"}]  # $3k-$9k: unit unknowable
    assert sal(js.describe(dict(ats="greenhouse", slug="x"), dict(id="1"))[0]) == "Unclear"


def test_lever_salary_range_reaches_desc(monkeypatch):
    posting = dict(id="a", text="Analyst", hostedUrl="https://x/a", categories={"location": "NY"}, descriptionPlain="Do work.",
                   salaryRange={"currency": "USD", "interval": "per-year-salary", "min": 125000, "max": 160000})
    monkeypatch.setattr(js, "http", lambda url, **kw: [posting])
    jobs, _ = js.lever(dict(slug="x"))
    assert sal(jobs[0]["desc"]) == "$125,000 - $160,000"
    posting["salaryRange"] = {"currency": "USD", "interval": "per-hour-wage", "min": 30, "max": 40}
    assert sal(js.lever(dict(slug="x"))[0][0]["desc"]).startswith("$30 - $40/hr")


def test_ashby_salary_component_only(monkeypatch):
    comp = {"summaryComponents": [
        {"compensationType": "EquityPercentage", "interval": "NONE", "currencyCode": None, "minValue": None, "maxValue": None},
        {"compensationType": "Bonus", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 30000, "maxValue": 40000},
        {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD", "minValue": 211400, "maxValue": 290600}]}
    monkeypatch.setattr(js, "http", lambda url, **kw: {"jobs": [dict(id="1", title="Eng", location="NY", jobUrl="https://x/1",
                                                                      descriptionPlain="Build.", publishedAt="2026-10-01", compensation=comp)]})
    jobs, _ = js.ashby(dict(slug="x"))
    assert sal(jobs[0]["desc"]) == "$211,400 - $290,600"
    comp["summaryComponents"] = comp["summaryComponents"][:2]
    assert sal(js.ashby(dict(slug="x"))[0][0]["desc"]) == "Unclear"  # bonus-only is never reported as salary


def test_unlabelled_small_pay_pair_is_not_guessed_as_hourly():
    # no stated pay period: never assume hourly, leave it Unclear
    assert sal("compensation programs. Base Pay Range $31 \u2014 $36 USD Benefits: competitive") == "Unclear"
