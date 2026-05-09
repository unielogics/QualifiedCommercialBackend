"""Schema-level tests for the SmartIntake redesign.

Pure-Python — no DB. Guards the wire shape between the desktop
SmartIntakeModal and `/intake` so future schema edits don't silently
break the wizard's payload.
"""

from __future__ import annotations

import pytest

from app.enums import LoanPurpose
from app.schemas.intake import (
    AssetStep,
    IntakeDocumentOverrides,
    NumbersStep,
    SmartIntakePayload,
)


def test_numbers_step_accepts_purchase_purpose() -> None:
    n = NumbersStep.model_validate({
        "type": "dscr",
        "purpose": "purchase",
        "amount": 365_000,
        "ltv": 0.75,
        "base_rate": 7.5,
    })
    assert n.purpose is LoanPurpose.PURCHASE


def test_numbers_step_accepts_refinance_purpose() -> None:
    n = NumbersStep.model_validate({
        "type": "dscr",
        "purpose": "cash_out_refi",
        "amount": 250_000,
        "ltv": 0.7,
        "base_rate": 7.875,
    })
    assert n.purpose is LoanPurpose.CASH_OUT_REFI


def test_numbers_step_purpose_optional_for_back_compat() -> None:
    """Old clients that haven't been redeployed still post without `purpose`.
    The field is nullable on the schema so the request validates."""
    n = NumbersStep.model_validate({
        "type": "dscr",
        "amount": 100_000,
        "ltv": 0.75,
        "base_rate": 7.5,
    })
    assert n.purpose is None


def test_asset_step_validates_with_only_address_and_property_type() -> None:
    """Phase B strips sqft / annual_taxes / annual_insurance from the
    SmartIntakeModal Step 2. AssetStep must validate without them."""
    a = AssetStep.model_validate({
        "address": "123 Main St",
        "city": "Brooklyn",
        "state": "NY",
        "property_type": "single_family",
    })
    assert a.address == "123 Main St"
    assert a.state == "NY"
    assert a.sqft is None
    # annual_taxes / annual_insurance default to 0
    assert a.annual_taxes == 0
    assert a.annual_insurance == 0


def test_intake_document_overrides_round_trip_with_due_offsets() -> None:
    """Step 4's per-item due-offset edits land on the new
    `due_offset_overrides` map — a sibling to skip_names + add_items."""
    o = IntakeDocumentOverrides.model_validate({
        "skip_names": ["Government ID"],
        "add_items": [{"name": "Notarized POA"}],
        "due_offset_overrides": {"Pre-Approval Letter": 7, "Purchase Agreement": 14},
    })
    assert o.skip_names == ["Government ID"]
    assert o.due_offset_overrides == {
        "Pre-Approval Letter": 7,
        "Purchase Agreement": 14,
    }
    assert len(o.add_items) == 1


def test_intake_document_overrides_defaults_to_empty_dict() -> None:
    """Old clients posting overrides without the new map shouldn't fail."""
    o = IntakeDocumentOverrides.model_validate({
        "skip_names": ["X"],
        "add_items": [],
    })
    assert o.due_offset_overrides == {}


def test_smart_intake_payload_accepts_full_redesigned_shape() -> None:
    """End-to-end shape: Step 1 purpose + loan type, Step 2 address only,
    Step 3 calculator-derived amount + ltv, Step 4 overrides with due_offsets."""
    payload = SmartIntakePayload.model_validate({
        "borrower": {
            "name": "Marcus Holloway",
            "email": "marcus@holloway.cap",
            "phone": "(555) 555-1234",
            "entity_type": "llc",
            "entity_name": "Holloway Capital LLC",
            "experience": "1_2_flips",
        },
        "asset": {
            "address": "123 Main St",
            "city": "Brooklyn",
            "state": "NY",
            "property_type": "single_family",
        },
        "numbers": {
            "type": "dscr",
            "purpose": "purchase",
            "amount": 365_000,
            "ltv": 0.75,
            "base_rate": 7.5,
        },
        "ai_rules": {"floor_rate": 6.5},
        "side": "buyer",
        "document_overrides": {
            "skip_names": ["Government ID"],
            "due_offset_overrides": {"Pre-Approval Letter": 5},
        },
    })
    assert payload.numbers.purpose is LoanPurpose.PURCHASE
    assert payload.asset.state == "NY"
    assert payload.document_overrides is not None
    assert payload.document_overrides.due_offset_overrides == {"Pre-Approval Letter": 5}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
