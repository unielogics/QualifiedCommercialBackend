"""Profile-owned annual NOI objectives, independent of editable client answers."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.enums import Role
from app.models.file_event import NOTICE_NONE, VISIBILITY_DESK, FileEvent

KIND = "operating_plan.goal_set"
METRIC = "annual_noi"
WRITE_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}


def public_goal(value=None):
    value = value or {}
    amount = value.get("amount")
    try:
        numeric = Decimal(str(amount))
        configured = numeric.is_finite() and numeric > 0 and bool(re.fullmatch(r"[A-Z]{3}", str(value.get("currency") or "")))
    except (InvalidOperation, ValueError, TypeError):
        configured = False
    return {"status": "set" if configured else "awaiting_staff", "metric": METRIC,
        "amount": float(amount) if configured else None, "currency": value.get("currency") if configured else None,
        "revision": int(value.get("revision") or 0), "updated_at": value.get("updated_at")}


async def _events(db, profile):
    rows = (await db.execute(select(FileEvent).where(FileEvent.profile_id == profile.id, FileEvent.kind == KIND))).scalars().all()
    return [row for row in rows if getattr(row, "kind", None) == KIND and str(getattr(row, "profile_id", "")) == str(profile.id)]


def _latest(events):
    # Revision is allocated under the profile lock; timestamp/UUID order is not authority.
    candidates = [row for row in events if isinstance(row.meta, dict) and isinstance(row.meta.get("revision"), int)]
    return max(candidates, key=lambda row: row.meta["revision"]) if candidates else None


async def read_goal(db, profile):
    latest = _latest(await _events(db, profile))
    return public_goal(latest.meta if latest else None)


async def require_goal(db, profile):
    goal = await read_goal(db, profile)
    if goal["status"] != "set":
        raise HTTPException(409, {"code": "operating_goal_required", "state": "goal_awaiting_staff",
            "message": "Your team needs to set the annual NOI goal before this operating plan can be opened or shared.", "goal": goal})
    return goal


def reject_client_goal(incoming):
    if isinstance(incoming, dict) and any(key in incoming for key in ("goal", "operating_goal", "goal_progress", "goal_revision")):
        raise HTTPException(422, {"code": "operating_goal_read_only", "message": "The annual NOI goal is set by your team and cannot be changed through this form."})


async def set_goal(db, profile, *, amount, currency, expected_revision, idempotency_key, actor):
    if actor.role not in WRITE_ROLES or getattr(actor, "id", None) is None:
        raise HTTPException(403, "Underwriting role required to configure the operating-plan goal")
    from pydantic import ValidationError

    from app.schemas.operating_plan_goal import GoalWrite

    try:
        payload = GoalWrite(amount=amount, currency=currency, expected_revision=expected_revision, idempotency_key=idempotency_key)
    except ValidationError as error:
        raise HTTPException(422, "Use a positive annual NOI goal, confirmed currency and current revision") from error
    amount, currency = payload.amount, payload.currency
    from app.services.financial_workspace import lock_financial_profile

    await lock_financial_profile(db, profile)
    events = await _events(db, profile)
    canonical = {"metric": METRIC, "amount": str(Decimal(str(amount)).normalize()), "currency": currency}
    stamp = hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    prior = next((row for row in events if row.meta.get("idempotency_key") == idempotency_key), None)
    if prior is not None:
        if str(prior.actor_user_id) != str(actor.id) or prior.meta.get("request_stamp") != stamp:
            raise HTTPException(409, {"code": "operating_goal_idempotency_conflict", "message": "This save identifier belongs to a different goal change or staff member."})
        return public_goal(_latest(events).meta)
    latest = _latest(events)
    current = public_goal(latest.meta if latest else None)
    if expected_revision != current["revision"]:
        raise HTTPException(409, {"code": "operating_goal_changed", "message": "The annual NOI goal changed. Refresh before saving.", "goal": current})
    if current["status"] == "set" and Decimal(str(latest.meta["amount"])) == amount and current["currency"] == currency:
        return current
    timestamp = datetime.now(UTC)
    record = {"metric": METRIC, "amount": str(amount), "currency": currency, "revision": current["revision"] + 1,
        "updated_at": timestamp.isoformat(), "idempotency_key": idempotency_key, "request_stamp": stamp,
        "set_by_role": str(actor.role)}
    db.add(FileEvent(id=uuid4(), profile_id=profile.id, kind=KIND, visibility=VISIBILITY_DESK,
        actor_user_id=actor.id, actor_label=getattr(actor, "name", None), title="Annual operating-plan NOI goal set",
        target_type="operating_plan", target_id=str(profile.id), meta=record, notice_status=NOTICE_NONE,
        notice_recipients=[], created_at=timestamp))
    await db.flush()
    return public_goal(record)
