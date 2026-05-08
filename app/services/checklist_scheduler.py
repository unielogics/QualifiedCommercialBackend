"""Eager event-driven scheduler for the doc checklist.

Owns the "for each checklist item, do the right thing" matrix that
both `kickoff_loan` (anchor='loan_created') and
`/documents/upload-complete` (anchor='doc_received:<name>') call.
Centralizing the matrix keeps the two entry points in sync — there's
exactly one place that decides "this is internal, spawn an AITask"
or "this is external + per_unit, fan out to N Documents."

Idempotency: the de-dup key for external Documents is
`(loan_id, name)` (lower-cased + trimmed), and for AITasks it's
`(loan_id, action)`. Re-firing the same anchor never duplicates
rows.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import AITaskPriority, AITaskSource, AITaskStatus, DocStatus
from app.models.activity import Activity
from app.models.ai_task import AITask
from app.models.document import Document
from app.models.loan import Loan
from app.schemas.settings import DocChecklistItem, LoanTypeChecklist
from app.services import calendar_emitter

log = logging.getLogger(__name__)


def _display(item: DocChecklistItem) -> str:
    """What the user sees for this item — falls back to `name`."""
    return (item.display_name or item.name).strip()


async def _existing_doc_names(db: AsyncSession, loan_id) -> set[str]:
    rows = (
        await db.execute(select(Document.name).where(Document.loan_id == loan_id))
    ).scalars().all()
    return {n.lower().strip() for n in rows}


async def _existing_ai_task_actions(db: AsyncSession, loan_id) -> set[str]:
    """Open AITask actions on this loan (PENDING only — APPROVED /
    DISMISSED tasks are 'done' and shouldn't gate re-creation)."""
    rows = (
        await db.execute(
            select(AITask.action).where(
                AITask.loan_id == loan_id,
                AITask.status == AITaskStatus.PENDING,
            )
        )
    ).scalars().all()
    return {a.lower().strip() for a in rows if a}


async def _spawn_internal_task(
    db: AsyncSession, loan: Loan, item: DocChecklistItem
) -> AITask | None:
    """Create the AITask that the operator team uses to track this
    operator-ordered item (Appraisal / Insurance / Title / PFS).
    Returns the new task, or None if a matching one already exists."""
    action = (item.internal_action or item.name).strip()
    existing_actions = await _existing_ai_task_actions(db, loan.id)
    if action.lower() in existing_actions:
        return None
    task = AITask(
        loan_id=loan.id,
        source=AITaskSource.DOCUMENTS,
        priority=AITaskPriority.MEDIUM,
        status=AITaskStatus.PENDING,
        action=action,
        title=_display(item),
        summary=(
            f"Internal task — {_display(item)}. "
            f"Operator-ordered item per the {loan.type} checklist."
        ),
        agent="doc_checklist",
    )
    db.add(task)
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=None,
            actor_label="ai",
            kind="ai_task.created",
            summary=f"Internal doc task: {_display(item)}",
            payload={
                "action": action,
                "checklist_key": item.name,
                "anchor": item.anchor,
            },
        )
    )
    return task


async def _spawn_external_doc(
    db: AsyncSession,
    loan: Loan,
    *,
    name: str,
    checklist_key: str,
    due_in_days: int,
    existing_names: set[str],
) -> Document | None:
    if name.lower().strip() in existing_names:
        return None
    doc = Document(
        loan_id=loan.id,
        name=name,
        checklist_key=checklist_key,
        status=DocStatus.REQUESTED,
        requested_on=date.today(),
    )
    db.add(doc)
    await db.flush()
    await db.refresh(doc)
    db.add(
        Activity(
            loan_id=loan.id,
            actor_id=None,
            actor_label="ai",
            kind="document.requested",
            summary=f"Auto-requested: {name}",
            payload={
                "doc_id": str(doc.id),
                "auto": True,
                "checklist_key": checklist_key,
                "loan_type": str(loan.type),
            },
        )
    )
    await calendar_emitter.emit_for_document_request(db, doc, due_in_days=due_in_days)
    existing_names.add(name.lower().strip())
    return doc


async def materialize_item(
    db: AsyncSession,
    loan: Loan,
    item: DocChecklistItem,
    *,
    existing_names: set[str] | None = None,
    existing_actions: set[str] | None = None,
) -> tuple[int, int]:
    """Create the right artifact(s) for one checklist item.

    Returns `(docs_created, tasks_created)`. Caller passes
    pre-fetched existing-name / existing-action sets when it's
    iterating multiple items, so we don't requery per item.
    """
    if not item.auto_request:
        return 0, 0
    if existing_names is None:
        existing_names = await _existing_doc_names(db, loan.id)
    if existing_actions is None:
        existing_actions = await _existing_ai_task_actions(db, loan.id)

    if item.type == "internal":
        # Internal — operator-ordered. AITask only.
        action = (item.internal_action or item.name).strip()
        if action.lower() in existing_actions:
            return 0, 0
        await _spawn_internal_task(db, loan, item)
        existing_actions.add(action.lower())
        return 0, 1

    # External — borrower upload. Per-unit may fan out to N rows.
    docs_created = 0
    if item.per_unit:
        # 4-plex with per_unit=True on "Lease" → 4 Documents named
        # "Lease — Unit 1" through "Lease — Unit 4". Each carries
        # the same `checklist_key` so uploads route to the right slot.
        n = max(1, int(loan.unit_count or 1))
        for i in range(1, n + 1):
            row_name = f"{_display(item)} — Unit {i}"
            doc = await _spawn_external_doc(
                db, loan,
                name=row_name,
                checklist_key=item.name,
                due_in_days=item.due_offset_days,
                existing_names=existing_names,
            )
            if doc is not None:
                docs_created += 1
    else:
        doc = await _spawn_external_doc(
            db, loan,
            name=_display(item),
            checklist_key=item.name,
            due_in_days=item.due_offset_days,
            existing_names=existing_names,
        )
        if doc is not None:
            docs_created += 1

    return docs_created, 0


async def fire_anchor_dependents(
    db: AsyncSession,
    *,
    loan: Loan,
    anchor_event: str,
    checklist: LoanTypeChecklist | None = None,
) -> tuple[int, int]:
    """Materialize every checklist item whose `anchor` matches the
    fired event. Called from `/documents/upload-complete` once a
    doc flips to RECEIVED.

    Example: when the borrower uploads "Bank Statements (2 mo)" we
    fire `doc_received:Bank Statements (2 mo)`. Any items with
    that anchor (e.g. internal "Order appraisal" with
    due_offset_days=2) get materialized.

    Idempotent — re-firing the same anchor doesn't duplicate.
    Returns `(docs_created, tasks_created)`.
    """
    if checklist is None:
        # Local imports avoid circulars (loan_intake_automation imports
        # this module).
        from sqlalchemy import select as _select
        from app.models.app_settings import AppSettings as _AppSettings
        from app.services.loan_intake_automation import _checklist_for, _coerce_settings
        settings_row = (
            await db.execute(_select(_AppSettings).limit(1))
        ).scalar_one_or_none()
        settings = _coerce_settings(settings_row)
        checklist = _checklist_for(settings, str(loan.type))

    if not checklist.docs:
        return 0, 0

    matching: list[DocChecklistItem] = [
        item for item in checklist.docs if item.anchor == anchor_event
    ]
    if not matching:
        return 0, 0

    existing_names = await _existing_doc_names(db, loan.id)
    existing_actions = await _existing_ai_task_actions(db, loan.id)
    docs_total = 0
    tasks_total = 0
    new_external_items: list[DocChecklistItem] = []
    new_internal_items: list[DocChecklistItem] = []
    for item in matching:
        d, t = await materialize_item(
            db, loan, item,
            existing_names=existing_names,
            existing_actions=existing_actions,
        )
        docs_total += d
        tasks_total += t
        if d > 0 and item.type == "external":
            new_external_items.append(item)
        if t > 0 and item.type == "internal":
            new_internal_items.append(item)
    log.info(
        "fire_anchor_dependents: loan=%s anchor=%r → %d doc(s), %d task(s)",
        loan.deal_id, anchor_event, docs_total, tasks_total,
    )

    # Conversational anchor narration (Phase D). When a doc-received
    # anchor fires, drop a short message in the borrower's chat:
    # external dependents get inline upload CTAs; internal-only
    # firings get a "we've ordered X" status update with no buttons.
    # Best-effort — never fail the scheduler over messaging hiccups.
    if new_external_items or new_internal_items:
        try:
            await _post_anchor_narration(
                db,
                loan=loan,
                anchor_event=anchor_event,
                external_items=new_external_items,
                internal_items=new_internal_items,
            )
        except Exception:  # noqa: BLE001
            log.exception(
                "fire_anchor_dependents: narration failed loan=%s anchor=%s",
                loan.deal_id, anchor_event,
            )

    return docs_total, tasks_total


# Human-readable ETAs for each internal action — used when narrating
# anchor firings ("I've ordered the appraisal — that usually takes
# 5–7 business days"). Falls back to a generic line for unknown
# action keys.
_INTERNAL_ACTION_ETAS: dict[str, str] = {
    "order_appraisal": "5–7 business days",
    "order_title": "3–5 business days",
    "shop_insurance": "2–3 business days",
    "request_pfs": "shortly — we'll be in touch",
}


def _trigger_label_from_anchor(anchor_event: str) -> str:
    """`doc_received:Bank Statements (2 mo)` → `Bank Statements (2 mo)`."""
    if anchor_event.startswith("doc_received:"):
        return anchor_event.split(":", 1)[1].strip() or "your last upload"
    return "your last upload"


async def _post_anchor_narration(
    db: AsyncSession,
    *,
    loan: Loan,
    anchor_event: str,
    external_items: list[DocChecklistItem],
    internal_items: list[DocChecklistItem],
) -> None:
    """Drop a chat message describing what just happened after an
    anchor fired. Mixed firings (external + internal) get one
    message that lists both — the borrower sees their next ask plus
    what we ordered on their behalf."""
    from sqlalchemy import select as _select
    from app.models.client import Client as _Client
    from app.services.ai_messaging import post_ai_message

    client = (
        await db.execute(_select(_Client).where(_Client.id == loan.client_id))
    ).scalar_one_or_none()
    if client is None or client.user_id is None:
        return

    trigger = _trigger_label_from_anchor(anchor_event)
    parts: list[str] = []
    actions: list[dict] = []

    if external_items:
        names = ", ".join(f"**{_display(item)}**" for item in external_items[:3])
        parts.append(f"Got your {trigger}! Next up I need {names}.")
        # Re-resolve the freshly-created Documents so we can deep-link
        # the upload CTAs to specific document_ids.
        from app.models.document import Document as _Document
        from app.enums import DocStatus as _DocStatus
        for item in external_items[:5]:
            doc = (
                await db.execute(
                    _select(_Document).where(
                        _Document.loan_id == loan.id,
                        _Document.checklist_key == item.name,
                        _Document.status == _DocStatus.REQUESTED,
                    )
                )
            ).scalars().first()
            actions.append({
                "kind": "upload_document",
                "label": f"Upload {_display(item)}",
                "document_id": str(doc.id) if doc is not None else None,
                "checklist_key": item.name,
                "confirm": True,
            })
    if internal_items:
        order_phrases: list[str] = []
        for item in internal_items:
            action = (item.internal_action or "").strip()
            eta = _INTERNAL_ACTION_ETAS.get(action, "soon")
            order_phrases.append(f"the {_display(item).lower()} ({eta})")
        if order_phrases:
            joined = ", ".join(order_phrases)
            if external_items:
                parts.append(f"On our side I've kicked off: {joined}.")
            else:
                parts.append(
                    f"Got your {trigger}! On our side I've kicked off: {joined}."
                )

    if not parts:
        return

    body = " ".join(parts)
    await post_ai_message(
        db,
        user_id=client.user_id,
        loan_id=loan.id,
        body=body,
        actions=actions or None,
    )


async def materialize_kickoff_items(
    db: AsyncSession,
    loan: Loan,
    checklist: LoanTypeChecklist,
) -> tuple[int, int]:
    """Run materialize_item over every checklist item whose anchor
    is `loan_created`. Items anchored on doc-received fire later via
    `fire_anchor_dependents`. Returns `(docs_created, tasks_created)`.
    """
    items = [item for item in checklist.docs if item.anchor == "loan_created"]
    if not items:
        return 0, 0
    existing_names = await _existing_doc_names(db, loan.id)
    existing_actions = await _existing_ai_task_actions(db, loan.id)
    docs_total = 0
    tasks_total = 0
    for item in items:
        d, t = await materialize_item(
            db, loan, item,
            existing_names=existing_names,
            existing_actions=existing_actions,
        )
        docs_total += d
        tasks_total += t
    return docs_total, tasks_total
