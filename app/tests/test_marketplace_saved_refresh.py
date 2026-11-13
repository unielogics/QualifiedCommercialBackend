"""Real ORM leases/snapshots/PPM authority, with only PDF/storage boundaries fake."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.capital_partners import enums as E
from app.capital_partners import router_admin
from app.capital_partners.models import CpSubscription
from app.capital_partners.services import listings, package_documents, ppm, refresh, sync
from app.models.application_profile import ApplicationProfile
from app.services import marketplace_package_refresh as packages
from app.services.application_profiles import log_profile_action
from app.tests.test_capital_partners import Scenario
from app.tests.test_marketplace_ppm_lifecycle import ppm_harness
from app.tests.test_ppm_saved_source_stability import linked, real_sources  # noqa: F401


async def setup(h, monkeypatch):
    listing, profile, _, _ = await linked(h, monkeypatch)
    profile.intake_id = uuid4()
    await h.db.commit()
    factory = async_sessionmaker(h.db.bind, expire_on_commit=False)
    artifacts = [(SimpleNamespace(id=uuid4(), s3_key="synthetic/new.pdf"), kind, kind)
        for kind in (E.DocumentKind.EXECUTIVE_SUMMARY, E.DocumentKind.UNDERWRITING_PACKAGE, E.DocumentKind.INVESTOR_PRESENTATION)]

    async def fingerprint(_db, current):
        return sync.saved_source_fingerprint(current.financial_plan)

    monkeypatch.setattr(packages, "current_input_fingerprint", fingerprint)
    monkeypatch.setattr(packages, "lock_source", AsyncMock())
    prepare = AsyncMock(return_value={"artifacts": artifacts, "created_s3_keys": []})
    monkeypatch.setattr(packages, "prepare", prepare)
    monkeypatch.setattr(packages, "publish_prepared", AsyncMock(side_effect=lambda _db, value, **kw: value))
    discard = AsyncMock()
    monkeypatch.setattr(packages, "discard", discard)
    monkeypatch.setattr(sync, "current_package", AsyncMock(return_value={"generation_id": "synthetic-fixed"}))
    monkeypatch.setattr(package_documents.storage, "copy_object", lambda *args, **kw: True)
    return listing, profile, factory, prepare, discard


async def reload(h, listing):
    await h.db.refresh(listing)
    await h.db.refresh(listing, attribute_names=["documents"])
    return refresh.state(listing)


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
async def test_real_orm_manual_job_is_durable_idempotent_and_noop_preserves_ppm_bytes(monkeypatch):
    async with ppm_harness() as h:
        listing, profile, factory, prepare, _ = await setup(h, monkeypatch)
        await router_admin._apply_file_financing(h.db, listing)
        current = await ppm.saved_context(h.db, listing)
        draft = await ppm.generate(h.db, listing, h.current.id, ppm.PpmGenerateIn(
            expected_source_fingerprint=current["fingerprint"], idempotency_key="initial-synthetic"))
        version = draft["latest_version"]
        await h.db.commit()
        response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/refresh", json={"idempotency_key": "first"})
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]
        assert await refresh.run_one(factory, listing.id) == "succeeded"
        result = await reload(h, listing)
        assert result["job_id"] == job_id and result["documents_updated"]
        assert result["review_required"] is True
        assert (await ppm.read_state(h.db, listing))["latest_version"] == version
        documents = [(row.id, row.s3_key) for row in listing.documents]
        fingerprints = (await ppm.saved_context(h.db, listing))["fingerprint"]
        repeat = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/refresh", json={"idempotency_key": "first"})
        assert repeat.json()["job_id"] == job_id
        assert await refresh.run_one(factory, listing.id) == "skipped"
        assert [(row.id, row.s3_key) for row in listing.documents] == documents
        assert (await ppm.saved_context(h.db, listing))["fingerprint"] == fingerprints
        assert prepare.await_count == 1
        assert (await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/refresh")).json()["status"] == "succeeded"
        # A saved economic change gets new intent and an unapproved PPM draft.
        profile.financial_plan = {**profile.financial_plan, "new_loan_rate_pct": 19.5}
        await h.db.commit()
        request = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/refresh", json={"idempotency_key": "changed"})
        assert request.status_code == 202
        assert request.json()["job_id"] != job_id
        assert await refresh.run_one(factory, listing.id) == "succeeded"
        await reload(h, listing)
        updated = await ppm.read_state(h.db, listing)
        assert updated["latest_version"] > version
        assert updated["current_approved"] is None
        assert updated["current_draft"]["stale"] is False
        conflict = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/refresh", json={"idempotency_key": "first"})
        assert conflict.status_code == 409


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
async def test_debounce_expired_lease_and_two_workers_do_not_duplicate_current_work(monkeypatch):
    async with ppm_harness() as h:
        listing, profile, factory, prepare, _ = await setup(h, monkeypatch)
        await refresh.queue_profile(h.db, profile.id)
        first = refresh.state(listing)["job_id"]
        await h.db.commit()
        assert await refresh.run_one(factory, listing.id) == "skipped"
        await refresh.queue_profile(h.db, profile.id)
        assert refresh.state(listing)["job_id"] != first
        job = refresh._job(listing)
        job.update(status="running", lease_until=(datetime.now(UTC) - timedelta(seconds=1)).isoformat())
        refresh._put(listing, job)
        await h.db.commit()
        async with factory() as db:
            claim = await refresh._claim(db, listing.id)
            assert claim is not None
        async with factory() as db:
            assert await refresh._claim(db, listing.id) is None
        assert prepare.await_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", ["new_intent", "reclaimed", "source_change", "render_error", "closed"])
@pytest.mark.usefixtures("real_sources")
async def test_render_races_and_failures_retain_snapshot_documents_and_newer_intent(monkeypatch, cause):
    async with ppm_harness() as h:
        listing, profile, factory, prepare, discard = await setup(h, monkeypatch)
        queued = await refresh.request(h.db, listing, actor_id=h.current.id)
        await h.db.commit()
        snapshot = deepcopy(listing.source_snapshot)
        before_docs = [(row.id, row.status) for row in listing.documents]
        payload = prepare.return_value

        async def during(db, current, **kw):
            if cause == "render_error":
                raise RuntimeError("synthetic secret must not leak")
            async with factory() as other:
                changed = await refresh._load(other, listing.id)
                if cause == "new_intent":
                    refresh._enqueue(changed, reason="newer-save")
                elif cause == "reclaimed":
                    job = refresh._job(changed)
                    job["claim_token"] = str(uuid4())
                    refresh._put(changed, job)
                elif cause == "closed":
                    changed.status = E.ListingStatus.WITHDRAWN
                else:
                    row = await other.get(ApplicationProfile, profile.id)
                    row.financial_plan = {**row.financial_plan, "new_loan_rate_pct": 7.25}
                await other.commit()
            return payload

        prepare.side_effect = during
        assert await refresh.run_one(factory, listing.id) == "failed"
        status = await reload(h, listing)
        assert [(row.id, row.status) for row in listing.documents] == before_docs
        assert listing.source_snapshot.get("financials") == snapshot.get("financials")
        if cause == "new_intent":
            assert status["job_id"] != queued["job_id"] and status["status"] == "queued"
        elif cause == "reclaimed":
            assert status["job_id"] == queued["job_id"] and status["status"] == "running"
        else:
            assert status["status"] in {"failed", "blocked"}
        assert "secret" not in str(status)
        if cause != "render_error":
            discard.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_before_pricing_and_standalone_listing_rejected(monkeypatch):
    async with ppm_harness() as h:
        scenario = Scenario(h)
        admin = await scenario.admin()
        listing = await scenario.listing(admin, publish=False)
        priced = AsyncMock()
        monkeypatch.setattr(refresh, "_source", priced)
        response = await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/refresh", json={})
        assert response.status_code == 422
        member, _ = await scenario.member("refresh-synthetic@example.test")
        h.current = member
        assert (await h.client.post(f"/api/v1/capital/admin/listings/{listing.id}/refresh", json={})).status_code == 403
        assert (await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/refresh")).status_code == 403
        priced.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
async def test_recovery_detects_saved_sources_without_any_browser_and_status_get_is_read_only(monkeypatch):
    async with ppm_harness() as h:
        listing, _, factory, _, _ = await setup(h, monkeypatch)
        refresh._cursor = None
        result = await h.client.get(f"/api/v1/capital/admin/listings/{listing.id}/refresh")
        assert result.json()["status"] == "idle"
        assert "refresh_job" not in listing.source_snapshot
        await refresh.recover_sources(factory, limit=3)
        result = await reload(h, listing)
        assert result["status"] == "queued"
        assert result["source_fingerprint"]


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
async def test_lost_commit_ack_retains_success_and_does_not_delete_committed_pdf_bytes(monkeypatch):
    async with ppm_harness() as h:
        listing, _, factory, _, discard = await setup(h, monkeypatch)
        await refresh.request(h.db, listing, actor_id=h.current.id)
        await h.db.commit()
        original = AsyncSession.commit
        raised = False

        async def lost_ack(db):
            nonlocal raised
            committed_job = any(isinstance(row, type(listing)) and refresh._job(row).get("status") == "succeeded" for row in db.identity_map.values())
            await original(db)
            if committed_job and not raised:
                raised = True
                raise RuntimeError("synthetic lost acknowledgement after successful commit")

        monkeypatch.setattr(AsyncSession, "commit", lost_ack)
        assert await refresh.run_one(factory, listing.id) == "succeeded"
        assert raised
        result = await reload(h, listing)
        assert result["status"] == "succeeded"
        assert sum(row.kind == E.DocumentKind.INVESTOR_PRESENTATION for row in listing.documents) == 1
        discard.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
@pytest.mark.parametrize("economic_change", [False, True])
async def test_committed_offer_can_prepare_updated_evidence_but_cannot_change_contract_terms(monkeypatch, request, economic_change):
    evidence = request.getfixturevalue("real_sources")
    async with ppm_harness() as h:
        listing, profile, factory, _, _ = await setup(h, monkeypatch)
        await router_admin._apply_file_financing(h.db, listing)
        listing.status, listing.published_at = E.ListingStatus.OPEN, datetime.now(UTC)
        sync.preserve_active_financials(listing)
        original = deepcopy(listing.source_snapshot["active_financials"])
        _, member = await Scenario(h).member("committed-refresh@example.test")
        h.db.add(CpSubscription(listing_id=listing.id, member_id=member.id, amount=25000, status="signed", ack_ppm=True, ack_accredited=True))
        if economic_change:
            profile.financial_plan = {**profile.financial_plan, "new_loan_rate_pct": 7.25}
        else:
            evidence[0]["key_facts"]["ordinary_business_income"] += 1000
        await h.db.commit()
        await refresh.request(h.db, listing, actor_id=h.current.id)
        await h.db.commit()
        result = await refresh.run_one(factory, listing.id)
        status = await reload(h, listing)
        assert listing.source_snapshot["active_financials"] == original
        if economic_change:
            assert result == "failed" and status["status"] == "blocked"
        else:
            assert result == "succeeded" and status["figures_updated"] is True
            assert listings.partner_financials(listing)["financial_update_pending"] is True


@pytest.mark.asyncio
@pytest.mark.usefixtures("real_sources")
async def test_real_saved_financial_action_enqueues_in_same_transaction_without_opening_listing(monkeypatch):
    async with ppm_harness() as h:
        listing, profile, factory, _, _ = await setup(h, monkeypatch)
        profile.financial_plan = {**profile.financial_plan, "new_loan_rate_pct": 17.5}
        await log_profile_action(h.db, profile, h.current, "financial_plan.updated", "Synthetic saved financial update")
        await h.db.commit()
        async with factory() as db:
            current = await refresh._load(db, listing.id)
            assert refresh.state(current)["status"] == "queued"
            assert refresh._job(current)["reason"] == "saved_financial_input"
            assert (await db.get(ApplicationProfile, profile.id)).financial_plan["new_loan_rate_pct"] == 17.5
