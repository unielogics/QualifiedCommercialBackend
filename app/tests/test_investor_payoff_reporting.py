"""One investor-safe calculation reaches frozen terms, packets and the draft."""
import json
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import fitz

from app.services.deal_budget import calculate, reporting
from app.services.financing_budget_disclosure import (
    frozen_budget,
    public_budget,
    render_budget_html,
)
from app.services.investor_presentation_pdf import render_investor_presentation_pdf
from app.tests.test_deal_budget_independent_math import budget, cost, terms
from app.tests.test_investor_report_layout import detailed_investor_financials


def example():
    saved = budget([cost(label="Private Provider software", recipient="Private Provider", source_ref="private-source",
                        notes="Private operator reason", category="software", value=100, timing="monthly")],
                   servicing_mode="company_managed", payoff_terms={"interest_policy": "accrued_only", "charge_amount": 0},
                   software_monitoring={"software_status": "configured", "monitoring_status": "planned",
                                        "provider": "Private Provider", "notes": "Private setup note"})
    saved["revision"] = 3
    return saved, terms()


def test_safe_projection_carries_same_net_scenarios_without_private_provider_notes_or_ids():
    saved, loan = example()
    calculated = calculate(saved, loan)
    safe = public_budget({"current": saved, "calculation": calculated})
    for key in ("six_month_payoff", "full_term"):
        projected = safe["calculation"]["investor_scenarios"][key]
        assert projected["net_income"] == calculated["investor_scenarios"][key]["net_income"]
        assert projected["principal_returned"] == 100000
        assert projected["rows"][0]["label"] == "Software fee"
    serialized = json.dumps(safe)
    for secret in ("Private Provider", "Private operator reason", "Private setup note", "private-source", saved["rows"][0]["id"]):
        assert secret not in serialized
    assert safe["servicing_mode"] == "company_managed"
    assert safe["software_monitoring"] == {"software_status": "configured", "monitoring_status": "planned"}
    assert safe["calculation"]["setup_readiness"]["verification"] == "staff_recorded_only"


def test_old_saved_terms_gain_additive_comparison_from_their_snapshot_without_current_draft_or_write():
    saved, loan = example()
    old_calculation = calculate(saved, loan)
    del old_calculation["investor_scenarios"]
    del old_calculation["setup_readiness"]
    current = deepcopy(saved)
    current["rows"][0]["value"] = 900
    current["revision"] = 4
    profile = SimpleNamespace(id=uuid4(), deal_budget=current, use_of_funds=None)
    recorded = {**loan, "source": "term_sheet", "recorded_terms_ref": {"id": "synthetic-terms", "version": 2},
                "deal_budget_snapshot": saved, "deal_budget_revision": 3, "deal_budget_calculation": old_calculation}
    before = deepcopy(recorded)
    view = reporting(profile, recorded)
    assert view["stale"] is True
    assert view["calculation"]["investor_scenarios"]["six_month_payoff"]["total_fees"] == 600
    assert view["calculation"]["investor_net_income"] == old_calculation["investor_net_income"] == 10800
    assert recorded == before and profile.deal_budget == current
    frozen = frozen_budget(saved, 3, recorded, old_calculation)
    assert frozen["calculation"]["investor_scenarios"]["six_month_payoff"]["net_income"] == 5400
    assert "investor_scenarios" not in old_calculation


def test_terms_html_discloses_net_comparison_principal_assets_and_unverified_setup():
    saved, loan = example()
    safe = public_budget({"current": saved, "calculation": calculate(saved, loan)})
    html = render_budget_html(safe)
    assert "Paid off at six months" in html and "Full note term" in html
    assert "5,400.00 USD" in html and "10,800.00 USD" in html
    assert "Principal returned" in html and "Net profit" in html
    assert "no external connection is verified" in html
    assert "Private Provider" not in html


def test_investor_pdf_uses_same_net_amounts_and_retains_bounded_five_page_layout():
    saved, loan = example()
    data = detailed_investor_financials()
    data["proposed_terms"] = loan
    data["investor_report"]["deal_budget"] = public_budget({"current": saved, "calculation": calculate(saved, loan)})
    output = render_investor_presentation_pdf(intake=SimpleNamespace(business_name="Synthetic Example LLC", full_name="Synthetic Owner"), financials=data)
    doc = fitz.open(stream=output, filetype="pdf")
    assert len(doc) == 5
    text = " ".join(doc[0].get_text().split())
    assert "net investor return - full term" in text.lower()
    assert "Six months (capped)" in text and "5,400" in text and "10,800" in text
    assert "unknown" not in text.lower().split("net investor return - full term", 1)[1].split("net proceeds", 1)[0]
