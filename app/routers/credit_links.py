# ruff: noqa: B008
"""Credit pull linking at the firm level: the mode switch and the sweep.

Per-file linking lives on the application profile router (owners'
credit-candidates, credit-link and credit-links/auto routes).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.activity import Activity
from app.models.user import User
from app.schemas.credit_links import CreditLinkingRead, CreditLinkingWrite, CreditLinkRunReport
from app.services import credit_links

router = APIRouter(prefix="/credit-links", tags=["credit-links"])


@router.get("/settings", response_model=CreditLinkingRead)
async def read_credit_linking(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> CreditLinkingRead:
    return CreditLinkingRead(**await credit_links.read_settings(db))


@router.put("/settings", response_model=CreditLinkingRead)
async def write_credit_linking(
    payload: CreditLinkingWrite,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> CreditLinkingRead:
    """Manual: pulls wait for a person. Automatic: the hook and the sweep link
    them and the desk is told what connected."""

    await credit_links.write_section(db, mode=payload.mode)
    db.add(
        Activity(
            loan_id=None,
            actor_id=user.id,
            actor_label=user.role.value if hasattr(user.role, "value") else str(user.role),
            kind="settings.updated",
            summary=f"Credit pull linking set to {payload.mode}",
            payload={"credit_linking": {"mode": payload.mode}},
        )
    )
    await db.commit()
    return CreditLinkingRead(**await credit_links.read_settings(db))


@router.post("/run", response_model=CreditLinkRunReport)
async def run_credit_linking(
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> CreditLinkRunReport:
    """Connect every unowned completed pull to the owner carrying its name,
    in either mode, and keep the report as the last run."""

    report = await credit_links.run_everywhere(db, actor=user, how=credit_links.HOW_SWEEP)
    await credit_links.remember_run(db, report)
    await db.commit()
    return CreditLinkRunReport(**report, words=credit_links.report_words(report))
