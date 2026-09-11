from __future__ import annotations

# ruff: noqa: B008
from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.user import User
from app.schemas.funding_program import (
    FundingProgramCatalogItem,
    FundingProgramCreate,
    FundingProgramPublishRequest,
    FundingProgramRetireRequest,
    FundingProgramScopePatch,
    FundingProgramVersionCreate,
    PublicFundingProgramCatalogItem,
)
from app.services import funding_programs

public_router = APIRouter(prefix="/public/funding-programs", tags=["funding-programs"])
admin_router = APIRouter(prefix="/admin/funding-programs", tags=["funding-programs"])


@public_router.get("", response_model=list[PublicFundingProgramCatalogItem])
async def list_public_funding_programs(
    db: AsyncSession = Depends(get_db),
) -> list[PublicFundingProgramCatalogItem]:
    return await funding_programs.public_catalog(db)


@admin_router.get("", response_model=list[FundingProgramCatalogItem])
async def list_admin_funding_programs(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[FundingProgramCatalogItem]:
    return await funding_programs.admin_catalog(db)


@admin_router.post(
    "",
    response_model=list[FundingProgramCatalogItem],
    status_code=status.HTTP_201_CREATED,
)
async def create_funding_program(
    payload: FundingProgramCreate,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[FundingProgramCatalogItem]:
    await funding_programs.create_program(db, payload, user)
    await db.commit()
    return await funding_programs.admin_catalog(db)


@admin_router.patch("/{program_key}", response_model=list[FundingProgramCatalogItem])
async def update_funding_program(
    program_key: str,
    payload: FundingProgramScopePatch,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[FundingProgramCatalogItem]:
    program = await funding_programs.catalog_item_or_404(db, program_key)
    await funding_programs.update_program(db, program, payload, user)
    await db.commit()
    return await funding_programs.admin_catalog(db)


@admin_router.post(
    "/{program_key}/versions",
    response_model=list[FundingProgramCatalogItem],
    status_code=status.HTTP_201_CREATED,
)
async def create_funding_program_version(
    program_key: str,
    payload: FundingProgramVersionCreate,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[FundingProgramCatalogItem]:
    program = await funding_programs.catalog_item_or_404(db, program_key)
    await funding_programs.create_version(db, program, payload, user)
    await db.commit()
    return await funding_programs.admin_catalog(db)


@admin_router.post(
    "/{program_key}/versions/{playbook_id}/publish",
    response_model=list[FundingProgramCatalogItem],
)
async def publish_funding_program_version(
    program_key: str,
    playbook_id: UUID,
    payload: FundingProgramPublishRequest,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[FundingProgramCatalogItem]:
    program = await funding_programs.catalog_item_or_404(db, program_key)
    await funding_programs.publish_version(db, program, playbook_id, payload.reason, user)
    await db.commit()
    return await funding_programs.admin_catalog(db)


@admin_router.post(
    "/{program_key}/retire",
    response_model=list[FundingProgramCatalogItem],
)
async def retire_funding_program(
    program_key: str,
    payload: FundingProgramRetireRequest,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[FundingProgramCatalogItem]:
    program = await funding_programs.catalog_item_or_404(db, program_key)
    await funding_programs.retire_program(db, program, payload, user)
    await db.commit()
    return await funding_programs.admin_catalog(db)
