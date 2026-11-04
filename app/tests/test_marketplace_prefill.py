"""Listing a file from the file page: the draft carries program readiness and
the cockpit's figures, an AI lead resolves its profile first, and a file that
is already listed is pointed at its listing instead of a duplicate.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest

from app.capital_partners.services import prefill
from app.services import application_programs as programs
from app.services import financial_cockpit


def test_the_summary_sentence_states_months_and_average_deposits_or_nothing() -> None:
    assert prefill.file_summary_sentence({"bank_months": 6, "avg_monthly_deposits": 57536.4}) == "6 months of bank statements reviewed; average monthly deposits $57,536."
    assert prefill.file_summary_sentence({"bank_months": 0, "avg_monthly_deposits": None}) is None
    assert prefill.file_summary_sentence({}) is None


@pytest.mark.asyncio
async def test_the_readiness_snapshot_carries_programs_coverage_and_the_cockpit_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    profile = SimpleNamespace(id=uuid.uuid4())
    selection = SimpleNamespace(program_key="mca_refinance", program_name="MCA Refinance", source="ai_auto")
    candidate = SimpleNamespace(
        program_key="mca_refinance",
        eligible=True,
        recommendation_status=SimpleNamespace(value="recommended"),
        rule_state="pass",
        requires_review=True,
        reasons=["Revenue estimated from deposits", "Years in business recorded by the desk", "r3", "r4", "r5"],
    )

    async def selections(_db, _profile_id):
        return [selection]

    async def candidates(_db, _profile):
        return [candidate]

    async def cockpit(_db, _profile):
        return {
            "generated_at": "2026-11-03T20:00:00+00:00",
            "coverage": {"bank_months": 6, "bank_months_accepted": 6, "bank_months_required": 6, "tax_years": [], "debt_rows": 0, "has_debt_schedule_document": True, "files_analyzed": 8, "files_accepted": 7},
            "bank": {"summary": {"avg_deposits": 50000.0, "annualized_deposits": 600000.0, "avg_net": 5000.0, "average_balance": 15500.0, "nsf_total": 0, "deposit_trend_pct": 4.2}},
            "dscr": {"headline": {"key": "bank_cash_flow", "value": 1.47, "status": "healthy"}, "pro_forma_headline": None, "annual_debt_service": 127600.0},
            "net_profit": {"conclusion": {"key": "bank_cash_flow", "value": 60000.0, "period": "Apr 2026–Sep 2026"}},
            "tax": {"years": []},
        }

    class _Result:
        def scalar_one_or_none(self):
            return SimpleNamespace(stage=1, status="draft", version=2)

    class _Db:
        async def execute(self, _stmt):
            return _Result()

    monkeypatch.setattr(programs, "active_selections", selections)
    monkeypatch.setattr(programs, "published_candidates", candidates)
    monkeypatch.setattr(financial_cockpit, "load_cockpit", cockpit)

    snapshot = await prefill.readiness_snapshot(_Db(), profile)
    readiness = snapshot["readiness"]
    assert readiness["programs"] == [
        {
            "key": "mca_refinance",
            "name": "MCA Refinance",
            "source": "ai_auto",
            "eligible": True,
            "recommendation_status": "recommended",
            "rule_state": "pass",
            "requires_review": True,
            "reasons": ["Revenue estimated from deposits", "Years in business recorded by the desk", "r3", "r4"],
        }
    ]
    assert readiness["coverage_complete"] is True and readiness["has_debt_schedule"] is True
    assert (readiness["files_analyzed"], readiness["files_accepted"]) == (8, 7)
    financials = snapshot["financials"]
    assert (financials["bank_months"], financials["avg_monthly_deposits"], financials["dscr"], financials["dscr_basis"]) == (6, 50000.0, 1.47, "bank_cash_flow")
    assert (financials["net_profit"], financials["net_profit_basis"]) == (60000.0, "bank_cash_flow")
    assert snapshot["production_package"] == {"stage": 1, "status": "draft", "version": 2}


@pytest.mark.asyncio
async def test_an_unknown_intake_reads_as_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import application_profiles as profiles

    async def failing(_db, _kind, _id, _user):
        raise RuntimeError("no such intake")

    monkeypatch.setattr(profiles, "resolve_profile", failing)
    with pytest.raises(LookupError):
        await prefill.resolve_intake_profile(SimpleNamespace(), SimpleNamespace(), uuid.uuid4())


def test_products_apply_as_plain_json_with_one_primary() -> None:
    from decimal import Decimal

    from app.capital_partners import schemas as S
    from app.capital_partners.models import CpListing
    from app.capital_partners.router_admin import _apply

    listing = CpListing()
    _apply(
        listing,
        S.ListingIn(
            products=[
                S.ListingProductIn(program_key="mca_refinance", label="MCA Refinance", amount=Decimal("70000"), rate_pct=Decimal("16"), term_months=120, monthly_payment=Decimal("1172.26"), dscr=Decimal("1.31")),
                S.ListingProductIn(program_key="rbf", label="Revenue-based", amount=Decimal("50000"), factor_rate=Decimal("1.3"), term_months=12),
            ]
        ),
    )
    assert listing.products[0]["primary"] is True and listing.products[1]["primary"] is False
    assert listing.products[0]["amount"] == 70000.0 and listing.products[0]["rate_pct"] == 16.0
    assert listing.products[1]["factor_rate"] == 1.3 and listing.products[1]["payment_frequency"] == "monthly"
    assert all(not isinstance(v, Decimal) for p in listing.products for v in p.values())


@pytest.mark.asyncio
async def test_suggested_products_mark_the_first_as_primary(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import offer_suggestions

    async def fake(_db, _profile, *, program_keys=None):
        return {"products": [{"program_key": "a", "label": "A", "amount": 70000.0, "rate_pct": 16.0, "term_months": 120, "monthly_payment": 1172.26, "dscr": 1.31, "reading": "A: fine."}, {"program_key": "b", "label": "B", "amount": None, "reason": "no room"}]}

    monkeypatch.setattr(offer_suggestions, "suggest_offers", fake)
    products = await prefill.suggested_products(SimpleNamespace(), SimpleNamespace(id=uuid.uuid4()))
    assert [p["primary"] for p in products] == [True, False]
    assert products[0]["notes"] == "A: fine." and products[1]["amount"] is None


def test_members_see_the_file_figures_and_the_plan_but_not_the_raw_snapshot() -> None:
    from types import SimpleNamespace

    from app.capital_partners.services.listings import partner_financials

    snapshot = {
        "readiness": {"programs": []},
        "financials": {
            "bank_months": 6, "avg_monthly_deposits": 57533.0, "dscr": 0.8, "dscr_basis": "bank_cash_flow", "dscr_status": "concerning",
            "dscr_after_plan": 1.62, "net_profit": -29362.0, "secret_internal": "never",
            "plan": {"active": True, "modifications": [{"kind": "retire", "label": "Pay off OnDeck with the new loan", "detail": "Its 5,850 a month stops.", "monthly_effect": -5850}], "impact": {"dscr": {"before": 0.8, "after": 1.62}}},
        },
        "refreshed_at": "2026-11-04T01:00:00+00:00",
    }
    out = partner_financials(SimpleNamespace(source_snapshot=snapshot))
    assert out["dscr"] == 0.8 and out["dscr_after_plan"] == 1.62 and "secret_internal" not in out
    assert out["plan"]["modifications"] == [{"kind": "retire", "label": "Pay off OnDeck with the new loan", "detail": "Its 5,850 a month stops."}]
    assert out["plan"]["impact"]["dscr"]["after"] == 1.62 and out["refreshed_at"] == "2026-11-04T01:00:00+00:00"
    assert partner_financials(SimpleNamespace(source_snapshot={})) is None
    inactive = partner_financials(SimpleNamespace(source_snapshot={"financials": {"dscr": 1.1, "plan": {"active": False, "modifications": []}}}))
    assert "plan" not in inactive and inactive["dscr"] == 1.1


def test_the_listing_knows_what_changed_on_the_file_since_the_last_push() -> None:
    from types import SimpleNamespace

    from app.capital_partners.services import sync

    cockpit = {
        "requested_amount": 70000.0,
        "coverage": {"bank_months": 6, "bank_months_accepted": 6},
        "bank": {"summary": {"avg_deposits": 57533.0, "annualized_deposits": 690000.0, "avg_net": -2447.0, "average_balance": 6477.0, "nsf_total": 12, "deposit_trend_pct": -12.6}},
        "dscr": {"headline": {"key": "bank_cash_flow", "value": 0.8, "status": "concerning"}, "pro_forma_headline": None, "annual_debt_service": 143990.0, "proposed": None},
        "net_profit": {"conclusion": {"key": "bank_cash_flow", "value": -29362.0, "period": "May–Oct 2026"}},
        "tax": {"years": []},
        "plan": {"record": {"note": None, "updated_at": "2026-11-04T01:00:00+00:00"}, "evaluation": {"active": False, "headline_after": 0.8, "debt_service_after_monthly": 11999.0}, "modifications": [], "impact": {"active": False, "loan": None}},
    }
    sections = sync.file_sections(cockpit, program_keys=["mca_refinance"], package=None)
    assert sections["terms"]["proposed"] is None and sections["programs"] == ["mca_refinance"]
    assert "generated_at" not in sections["figures"] and "plan" not in sections["figures"]
    current = sync.fingerprints(sections)

    listing = SimpleNamespace(source_snapshot={}, status="draft")
    state = sync.sync_state(listing, current)
    assert state["published"] is False and state["up_to_date"] is False
    assert {c["key"] for c in state["changed"]} == {"figures", "plan", "terms", "programs"}  # no package yet, so it is not "changed"

    sync.mark_published(listing, current, by=None)
    assert sync.sync_state(listing, current)["up_to_date"] is True

    # The desk saves a plan with a loan: the plan and the terms move, the figures do not.
    moved = dict(cockpit)
    moved["plan"] = {**cockpit["plan"], "evaluation": {"active": True, "headline_after": 1.6, "debt_service_after_monthly": 6000.0}, "modifications": [{"kind": "loan", "label": "New loan", "detail": None}], "impact": {"active": True, "loan": {"amount": 150000.0, "rate_pct": 12.0, "term_months": None, "interest_only": True, "payment_monthly": 1500.0}}}
    after = sync.fingerprints(sync.file_sections(moved, program_keys=["mca_refinance"], package=None))
    changed = {c["key"]: c["label"] for c in sync.sync_state(listing, after)["changed"]}
    assert set(changed) == {"plan", "terms", "figures"} and changed["terms"] == "Proposed terms"  # figures carry dscr_after_plan
    assert sync.proposed_terms(moved) == {"source": "plan", "label": "The desk's plan", "amount": 150000.0, "rate_pct": 12.0, "term_months": None, "monthly_payment": 1500.0, "interest_only": True}

    # A refresh stamps the snapshot sections but not the package; a package push stamps everything.
    sync.mark_published(listing, after, by=None, keys=["figures", "plan", "terms", "programs"])
    with_package = sync.fingerprints(sync.file_sections(moved, program_keys=["mca_refinance"], package={"generation_id": "g1"}))
    assert [c["key"] for c in sync.sync_state(listing, with_package)["changed"]] == ["package"]
    sync.mark_published(listing, with_package, by=None, package={"generation_id": "g1", "documents": []})
    state = sync.sync_state(listing, with_package)
    assert state["up_to_date"] and state["package"]["generation_id"] == "g1"


def test_the_package_prints_proposed_terms_and_the_plan_for_every_client() -> None:
    from app.services.public_underwriting_packet_pdf import _plan_section

    assert _plan_section({}) == ""
    terms_only = _plan_section({"proposed_terms": {"source": "term_sheet", "amount": 100000, "rate_pct": 10, "term_months": 60, "monthly_payment": 2124.7, "interest_only": False}})
    assert "Proposed terms (recorded term sheet)" in terms_only and "$100,000" in terms_only and "over 60 months" in terms_only
    html = _plan_section({
        "proposed_terms": {"source": "plan", "amount": 150000, "rate_pct": 12, "term_months": None, "monthly_payment": 1500, "interest_only": True},
        "plan": {"active": True, "modifications": [{"label": "Pay off OnDeck with the new loan", "detail": "Its 5,850 a month stops."}], "impact": {"dscr": {"before": 0.8, "after": 1.62, "change": 0.82, "basis": "bank_cash_flow"}, "debt_service_monthly": {"before": 11897, "after": 6647, "change": -5250}}, "note": "Refinance at closing"},
    })
    assert "the desk's plan" in html and "interest-only" in html and "principal at maturity" in html
    assert "0.80x" in html and "1.62x" in html and "+0.82x" in html and "−$5,250" in html
    assert "Pay off OnDeck with the new loan" in html and "Refinance at closing" in html
    compact = _plan_section({"plan": {"active": True, "modifications": [{"label": "x"}], "impact": {"dscr": {"before": 0.8, "after": 1.1, "change": 0.3}}}}, compact=True)
    assert "<ul>" not in compact and "0.80x" in compact
