"""A filled-in financial form, read back the way a person reads it."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class FormAnswerLine(BaseModel):
    label: str
    #: Already formatted for the screen: "$25,000", "Yes", "Nov 5, 2026".
    value: str
    #: The figure behind `value`, when there is one.
    amount: float | None = None
    note: str | None = None


class FormAnswerSection(BaseModel):
    key: str
    title: str
    rows: list[FormAnswerLine] = Field(default_factory=list)
    total: FormAnswerLine | None = None
    #: Lines the form offers that were left empty. Said, never listed.
    left_blank: int = 0
    #: A long section the screen folds away until it is opened.
    collapsed: bool = False


class FormAnswerTable(BaseModel):
    key: str
    title: str
    columns: list[str]
    rows: list[list[str]] = Field(default_factory=list)
    total_row: list[str] | None = None


class FormAnswerFile(BaseModel):
    file_id: UUID
    file_name: str
    created_at: datetime
    #: "filled_form" is a PDF we drew from typed answers; "uploaded" is a
    #: document someone sent.
    origin: Literal["filled_form", "uploaded"]


class FormAnswersRead(BaseModel):
    kind: Literal["pfs", "debt_schedule", "p_and_l", "balance_sheet"]
    label: str
    source: Literal["filled", "uploaded", "none"]
    #: Whether the figures are what somebody typed or what the AI read off a
    #: document. A typed form and a scan carry different weight.
    figures_from: Literal["form", "document"] | None = None
    #: A draft was started but never submitted.
    draft: bool = False
    who: str | None = None
    as_of: str | None = None
    #: "the borrower", "the desk", "a document we read" — whoever's answers
    #: these are.
    filled_by: str | None = None
    updated_at: datetime | None = None
    headline: list[FormAnswerLine] = Field(default_factory=list)
    sections: list[FormAnswerSection] = Field(default_factory=list)
    tables: list[FormAnswerTable] = Field(default_factory=list)
    notes: str | None = None
    files: list[FormAnswerFile] = Field(default_factory=list)
    #: One sentence for when there is nothing to show, so an empty screen
    #: says why.
    empty_reason: str | None = None
