"""Contradiction detector — runs after every document analysis.

Compares a fresh `document_analysis_results.extracted_facts` against:

  - Other documents on the same client/loan (via prior
    document_analysis_results).
  - The Realtor profile's known_facts + buyer/seller_profile.
  - client_requirement_status notes.

Uses `app.services.ai.fact_priority` to decide whether the new
document silently wins (higher trust) or whether to surface a
ChatAction asking the human to resolve.

Writes back:
  - `document_analysis_results.issues`: appended issue rows.
  - `document_analysis_results.recommended_action`: e.g.
    `confirm_update_purchase_price`, `request_correct_doc`.
  - `ai_audit_events`: one `document_conflict_detected` row per issue.
  - Returns a list of ChatAction dicts the caller appends to the
    chat thread (so the agent or borrower can resolve in-line).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.client import Client
from app.models.document import Document
from app.models.document_analysis_result import DocumentAnalysisResult
from app.services.ai.audit import record_event
from app.services.ai.fact_priority import (
    FactCandidate,
    SOURCE_PRIORITY,
    resolve_fact_conflict,
)

log = logging.getLogger(__name__)


# Fields we actively compare across documents + chat.
_COMPARABLE_FIELDS = {
    "buyer_name", "owner_name",
    "purchase_price", "sale_price",
    "property_address",
    "borrower_name", "borrower_entity_name",
    "loan_amount",
    "ltv", "dscr",
    "annual_property_taxes",
    "monthly_rent",
    "insurance_annual_premium",
}


async def detect_and_record(
    db: AsyncSession,
    *,
    analysis: DocumentAnalysisResult,
) -> list[dict[str, Any]]:
    """Run contradiction detection for one freshly-landed analysis row.

    Returns a list of ChatAction dicts the caller appends to the
    relevant chat thread."""
    document = await db.get(Document, analysis.document_id)
    if document is None or not analysis.extracted_facts:
        return []

    client_id = await _client_id_for_document(db, document)
    if client_id is None:
        return []

    candidates_by_field = await _collect_candidates_for_client(db, client_id, exclude_analysis_id=analysis.id)
    new_facts = analysis.extracted_facts or {}
    issues: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    recommended_action: str | None = None

    for field, new_value in new_facts.items():
        if field not in _COMPARABLE_FIELDS:
            continue
        if new_value in (None, "", 0):
            continue

        new_cand = FactCandidate(
            field=field,
            value=new_value,
            source="verified_document",
            evidence_id=str(document.id),
        )
        existing = candidates_by_field.get(field, [])
        all_candidates = existing + [new_cand]
        res = resolve_fact_conflict(all_candidates)
        if res is None or not res.has_conflict:
            continue

        # We have a real conflict. Build an issue row + a ChatAction.
        winning_score = SOURCE_PRIORITY.get(res.winning_source, 0)
        new_score = SOURCE_PRIORITY.get(new_cand.source, 0)
        # If the NEW doc is the winner, we recommend updating downstream
        # state; otherwise the new doc loses and we recommend asking
        # for a correct version.
        if winning_score == new_score and res.winning_value == new_cand.value:
            recommended_action = recommended_action or f"confirm_update_{field}"
            actions.append({
                "kind": "confirm_document_routing",
                "field": field,
                "winning_value": new_cand.value,
                "winning_source": new_cand.source,
                "previous_value": next((c.value for c in res.conflicting), None),
                "previous_source": next((c.source for c in res.conflicting), None),
                "buttons": [
                    {"label": f"Update {field} to {_fmt(new_cand.value)}", "action_id": f"update_field_from_doc:{field}"},
                    {"label": f"Keep {_fmt(next((c.value for c in res.conflicting), None))}", "action_id": f"confirm_field_chat:{field}"},
                    {"label": "Ask borrower", "action_id": f"request_borrower_confirm:{field}"},
                ],
            })
        else:
            recommended_action = recommended_action or "request_correct_doc"
            actions.append({
                "kind": "confirm_document_routing",
                "field": field,
                "winning_value": res.winning_value,
                "winning_source": res.winning_source,
                "rejected_value": new_cand.value,
                "rejected_source": new_cand.source,
                "buttons": [
                    {"label": "Keep existing — request corrected doc", "action_id": "request_correct_doc"},
                    {"label": "Override with this doc anyway", "action_id": f"update_field_from_doc:{field}"},
                ],
            })

        issues.append({
            "type": "fact_conflict",
            "field": field,
            "winning_value": _scalarize(res.winning_value),
            "winning_source": res.winning_source,
            "conflicts": [
                {"value": _scalarize(c.value), "source": c.source, "evidence_id": c.evidence_id}
                for c in res.conflicting
            ],
            "severity": "high" if field in ("purchase_price", "loan_amount", "borrower_name") else "medium",
        })

        await record_event(
            db,
            event_type="document_conflict_detected",
            actor_type="ai",
            client_id=client_id,
            requirement_key=field,
            payload={
                "document_id": str(document.id),
                "field": field,
                "winning_value": _scalarize(res.winning_value),
                "winning_source": res.winning_source,
            },
        )

    if issues:
        existing_issues = list(analysis.issues or [])
        analysis.issues = existing_issues + issues
        analysis.recommended_action = recommended_action
        await db.flush()
    return actions


# ── Helpers ────────────────────────────────────────────────────────


async def _client_id_for_document(db: AsyncSession, document: Document) -> UUID | None:
    """Documents are loan-scoped; walk up to the loan to find the
    client. Falls back to None if the document isn't attached to a
    loan (e.g. mid-intake)."""
    if document.loan_id is None:
        return None
    from app.models.loan import Loan
    loan = await db.get(Loan, document.loan_id)
    if loan is None:
        return None
    return loan.client_id


async def _collect_candidates_for_client(
    db: AsyncSession,
    client_id: UUID,
    *,
    exclude_analysis_id: UUID | None = None,
) -> dict[str, list[FactCandidate]]:
    """Walk every prior `document_analysis_results` for this client +
    fold in the realtor_profile. Returns a dict keyed by field with
    every known candidate for that field."""
    out: dict[str, list[FactCandidate]] = {}

    # Prior docs: join via Document → Loan.
    from app.models.loan import Loan
    rows = (await db.execute(
        select(DocumentAnalysisResult, Document)
        .join(Document, Document.id == DocumentAnalysisResult.document_id)
        .join(Loan, Loan.id == Document.loan_id)
        .where(Loan.client_id == client_id)
    )).all()
    for an, doc in rows:
        if exclude_analysis_id is not None and an.id == exclude_analysis_id:
            continue
        for k, v in (an.extracted_facts or {}).items():
            if k not in _COMPARABLE_FIELDS or v in (None, "", 0):
                continue
            out.setdefault(k, []).append(FactCandidate(
                field=k, value=v, source="verified_document",
                evidence_id=str(doc.id),
            ))

    # Realtor profile: chat-extracted facts.
    client = await db.get(Client, client_id)
    profile = (client.realtor_profile if client else None) or {}
    bp = profile.get("buyer_profile") or {}
    sp = profile.get("seller_profile") or {}
    for k, v in bp.items():
        if k in _COMPARABLE_FIELDS and v not in (None, "", 0):
            out.setdefault(k, []).append(FactCandidate(field=k, value=v, source="ai_extracted_chat"))
    for k, v in sp.items():
        if k in _COMPARABLE_FIELDS and v not in (None, "", 0):
            out.setdefault(k, []).append(FactCandidate(field=k, value=v, source="ai_extracted_chat"))
    for f in (profile.get("known_facts") or []):
        if not isinstance(f, dict):
            continue
        field, value = f.get("field"), f.get("value")
        if field in _COMPARABLE_FIELDS and value not in (None, "", 0):
            src = "agent_entered" if f.get("source") == "agent" else "ai_extracted_chat"
            out.setdefault(field, []).append(FactCandidate(field=field, value=value, source=src))

    return out


def _fmt(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"${int(value):,}" if abs(value) >= 1000 else str(value)
    return str(value)


def _scalarize(value: Any) -> Any:
    """JSON-friendly scalar coercion for issues storage."""
    if isinstance(value, (int, float, str, bool)) or value is None:
        return value
    return str(value)
