from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.enums import Role
from app.routers import production_packages as routes
from app.schemas.production_package import ProductionTermSheetEmailRequest
from app.services import production_term_sheets as sheet_service
from app.services.production_term_sheet_pdf import filename_for, render_term_sheet_html


def _sheet(**overrides):
    values = {
        "id": uuid.uuid4(),
        "version": 3,
        "status": "current",
        "funding_party_kind": "Lender",
        "funding_party_name": "Northstar Capital",
        "facility_type": "Term advance",
        "approved_amount": 500_000,
        "min_activation_amount": 400_000,
        "rate_pct": 35,
        "term_months": 12,
        "monthly_debt_service": 50_100,
        "debt_service_is_level_payment": True,
        "expected_funding_date": date(2026, 9, 22),
        "activation_date": date(2026, 9, 22),
        "commencement_date": date(2026, 10, 1),
        "maturity_date": date(2027, 9, 22),
        "use_of_funds": {"inventory": 300_000, "working_capital": 200_000},
        "conditions": "Final bank verification.\nSatisfactory closing documents.",
        "notes": "INTERNAL: do not show this client note",
        "entered_at": datetime(2026, 9, 15, 20, 14, tzinfo=UTC),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4(), role=Role.LOAN_EXEC, name="Underwriter", email="uw@example.com")


def _access(sheet=None) -> SimpleNamespace:
    profile = SimpleNamespace(id=uuid.uuid4(), dealer_id=uuid.uuid4(), intake_id=uuid.uuid4())
    package = SimpleNamespace(id=uuid.uuid4(), sponsor_company_id=None)
    return SimpleNamespace(profile=profile, package=package, term_sheet=sheet or _sheet())


def test_client_loan_terms_html_is_branded_exact_and_client_safe() -> None:
    sheet = _sheet()

    html = render_term_sheet_html(
        sheet,
        business_name="Watertown Motors LLC",
        client_name="Jonathan Franco",
        sponsor_name="UrChoice",
    )

    assert "Qualified Commercial" in html
    assert "data:image/png;base64," in html
    assert "Conditional Financing Terms" in html
    assert "$500,000.00" in html
    assert "35.00%" in html
    assert "12 months" in html
    assert "Northstar Capital" in html
    assert "Jonathan Franco" in html
    assert "Final bank verification." in html
    assert "INTERNAL: do not show this client note" not in html
    assert filename_for(sheet, "Watertown Motors LLC") == "Watertown-Motors-LLC-Loan-Terms-v3.pdf"
    assert filename_for(sheet, "株式会社") == "Client-Loan-Terms-v3.pdf"


def test_term_email_payload_deduplicates_explicit_recipients() -> None:
    payload = ProductionTermSheetEmailRequest(
        expected_version=3,
        delivery_key=uuid.uuid4(),
        to_emails=["Client@Example.com", "client@example.com"],
        cc_emails=["Desk@example.com", "desk@example.com"],
        subject="Your loan terms",
        body="Please review the attached terms.",
    )

    assert [str(value).lower() for value in payload.to_emails] == ["client@example.com"]
    assert [str(value).lower() for value in payload.cc_emails] == ["desk@example.com"]


def test_stale_sheet_version_is_rejected() -> None:
    with pytest.raises(HTTPException) as exc:
        routes._require_sheet_version(_sheet(version=4), 3, "opening the PDF")

    assert exc.value.status_code == 409
    assert "newer loan-terms version" in str(exc.value.detail)


def test_client_term_artifacts_require_an_underwriting_role() -> None:
    user = SimpleNamespace(role=Role.DEALER_PARTNER)

    with pytest.raises(HTTPException) as exc:
        sheet_service.require_term_role(user)

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_client_pdf_route_returns_inline_pdf_and_audits() -> None:
    sheet = _sheet()
    access = _access(sheet)
    user = _user()
    db = SimpleNamespace(commit=AsyncMock())
    log = AsyncMock()

    with (
        patch.object(routes.sheets_svc, "require_term_role"),
        patch.object(routes, "_profile_access", AsyncMock(return_value=access)),
        patch.object(routes, "_locked_current_sheet", AsyncMock(return_value=sheet)),
        patch.object(routes, "_render_client_term_pdf", AsyncMock(return_value=(b"%PDF-test", "Terms.pdf"))),
        patch.object(routes.profiles, "log_profile_action", log),
    ):
        response = await routes.client_term_sheet_pdf(
            access.profile.id,
            3,
            user,
            disposition="inline",
            db=db,
        )

    assert response.body == b"%PDF-test"
    assert response.media_type == "application/pdf"
    assert response.headers["content-disposition"] == 'inline; filename="Terms.pdf"'
    assert response.headers["cache-control"] == "private, no-store"
    log.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_suppressed_dealer_file_cannot_email_terms() -> None:
    sheet = _sheet()
    access = _access(sheet)
    user = _user()
    db = SimpleNamespace()
    payload = ProductionTermSheetEmailRequest(
        expected_version=3,
        delivery_key=uuid.uuid4(),
        to_emails=["client@example.com"],
        subject="Your loan terms",
        body="Please review the attached terms.",
    )

    with (
        patch.object(routes.sheets_svc, "require_term_role"),
        patch.object(routes, "_profile_access", AsyncMock(return_value=access)),
        patch.object(routes, "_locked_current_sheet", AsyncMock(return_value=sheet)),
        patch.object(
            routes.file_contacts,
            "load_sources",
            AsyncMock(return_value=SimpleNamespace(intake=SimpleNamespace(client_contact_suppressed=True))),
        ),
        patch.object(routes, "send_as_user", AsyncMock()) as send,
    ):
        with pytest.raises(HTTPException) as exc:
            await routes.email_client_term_sheet(access.profile.id, payload, user, db=db)

    assert exc.value.status_code == 409
    assert "suppressed" in str(exc.value.detail)
    send.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_terms_claims_delivery_before_send_and_attaches_exact_pdf() -> None:
    sheet = _sheet()
    access = _access(sheet)
    user = _user()
    db = SimpleNamespace(commit=AsyncMock())
    payload = ProductionTermSheetEmailRequest(
        expected_version=3,
        delivery_key=uuid.uuid4(),
        to_emails=["client@example.com"],
        cc_emails=["closings@example.com"],
        subject="Your loan terms",
        body="Please review the attached terms.",
    )
    audit = AsyncMock()
    send = AsyncMock(return_value=SimpleNamespace(ok=True, message_id="gmail-123", detail="sent_gmail"))

    with (
        patch.object(routes.sheets_svc, "require_term_role"),
        patch.object(routes, "_profile_access", AsyncMock(return_value=access)),
        patch.object(routes, "_locked_current_sheet", AsyncMock(return_value=sheet)),
        patch.object(
            routes.file_contacts,
            "load_sources",
            AsyncMock(return_value=SimpleNamespace(intake=SimpleNamespace(client_contact_suppressed=False))),
        ),
        patch.object(routes, "_email_delivery_audit", AsyncMock(return_value=[])),
        patch.object(routes, "_render_client_term_pdf", AsyncMock(return_value=(b"%PDF-exact", "Terms-v3.pdf"))),
        patch.object(routes.profiles, "log_profile_action", audit),
        patch.object(routes, "send_as_user", send),
    ):
        result = await routes.email_client_term_sheet(access.profile.id, payload, user, db=db)

    assert result.sent is True
    assert result.filename == "Terms-v3.pdf"
    assert result.message_id == "gmail-123"
    assert db.commit.await_count == 2
    assert audit.await_count == 2
    assert audit.await_args_list[0].args[3] == "production_term_sheet.email_queued"
    assert audit.await_args_list[1].args[3] == "production_term_sheet.emailed"
    assert send.await_args.kwargs["to_emails"] == ["client@example.com"]
    assert send.await_args.kwargs["cc_emails"] == ["closings@example.com"]
    assert send.await_args.kwargs["attachments"] == [("Terms-v3.pdf", b"%PDF-exact", "application/pdf")]


@pytest.mark.asyncio
async def test_repeated_sent_delivery_key_returns_audit_result_without_resending() -> None:
    sheet = _sheet()
    access = _access(sheet)
    user = _user()
    db = SimpleNamespace()
    payload = ProductionTermSheetEmailRequest(
        expected_version=3,
        delivery_key=uuid.uuid4(),
        to_emails=["client@example.com"],
        subject="Your loan terms",
        body="Please review the attached terms.",
    )
    prior = SimpleNamespace(
        entity_id=sheet.id,
        action="production_term_sheet.emailed",
        after={"filename": "Terms-v3.pdf", "message_id": "gmail-123", "provider": "sent_gmail"},
    )

    with (
        patch.object(routes.sheets_svc, "require_term_role"),
        patch.object(routes, "_profile_access", AsyncMock(return_value=access)),
        patch.object(routes, "_locked_current_sheet", AsyncMock(return_value=sheet)),
        patch.object(
            routes.file_contacts,
            "load_sources",
            AsyncMock(return_value=SimpleNamespace(intake=SimpleNamespace(client_contact_suppressed=False))),
        ),
        patch.object(routes, "_email_delivery_audit", AsyncMock(return_value=[prior])),
        patch.object(routes, "_render_client_term_pdf", AsyncMock()) as render,
        patch.object(routes, "send_as_user", AsyncMock()) as send,
    ):
        result = await routes.email_client_term_sheet(access.profile.id, payload, user, db=db)

    assert result.sent is True
    assert result.filename == "Terms-v3.pdf"
    assert result.message_id == "gmail-123"
    render.assert_not_awaited()
    send.assert_not_awaited()
