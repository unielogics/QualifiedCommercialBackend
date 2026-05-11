"""AI Deal Secretary — server-side helpers that prepare a deal's task list.

Two helpers here. Both are idempotent.

  • bootstrap_requirement_status_rows(db, loan)
      Called immediately after a Loan is created (POST /intake,
      POST /loans, _spawn_loan_from_approved_request). Walks the
      requirement resolver against the loan's (product, side, agent)
      and creates one ClientRequirementStatus per resolved requirement
      with status='missing' and owner_type from the catalog default.
      Also ensures the ClientAIPlan row exists so ai_secretary_settings
      (file-level outreach_mode kill switch) is in place from minute
      one.

  • materialize_pending_assignments(db, client, loan)
      Walks the buffered "wizard intent" stored on
      ClientAIPlan.ai_secretary_settings.pending_assignments — agents
      who picked AI-handled tasks in Step 4 of AgentLeadModal BEFORE
      the loan existed get those choices materialized into real
      AITaskAssignment rows the moment a Loan is created from their
      prequal. (Phase 2 wiring — written here so both phases share
      the module.)

Both helpers respect the file-level outreach_mode = 'draft_first'
default — nothing actually sends to the borrower until an operator
flips the mode in the workbench.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import LoanPurpose, LoanSide, LoanType, TaskOwnerType
from app.models.client_ai_plan import ClientAIPlan
from app.models.client_requirement_status import ClientRequirementStatus
from app.models.loan import Loan
from app.services.ai.requirement_resolver import (
    ResolvedRequirement,
    resolve_requirements,
)

log = logging.getLogger(__name__)


# Loan.type + Loan.purpose → platform playbook product_key. Returns None
# for loan types we don't have a platform playbook for (portfolio, raw
# cash_out_refi) — bootstrap still runs but the resolver will return no
# rows, which is fine.
def _product_key_for_loan(loan: Loan) -> str | None:
    if loan.type == LoanType.DSCR:
        if loan.purpose == LoanPurpose.PURCHASE:
            return "dscr_purchase"
        return "dscr_refi"
    if loan.type == LoanType.FIX_AND_FLIP:
        return "fix_flip"
    if loan.type == LoanType.GROUND_UP:
        return "construction"
    if loan.type == LoanType.BRIDGE:
        return "bridge"
    if loan.type == LoanType.CASH_OUT_REFI:
        return "dscr_refi"
    return None


def _side_for_loan(loan: Loan) -> str | None:
    """Loan.side is buyer/seller; fall back to "buyer" since that's the
    overwhelming default for lending-phase loans (sellers don't go
    through the funding pipeline typically)."""
    if loan.side is None:
        return "buyer"
    return loan.side.value if hasattr(loan.side, "value") else str(loan.side)


def _context_for_loan(loan: Loan) -> dict[str, Any]:
    """Best-effort facts for the resolver's applies_when evaluator.
    Pulls what's already on the Loan row — entity type, contract
    status, refi-vs-purchase flag. Other facts get filled in over
    the deal's lifetime by chat updates."""
    ctx: dict[str, Any] = {
        "under_contract": True,  # offer_accepted spawned the loan
        "loan_purpose": str(loan.purpose) if loan.purpose else None,
        "financing_needed": True,
        "property_type": str(loan.property_type) if loan.property_type else None,
    }
    return ctx


async def bootstrap_requirement_status_rows(
    db: AsyncSession,
    loan: Loan,
    *,
    log_label: str = "bootstrap",
) -> dict[str, int]:
    """Create missing ClientRequirementStatus rows for `loan` from the
    resolved playbook + ensure a ClientAIPlan row exists.

    Idempotent. Returns counts: {"crs_inserted": int, "crs_skipped": int,
    "plan_created": bool}. Safe to call on existing deals — only
    inserts CRS rows whose (client_id, loan_id, requirement_key)
    triple isn't already present.

    The caller decides when to invoke:
      - POST /intake (operator-side wizard) — call after the Loan is created.
      - POST /loans (direct create) — same.
      - _spawn_loan_from_approved_request (prequal → loan) — same.
      - Repair endpoint — for loans that pre-date this feature.
    """
    product_key = _product_key_for_loan(loan)
    side = _side_for_loan(loan)
    context = _context_for_loan(loan)
    agent_id = loan.broker_id  # NULL is OK — resolver tolerates it.

    resolved: list[ResolvedRequirement] = await resolve_requirements(
        db,
        client_id=loan.client_id,
        loan_id=loan.id,
        phase="lending",
        loan_product=product_key,
        side=side,  # type: ignore[arg-type]
        agent_id=agent_id,
        context=context,
    )

    # Snapshot existing CRS keys for this (client, loan) so we can skip them.
    existing_keys = {
        row[0]
        for row in (
            await db.execute(
                select(ClientRequirementStatus.requirement_key)
                .where(
                    ClientRequirementStatus.client_id == loan.client_id,
                    ClientRequirementStatus.loan_id == loan.id,
                )
            )
        ).all()
    }

    crs_inserted = 0
    crs_skipped = 0
    for r in resolved:
        if r.requirement_key in existing_keys:
            crs_skipped += 1
            continue
        # Funding-locked items get pinned to that owner_type so agents
        # can't move them. Everything else inherits the catalog default
        # (which is typically 'human'; the AI Deal Secretary picker
        # is what flips it to 'ai' later).
        owner_type = r.default_owner_type
        if not r.can_agent_override and r.source == "funding_required":
            owner_type = TaskOwnerType.FUNDING_LOCKED.value
        db.add(
            ClientRequirementStatus(
                id=uuid.uuid4(),
                client_id=loan.client_id,
                loan_id=loan.id,
                requirement_key=r.requirement_key,
                status="missing",
                source=r.source,
                owner_type=owner_type,
                due_at=None,
                notes=None,
            )
        )
        crs_inserted += 1

    # Ensure a ClientAIPlan row exists so the file-level
    # ai_secretary_settings JSONB (outreach_mode kill switch) is
    # available from the first request. The JSONB column has a
    # server default of {"outreach_mode": "draft_first"} — we
    # rely on that here rather than rebuilding the full snapshot.
    plan_created = False
    existing_plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == loan.client_id,
                ClientAIPlan.loan_id == loan.id,
            )
        )
    ).scalar_one_or_none()
    if existing_plan is None:
        db.add(
            ClientAIPlan(
                id=uuid.uuid4(),
                client_id=loan.client_id,
                loan_id=loan.id,
                agent_id=loan.broker_id,
                current_phase="lending",
                active_playbook_versions=[],
                custom_instructions=None,
                required_items=[],
                waived_items=[],
                ai_suggested_items=[],
                next_best_question=None,
                next_best_action=None,
                readiness_score=None,
                # ai_secretary_settings: JSONB column defaults to
                # {"outreach_mode": "draft_first"} via the model's
                # default callable; explicit set keeps it readable.
                ai_secretary_settings={"outreach_mode": "draft_first"},
            )
        )
        plan_created = True

    await db.flush()
    log.info(
        "deal_secretary.%s loan_id=%s product=%s side=%s "
        "crs_inserted=%d crs_skipped=%d plan_created=%s",
        log_label,
        loan.id,
        product_key,
        side,
        crs_inserted,
        crs_skipped,
        plan_created,
    )
    return {
        "crs_inserted": crs_inserted,
        "crs_skipped": crs_skipped,
        "plan_created": plan_created,
    }


async def materialize_pending_assignments(
    db: AsyncSession,
    loan: Loan,
) -> int:
    """Phase 2 helper — converts the `pending_assignments` buffer on
    ClientAIPlan.ai_secretary_settings into real AITaskAssignment
    rows once the Loan exists.

    Used by the prequal-accept path: the agent picked AI-handled
    tasks in Step 4 of AgentLeadModal BEFORE there was a Loan; we
    stored the intent on the (lead-stage) ClientAIPlan and now
    materialize it.

    Returns the number of AITaskAssignment rows created. Idempotent —
    clears the buffer after a successful materialization so a second
    call is a no-op.

    NOTE: full implementation lands in Phase 2 with the assignments
    router. This stub keeps the module surface stable so callers
    can be wired without ImportErrors. The plan_builder.rebuild()
    follow-up + outreach gate live in Phase 4.
    """
    # Phase 2 will fill this in. For now we just clear the buffer so
    # subsequent ClientAIPlan reads aren't confused by stale intent.
    plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == loan.client_id,
                ClientAIPlan.loan_id == loan.id,
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        return 0
    settings = dict(plan.ai_secretary_settings or {})
    pending = settings.pop("pending_assignments", None)
    if not pending:
        return 0
    plan.ai_secretary_settings = settings
    await db.flush()
    log.info(
        "deal_secretary.materialize_pending loan_id=%s pending_count=%d "
        "(stub — full materialization lands in Phase 2)",
        loan.id,
        len(pending) if isinstance(pending, list) else 0,
    )
    return 0
