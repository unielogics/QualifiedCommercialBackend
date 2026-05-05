"""Module 7 — Expose enums + lending limits to frontends."""

from __future__ import annotations

from enum import EnumMeta

from fastapi import APIRouter

from app import enums as enum_module
from app.schemas.meta import EnumGroup, EnumValue, LendingLimits, MetaResponse

router = APIRouter(prefix="/meta", tags=["meta"])


def _humanize(value: str) -> str:
    return value.replace("_", " ").title()


@router.get("", response_model=MetaResponse)
async def get_meta() -> MetaResponse:
    groups: list[EnumGroup] = []
    for name in dir(enum_module):
        obj = getattr(enum_module, name)
        if isinstance(obj, EnumMeta) and obj is not enum_module.Role:
            groups.append(
                EnumGroup(
                    name=name,
                    values=[EnumValue(value=m.value, label=_humanize(m.value)) for m in obj],
                )
            )
        elif isinstance(obj, EnumMeta):
            # Include Role too — frontend needs it for route guards
            groups.append(
                EnumGroup(
                    name=name,
                    values=[EnumValue(value=m.value, label=_humanize(m.value)) for m in obj],
                )
            )
    # Dedup
    seen = set()
    unique = []
    for g in groups:
        if g.name not in seen:
            unique.append(g)
            seen.add(g.name)
    return MetaResponse(enums=unique, limits=LendingLimits())
