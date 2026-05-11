"""Agent knowledge — PDF parsing + system-prompt injection.

The agent uploads PDFs (talking points, product sheets, FAQs) on
/agent-settings/ai → Knowledge & Voice. We extract plain text on the
upload-complete path and stash it on the row. The chat assembly layer
calls `load_agent_knowledge(agent_user_id)` to concatenate ready
documents + the free-text FAQ from the agent's playbook into a single
block prepended to the system prompt.

V1 is intentionally simple — no embeddings, no chunking, no retrieval.
Every chat call gets every ready document, truncated to the budget
below. Small N (agents have a handful of small docs), so cost stays
trivial. When the volume grows past a few thousand tokens per agent,
swap in a proper retrieval step in this same function and the chat
context call site keeps working.

Truncation safety: 32k characters ≈ 8k tokens, well under any model's
window even when stacked with all the other context blocks. The
truncation point is per-document, then again on the joined block, so
one giant upload can't drown out everything else."""

from __future__ import annotations

import io
import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.ai_knowledge_document import AIKnowledgeDocument
from app.services.ai.agent_settings import load_agent_cadence_rules

log = logging.getLogger(__name__)

# Per-document and aggregate budgets. Generous — the AI context layer
# has plenty of room and we'd rather pass the whole agent FAQ than
# clip the last paragraph of a 2-page product sheet.
MAX_DOC_CHARS = 12_000
MAX_TOTAL_CHARS = 32_000


def extract_text_from_bytes(content_type: str, body: bytes) -> str:
    """Best-effort plain-text extraction. PDFs go through pypdf;
    text/plain and text/markdown pass through; anything else errors —
    the upload-complete handler catches and writes status='failed'.

    pypdf raises a variety of exceptions on malformed files; we let
    them bubble so the caller writes a useful `error` field."""
    if content_type == "application/pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(body))
        pages: list[str] = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception as exc:  # noqa: BLE001
                log.warning("pypdf page extract failed: %s", exc)
                pages.append("")
        return "\n\n".join(pages).strip()
    if content_type in {"text/plain", "text/markdown"}:
        try:
            return body.decode("utf-8").strip()
        except UnicodeDecodeError:
            return body.decode("utf-8", errors="replace").strip()
    raise ValueError(f"Unsupported content_type for knowledge document: {content_type!r}")


def _fetch_s3_object_bytes(s3_key: str) -> bytes:
    """Sync S3 GET — used in parse-on-complete. Uses the same boto3
    client pattern as document_zip / prequal_pdf so the credential
    chain stays consistent."""
    import boto3

    settings = get_settings()
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        region_name=settings.aws_region,
    )
    obj = s3.get_object(Bucket=settings.s3_bucket, Key=s3_key)
    return obj["Body"].read()


def _delete_s3_object(s3_key: str) -> None:
    """Hard-delete the S3 object. Called from the soft-delete endpoint —
    keeping the row but cleaning up the bytes is the friendly compromise."""
    import boto3

    settings = get_settings()
    s3 = boto3.client(
        "s3",
        aws_access_key_id=settings.aws_access_key_id or None,
        aws_secret_access_key=settings.aws_secret_access_key or None,
        region_name=settings.aws_region,
    )
    try:
        s3.delete_object(Bucket=settings.s3_bucket, Key=s3_key)
    except Exception as exc:  # noqa: BLE001
        log.warning("s3 delete failed key=%s: %s", s3_key, exc)


async def parse_document_inline(
    db: AsyncSession,
    doc: AIKnowledgeDocument,
) -> None:
    """Fetch from S3, extract text, write parsed_text + status='ready'
    in one transaction. On failure, status='failed' + error text. Safe
    to retry — overwrites parsed_text + error on each call."""
    doc.status = "parsing"
    doc.error = None
    await db.flush()
    try:
        body = _fetch_s3_object_bytes(doc.s3_key)
        text = extract_text_from_bytes(doc.content_type, body)
        if len(text) > MAX_DOC_CHARS:
            text = text[:MAX_DOC_CHARS]
        doc.parsed_text = text
        doc.status = "ready"
    except Exception as exc:  # noqa: BLE001
        doc.status = "failed"
        doc.error = str(exc)[:1000]
        log.exception("knowledge.parse_failed doc=%s", doc.id)
    await db.flush()


async def load_agent_knowledge(
    db: AsyncSession,
    agent_user_id: UUID | None,
) -> str:
    """Return the agent's combined knowledge block — FAQ text from the
    playbook's `rules.knowledge.faq_text` + every ready document's
    parsed_text, joined with headers. Empty string when nothing is
    configured so callers can do `if kb: ...`.

    Soft-deleted rows and non-ready statuses are skipped. Final block
    is capped at MAX_TOTAL_CHARS so the prompt stays predictable."""
    if agent_user_id is None:
        return ""

    parts: list[str] = []

    rules = await load_agent_cadence_rules(db, agent_user_id)
    knowledge = (rules or {}).get("knowledge") if isinstance(rules, dict) else None
    faq_text = ""
    if isinstance(knowledge, dict):
        v = knowledge.get("faq_text")
        if isinstance(v, str):
            faq_text = v.strip()
    if faq_text:
        if len(faq_text) > MAX_DOC_CHARS:
            faq_text = faq_text[:MAX_DOC_CHARS]
        parts.append(f"### Agent FAQ\n{faq_text}")

    docs = (
        await db.execute(
            select(AIKnowledgeDocument)
            .where(
                AIKnowledgeDocument.agent_user_id == agent_user_id,
                AIKnowledgeDocument.status == "ready",
                AIKnowledgeDocument.deleted_at.is_(None),
            )
            .order_by(AIKnowledgeDocument.created_at.desc())
        )
    ).scalars().all()
    for doc in docs:
        if not doc.parsed_text:
            continue
        snippet = doc.parsed_text
        if len(snippet) > MAX_DOC_CHARS:
            snippet = snippet[:MAX_DOC_CHARS]
        parts.append(f"### {doc.filename}\n{snippet}")

    if not parts:
        return ""

    block = "\n\n".join(parts)
    if len(block) > MAX_TOTAL_CHARS:
        block = block[:MAX_TOTAL_CHARS]
    return block


def serialize(doc: AIKnowledgeDocument) -> dict[str, Any]:
    return {
        "id": str(doc.id),
        "filename": doc.filename,
        "content_type": doc.content_type,
        "size_bytes": doc.size_bytes,
        "status": doc.status,
        "error": doc.error,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
    }
