from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class MortgageStatementImport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family_id: UUID
    file_id: UUID
    expected_revision: int = Field(ge=0)
    property_row_id: UUID | None = None
    mortgage_row_id: UUID | None = None
    source_mortgage_id: UUID | None = None
    create_property: bool = False
    create_mortgage: bool = False


class MortgageStatementApply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    keys: list[str] = Field(default_factory=list, max_length=100)
    confirm_association: bool = False
    confirm_latest_statement: bool = False
    association_override_reason: str = Field(default="", max_length=1000)


class MortgageStatementUnlink(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family_id: UUID
    expected_revision: int = Field(ge=0)
    mortgage_row_id: UUID
    import_id: UUID | None = None
    file_id: UUID | None = None
