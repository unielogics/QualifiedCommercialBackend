from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.dealer_os import router as dealer_router
from app.dealer_os.services import client_room
from app.routers import application_profiles, buckets, public_payments
from app.routers import payments as payment_routes
from app.schemas.payments import FeeObligationCreate
from app.services import ach_fee_workflow, payments


def _signed_terms() -> dict[str, object]:
    return {
        "include_origination_fee": True,
        "include_consulting_fee": False,
        "consulting_milestone_confirmed": False,
        "origination_fee_cents": 10_000,
        "consulting_fee_cents": 0,
        "gross_fee_cents": 10_000,
        "client_ach_cents": 8_000,
        "origination_client_ach_cents": 8_000,
        "consulting_client_ach_cents": 0,
        "bank_direct_cents": 2_000,
        "external_cents": 0,
        "deferred_cents": 0,
        "waived_cents": 0,
    }


def test_compatibility_obligation_must_match_executed_fee_terms() -> None:
    payload = FeeObligationCreate(
        include_origination_fee=True,
        include_consulting_fee=False,
        client_ach_cents=8_000,
        origination_client_ach_cents=8_000,
        bank_direct_cents=2_000,
    )
    exact = payments._require_obligation_matches_signed_fee_terms(
        payload=payload,
        signed_terms=_signed_terms(),
        origination_fee_cents=10_000,
        consulting_fee_cents=0,
        gross_fee_cents=10_000,
        allocation={
            "client_ach_cents": 8_000,
            "bank_direct_cents": 2_000,
            "external_cents": 0,
            "deferred_cents": 0,
            "waived_cents": 0,
        },
        origination_client_ach_cents=8_000,
        consulting_client_ach_cents=0,
    )
    assert exact == _signed_terms()

    with pytest.raises(HTTPException) as error:
        payments._require_obligation_matches_signed_fee_terms(
            payload=payload,
            signed_terms=_signed_terms(),
            origination_fee_cents=10_000,
            consulting_fee_cents=0,
            gross_fee_cents=10_000,
            allocation={
                "client_ach_cents": 9_000,
                "bank_direct_cents": 1_000,
                "external_cents": 0,
                "deferred_cents": 0,
                "waived_cents": 0,
            },
            origination_client_ach_cents=9_000,
            consulting_client_ach_cents=0,
        )
    assert error.value.status_code == 409


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_state",
    [
        {"completed_at": datetime.now(UTC), "allow_multiple_sessions": False},
        {"expires_at": datetime.now(UTC) - timedelta(seconds=1)},
    ],
)
async def test_payment_identity_rejects_unusable_room(
    invalid_state: dict[str, object],
) -> None:
    link = SimpleNamespace(
        **{
            "id": uuid4(),
            "bucket_id": uuid4(),
            "recipient_email": "client@example.com",
            "status": "active",
            "completed_at": None,
            "allow_multiple_sessions": True,
            "expires_at": None,
            **invalid_state,
        }
    )
    profile = SimpleNamespace(id=uuid4())
    with (
        patch.object(
            payments,
            "_profile_identity",
            AsyncMock(return_value=("Business", "Client", "client@example.com")),
        ),
        patch(
            "app.dealer_os.services.client_room.active_link",
            AsyncMock(return_value=link),
        ),
    ):
        with pytest.raises(HTTPException) as error:
            await payments.assert_payment_room_identity(
                SimpleNamespace(), link=link, profile=profile
            )
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_secondary_room_cannot_read_success_fee_agreement_text() -> None:
    fee = SimpleNamespace(
        id=uuid4(),
        name="Success Fee Agreement",
        signature_kind="success_fee_agreement",
        status="requested",
        signature_document_text="private deal economics",
    )
    credit = SimpleNamespace(
        id=uuid4(),
        name="Credit Authorization",
        signature_kind="credit_authorization",
        status="requested",
        signature_document_text="ordinary signable text",
    )
    result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [fee, credit])
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    hidden = await application_profiles._application_room_signables(
        db, uuid4(), include_payment_agreements=False
    )
    visible = await application_profiles._application_room_signables(
        db, uuid4(), include_payment_agreements=True
    )

    assert [row.id for row in hidden] == [credit.id]
    assert {row.id for row in visible} == {fee.id, credit.id}
    assert "private deal economics" not in str(hidden)


def test_fee_agreement_sign_route_matches_room_variant() -> None:
    assert ach_fee_workflow._success_fee_sign_route(
        SimpleNamespace(dealer_id=None)
    ) == "/application-profiles/public/room/{token}/sign"
    assert ach_fee_workflow._success_fee_sign_route(
        SimpleNamespace(dealer_id=uuid4())
    ) == "/dealer-os/public/room/{token}/sign"


def test_room_usability_allows_completed_multiuse_but_not_single_use() -> None:
    base = {
        "status": "active",
        "expires_at": None,
        "completed_at": datetime.now(UTC),
    }
    assert client_room.link_is_usable(
        SimpleNamespace(**base, allow_multiple_sessions=True)
    )
    assert not client_room.link_is_usable(
        SimpleNamespace(**base, allow_multiple_sessions=False)
    )


def test_dealer_secondary_room_cannot_read_success_fee_agreement_text() -> None:
    fee = SimpleNamespace(
        id=uuid4(),
        name="Success Fee Agreement",
        signature_kind="success_fee_agreement",
        status="requested",
        signature_document_text="private dealer economics",
    )
    ordinary = SimpleNamespace(
        id=uuid4(),
        name="Credit Authorization",
        signature_kind="credit_authorization",
        status="requested",
        signature_document_text="ordinary signable text",
    )

    hidden = dealer_router._room_signable_reads(
        [fee, ordinary], include_payment_agreements=False
    )
    visible = dealer_router._room_signable_reads(
        [fee, ordinary], include_payment_agreements=True
    )

    assert [row.id for row in hidden] == [ordinary.id]
    assert {row.id for row in visible} == {fee.id, ordinary.id}
    assert "private dealer economics" not in str(hidden)


def test_success_fee_signing_gate_fails_closed_before_upload() -> None:
    with patch.object(
        ach_fee_workflow,
        "get_settings",
        return_value=SimpleNamespace(payments_enabled=False),
    ):
        with pytest.raises(HTTPException) as error:
            ach_fee_workflow.require_fee_workflow_enabled()
    assert error.value.status_code == 503

    application_sign = inspect.getsource(
        application_profiles.public_application_room_sign
    )
    dealer_sign = inspect.getsource(dealer_router.public_room_sign)
    for source in (application_sign, dealer_sign):
        assert source.index(
            "ach_fee_workflow.require_fee_workflow_enabled()"
        ) < source.index("result_file = await _sign_requested_document(")


@pytest.mark.asyncio
async def test_plaid_exchange_is_durable_before_account_lookup() -> None:
    payload = public_payments.RoomPaymentExchange(
        passcode="123456",
        owner_type="business",
        purpose="fee",
        business_account_attested=True,
        public_token="public-once",
        plaid_account_id="account-1",
    )
    added = []
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
        ),
        add=added.append,
        flush=AsyncMock(),
        commit=AsyncMock(),
    )
    exchange = AsyncMock(return_value=("access-secret", "item-1"))
    with (
        patch.object(public_payments.plaid_transfer, "exchange_public_token", exchange),
        patch.object(
            public_payments.plaid_transfer,
            "encrypt_access_token",
            return_value="encrypted-secret",
        ),
    ):
        source, access_token = (
            await public_payments._exchange_or_recover_payment_source(
                db,
                profile=SimpleNamespace(id=uuid4(), client_id=uuid4()),
                link=SimpleNamespace(id=uuid4()),
                payload=payload,
            )
        )

    assert source is added[0]
    assert source.status == "pending"
    assert source.access_token_ciphertext == "encrypted-secret"
    assert source.metadata_json["exchange_public_token_sha256"]
    assert access_token == "access-secret"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_plaid_exchange_retry_recovers_without_reexchanging_public_token() -> None:
    payload = public_payments.RoomPaymentExchange(
        passcode="123456",
        owner_type="business",
        purpose="fee",
        business_account_attested=True,
        public_token="public-once",
        plaid_account_id="account-1",
    )
    link_id = uuid4()
    pending = SimpleNamespace(
        status="pending",
        owner_type="business",
        access_token_ciphertext="encrypted-secret",
        plaid_item_id="item-1",
        metadata_json={
            "payment_purpose": "fee",
            "room_link_id": str(link_id),
        },
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: pending)
        )
    )
    exchange = AsyncMock()
    with (
        patch.object(public_payments.plaid_transfer, "exchange_public_token", exchange),
        patch.object(
            public_payments.plaid_transfer,
            "decrypt_access_token",
            return_value="access-secret",
        ),
    ):
        source, access_token = (
            await public_payments._exchange_or_recover_payment_source(
                db,
                profile=SimpleNamespace(id=uuid4()),
                link=SimpleNamespace(id=link_id),
                payload=payload,
            )
        )

    assert source is pending
    assert access_token == "access-secret"
    exchange.assert_not_awaited()


def _verified_exchange_source(link_id, **overrides):
    values = {
        "status": "verified",
        "revoked_at": None,
        "plaid_account_id": "account-1",
        "owner_type": "business",
        "ach_class": "CCD",
        "plaid_item_id": "item-1",
        "access_token_ciphertext": "encrypted-secret",
        "account_mask": "1234",
        "account_subtype": "checking",
        "verified_at": datetime.now(UTC),
        "metadata_json": {
            "payment_purpose": "fee",
            "room_link_id": str(link_id),
            "account_type": "depository",
            "business_account_attestation": {
                "attested": True,
                "room_link_id": str(link_id),
            },
        },
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.asyncio
async def test_verified_plaid_exchange_retry_short_circuits_all_provider_calls() -> None:
    profile = SimpleNamespace(id=uuid4(), client_id=uuid4())
    link = SimpleNamespace(id=uuid4())
    source = _verified_exchange_source(link.id)
    payload = public_payments.RoomPaymentExchange(
        passcode="123456",
        owner_type="business",
        purpose="fee",
        business_account_attested=True,
        public_token="public-once",
        plaid_account_id="account-1",
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: source)
        )
    )
    exchange = AsyncMock()
    accounts = AsyncMock()
    state = AsyncMock(return_value={"funding_source": "connected"})
    with (
        patch.object(public_payments, "_enabled"),
        patch.object(
            public_payments,
            "_room",
            AsyncMock(return_value=(link, profile)),
        ),
        patch.object(
            public_payments.pay,
            "current_obligation",
            AsyncMock(return_value=SimpleNamespace(status="awaiting_authorization")),
        ),
        patch.object(public_payments.plaid_transfer, "exchange_public_token", exchange),
        patch.object(public_payments.plaid_transfer, "accounts", accounts),
        patch.object(public_payments, "_state", state),
    ):
        result = await public_payments.public_payment_exchange(
            "room-token",
            payload,
            SimpleNamespace(),
            db,
        )

    assert result == {"funding_source": "connected"}
    exchange.assert_not_awaited()
    accounts.assert_not_awaited()
    state.assert_awaited_once_with(db, profile)


@pytest.mark.asyncio
async def test_verified_plaid_exchange_retry_rejects_identity_or_consent_mismatch() -> None:
    link = SimpleNamespace(id=uuid4())
    payload = public_payments.RoomPaymentExchange(
        passcode="123456",
        owner_type="business",
        purpose="fee",
        business_account_attested=True,
        public_token="public-once",
        plaid_account_id="account-1",
    )
    bad_sources = [
        _verified_exchange_source(link.id, plaid_account_id="account-other"),
        _verified_exchange_source(link.id, owner_type="consumer"),
        _verified_exchange_source(
            link.id,
            metadata_json={
                **_verified_exchange_source(link.id).metadata_json,
                "payment_purpose": "private_schedule",
            },
        ),
        _verified_exchange_source(
            link.id,
            metadata_json={
                **_verified_exchange_source(link.id).metadata_json,
                "business_account_attestation": {
                    "attested": False,
                    "room_link_id": str(link.id),
                },
            },
        ),
    ]
    exchange = AsyncMock()
    with patch.object(
        public_payments.plaid_transfer, "exchange_public_token", exchange
    ):
        for source in bad_sources:
            db = SimpleNamespace(
                execute=AsyncMock(
                    return_value=SimpleNamespace(
                        scalar_one_or_none=lambda source=source: source
                    )
                )
            )
            with pytest.raises(HTTPException) as error:
                await public_payments._exchange_or_recover_payment_source(
                    db,
                    profile=SimpleNamespace(id=uuid4()),
                    link=link,
                    payload=payload,
                )
            assert error.value.status_code == 409
    exchange.assert_not_awaited()


@pytest.mark.asyncio
async def test_plaid_exchange_removes_item_when_durable_write_fails() -> None:
    payload = public_payments.RoomPaymentExchange(
        passcode="123456",
        owner_type="business",
        purpose="fee",
        business_account_attested=True,
        public_token="public-once",
        plaid_account_id="account-1",
    )
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(scalar_one_or_none=lambda: None)
        ),
        add=lambda _row: None,
        flush=AsyncMock(),
        commit=AsyncMock(side_effect=RuntimeError("commit failed")),
    )
    remove = AsyncMock()
    with (
        patch.object(
            public_payments.plaid_transfer,
            "exchange_public_token",
            AsyncMock(return_value=("access-secret", "item-1")),
        ),
        patch.object(
            public_payments.plaid_transfer,
            "encrypt_access_token",
            return_value="encrypted-secret",
        ),
        patch.object(public_payments.plaid_transfer, "remove_item", remove),
    ):
        with pytest.raises(RuntimeError, match="commit failed"):
            await public_payments._exchange_or_recover_payment_source(
                db,
                profile=SimpleNamespace(id=uuid4(), client_id=uuid4()),
                link=SimpleNamespace(id=uuid4()),
                payload=payload,
            )

    remove.assert_awaited_once_with("access-secret")


def test_payment_boundaries_use_shared_authority_and_hide_protected_documents() -> None:
    staff_send = inspect.getsource(payment_routes._send_authorization_email_once)
    room_read = inspect.getsource(buckets._request_access_read)
    application_state = inspect.getsource(application_profiles._application_room_state)
    public_state = inspect.getsource(public_payments._state)

    assert "assert_payment_room_identity" in staff_send
    assert "PROTECTED_RETENTION_CLASSES" in room_read
    assert 'd.signature_kind != "success_fee_agreement"' in room_read
    assert "include_payment_agreements=payment_agreements_visible" in application_state
    assert "pay._mandate_is_current(plan_mandate)" in public_state
