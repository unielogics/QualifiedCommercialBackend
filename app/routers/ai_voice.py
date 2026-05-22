"""AI Voice Profiles — broker-scoped, reusable tonality baselines.

Each profile is a named bag of short templates (greeting, late-item
ask, under-contract message, etc.) that establishes how the broker
actually writes to clients. The composer injects the templates as a
voice & tone block in any AI Agent's system prompt that links to this
profile — so one profile can drive many agents.

All endpoints are BROKER-scoped to the caller's own profiles.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser
from app.enums import Role
from app.models.ai_agent import AIAgent, AIVoiceProfile
from app.models.broker import Broker

router = APIRouter(prefix="/ai-voice-profiles", tags=["ai-voice-profiles"])
log = logging.getLogger(__name__)


# The catalog of situation slots the UI exposes. New keys can be added
# here without a migration — `templates` is JSONB.
VOICE_SITUATIONS: list[dict[str, str]] = [
    {
        "key": "greeting",
        "label": "Greeting a new client",
        "hint": "How you reach out first / introduce yourself.",
    },
    {
        "key": "due_soon",
        "label": "Asking for something coming due soon",
        "hint": "Friendly heads-up before a deadline.",
    },
    {
        "key": "late_item",
        "label": "Chasing a late / overdue item",
        "hint": "Past due — how you'd nudge without nagging.",
    },
    {
        "key": "under_contract",
        "label": "Letting a client know you're under contract",
        "hint": "Big-news moment — set expectations for what's next.",
    },
    {
        "key": "check_in",
        "label": "A general check-in or nudge",
        "hint": "Quiet client — how you'd open a casual touch-base.",
    },
]
ALLOWED_KEYS: set[str] = {s["key"] for s in VOICE_SITUATIONS}


async def _broker_for(db: AsyncSession, user) -> Broker:
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only.")
    broker = (
        await db.execute(select(Broker).where(Broker.user_id == user.id))
    ).scalar_one_or_none()
    if broker is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No broker profile.")
    return broker


async def _profile_or_404(
    db: AsyncSession, user, profile_id: uuid.UUID
) -> AIVoiceProfile:
    broker = await _broker_for(db, user)
    vp = await db.get(AIVoiceProfile, profile_id)
    if vp is None or vp.broker_id != broker.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Voice profile not found.")
    return vp


def _clean_templates(raw: dict[str, Any]) -> dict[str, str]:
    """Keep only catalog keys with non-empty string bodies."""
    out: dict[str, str] = {}
    for k, v in (raw or {}).items():
        if k in ALLOWED_KEYS and isinstance(v, str) and v.strip():
            out[k] = v.strip()[:8000]
    return out


def _ser(vp: AIVoiceProfile, *, used_by: int | None = None) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": str(vp.id),
        "name": vp.name,
        "templates": vp.templates or {},
        "created_at": vp.created_at.isoformat() if vp.created_at else None,
        "updated_at": vp.updated_at.isoformat() if vp.updated_at else None,
    }
    if used_by is not None:
        row["used_by"] = used_by
    return row


class VoiceProfileIn(BaseModel):
    name: str
    templates: dict[str, str] = {}


@router.get("/situations")
async def list_situations(user: CurrentUser):
    """The catalog of situation slots the UI offers. Static — included
    here so the frontend doesn't have to hard-code labels/hints."""
    if user.role != Role.BROKER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Agent-only.")
    return VOICE_SITUATIONS


@router.get("")
async def list_profiles(user: CurrentUser, db: AsyncSession = Depends(get_db)):
    broker = await _broker_for(db, user)
    rows = list(
        (
            await db.execute(
                select(AIVoiceProfile)
                .where(AIVoiceProfile.broker_id == broker.id)
                .order_by(AIVoiceProfile.created_at.desc())
            )
        ).scalars().all()
    )
    # Count how many AI Agents currently use each profile — drives the
    # "used by N agents" hint in the picker.
    out = []
    for vp in rows:
        used = int(
            (
                await db.execute(
                    select(func.count())
                    .select_from(AIAgent)
                    .where(AIAgent.voice_profile_id == vp.id)
                )
            ).scalar()
            or 0
        )
        out.append(_ser(vp, used_by=used))
    return out


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_profile(
    payload: VoiceProfileIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    broker = await _broker_for(db, user)
    vp = AIVoiceProfile(
        broker_id=broker.id,
        owner_user_id=user.id,
        name=(payload.name or "").strip()[:160] or "Untitled voice",
        templates=_clean_templates(payload.templates),
    )
    db.add(vp)
    await db.flush()
    await db.commit()
    return _ser(vp)


@router.get("/{profile_id}")
async def get_profile(
    profile_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    vp = await _profile_or_404(db, user, profile_id)
    return _ser(vp)


@router.put("/{profile_id}")
async def update_profile(
    profile_id: uuid.UUID,
    payload: VoiceProfileIn,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
):
    vp = await _profile_or_404(db, user, profile_id)
    vp.name = (payload.name or "").strip()[:160] or vp.name
    vp.templates = _clean_templates(payload.templates)
    await db.commit()
    return _ser(vp)


@router.delete("/{profile_id}")
async def delete_profile(
    profile_id: uuid.UUID, user: CurrentUser, db: AsyncSession = Depends(get_db)
):
    vp = await _profile_or_404(db, user, profile_id)
    await db.delete(vp)
    await db.commit()
    return {"ok": True}
