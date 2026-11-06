"""Wire types for a working draft and a deliberately saved financial version."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

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


class DebtBatchCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family_id: UUID | None = None
    expected_revision: int = Field(ge=0)
    file_ids: list[UUID] = Field(min_length=1, max_length=20)
    idempotency_key: UUID
    role: Literal["supporting", "agreement", "payoff"] = "supporting"


class DebtBatchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_id: UUID
    action: Literal["create", "update", "link", "skip"]
    target_row_id: UUID | None = None
    selected_keys: list[str] = Field(default_factory=list, max_length=1000)
    confirmed: bool = False
    confirm_separate_loan: bool = False


class DebtBatchApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    idempotency_key: UUID
    decisions: list[DebtBatchDecision] = Field(min_length=0, max_length=1000)


class DebtBatchRetry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    file_ids: list[UUID] | None = Field(default=None, max_length=20)


class DebtBatchRefresh(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    idempotency_key: UUID
