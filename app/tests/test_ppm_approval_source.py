"""Actual review derivatives against real linked financial source projections."""
from copy import deepcopy
from uuid import UUID, uuid4

import pytest

from app.capital_partners import router_admin
from app.capital_partners.schemas import PpmReviewIn
from app.capital_partners.services import ppm
from app.services import financial_cockpit
from app.tests.test_marketplace_ppm_lifecycle import ppm_harness
from app.tests.test_ppm_saved_source_stability import linked
from app.tests.test_ppm_saved_source_stability import real_sources as real_sources


def add_comparison_sources(evidence):
    older = deepcopy(evidence[0])
    older.update(file_id="10000000-0000-4000-8000-000000000002", file_name="Synthetic prior tax.pdf")
    older["key_facts"]["tax_year"] = 2024
    personal = deepcopy(evidence[0])
    personal.update(file_id="10000000-0000-4000-8000-000000000003", file_name="Synthetic personal tax.pdf",
                    classification="personal_tax_return", classifications=["personal_tax_return"])
    personal["key_facts"] = {"tax_year": 2025, "taxpayer": "Synthetic Person", "tax_form": "1040", "currency": "USD"}
    evidence.extend([older, personal])


async def linked_draft(h, monkeypatch):
    listing, profile, renderer, stored = await linked(h, monkeypatch)
    await router_admin._apply_file_financing(h.db, listing)
    context = await ppm.saved_context(h.db, listing)
    legal, selected = ppm.ppm_presets.defaults(context, ppm.LEGAL_FIELDS)
    legal["jurisdiction"], selected["jurisdiction"] = "Delaware; synthetic reviewed offering jurisdiction.", "custom"
    assert ppm._missing(context, legal, selected) == []
    await ppm._event(h.db, listing, "ppm.adopted", h.current.id, {"auto_refresh": True})
    doc = await ppm._version(h.db, listing, context, legal, {"overview": "Preserved staff description."}, h.current.id,
                             generation_status="source_based", legal_presets=selected)
    await h.db.commit()
    return listing, profile, renderer, stored, context, doc


@pytest.mark.asyncio
async def test_real_reviewed_derivative_ignores_retrieval_order_and_preserves_immutable_draft(real_sources, monkeypatch):
    add_comparison_sources(real_sources)
    async with ppm_harness() as h:
        listing, _profile, renderer, stored, context, draft = await linked_draft(h, monkeypatch)
        original_key = draft.s3_key
        render = renderer.return_value

        def reviewed_render(*_args, **kwargs):
            assert kwargs["status"] == "reviewed"
            real_sources.reverse()  # Same accepted facts, different database retrieval order.
            return render

        renderer.side_effect = reviewed_render
        result = await ppm.review(h.db, listing, h.current.id, PpmReviewIn(
            expected_version=draft.version, expected_source_fingerprint=context["fingerprint"],
            idempotency_key=str(uuid4()), confirmed=True))
        assert result["current_approved"]["version"] == draft.version + 1
        assert result["current_approved"]["document_id"] != str(draft.id)
        assert draft.s3_key == original_key
        assert stored.call_count == 2
        after = await ppm.saved_context(h.db, listing)
        assert after["fingerprint"] == context["fingerprint"]
        assert not any(row["key"] == "listing_snapshot" for row in after["missing"])
        sources = after["financials"]["operating_earnings"]["source_selection"]["sources"]
        assert len(sources) == 3


@pytest.mark.parametrize("change", ["value", "period", "acceptance"])
@pytest.mark.asyncio
async def test_real_review_rejects_changed_source_during_derivative(real_sources, monkeypatch, change):
    add_comparison_sources(real_sources)
    async with ppm_harness() as h:
        listing, _profile, renderer, _stored, context, draft = await linked_draft(h, monkeypatch)
        pdf = renderer.return_value

        def changed_source(*_args, **kwargs):
            assert kwargs["status"] == "reviewed"
            if change == "value":
                real_sources[0]["key_facts"]["ordinary_business_income"] += 1
            elif change == "period":
                real_sources[0]["key_facts"]["tax_year"] = 2023
            else:
                real_sources[0]["accepted"] = False
            return pdf

        renderer.side_effect = changed_source
        response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/ppm/review", json={
            "expected_version": draft.version, "expected_source_fingerprint": context["fingerprint"],
            "idempotency_key": str(uuid4()), "confirmed": True})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "ppm_source_changed"
        await h.db.refresh(listing, attribute_names=["documents"])
        assert len(listing.documents) == 1
        events = await ppm._events(h.db, listing.id)
        assert not any(row.kind == "ppm.reviewed" for row in events)


@pytest.mark.asyncio
async def test_actual_review_route_and_admin_readiness_stay_current_across_alternating_order(real_sources, monkeypatch):
    add_comparison_sources(real_sources)
    async with ppm_harness() as h:
        listing, _profile, renderer, _stored, context, draft = await linked_draft(h, monkeypatch)
        pdf = renderer.return_value
        renderer.side_effect = lambda *_args, **_kwargs: (real_sources.reverse(), pdf)[1]
        payload = {"expected_version": draft.version, "expected_source_fingerprint": context["fingerprint"],
                   "idempotency_key": str(uuid4()), "confirmed": True}
        path = f"/api/v1/capital/admin/listings/{listing.id}"
        reviewed = await h.client.post(path + "/ppm/review", json=payload)
        assert reviewed.status_code == 200, reviewed.text
        assert reviewed.json()["active_version"] is None
        assert reviewed.json()["current_approved"]["version"] == draft.version + 1
        replay = await h.client.post(path + "/ppm/review", json=payload)
        assert replay.status_code == 200
        assert replay.json()["latest_version"] == reviewed.json()["latest_version"]
        for _ in range(3):
            real_sources.reverse()
            current = await h.client.get(path + "/ppm")
            assert current.json()["source_fingerprint"] == context["fingerprint"]
            assert current.json()["missing"] == []
        ready = await h.client.get(path)
        assert ready.status_code == 200
        assert ready.json()["publish_issues"] == []
        published = await h.client.post(path + "/publish", json={"confirmed": True, "reason": "Publish reviewed synthetic offer"})
        assert published.status_code == 200, published.text
        state = (await h.client.get(path + "/ppm")).json()
        assert state["active_version"]["document_id"] == state["current_approved"]["document_id"]


def test_equal_period_source_selection_uses_persisted_provenance_not_retrieval_order():
    from app.tests.test_ppm_saved_source_stability import sources

    first = sources()[0]
    first.update(analysis_id="analysis-a", analyzed_at="2026-01-01T00:00:00+00:00")
    second = deepcopy(first)
    second.update(file_id="10000000-0000-4000-8000-000000000004", analysis_id="analysis-b", analyzed_at="2026-02-01T00:00:00+00:00")
    second["key_facts"]["ordinary_business_income"] = 30000
    results = [financial_cockpit.build_cockpit(order, [], business_entity="Synthetic Services LLC")
               for order in ([first, second], [second, first])]
    selections = [row["ebitda"]["source_selection"] for row in results]
    assert selections[0] == selections[1]
    assert selections[0]["selected_file_id"] == second["file_id"]
    assert results[0]["ebitda"]["headline"] == results[1]["ebitda"]["headline"]


def test_real_source_selection_flags_remain_bound_financial_facts():
    from app.capital_partners.services import sync
    from app.services.financial_earnings_sources import change
    from app.tests.test_financial_metric_review import ACTOR
    from app.tests.test_ppm_saved_source_stability import sources

    evidence = sources()
    current = financial_cockpit.build_cockpit(evidence, [], business_entity="Synthetic Services LLC")
    selection = current["ebitda"]["source_selection"]
    state, _ = change(None, selection, mode="manual", selected_file_id=None,
        excluded_file_ids=[selection["selected_file_id"]], reason="Explicitly exclude this source pending staff reconciliation",
        expected_revision=0, source_stamp=selection["source_stamp"], idempotency_key="synthetic-source-change", actor=ACTOR)
    changed = financial_cockpit.build_cockpit(evidence, [], business_entity="Synthetic Services LLC", metric_review_state=state)
    changed_selection = changed["ebitda"]["source_selection"]
    assert changed_selection["selected_file_id"] is None
    assert changed_selection["sources"][0]["included"] is False
    assert changed_selection["sources"][0]["selected"] is False
    def bound(cockpit):
        return sync.fingerprints(sync.file_sections(cockpit, program_keys=[], package=None))["figures"]

    assert bound(current) != bound(changed)


@pytest.mark.parametrize("listing_status", ["closing", "funded", "repaid", "withdrawn"])
@pytest.mark.asyncio
async def test_publish_update_does_not_reopen_other_lifecycle_states(listing_status):
    from app.tests.test_capital_partners import Scenario

    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        listing.status = listing_status
        response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/publish", json={
            "confirmed": True, "reason": "This must not change the listing lifecycle"})
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "not_draft"
        await h.db.refresh(listing)
        assert listing.status == listing_status
        assert listing.published_at is None
        assert not any(row.kind == "ppm.published" for row in await ppm._events(h.db, listing.id))


@pytest.mark.parametrize("changed", ["candidate", "source"])
@pytest.mark.asyncio
async def test_publish_confirmation_rejects_changed_candidate_or_source_without_switching_active(monkeypatch, changed):
    from app.tests.test_capital_partners import Scenario
    from app.tests.test_marketplace_ppm_lifecycle import (
        completed_draft,
        configure,
        review_payload,
        state,
    )

    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        path = f"/api/v1/capital/admin/listings/{listing.id}"
        first = await completed_draft(h, listing, context)
        approved = await h.client.post(path + "/ppm/review", json=review_payload(first, context))
        original = approved.json()["current_approved"]
        assert (await h.client.post(path + "/publish", json={"confirmed": True, "reason": "First explicit publication"})).status_code == 200
        current = await state(h, listing)
        restored = await h.client.post(path + f"/ppm/versions/{original['document_id']}/reinstate", json={
            "expected_version": current["latest_version"], "expected_source_fingerprint": context["fingerprint"], "idempotency_key": str(uuid4())})
        assert restored.status_code == 200, restored.text
        replacement = await h.client.post(path + "/ppm/review", json=review_payload(restored.json(), context))
        candidate = replacement.json()["current_approved"]
        payload = {"confirmed": True, "reason": "Publish only the version explicitly confirmed",
                   "expected_ppm_document_id": original["document_id"] if changed == "candidate" else candidate["document_id"],
                   "expected_ppm_source_fingerprint": context["fingerprint"]}
        if changed == "source":
            context["fingerprint"] = "b" * 64
        refused = await h.client.post(path + "/publish", json=payload)
        assert refused.status_code == 409
        assert refused.json()["detail"]["code"] == ("ppm_version_changed" if changed == "candidate" else "ppm_source_changed")
        unchanged = await state(h, listing)
        assert unchanged["active_version"]["document_id"] == original["document_id"]
        assert len([event for event in await ppm._events(h.db, listing.id) if event.kind == "ppm.published"]) == 1
        if changed == "candidate":
            fresh = await h.client.post(path + "/publish", json={**payload, "expected_ppm_document_id": candidate["document_id"]})
            assert fresh.status_code == 200, fresh.text
            assert (await state(h, listing))["active_version"]["document_id"] == candidate["document_id"]


@pytest.mark.asyncio
async def test_new_saved_version_archives_saved_drafts_but_preserves_inflight_upload(monkeypatch):
    from app.capital_partners import enums as E
    from app.capital_partners.models import CpListingDocument
    from app.tests.test_capital_partners import Scenario
    from app.tests.test_marketplace_ppm_lifecycle import completed_draft, configure, generate

    async with ppm_harness() as h:
        scenario = Scenario(h)
        listing = await scenario.listing(await scenario.admin(), publish=False)
        context, _renderer, _stored = configure(monkeypatch, listing)
        current = await completed_draft(h, listing, context)
        old_id = current["current_draft"]["document_id"]
        path = f"/api/v1/capital/admin/listings/{listing.id}"
        initialized = await h.client.post(path + "/documents/upload-init", json={
            "kind": "ppm", "name": "Synthetic in-flight upload.pdf", "content_type": "application/pdf", "size_bytes": 1234})
        assert initialized.status_code == 200, initialized.text
        inflight_id = initialized.json()["document_id"]
        fresh = await generate(h, listing, context, force=True)
        assert fresh["current_draft"]["document_id"] not in {old_id, inflight_id}
        inflight = await h.db.get(CpListingDocument, UUID(inflight_id), populate_existing=True)
        previous = await h.db.get(CpListingDocument, UUID(old_id), populate_existing=True)
        assert inflight.status == E.DocumentStatus.PENDING
        assert inflight.superseded_at is None and inflight.uploaded_at is None
        assert previous.status == E.DocumentStatus.SUPERSEDED
