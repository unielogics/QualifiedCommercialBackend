"""Thin RAG layer — retrieve recent deal events, format as system context."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.ai.vector_store import retrieve_for_deal


async def deal_context(db: AsyncSession, deal_id: str, limit: int = 12) -> str:
    """Returns a text block summarising the last N events for a deal."""
    rows = await retrieve_for_deal(db, deal_id, limit=limit)
    if not rows:
        return f"(No prior events recorded for deal {deal_id}.)"
    lines = [f"--- Deal {deal_id} — Center of Truth ({len(rows)} events) ---"]
    for r in rows:
        ts = r.occurred_at.isoformat() if r.occurred_at else "?"
        lines.append(f"[{ts}] {r.kind}: {r.content}")
    return "\n".join(lines)
