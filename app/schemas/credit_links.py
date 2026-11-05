"""Firm-level credit pull linking: the mode switch and the sweep report."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreditLinkingRead(BaseModel):
    mode: Literal["manual", "automatic"]
    last_run: dict[str, Any] | None = None
    sweep_minutes: int


class CreditLinkingWrite(BaseModel):
    mode: Literal["manual", "automatic"]


class CreditLinkRunReport(BaseModel):
    at: str
    how: str
    pulls_checked: int
    linked: list[dict[str, Any]] = Field(default_factory=list)
    unmatched: list[dict[str, Any]] = Field(default_factory=list)
    words: str
