"""Pre-funding chat → funding-team chat handoff helper.

Drop this helper into app/services/handoff.py (or import it from
there). Call it from inside promote_deal_to_loan() AFTER the new
Loan row has been flushed but BEFORE the function returns. The call
is best-effort — if the AI is unreachable or there's no (A) chat
yet, we skip silently rather than blocking the promotion.

Insertion point in handoff.py:

    db.add(loan)
    await db.flush()
    await db.refresh(loan)

    # NEW — seed (L) with a summary of (A).
    await seed_loan_chat_from_deal(db, deal=deal, loan=loan)

    # existing: try bootstrap_requirement_status_rows ...
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import DealChatRole
from app.models.deal import Deal
from app.models.deal_chat_message import DealChatMessage
from app.models.loan import Loan
from app.models.loan_chat_message import LoanChatMessage
from app.services.ai.bedrock_client import get_client, model_light
from app.services.ai.usage import tracked_messages_create

log = logging.getLogger(__name__)


async def seed_loan_chat_from_deal(
    db: AsyncSession,
    *,
    deal: Deal,
    loan: Loan,
) -> LoanChatMessage | None:
    """Summarize (A) deal-chat → write one broker_internal turn at the
    top of (L). Returns the new LoanChatMessage, or None if there's
    nothing to seed or the AI is unavailable.

    The (A) thread itself is left alone — it remains accessible for
    the broker's ongoing nurture conversation post-promotion. Only
    the funding-team-facing surface (L) gets the handoff context.
    """
    msgs = list(
        (
            await db.execute(
                select(DealChatMessage)
                .where(DealChatMessage.deal_id == deal.id)
                .order_by(DealChatMessage.created_at.asc())
            )
        )
        .scalars()
        .all()
    )
    if not msgs:
        return None

    transcript_lines: list[str] = []
    for m in msgs:
        role_label = {
            DealChatRole.AI: "AI",
            DealChatRole.BROKER: "Agent",
            DealChatRole.BROKER_INTERNAL: "Agent (private)",
            DealChatRole.CLIENT: "Borrower",
            DealChatRole.SUPER_ADMIN: "Operator",
        }.get(m.from_role, str(m.from_role))
        transcript_lines.append(f"[{role_label}] {m.body}")
    transcript = "\n".join(transcript_lines)[:12000]  # cap context size

    summary_prompt = (
        "Below is the full pre-funding conversation between an agent, "
        "the borrower, and the AI on a real-estate deal that just got "
        "promoted to a funding file. Produce a tight handoff brief for "
        "the funding/lending team. Keep it under 200 words. Cover:\n"
        "- Borrower's stated goals + timeline\n"
        "- Property + use case\n"
        "- Anything the borrower already committed to / agreed to\n"
        "- Open questions the funding team needs to resolve\n"
        "- Sensitivities (financing constraints, deadlines, soft spots)\n\n"
        "CONVERSATION:\n"
        f"{transcript}"
    )

    settings = get_settings()
    if not settings.ai_provider_enabled:
        body = (
            "Pre-funding handoff (stub — Bedrock provider disabled):\n\n"
            f"{transcript[:1500]}"
        )
    else:
        try:
            client = get_client()
            resp = await tracked_messages_create(
                db,
                feature="handoff_seed",
                client=client,
                model=model_light(),
                loan_id=loan.id,
                client_id=loan.client_id,
                metadata={"deal_id": str(deal.id)},
                max_tokens=600,
                messages=[{"role": "user", "content": summary_prompt}],
            )
            body = "".join(
                b.text for b in resp.content if getattr(b, "type", None) == "text"
            ).strip()
            if not body:
                body = "(no handoff summary produced)"
            body = (
                "**Pre-funding handoff** — summary of the (A) agent-chat "
                "before this deal was promoted to a funding file.\n\n"
                f"{body}"
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("seed_loan_chat_from_deal: AI summary failed: %s", exc)
            body = (
                "**Pre-funding handoff** — AI summary unavailable; raw "
                "transcript follows.\n\n"
                f"{transcript[:2000]}"
            )

    seeded = LoanChatMessage(
        loan_id=loan.id,
        from_role=DealChatRole.BROKER_INTERNAL,
        from_user_id=None,
        body=body,
        client_visible=False,
    )
    db.add(seeded)
    await db.flush()
    await db.refresh(seeded)
    return seeded
