"""Native Anthropic tool-use definitions.

Each tool is a plain async function. The orchestrator registers them with the
SDK via `tool_use` blocks. Adding a new tool = add a function here + a JSON
schema entry in TOOL_SCHEMAS.
"""

from __future__ import annotations

from typing import Any

from app.enums import LoanStage, LoanType, PropertyType
from app.services.hud_template import build_hud_draft
from app.services.math import (
    amortization_schedule,
    dscr,
    interest_only_payment,
    monthly_payment,
    pricing_quote,
)


async def calculate_amortization(
    principal: float,
    annual_rate: float,
    term_months: int,
    interest_only: bool = False,
) -> dict[str, Any]:
    """Tool: returns monthly payment + total interest. IO if requested."""
    if interest_only:
        pmt = interest_only_payment(principal, annual_rate)
        return {
            "monthly_payment": pmt,
            "total_interest": round(pmt * term_months, 2),
            "interest_only": True,
        }
    pmt = monthly_payment(principal, annual_rate, term_months)
    schedule = amortization_schedule(principal, annual_rate, term_months)
    return {
        "monthly_payment": round(pmt, 2),
        "total_interest": round(sum(r.interest for r in schedule), 2),
        "interest_only": False,
        "first_payment": {
            "principal": schedule[0].principal,
            "interest": schedule[0].interest,
        },
    }


async def calculate_dscr(
    monthly_rent: float,
    principal: float,
    annual_rate: float,
    term_months: int,
    annual_taxes: float,
    annual_insurance: float,
    monthly_hoa: float = 0.0,
) -> dict[str, Any]:
    """Tool: returns DSCR ratio with status tier."""
    ratio = dscr(monthly_rent, principal, annual_rate, term_months, annual_taxes, annual_insurance, monthly_hoa)
    if ratio >= 1.25:
        tier = "preferred"
    elif ratio >= 1.0:
        tier = "standard"
    else:
        tier = "below_min"
    return {"dscr": ratio, "tier": tier}


async def quote_pricing(
    base_rate: float,
    loan_amount: float,
    discount_points: float = 0.0,
) -> dict[str, Any]:
    """Tool: returns a full pricing quote (final rate, broker comp, cash-to-close)."""
    q = pricing_quote(base_rate, loan_amount, discount_points)
    return {
        "base_rate": q.base_rate,
        "discount_points": q.discount_points,
        "final_rate": q.final_rate,
        "origination_dollars": q.broker_origination_dollars,
        "discount_dollars": q.discount_dollars,
        "total_broker_points_pct": q.total_broker_points,
        "cash_to_close_pricing": q.cash_to_close_pricing,
    }


async def draft_hud(
    loan_amount: float,
    property_type: str,
    loan_type: str,
    broker_origination_dollars: float,
) -> dict[str, Any]:
    """Tool: builds a draft HUD-1 with line items + total."""
    draft = build_hud_draft(
        loan_amount=loan_amount,
        property_type=PropertyType(property_type),
        loan_type=LoanType(loan_type),
        broker_origination_dollars=broker_origination_dollars,
    )
    return {
        "items": [
            {"code": i.code, "label": i.label, "amount": i.amount, "category": i.category}
            for i in draft.items
        ],
        "total": draft.total,
    }


# Stub tools — wired to real DB writes by the orchestrator caller.
async def request_document(loan_id: str, document_name: str, due_in_days: int = 7) -> dict[str, Any]:
    return {"queued": True, "loan_id": loan_id, "document_name": document_name, "due_in_days": due_in_days}


async def update_pipeline_status(loan_id: str, new_stage: str, note: str = "") -> dict[str, Any]:
    LoanStage(new_stage)  # validate
    return {"updated": True, "loan_id": loan_id, "new_stage": new_stage, "note": note}


async def read_email_thread(deal_id: str, limit: int = 20) -> dict[str, Any]:
    return {"deal_id": deal_id, "messages": [], "limit": limit}


TOOLS = {
    "calculate_amortization": calculate_amortization,
    "calculate_dscr": calculate_dscr,
    "quote_pricing": quote_pricing,
    "draft_hud": draft_hud,
    "request_document": request_document,
    "update_pipeline_status": update_pipeline_status,
    "read_email_thread": read_email_thread,
}


# JSON schemas for the SDK's tool-use protocol
TOOL_SCHEMAS = [
    {
        "name": "calculate_amortization",
        "description": "Compute monthly P&I (or interest-only) payment and total interest.",
        "input_schema": {
            "type": "object",
            "properties": {
                "principal": {"type": "number"},
                "annual_rate": {"type": "number", "description": "decimal, e.g. 0.07"},
                "term_months": {"type": "integer"},
                "interest_only": {"type": "boolean", "default": False},
            },
            "required": ["principal", "annual_rate", "term_months"],
        },
    },
    {
        "name": "calculate_dscr",
        "description": "Compute the DSCR ratio for a rental property.",
        "input_schema": {
            "type": "object",
            "properties": {
                "monthly_rent": {"type": "number"},
                "principal": {"type": "number"},
                "annual_rate": {"type": "number"},
                "term_months": {"type": "integer"},
                "annual_taxes": {"type": "number"},
                "annual_insurance": {"type": "number"},
                "monthly_hoa": {"type": "number", "default": 0},
            },
            "required": [
                "monthly_rent", "principal", "annual_rate", "term_months",
                "annual_taxes", "annual_insurance",
            ],
        },
    },
    {
        "name": "quote_pricing",
        "description": "Apply broker buy-down pricing logic; returns final rate + cash-to-close.",
        "input_schema": {
            "type": "object",
            "properties": {
                "base_rate": {"type": "number"},
                "loan_amount": {"type": "number"},
                "discount_points": {"type": "number", "default": 0},
            },
            "required": ["base_rate", "loan_amount"],
        },
    },
    {
        "name": "draft_hud",
        "description": "Generate a draft HUD-1 with the standard CRE fee map.",
        "input_schema": {
            "type": "object",
            "properties": {
                "loan_amount": {"type": "number"},
                "property_type": {"type": "string"},
                "loan_type": {"type": "string"},
                "broker_origination_dollars": {"type": "number"},
            },
            "required": ["loan_amount", "property_type", "loan_type", "broker_origination_dollars"],
        },
    },
    {
        "name": "request_document",
        "description": "Queue a doc request from the borrower.",
        "input_schema": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "document_name": {"type": "string"},
                "due_in_days": {"type": "integer", "default": 7},
            },
            "required": ["loan_id", "document_name"],
        },
    },
    {
        "name": "update_pipeline_status",
        "description": "Move a loan to a new pipeline stage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "loan_id": {"type": "string"},
                "new_stage": {"type": "string"},
                "note": {"type": "string"},
            },
            "required": ["loan_id", "new_stage"],
        },
    },
    {
        "name": "read_email_thread",
        "description": "Fetch the email thread for a deal (lender ↔ broker).",
        "input_schema": {
            "type": "object",
            "properties": {
                "deal_id": {"type": "string"},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["deal_id"],
        },
    },
]
