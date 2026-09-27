"""Structured, staff-facing document review policy, never borrower instructions."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, Field, field_validator


class DocumentReviewCheck(BaseModel):
    key: str = Field(min_length=1, max_length=71)
    label: str = Field(min_length=2, max_length=160)
    instructions: str = Field(min_length=1, max_length=2000)
    severity: Literal["review", "block"] = "review"

    @field_validator("key")
    @classmethod
    def _supported_key(cls, value: str) -> str:
        if value not in {
            "net_income_nonnegative",
            "net_income_not_declining",
            "revenue_not_declining",
            "no_mca_debits",
            "no_nsf",
            "positive_ending_balance",
            "custom",
        } and not re.fullmatch(r"custom_[a-z0-9_]{1,64}", value):
            raise ValueError("Unsupported document review check key")
        return value

    @field_validator("label", "instructions", mode="before")
    @classmethod
    def _trim_text(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


def _unique_checks(values: list[DocumentReviewCheck]) -> list[DocumentReviewCheck]:
    keys = [value.key for value in values]
    if len(keys) != len(set(keys)):
        raise ValueError("Document review check keys must be unique within a requirement")
    return values


DocumentReviewChecks = Annotated[
    list[DocumentReviewCheck], Field(max_length=20), AfterValidator(_unique_checks)
]
