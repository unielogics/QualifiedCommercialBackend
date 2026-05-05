"""Pydantic shared response shapes."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class IdName(ORMModel):
    id: UUID
    name: str


class TimestampedMixin(BaseModel):
    created_at: datetime
    updated_at: datetime


__all__ = ["ORMModel", "IdName", "TimestampedMixin", "UUID", "date", "datetime", "Any"]
