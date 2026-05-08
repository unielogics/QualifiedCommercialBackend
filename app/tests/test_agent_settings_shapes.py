"""Schema-level tests for the realtor overhaul (alembic 0025).

These don't need Postgres — pure Pydantic round-trips. They guard:

  - `AgentSettingsData` accepts the post-codex-PR shape (side-only
    checklist keys, single cadence object, slim letterhead).
  - The `_migrate_v1_shapes` model_validator silently strips legacy
    `"loan_type:side"` checklist keys and collapses old
    `dict[loan_type, AgentCadenceOverride]` shapes.
  - `_merge_cadence` cascades firm → broker → client per field.
"""

from __future__ import annotations

import pytest

from app.schemas.broker_settings import (
    AgentCadenceOverride,
    AgentChecklistOverlay,
    AgentLetterhead,
    AgentSettingsData,
)
from app.schemas.settings import AppSettingsData, LoanTypeChecklist
from app.services.agent_checklist import _merge_cadence


def test_agent_settings_new_shape_roundtrips() -> None:
    raw = {
        "checklists": {
            "buyer": {
                "disabled_firm_items": ["Government ID"],
                "extra_items": [],
            }
        },
        "cadence": {
            "first_reminder_days": 5,
            "second_reminder_days": 12,
            "escalate_after_days": 21,
        },
        "letterhead": {
            "title": "Realtor",
            "license_number": "L-1",
            "brokerage_name": "Acme Realty",
            "headshot_s3_key": "brokers/abc/headshot.png",
        },
    }
    d = AgentSettingsData.model_validate(raw)
    assert list(d.checklists.keys()) == ["buyer"]
    assert d.cadence is not None
    assert d.cadence.first_reminder_days == 5
    assert d.letterhead is not None
    assert d.letterhead.headshot_s3_key == "brokers/abc/headshot.png"


def test_agent_settings_legacy_v1_keys_dropped() -> None:
    """Old `loan_type:side` checklist keys are silently stripped."""
    raw = {
        "checklists": {
            "dscr:buyer": {"disabled_firm_items": ["x"], "extra_items": []},
            "bridge:seller": {"disabled_firm_items": [], "extra_items": []},
            "buyer": {"disabled_firm_items": ["new style"], "extra_items": []},
        }
    }
    d = AgentSettingsData.model_validate(raw)
    # Only the side-only key survives.
    assert set(d.checklists.keys()) == {"buyer"}


def test_agent_settings_legacy_per_loan_type_cadence_collapses() -> None:
    """Legacy `dict[loan_type, AgentCadenceOverride]` collapses to a
    single AgentCadenceOverride. First non-null per field wins."""
    raw = {
        "cadence": {
            "dscr": {
                "first_reminder_days": 2,
                "second_reminder_days": 5,
                "escalate_after_days": 10,
            },
            "bridge": {
                "first_reminder_days": None,
                "second_reminder_days": 7,
                "escalate_after_days": None,
            },
        }
    }
    d = AgentSettingsData.model_validate(raw)
    assert d.cadence is not None
    assert d.cadence.first_reminder_days == 2
    assert d.cadence.second_reminder_days == 5
    assert d.cadence.escalate_after_days == 10


def test_agent_settings_letterhead_drops_legacy_identity_fields() -> None:
    """display_name/email/phone/signature_block from the v1 shape are
    silently dropped — the new schema doesn't declare them."""
    raw = {
        "letterhead": {
            "display_name": "old",
            "email": "old@x.com",
            "phone": "555",
            "signature_block": "sig",
            "title": "Real Estate Agent",
            "license_number": "L-2",
            "brokerage_name": "B",
        }
    }
    d = AgentSettingsData.model_validate(raw)
    lh = d.letterhead
    assert isinstance(lh, AgentLetterhead)
    assert lh.title == "Real Estate Agent"
    # Dropped fields are not declared on the new schema:
    assert not hasattr(lh, "display_name") or getattr(lh, "display_name", None) is None


def test_merge_cadence_client_wins_over_broker_over_firm() -> None:
    base = LoanTypeChecklist(
        first_reminder_days=3, second_reminder_days=7, escalate_after_days=14
    )
    broker = AgentCadenceOverride(
        first_reminder_days=5,
        second_reminder_days=None,
        escalate_after_days=21,
    )
    client = AgentCadenceOverride(
        first_reminder_days=2,
        second_reminder_days=None,
        escalate_after_days=None,
    )
    merged = _merge_cadence(base, broker, client)
    # client wins
    assert merged.first_reminder_days == 2
    # both null → firm fallback
    assert merged.second_reminder_days == 7
    # broker wins (client null)
    assert merged.escalate_after_days == 21
    # docs + auto_approve_risk_score pass through unchanged
    assert merged.docs == base.docs
    assert merged.auto_approve_risk_score == base.auto_approve_risk_score


def test_merge_cadence_no_overrides_returns_base() -> None:
    base = LoanTypeChecklist(
        first_reminder_days=3, second_reminder_days=7, escalate_after_days=14
    )
    merged = _merge_cadence(base, None, None)
    assert merged is base  # no copy when nothing to merge


def test_app_settings_seeds_transaction_checklists() -> None:
    """A bare AppSettingsData() returns the buyer/seller defaults so
    the agent's lead-stage UI can read them without a super-admin
    having configured anything yet."""
    d = AppSettingsData()
    assert "buyer" in d.transaction_checklists
    assert "seller" in d.transaction_checklists
    buyer_names = [it.name for it in d.transaction_checklists["buyer"].docs]
    assert "Government ID" in buyer_names
    assert "Purchase Agreement" in buyer_names
    seller_names = [it.name for it in d.transaction_checklists["seller"].docs]
    assert "Listing Agreement" in seller_names


def test_agent_checklist_overlay_extras_validate() -> None:
    """The wizard sends DocChecklistItem with side='buyer' inside
    extra_items — make sure that validates."""
    overlay = AgentChecklistOverlay.model_validate({
        "disabled_firm_items": ["Government ID"],
        "extra_items": [
            {
                "name": "Custom doc",
                "due_offset_days": 5,
                "side": "buyer",
                "required": False,
                "auto_request": True,
            }
        ],
    })
    assert overlay.extra_items[0].name == "Custom doc"
    assert overlay.extra_items[0].side == "buyer"


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
