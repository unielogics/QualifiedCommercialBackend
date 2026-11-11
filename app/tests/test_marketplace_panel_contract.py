"""Same-panel draft creation and saved-file refresh keep publication explicit."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.capital_partners import router_admin as routes
from app.capital_partners.models import CpListing
from app.capital_partners.schemas import ListingIn
from app.capital_partners.services import listing_draft
from app.tests.test_shared_financing_marketplace import financing


def saved_file():
    financials = financing()
    financials["deal_budget"] = {"current": {"revision": 5}, "calculation": {
        "status": "ready", "investor_funding_amount": 150000, "minimum_investor_contribution": 10000}}
    return {"financials": financials, "file_fingerprints": {"terms": "saved-terms-v3", "deal_budget": "saved-costs-v5"},
        "readiness": {"programs": []}, "production_package": None}


@pytest.fixture
def panel(monkeypatch):
    profile = SimpleNamespace(id=uuid4(), financial_plan={"retain": "saved plan"}, deal_budget={"revision": 5})
    actor = SimpleNamespace(id=uuid4())
    stored = []
    db = SimpleNamespace(execute=AsyncMock(), get=AsyncMock(return_value=profile), add=stored.append, commit=AsyncMock())

    async def flush():
        for row in stored:
            if row.id is None:
                row.id = uuid4()

    db.flush = AsyncMock(side_effect=flush)

    async def existing(**_values):
        return {"id": str(stored[0].id)} if stored else None

    async def existing_for(_db, **values):
        return await existing(**values)

    async def load(_db, identifier):
        return next(row for row in stored if row.id == identifier)

    async def serialize(_db, listing):
        return listing

    fresh = saved_file()
    monkeypatch.setattr(routes.P, "existing_listing", AsyncMock(side_effect=existing_for))
    monkeypatch.setattr(routes.P, "readiness_snapshot", AsyncMock(return_value=fresh))
    monkeypatch.setattr(routes.R, "next_listing_ref", AsyncMock(return_value="SYN-0001"))
    monkeypatch.setattr(routes, "_load_admin_listing", load)
    monkeypatch.setattr(routes, "_listing_admin_out", serialize)
    monkeypatch.setattr(routes.L, "committed_amounts", AsyncMock(return_value={}))
    monkeypatch.setattr(routes.L, "log_event", AsyncMock())
    publish = AsyncMock(side_effect=AssertionError("Draft work must not publish an offering"))
    monkeypatch.setattr(routes.L, "publish", publish)
    return SimpleNamespace(db=db, profile=profile, actor=actor, stored=stored, fresh=fresh, publish=publish)


def create_payload(env, **changes):
    return ListingIn(profile_id=env.profile.id, source_kind="profile", name="Staff title", summary="Staff-written summary",
        vertical="business", servicer_label="Example servicer", ai_draft_id="reviewed-source-draft",
        expected_deal_budget_revision=5, expected_source_fingerprint=listing_draft.source_fingerprint(env.fresh),
        **changes)


@pytest.mark.asyncio
async def test_create_draft_without_package_uses_saved_financing_and_replay_has_one_listing(panel):
    payload = create_payload(panel)
    profile_before = deepcopy(vars(panel.profile))
    listing = await routes.create_listing(payload, panel.actor, panel.db)
    assert listing.status == "draft" and listing.published_at is None
    assert panel.fresh["production_package"] is None
    assert listing.loan_amount == 200000 and listing.offered_amount == 150000 and listing.minimum_investment == 10000
    assert listing.products[0]["financing_source"] == "canonical_financing"
    assert listing.source_snapshot["financials"]["deal_budget"]["current"]["revision"] == 5
    assert listing.name == "Staff title" and listing.summary == "Staff-written summary"
    repeated = await routes.create_listing(payload, panel.actor, panel.db)
    assert repeated is listing and len(panel.stored) == 1
    assert vars(panel.profile) == profile_before
    panel.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_another_create_intent_reports_existing_listing_instead_of_cutting_a_duplicate(panel):
    listing = await routes.create_listing(create_payload(panel), panel.actor, panel.db)
    changed = create_payload(panel).model_copy(update={"ai_draft_id": "another-reviewed-draft"})
    with pytest.raises(HTTPException) as error:
        await routes.create_listing(changed, panel.actor, panel.db)
    assert error.value.status_code == 409
    assert error.value.detail["code"] == "listing_exists"
    assert error.value.detail["listing_id"] == str(listing.id)
    assert len(panel.stored) == 1 and listing.status == "draft"


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", ["budget", "source"])
async def test_create_keeps_source_revision_guards_even_without_a_current_package(panel, stale):
    payload = create_payload(panel).model_copy(update={
        "expected_deal_budget_revision": 4 if stale == "budget" else 5,
        "expected_source_fingerprint": "0" * 64 if stale == "source" else listing_draft.source_fingerprint(panel.fresh)})
    with pytest.raises(HTTPException) as error:
        await routes.create_listing(payload, panel.actor, panel.db)
    assert error.value.status_code == 409
    assert "changed" in error.value.detail
    assert panel.stored == []
    panel.db.flush.assert_not_awaited()
    panel.publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_updates_saved_finances_and_snapshot_while_preserving_copy_alternatives_and_draft_status(panel, monkeypatch):
    listing = CpListing(id=uuid4(), ref="SYN-0001", profile_id=panel.profile.id, vertical="business", status="draft",
        name="Keep this custom headline", summary="Keep this manually reviewed summary", loan_amount=70000, offered_amount=25000,
        minimum_investment=5000, note_rate_pct=12, term_months=120,
        products=[{"program_key": "sba", "label": "Explicit alternative", "primary": True, "amount": 125000}],
        source_snapshot={"keep": "prior audit metadata"})
    panel.stored.append(listing)
    profile_before = deepcopy(vars(panel.profile))
    monkeypatch.setattr(routes.SYNC, "current_fingerprints", AsyncMock(return_value=(panel.fresh["file_fingerprints"], {}, {})))
    result = await routes.refresh_listing_snapshot(listing.id, panel.actor, panel.db)
    assert result is listing
    assert listing.name == "Keep this custom headline" and listing.summary == "Keep this manually reviewed summary"
    assert listing.status == "draft" and listing.published_at is None
    assert listing.loan_amount == 200000 and listing.offered_amount == 150000 and listing.minimum_investment == 10000
    assert listing.note_rate_pct == 0 and listing.term_months == 24
    assert len(listing.products) == 2 and listing.products[0]["financing_source"] == "canonical_financing"
    assert listing.products[1] == {"program_key": "sba", "label": "Explicit alternative", "primary": False, "amount": 125000}
    assert listing.source_snapshot["financials"] == panel.fresh["financials"]
    assert listing.source_snapshot["keep"] == "prior audit metadata"
    assert listing.source_snapshot["published"]["fingerprints"]["deal_budget"] == "saved-costs-v5"
    assert vars(panel.profile) == profile_before
    panel.publish.assert_not_awaited()
    panel.db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_committed_capital_blocks_financing_refresh_before_replacing_manual_copy_or_snapshot(panel, monkeypatch):
    listing = CpListing(id=uuid4(), profile_id=panel.profile.id, status="open", name="Original title", summary="Original summary",
        loan_amount=70000, offered_amount=70000, note_rate_pct=12, term_months=120, products=[], source_snapshot={"keep": True})
    panel.stored.append(listing)
    before = deepcopy({key: getattr(listing, key) for key in ("status", "name", "summary", "loan_amount", "source_snapshot")})
    monkeypatch.setattr(routes.L, "committed_amounts", AsyncMock(return_value={listing.id: 10000}))
    with pytest.raises(HTTPException) as error:
        await routes.refresh_listing_snapshot(listing.id, panel.actor, panel.db)
    assert error.value.status_code == 409
    assert {key: getattr(listing, key) for key in before} == before
    panel.db.commit.assert_not_awaited()
    panel.publish.assert_not_awaited()
