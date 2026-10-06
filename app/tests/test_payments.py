from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.models.payments import (
    AchMandate,
    PaymentFundingSource,
    PaymentRefund,
)
from app.routers import payments as payment_routes
from app.routers import public_payments
from app.schemas.payments import FeeAllocationPatch, PrivatePlanCreate
from app.services import payment_processing
from app.services.payments import (
    PLAID_LINK_REPAIR_CODES,
    PRIVATE_FUNDER_TYPES,
    _component_client_ach_split,
    _derived_fee_collection_status,
    _mandate_is_current,
    _patch_allocation_dict,
    _patch_component_client_ach,
    _provider_failure_details,
    _provider_refund_idempotency_key,
    _refund_operation_fingerprint,
    _select_obligation_allocation,
    _servicing_authority_is_effective,
    _should_apply_transfer_status,
    _transfer_is_locally_cancellable,
    allocation_response,
    claim_due_transfers,
    fee_obligation_sha256,
    generate_schedule,
    next_banking_day,
)


class _AsyncSessionContext:
    def __init__(self, db: object) -> None:
        self.db = db

    async def __aenter__(self) -> object:
        return self.db

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Copyable(SimpleNamespace):
    def model_copy(self, *, update: dict | None = None):
        return _Copyable(**{**vars(self), **(update or {})})


@pytest.mark.asyncio
async def test_read_only_payment_summary_redacts_sensitive_ach_details() -> None:
    sensitive = _Copyable(
        funding_source=_Copyable(
            institution_name="Secret Bank",
            account_name="Operating Account",
            account_mask="1234",
            account_subtype="checking",
        ),
        mandate=_Copyable(
            payer_name="Private Signer",
            certificate_available=True,
            can_revoke=True,
            can_resend_proof=True,
            artifact={"download_url": "signed-proof"},
        ),
        debit_notice={"recipient": "private@example.com"},
        fee_agreement={
            "id": "agreement-1",
            "status": "signed",
            "current": True,
            "document_sha256": "secret-hash",
            "artifact": {"download_url": "agreement-proof"},
        },
        agreement_documents=[{"sha256": "secret"}],
        servicing_authorities=[{"destination": "secret"}],
        aggregate_status="ready",
    )
    with patch.object(
        payment_routes.pay,
        "build_summary",
        AsyncMock(return_value=sensitive),
    ):
        redacted = await payment_routes._summary(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(role=Role.BROKER),
        )
        manager = await payment_routes._summary(
            SimpleNamespace(),
            SimpleNamespace(),
            SimpleNamespace(role=Role.LOAN_EXEC),
        )

    assert redacted.aggregate_status == "ready"
    assert redacted.funding_source.institution_name is None
    assert redacted.funding_source.account_name is None
    assert redacted.funding_source.account_mask is None
    assert redacted.mandate.payer_name == "Client"
    assert redacted.mandate.artifact is None
    assert redacted.debit_notice is None
    assert redacted.fee_agreement == {
        "id": "agreement-1",
        "status": "signed",
        "template_version": None,
        "prepared_at": None,
        "sent_at": None,
        "signed_at": None,
        "countersigned_at": None,
        "proof_email_status": None,
        "current": True,
    }
    assert redacted.agreement_documents == []
    assert redacted.servicing_authorities == []
    assert manager is sensitive


def test_private_funder_registry_includes_normalized_production_values() -> None:
    assert {"private_credit", "family_office", "balance_sheet"} <= PRIVATE_FUNDER_TYPES


def test_private_schedule_authorization_remains_phase_two_disabled() -> None:
    with (
        patch.object(
            public_payments,
            "get_settings",
            return_value=SimpleNamespace(
                payments_enabled=True,
                private_funding_payments_enabled=False,
            ),
        ),
        patch.object(public_payments.plaid_transfer, "enabled", return_value=True),
        patch.object(public_payments.ach_fee_workflow, "require_legal_approval"),
    ):
        with pytest.raises(HTTPException) as error:
            public_payments._private_enabled()
    assert error.value.status_code == 503


def test_fee_allocation_accepts_dollars_and_preserves_component_split() -> None:
    payload = FeeAllocationPatch(
        collection_mode="split",
        client_ach_amount=Decimal("700.00"),
        origination_client_ach_amount=Decimal("500.00"),
        consulting_client_ach_amount=Decimal("200.00"),
        bank_direct_amount=Decimal("300.00"),
    )

    allocation = _patch_allocation_dict(payload)
    components = _patch_component_client_ach(
        payload,
        client_ach_cents=allocation["client_ach_cents"],
        origination_fee_cents=80_000,
        consulting_fee_cents=20_000,
    )

    assert allocation == {
        "client_ach_cents": 70_000,
        "bank_direct_cents": 30_000,
        "external_cents": 0,
        "deferred_cents": 0,
        "waived_cents": 0,
    }
    assert components == (50_000, 20_000)


def test_fee_allocation_accepts_legacy_cents_contract() -> None:
    payload = FeeAllocationPatch(
        client_ach_cents=12_345,
        origination_client_ach_cents=10_000,
        consulting_client_ach_cents=2_345,
        waived_cents=655,
    )

    allocation = _patch_allocation_dict(payload)
    components = _patch_component_client_ach(
        payload,
        client_ach_cents=allocation["client_ach_cents"],
        origination_fee_cents=10_000,
        consulting_fee_cents=3_000,
    )

    assert allocation["client_ach_cents"] == 12_345
    assert allocation["waived_cents"] == 655
    assert components == (10_000, 2_345)


def test_loan_exec_may_carry_forward_but_not_create_or_change_waiver() -> None:
    user = SimpleNamespace(role=Role.LOAN_EXEC)
    latest = SimpleNamespace(allocation={"waived_cents": 25_000})

    payment_routes._enforce_waiver_change_permission(
        user=user,
        latest=latest,
        requested_waived_cents=25_000,
    )

    with pytest.raises(HTTPException) as error:
        payment_routes._enforce_waiver_change_permission(
            user=user,
            latest=latest,
            requested_waived_cents=0,
        )

    assert error.value.status_code == 403


def test_super_admin_may_change_waiver() -> None:
    payment_routes._enforce_waiver_change_permission(
        user=SimpleNamespace(role=Role.SUPER_ADMIN),
        latest=SimpleNamespace(allocation={"waived_cents": 25_000}),
        requested_waived_cents=0,
    )


def test_component_ach_cannot_exceed_underlying_fee() -> None:
    with pytest.raises(HTTPException) as error:
        _component_client_ach_split(
            client_ach_cents=70_000,
            origination_fee_cents=40_000,
            consulting_fee_cents=30_000,
            origination_client_ach_cents=50_000,
            consulting_client_ach_cents=20_000,
        )

    assert error.value.status_code == 422
    assert "Origination" in str(error.value.detail)


def test_allocation_response_round_trips_exact_component_split() -> None:
    row = SimpleNamespace(
        id=uuid4(),
        version=3,
        collection_mode="split",
        gross_fee_cents=100_000,
        origination_client_ach_cents=50_000,
        consulting_client_ach_cents=20_000,
        allocation={
            "client_ach_cents": 70_000,
            "bank_direct_cents": 30_000,
            "external_cents": 0,
            "deferred_cents": 0,
            "waived_cents": 0,
        },
        updated_at=datetime.now(UTC),
    )

    response = allocation_response(row, current_gross_cents=100_000)

    assert response.origination_client_ach_amount == 500
    assert response.consulting_client_ach_amount == 200
    assert response.origination_client_ach_cents == 50_000
    assert response.consulting_client_ach_cents == 20_000
    assert response.is_balanced is True


def test_obligation_can_exclude_deferred_unearned_consulting_fee() -> None:
    allocation, origination_ach, consulting_ach = _select_obligation_allocation(
        allocation={
            "client_ach_cents": 50_000,
            "bank_direct_cents": 30_000,
            "external_cents": 0,
            "deferred_cents": 20_000,
            "waived_cents": 0,
        },
        full_origination_cents=80_000,
        full_consulting_cents=20_000,
        include_origination=True,
        include_consulting=False,
        origination_client_ach_cents=50_000,
        consulting_client_ach_cents=0,
    )

    assert allocation == {
        "client_ach_cents": 50_000,
        "bank_direct_cents": 30_000,
        "external_cents": 0,
        "deferred_cents": 0,
        "waived_cents": 0,
    }
    assert (origination_ach, consulting_ach) == (50_000, 0)


def test_obligation_cannot_exclude_consulting_fee_allocated_to_client_ach() -> None:
    with pytest.raises(HTTPException) as error:
        _select_obligation_allocation(
            allocation={
                "client_ach_cents": 70_000,
                "bank_direct_cents": 10_000,
                "external_cents": 0,
                "deferred_cents": 20_000,
                "waived_cents": 0,
            },
            full_origination_cents=80_000,
            full_consulting_cents=20_000,
            include_origination=True,
            include_consulting=False,
            origination_client_ach_cents=50_000,
            consulting_client_ach_cents=20_000,
        )

    assert error.value.status_code == 422
    assert "consulting client ACH" in str(error.value.detail)


def test_dispatch_authorization_rejects_revoked_and_expired_mandates() -> None:
    now = datetime.now(UTC)
    valid = SimpleNamespace(status="active", revoked_at=None, expires_at=now + timedelta(minutes=1))
    expired = SimpleNamespace(status="active", revoked_at=None, expires_at=now - timedelta(seconds=1))
    revoked = SimpleNamespace(status="active", revoked_at=now, expires_at=None)

    assert _mandate_is_current(valid, at=now) is True
    assert _mandate_is_current(expired, at=now) is False
    assert _mandate_is_current(revoked, at=now) is False


def test_only_pre_handoff_transfer_can_be_cancelled_locally() -> None:
    authorizing = SimpleNamespace(
        status="authorizing", plaid_transfer_id=None, submitted_at=None
    )
    submitting = SimpleNamespace(
        status="submitting", plaid_transfer_id=None, submitted_at=None
    )
    provider_backed = SimpleNamespace(
        status="pending", plaid_transfer_id="plaid-1", submitted_at=datetime.now(UTC)
    )

    assert _transfer_is_locally_cancellable(authorizing) is True
    assert _transfer_is_locally_cancellable(submitting) is False
    assert _transfer_is_locally_cancellable(provider_backed) is False


def test_transfer_lifecycle_rejects_late_terminal_regression() -> None:
    assert _should_apply_transfer_status("returned", "funds_available") is False
    assert _should_apply_transfer_status("cancelled", "pending") is False
    assert _should_apply_transfer_status("funds_available", "returned") is True
    assert _should_apply_transfer_status("posted", "pending") is False


def test_plaid_failure_reason_extracts_retry_code_and_message() -> None:
    code, message = _provider_failure_details(
        {
            "failure_reason": {
                "failure_code": "R01",
                "description": "Insufficient funds",
            }
        },
        default_code="returned",
    )

    assert code == "R01"
    assert message == "Insufficient funds"


def test_refund_provider_key_is_stable_and_within_plaid_limit() -> None:
    refund_id = uuid4()
    key = _provider_refund_idempotency_key(refund_id)

    assert key == f"qcr-{refund_id}"
    assert len(key) <= 50


def test_equal_refunds_require_distinct_operator_identities() -> None:
    transfer_id = uuid4()
    first = _refund_operation_fingerprint(
        transfer_id=transfer_id,
        amount_cents=10_000,
        reason="Approved partial refund",
        idempotency_key="refund-operation-one",
    )
    replay = _refund_operation_fingerprint(
        transfer_id=transfer_id,
        amount_cents=10_000,
        reason="  APPROVED   partial refund ",
        idempotency_key="refund-operation-one",
    )
    second = _refund_operation_fingerprint(
        transfer_id=transfer_id,
        amount_cents=10_000,
        reason="Approved partial refund",
        idempotency_key="refund-operation-two",
    )

    assert replay == first
    assert second != first


@pytest.mark.asyncio
async def test_fee_authorization_hash_ignores_mutable_lifecycle_version() -> None:
    obligation = SimpleNamespace(
        id=uuid4(),
        version=1,
        record_version=1,
        client_ach_cents=12_500,
        accepted_amount=Decimal("500000.00"),
        funded_amount=None,
        origination_points=Decimal("2.5000"),
        agreement_reference="bucket-file:agreement-1",
        agreement_sha256="a" * 64,
    )
    line = SimpleNamespace(
        line_type="origination",
        amount_cents=12_500,
        client_ach_cents=12_500,
    )
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [line]))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    signed_hash = await fee_obligation_sha256(db, obligation)
    obligation.record_version += 2  # authorized, then released/processing
    dispatch_hash = await fee_obligation_sha256(db, obligation)

    assert dispatch_hash == signed_hash


@pytest.mark.asyncio
async def test_fee_repair_link_token_keeps_fee_transfer(monkeypatch) -> None:
    profile_id = uuid4()
    obligation_id = uuid4()
    transfer_id = uuid4()
    profile = SimpleNamespace(id=profile_id)
    obligation = SimpleNamespace(
        id=obligation_id,
        status="authorized",
        client_ach_cents=10_000,
    )
    repair = SimpleNamespace(id=transfer_id, plaid_authorization_id="auth-fee")
    calls: list[tuple[str, object]] = []

    async def room(*_args, **_kwargs):
        return SimpleNamespace(), profile

    async def current_obligation(*_args, **_kwargs):
        return obligation

    async def repairable(_db, *, purpose, target_id):
        calls.append((purpose, target_id))
        return repair

    async def link_token(**kwargs):
        calls.append(("link", kwargs))
        return "link-fee"

    monkeypatch.setattr(public_payments, "_enabled", lambda: None)
    monkeypatch.setattr(public_payments, "_room", room)
    monkeypatch.setattr(public_payments.pay, "current_obligation", current_obligation)
    monkeypatch.setattr(public_payments, "_repairable_transfer", repairable)
    monkeypatch.setattr(public_payments.plaid_transfer, "create_link_token", link_token)

    result = await public_payments.public_payment_link_token(
        "room-token",
        public_payments.RoomPaymentLinkRequest(
            passcode="123456",
            owner_type="business",
            purpose="fee",
            business_account_attested=True,
        ),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    assert calls[0] == ("fee", obligation_id)
    assert result == {
        "link_token": "link-fee",
        "owner_type": "business",
        "purpose": "fee",
        "repair_mode": True,
        "exchange_required": False,
        "transfer_id": str(transfer_id),
    }


@pytest.mark.asyncio
async def test_active_private_plan_allows_repair_link_token(monkeypatch) -> None:
    profile_id = uuid4()
    plan_id = uuid4()
    transfer_id = uuid4()
    profile = SimpleNamespace(id=profile_id)
    plan = SimpleNamespace(id=plan_id, status="active")
    repair = SimpleNamespace(id=transfer_id, plaid_authorization_id="auth-private")
    calls: list[tuple[str, object]] = []

    async def room(*_args, **_kwargs):
        return SimpleNamespace(), profile

    async def current_plan(*_args, **_kwargs):
        return plan

    async def repairable(_db, *, purpose, target_id):
        calls.append((purpose, target_id))
        return repair

    async def link_token(**kwargs):
        calls.append(("link", kwargs))
        return "link-private"

    monkeypatch.setattr(public_payments, "_private_enabled", lambda: None)
    monkeypatch.setattr(public_payments, "_room", room)
    monkeypatch.setattr(public_payments, "_current_private_plan", current_plan)
    monkeypatch.setattr(public_payments, "_repairable_transfer", repairable)
    monkeypatch.setattr(public_payments.plaid_transfer, "create_link_token", link_token)

    result = await public_payments.public_payment_link_token(
        "room-token",
        public_payments.RoomPaymentLinkRequest(
            passcode="123456",
            owner_type="business",
            purpose="private_schedule",
            business_account_attested=True,
        ),
        SimpleNamespace(),
        SimpleNamespace(),
    )

    assert calls[0] == ("private_schedule", plan_id)
    assert result["repair_mode"] is True
    assert result["exchange_required"] is False
    assert result["transfer_id"] == str(transfer_id)


@pytest.mark.asyncio
async def test_claim_atomically_enters_submitting_state(monkeypatch) -> None:
    transfer = SimpleNamespace(
        id=uuid4(),
        application_profile_id=uuid4(),
        status="authorizing",
        claimed_at=None,
        amount_cents=12_345,
        ach_class="CCD",
        idempotency_key="payment-attempt-1",
        funding_source_id=uuid4(),
    )
    source = SimpleNamespace(
        plaid_account_id="account-1", access_token_ciphertext="encrypted-token"
    )
    result = SimpleNamespace(
        scalars=lambda: SimpleNamespace(all=lambda: [transfer])
    )
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    async def ready(_db, _transfer):
        return source, SimpleNamespace(), None

    monkeypatch.setattr("app.services.payments.transfer_dispatch_readiness", ready)

    claims = await claim_due_transfers(db, limit=1)

    assert len(claims) == 1
    assert transfer.status == "submitting"
    assert transfer.claimed_at is not None


@pytest.mark.asyncio
async def test_claim_reclaims_only_unresolved_submissions_older_than_stale_window() -> None:
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))
    started_at = datetime.now(UTC)

    assert await claim_due_transfers(
        db,
        stale_after=timedelta(minutes=10),
    ) == []

    statement = db.execute.await_args.args[0]
    sql = str(statement)
    parameters = statement.compile().params
    cutoffs = [value for value in parameters.values() if isinstance(value, datetime)]
    assert "payment_transfers.status = :status_1 OR" in sql
    assert "payment_transfers.status = :status_2" in sql
    assert "payment_transfers.plaid_transfer_id IS NULL" in sql
    assert "payment_transfers.submitted_at IS NULL" in sql
    assert "payment_transfers.claimed_at IS NOT NULL" in sql
    assert "payment_transfers.claimed_at <=" in sql
    assert len(cutoffs) == 1
    assert started_at - timedelta(minutes=11) < cutoffs[0] < started_at - timedelta(minutes=9)


@pytest.mark.asyncio
async def test_payment_kill_switch_prevents_claim_and_provider_handoff(monkeypatch) -> None:
    claim = AsyncMock()
    provider_context = AsyncMock()
    create_authorization = AsyncMock()
    create_transfer = AsyncMock()

    monkeypatch.setattr(
        payment_processing,
        "get_settings",
        lambda: SimpleNamespace(
            payments_enabled=False,
            private_funding_payments_enabled=True,
        ),
    )
    monkeypatch.setattr(payment_processing.plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(payment_processing.payments, "claim_due_transfers", claim)
    monkeypatch.setattr(payment_processing, "_provider_context", provider_context)
    monkeypatch.setattr(
        payment_processing.plaid_transfer, "create_authorization", create_authorization
    )
    monkeypatch.setattr(
        payment_processing.plaid_transfer, "create_transfer", create_transfer
    )

    assert await payment_processing.dispatch_payment_transfers() == 0
    claim.assert_not_awaited()
    provider_context.assert_not_awaited()
    create_authorization.assert_not_awaited()
    create_transfer.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_submission_recovers_existing_transfer_before_create(monkeypatch) -> None:
    transfer_id = uuid4()
    claim_db = SimpleNamespace(commit=AsyncMock())
    record_db = SimpleNamespace(commit=AsyncMock())
    sessions = iter([claim_db, record_db])
    context = payment_processing._ProviderContext(
        transfer_id=transfer_id,
        amount_cents=12_345,
        ach_class="ccd",
        idempotency_key="payment-attempt-1",
        attempt_no=1,
        account_id="account-1",
        access_token="access-token",
        legal_name="Example Business",
        email="owner@example.com",
        ip_address="127.0.0.1",
        user_agent="pytest",
        authorization_id="auth-1",
        fee_obligation_id=uuid4(),
        installment_id=None,
    )
    record_submission = AsyncMock()
    create_transfer = AsyncMock()

    monkeypatch.setattr(
        payment_processing,
        "get_settings",
        lambda: SimpleNamespace(
            payments_enabled=True,
            private_funding_payments_enabled=False,
        ),
    )
    monkeypatch.setattr(payment_processing.plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(
        payment_processing,
        "SessionLocal",
        lambda: _AsyncSessionContext(next(sessions)),
    )
    monkeypatch.setattr(
        payment_processing.payments,
        "claim_due_transfers",
        AsyncMock(return_value=[SimpleNamespace(transfer_id=transfer_id)]),
    )
    monkeypatch.setattr(
        payment_processing,
        "_provider_context",
        AsyncMock(return_value=context),
    )
    lookup = AsyncMock(return_value={"id": "transfer-1", "status": "pending"})
    monkeypatch.setattr(
        payment_processing.plaid_transfer,
        "get_transfer_by_authorization",
        lookup,
    )
    monkeypatch.setattr(
        payment_processing.plaid_transfer,
        "create_transfer",
        create_transfer,
    )
    monkeypatch.setattr(
        payment_processing.payments,
        "record_transfer_submission",
        record_submission,
    )

    assert await payment_processing.dispatch_payment_transfers() == 1
    lookup.assert_awaited_once_with("auth-1")
    create_transfer.assert_not_awaited()
    record_submission.assert_awaited_once_with(
        record_db,
        transfer_id=transfer_id,
        plaid_authorization_id="auth-1",
        plaid_transfer_id="transfer-1",
        provider_status="pending",
        metadata={"reconciled_by": "authorization_id_before_create"},
    )


@pytest.mark.asyncio
async def test_stale_submission_reuses_authorization_after_definite_empty_lookup(
    monkeypatch,
) -> None:
    transfer_id = uuid4()
    claim_db = SimpleNamespace(commit=AsyncMock())
    record_db = SimpleNamespace(commit=AsyncMock())
    sessions = iter([claim_db, record_db])
    context = payment_processing._ProviderContext(
        transfer_id=transfer_id,
        amount_cents=12_345,
        ach_class="ccd",
        idempotency_key="payment-attempt-1",
        attempt_no=1,
        account_id="account-1",
        access_token="access-token",
        legal_name="Example Business",
        email="owner@example.com",
        ip_address="127.0.0.1",
        user_agent="pytest",
        authorization_id="auth-1",
        fee_obligation_id=uuid4(),
        installment_id=None,
    )
    record_submission = AsyncMock()
    create_transfer = AsyncMock(return_value={"id": "transfer-1", "status": "pending"})

    monkeypatch.setattr(
        payment_processing,
        "get_settings",
        lambda: SimpleNamespace(
            payments_enabled=True,
            private_funding_payments_enabled=False,
        ),
    )
    monkeypatch.setattr(payment_processing.plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(
        payment_processing,
        "SessionLocal",
        lambda: _AsyncSessionContext(next(sessions)),
    )
    monkeypatch.setattr(
        payment_processing.payments,
        "claim_due_transfers",
        AsyncMock(return_value=[SimpleNamespace(transfer_id=transfer_id)]),
    )
    monkeypatch.setattr(
        payment_processing,
        "_provider_context",
        AsyncMock(return_value=context),
    )
    lookup = AsyncMock(return_value=None)
    monkeypatch.setattr(
        payment_processing.plaid_transfer,
        "get_transfer_by_authorization",
        lookup,
    )
    monkeypatch.setattr(
        payment_processing.plaid_transfer,
        "create_transfer",
        create_transfer,
    )
    monkeypatch.setattr(
        payment_processing.payments,
        "record_transfer_submission",
        record_submission,
    )

    assert await payment_processing.dispatch_payment_transfers() == 1
    lookup.assert_awaited_once_with("auth-1")
    assert create_transfer.await_args.kwargs["authorization_id"] == "auth-1"
    record_submission.assert_awaited_once()


@pytest.mark.asyncio
async def test_private_switch_excludes_installments_before_claim(monkeypatch) -> None:
    db = SimpleNamespace(commit=AsyncMock())
    claim = AsyncMock(return_value=[])
    monkeypatch.setattr(
        payment_processing,
        "get_settings",
        lambda: SimpleNamespace(
            payments_enabled=True,
            private_funding_payments_enabled=False,
        ),
    )
    monkeypatch.setattr(payment_processing.plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(payment_processing.payments, "claim_due_transfers", claim)
    monkeypatch.setattr(
        payment_processing, "SessionLocal", lambda: _AsyncSessionContext(db)
    )

    assert await payment_processing.dispatch_payment_transfers() == 0
    claim.assert_awaited_once_with(db, limit=25, include_private=False)


@pytest.mark.asyncio
async def test_refund_dispatch_recovers_only_stale_submitting_intents(monkeypatch) -> None:
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(execute=AsyncMock(return_value=result), commit=AsyncMock())
    monkeypatch.setattr(
        payment_processing,
        "get_settings",
        lambda: SimpleNamespace(
            payments_enabled=True,
            payment_refunds_enabled=True,
        ),
    )
    monkeypatch.setattr(payment_processing.plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(
        payment_processing, "SessionLocal", lambda: _AsyncSessionContext(db)
    )

    assert await payment_processing.dispatch_payment_refunds() == 0

    query = db.execute.await_args.args[0]
    statement = str(query)
    statuses = {
        value
        for value in query.compile().params.values()
        if isinstance(value, str)
    }
    assert {"pending", "checking_balance", "submitting"} <= statuses
    assert "payment_refunds.plaid_refund_id IS NULL" in statement
    assert "payment_refunds.updated_at <=" in statement


@pytest.mark.asyncio
async def test_refund_dispatch_blocks_before_create_when_ledger_is_short(
    monkeypatch,
) -> None:
    refund_id = uuid4()
    transfer_id = uuid4()
    refund = SimpleNamespace(
        id=refund_id,
        transfer_id=transfer_id,
        status="pending",
        plaid_refund_id=None,
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
        amount_cents=15_000,
        provider_idempotency_key="refund-key",
        completed_at=None,
    )
    transfer = SimpleNamespace(
        id=transfer_id,
        installment_id=None,
        status="funds_available",
        plaid_transfer_id="transfer-1",
    )
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [refund]))

    async def get_row(model, _row_id, **_kwargs):
        return refund if model is PaymentRefund else transfer

    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(side_effect=get_row),
        commit=AsyncMock(),
    )
    create_refund = AsyncMock()
    monkeypatch.setattr(
        payment_processing,
        "get_settings",
        lambda: SimpleNamespace(
            payments_enabled=True,
            payment_refunds_enabled=True,
        ),
    )
    monkeypatch.setattr(payment_processing.plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(
        payment_processing, "SessionLocal", lambda: _AsyncSessionContext(db)
    )
    monkeypatch.setattr(
        payment_processing.plaid_transfer,
        "get_transfer",
        AsyncMock(
            return_value={
                "id": "transfer-1",
                "ledger_id": "ledger-1",
                "originator_client_id": "originator-1",
            }
        ),
    )
    monkeypatch.setattr(
        payment_processing.plaid_transfer,
        "get_ledger_available_balance",
        AsyncMock(return_value=Decimal("149.99")),
    )
    monkeypatch.setattr(
        payment_processing.plaid_transfer, "create_refund", create_refund
    )

    assert await payment_processing.dispatch_payment_refunds() == 0
    assert refund.status == "action_required"
    create_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_refund_guard_read_error_stays_before_money_movement(monkeypatch) -> None:
    refund_id = uuid4()
    transfer_id = uuid4()
    refund = SimpleNamespace(
        id=refund_id,
        transfer_id=transfer_id,
        status="pending",
        plaid_refund_id=None,
        updated_at=datetime.now(UTC) - timedelta(minutes=20),
        amount_cents=15_000,
        provider_idempotency_key="refund-key",
        completed_at=None,
    )
    transfer = SimpleNamespace(
        id=transfer_id,
        installment_id=None,
        status="funds_available",
        plaid_transfer_id="transfer-1",
    )
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [refund]))

    async def get_row(model, _row_id, **_kwargs):
        return refund if model is PaymentRefund else transfer

    db = SimpleNamespace(
        execute=AsyncMock(return_value=result),
        get=AsyncMock(side_effect=get_row),
        commit=AsyncMock(),
    )
    create_refund = AsyncMock()
    monkeypatch.setattr(
        payment_processing,
        "get_settings",
        lambda: SimpleNamespace(
            payments_enabled=True,
            payment_refunds_enabled=True,
        ),
    )
    monkeypatch.setattr(payment_processing.plaid_transfer, "enabled", lambda: True)
    monkeypatch.setattr(
        payment_processing, "SessionLocal", lambda: _AsyncSessionContext(db)
    )
    monkeypatch.setattr(
        payment_processing.plaid_transfer,
        "get_transfer",
        AsyncMock(
            side_effect=payment_processing.plaid_transfer.PlaidTransferError(
                "read timed out",
                code="PLAID_NETWORK_UNCERTAIN",
                retryable=True,
            )
        ),
    )
    monkeypatch.setattr(
        payment_processing.plaid_transfer, "create_refund", create_refund
    )

    assert await payment_processing.dispatch_payment_refunds() == 0
    assert refund.status == "checking_balance"
    create_refund.assert_not_awaited()


@pytest.mark.asyncio
async def test_claim_query_excludes_private_installments_when_disabled() -> None:
    result = SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    db = SimpleNamespace(execute=AsyncMock(return_value=result))

    assert await claim_due_transfers(db, include_private=False) == []

    statement = str(db.execute.await_args.args[0])
    assert "payment_transfers.installment_id IS NULL" in statement
    assert "payment_transfers.status = :status_1 OR" in statement
    assert "payment_transfers.claimed_at <=" in statement


def test_mixed_source_collection_status_requires_every_collectible_dollar() -> None:
    obligation = SimpleNamespace(
        gross_fee_cents=100_000,
        deferred_cents=0,
        waived_cents=0,
        status="processing",
    )

    assert _derived_fee_collection_status(
        obligation,
        ach_collected_cents=70_000,
        receipt_collected_cents=0,
        has_processing_transfer=False,
        has_returned_transfer=False,
    ) == "partially_collected"
    assert _derived_fee_collection_status(
        obligation,
        ach_collected_cents=70_000,
        receipt_collected_cents=30_000,
        has_processing_transfer=False,
        has_returned_transfer=False,
    ) == "collected"


def test_returned_ach_preserves_other_collections_and_actionable_empty_state() -> None:
    obligation = SimpleNamespace(
        gross_fee_cents=100_000,
        deferred_cents=0,
        waived_cents=0,
        status="collected",
    )

    assert _derived_fee_collection_status(
        obligation,
        ach_collected_cents=0,
        receipt_collected_cents=30_000,
        has_processing_transfer=False,
        has_returned_transfer=True,
    ) == "partially_collected"
    assert _derived_fee_collection_status(
        obligation,
        ach_collected_cents=0,
        receipt_collected_cents=0,
        has_processing_transfer=False,
        has_returned_transfer=True,
    ) == "returned"


def test_deferred_and_waived_amounts_are_not_required_for_collection() -> None:
    obligation = SimpleNamespace(
        gross_fee_cents=120_000,
        deferred_cents=10_000,
        waived_cents=10_000,
        status="partially_collected",
    )

    assert _derived_fee_collection_status(
        obligation,
        ach_collected_cents=70_000,
        receipt_collected_cents=30_000,
        has_processing_transfer=False,
        has_returned_transfer=False,
    ) == "collected"


def test_servicing_authority_must_match_profile_and_effective_date() -> None:
    profile_id = uuid4()
    authority = SimpleNamespace(
        application_profile_id=profile_id,
        status="active",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
    )

    assert _servicing_authority_is_effective(
        authority, profile_id=profile_id, on_date=date(2026, 6, 1)
    ) is True
    assert _servicing_authority_is_effective(
        authority, profile_id=uuid4(), on_date=date(2026, 6, 1)
    ) is False
    assert _servicing_authority_is_effective(
        authority, profile_id=profile_id, on_date=date(2027, 1, 1)
    ) is False


def test_fixed_schedule_skips_bank_holiday_and_absorbs_rounding() -> None:
    payload = PrivatePlanCreate(
        creditor_name="QC Private Capital",
        cadence="business_daily",
        total_amount_cents=10_001,
        installment_count=3,
        first_due_date=date(2027, 7, 3),
        agreement_reference="executed-agreement-1",
    )

    schedule = generate_schedule(payload)

    assert [item[0] for item in schedule] == [
        date(2027, 7, 6),
        date(2027, 7, 7),
        date(2027, 7, 8),
    ]
    assert [item[1] for item in schedule] == [3333, 3333, 3335]
    assert sum(item[1] for item in schedule) == 10_001


def test_next_banking_day_includes_following_year_observed_holiday() -> None:
    # January 1, 2022 fell on Saturday, so the Federal Reserve observed it on
    # Friday December 31, 2021. Looking only at the candidate calendar year
    # incorrectly treated that Friday as available.
    assert next_banking_day(date(2021, 12, 31)) == date(2022, 1, 3)


def test_payment_source_reconnect_uses_versioned_current_partial_index() -> None:
    indexes = {index.name: index for index in PaymentFundingSource.__table__.indexes}
    current = indexes["uq_payment_funding_sources_current_account"]

    assert current.unique is True
    assert [column.name for column in current.columns] == ["application_profile_id"]
    predicate = str(current.dialect_options["postgresql"]["where"])
    assert "revoked_at IS NULL" in predicate
    assert "status = 'verified'" in predicate


def test_mandate_snapshots_payment_account_evidence() -> None:
    assert "funding_source_snapshot" in AchMandate.__table__.columns
    assert "funding_source_sha256" in AchMandate.__table__.columns


def test_bank_repair_codes_are_narrow_and_do_not_include_network_failures() -> None:
    assert "ITEM_LOGIN_REQUIRED" in PLAID_LINK_REPAIR_CODES
    assert "PLAID_NETWORK_UNCERTAIN" not in PLAID_LINK_REPAIR_CODES
