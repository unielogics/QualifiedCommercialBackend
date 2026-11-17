"""Firm-level credit pull linking: the mode switch and the sweep report."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class CreditLinkingRead(BaseModel):
    mode: Literal["manual", "automatic"]
    last_run: dict[str, Any] | None = None
    sweep_minutes: int
    #: A completed soft pull younger than this is reused instead of pulling again.
    reuse_window_days: int = 30


class CreditLinkingWrite(BaseModel):
    mode: Literal["manual", "automatic"] | None = None
    reuse_window_days: int | None = Field(default=None, ge=0, le=365)


class CreditLinkRunReport(BaseModel):
    at: str
    how: str
    pulls_checked: int
    linked: list[dict[str, Any]] = Field(default_factory=list)
    unmatched: list[dict[str, Any]] = Field(default_factory=list)
    words: str
