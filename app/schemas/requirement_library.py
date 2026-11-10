"""Request and response shapes for the requirement library (app/services/requirement_library.py)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

LibraryCategory = Literal[
    "financials",
    "borrower_info",
    "credit",
    "compliance",
    "agreements",
    "property_data",
    "insurance",
    "title_and_escrow",
    "appraisal_and_inspection",
]
RequiredLevel = Literal["required", "recommended", "optional"]


class LibraryItemRead(BaseModel):
    key: str
    label: str
    category: str
    category_label: str
    kind: str
    required_level: RequiredLevel
    description: str = ""
    #: The first online form this item is filled in through, when it is one of the four.
    form_kind: Literal["pfs", "debt_schedule", "p_and_l", "balance_sheet", "real_estate_schedule", "operating_plan"] | None = None
    form_kinds: list[Literal["pfs", "debt_schedule", "p_and_l", "balance_sheet", "real_estate_schedule", "operating_plan"]] = Field(default_factory=list)
    #: False for an internal review the client is never asked for.
    client_visible: bool = True
    verification_required: bool = True
    #: library (a published playbook), baseline (code-defined) or custom (typed by the desk).
    source: Literal["library", "baseline", "custom"]
    #: The file already requires it, however it got there.
    on_file: bool = False
    #: It is on the file because the desk added it, so the desk can take it off.
    added: bool = False
    source_requirement_id: UUID | None = None


class LibraryCategoryRead(BaseModel):
    key: str
    label: str


class RequirementLibraryRead(BaseModel):
    profile_id: UUID
    items: list[LibraryItemRead] = Field(default_factory=list)
    categories: list[LibraryCategoryRead] = Field(default_factory=list)


class CustomRequirementWrite(BaseModel):
    label: str = Field(min_length=2, max_length=120)
    category: LibraryCategory
    instructions: str | None = Field(default=None, max_length=1000)


class RequirementAddWrite(BaseModel):
    """One library key, or a custom item, never both."""

    requirement_key: str | None = Field(default=None, min_length=1, max_length=120)
    required_level: RequiredLevel | None = None
    note: str | None = Field(default=None, max_length=500)
    custom: CustomRequirementWrite | None = None

    @model_validator(mode="after")
    def exactly_one_source(self) -> RequirementAddWrite:
        if bool(self.requirement_key) == bool(self.custom):
            raise ValueError("Send either requirement_key or custom, not both and not neither")
        return self
