"""Super Admin governance for immutable Capital Readiness policy versions."""

from __future__ import annotations

# FastAPI dependencies are intentionally declared in route signatures.
# ruff: noqa: B008
import math
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.activity import Activity
from app.models.capital_readiness import CapitalReadinessPolicyVersion
from app.models.user import User

router = APIRouter(
    prefix="/admin/capital-readiness/policies",
    tags=["capital-readiness-policies"],
)

PolicyStatus = Literal["draft", "published", "retired"]
PolicyScopeKind = Literal["firm", "vertical", "industry", "naics_prefix"]
_LOCK_NAMESPACE = "capital-readiness-policy"
_DEFAULT_POLICY_KEY = "qc_lending_margin_v1"
_VERTICALS = {"real_estate", "main_street", "dealer", "mca"}


class PillarWeights(BaseModel):
    """The complete, closed set of inputs to the readiness weighted score."""

    model_config = ConfigDict(extra="forbid", strict=True)

    revenue_earnings: float = Field(ge=0, le=100, allow_inf_nan=False)
    debt_capital: float = Field(ge=0, le=100, allow_inf_nan=False)
    liquidity_banking: float = Field(ge=0, le=100, allow_inf_nan=False)
    bookkeeping_tax: float = Field(ge=0, le=100, allow_inf_nan=False)
    credit_collateral: float = Field(ge=0, le=100, allow_inf_nan=False)
    transaction_use: float = Field(ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def _weights_sum_to_one_hundred(self) -> Self:
        total = math.fsum(
            (
                self.revenue_earnings,
                self.debt_capital,
                self.liquidity_banking,
                self.bookkeeping_tax,
                self.credit_collateral,
                self.transaction_use,
            )
        )
        if not math.isclose(total, 100.0, abs_tol=1e-6):
            raise ValueError("The six pillar weights must sum to 100")
        return self


class OrderedThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    acceptable: float = Field(allow_inf_nan=False)
    healthy: float = Field(allow_inf_nan=False)
    very_strong: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def _strictly_increasing(self) -> Self:
        if not self.acceptable < self.healthy < self.very_strong:
            raise ValueError("Thresholds must increase: acceptable < healthy < very_strong")
        return self


class DscrThresholds(OrderedThresholds):
    acceptable: float = Field(ge=0, allow_inf_nan=False)
    healthy: float = Field(ge=0, allow_inf_nan=False)
    very_strong: float = Field(ge=0, allow_inf_nan=False)


class PolicyScope(BaseModel):
    """Explicit classification scope for one policy family."""

    model_config = ConfigDict(extra="forbid", strict=True)

    kind: PolicyScopeKind
    key: str | None = Field(default=None, max_length=80)

    @field_validator("kind", mode="before")
    @classmethod
    def _normalize_kind(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("key", mode="before")
    @classmethod
    def _normalize_key(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip().casefold() or None

    @model_validator(mode="after")
    def _validate_key_for_kind(self) -> Self:
        if self.kind == "firm":
            if self.key is not None:
                raise ValueError("Firm policy scope must not include a key")
            return self
        if self.key is None:
            raise ValueError(f"{self.kind} policy scope requires a key")
        if self.kind == "vertical" and self.key not in _VERTICALS:
            raise ValueError("Vertical policy scope key is not recognized")
        if self.kind == "naics_prefix" and (
            not self.key.isdigit() or not 2 <= len(self.key) <= 6
        ):
            raise ValueError("NAICS policy scope key must contain two to six digits")
        return self


class MetricThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    gross_margin_pct: OrderedThresholds
    net_margin_pct: OrderedThresholds
    dscr: DscrThresholds
    scope: PolicyScope | None = None


class PolicyDraftCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    policy_key: str = Field(
        min_length=3,
        max_length=80,
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    minimum_coverage_pct: float = Field(ge=60, le=100, allow_inf_nan=False)
    pillar_weights: PillarWeights
    metric_thresholds: MetricThresholds
    reason: str | None = Field(default=None, max_length=2000)
    confirmed: Literal[True]

    @field_validator("policy_key", mode="before")
    @classmethod
    def _normalize_policy_key(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("reason", mode="before")
    @classmethod
    def _normalize_reason(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None

    @model_validator(mode="after")
    def _validate_policy_family_scope(self) -> Self:
        scope = self.metric_thresholds.scope
        if self.policy_key == _DEFAULT_POLICY_KEY:
            if scope is not None and scope.kind != "firm":
                raise ValueError("The default policy family must use firm scope")
            return self
        if scope is None:
            raise ValueError("A non-default policy requires an explicit scope")
        if scope.kind == "firm":
            raise ValueError("Only the default policy family may use firm scope")
        return self


class PolicyPublishRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    reason: str | None = Field(default=None, max_length=2000)
    confirmed: Literal[True]

    @field_validator("reason", mode="before")
    @classmethod
    def _normalize_reason(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        return value.strip() or None


class CapitalReadinessPolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, extra="forbid")

    id: UUID
    policy_key: str
    version: int
    status: PolicyStatus
    minimum_coverage_pct: float
    pillar_weights: PillarWeights
    metric_thresholds: MetricThresholds
    published_at: datetime | None = None
    published_by_user_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


def _read_policy(row: CapitalReadinessPolicyVersion) -> CapitalReadinessPolicyRead:
    return CapitalReadinessPolicyRead.model_validate(row)


def _validated_policy_scope(
    policy_key: str, metric_thresholds: dict
) -> PolicyScope | None:
    try:
        thresholds = MetricThresholds.model_validate(metric_thresholds)
    except ValidationError as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Capital Readiness policy thresholds or scope are invalid",
        ) from exc
    scope = thresholds.scope
    if policy_key == _DEFAULT_POLICY_KEY:
        if scope is not None and scope.kind != "firm":
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "The default policy family must use firm scope",
            )
        return scope
    if scope is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "A non-default policy requires an explicit scope",
        )
    if scope.kind == "firm":
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            "Only the default policy family may use firm scope",
        )
    return scope


async def _lock_policy_key(db: AsyncSession, policy_key: str) -> None:
    """Serialize version assignment and publication for one policy family."""

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"{_LOCK_NAMESPACE}:{policy_key}"},
    )


async def _policy_or_404(
    db: AsyncSession,
    policy_id: UUID,
    *,
    for_update: bool = False,
) -> CapitalReadinessPolicyVersion:
    query = select(CapitalReadinessPolicyVersion).where(
        CapitalReadinessPolicyVersion.id == policy_id
    )
    if for_update:
        query = query.with_for_update()
    row = (await db.execute(query)).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Capital Readiness policy not found")
    return row


async def _create_policy_draft(
    db: AsyncSession,
    payload: PolicyDraftCreate,
    user: User,
) -> CapitalReadinessPolicyVersion:
    await _lock_policy_key(db, payload.policy_key)
    latest_version = (
        await db.execute(
            select(func.max(CapitalReadinessPolicyVersion.version)).where(
                CapitalReadinessPolicyVersion.policy_key == payload.policy_key
            )
        )
    ).scalar_one_or_none()
    row = CapitalReadinessPolicyVersion(
        policy_key=payload.policy_key,
        version=int(latest_version or 0) + 1,
        status="draft",
        minimum_coverage_pct=payload.minimum_coverage_pct,
        pillar_weights=payload.pillar_weights.model_dump(mode="json"),
        metric_thresholds=payload.metric_thresholds.model_dump(mode="json"),
    )
    db.add(row)
    db.add(
        Activity(
            actor_id=user.id,
            actor_label="super_admin",
            kind="capital_readiness.policy_draft_created",
            summary=f"Created Capital Readiness policy {row.policy_key} v{row.version} draft",
            payload={
                "policy_key": row.policy_key,
                "version": row.version,
                "reason": payload.reason or "Capital Readiness policy draft saved",
            },
        )
    )
    await db.flush()
    return row


async def _publish_policy(
    db: AsyncSession,
    policy_id: UUID,
    payload: PolicyPublishRequest,
    user: User,
) -> CapitalReadinessPolicyVersion:
    candidate = await _policy_or_404(db, policy_id)
    await _lock_policy_key(db, candidate.policy_key)
    candidate = await _policy_or_404(db, policy_id, for_update=True)
    if candidate.status != "draft":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only a draft Capital Readiness policy can be published",
        )
    _validated_policy_scope(candidate.policy_key, dict(candidate.metric_thresholds or {}))

    published = list(
        (
            await db.execute(
                select(CapitalReadinessPolicyVersion)
                .where(
                    CapitalReadinessPolicyVersion.policy_key == candidate.policy_key,
                    CapitalReadinessPolicyVersion.status == "published",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    for previous in published:
        previous.status = "retired"

    # Flush the retirement first so the partial unique index never observes
    # two published rows, while both changes remain in the same transaction.
    await db.flush()
    candidate.status = "published"
    candidate.published_at = datetime.now(UTC)
    candidate.published_by_user_id = user.id
    db.add(
        Activity(
            actor_id=user.id,
            actor_label="super_admin",
            kind="capital_readiness.policy_published",
            summary=(
                f"Published Capital Readiness policy "
                f"{candidate.policy_key} v{candidate.version}"
            ),
            payload={
                "policy_key": candidate.policy_key,
                "version": candidate.version,
                "retired_versions": [row.version for row in published],
                "reason": payload.reason or "Capital Readiness policy published",
            },
        )
    )
    await db.flush()
    return candidate


@router.get("", response_model=list[CapitalReadinessPolicyRead])
async def list_capital_readiness_policies(
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> list[CapitalReadinessPolicyRead]:
    rows = list(
        (
            await db.execute(
                select(CapitalReadinessPolicyVersion).order_by(
                    CapitalReadinessPolicyVersion.policy_key,
                    CapitalReadinessPolicyVersion.version.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return [_read_policy(row) for row in rows]


@router.get("/{policy_id}", response_model=CapitalReadinessPolicyRead)
async def get_capital_readiness_policy(
    policy_id: UUID,
    _: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> CapitalReadinessPolicyRead:
    return _read_policy(await _policy_or_404(db, policy_id))


@router.post(
    "",
    response_model=CapitalReadinessPolicyRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_capital_readiness_policy_draft(
    payload: PolicyDraftCreate,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> CapitalReadinessPolicyRead:
    row = await _create_policy_draft(db, payload, user)
    await db.refresh(row)
    response = _read_policy(row)
    await db.commit()
    return response


@router.post("/{policy_id}/publish", response_model=CapitalReadinessPolicyRead)
async def publish_capital_readiness_policy(
    policy_id: UUID,
    payload: PolicyPublishRequest,
    user: User = Depends(require_role(Role.SUPER_ADMIN)),
    db: AsyncSession = Depends(get_db),
) -> CapitalReadinessPolicyRead:
    row = await _publish_policy(db, policy_id, payload, user)
    await db.refresh(row)
    response = _read_policy(row)
    await db.commit()
    return response
