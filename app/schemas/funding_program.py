from __future__ import annotations

import re
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from app.schemas.requirement_review import DocumentReviewChecks


class FundingProgramScopeRead(BaseModel):
    id: UUID
    vertical: Literal["real_estate", "dealer", "main_street", "mca"]
    scope_key: str
    intake_variants: list[str] = Field(default_factory=list)
    intent_keys: list[str] = Field(default_factory=list)
    naics_prefixes: list[str] = Field(default_factory=list)
    excluded_naics_prefixes: list[str] = Field(default_factory=list)
    industry_keys: list[str] = Field(default_factory=list)
    required_fact_keys: list[str] = Field(default_factory=list)
    is_active: bool = True

    model_config = {"from_attributes": True}


class FundingProgramRequirementRead(BaseModel):
    requirement_key: str
    label: str
    category: str
    required_level: str
    applies_when: dict | None = None
    blocks_stage: str | None = None
    visibility: list[str] = Field(default_factory=list)
    can_underwriter_waive: bool = True
    verification_required: bool = False
    expiration_days: int | None = None
    ai_request_message_template: str | None = None
    display_order: int = 0
    objective_text: str = ""
    completion_criteria: str = ""
    completion_mode: str = "ai_can_complete"
    review_checks: DocumentReviewChecks = Field(default_factory=list)


class FundingProgramVersionRead(BaseModel):
    playbook_id: UUID
    version: int
    status: Literal["draft", "published", "archived"]
    rules: dict = Field(default_factory=dict)
    requirements: list[FundingProgramRequirementRead] = Field(default_factory=list)
    published_at: datetime | None = None


class FundingProgramCatalogItem(BaseModel):
    id: UUID
    program_key: str
    public_slug: str
    name: str
    short_description: str | None = None
    aliases: list[str] = Field(default_factory=list)
    display_order: int
    status: Literal["active", "retired"]
    scopes: list[FundingProgramScopeRead] = Field(default_factory=list)
    published_version: FundingProgramVersionRead | None = None
    draft_versions: list[FundingProgramVersionRead] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class PublicFundingProgramCatalogItem(BaseModel):
    program_key: str
    public_slug: str
    name: str
    short_description: str | None = None
    display_order: int
    verticals: list[Literal["real_estate", "dealer", "main_street", "mca"]]


class FundingProgramScopeWrite(BaseModel):
    vertical: Literal["real_estate", "dealer", "main_street", "mca"]
    scope_key: str = Field(default="default", min_length=1, max_length=80)
    intake_variants: list[str] = Field(default_factory=list, max_length=20)
    intent_keys: list[str] = Field(default_factory=list, max_length=30)
    naics_prefixes: list[str] = Field(default_factory=list, max_length=30)
    excluded_naics_prefixes: list[str] = Field(default_factory=list, max_length=30)
    industry_keys: list[str] = Field(default_factory=list, max_length=30)
    required_fact_keys: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("scope_key")
    @classmethod
    def _normalize_scope_key(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Routing reference cannot be blank")
        return normalized

    @field_validator("naics_prefixes", "excluded_naics_prefixes")
    @classmethod
    def _valid_naics_prefixes(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = str(raw).strip()
            direct = re.fullmatch(r"\d{2,6}", value)
            bounded_range = re.fullmatch(r"(\d{2,6})\s*-\s*(\d{2,6})", value)
            if direct:
                expanded = [value]
            elif bounded_range:
                start_text, end_text = bounded_range.groups()
                start, end = int(start_text), int(end_text)
                if len(start_text) != len(end_text) or end < start or end - start > 20:
                    raise ValueError("NAICS prefix ranges must be ascending and bounded")
                expanded = [
                    str(item).zfill(len(start_text))
                    for item in range(start, end + 1)
                ]
            else:
                raise ValueError(
                    "NAICS prefixes must contain 2 to 6 digits or a bounded numeric range"
                )
            for prefix in expanded:
                if prefix not in normalized:
                    normalized.append(prefix)
        if len(normalized) > 100:
            raise ValueError("At most 100 expanded NAICS prefixes are allowed per list")
        return normalized

    @field_validator("intake_variants", "intent_keys", "industry_keys", "required_fact_keys")
    @classmethod
    def _normalize_scope_keys(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw in values:
            value = str(raw).strip().casefold()
            if not value:
                raise ValueError("Scope keys cannot be blank")
            if value not in normalized:
                normalized.append(value)
        return normalized


def _unique_scope_pairs(
    rows: list[FundingProgramScopeWrite] | None,
) -> list[FundingProgramScopeWrite] | None:
    """Reject duplicate routing rows before the database unique constraint does.

    The database protects ``(program_id, vertical, scope_key)``, but surfacing the
    same rule during request validation gives the editor a useful 422 response
    instead of an IntegrityError/500 after it has replaced the existing rows.
    """

    if rows is None:
        return None
    seen: set[tuple[str, str]] = set()
    for row in rows:
        pair = (row.vertical, row.scope_key)
        if pair in seen:
            raise ValueError(
                f"Duplicate workspace routing row: {row.vertical} / {row.scope_key}"
            )
        seen.add(pair)
    return rows


class _OptionalReviewReason(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)

    @field_validator("reason", mode="before")
    @classmethod
    def _normalize_optional_reason(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class FundingProgramCreate(BaseModel):
    program_key: str = Field(pattern=r"^[a-z0-9_]{2,64}$")
    public_slug: str = Field(pattern=r"^[a-z0-9-]{2,100}$")
    name: str = Field(min_length=2, max_length=160)
    short_description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    display_order: int = Field(default=100, ge=0, le=10000)
    scopes: list[FundingProgramScopeWrite] = Field(min_length=1, max_length=20)
    confirmed: Literal[True]

    @field_validator("scopes")
    @classmethod
    def _validate_unique_scopes(
        cls, rows: list[FundingProgramScopeWrite]
    ) -> list[FundingProgramScopeWrite]:
        return _unique_scope_pairs(rows) or []


class FundingProgramRequirementWrite(BaseModel):
    requirement_key: str = Field(pattern=r"^[a-z0-9_]{2,120}$")
    label: str = Field(min_length=2, max_length=200)
    category: Literal[
        "borrower_info",
        "property_data",
        "financials",
        "credit",
        "agreements",
        "insurance",
        "title_and_escrow",
        "appraisal_and_inspection",
        "scheduling",
        "compliance",
        "communication",
        "ai_internal",
    ] = "financials"
    required_level: Literal["required", "recommended", "optional"] = "required"
    applies_when: dict | None = None
    blocks_stage: (
        Literal[
            "prequalification",
            "term_sheet",
            "underwriting",
            "closing",
            "showings",
            "listed",
        ]
        | None
    ) = "underwriting"
    visibility: list[Literal["agent", "borrower", "underwriter"]] = Field(
        default_factory=lambda: ["borrower", "underwriter"],
        min_length=1,
        max_length=3,
    )
    can_underwriter_waive: bool = True
    verification_required: bool = False
    expiration_days: int | None = Field(default=None, ge=1, le=3650)
    ai_request_message_template: str | None = Field(default=None, max_length=4000)
    display_order: int = Field(default=0, ge=0, le=10000)
    objective_text: str = Field(default="", max_length=2000)
    completion_criteria: str = Field(default="", max_length=4000)
    review_checks: DocumentReviewChecks = Field(default_factory=list)
    completion_mode: Literal["ai_can_complete", "requires_human_verify", "borrower_self_attest"] = (
        "ai_can_complete"
    )

    @field_validator("requirement_key")
    @classmethod
    def _bounded_period_key(cls, value: str) -> str:
        bank = re.fullmatch(r"business_bank_statements_(\d+)_months", value)
        tax = re.fullmatch(r"business_tax_returns_(\d+)_years", value)
        if bank and not 1 <= int(bank.group(1)) <= 60:
            raise ValueError("Bank statement requirements must specify 1 to 60 months")
        if tax and not 1 <= int(tax.group(1)) <= 10:
            raise ValueError("Tax return requirements must specify 1 to 10 years")
        return value


class FundingProgramVersionCreate(_OptionalReviewReason):
    name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    rules: dict = Field(default_factory=dict)
    requirements: list[FundingProgramRequirementWrite] = Field(default_factory=list, max_length=100)
    confirmed: Literal[True]

    @field_validator("requirements")
    @classmethod
    def _validate_requirements(
        cls, rows: list[FundingProgramRequirementWrite]
    ) -> list[FundingProgramRequirementWrite]:
        keys: set[str] = set()
        for row in rows:
            key = row.requirement_key
            if key in keys:
                raise ValueError(f"Duplicate requirement key: {key}")
            keys.add(key)
        return rows


class FundingProgramPublishRequest(_OptionalReviewReason):
    confirmed: Literal[True]


class FundingProgramRetireRequest(_OptionalReviewReason):
    retired: bool = True
    confirmed: Literal[True]


class FundingProgramScopePatch(_OptionalReviewReason):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    short_description: str | None = Field(default=None, max_length=1000)
    display_order: int | None = Field(default=None, ge=0, le=10000)
    scopes: list[FundingProgramScopeWrite] | None = Field(default=None, min_length=1, max_length=20)
    confirmed: Literal[True]

    @field_validator("scopes")
    @classmethod
    def _validate_unique_scopes(
        cls, rows: list[FundingProgramScopeWrite] | None
    ) -> list[FundingProgramScopeWrite] | None:
        return _unique_scope_pairs(rows)

    @model_validator(mode="after")
    def _has_change(self) -> FundingProgramScopePatch:
        if not self.model_fields_set.intersection(
            {"name", "short_description", "display_order", "scopes"}
        ):
            raise ValueError("At least one catalog field must change")
        return self
