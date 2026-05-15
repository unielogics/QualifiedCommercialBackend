"""Resolve chat-message sender display names.

Chat rows store only from_user_id + from_role. The frontend wants to
render "Full Name (Agent)" / "Full Name (Operator)" and "Smart
Assistant" for AI. We resolve from_user_id → users.name, falling back
to brokers.display_name when the user's name is blank. AI rows
(from_user_id is None) resolve to None and the frontend shows
"Smart Assistant".

Batched (one users query + one brokers query) to avoid N+1 across a
whole thread.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.broker import Broker
from app.models.document import Document
from app.models.user import User
from app.schemas.loan_workspace import ChatAttachmentRead, ChatMessageRead


def _presign_doc(s3_key: str | None) -> str | None:
    settings = get_settings()
    if not (settings.s3_bucket and s3_key):
        return None
    import boto3

    kwargs: dict[str, str] = {"region_name": settings.aws_region}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        kwargs["aws_access_key_id"] = settings.aws_access_key_id
        kwargs["aws_secret_access_key"] = settings.aws_secret_access_key
    return boto3.client("s3", **kwargs).generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": s3_key},
        ExpiresIn=3600,
    )


async def resolve_chat_names(
    db: AsyncSession, user_ids: set[UUID | None]
) -> dict[UUID, str]:
    ids = {u for u in user_ids if u is not None}
    if not ids:
        return {}
    out: dict[UUID, str] = {}
    rows = (
        await db.execute(select(User.id, User.name).where(User.id.in_(ids)))
    ).all()
    for uid, name in rows:
        n = (name or "").strip()
        if n:
            out[uid] = n
    missing = ids - set(out.keys())
    if missing:
        brows = (
            await db.execute(
                select(Broker.user_id, Broker.display_name).where(
                    Broker.user_id.in_(missing)
                )
            )
        ).all()
        for uid, dn in brows:
            d = (dn or "").strip()
            if d:
                out[uid] = d
    return out


async def serialize_chat(db: AsyncSession, msgs: list) -> list[ChatMessageRead]:
    """ORM chat rows → ChatMessageRead[] with from_name populated.

    Works for both LoanChatMessage (loan_id) and DealChatMessage
    (deal_id) — loan_id is optional on the schema.
    """
    reads = [ChatMessageRead.model_validate(m) for m in msgs]
    names = await resolve_chat_names(
        db, {r.from_user_id for r in reads if r.from_user_id is not None}
    )
    # Resolve attachments (only the rows that have one).
    doc_ids = {
        getattr(m, "attachment_document_id", None)
        for m in msgs
        if getattr(m, "attachment_document_id", None) is not None
    }
    docs: dict = {}
    if doc_ids:
        rows = (
            await db.execute(select(Document).where(Document.id.in_(doc_ids)))
        ).scalars().all()
        docs = {d.id: d for d in rows}
    for r, m in zip(reads, msgs):
        if r.from_user_id is not None:
            r.from_name = names.get(r.from_user_id)
        adid = getattr(m, "attachment_document_id", None)
        if adid is not None and adid in docs:
            d = docs[adid]
            r.attachment = ChatAttachmentRead(
                document_id=d.id,
                name=d.name,
                mime=getattr(d, "mime", None) or getattr(d, "content_type", None),
                url=_presign_doc(getattr(d, "s3_key", None)),
            )
    return reads


async def serialize_chat_one(db: AsyncSession, msg) -> ChatMessageRead:
    return (await serialize_chat(db, [msg]))[0]
