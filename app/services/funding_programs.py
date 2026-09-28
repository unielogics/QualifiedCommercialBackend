from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.activity import Activity
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.funding_program import FundingProgramCatalog, FundingProgramScope
from app.models.user import User
from app.schemas.funding_program import (
    FundingProgramCatalogItem,
    FundingProgramCreate,
    FundingProgramRetireRequest,
    FundingProgramScopePatch,
    FundingProgramScopeRead,
    FundingProgramVersionCreate,
    FundingProgramVersionRead,
    PublicFundingProgramCatalogItem,
)
from app.services.program_rules import ProgramRuleError, validate_rules


def _now() -> datetime:
    return datetime.now(UTC)


def _audit_reason(reason: str | None, fallback: str) -> str:
    """Keep the audit ledger descriptive without forcing an operator note."""

    return (reason or "").strip() or fallback


def _field_validation_detail(
    *path: str,
    message: str,
    input_value: Any = None,
) -> list[dict[str, Any]]:
    """Return the same field-addressable shape as FastAPI request validation."""

    return [
        {
            "type": "value_error",
            "loc": ["body", *path],
            "msg": message,
            "input": input_value,
        }
    ]


def _validate_program_rules_or_422(rules: dict[str, Any] | None) -> None:
    try:
        validate_rules(rules, enforce_supported_fields=True)
    except ProgramRuleError as exc:
        field = "fit"
        input_value = (rules or {}).get("fit")
        if rules and "recommendation_preferences" in rules:
            fit_only = dict(rules)
            fit_only.pop("recommendation_preferences", None)
            try:
                validate_rules(fit_only, enforce_supported_fields=True)
            except ProgramRuleError:
                # Prefer the first actionable eligibility error when both
                # sections are invalid; a later retry will then address the
                # preference section.
                pass
            else:
                field = "recommendation_preferences"
                input_value = rules.get("recommendation_preferences")
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_field_validation_detail(
                "rules",
                field,
                message=str(exc),
                input_value=input_value,
            ),
        ) from exc


def _version_read(
    playbook: AIPlaybookTemplate,
    requirements: list[AICollectionRequirement],
) -> FundingProgramVersionRead:
    return FundingProgramVersionRead(
        playbook_id=playbook.id,
        version=playbook.version,
        status=playbook.status,
        rules=dict(playbook.rules or {}),
        requirements=[
            {
                "requirement_key": row.requirement_key,
                "label": row.label,
                "category": row.category,
                "required_level": row.required_level,
                "applies_when": row.applies_when,
                "blocks_stage": row.blocks_stage,
                "visibility": list(row.visibility or []),
                "can_underwriter_waive": row.can_underwriter_waive,
                "verification_required": row.verification_required,
                "expiration_days": row.expiration_days,
                "ai_request_message_template": row.ai_request_message_template,
                "completion_mode": row.completion_mode,
                "display_order": row.display_order,
                "objective_text": row.objective_text or "",
                "completion_criteria": row.completion_criteria or "",
                "review_checks": list(getattr(row, "review_checks", None) or []),
            }
            for row in requirements
        ],
        published_at=playbook.published_at,
    )


async def catalog_rows(
    db: AsyncSession,
    *,
    include_retired: bool = False,
) -> list[FundingProgramCatalog]:
    query = select(FundingProgramCatalog)
    if not include_retired:
        query = query.where(FundingProgramCatalog.status == "active")
    return list(
        (
            await db.execute(
                query.order_by(
                    FundingProgramCatalog.display_order,
                    FundingProgramCatalog.name,
                )
            )
        )
        .scalars()
        .all()
    )


async def scopes_by_program(
    db: AsyncSession,
    program_ids: list[uuid.UUID],
    *,
    active_only: bool = True,
) -> dict[uuid.UUID, list[FundingProgramScope]]:
    if not program_ids:
        return {}
    query = select(FundingProgramScope).where(FundingProgramScope.program_id.in_(program_ids))
    if active_only:
        query = query.where(FundingProgramScope.is_active.is_(True))
    rows = list(
        (
            await db.execute(
                query.order_by(
                    FundingProgramScope.vertical,
                    FundingProgramScope.scope_key,
                )
            )
        )
        .scalars()
        .all()
    )
    grouped: dict[uuid.UUID, list[FundingProgramScope]] = defaultdict(list)
    for row in rows:
        grouped[row.program_id].append(row)
    return grouped


def authoritative_published_versions(
    playbooks: list[AIPlaybookTemplate],
) -> dict[uuid.UUID, AIPlaybookTemplate]:
    """Resolve one approved version per program consistently for every consumer.

    Normal publication archives older rows, but legacy imports and interrupted
    transactions can leave more than one published/active row. Version is the
    primary authority; publication time and id only provide deterministic tie
    breaking. Owner type must not let an older row shadow a newer approved one.
    """

    selected: dict[uuid.UUID, AIPlaybookTemplate] = {}
    for row in playbooks:
        if (
            row.funding_program_id is None
            or row.playbook_type != "loan_product"
            or row.status != "published"
            or not row.is_active
        ):
            continue
        current = selected.get(row.funding_program_id)
        row_timestamp = row.published_at or row.created_at
        row_key = (
            int(row.version or 0),
            row_timestamp.isoformat() if row_timestamp is not None else "",
            str(row.id),
        )
        if current is not None:
            current_timestamp = current.published_at or current.created_at
            current_key = (
                int(current.version or 0),
                current_timestamp.isoformat() if current_timestamp is not None else "",
                str(current.id),
            )
        else:
            current_key = None
        if current_key is None or row_key > current_key:
            selected[row.funding_program_id] = row
    return selected


async def published_versions_by_program(
    db: AsyncSession,
    program_ids: list[uuid.UUID],
) -> dict[uuid.UUID, AIPlaybookTemplate]:
    if not program_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(AIPlaybookTemplate).where(
                    AIPlaybookTemplate.playbook_type == "loan_product",
                    AIPlaybookTemplate.status == "published",
                    AIPlaybookTemplate.is_active.is_(True),
                    AIPlaybookTemplate.funding_program_id.in_(program_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return authoritative_published_versions(rows)


async def public_catalog(db: AsyncSession) -> list[PublicFundingProgramCatalogItem]:
    rows = await catalog_rows(db)
    scopes = await scopes_by_program(db, [row.id for row in rows])
    return [
        PublicFundingProgramCatalogItem(
            program_key=row.program_key,
            public_slug=row.public_slug,
            name=row.name,
            short_description=row.short_description,
            display_order=row.display_order,
            verticals=list(dict.fromkeys(scope.vertical for scope in scopes.get(row.id, []))),
        )
        for row in rows
    ]


async def admin_catalog(db: AsyncSession) -> list[FundingProgramCatalogItem]:
    rows = await catalog_rows(db, include_retired=True)
    program_ids = [row.id for row in rows]
    # The editable catalog contract intentionally excludes retired scope rows.
    # Scope writes replace their rows and do not carry an ``is_active`` field;
    # returning an inactive legacy row here would therefore reactivate it on an
    # otherwise unrelated save.
    scopes = await scopes_by_program(db, program_ids)
    playbooks = (
        list(
            (
                await db.execute(
                    select(AIPlaybookTemplate)
                    .where(AIPlaybookTemplate.funding_program_id.in_(program_ids))
                    .order_by(
                        AIPlaybookTemplate.funding_program_id,
                        AIPlaybookTemplate.version.desc(),
                    )
                )
            )
            .scalars()
            .all()
        )
        if program_ids
        else []
    )
    requirements = (
        list(
            (
                await db.execute(
                    select(AICollectionRequirement).where(
                        AICollectionRequirement.playbook_id.in_([row.id for row in playbooks])
                    )
                )
            )
            .scalars()
            .all()
        )
        if playbooks
        else []
    )
    requirements_by_playbook: dict[uuid.UUID, list[AICollectionRequirement]] = defaultdict(list)
    for requirement in requirements:
        requirements_by_playbook[requirement.playbook_id].append(requirement)
    playbooks_by_program: dict[uuid.UUID, list[AIPlaybookTemplate]] = defaultdict(list)
    for playbook in playbooks:
        playbooks_by_program[playbook.funding_program_id].append(playbook)
    published_by_program = authoritative_published_versions(playbooks)

    result: list[FundingProgramCatalogItem] = []
    for row in rows:
        versions = playbooks_by_program.get(row.id, [])
        published = published_by_program.get(row.id)
        result.append(
            FundingProgramCatalogItem(
                id=row.id,
                program_key=row.program_key,
                public_slug=row.public_slug,
                name=row.name,
                short_description=row.short_description,
                aliases=list(row.aliases or []),
                display_order=row.display_order,
                status=row.status,
                scopes=[
                    FundingProgramScopeRead.model_validate(item) for item in scopes.get(row.id, [])
                ],
                published_version=(
                    _version_read(published, requirements_by_playbook.get(published.id, []))
                    if published
                    else None
                ),
                draft_versions=[
                    _version_read(item, requirements_by_playbook.get(item.id, []))
                    for item in versions
                    if item.status == "draft"
                ],
                created_at=row.created_at,
                updated_at=row.updated_at,
            )
        )
    return result


async def catalog_item_or_404(db: AsyncSession, program_key: str) -> FundingProgramCatalog:
    row = (
        await db.execute(
            select(FundingProgramCatalog).where(FundingProgramCatalog.program_key == program_key)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Funding program not found")
    return row


async def _replace_scopes(
    db: AsyncSession,
    program: FundingProgramCatalog,
    scopes: list[Any],
) -> None:
    await db.execute(
        delete(FundingProgramScope).where(FundingProgramScope.program_id == program.id)
    )
    for scope in scopes:
        db.add(
            FundingProgramScope(
                program_id=program.id,
                vertical=scope.vertical,
                scope_key=scope.scope_key,
                intake_variants=list(scope.intake_variants),
                intent_keys=list(scope.intent_keys),
                naics_prefixes=list(scope.naics_prefixes),
                excluded_naics_prefixes=list(scope.excluded_naics_prefixes),
                industry_keys=list(scope.industry_keys),
                required_fact_keys=list(scope.required_fact_keys),
            )
        )


async def create_program(
    db: AsyncSession,
    payload: FundingProgramCreate,
    user: User,
) -> FundingProgramCatalog:
    existing = (
        await db.execute(
            select(FundingProgramCatalog.id).where(
                (FundingProgramCatalog.program_key == payload.program_key)
                | (FundingProgramCatalog.public_slug == payload.public_slug)
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, "Program key or public slug already exists")
    row = FundingProgramCatalog(
        program_key=payload.program_key,
        public_slug=payload.public_slug,
        name=payload.name.strip(),
        short_description=(payload.short_description or "").strip() or None,
        aliases=list(dict.fromkeys(payload.aliases)),
        display_order=payload.display_order,
        created_by_user_id=user.id,
    )
    db.add(row)
    await db.flush()
    await _replace_scopes(db, row, payload.scopes)
    db.add(
        Activity(
            actor_id=user.id,
            actor_label="super_admin",
            kind="funding_program.created",
            summary=f"Created funding program {row.name}",
            payload={"program_key": row.program_key},
        )
    )
    return row


async def update_program(
    db: AsyncSession,
    program: FundingProgramCatalog,
    payload: FundingProgramScopePatch,
    user: User,
) -> None:
    before = {
        "name": program.name,
        "short_description": program.short_description,
        "display_order": program.display_order,
    }
    if payload.name is not None:
        program.name = payload.name.strip()
    if "short_description" in payload.model_fields_set:
        program.short_description = (payload.short_description or "").strip() or None
    if payload.display_order is not None:
        program.display_order = payload.display_order
    if payload.scopes is not None:
        await _replace_scopes(db, program, payload.scopes)
    db.add(
        Activity(
            actor_id=user.id,
            actor_label="super_admin",
            kind="funding_program.updated",
            summary=f"Updated funding program {program.name}",
            payload={
                "program_key": program.program_key,
                "before": before,
                "reason": _audit_reason(
                    payload.reason,
                    "Program details and availability updated",
                ),
            },
        )
    )


async def create_version(
    db: AsyncSession,
    program: FundingProgramCatalog,
    payload: FundingProgramVersionCreate,
    user: User,
) -> AIPlaybookTemplate:
    _validate_program_rules_or_422(payload.rules)
    latest_version = (
        await db.execute(
            select(AIPlaybookTemplate.version)
            .where(AIPlaybookTemplate.funding_program_id == program.id)
            .order_by(AIPlaybookTemplate.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    playbook = AIPlaybookTemplate(
        owner_type="funding",
        owner_id=user.id,
        playbook_type="loan_product",
        product_key=program.program_key,
        funding_program_id=program.id,
        name=(payload.name or program.name).strip(),
        description=(payload.description or program.short_description or "").strip() or None,
        rules=dict(payload.rules),
        version=int(latest_version or 0) + 1,
        status="draft",
        is_active=True,
    )
    db.add(playbook)
    await db.flush()
    for item in payload.requirements:
        db.add(
            AICollectionRequirement(
                playbook_id=playbook.id,
                requirement_key=item.requirement_key,
                label=item.label.strip(),
                category=item.category,
                required_level=item.required_level,
                applies_when=item.applies_when,
                blocks_stage=item.blocks_stage,
                visibility=list(item.visibility),
                can_underwriter_waive=item.can_underwriter_waive,
                verification_required=item.verification_required,
                expiration_days=item.expiration_days,
                ai_request_message_template=item.ai_request_message_template,
                display_order=item.display_order,
                objective_text=item.objective_text,
                completion_criteria=item.completion_criteria,
                review_checks=[check.model_dump() for check in item.review_checks],
                completion_mode=item.completion_mode,
            )
        )
    db.add(
        Activity(
            actor_id=user.id,
            actor_label="super_admin",
            kind="funding_program.version_created",
            summary=f"Created draft version {playbook.version} for {program.name}",
            payload={
                "program_key": program.program_key,
                "reason": _audit_reason(payload.reason, "Criteria draft saved"),
            },
        )
    )
    return playbook


async def publish_version(
    db: AsyncSession,
    program: FundingProgramCatalog,
    playbook_id: uuid.UUID,
    reason: str | None,
    user: User,
) -> AIPlaybookTemplate:
    playbook = (
        await db.execute(
            select(AIPlaybookTemplate).where(
                AIPlaybookTemplate.id == playbook_id,
                AIPlaybookTemplate.funding_program_id == program.id,
            )
        )
    ).scalar_one_or_none()
    if playbook is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Program version not found")
    if playbook.status != "draft":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only a draft version can be published")
    _validate_program_rules_or_422(playbook.rules)
    if not (playbook.rules or {}).get("fit"):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_field_validation_detail(
                "rules",
                "fit",
                message="Publish requires at least one validated eligibility rule",
                input_value=(playbook.rules or {}).get("fit"),
            ),
        )
    unresolved = (playbook.rules or {}).get("unresolved_review_items") or []
    if unresolved:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=_field_validation_detail(
                "rules",
                "unresolved_review_items",
                message=(
                    "Resolve every imported criteria review item in a new draft before publishing"
                ),
                input_value=unresolved,
            ),
        )
    current = list(
        (
            await db.execute(
                select(AIPlaybookTemplate).where(
                    AIPlaybookTemplate.funding_program_id == program.id,
                    AIPlaybookTemplate.status == "published",
                )
            )
        )
        .scalars()
        .all()
    )
    for row in current:
        row.status = "archived"
        row.is_active = False
    playbook.status = "published"
    playbook.is_active = True
    playbook.published_at = _now()
    db.add(
        Activity(
            actor_id=user.id,
            actor_label="super_admin",
            kind="funding_program.published",
            summary=f"Published {program.name} version {playbook.version}",
            payload={
                "program_key": program.program_key,
                "reason": _audit_reason(reason, "Criteria version published"),
            },
        )
    )
    return playbook


async def retire_program(
    db: AsyncSession,
    program: FundingProgramCatalog,
    payload: FundingProgramRetireRequest,
    user: User,
) -> None:
    program.status = "retired" if payload.retired else "active"
    program.retired_at = _now() if payload.retired else None
    program.retired_by_user_id = user.id if payload.retired else None
    db.add(
        Activity(
            actor_id=user.id,
            actor_label="super_admin",
            kind="funding_program.retired" if payload.retired else "funding_program.restored",
            summary=f"{'Retired' if payload.retired else 'Restored'} funding program {program.name}",
            payload={
                "program_key": program.program_key,
                "reason": _audit_reason(
                    payload.reason,
                    "Funding program retired" if payload.retired else "Funding program restored",
                ),
            },
        )
    )
