"""Shared "spawn the lending-phase AI thread" primitive.

Two independent handoff builders both need this exact sequence — creating
the lending AIChatThread, backfilling LendingHandoffPacket.lending_thread_id,
and dropping the deterministic first AI message:

  - services/handoff.promote_deal_to_loan (Deal -> Loan, the current/primary
    "Ready for Lending" path — reads a Deal + ClientAIPlan)
  - routers/clients.request_prequalification (legacy pre-Deal
    Client.lead_promotion_status path — reads Client.lead_intake)

The two builders remain intentionally separate (different input shapes),
but this shared helper closes the most likely source of future drift: the
thread-spawn shape silently diverging between them.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_chat_thread import AIChatMessage, AIChatThread

if TYPE_CHECKING:
    from app.models.client import Client
    from app.models.lending_handoff_packet import LendingHandoffPacket


async def spawn_lending_thread(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    client: Client,
    loan_id: uuid.UUID | None,
    packet: LendingHandoffPacket,
    packet_payload: dict[str, Any],
    realtor_thread_id: uuid.UUID | None,
    prequal_request_id: uuid.UUID | None,
) -> AIChatThread | None:
    """Creates the lending-phase AIChatThread, backfills
    packet.lending_thread_id, and drops the deterministic first AI message.
    Returns None (no-op) when there's no user to attribute the thread to —
    mirrors handoff.py's existing "only spawn when user_id is present"
    behavior."""
    if user_id is None:
        return None
    lending_thread = AIChatThread(
        user_id=user_id,
        client_id=client.id,
        loan_id=loan_id,
        phase="lending",
        parent_thread_id=realtor_thread_id,
        handoff_packet_id=packet.id,
        prequal_request_id=prequal_request_id,
        title=f"Lending — {client.name[:80]}",
    )
    db.add(lending_thread)
    await db.flush()

    packet.lending_thread_id = lending_thread.id

    first_msg_body = compose_first_lending_message(packet_payload, client)
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
    return lending_thread


def compose_first_lending_message(packet: dict[str, Any], client: Client) -> str:
    """Build the deterministic first-message body the Lending AI drops into
    the freshly-spawned lending thread. Demonstrates memory inheritance —
    lists what we know, lists what's missing, asks the highest-leverage
    first question. Identical for both handoff paths."""
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
            lines.append(f"  • {humanize_field(m)}")
    first_q = packet.get("first_lending_question")
    if first_q:
        lines.append("")
        lines.append(first_q)
    return "\n".join(lines)


def humanize_field(field: str) -> str:
    return field.replace("_", " ").replace(".", " · ").capitalize()
