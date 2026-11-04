"""Back-office, servicing and payments-operations titles are excluded; treasury, risk and trade-support titles stay."""
import json
import pytest
import job_scout as js

FILTERS = json.load(open("data/config.json", encoding="utf-8"))["filters"]


def _job(title):
    return {"title": title, "location": "New York, NY"}


@pytest.mark.parametrize("title", [
    "Client Servicing Associate",
    "Client Onboarding Associate Analyst",
    "Associate, FX Settlements",
    "Loan Documentation & Processing Associate Analyst",
    "Loan Servicing Business Analyst",
    "Associate, Fund Operations",
    "Transfer Agent Operations Specialist (Global Capital Markets Support Services) - Hybrid",
    "Associate Analyst, Corporate Actions - Specialized Service Team",
    "Derivatives Operations Analyst",
    "Associate, Payment Integrity IBR",
    "Associate Manager, Merchant Onboarding and Experience",
    "Banking Services Operations Associate II - Lockbox Processor",
    "Collateral Release Specialist - Charleston",
    "Senior Associate, Process Manager",
    "Associate, Middle Office - US Equity",
])
def test_ops_payments_titles_excluded(title):
    assert not js.matches(_job(title), FILTERS)


@pytest.mark.parametrize("title", [
    "Treasury Operations Analyst - Insurance Liquidity Forecast",
    "Treasury Operations Associate",
    "Liquidity Risk Analyst",
    "Cash Management Product Strategy Associate",
    "Model Validation Associate",
    "Credit Risk Associate",
    "Trade Support Analyst",
    "Assistant Manager, Trade Support Fixed Income Middle Office",
    "Collateral Management Analyst",
    "Assistant Vice President, Collateral & Intraday Liquidity Management",
    "Counterparty Credit Risk - Collateral and Margin Associate",
    "Credit Risk Middle Office Analyst",
    "Regulatory Reporting Operations Analyst",
    "Senior Treasury Analyst - Global Payment Operations",
    "Treasury Analyst, Global Treasury Reconciliation",
    "Principal Financial Analyst - Card Operations",
    "Lead Director, FP&A - Aetna Service Operations",
])
def test_protected_titles_kept(title):
    assert js.matches(_job(title), FILTERS)
