"""Wire types for a working draft and a deliberately saved financial version."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

FinancialKind = Literal["pfs", "debt_schedule", "p_and_l", "balance_sheet", "real_estate_schedule"]


class FinancialFamilyCreate(BaseModel):
    kind: FinancialKind
    label: str | None = Field(default=None, min_length=1, max_length=255)
    identity: dict[str, Any] = Field(default_factory=dict)


class FinancialDraftWrite(BaseModel):
    kind: FinancialKind
    family_id: UUID | None = None
    body: dict[str, Any]
    expected_revision: int = Field(ge=0)


class FinancialVersionSave(BaseModel):
    kind: FinancialKind
    family_id: UUID | None = None
    expected_revision: int = Field(ge=0)
    base_version_id: UUID | None = None
    idempotency_key: str = Field(min_length=8, max_length=128)
    submit: bool = False


class FinancialImportCreate(BaseModel):
    kind: FinancialKind
    file_id: UUID
    family_id: UUID | None = None


class FinancialImportApply(BaseModel):
    expected_revision: int = Field(ge=0)
    selected_keys: list[str] | None = Field(default=None, max_length=5000)


class FinancialVersionRestore(BaseModel):
    expected_revision: int = Field(ge=0)
