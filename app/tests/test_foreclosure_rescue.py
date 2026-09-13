from datetime import date, timedelta
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.routers.foreclosure_rescue import ForeclosureRescueIntakeCreate, RescueDocumentStatusUpdate
from app.services.foreclosure_rescue import (
    AMORTIZATION_MONTHS,
    NOTE_RATE_PCT,
    TERM_MONTHS,
    balloon_balance,
    monthly_principal_and_interest,
    urgency_for,
    validate_term_sheet_note_rate,
)


def _intake(**changes):
    values = {
        "submitter_type": "attorney",
        "contact_name": "A. Counsel",
        "firm_name": "Counsel LLP",
        "contact_phone": "9735550100",
        "contact_email": "counsel@example.com",
        "borrower_name": "Owner Guarantor",
        "client_email": "client@example.com",
        "client_phone": "9735550111",
        "holding_entity": "123 Main LLC",
        "ownership_structure": "Owner Guarantor owns 100%",
        "property_addresses": ["123 Main St, Newark, NJ"],
        "parcel_building_count": 1,
        "property_type": "mixed_use",
        "occupancy_rate_pct": 85,
        "estimated_market_value": 2_000_000,
        "senior_lender": "Example Bank",
        "payoff_balance": 1_100_000,
        "legal_statuses": ["notice_of_default"],
        "has_other_liens_or_back_taxes": False,
        "requested_loan_amount": 1_100_000,
        "exit_strategy": "conventional_refinance",
        "authority_attested": True,
        "terms_accepted": True,
        "privacy_accepted": True,
    }
    values.update(changes)
    return ForeclosureRescueIntakeCreate.model_validate(values)


def test_program_terms_and_amortization_fixture():
    assert NOTE_RATE_PCT == Decimal("12.99")
    assert TERM_MONTHS == 24
    assert AMORTIZATION_MONTHS == 480
    assert monthly_principal_and_interest(1_000_000) == Decimal("10887.01")
    assert balloon_balance(1_000_000) == Decimal("998310.99")
    validate_term_sheet_note_rate("12.99")
    with pytest.raises(ValueError):
        validate_term_sheet_note_rate("13.00")


def test_urgency_bands_and_missing_date_warning():
    today = date.today()
    assert urgency_for(today + timedelta(days=7))["key"] == "critical"
    assert urgency_for(today + timedelta(days=8))["key"] == "urgent"
    assert urgency_for(today + timedelta(days=15))["key"] == "time_sensitive"
    assert urgency_for(today + timedelta(days=31))["key"] == "standard"
    assert urgency_for(today - timedelta(days=1))["warning"] == "Sale date has passed — confirm the current deadline"
    assert urgency_for(None)["warning"] == "Sale date not provided"


def test_professional_submitter_requires_client_contacts_but_owner_does_not():
    with pytest.raises(ValidationError):
        _intake(client_email=None)
    owner = _intake(submitter_type="owner_direct", client_email=None, client_phone=None, owner_contact_consent=True)
    assert owner.submitter_type == "owner_direct"


def test_scheduled_sale_requires_date_and_waiver_requires_reason():
    with pytest.raises(ValidationError):
        _intake(legal_statuses=["auction_sale_scheduled"], scheduled_sale_date=None)
    with pytest.raises(ValidationError):
        RescueDocumentStatusUpdate(status="waived")
    assert RescueDocumentStatusUpdate(status="not_applicable", reason="No guarantor").reason == "No guarantor"
