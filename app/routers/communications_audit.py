"""The communications audit log: every message we sent, and what caused it.

Operator-only, and scoped inside that: everyone sees their own, a super admin
sees everything. A message that belongs to nobody — a cron send about no
particular file — is super-admin only, because defaulting it to everybody
would be a leak.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.services.messaging import audit_feed
from app.services.production_packages import OPERATOR_ROLES

router = APIRouter(prefix="/admin/communications", tags=["communications-audit"])


def _require_operator(user) -> None:
    if user.role not in OPERATOR_ROLES:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Team role required")


@router.get("/messages")
async def list_messages(
    user: CurrentUser,
    q: str = "",
    channel: str = "",
    status_filter: str = "",
    context: str = "",
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    _require_operator(user)
    statuses = tuple(s for s in status_filter.split(",") if s) if status_filter else ()
    rows, total = await audit_feed.list_messages(
        db, user, q=q, channel=channel, statuses=statuses, context=context,
        limit=min(max(limit, 1), 200), offset=max(offset, 0),
    )
    return {
        "rows": [r.as_dict() for r in rows],
        "total": total,
        "scope": "all" if audit_feed.is_super_admin(user) else "own",
        "contexts": await audit_feed.contexts(db),
    }


@router.get("/messages/{message_id}")
async def message_detail(
    message_id: str, user: CurrentUser, db: AsyncSession = Depends(get_db)  # noqa: B008
) -> dict[str, Any]:
    _require_operator(user)
    detail = await audit_feed.message_detail(db, user, message_id)
    if detail is None:
        # Missing and not-yours are the same answer on purpose.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such message")
    detail["caused_by"] = [
        r.as_dict() for r in await audit_feed.caused_by(db, user, detail.get("request_id") or "")
    ]
    return detail


@router.get("/activity")
async def list_activity(
    user: CurrentUser,
    q: str = "",
    source: str = "",
    request_id: str = "",
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),  # noqa: B008
) -> dict[str, Any]:
    _require_operator(user)
    rows, total = await audit_feed.list_activity(
        db, user, q=q, source=source, request_id=request_id,
        limit=min(max(limit, 1), 200), offset=max(offset, 0),
    )
    return {"rows": [r.as_dict() for r in rows], "total": total}
