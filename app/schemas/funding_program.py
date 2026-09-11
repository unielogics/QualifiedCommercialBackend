from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator


class FundingProgramScopeRead(BaseModel):
    id: UUID
    vertical: Literal["real_estate", "dealer", "main_street", "mca"]
    scope_key: str
    intake_variants: list[str] = Field(default_factory=list)
    intent_keys: list[str] = Field(default_factory=list)
    naics_prefixes: list[str] = Field(default_factory=list)
    industry_keys: list[str] = Field(default_factory=list)
    required_fact_keys: list[str] = Field(default_factory=list)
    is_active: bool = True

    model_config = {"from_attributes": True}


class FundingProgramVersionRead(BaseModel):
    playbook_id: UUID
    version: int
    status: Literal["draft", "published", "archived"]
    rules: dict = Field(default_factory=dict)
    requirements: list[dict] = Field(default_factory=list)
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
    industry_keys: list[str] = Field(default_factory=list, max_length=30)
    required_fact_keys: list[str] = Field(default_factory=list, max_length=20)


class FundingProgramCreate(BaseModel):
    program_key: str = Field(pattern=r"^[a-z0-9_]{2,64}$")
    public_slug: str = Field(pattern=r"^[a-z0-9-]{2,100}$")
    name: str = Field(min_length=2, max_length=160)
    short_description: str | None = Field(default=None, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=20)
    display_order: int = Field(default=100, ge=0, le=10000)
    scopes: list[FundingProgramScopeWrite] = Field(min_length=1, max_length=20)
    confirmed: Literal[True]


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
    completion_mode: Literal["ai_can_complete", "requires_human_verify", "borrower_self_attest"] = (
        "ai_can_complete"
    )


class FundingProgramVersionCreate(BaseModel):
    name: str | None = Field(default=None, max_length=160)
    description: str | None = Field(default=None, max_length=4000)
    rules: dict = Field(default_factory=dict)
    requirements: list[FundingProgramRequirementWrite] = Field(default_factory=list, max_length=100)
    reason: str = Field(min_length=8, max_length=2000)
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


class FundingProgramPublishRequest(BaseModel):
    reason: str = Field(min_length=8, max_length=2000)
    confirmed: Literal[True]


class FundingProgramRetireRequest(BaseModel):
    retired: bool = True
    reason: str = Field(min_length=8, max_length=2000)
    confirmed: Literal[True]


class FundingProgramScopePatch(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    short_description: str | None = Field(default=None, max_length=1000)
    display_order: int | None = Field(default=None, ge=0, le=10000)
    scopes: list[FundingProgramScopeWrite] | None = Field(default=None, min_length=1, max_length=20)
    reason: str = Field(min_length=8, max_length=2000)
    confirmed: Literal[True]

    @model_validator(mode="after")
    def _has_change(self) -> FundingProgramScopePatch:
        if all(
            value is None
            for value in (self.name, self.short_description, self.display_order, self.scopes)
        ):
            raise ValueError("At least one catalog field must change")
        return self
