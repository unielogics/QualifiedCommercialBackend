"""Seed realistic defaults onto the existing platform playbook catalog.

Revision ID: 0039
Revises: 0038
Create Date: 2026-05-11

Migration 0038 widened the catalog (12-value RequirementCategory
taxonomy, link_url, objective_text, completion_criteria, completion_mode,
default_channels, default_cadence_hours). This data migration walks the
71 existing AICollectionRequirement rows seeded by 0032 and enriches
each with:

  • A category from the 12-value taxonomy (re-categorizes more
    accurately than the broad fact→borrower_info / document→financials
    fallback applied in 0038).
  • A one-line plain-language objective.
  • An explicit completion_criteria string.
  • A completion_mode (ai_can_complete vs requires_human_verify vs
    borrower_self_attest).
  • A sensible default_channels list + default_cadence_hours.
  • Where applicable, a link_kind (docusign for executed agreements
    like buyer_agency_agreement, listing_agreement; link_url itself
    stays NULL because it's firm-specific — the agent fills it on
    their Settings page).

Idempotent — uses UPDATE WHERE requirement_key=X so re-running this
migration on an enriched catalog is a no-op (Postgres just overwrites
with the same values).

Critical design choice: requirement_key is treated as semantically
stable across playbooks. `bank_statements` means the same thing
whether it's on dscr_purchase or bridge, so the same enrichment
applies regardless of which playbook the row sits on.
"""

from __future__ import annotations

from typing import Any, Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: Union[str, None] = "0038"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Per-requirement enrichment dictionary. Keys MUST match the
# `requirement_key` strings seeded in 0032_playbooks_and_requirements.py.
# Any key not present here keeps the post-0038 defaults
# (category=financials, objective="", completion_mode=ai_can_complete,
# channels=["portal"], cadence_hours=48).
_REQUIREMENT_ENRICHMENT: dict[str, dict[str, Any]] = {
    # ---------- Borrower-side facts (lending playbooks) ----------
    "borrower_entity_type": {
        "category": "borrower_info",
        "objective": "Confirm whether the borrower is buying as an individual, LLC, corporation, or trust.",
        "completion_criteria": "Borrower confirms entity type in chat or operator enters it on the loan record.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "credit_authorization": {
        "category": "credit",
        "objective": "Collect signed credit pull authorization.",
        "completion_criteria": "Signed authorization document uploaded and on file.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 48,
        "link_kind": "docusign",
        "link_label": "Sign Credit Authorization",
    },
    "id_document": {
        "category": "compliance",
        "objective": "Collect government-issued photo ID for the borrower.",
        "completion_criteria": "Clear photo of driver's license or passport uploaded; name matches loan record.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "exit_strategy": {
        "category": "borrower_info",
        "objective": "Confirm the borrower's exit plan (refinance, sale, hold-to-rent).",
        "completion_criteria": "Borrower describes exit plan in chat or selects from intake choices.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "experience_tier": {
        "category": "borrower_info",
        "objective": "Confirm the borrower's investment experience tier.",
        "completion_criteria": "Borrower confirms property count + ownership history.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 48,
    },

    # ---------- Property / collateral ----------
    "appraisal": {
        "category": "appraisal_and_inspection",
        "objective": "Order and receive appraisal for the subject property.",
        "completion_criteria": "Appraisal PDF uploaded with current effective date and matching property address.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 72,
    },
    "land_appraisal": {
        "category": "appraisal_and_inspection",
        "objective": "Order land + as-completed valuation for ground-up construction.",
        "completion_criteria": "Appraisal report with land value + as-completed value uploaded.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 72,
    },
    "property_taxes": {
        "category": "financials",
        "objective": "Confirm annual property tax amount for the subject property.",
        "completion_criteria": "Tax bill or assessor record uploaded showing current annual amount.",
        "completion_mode": "ai_can_complete",
        "channels": ["portal"],
        "cadence_hours": 48,
    },

    # ---------- Financial docs ----------
    "bank_statements": {
        "category": "financials",
        "objective": "Collect 2 months of bank statements for the borrower's primary operating account.",
        "completion_criteria": "Two consecutive full-month PDF statements uploaded; scanner confirms account holder name + date range.",
        "completion_mode": "ai_can_complete",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "rent_roll": {
        "category": "financials",
        "objective": "Collect current rent roll for the subject property.",
        "completion_criteria": "Schedule of leases uploaded with monthly rent + lease end dates per unit.",
        "completion_mode": "ai_can_complete",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "current_mortgage_statement": {
        "category": "financials",
        "objective": "Collect current mortgage statement on the property being refinanced.",
        "completion_criteria": "Most recent month's statement uploaded showing principal balance and payment history.",
        "completion_mode": "ai_can_complete",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "rehab_budget": {
        "category": "financials",
        "objective": "Collect itemized rehab budget for the construction scope.",
        "completion_criteria": "Line-item budget uploaded with total matching loan request.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 72,
    },
    "scope_of_work": {
        "category": "financials",
        "objective": "Collect detailed scope of work for the rehab/construction.",
        "completion_criteria": "Written SOW uploaded describing each phase, materials, and contractor.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 72,
    },

    # ---------- Agreements ----------
    "purchase_contract": {
        "category": "agreements",
        "objective": "Collect fully executed purchase contract.",
        "completion_criteria": "Signed PDF uploaded with all parties' signatures, agreed price, and closing date.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 24,
    },
    "operating_agreement": {
        "category": "agreements",
        "objective": "Collect signed operating agreement for the borrowing entity.",
        "completion_criteria": "Executed operating agreement uploaded with member signatures.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal"],
        "cadence_hours": 72,
    },

    # ---------- Insurance ----------
    "insurance_estimate": {
        "category": "insurance",
        "objective": "Collect insurance quote or binder for the subject property.",
        "completion_criteria": "Quote or binder PDF uploaded showing coverage amount and effective dates.",
        "completion_mode": "ai_can_complete",
        "channels": ["portal", "email"],
        "cadence_hours": 48,
    },
    "insurance_declarations": {
        "category": "insurance",
        "objective": "Collect current insurance declarations page.",
        "completion_criteria": "Dec page PDF uploaded showing dwelling coverage and effective dates.",
        "completion_mode": "ai_can_complete",
        "channels": ["portal", "email"],
        "cadence_hours": 48,
    },

    # ---------- Construction-specific ----------
    "permits": {
        "category": "compliance",
        "objective": "Collect approved building permits for the construction scope.",
        "completion_criteria": "Permit documents uploaded showing approval from the local jurisdiction.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 72,
    },
    "general_contractor": {
        "category": "compliance",
        "objective": "Confirm general contractor identity, license, and insurance.",
        "completion_criteria": "GC's license + insurance certificate uploaded; license verified as current.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 72,
    },

    # ---------- Buyer transaction playbook ----------
    "client_name": {
        "category": "borrower_info",
        "objective": "Capture the client's full legal name.",
        "completion_criteria": "Name confirmed in chat or on the lead intake form.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "client_phone": {
        "category": "borrower_info",
        "objective": "Capture the client's phone number.",
        "completion_criteria": "Phone number captured and verified by SMS or call-back.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "client_email": {
        "category": "borrower_info",
        "objective": "Capture the client's email address.",
        "completion_criteria": "Email captured; client receives at least one delivery confirmation.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "target_property_type": {
        "category": "property_data",
        "objective": "Confirm what kind of property the buyer is targeting.",
        "completion_criteria": "Buyer selects from SFR / 2-4 / mixed-use / commercial.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "target_location": {
        "category": "property_data",
        "objective": "Confirm target neighborhoods or zip codes.",
        "completion_criteria": "Buyer lists target areas in chat or on the intake form.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "target_budget": {
        "category": "financials",
        "objective": "Confirm the buyer's purchase price range and available capital.",
        "completion_criteria": "Buyer confirms a price ceiling and down-payment capacity.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "purchase_timeline": {
        "category": "borrower_info",
        "objective": "Confirm the buyer's purchase timeline.",
        "completion_criteria": "Buyer selects ASAP / 0-30d / 30-60d / 60+d.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "financing_needed": {
        "category": "borrower_info",
        "objective": "Confirm whether the buyer needs financing or is paying cash.",
        "completion_criteria": "Buyer selects financing / cash / not sure.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "buyer_agency_agreement": {
        "category": "agreements",
        "objective": "Get the buyer agency agreement signed before scheduling showings.",
        "completion_criteria": "Executed buyer agency agreement uploaded; both parties signed.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 24,
        "link_kind": "docusign",
        "link_label": "Sign Buyer Agency Agreement",
    },
    "prequalification_status": {
        "category": "financials",
        "objective": "Confirm whether the buyer has a current prequalification letter.",
        "completion_criteria": "Letter uploaded OR borrower acknowledges they need to start prequal.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "proof_of_funds": {
        "category": "financials",
        "objective": "Collect proof of funds for the down payment.",
        "completion_criteria": "Recent statement or bank letter uploaded showing required liquidity.",
        "completion_mode": "ai_can_complete",
        "channels": ["portal", "email"],
        "cadence_hours": 48,
    },

    # ---------- Seller transaction playbook ----------
    "property_address": {
        "category": "property_data",
        "objective": "Capture the listing's full property address.",
        "completion_criteria": "Address confirmed in chat or on the intake form.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "owner_name": {
        "category": "borrower_info",
        "objective": "Confirm the legal owner of the property.",
        "completion_criteria": "Owner name captured and matches property record.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "desired_list_price": {
        "category": "financials",
        "objective": "Confirm the seller's target list price.",
        "completion_criteria": "Seller provides a target list price.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "selling_timeline": {
        "category": "borrower_info",
        "objective": "Confirm when the seller wants to be off-market.",
        "completion_criteria": "Seller selects a target close window.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 24,
    },
    "occupancy_status": {
        "category": "property_data",
        "objective": "Confirm whether the property is owner-occupied, tenant-occupied, or vacant.",
        "completion_criteria": "Seller selects occupancy status.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "property_condition": {
        "category": "property_data",
        "objective": "Capture the seller's description of property condition.",
        "completion_criteria": "Seller describes condition + flags any known issues.",
        "completion_mode": "borrower_self_attest",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "listing_agreement": {
        "category": "agreements",
        "objective": "Get the listing agreement signed.",
        "completion_criteria": "Executed listing agreement uploaded; both parties signed.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal", "email"],
        "cadence_hours": 24,
        "link_kind": "docusign",
        "link_label": "Sign Listing Agreement",
    },
    "cma_task": {
        "category": "communication",
        "objective": "Deliver a comparative market analysis to the seller.",
        "completion_criteria": "Agent shares CMA in chat; seller acknowledges.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
    "picture_day": {
        "category": "scheduling",
        "objective": "Schedule professional listing photography.",
        "completion_criteria": "Date confirmed with seller + photographer.",
        "completion_mode": "requires_human_verify",
        "channels": ["portal"],
        "cadence_hours": 48,
    },
}


def upgrade() -> None:
    bind = op.get_bind()

    for requirement_key, fields in _REQUIREMENT_ENRICHMENT.items():
        # Build the SET clause dynamically — only update fields that
        # this requirement actually overrides (so we don't trample
        # defaults with NULL on rows missing a field).
        set_parts = ["category = :category"]
        params: dict[str, Any] = {
            "category": fields["category"],
            "requirement_key": requirement_key,
        }

        if "objective" in fields:
            set_parts.append("objective_text = :objective_text")
            params["objective_text"] = fields["objective"]
        if "completion_criteria" in fields:
            set_parts.append("completion_criteria = :completion_criteria")
            params["completion_criteria"] = fields["completion_criteria"]
        if "completion_mode" in fields:
            set_parts.append("completion_mode = :completion_mode")
            params["completion_mode"] = fields["completion_mode"]
        if "channels" in fields:
            # JSONB literal — psycopg accepts a JSON string here.
            import json
            set_parts.append("default_channels = CAST(:default_channels AS jsonb)")
            params["default_channels"] = json.dumps(fields["channels"])
        if "cadence_hours" in fields:
            set_parts.append("default_cadence_hours = :default_cadence_hours")
            params["default_cadence_hours"] = fields["cadence_hours"]
        if "link_kind" in fields:
            set_parts.append("link_kind = :link_kind")
            params["link_kind"] = fields["link_kind"]
        if "link_label" in fields:
            set_parts.append("link_label = :link_label")
            params["link_label"] = fields["link_label"]

        stmt = sa.text(
            f"UPDATE ai_collection_requirements SET {', '.join(set_parts)} "
            f"WHERE requirement_key = :requirement_key"
        )
        bind.execute(stmt, params)


def downgrade() -> None:
    # Reset enriched fields back to the post-0038 defaults. We do NOT
    # try to restore the pre-0039 category (it was already remapped by
    # 0038's data step). Downgrading 0039 only undoes the enrichment.
    bind = op.get_bind()
    bind.execute(
        sa.text(
            "UPDATE ai_collection_requirements SET "
            "  objective_text = '', "
            "  completion_criteria = '', "
            "  completion_mode = 'ai_can_complete', "
            "  default_channels = CAST('[\"portal\"]' AS jsonb), "
            "  default_cadence_hours = 48, "
            "  link_kind = NULL, "
            "  link_label = NULL "
            "WHERE requirement_key = ANY(:keys)"
        ),
        {"keys": list(_REQUIREMENT_ENRICHMENT.keys())},
    )
