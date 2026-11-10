from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import Role
from app.routers import application_offer_deliveries as delivery_routes
from app.routers import application_profiles as routes
from app.schemas.application_offer_delivery import ProductionTermSheetItemRef
from app.services import application_offer_deliveries as offers


def fixture(kind="application_term_sheet", **summary):
    now = datetime.now(UTC)
    profile = SimpleNamespace(id=uuid4(), underwriting_accepted_amount=None, underwriting_accepted_at=None,
        accepted_terms_ref=None, underwriting_updated_at=None, underwriting_status="in_underwriting", underwriting_approved_amount=300000)
    delivery = SimpleNamespace(id=uuid4(), profile_id=profile.id, sent_at=now - timedelta(hours=1), status="sent", items=[])
    item = SimpleNamespace(id=uuid4(), delivery_id=delivery.id, kind=kind, source_id=uuid4(), source_version=5,
        decision_status="pending", expires_at=now + timedelta(hours=1), sha256="a" * 64,
        label="Loan terms v5", canonical_summary={"accepted_amount": "250000.00", "rate_pct": "14.5", "term_months": 36, **summary},
        item_key="", title="Financing terms", file_name="Terms.pdf", content_type="application/pdf", size_bytes=100,
        responded_at=None, responded_name=None)
    item.item_key = offers.item_key(kind, item.source_id, item.source_version)
    delivery.items = [item]
    return profile, delivery, item, now


async def respond(profile, delivery, item, when, *, response="accepted", channel="authenticated_client", db=None):
    return await offers.record_response(db or SimpleNamespace(), profile=profile, delivery=delivery, item=item,
        response=response, responder_name="Synthetic Client", reason="Client decision", channel=channel,
        responded_at=when, ip_address=None, user_agent="Synthetic test", user_id=uuid4(),
        attestation="Staff recorded the client's explicit decision.")


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["application_term_sheet", "production_term_sheet"])
@pytest.mark.parametrize("channel", ["authenticated_client", "secure_room", "email", "phone"])
async def test_acceptance_projects_exact_immutable_source_and_time_without_approving_file(kind, channel):
    profile, delivery, item, when = fixture(kind)
    with patch.object(offers.application_profiles, "log_profile_action", AsyncMock()) as audit:
        assert await respond(profile, delivery, item, when, channel=channel)
    assert profile.underwriting_accepted_amount == 250000
    assert profile.underwriting_accepted_at == when
    ref = profile.accepted_terms_ref
    assert (ref["kind"], ref["id"], ref["version"]) == (kind, str(item.source_id), 5)
    assert (ref["rate_pct"], ref["term_months"]) == (14.5, 36)
    assert ref["delivery_id"] == str(delivery.id) and ref["snapshot_sha256"] == item.sha256
    assert profile.underwriting_status == "in_underwriting" and profile.underwriting_approved_amount == 300000
    assert audit.await_args.kwargs["metadata"]["profile_projection_updated"] is True
    previous = deepcopy(vars(profile))
    assert not await respond(profile, delivery, item, when + timedelta(seconds=1), channel=channel)
    assert vars(profile) == previous


@pytest.mark.asyncio
async def test_legacy_acceptance_keeps_unknown_rate_and_term_without_reading_mutable_current_terms():
    profile, delivery, item, when = fixture()
    item.canonical_summary = {"lines": ["Amount: $250,000.00"]}
    db = SimpleNamespace(get=AsyncMock(side_effect=AssertionError("Must not read mutable current terms")))
    with patch.object(offers.application_profiles, "log_profile_action", AsyncMock()):
        assert await respond(profile, delivery, item, when, db=db)
    assert profile.accepted_terms_ref["id"] == str(item.source_id)
    assert profile.accepted_terms_ref["version"] == 5
    assert profile.accepted_terms_ref["rate_pct"] is None and profile.accepted_terms_ref["term_months"] is None
    db.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("older_version", [True, False])
async def test_historical_response_is_retained_without_regressing_newer_profile_acceptance(older_version):
    profile, delivery, item, when = fixture()
    profile.underwriting_accepted_amount = 300000
    profile.underwriting_accepted_at = when - timedelta(minutes=1) if older_version else when + timedelta(minutes=1)
    profile.accepted_terms_ref = {"kind": item.kind, "id": str(uuid4()), "version": 6 if older_version else 4}
    previous = deepcopy(vars(profile))
    with patch.object(offers.application_profiles, "log_profile_action", AsyncMock()) as audit:
        assert await respond(profile, delivery, item, when)
    assert item.decision_status == "accepted" and item.responded_at == when
    assert vars(profile) == previous
    assert audit.await_args.kwargs["metadata"]["profile_projection_updated"] is False


@pytest.mark.asyncio
async def test_client_decline_is_an_offer_response_and_does_not_deny_or_close_underwriting():
    profile, delivery, item, when = fixture()
    before = deepcopy(vars(profile))
    assert await respond(profile, delivery, item, when, response="declined")
    assert item.decision_status == "declined" and delivery.status == "completed"
    assert vars(profile) == before


@pytest.mark.asyncio
async def test_superseded_offer_cannot_replace_profile_acceptance():
    profile, delivery, item, when = fixture()
    item.decision_status = "superseded"
    before = deepcopy(vars(profile))
    with pytest.raises(HTTPException) as error:
        await respond(profile, delivery, item, when)
    assert error.value.status_code == 409 and vars(profile) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy_ref", ["legacy import", ["unexpected"], 7])
async def test_malformed_legacy_reference_does_not_prevent_a_valid_acceptance(legacy_ref):
    profile, delivery, item, when = fixture()
    profile.accepted_terms_ref = legacy_ref
    with patch.object(offers.application_profiles, "log_profile_action", AsyncMock()):
        assert await respond(profile, delivery, item, when)
    assert profile.accepted_terms_ref["id"] == str(item.source_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("refreshed_status", ["pending", "superseded"])
async def test_async_session_acceptance_locks_and_refreshes_before_mutating(refreshed_status):
    """Exercise the production AsyncSession branch, not the lightweight fake path."""
    profile, delivery, item, when = fixture()
    events = []
    async with AsyncSession() as db:
        async def execute(statement, *args, **kwargs):
            assert db.sync_session.autoflush is False
            assert "application_profiles" in str(statement) and statement._for_update_arg is not None
            assert item.decision_status == "pending" and profile.accepted_terms_ref is None
            events.append("profile_lock")

        async def refresh(target, *, attribute_names, **kwargs):
            assert db.sync_session.autoflush is False
            if target is profile:
                events.append("profile_refresh")
                assert "accepted_terms_ref" in attribute_names
                # Another accepted delivery committed while this request waited.
                profile.underwriting_accepted_amount = 300000
                profile.underwriting_accepted_at = when + timedelta(minutes=1)
                profile.accepted_terms_ref = {"kind": item.kind, "id": str(uuid4()), "version": 6}
            else:
                events.append("item_refresh_lock")
                assert target is item and kwargs["with_for_update"] is True
                assert "decision_status" in attribute_names
                item.decision_status = refreshed_status

        db.execute = AsyncMock(side_effect=execute)
        db.refresh = AsyncMock(side_effect=refresh)
        with patch.object(offers.application_profiles, "log_profile_action", AsyncMock()) as audit:
            if refreshed_status == "superseded":
                with pytest.raises(HTTPException) as error:
                    await respond(profile, delivery, item, when, db=db)
                assert error.value.status_code == 409
                audit.assert_not_awaited()
                assert item.responded_at is None
            else:
                assert await respond(profile, delivery, item, when, db=db)
                assert audit.await_args.kwargs["metadata"]["profile_projection_updated"] is False
        assert events == ["profile_lock", "profile_refresh", "item_refresh_lock"]
        assert profile.underwriting_accepted_amount == 300000
        assert profile.accepted_terms_ref["version"] == 6
        assert db.sync_session.autoflush is True


@pytest.mark.asyncio
async def test_publication_delivery_lookup_uses_profile_then_delivery_lock():
    profile, delivery, _item, _when = fixture()
    delivery.effects_applied_at = datetime.now(UTC)
    events = []
    async with AsyncSession() as db:
        async def execute(statement, *args, **kwargs):
            assert statement._for_update_arg is not None
            if "FROM application_profiles" in str(statement):
                assert db.sync_session.autoflush is False
                events.append("profile_lock")
                return SimpleNamespace()
            events.append("delivery_lock")
            return SimpleNamespace(scalar_one_or_none=lambda: delivery)
        db.execute = AsyncMock(side_effect=execute)
        await delivery_routes._apply_delivery_effects(db, profile=profile, delivery_id=delivery.id,
            user=SimpleNamespace(id=uuid4()))
    assert events == ["profile_lock", "delivery_lock"]


@pytest.mark.asyncio
async def test_provider_source_lock_uses_the_same_profile_first_order_as_terms_save():
    profile, _delivery, item, _when = fixture("production_term_sheet")
    source = SimpleNamespace(id=item.source_id, status="current", version=item.source_version)
    events = []
    async with AsyncSession() as db:
        async def execute(statement, *args, **kwargs):
            assert db.sync_session.autoflush is False
            assert statement._for_update_arg is not None
            events.append("profile_lock")
        async def get(model, source_id, *, with_for_update):
            assert with_for_update is True and source_id == source.id
            events.append("source_lock")
            return source
        db.execute = AsyncMock(side_effect=execute)
        db.get = AsyncMock(side_effect=get)
        with patch.object(offers, "_business_context", AsyncMock(return_value=("Synthetic LLC", "Client"))), \
            patch.object(offers.production_term_sheets, "current_sheet", AsyncMock(return_value=source)), \
            patch.object(offers, "production_term_lines", return_value=[]), \
            patch.object(offers, "_production_expiry", return_value=None):
            await offers.resolve_offers(db, profile, [ProductionTermSheetItemRef(kind=item.kind,
                term_sheet_id=source.id, expected_version=source.version)], lock=True)
    assert events == ["profile_lock", "source_lock"]


@pytest.mark.asyncio
@pytest.mark.parametrize("ownership_retained", [True, False])
async def test_authenticated_lookup_rechecks_owner_after_profile_lock_before_delivery_lock(ownership_retained):
    profile, delivery, _item, _when = fixture()
    actor = SimpleNamespace(role=Role.CLIENT, client=SimpleNamespace(id=uuid4()))
    events = []
    async with AsyncSession() as db:
        async def execute(statement, *args, **kwargs):
            text = str(statement)
            if "JOIN application_profiles" in text:
                assert statement._for_update_arg is None
                events.append("authorize")
                row = (delivery, profile) if ownership_retained or events.count("authorize") == 1 else None
                return SimpleNamespace(one_or_none=lambda: row)
            assert statement._for_update_arg is not None
            if "FROM application_profiles" in text:
                events.append("profile_lock")
                return SimpleNamespace()
            events.append("delivery_lock")
            return SimpleNamespace(scalar_one_or_none=lambda: delivery)
        db.execute = AsyncMock(side_effect=execute)
        if ownership_retained:
            assert await delivery_routes._client_delivery(db, actor, delivery.id, lock=True) == (profile, delivery)
            assert events == ["authorize", "profile_lock", "authorize", "profile_lock", "delivery_lock"]
        else:
            with pytest.raises(HTTPException) as error:
                await delivery_routes._client_delivery(db, actor, delivery.id, lock=True)
            assert error.value.status_code == 404
            assert events == ["authorize", "profile_lock", "authorize"]


def test_delivery_item_read_exposes_exact_source_identity_for_current_version_matching():
    _profile, _delivery, item, _when = fixture()
    result = offers.item_read(item, base_url="/scoped/items")
    assert result.source_id == item.source_id and result.source_version == 5
    assert result.item_key.endswith(":v5")


@pytest.mark.parametrize("kind", ["application_term_sheet", "production_term_sheet"])
def test_new_delivery_snapshot_freezes_typed_quote_values_for_acceptance(kind):
    row = SimpleNamespace(approved_amount="250000.00", amount="250000.00", rate_pct="14.1250", apr_pct="14.1250", term_months=36)
    offer = offers.ResolvedOffer(kind=kind, source_id=uuid4(), version=5, source=row, label="Loan terms v5",
        title="Financing terms", file_name="Terms.pdf", lines=["Immutable visible terms"])
    snapshot = offer.canonical_summary()
    row.rate_pct = row.apr_pct = "99"
    row.term_months = 12
    assert snapshot["accepted_amount"] == "250000.00"
    assert snapshot["rate_pct"] == "14.1250" and snapshot["term_months"] == 36


@pytest.mark.asyncio
@pytest.mark.parametrize("role,can_manage", [(Role.LOAN_EXEC, False), (Role.SUPER_ADMIN, True)])
async def test_marketplace_publication_read_is_scoped_and_does_not_expose_admin_data(role, can_manage):
    profile = SimpleNamespace(id=uuid4(), loan_id=None)
    listing = SimpleNamespace(id=uuid4(), ref="QC-PRIVATE-1", name="Synthetic offering", status="open",
        published_at=datetime.now(UTC), source_snapshot={"private": "Investor private data"})
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: listing)), commit=AsyncMock())
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)) as load:
        result = await routes.get_marketplace_publication(profile.id, SimpleNamespace(role=role), db)
    assert result.can_manage is can_manage and result.listing.ref == listing.ref
    assert "Investor private data" not in result.model_dump_json()
    load.assert_awaited_once()
    db.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_marketplace_publication_missing_listing_is_distinct_from_scope_or_query_errors():
    profile = SimpleNamespace(id=uuid4(), loan_id=None)
    actor = SimpleNamespace(role=Role.LOAN_EXEC)
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None)))
    with patch.object(routes.profiles, "load_profile", AsyncMock(return_value=profile)):
        assert (await routes.get_marketplace_publication(profile.id, actor, db)).listing is None
        db.execute.side_effect = RuntimeError("Synthetic query failure")
        with pytest.raises(RuntimeError):
            await routes.get_marketplace_publication(profile.id, actor, db)
    with patch.object(routes.profiles, "load_profile", AsyncMock(side_effect=HTTPException(404, "Not found"))):
        with pytest.raises(HTTPException) as error:
            await routes.get_marketplace_publication(profile.id, actor, db)
    assert error.value.status_code == 404
    with pytest.raises(HTTPException) as denied:
        await routes.get_marketplace_publication(profile.id, SimpleNamespace(role=Role.BROKER), db)
    assert denied.value.status_code == 403
