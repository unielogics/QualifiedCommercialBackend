"""Queue-only staff controls. No file, reminder, client event or email writes."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import func, select

from app.enums import Role
from app.models.operator_attention import OperatorAttentionControl, OperatorAttentionEvent
from app.schemas.operator_file import (
    AttentionAction,
    AttentionActionResult,
    AttentionDuplicateTarget,
    AttentionEventRead,
    AttentionHistory,
    AttentionItem,
)

STAFF_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
HIDDEN_STATUSES = {"archived", "dismissed", "duplicate"}
ACTION_STATUS = {"archive": "archived", "dismiss": "dismissed", "duplicate": "duplicate", "restore": "active"}


def require_staff(actor) -> None:
    if getattr(actor, "role", None) not in STAFF_ROLES or not getattr(actor, "id", None):
        raise HTTPException(403, "The attention queue is a desk view")


def item_key(item: AttentionItem) -> str:
    source_id = item.intake_id if item.kind == "intake_no_files" else item.bucket_id
    if source_id is None:
        raise ValueError("An attention item requires its canonical intake or bucket identity")
    return f"{item.kind}:{source_id}"


def validate_key(value: str) -> str:
    kind, separator, identifier = value.partition(":")
    try:
        source_id = UUID(identifier)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "Choose a valid attention item") from None
    if not separator or kind not in {"intake_no_files", "no_client_entry", "request_overdue", "action_items_stale"}:
        raise HTTPException(422, "Choose a valid attention item")
    return f"{kind}:{source_id}"


async def load_controls(db) -> dict[str, OperatorAttentionControl]:
    rows = (await db.execute(select(OperatorAttentionControl).execution_options(populate_existing=True))).scalars().all()
    return {row.item_key: row for row in rows}


def _target(snapshot, live) -> AttentionDuplicateTarget | None:
    if not snapshot:
        return None
    return AttentionDuplicateTarget(**{**snapshot, "current_source_active": snapshot["item_key"] in live})


def decorate(item, control, live) -> AttentionItem:
    result = item.model_copy(deep=True)
    key = item_key(result)
    result.item_key = key
    result.current_source_active = key in live
    if control is not None:
        result.queue_status = control.status
        result.queue_revision = control.revision
        result.queue_updated_at = control.updated_at
        result.queue_actor_name = control.actor_name
        result.queue_reason = control.reason
        result.duplicate_of = _target(control.duplicate_snapshot, live) if control.status == "duplicate" else None
    if result.queue_status != "active" or not result.current_source_active:
        result.actions = []
    return result


def visible_items(source_items, controls):
    """Keep hidden snapshots even after their original condition has resolved."""
    live = {item_key(item): item for item in source_items}
    combined = {key: decorate(item, controls.get(key), live) for key, item in live.items()}
    for key, control in controls.items():
        if control.status in HIDDEN_STATUSES and key not in combined:
            combined[key] = decorate(AttentionItem.model_validate(control.snapshot), control, live)
    return list(combined.values())


def event_read(event, live) -> AttentionEventRead:
    return AttentionEventRead(id=event.id, item_key=event.item_key, action=event.action,
        previous_status=event.previous_status, status=event.status, revision=event.revision,
        reason=event.reason, actor_user_id=event.actor_user_id, actor_name=event.actor_name,
        occurred_at=event.occurred_at, duplicate_of=_target(event.duplicate_snapshot, live))


def _snapshot(item):
    return item.model_dump(mode="json", exclude={"queue_status", "queue_revision", "queue_updated_at", "queue_actor_name", "queue_reason", "duplicate_of", "current_source_active"})


def _duplicate_snapshot(item):
    return {"item_key": item_key(item), "kind": item.kind, "title": item.title, "href": item.href}


def _result(item, event, live, *, replayed):
    current = item.item_key in live
    if replayed:
        message = "This action was already recorded. The queue reflects its current state."
    elif event.action == "restore" and not current:
        message = "Restored. The original issue has resolved, so no active alert was added."
    elif event.action == "restore":
        message = "Restored to the active attention queue."
    else:
        message = "Hidden from the attention queue. Files, requests and reminders are unchanged."
    return AttentionActionResult(item=item, event=event_read(event, live), replayed=replayed,
        current_source_active=current, message=message)


async def apply_action(db, *, payload: AttentionAction, actor) -> AttentionActionResult:
    require_staff(actor)
    key = validate_key(payload.item_key)
    target_key = validate_key(payload.duplicate_of_key) if payload.duplicate_of_key else None
    if (payload.action == "duplicate") != bool(target_key):
        raise HTTPException(422, "Choose a survivor only when marking an item as duplicate")
    reason = payload.reason.strip() or None
    material = {**payload.model_dump(mode="json"), "item_key": key, "duplicate_of_key": target_key,
                "reason": reason, "actor_user_id": str(actor.id)}
    fingerprint = hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    # Cross-item serialization is intentional: locking only A permits concurrent
    # A -> B and B -> A. This transaction-scoped desk namespace prevents cycles
    # and survivor-hide races, without locking any underlying client records.
    await db.execute(select(func.pg_advisory_xact_lock(20261106, 266)))
    prior = (await db.execute(select(OperatorAttentionEvent).where(
        OperatorAttentionEvent.idempotency_key == payload.idempotency_key))).scalar_one_or_none()
    from app.services.attention_queue import load_source_items

    source_items = await load_source_items(db)
    live = {item_key(item): item for item in source_items}
    controls = await load_controls(db)
    control = controls.get(key)
    if prior is not None:
        if prior.payload_hash != fingerprint:
            raise HTTPException(409, {"code": "attention_idempotency_conflict", "message": "This action key was already used for a different request"})
        if control is None:
            raise HTTPException(409, "Attention history is unavailable")
        current = live.get(key) or AttentionItem.model_validate(control.snapshot)
        return _result(decorate(current, control, live), prior, live, replayed=True)
    if (control.revision if control is not None else 0) != payload.expected_revision:
        raise HTTPException(409, {"code": "attention_revision_conflict", "message": "This attention item changed. Refresh the queue before acting again."})
    previous_status = control.status if control is not None else "active"
    current = live.get(key)
    if payload.action == "restore":
        if control is None or previous_status == "active":
            raise HTTPException(409, "This attention item is already active")
        current = current or AttentionItem.model_validate(control.snapshot)
    elif current is None:
        raise HTTPException(409, "This issue is no longer active. Refresh the queue.")
    elif previous_status != "active":
        raise HTTPException(409, "Restore this hidden item before changing its queue disposition")
    if payload.action != "restore" and any(row.status == "duplicate" and row.duplicate_of_key == key for row in controls.values()):
        raise HTTPException(409, {"code": "attention_duplicate_dependents", "message": "Restore or reassign this item's duplicates before hiding their survivor."})
    target = None
    if target_key:
        target = live.get(target_key)
        target_control = controls.get(target_key)
        if target_key == key or target is None or target.kind != current.kind or target_control is not None and target_control.status != "active":
            raise HTTPException(422, "Choose a different currently active item of the same issue kind as the duplicate survivor")
        if target_control is None:
            target_control = OperatorAttentionControl(item_key=target_key, status="active", revision=0, snapshot=_snapshot(target))
            db.add(target_control)
            controls[target_key] = target_control
            await db.flush()
    if control is None:
        control = OperatorAttentionControl(item_key=key, status="active", revision=0, snapshot=_snapshot(current))
        db.add(control)
        controls[key] = control
        await db.flush()
    old_duplicate_key, old_duplicate_snapshot = control.duplicate_of_key, copy.deepcopy(control.duplicate_snapshot)
    stamp = datetime.now(UTC)
    control.status = ACTION_STATUS[payload.action]
    control.revision += 1
    control.snapshot = _snapshot(current)
    control.duplicate_of_key = target_key
    control.duplicate_snapshot = _duplicate_snapshot(target) if target else None
    control.reason = reason
    control.actor_user_id, control.actor_name = actor.id, str(getattr(actor, "name", None) or "Staff")[:255]
    control.updated_at = stamp
    event = OperatorAttentionEvent(id=uuid4(), item_key=key, idempotency_key=payload.idempotency_key,
        payload_hash=fingerprint, action=payload.action, previous_status=previous_status, status=control.status,
        revision=control.revision, reason=reason, duplicate_of_key=target_key or old_duplicate_key,
        duplicate_snapshot=copy.deepcopy(control.duplicate_snapshot or old_duplicate_snapshot), snapshot=copy.deepcopy(control.snapshot),
        actor_user_id=actor.id, actor_name=control.actor_name, occurred_at=stamp)
    db.add(event)
    await db.flush()
    return _result(decorate(current, control, live), event, live, replayed=False)


async def history(db, *, key: str, limit: int, offset: int, actor) -> AttentionHistory:
    require_staff(actor)
    key = validate_key(key)
    control = await db.get(OperatorAttentionControl, key)
    if control is None:
        raise HTTPException(404, "No recorded attention actions for this item")
    total = (await db.execute(select(func.count()).select_from(OperatorAttentionEvent).where(OperatorAttentionEvent.item_key == key))).scalar_one()
    rows = (await db.execute(select(OperatorAttentionEvent).where(OperatorAttentionEvent.item_key == key)
        .order_by(OperatorAttentionEvent.revision.desc()).limit(limit).offset(offset))).scalars().all()
    from app.services.attention_queue import load_source_items

    live = {item_key(item): item for item in await load_source_items(db)}
    return AttentionHistory(item_key=key, items=[event_read(row, live) for row in rows], total=total, limit=limit, offset=offset)
