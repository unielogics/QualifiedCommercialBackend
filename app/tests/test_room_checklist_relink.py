"""Linking what the client already gave to what the application needs.

Body Good Studio LLC uploaded six statements and signed the credit
authorization in its room, and the application still showed every ask open:
the playbook had minted its own rows beside the room's checklist, and the
June statement (a cycle of May 30 to June 30) had been filed under May.
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.routers.buckets import request_link_choose_passcode
from app.routers.dealer_ai_intake import DealerFileUploadInit
from app.schemas.bucket import BucketRequestPasscodeChange
from app.services.application_programs import _requested_document, adoptable_room_request
from app.services.bucket_evidence import (
    statement_months_from_analysis,
    statement_months_from_filename,
    statement_period_month,
)

# --- the month a statement is filed under ------------------------------------


def test_a_cycle_that_crosses_a_month_boundary_is_filed_under_its_closing_month() -> None:
    assert statement_period_month("2026-05-30 to 2026-06-30") == "2026-06"
    assert statement_period_month("2026-05-01 to 2026-05-29") == "2026-05"
    assert statement_period_month("05/30/2026 - 06/30/2026") == "2026-06"
    assert statement_period_month("May 30, 2026 through June 30, 2026") == "2026-06"


def test_a_single_month_or_nothing_reads_as_itself() -> None:
    assert statement_period_month("2026-07") == "2026-07"
    assert statement_period_month("July 2026") == "2026-07"
    assert statement_period_month(None) is None
    assert statement_period_month("no dates here") is None


def test_analysis_months_use_the_closing_month_rule() -> None:
    assert statement_months_from_analysis({"key_facts": {"statement_period": "2026-05-30 to 2026-06-30"}}) == {"2026-06"}
    assert statement_months_from_analysis(
        {"key_facts": {"months": [{"statement_period": "2026-07-01 to 2026-07-31"}, {"start_date": "2026-08-01"}]}}
    ) == {"2026-07", "2026-08"}
    assert statement_months_from_analysis({"key_facts": {}}) == set()


def test_bank_export_names_without_separators_name_their_closing_month() -> None:
    assert statement_months_from_filename("20260630-statements-1204-.pdf") == {"2026-06"}
    assert statement_months_from_filename("eStmt_2026-01-30 (8).pdf") == {"2026-01"}


# --- the playbook adopts the room's own ask -----------------------------------


def _requirement(key: str, label: str) -> SimpleNamespace:
    return SimpleNamespace(
        requirement_key=key,
        requirement_kind="document",
        canonical_key=None,
        label=label,
        category="financials",
        playbook_id=uuid4(),
        visibility=["borrower", "underwriter"],
        collection_config={},
        objective_text=f"Collect {label}",
        ai_request_message_template=None,
        required_level="required",
    )


def _room_row(name: str, category: str, **overrides) -> SimpleNamespace:
    row = SimpleNamespace(
        id=uuid4(),
        name=name,
        category=category,
        description=f"Upload {name}",
        required=True,
        allow_multiple_files=True,
        requires_signature=False,
        signature_kind=None,
        status="requested",
        requirement_key=None,
        requirement_source=None,
        created_at=datetime(2026, 11, 3, 1, 21, tzinfo=UTC),
    )
    for name_, value in overrides.items():
        setattr(row, name_, value)
    return row


def _db(*results):
    return SimpleNamespace(execute=AsyncMock(side_effect=list(results)), flush=AsyncMock(), add=Mock())


EMPTY = SimpleNamespace(scalar_one_or_none=lambda: None)


def _found(row):
    return SimpleNamespace(scalar_one_or_none=lambda: row)


def _candidates(*rows):
    return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: list(rows)))


@pytest.mark.asyncio
async def test_the_playbook_adopts_the_rooms_bank_statement_ask_instead_of_minting_a_second() -> None:
    room_row = _room_row("Last 6 months bank statements", "Bank Statements", status="uploaded")
    db = _db(EMPTY, EMPTY, _candidates(room_row))

    result = await _requested_document(
        db,
        SimpleNamespace(primary_bucket_id=uuid4()),
        _requirement("business_bank_statements_6_months", "Last 6 months business bank statements"),
        [],
        ["mca_baseline"],
    )

    assert result is room_row
    assert room_row.requirement_key == "business_bank_statements_6_months"
    assert room_row.requirement_source["kind"] == "program_readiness"
    assert room_row.requirement_source["adopted"] is True
    assert room_row.requirement_source["policy_keys"] == ["mca_baseline"]
    # The client has already read this wording; it stays.
    assert room_row.name == "Last 6 months bank statements"
    assert room_row.status == "uploaded"
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_a_signature_requirement_adopts_the_row_the_client_signs() -> None:
    credit = _room_row(
        "Credit Report Authorization",
        "Compliance",
        requires_signature=True,
        signature_kind="credit_authorization",
        allow_multiple_files=False,
        status="uploaded",
    )
    db = _db(EMPTY, EMPTY, _candidates(credit))

    result = await _requested_document(
        db,
        SimpleNamespace(primary_bucket_id=uuid4()),
        _requirement("signed_credit_authorization", "Signed credit authorization"),
        [],
        ["mca_baseline"],
    )

    assert result is credit
    assert credit.requirement_key == "signed_credit_authorization"
    assert credit.name == "Credit Report Authorization"
    assert credit.allow_multiple_files is False
    assert credit.requires_signature is True


@pytest.mark.asyncio
async def test_a_room_with_no_matching_ask_still_gets_the_playbooks_own_row() -> None:
    unrelated = _room_row("Lease, rent roll, or rent support", "Property income")
    db = _db(EMPTY, EMPTY, _candidates(unrelated))

    result = await _requested_document(
        db,
        SimpleNamespace(primary_bucket_id=uuid4()),
        _requirement("business_tax_returns_2_years", "Last 2 years business tax returns"),
        [],
        ["business_baseline"],
    )

    assert result is not unrelated
    assert result.name == "Last 2 years business tax returns"
    assert result.requirement_source.get("adopted") is None
    db.add.assert_called_once_with(result)


@pytest.mark.asyncio
async def test_a_row_the_playbook_minted_is_still_kept_in_step_with_the_playbook() -> None:
    minted = _room_row(
        "Old label",
        "financials",
        requirement_key="business_debt_schedule",
        requirement_source={"kind": "program_readiness", "policy_keys": ["business_baseline"]},
    )
    db = _db(_found(minted))

    result = await _requested_document(
        db,
        SimpleNamespace(primary_bucket_id=uuid4()),
        _requirement("business_debt_schedule", "Business debt schedule"),
        [],
        ["business_baseline"],
    )

    assert result is minted
    assert minted.name == "Business debt schedule"
    assert minted.requirement_source.get("adopted") is None


@pytest.mark.asyncio
async def test_an_adopted_row_keeps_its_wording_on_every_later_pass() -> None:
    adopted = _room_row(
        "Debt schedule",
        "Debts",
        requirement_key="business_debt_schedule",
        requirement_source={"kind": "program_readiness", "adopted": True},
    )
    db = _db(_found(adopted))

    await _requested_document(
        db,
        SimpleNamespace(primary_bucket_id=uuid4()),
        _requirement("business_debt_schedule", "Business debt schedule"),
        [],
        ["business_baseline"],
    )

    assert adopted.name == "Debt schedule"
    assert adopted.requirement_source["adopted"] is True


def test_adoption_prefers_the_row_already_holding_files_and_never_a_signature_row() -> None:
    older = _room_row("Last 3 months bank statements", "Bank Statements")
    newer = _room_row("Last 6 months bank statements", "Bank Statements", created_at=datetime(2026, 11, 4, tzinfo=UTC))
    signed = _room_row("Bank statement attestation", "Compliance", requires_signature=True, signature_kind="attestation")

    chosen = adoptable_room_request(
        [signed, older, newer],
        requirement_key="business_bank_statements_6_months",
        expected={"bank_statement"},
        file_counts={newer.id: 6},
    )
    assert chosen is newer
    assert adoptable_room_request(
        [signed, older, newer], requirement_key="business_bank_statements_6_months", expected={"bank_statement"}
    ) is older
    assert adoptable_room_request([signed], requirement_key="business_bank_statements_6_months", expected={"bank_statement"}) is None
    assert adoptable_room_request([older], requirement_key="owner_personal_financial_statement", expected={"personal_financial_statement"}) is None


# --- the intake upload size -----------------------------------------------------


def test_the_intake_upload_accepts_the_size_under_either_name() -> None:
    assert DealerFileUploadInit(file_name="a.pdf", size=123).size_bytes == 123
    assert DealerFileUploadInit(file_name="a.pdf", size_bytes=5, size=9).size_bytes == 5
    assert DealerFileUploadInit(file_name="a.pdf").size_bytes == 0


# --- the client chooses their own room PIN --------------------------------------


def _link():
    return SimpleNamespace(
        id=uuid4(),
        bucket_id=uuid4(),
        passcode_hash="hash",
        recipient_name="Linda Louis",
        recipient_email="linda@example.com",
        passcode_set_by_client_at=None,
    )


@pytest.mark.asyncio
async def test_a_client_replaces_the_generated_pin_with_their_own() -> None:
    link = _link()
    db = SimpleNamespace(commit=AsyncMock())
    with (
        patch("app.routers.buckets._load_upload_link_or_404", AsyncMock(return_value=link)),
        patch("app.routers.buckets._client_ip", return_value="203.0.113.6"),
        patch("app.routers.buckets._verify_passcode", return_value=True),
        patch("app.routers.buckets._log", AsyncMock()) as audit,
        patch("app.routers.buckets.client_room.set_passcode", AsyncMock()) as set_passcode,
    ):
        result = await request_link_choose_passcode(
            "token",
            BucketRequestPasscodeChange(passcode="402913", new_passcode="738261"),
            SimpleNamespace(),
            db,
        )

    assert result.passcode_needs_setup is False
    set_passcode.assert_awaited_once_with(db, link, "738261")
    assert audit.await_args.args[2] == "room_passcode_chosen"
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_trivial_or_unchanged_pin_is_refused_before_anything_is_stored() -> None:
    link = _link()
    db = SimpleNamespace(commit=AsyncMock())
    for new_passcode in ("111111", "123456", "402913"):
        with (
            patch("app.routers.buckets._load_upload_link_or_404", AsyncMock(return_value=link)),
            patch("app.routers.buckets._client_ip", return_value="203.0.113.6"),
            patch("app.routers.buckets._verify_passcode", return_value=True),
            patch("app.routers.buckets._log", AsyncMock()),
            patch("app.routers.buckets.client_room.set_passcode", AsyncMock()) as set_passcode,
            pytest.raises(HTTPException) as error,
        ):
            await request_link_choose_passcode(
                "token",
                BucketRequestPasscodeChange(passcode="402913", new_passcode=new_passcode),
                SimpleNamespace(),
                db,
            )
        assert error.value.status_code == 422
        set_passcode.assert_not_awaited()
    db.commit.assert_not_awaited()
