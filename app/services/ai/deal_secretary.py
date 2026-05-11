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
from datetime import date, datetime, timezone
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


def _coerce_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


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
    # available from the first request. The starting outreach_mode
    # follows precedence: AGENT.sending_control → FIRM.outreach_defaults
    # .approval_mode → "draft_first" (safest). Agent setting wins when
    # both are configured.
    from app.services.ai.agent_settings import (
        load_agent_cadence_rules,
        load_agent_user_id_for_loan,
        load_firm_communication_rules,
        sending_control_to_outreach_mode,
    )
    agent_user_id = await load_agent_user_id_for_loan(db, loan)
    agent_rules = await load_agent_cadence_rules(db, agent_user_id)
    agent_sending = agent_rules.get("sending_control") if isinstance(agent_rules, dict) else None

    firm_sending = None
    if not agent_sending:
        firm_rules = await load_firm_communication_rules(db)
        firm_defaults = firm_rules.get("outreach_defaults") if isinstance(firm_rules, dict) else None
        if isinstance(firm_defaults, dict):
            firm_sending = firm_defaults.get("approval_mode")
    outreach_mode = sending_control_to_outreach_mode(agent_sending or firm_sending)

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
                ai_secretary_settings={"outreach_mode": outreach_mode},
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
    """Convert the `pending_assignments` buffer on the realtor-phase
    ClientAIPlan into real AITaskAssignment rows + flip CRS owner_type
    to 'ai' on the matching client_requirement_status rows.

    Used by the prequal-accept path: the agent picked AI-handled
    tasks in Step 4 of AgentLeadModal BEFORE there was a Loan; we
    stored the intent on the (lead-stage) ClientAIPlan and now
    materialize it.

    Returns the number of AITaskAssignment rows created. Idempotent —
    clears the buffer after a successful materialization so a second
    call is a no-op. Also copies file_settings (outreach_mode, etc.)
    from the realtor-phase plan onto the new loan-scoped plan.
    """
    from app.models.ai_task_assignment import AITaskAssignment
    from app.models.ai_playbook import AICollectionRequirement
    from app.enums import TaskOwnerType

    # Realtor-phase plan holds the buffered intent (loan_id IS NULL).
    realtor_plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == loan.client_id,
                ClientAIPlan.loan_id.is_(None),
            )
        )
    ).scalar_one_or_none()
    if realtor_plan is None:
        return 0
    realtor_settings = dict(realtor_plan.ai_secretary_settings or {})
    pending = realtor_settings.pop("pending_assignments", None)
    file_outreach_mode = realtor_settings.get("outreach_mode")

    if not pending and not file_outreach_mode:
        return 0

    # The loan-scoped plan should already exist (bootstrap created it).
    loan_plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == loan.client_id,
                ClientAIPlan.loan_id == loan.id,
            )
        )
    ).scalar_one_or_none()
    if loan_plan is not None:
        # Copy file-level settings onto the loan-scoped plan, preserving
        # anything operators set after bootstrap.
        loan_settings = dict(loan_plan.ai_secretary_settings or {})
        if file_outreach_mode:
            loan_settings["outreach_mode"] = file_outreach_mode
        for k in ("sms_consent", "email_opt_out", "default_cadence",
                  "complete_file_by"):
            if k in realtor_settings and k not in loan_settings:
                loan_settings[k] = realtor_settings[k]
        loan_plan.ai_secretary_settings = loan_settings

    created_count = 0
    if isinstance(pending, list):
        for entry in pending:
            if not isinstance(entry, dict):
                continue
            req_key = entry.get("requirement_key")
            if not req_key:
                continue
            # Find the CRS row that bootstrap_requirement_status_rows
            # created for this loan + requirement_key.
            crs = (
                await db.execute(
                    select(ClientRequirementStatus).where(
                        ClientRequirementStatus.client_id == loan.client_id,
                        ClientRequirementStatus.loan_id == loan.id,
                        ClientRequirementStatus.requirement_key == req_key,
                    )
                )
            ).scalar_one_or_none()
            if crs is None:
                # The CRS bootstrap might've skipped this key if the
                # resolver decided it didn't apply to this loan. Skip.
                log.info(
                    "materialize_pending: no CRS row for key=%s on loan=%s — skipping",
                    req_key, loan.id,
                )
                continue
            if crs.ai_assignment_id is not None:
                # Already assigned (idempotent re-run). Skip.
                continue
            # Funding-locked items: skip silently — agent shouldn't be
            # able to assign these in the wizard, but defense-in-depth.
            cat = (
                await db.execute(
                    select(AICollectionRequirement).where(
                        AICollectionRequirement.requirement_key == req_key
                    )
                )
            ).scalars().first()
            if cat is not None and not cat.can_agent_override:
                log.info(
                    "materialize_pending: skipping funding-locked key=%s",
                    req_key,
                )
                continue
            complete_file_by = _coerce_date(entry.get("complete_file_by")) or crs.due_at
            assignment = AITaskAssignment(
                id=uuid.uuid4(),
                client_requirement_status_id=crs.id,
                owner_type=TaskOwnerType.AI.value,
                instructions=entry.get("instructions") or "",
                instructions_visibility="agent",
                channels=entry.get("channels") or (
                    list(cat.default_channels) if cat and cat.default_channels else ["portal"]
                ),
                cadence=entry.get("cadence") or {
                    "hours_between_attempts": cat.default_cadence_hours if cat else 48,
                    "max_attempts": 3,
                },
                approval_mode=entry.get("approval_mode") or "draft_first",
                complete_file_by=complete_file_by,
                link_url=entry.get("link_url") or (cat.link_url if cat else None),
                link_label=entry.get("link_label") or (cat.link_label if cat else None),
                link_kind=entry.get("link_kind") or (cat.link_kind if cat else None),
                consent_state={},
                next_run_at=datetime.now(timezone.utc),
                created_by=None,
            )
            db.add(assignment)
            await db.flush()
            crs.owner_type = TaskOwnerType.AI.value
            crs.ai_assignment_id = assignment.id
            if complete_file_by is not None:
                crs.due_at = complete_file_by
            created_count += 1

    # Clear the buffer on the realtor plan now that we've materialized.
    realtor_plan.ai_secretary_settings = realtor_settings
    await db.flush()
    log.info(
        "deal_secretary.materialize_pending loan_id=%s materialized=%d "
        "file_outreach_mode=%s",
        loan.id, created_count, file_outreach_mode,
    )
    return created_count
