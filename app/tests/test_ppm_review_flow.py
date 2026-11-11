"""Action-based approval audits and immutable legacy disclosure upgrades."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.capital_partners import enums as E
from app.capital_partners import models as cp
from app.capital_partners.services import ppm, ppm_presets, sync
from app.enums import Role
from app.tests.test_capital_partners import Scenario
from app.tests.test_marketplace_ppm_lifecycle import (
    completed_draft,
    configure,
    generate,
    ppm_harness,
    review_payload,
    state,
)


async def events(h, listing, kind):
    return list((await h.db.execute(select(cp.CpListingEvent).where(
        cp.CpListingEvent.listing_id == listing.id, cp.CpListingEvent.kind == kind,
    ).order_by(cp.CpListingEvent.occurred_at, cp.CpListingEvent.id))).scalars().all())


async def seed_same_source_legacy_blanks(h, listing, context):
    """Model an earlier saved receipt, with the current financial fingerprint."""
    saved = await generate(h, listing, context)
    event = (await events(h, listing, "ppm.version"))[-1]
    detail = deepcopy(event.detail)
    detail.pop("preset_schema")
    detail["legal_inputs"] = {"issuer_legal_name": "Pinned synthetic issuer LLC", "jurisdiction": ""}
    detail["legal_presets"] = {}
    event.detail = detail
    await h.db.commit()
    return saved, deepcopy(detail)


@pytest.mark.asyncio
@pytest.mark.parametrize("optional_note", [{}, {"reason": None}, {"reason": ""}, {"reason": "   "}])
async def test_action_approval_needs_no_typed_note_and_records_a_bound_audit(monkeypatch, optional_note):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, renderer, _ = configure(monkeypatch, listing)
        current = await completed_draft(h, listing, context)
        payload = review_payload(current, context, decision="approve")
        payload.pop("reason")
        payload.update(optional_note)
        response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=payload)
        assert response.status_code == 200, response.text
        approved = response.json()["current_approved"]
        assert approved["version"] == current["current_draft"]["version"] + 1
        assert approved["document_id"] != current["current_draft"]["document_id"]
        audit = (await events(h, listing, "ppm.reviewed"))[-1]
        assert audit.actor_user_id == admin.id
        assert audit.detail["decision"] == "approve"
        assert audit.detail["reason"] == ppm.APPROVAL_AUDIT_STATEMENT
        assert audit.detail["reason_source"] == "approval_action"
        assert audit.detail["source_fingerprint"] == context["fingerprint"]
        assert audit.detail["reviewed_from_document_id"] == current["current_draft"]["document_id"]
        assert audit.detail["content_fingerprint"]
        assert renderer.call_args.kwargs["status"] == "reviewed"
        await h.db.refresh(listing)
        assert listing.status == E.ListingStatus.DRAFT and listing.published_at is None


@pytest.mark.asyncio
async def test_legacy_reason_payload_and_its_idempotent_replay_remain_compatible(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, renderer, _ = configure(monkeypatch, listing)
        current = await completed_draft(h, listing, context)
        payload = review_payload(current, context, reason="Reviewed the saved synthetic disclosures.")
        url = f"/api/v1/capital/admin/listings/{listing.id}/ppm/review"
        first = await h.client.post(url, json=payload)
        assert first.status_code == 200, first.text
        count = renderer.call_count
        replay = await h.client.post(url, json=payload)
        assert replay.status_code == 200 and replay.json()["current_approved"] == first.json()["current_approved"]
        assert renderer.call_count == count
        audits = await events(h, listing, "ppm.reviewed")
        assert len(audits) == 1
        assert audits[0].detail["intent"] == sync._fingerprint(payload)
        assert audits[0].detail["reason"] == payload["reason"]
        assert audits[0].detail["reason_source"] == "staff_note"


@pytest.mark.asyncio
@pytest.mark.parametrize(("changes", "status", "code"), [
    ({"confirmed": False}, 422, "ppm_review_confirmation"),
    ({"expected_source_fingerprint": "b" * 64}, 409, "ppm_source_changed"),
    ({"expected_version": 999}, 409, "ppm_version_changed"),
    ({"decision": "publish"}, 422, None),
    ({"reason": "x" * 2001}, 422, None),
])
async def test_action_approval_preserves_confirmation_and_snapshot_guards(monkeypatch, changes, status, code):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, renderer, _ = configure(monkeypatch, listing)
        current = await completed_draft(h, listing, context)
        payload = review_payload(current, context, **changes)
        if "reason" not in changes:
            payload.pop("reason", None)
        before = renderer.call_count
        response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=payload)
        assert response.status_code == status, response.text
        if code:
            assert response.json()["detail"]["code"] == code
        assert renderer.call_count == before and not await events(h, listing, "ppm.reviewed")


@pytest.mark.asyncio
async def test_optional_note_does_not_bypass_missing_disclosures_or_staff_role(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, _, _ = configure(monkeypatch, listing)
        current = await generate(h, listing, context)
        payload = review_payload(current, context)
        payload.pop("reason")
        url = f"/api/v1/capital/admin/listings/{listing.id}/ppm/review"
        incomplete = await h.client.post(url, json=payload)
        assert incomplete.status_code == 409 and incomplete.json()["detail"]["code"] == "ppm_incomplete"
        h.current = await h.user("synthetic-review-exec@example.test", Role.LOAN_EXEC)
        forbidden = await h.client.post(url, json=payload)
        assert forbidden.status_code == 403
        assert not await events(h, listing, "ppm.reviewed")


@pytest.mark.asyncio
async def test_same_fingerprint_legacy_blanks_materialize_once_without_rewriting_history(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, renderer, _ = configure(monkeypatch, listing)
        saved, legacy = await seed_same_source_legacy_blanks(h, listing, context)
        before = await state(h, listing)
        assert before["status"] == "draft" and before["draft_upgrade_available"] is True
        assert before["current_draft"]["legal_inputs"]["jurisdiction"] == ""
        response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json=review_payload(before, context))
        assert response.status_code == 409 and response.json()["detail"]["code"] == "ppm_draft_upgrade_required"
        key = str(uuid4())
        upgraded = await generate(h, listing, context, key=key)
        latest = upgraded["current_draft"]
        assert latest["version"] == saved["current_draft"]["version"] + 1
        assert latest["legal_inputs"]["issuer_legal_name"] == "Pinned synthetic issuer LLC"
        assert latest["legal_presets"]["issuer_legal_name"] == "custom"
        assert latest["legal_inputs"]["subscription_process"]
        assert latest["legal_presets"]["jurisdiction"] == "to_be_confirmed"
        assert upgraded["draft_upgrade_available"] is False
        assert "jurisdiction" in {row["key"] for row in upgraded["missing"]}
        history = next(row for row in upgraded["history"] if row["document_id"] == legacy["document_id"])
        assert history["legal_inputs"] == legacy["legal_inputs"]
        original = (await events(h, listing, "ppm.version"))[0]
        assert original.detail == legacy
        count = renderer.call_count
        assert (await generate(h, listing, context, key=key))["current_draft"]["document_id"] == latest["document_id"]
        assert (await generate(h, listing, context))["current_draft"]["document_id"] == latest["document_id"]
        assert renderer.call_count == count
        assert len(await events(h, listing, "ppm.version")) == 2


@pytest.mark.asyncio
async def test_current_schema_custom_blank_is_not_an_automatic_upgrade(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, renderer, _ = configure(monkeypatch, listing)
        current = await completed_draft(h, listing, context)
        current_draft = current["current_draft"]
        legal = {**current_draft["legal_inputs"], "jurisdiction": ""}
        response = await h.client.patch(f"/api/v1/capital/admin/listings/{listing.id}/ppm/draft", json={
            "expected_version": current_draft["version"], "expected_source_fingerprint": context["fingerprint"],
            "idempotency_key": str(uuid4()), "legal_inputs": legal, "legal_presets": {key: "custom" for key in legal}, "narrative_sections": {},
        })
        assert response.status_code == 200, response.text
        blank = response.json()
        assert blank["draft_upgrade_available"] is False
        count = renderer.call_count
        ensured = await generate(h, listing, context)
        assert ensured["current_draft"]["document_id"] == blank["current_draft"]["document_id"]
        assert ensured["current_draft"]["legal_inputs"]["jurisdiction"] == ""
        assert renderer.call_count == count


@pytest.mark.asyncio
async def test_same_source_managed_upload_is_not_replaced_by_default_upgrade(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, renderer, _ = configure(monkeypatch, listing)
        saved, _ = await seed_same_source_legacy_blanks(h, listing, context)
        event = (await events(h, listing, "ppm.version"))[-1]
        event.detail = {**event.detail, "source": "uploaded"}
        await h.db.commit()
        before = renderer.call_count
        unchanged = await generate(h, listing, context)
        assert unchanged["draft_upgrade_available"] is False
        assert unchanged["current_draft"]["source"] == "uploaded"
        assert unchanged["current_draft"]["document_id"] == saved["current_draft"]["document_id"]
        assert renderer.call_count == before
        assert len(await events(h, listing, "ppm.version")) == 1


@pytest.mark.asyncio
async def test_managed_sweep_upgrades_same_source_legacy_generated_defaults_once(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, _, _ = configure(monkeypatch, listing)
        saved, _ = await seed_same_source_legacy_blanks(h, listing, context)
        async with AsyncSession(h.db.bind, expire_on_commit=False) as session:
            result = await ppm.refresh_managed_listing(session, listing.id)
            await session.commit()
        assert result == {"status": "refreshed", "version": saved["current_draft"]["version"] + 1}
        async with AsyncSession(h.db.bind, expire_on_commit=False) as session:
            assert await ppm.refresh_managed_listing(session, listing.id) == {"status": "unchanged"}
        assert len(await events(h, listing, "ppm.version")) == 2


def test_legacy_without_missing_default_content_does_not_claim_an_upgrade():
    context = {"budget": {"currency": "USD"}}
    complete = {"source": "generated_ppm", "legal_inputs": {row["key"]: "Existing custom wording" for row in ppm.LEGAL_FIELDS}}
    assert ppm_presets.draft_upgrade_available(context, ppm.LEGAL_FIELDS, complete) is False
    explicit_blank = {"source": "generated_ppm", "legal_inputs": {}, "legal_presets": {row["key"]: "custom" for row in ppm.LEGAL_FIELDS}}
    assert ppm_presets.draft_upgrade_available(context, ppm.LEGAL_FIELDS, explicit_blank) is False


def test_legacy_uploaded_bytes_are_not_replaced_by_a_default_upgrade():
    context = {"budget": {"currency": "USD"}}
    previous = {"source": "uploaded", "legal_inputs": {}, "legal_presets": {}}
    before = deepcopy(previous)
    assert ppm_presets.draft_upgrade_available(context, ppm.LEGAL_FIELDS, previous) is False
    assert previous == before
    assert ppm_presets.draft_upgrade_available(context, ppm.LEGAL_FIELDS, {**previous, "source": "generated_ppm"}) is True
