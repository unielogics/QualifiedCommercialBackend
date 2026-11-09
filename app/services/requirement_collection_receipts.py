"""Durable private completion receipts beside the replaceable checklist cache.

The receipt is written with the evidence decision in the same transaction.
It never goes through best-effort timeline delivery, sends no notice and is
never included in client-visible completion events. Hydration restores audit
and retry identity only; current evidence still determines collection status.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.models.file_event import NOTICE_NONE, VISIBILITY_DESK, FileEvent

KIND = "requirement.collection_completion_receipt"
TARGET = "application_requirement"


def _validated(value):
    if not isinstance(value, dict):
        return None
    try:
        at = datetime.fromisoformat(value["at"])
        UUID(value["actor_id"])
        files = value["file_ids"]
        if not isinstance(files, list) or not files or len(files) > 100:
            return None
        if len({str(UUID(file_id)) for file_id in files}) != len(files):
            return None
        reason, retry = value["reason"], value["idempotency_key"]
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2000:
            return None
        if not isinstance(retry, str) or not 8 <= len(retry) <= 128:
            return None
        if at.tzinfo is None or at.utcoffset() is None or value.get("qualification_approved") is not False:
            return None
        if not all(isinstance(value.get(key), str) and re.fullmatch(r"[a-f0-9]{64}", value[key])
                   for key in ("fingerprint", "request_stamp")):
            return None
        request = {"file_ids": sorted(str(file_id) for file_id in files), "reason": reason.strip(),
                   "expected_fingerprint": value["fingerprint"]}
        expected = hashlib.sha256(json.dumps(request, sort_keys=True, default=str).encode()).hexdigest()
        if expected != value["request_stamp"]:
            return None
    except (KeyError, TypeError, ValueError, AttributeError):
        return None
    return deepcopy(value)


def _cached(state):
    metadata = state.provenance or {}
    return [receipt for value in [*(metadata.get("collection_completion_history") or []),
                                  metadata.get("collection_completion_receipt")]
            if (receipt := _validated(value)) is not None]


async def hydrate(db, profile, states):
    """Merge durable receipts into fresh states; never flush or change status."""
    scoped = {str(state.id): state for state in states if state.profile_id == profile.id}
    if not scoped:
        return
    events = list((await db.execute(select(FileEvent).where(
        FileEvent.profile_id == profile.id, FileEvent.kind == KIND,
        FileEvent.visibility == VISIBILITY_DESK, FileEvent.target_type == TARGET,
        FileEvent.target_id.in_(list(scoped)),
    ).order_by(FileEvent.created_at, FileEvent.id))).scalars().all())
    durable = {}
    for event in events:
        state = scoped[event.target_id]
        meta = event.meta or {}
        receipt = _validated(meta.get("receipt"))
        if meta.get("requirement_key") != state.requirement_key or receipt is None:
            continue
        durable.setdefault(event.target_id, []).append(receipt)
    for target, values in durable.items():
        state = scoped[target]
        # Durable rows take precedence over an overwritten cache for a given
        # retry key. Older receipts remain available for exact delivery retry.
        merged = {receipt["idempotency_key"]: receipt for receipt in [*_cached(state), *values]}
        ordered = sorted(merged.values(), key=lambda receipt: (datetime.fromisoformat(receipt["at"]), receipt["idempotency_key"]))
        state.provenance = {**(state.provenance or {}), "collection_completion_history": ordered[:-1],
                            "collection_completion_receipt": ordered[-1]}


async def receipts(db, profile, state):
    """Read retry identities from durable storage, retaining legacy receipts."""
    await hydrate(db, profile, [state])
    return _cached(state)


async def record(db, profile, state, receipt):
    """Mandatory transactional write; errors must abort document acceptance."""
    receipt = _validated(receipt)
    if receipt is None or state.profile_id != profile.id:
        raise ValueError("A valid, scoped document collection receipt is required")
    db.add(FileEvent(profile_id=profile.id, kind=KIND, visibility=VISIBILITY_DESK,
        actor_user_id=UUID(receipt["actor_id"]), title="Document collection review recorded",
        target_type=TARGET, target_id=str(state.id),
        meta={"requirement_key": state.requirement_key, "receipt": receipt}, notice_status=NOTICE_NONE))
    await db.flush()
