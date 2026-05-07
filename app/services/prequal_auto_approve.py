"""Deterministic auto-approval gate for pre-qualification requests.

When the math is clean — and ONLY when the math is clean — submitted
prequal requests bypass the admin queue and land in `status=approved`
with a generated PDF. Anything that fails any check stays `pending`
for manual review.

Public surface:
  AutoApproveDecision  (dataclass with all the inputs the approve
                        path needs + a list of human-readable reasons)
  evaluate(...)        async function that runs the checks

The gate is intentionally conservative — every condition is an AND.
The `auto_approve_blockers` list on the result tells the borrower
(and admin) exactly which check failed, so falling through to manual
review is itself a useful UX signal ("we need to verify your credit
before issuing a letter").

This is a deterministic policy engine, not an LLM. The point is that
every auto-approval is reproducible — given the same inputs the
system gives the same answer, every time. AI sanity-checks layer on
top of this in a future phase if needed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from app.enums import LoanType
from app.models.client import Client
from app.models.prequal_request import PrequalRequest
from app.routers.prequal import LTV_CAPS  # single source of truth for caps
from app.schemas.settings import AppSettingsData, PrequalAutoApprovalSettings
from app.services.credit_summary import fico_tier, tier_max_ltv

log = logging.getLogger(__name__)


@dataclass
class AutoApproveDecision:
    """Result of evaluating a prequal request against the auto-approve
    policy.

    auto_approve            True only when all checks pass.
    reasons                 Free-form human-readable reasons. On
                            auto_approve=True these explain WHY the
                            system approved (audit trail). On False
                            they explain WHY NOT (admin sees them on
                            the queue, borrower sees them in-app).
    approved_purchase_price Pre-validated to honor the matrix cap.
                            On auto-approve we use the borrower's
                            requested numbers exactly.
    approved_loan_amount    Same.
    scenario                Minimal stub that the approve path
                            stamps into approved_scenario JSONB. We
                            don't run the full operator calculator
                            here — that's an admin's call.
    """
    auto_approve: bool
    reasons: list[str] = field(default_factory=list)
    approved_purchase_price: float = 0.0
    approved_loan_amount: float = 0.0
    scenario: dict[str, Any] = field(default_factory=dict)


def evaluate(
    request: PrequalRequest,
    client: Client | None,
    settings: AppSettingsData,
) -> AutoApproveDecision:
    """Pure function. Doesn't touch the DB. Caller passes in the
    request, the (eager-loaded) Client, and the deserialized
    settings. Returns a decision.

    The Client argument is optional only so the function can be
    called pre-credit-pull and return a sensible "no credit on file"
    blocker. When `require_credit_pull=True` (the default) the
    absence of a Client.fico is itself a blocker, even though we
    have an authenticated borrower."""
    cfg: PrequalAutoApprovalSettings = settings.prequal_auto_approval
    blockers: list[str] = []

    if not cfg.enabled:
        blockers.append("Auto-approval is currently disabled in firm settings.")

    # Required-field check — request schema enforces these on submit
    # but we double-check here so the auto path never crashes on a
    # half-formed row (e.g. one created by a backfill or test fixture).
    if not request.target_property_address or len(request.target_property_address.strip()) < 3:
        blockers.append("Target property address is missing.")
    if not request.expected_closing_date:
        blockers.append("Expected closing date is missing.")
    if not request.loan_type:
        blockers.append("Loan type is missing.")

    purchase = float(request.purchase_price or 0)
    loan_amt = float(request.requested_loan_amount or 0)
    arv = float(request.arv_estimate or 0)

    if purchase <= 0:
        blockers.append("Purchase price must be greater than zero.")
    if loan_amt <= 0:
        blockers.append("Requested loan amount must be greater than zero.")

    # F&F is sized against the after-repair value (LTARV); other
    # products use loan / purchase. Without this branch every F&F
    # submit was stamped declined (loan/BRV is meaningless for fix &
    # flip — the loan funds the rehab to ARV).
    is_fix_flip = request.loan_type == "fix_flip"
    if is_fix_flip:
        if arv <= 0:
            blockers.append("F&F auto-approval requires an ARV in the Scope of Work.")
        ltv_basis = arv
        ltv_label = "LTARV"
    else:
        ltv_basis = purchase
        ltv_label = "LTV"

    # Loan amount safety ceiling.
    if loan_amt > cfg.safety_loan_ceiling_usd:
        blockers.append(
            f"Loan amount ${loan_amt:,.0f} exceeds auto-approval ceiling of "
            f"${cfg.safety_loan_ceiling_usd:,}. Admin review required."
        )

    # Matrix cap by product.
    cap = LTV_CAPS.get(request.loan_type)
    if cap is None:
        blockers.append(f"Unknown loan type: {request.loan_type}")
    elif ltv_basis > 0 and loan_amt > 0:
        ltv = loan_amt / ltv_basis
        if ltv > cap + 1e-6:
            blockers.append(
                f"Requested {ltv_label} {ltv * 100:.1f}% exceeds the "
                f"{request.loan_type} cap of {cap * 100:.0f}%."
            )

    # Credit / tier gate. Pull the FICO off the Client (post-pull) and
    # convert to tier; reject when no pull is on file (configurable).
    fico: int | None = client.fico if client is not None else None
    if fico is None:
        if cfg.require_credit_pull:
            blockers.append(
                "No credit pull on file. Run a soft credit check before "
                "auto-approval."
            )
    else:
        if fico < cfg.fico_floor:
            blockers.append(
                f"FICO {fico} is below the auto-approval floor of "
                f"{cfg.fico_floor}."
            )
        tier = fico_tier(fico)
        tier_cap = tier_max_ltv(tier)
        if tier_cap <= 0:
            blockers.append(
                f"Credit tier ({tier}) is blocked from new commercial "
                f"financing."
            )
        elif ltv_basis > 0 and loan_amt > 0:
            tier_ltv = loan_amt / ltv_basis
            if tier_ltv > tier_cap + 1e-6:
                blockers.append(
                    f"Requested {ltv_label} {tier_ltv * 100:.1f}% exceeds the "
                    f"borrower's tier cap of {tier_cap * 100:.0f}% ({tier})."
                )

    auto_approve = not blockers

    # Reasons-on-success: a tiny audit trail so the admin sees on the
    # queue exactly why the system approved.
    success_reasons: list[str] = []
    if auto_approve:
        success_reasons.append(
            f"{ltv_label} {(loan_amt / ltv_basis * 100):.1f}% within "
            f"{cap * 100:.0f}% matrix cap."
        )
        if fico is not None:
            success_reasons.append(
                f"FICO {fico} ({fico_tier(fico)}) clears auto-approval floor."
            )
        success_reasons.append(
            f"Loan amount ${loan_amt:,.0f} within "
            f"${cfg.safety_loan_ceiling_usd:,} ceiling."
        )

    return AutoApproveDecision(
        auto_approve=auto_approve,
        reasons=success_reasons if auto_approve else blockers,
        approved_purchase_price=purchase,
        approved_loan_amount=loan_amt,
        scenario={
            "auto_approved": auto_approve,
            "loan_type": request.loan_type,
            "ltv": (loan_amt / ltv_basis) if ltv_basis > 0 else None,
            "ltv_basis": ltv_label.lower(),
            "fico_at_approval": fico,
            "fico_tier_at_approval": fico_tier(fico) if fico is not None else None,
        },
    )
