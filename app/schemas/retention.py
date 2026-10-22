"""Contracts for governed record-retention controls."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class LegalHoldChange(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)

    @field_validator("reason")
    @classmethod
    def validate_reason(cls, value: str) -> str:
        reason = value.strip()
        if len(reason) < 3:
            raise ValueError("reason must contain at least 3 non-whitespace characters")
        return reason


class RetentionStatusRead(BaseModel):
    id: UUID
    resource_type: Literal["application_profile", "ai_intake", "bucket"]
    retention_class: str
    protected_until: datetime
    legal_hold: bool
    effective_legal_hold: bool = False
    hold_sources: list[str] = Field(default_factory=list)
    retention_state: Literal[
        "legal_hold", "protected", "expired", "unclassified"
    ]

