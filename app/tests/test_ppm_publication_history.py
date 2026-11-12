"""Independent ASGI publication/history tests with synthetic storage and facts."""

from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import Mock
from uuid import uuid4

import pytest

from app.capital_partners.services import storage
from app.enums import Role
from app.tests.test_capital_partners import Scenario
from app.tests.test_marketplace_ppm_lifecycle import (
    completed_draft,
    configure,
    legacy_ppm,
    ppm_harness,
    review_payload,
    state,
)


def base(listing):
    return f"/api/v1/capital/admin/listings/{listing.id}"


def restore_payload(current, context, **extra):
    return {"expected_version": current["latest_version"],
            "expected_source_fingerprint": context["fingerprint"],
            "idempotency_key": str(uuid4()), **extra}


def utc_instant(value):
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)


async def approve(h, listing, context, current):
    result = await h.client.post(base(listing) + "/ppm/review", json=review_payload(current, context))
    assert result.status_code == 200, result.text
    return result.json()


async def publish(h, listing):
    result = await h.client.post(base(listing) + "/publish", json={
        "confirmed": True, "reason": "Explicit synthetic publication after review"})
    assert result.status_code == 200, result.text
    return result.json()


async def member_url(h, listing, document_id):
    return await h.client.post(f"/api/v1/capital/listings/{listing.ref}/documents/{document_id}/url")


@pytest.mark.asyncio
async def test_open_offer_keeps_one_published_ppm_through_restore_and_review_until_publish_update(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False, offered="100000")
        context, _, _ = configure(monkeypatch, listing)
        draft = await completed_draft(h, listing, context, narrative={"overview": "Historical staff wording retained."})
        historical = deepcopy(draft["current_draft"])
        reviewed = await approve(h, listing, context, draft)
        first = reviewed["current_approved"]
        assert reviewed["active_version"] is None
        initial_publication = await publish(h, listing)
        assert (await state(h, listing))["active_version"]["document_id"] == first["document_id"]
        investor, _ = await scenario.member("publication-history@example.test")

        restored_response = await h.client.post(base(listing) + f"/ppm/versions/{historical['document_id']}/reinstate",
            json=restore_payload(await state(h, listing), context))
        assert restored_response.status_code == 200, restored_response.text
        restored = restored_response.json()
        assert restored["working_draft_document_id"] == restored["current_draft"]["document_id"]
        assert restored["current_draft"]["version"] > first["version"]
        assert restored["current_draft"]["narrative_sections"] == historical["narrative_sections"]
        assert restored["active_version"]["document_id"] == first["document_id"]
        h.current = investor
        assert (await member_url(h, listing, first["document_id"])).status_code == 200
        assert (await member_url(h, listing, restored["current_draft"]["document_id"])).status_code == 404
        h.current = admin

        candidate = await approve(h, listing, context, restored)
        second = candidate["current_approved"]
        assert second["document_id"] != first["document_id"]
        assert candidate["active_version"]["document_id"] == first["document_id"]
        assert candidate["working_draft_document_id"] is None
        h.current = investor
        assert (await member_url(h, listing, first["document_id"])).status_code == 200
        assert (await member_url(h, listing, second["document_id"])).status_code == 404
        visible = await h.client.get(f"/api/v1/capital/listings/{listing.ref}")
        assert [row["id"] for row in visible.json()["documents"] if row["kind"] == "ppm"] == [first["document_id"]]
        h.current = admin

        updated = await publish(h, listing)
        assert updated["status"] == "open"
        assert utc_instant(updated["published_at"]) == utc_instant(initial_publication["published_at"])
        current = await state(h, listing)
        assert current["active_version"]["document_id"] == second["document_id"]
        assert next(row for row in current["history"] if row["document_id"] == historical["document_id"])["document"] == historical["document"]
        h.current = investor
        assert (await member_url(h, listing, first["document_id"])).status_code == 404
        assert (await member_url(h, listing, second["document_id"])).status_code == 200


@pytest.mark.asyncio
async def test_reinstatement_uses_current_facts_and_never_bypasses_stale_active_subscription_guard(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False, offered="100000")
        context, _, _ = configure(monkeypatch, listing)
        draft = await completed_draft(h, listing, context, narrative={"overview": "Preserved historical explanation."})
        old_draft = deepcopy(draft["current_draft"])
        reviewed = await approve(h, listing, context, draft)
        old_active = reviewed["current_approved"]
        await publish(h, listing)
        context["fingerprint"] = "b" * 64
        context["listing"]["loan_amount"] = 225000
        context["financials"]["proposed_terms"]["amount"] = 225000
        investor, _ = await scenario.member("stale-history@example.test")
        h.current = investor
        assert (await member_url(h, listing, old_active["document_id"])).status_code == 404
        denied = await h.client.post(f"/api/v1/capital/listings/{listing.ref}/subscriptions",
            json={"amount": "25000", "ack_ppm": True, "ack_accredited": True})
        assert denied.status_code == 409 and denied.json()["detail"]["code"] == "ppm_review_required"
        h.current = admin

        restored = await h.client.post(base(listing) + f"/ppm/versions/{old_draft['document_id']}/reinstate",
            json=restore_payload(await state(h, listing), context))
        assert restored.status_code == 200, restored.text
        latest = restored.json()
        assert latest["current_draft"]["source_fingerprint"] == context["fingerprint"]
        assert latest["current_draft"]["narrative_sections"] == old_draft["narrative_sections"]
        facts = [fact["value"] for section in latest["current_draft"]["document"]["sections"] for fact in section["facts"]]
        assert any("225,000.00" in value for value in facts)
        assert latest["active_version"]["document_id"] == old_active["document_id"]
        h.current = investor
        assert (await member_url(h, listing, old_active["document_id"])).status_code == 404
        h.current = admin
        candidate = await approve(h, listing, context, latest)
        assert candidate["active_version"]["document_id"] == old_active["document_id"]
        await publish(h, listing)
        h.current = investor
        assert (await member_url(h, listing, candidate["current_approved"]["document_id"])).status_code == 200


@pytest.mark.asyncio
async def test_restore_replay_is_bound_to_actor_target_and_latest_revision(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, renderer, _ = configure(monkeypatch, listing)
        current = await completed_draft(h, listing, context)
        target = min(current["history"], key=lambda row: row["version"])
        url = base(listing) + f"/ppm/versions/{target['document_id']}/reinstate"
        payload = restore_payload(current, context)
        first = await h.client.post(url, json=payload)
        assert first.status_code == 200, first.text
        render_count = renderer.call_count
        replay = await h.client.post(url, json=payload)
        assert replay.status_code == 200 and replay.json()["latest_version"] == first.json()["latest_version"]
        assert renderer.call_count == render_count
        changed = await h.client.post(base(listing) + f"/ppm/versions/{current['current_draft']['document_id']}/reinstate", json=payload)
        assert changed.status_code == 409
        h.current = await h.user("another-history-staff@example.test", Role.SUPER_ADMIN)
        assert (await h.client.post(url, json=payload)).status_code == 409
        h.current = admin
        stale = await h.client.post(url, json={**payload, "idempotency_key": str(uuid4())})
        assert stale.status_code == 409 and stale.json()["detail"]["code"] == "ppm_version_changed"
        other = await scenario.listing(admin, publish=False)
        other_current = await completed_draft(h, other, context)
        cross_listing = await h.client.post(base(other) + f"/ppm/versions/{target['document_id']}/reinstate",
            json={**payload, "expected_version": other_current["latest_version"], "idempotency_key": str(uuid4())})
        assert cross_listing.status_code == 404


@pytest.mark.parametrize("role", [Role.LOAN_EXEC, Role.BROKER, Role.CAPITAL_PARTNER])
@pytest.mark.asyncio
async def test_restore_and_staff_history_url_do_not_expand_operator_access(monkeypatch, role):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        context, _, _ = configure(monkeypatch, listing)
        current = await completed_draft(h, listing, context)
        doc_id = current["current_draft"]["document_id"]
        h.current = await h.user(f"history-{role.value}@example.test", role)
        assert (await h.client.post(base(listing) + f"/ppm/versions/{doc_id}/reinstate", json=restore_payload(current, context))).status_code == 403
        assert (await h.client.post(base(listing) + f"/ppm/versions/{doc_id}/url?download=false")).status_code == 403


@pytest.mark.asyncio
async def test_legacy_uploaded_history_is_copied_to_a_new_pending_version_without_mutating_original(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        original = await legacy_ppm(h, listing, admin)
        identity = (str(original.id), original.s3_key, original.version, original.size_bytes)
        context, renderer, _ = configure(monkeypatch, listing)
        await publish(h, listing)
        copy = Mock(return_value=True)
        monkeypatch.setattr(storage, "copy_object", copy)
        current = await state(h, listing)
        payload = restore_payload(current, context)
        url = base(listing) + f"/ppm/versions/{original.id}/reinstate"
        restored = await h.client.post(url, json=payload)
        assert restored.status_code == 200, restored.text
        version = restored.json()["current_draft"]
        assert version["version"] == original.version + 1 and version["document_id"] != str(original.id)
        assert version["source"] == "uploaded" and version["status"] == "draft"
        assert any("copied unchanged" in warning for warning in version["document"]["warnings"])
        assert restored.json()["active_version"]["document_id"] == str(original.id)
        assert copy.call_count == 1 and renderer.call_count == 0
        await h.db.refresh(original)
        assert (str(original.id), original.s3_key, original.version, original.size_bytes) == identity
        replay = await h.client.post(url, json=payload)
        assert replay.status_code == 200 and replay.json()["current_draft"]["document_id"] == version["document_id"]
        assert copy.call_count == 1
        preview = await h.client.post(base(listing) + f"/ppm/versions/{version['document_id']}/url?download=false")
        assert preview.status_code == 200
