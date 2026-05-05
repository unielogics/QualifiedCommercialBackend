"""pgvector-backed Center of Truth.

Every router write should call `log_event(deal_id, kind, content, payload)`.
Embeddings use Anthropic's text-embedding fallback (Voyage) when keys are
present; in dev mode without keys, embeddings are zero-vectors so RAG returns
nothing — fine for early development; flip to real embeddings before AI go-live.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.vector_log import VectorLog

EMBEDDING_DIM = 1536
_ZERO_VECTOR = [0.0] * EMBEDDING_DIM


async def log_event(
    db: AsyncSession,
    *,
    loan_id: str | None = None,
    deal_id: str | None = None,
    kind: str,
    content: str,
    payload: dict[str, Any] | None = None,
    embedding: list[float] | None = None,
) -> VectorLog:
    """Append-only — never updates."""
    log = VectorLog(
        loan_id=loan_id,
        deal_id=deal_id,
        kind=kind,
        content=content,
        payload=payload,
        embedding=embedding or _ZERO_VECTOR,
    )
    db.add(log)
    await db.flush()
    return log


async def retrieve_for_deal(
    db: AsyncSession,
    deal_id: str,
    query_embedding: list[float] | None = None,
    limit: int = 8,
) -> list[VectorLog]:
    """Return the most-recent N events for a deal.

    When query_embedding is supplied (real Anthropic/Voyage call), do similarity
    search via pgvector's `<->` operator. Otherwise fall back to recency.
    """
    if query_embedding:
        stmt = (
            select(VectorLog)
            .where(VectorLog.deal_id == deal_id)
            .order_by(VectorLog.embedding.l2_distance(query_embedding))
            .limit(limit)
        )
    else:
        stmt = (
            select(VectorLog)
            .where(VectorLog.deal_id == deal_id)
            .order_by(VectorLog.occurred_at.desc())
            .limit(limit)
        )
    return list((await db.execute(stmt)).scalars().all())
