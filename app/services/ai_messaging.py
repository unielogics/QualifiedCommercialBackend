"""AI auto-messaging — system-side post into the user's chat threads.

Used by:
  - document_scanner: drops a one-line reaction after a vision scan.
  - loan_intake_automation: doc-reminder cron tier-1 nudges.
  - future scheduler-driven reactions.

Find-or-create semantics match `POST /ai/chat/threads/find-or-create`
— one canonical thread per (user_id, loan_id), enforced by the
partial unique idx in alembic 0017. Account threads (loan_id=NULL)
aren't constrained by the idx but lazy-create canonical too via the
`first matching account thread` lookup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.ai_chat_thread import AIChatMessage, AIChatThread
from app.models.loan import Loan

log = logging.getLogger(__name__)


def _preview(text: str, limit: int = 200) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


async def _find_or_create_thread(
    db: AsyncSession,
    *,
    user_id: UUID,
    loan_id: UUID | None,
    title_hint: str | None = None,
) -> AIChatThread:
    """Idempotent. Returns the canonical thread for (user, loan)."""
    stmt = (
        select(AIChatThread)
        .options(selectinload(AIChatThread.loan))
        .where(AIChatThread.user_id == user_id)
    )
    if loan_id is None:
        stmt = stmt.where(AIChatThread.loan_id.is_(None))
        stmt = stmt.order_by(
            AIChatThread.last_message_at.desc().nullslast(),
            AIChatThread.created_at.desc(),
        )
    else:
        stmt = stmt.where(AIChatThread.loan_id == loan_id)
    thread = (await db.execute(stmt)).scalars().first()
    if thread is not None:
        return thread

    # Build a default title
    if title_hint:
        title = title_hint[:120]
    elif loan_id is None:
        title = "Account questions"
    else:
        loan = await db.get(Loan, loan_id)
        if loan is None:
            raise ValueError(f"Loan {loan_id} not found")
        title = f"{loan.deal_id} — {loan.address[:80]}"
    thread = AIChatThread(user_id=user_id, loan_id=loan_id, title=title)
    db.add(thread)
    await db.flush()
    return thread


async def post_ai_message(
    db: AsyncSession,
    *,
    user_id: UUID,
    loan_id: UUID | None,
    body: str,
    title_hint: str | None = None,
    actions: list[dict] | None = None,
    attachments: list[dict] | None = None,
) -> AIChatThread:
    """Append an assistant-role message to the (user, loan) thread.
    Find-or-creates the thread, updates last_message_preview /
    last_message_at, returns the thread.

    `actions` and `attachments` ride on the message (alembic 0020):
    `actions` are CTAs the frontend renders as buttons (kickoff
    "Upload Bank Statements" pills, post-anchor confirmations); the
    schema is enforced by `app.routers.ai.ChatAction`. Empty / None
    means a plain text message — same as before.

    Caller is responsible for committing the surrounding session —
    this function only flushes (so it composes cleanly inside a
    larger transaction like the scanner's per-doc flow)."""
    if not body or not body.strip():
        raise ValueError("post_ai_message: body is empty")

    thread = await _find_or_create_thread(
        db, user_id=user_id, loan_id=loan_id, title_hint=title_hint
    )
    msg = AIChatMessage(
        thread_id=thread.id,
        role="assistant",
        body=body.strip(),
        actions=actions or None,
        attachments=attachments or None,
    )
    db.add(msg)
    thread.last_message_preview = _preview(body)
    thread.last_message_at = datetime.now(timezone.utc)
    await db.flush()
    log.info(
        "post_ai_message: user=%s loan=%s thread=%s body_len=%d actions=%d",
        user_id, loan_id, thread.id, len(body), len(actions or []),
    )
    return thread
