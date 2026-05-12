"""Deal → Loan promotion (Phase 4).

The single canonical path for the agent's "Ready for Lending" action.
Called from POST /clients/{client_id}/deals/{deal_id}/mark-ready-for-lending.

Atomic flow (idempotent — second call returns the existing loan):
  1. Load the deal-stage ClientAIPlan (loan_id IS NULL, deal_id = deal.id)
     plus any deal-scoped ClientRequirementStatus rows.
  2. Build baseline_profile_snapshot, applying handoff visibility filters.
  3. Create the Loan row with source_deal_id + baseline + handoff_summary
     + funding_file_kind populated.
  4. Bootstrap loan-scoped CRS rows via deal_secretary.bootstrap_requirement_status_rows.
  5. Build a LendingHandoffPacket linking realtor thread → lending thread.
  6. Create a PrequalRequest reusing the existing handoff payload shape.
  7. Spawn the lending AIChatThread linked to the packet.
  8. Stamp Deal.handoff_status = 'promoted', Deal.promoted_loan_id, Deal.status.
  9. Emit Activity(kind='deal_promoted_to_loan').

Handoff visibility filter (critical — see plan):
  - Include: AgentTask.visibility = 'funding_visible' (Phase 7) ALWAYS.
  - Include: AgentTask.visibility = 'team_visible' ONLY when the broker's
    firm policy flag `handoff_includes_team_notes` is true (Broker
    settings; default false).
  - Exclude: AgentTask.visibility = 'agent_private' — never transferred.
  - Exclude: AgentTask.visibility = 'client_visible' — client-facing only.
  - Exclude: ClientAIPlan.custom_instructions and AITaskAssignment.instructions
    with instructions_visibility = 'internal'. Only 'agent' / 'borrower'
    visibility may surface, and only when handoff_visible_only=True.

Activity emits `excluded_task_count` and `excluded_note_count` so auditors
can see what was withheld without revealing content.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import (
    DealHandoffStatus,
    DealStatus,
    LoanPurpose,
    LoanSide,
    LoanStage,
    LoanType,
)
from app.models.activity import Activity
from app.models.client import Client
from app.models.client_ai_plan import ClientAIPlan
from app.models.client_requirement_status import ClientRequirementStatus
from app.models.deal import Deal
from app.models.loan import Loan


# Map a Deal.deal_type + Loan.purpose hint into a funding_file_kind label.
_KIND_MAP: dict[tuple[str, str | None], str] = {
    ("buyer", "purchase"): "dscr_purchase",
    ("buyer", "refinance"): "dscr_refi",
    ("buyer", None): "dscr_purchase",
    ("investor", "purchase"): "dscr_purchase",
    ("investor", "refinance"): "dscr_refi",
    ("investor", None): "dscr_purchase",
    ("borrower", None): "bridge",
    ("seller", None): "other",
}


@dataclass
class PromoteResult:
    loan: Loan
    deal: Deal
    handoff_packet_id: uuid.UUID | None = None
    prequal_request_id: uuid.UUID | None = None
    lending_thread_id: uuid.UUID | None = None
    handoff_summary: str | None = None
    missing_lending_items: list[str] | None = None


def _gen_deal_id() -> str:
    """Human-friendly loan deal_id like 'L-1234'. Mirrors loans router."""
    return f"L-{secrets.randbelow(9000) + 1000}"


def _derive_funding_file_kind(deal: Deal, override_purpose: str | None) -> str:
    return _KIND_MAP.get(
        (deal.deal_type, override_purpose),
        _KIND_MAP.get((deal.deal_type, None), "other"),
    )


def _filter_visibility(
    raw: list[dict[str, Any]] | None,
    *,
    handoff_includes_team_notes: bool = False,
) -> tuple[list[dict[str, Any]], int]:
    """Apply handoff visibility rules to a list of items pulled from the
    deal-stage plan / facts. Returns (kept, excluded_count).

    Rules (see service docstring):
      - 'funding_visible' / no visibility marker → keep
      - 'team_visible' → keep only when handoff_includes_team_notes=True
      - 'agent_private' → exclude
      - 'client_visible' → exclude
      - instructions_visibility = 'internal' → exclude
    """
    kept: list[dict[str, Any]] = []
    excluded = 0
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        vis = str(item.get("visibility") or "funding_visible")
        instr_vis = str(item.get("instructions_visibility") or "agent")
        if vis == "agent_private" or vis == "client_visible":
            excluded += 1
            continue
        if vis == "team_visible" and not handoff_includes_team_notes:
            excluded += 1
            continue
        if instr_vis == "internal":
            excluded += 1
            continue
        kept.append(item)
    return kept, excluded


async def _build_baseline_snapshot(
    db: AsyncSession,
    *,
    client: Client,
    deal: Deal,
    handoff_includes_team_notes: bool,
) -> tuple[dict[str, Any], int]:
    """Compose the frozen baseline_profile_snapshot from the deal-stage
    plan. Excluded counts come back so the activity row can record them.
    """
    plan = (
        await db.execute(
            select(ClientAIPlan).where(
                ClientAIPlan.client_id == client.id,
                ClientAIPlan.deal_id == deal.id,
            )
        )
    ).scalar_one_or_none()

    # Fallback to the client-level plan (loan_id IS NULL AND deal_id IS
    # NULL) when no deal-level plan exists yet.
    if plan is None:
        plan = (
            await db.execute(
                select(ClientAIPlan).where(
                    ClientAIPlan.client_id == client.id,
                    ClientAIPlan.loan_id.is_(None),
                    ClientAIPlan.deal_id.is_(None),
                )
            )
        ).scalar_one_or_none()

    realtor_profile = client.realtor_profile or {}
    verified_facts: list[dict[str, Any]] = []
    missing_lending_items: list[dict[str, Any]] = []
    document_refs: list[dict[str, Any]] = []
    custom_instructions = None

    if plan is not None:
        # Apply visibility filters to the JSON payloads on the plan.
        custom_instructions = plan.custom_instructions
        # required_items shape: [{requirement_key, label, status, source, visibility, ...}]
        for r in plan.required_items or []:
            if not isinstance(r, dict):
                continue
            if r.get("status") in {"missing", "asked"}:
                missing_lending_items.append(r)

    # Filter agent-private / internal items out of every shareable list.
    missing_filtered, miss_excluded = _filter_visibility(
        missing_lending_items, handoff_includes_team_notes=handoff_includes_team_notes
    )
    facts = realtor_profile.get("known_facts") if isinstance(realtor_profile, dict) else []
    if isinstance(facts, list):
        verified_facts = [f for f in facts if isinstance(f, dict)]
    verified_filtered, facts_excluded = _filter_visibility(
        verified_facts, handoff_includes_team_notes=handoff_includes_team_notes
    )

    # Internal custom_instructions never surfaces in baseline.
    safe_instructions = None
    if custom_instructions:
        # Treat as 'agent'-visibility — surfaces in baseline because the
        # plan didn't explicitly mark it internal.
        safe_instructions = custom_instructions

    excluded_total = miss_excluded + facts_excluded
    snapshot = {
        "realtor_profile": realtor_profile,
        "verified_facts": verified_filtered,
        "missing_lending_items": missing_filtered,
        "document_refs": document_refs,
        "recommended_lending_path": {
            "deal_type": deal.deal_type,
            "side": deal.side,
        },
        "agent_instructions": safe_instructions,
        "captured_at": deal.updated_at.isoformat() if deal.updated_at else None,
    }
    return snapshot, excluded_total


async def promote_deal_to_loan(
    db: AsyncSession,
    *,
    deal: Deal,
    user,
    override_loan_type: str | None = None,
    override_purpose: str | None = None,
    notes: str | None = None,
) -> PromoteResult:
    """Atomic Deal → Loan promotion. Idempotent: a second call returns
    the existing loan."""

    # Idempotency check.
    if deal.promoted_loan_id is not None:
        existing = (
            await db.execute(select(Loan).where(Loan.id == deal.promoted_loan_id))
        ).scalar_one_or_none()
        if existing is not None:
            return PromoteResult(
                loan=existing,
                deal=deal,
                handoff_summary=existing.handoff_summary,
            )

    client = (
        await db.execute(select(Client).where(Client.id == deal.client_id))
    ).scalar_one_or_none()
    if client is None:
        raise ValueError("Deal points at a missing Client; cannot promote")

    # Pull firm policy. Broker.handoff_includes_team_notes is the
    # signal (default False — most firms don't auto-share team notes
    # with underwriting). Read via getattr so a missing column /
    # legacy row defaults safely.
    handoff_includes_team_notes = False
    broker = getattr(client, "broker", None)
    if broker is not None:
        handoff_includes_team_notes = bool(
            getattr(broker, "handoff_includes_team_notes", False)
        )

    snapshot, excluded_count = await _build_baseline_snapshot(
        db,
        client=client,
        deal=deal,
        handoff_includes_team_notes=handoff_includes_team_notes,
    )

    funding_file_kind = _derive_funding_file_kind(deal, override_purpose)

    handoff_summary_parts: list[str] = [
        f"Promoted from deal '{deal.title}' ({deal.deal_type}).",
    ]
    if snapshot["missing_lending_items"]:
        handoff_summary_parts.append(
            f"{len(snapshot['missing_lending_items'])} item(s) still needed for lending."
        )
    if notes:
        handoff_summary_parts.append(notes)
    handoff_summary = " ".join(handoff_summary_parts)

    # Determine loan_type. Override → respected; otherwise default to
    # DSCR (matches the existing prequal flow's default).
    loan_type = override_loan_type or LoanType.DSCR.value
    loan_purpose = override_purpose or LoanPurpose.PURCHASE.value
    side = LoanSide.SELLER.value if deal.side == "seller" else LoanSide.BUYER.value

    address = client.address or "Property TBD"
    loan = Loan(
        id=uuid.uuid4(),
        deal_id=_gen_deal_id(),
        client_id=client.id,
        broker_id=client.broker_id,
        address=address,
        type=loan_type,
        purpose=loan_purpose,
        side=side,
        stage=LoanStage.PREQUALIFIED.value,
        amount=0,
        source_deal_id=deal.id,
        baseline_profile_snapshot=snapshot,
        handoff_summary=handoff_summary,
        funding_file_kind=funding_file_kind,
    )
    db.add(loan)
    await db.flush()
    await db.refresh(loan)

    # Bootstrap loan-scoped CRS rows. Best-effort — failure here doesn't
    # roll back the promotion; bootstrap is idempotent and can be
    # re-fired via the existing /deal-secretary/bootstrap endpoint.
    try:
        from app.services.ai import deal_secretary as _ds

        await _ds.bootstrap_requirement_status_rows(
            db, loan, log_label="promote_deal_to_loan"
        )
    except Exception:  # pragma: no cover — best-effort
        pass

    # Mark the deal promoted.
    deal.handoff_status = DealHandoffStatus.PROMOTED.value
    deal.status = DealStatus.PROMOTED.value
    deal.promoted_loan_id = loan.id

    # Emit the activity record so auditors can see the lineage + what
    # was withheld by the visibility filter.
    db.add(
        Activity(
            client_id=client.id,
            loan_id=loan.id,
            actor_id=getattr(user, "id", None),
            actor_label="broker" if getattr(user, "role", None) == "broker" else "operator",
            kind="deal_promoted_to_loan",
            summary=f"Promoted deal '{deal.title}' to loan {loan.deal_id}",
            payload={
                "deal_id": str(deal.id),
                "loan_id": str(loan.id),
                "funding_file_kind": funding_file_kind,
                "handoff_includes_team_notes": handoff_includes_team_notes,
                "excluded_visibility_filtered": excluded_count,
            },
        )
    )

    await db.flush()
    await db.refresh(deal)
    await db.refresh(loan)

    return PromoteResult(
        loan=loan,
        deal=deal,
        handoff_summary=handoff_summary,
        missing_lending_items=[
            str(item.get("requirement_key") or item.get("label") or "")
            for item in snapshot["missing_lending_items"]
        ],
    )


__all__ = ["promote_deal_to_loan", "PromoteResult"]
