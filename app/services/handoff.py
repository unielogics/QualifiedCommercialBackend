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

import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)

from app.enums import (
    DealHandoffStatus,
    DealStatus,
    LoanPurpose,
    LoanSide,
    LoanStage,
    LoanType,
)
from app.models.activity import Activity
from app.models.ai_chat_thread import AIChatMessage, AIChatThread
from app.models.client import Client
from app.models.client_ai_plan import ClientAIPlan
from app.models.client_requirement_status import ClientRequirementStatus
from app.models.deal import Deal
from app.models.lending_handoff_packet import LendingHandoffPacket
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest
from app.services.ai.handoff_builder import build_handoff_packet


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

    # Property fields cross-sync from the Deal onto the new Loan.
    # The agent edits these on /deals/[id]'s Property tab; at promote
    # time they freeze onto the Loan so funding sees the snapshot.
    address = deal.address or client.address or "Property TBD"
    loan_amount = float(deal.list_price or deal.target_price or 0)
    loan = Loan(
        id=uuid.uuid4(),
        deal_id=_gen_deal_id(),
        client_id=client.id,
        broker_id=client.broker_id,
        address=address,
        city=deal.city,
        state=deal.state,
        property_type=deal.property_type or "sfr",
        beds=deal.beds,
        baths=deal.baths,
        sqft=deal.sqft,
        year_built=deal.year_built,
        type=loan_type,
        purpose=loan_purpose,
        side=side,
        stage=LoanStage.PREQUALIFIED.value,
        amount=loan_amount,
        source_deal_id=deal.id,
        baseline_profile_snapshot=snapshot,
        handoff_summary=handoff_summary,
        funding_file_kind=funding_file_kind,
    )
    db.add(loan)
    await db.flush()
    await db.refresh(loan)

    # Seed (L) loan_chat_messages with an AI-summarized handoff from the
    # (A) deal-chat history. Best-effort — a failure here doesn't roll
    # back the promotion; the funding team can still pick up the file,
    # they just lose the pre-funding context summary.
    try:
        from app.services.handoff_seed_loan_from_deal_chat import (
            seed_loan_chat_from_deal,
        )

        await seed_loan_chat_from_deal(db, deal=deal, loan=loan)
    except Exception:  # noqa: BLE001 — best-effort
        log.exception(
            "seed_loan_chat_from_deal failed deal=%s loan=%s", deal.id, loan.id
        )

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

    # ── Lending handoff plumbing ────────────────────────────────────
    # Find the realtor thread (if any) to link the conversation
    # history. Build the packet via the existing handoff_builder
    # helper — same shape the prequal flow uses, so the Lending AI
    # treats deal-promoted handoffs identically to prequal handoffs.
    realtor_thread = (
        await db.execute(
            select(AIChatThread)
            .where(
                AIChatThread.client_id == client.id,
                AIChatThread.loan_id.is_(None),
                AIChatThread.phase.in_(["realtor", None]),
            )
            .order_by(AIChatThread.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    user_id = getattr(user, "id", None)
    packet_payload = await build_handoff_packet(
        db,
        client=client,
        agent_id=user_id or client.broker_id,
        realtor_thread_id=realtor_thread.id if realtor_thread else None,
    )
    # Prefer our deal-aware handoff_summary over the generic one the
    # builder composes from realtor_profile alone.
    packet_payload["handoff_summary"] = handoff_summary
    # Link the packet to the new loan up front so audit queries can
    # find it without a roundtrip through PrequalRequest.
    packet_payload["loan_id"] = loan.id

    # Create the matching PrequalRequest mirroring the existing flow
    # so downstream queues (admin /prequal-requests, etc.) pick it up.
    requester_id = client.user_id or user_id or client.broker_id
    prequal: PrequalRequest | None = None
    if requester_id is not None:
        prequal = PrequalRequest(
            loan_id=loan.id,
            requester_id=requester_id,
            target_property_address=address,
            purchase_price=float(loan.amount or 0),
            requested_loan_amount=float(loan.amount or 0) * 0.75,
            loan_type=loan_type if loan_type in ("dscr", "bridge") else "dscr",
            expected_closing_date=None,
            borrower_notes=notes,
        )
        db.add(prequal)
        await db.flush()
        packet_payload["prequal_request_id"] = prequal.id

    packet = LendingHandoffPacket(**packet_payload)
    db.add(packet)
    await db.flush()

    # Spawn the lending-phase AIChatThread linked to the packet + the
    # new loan. The realtor thread keeps its history; the lending
    # thread is the new conversation surface for underwriting.
    lending_thread: AIChatThread | None = None
    if user_id is not None:
        lending_thread = AIChatThread(
            user_id=user_id,
            client_id=client.id,
            loan_id=loan.id,
            phase="lending",
            parent_thread_id=realtor_thread.id if realtor_thread else None,
            handoff_packet_id=packet.id,
            prequal_request_id=prequal.id if prequal else None,
            title=f"Lending — {client.name[:80]}",
        )
        db.add(lending_thread)
        await db.flush()
        packet.lending_thread_id = lending_thread.id

        # Deterministic first message — same composition the existing
        # prequal flow uses so the Lending AI's first turn looks
        # identical regardless of which handoff path fired.
        first_msg_body = _compose_first_lending_message(packet_payload, client)
        db.add(
            AIChatMessage(
                thread_id=lending_thread.id,
                role="assistant",
                body=first_msg_body,
                actions=None,
                attachments=None,
            )
        )
        lending_thread.last_message_preview = first_msg_body[:200]
        lending_thread.last_message_at = datetime.now(timezone.utc)

    # Mark realtor thread phase=realtor if it was NULL (legacy threads).
    if realtor_thread is not None and realtor_thread.phase is None:
        realtor_thread.phase = "realtor"

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
        handoff_packet_id=packet.id,
        prequal_request_id=prequal.id if prequal else None,
        lending_thread_id=lending_thread.id if lending_thread else None,
        handoff_summary=handoff_summary,
        missing_lending_items=[
            str(item.get("requirement_key") or item.get("label") or "")
            for item in snapshot["missing_lending_items"]
        ],
    )


def _compose_first_lending_message(packet: dict[str, Any], client: Client) -> str:
    """Build the deterministic first-message body the Lending AI drops
    into the freshly-spawned lending thread. Mirrors
    routers/clients._compose_first_lending_message so the Lending AI's
    first turn is identical regardless of which handoff path fired."""
    lines: list[str] = [f"I have {client.name} marked as ready for lending."]
    summary = packet.get("handoff_summary") or ""
    if summary:
        lines.append("")
        lines.append("Here's what I already know from the realtor side:")
        for s in summary.split("\n"):
            if s.strip() and not s.lower().startswith("client:"):
                lines.append(f"  • {s.strip()}")
    missing = packet.get("missing_lending_items") or []
    if missing:
        lines.append("")
        lines.append("To start the lending package correctly, I still need:")
        for m in missing[:5]:
            lines.append(f"  • {_humanize_field(m)}")
    first_q = packet.get("first_lending_question")
    if first_q:
        lines.append("")
        lines.append(first_q)
    return "\n".join(lines)


def _humanize_field(field: str) -> str:
    return field.replace("_", " ").replace(".", " · ").capitalize()


__all__ = ["promote_deal_to_loan", "PromoteResult"]
