"""Private administrative configuration for public catalog delivery."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CatalogOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")

    included: bool = True
    title: str | None = Field(default=None, max_length=150)
    description: str | None = Field(default=None, max_length=5000)
    canonical_url: str | None = Field(default=None, max_length=2048)
    image_url: str | None = Field(default=None, max_length=2048)
    actual_price: str | None = Field(default=None, max_length=40)
    content_approved: bool = False
    pricing_confirmed: bool = False
    approved_source_revision: str | None = Field(default=None, max_length=64)

    @field_validator("actual_price")
    @classmethod
    def price(cls, value: str | None) -> str | None:
        if not value:
            return None
        if not re.fullmatch(r"\d{1,12}(?:\.\d{1,2})?", value):
            raise ValueError("Use a positive purchase price with at most two decimal places.")
        parsed = Decimal(value)
        if parsed <= 0:
            raise ValueError("Purchase price must be positive; a loan amount or rate is not a price.")
        return f"{parsed:.2f}"


class FeedSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    revision: int = Field(default=0, ge=0)
    enabled: bool = False
    acceptance_confirmed: bool = False
    acceptance_reference: str = Field(default="", max_length=2000)
    sftp_host: str = Field(default="", max_length=253)
    sftp_port: int = Field(default=22, ge=1, le=65535)
    sftp_username: str = Field(default="", max_length=128)
    server_fingerprint: str = Field(default="", max_length=100)
    remote_directory: str = Field(default=".", max_length=512)
    staging_directory: str = Field(default="", max_length=512)
    staging_isolation_confirmed: bool = False
    filename: str = "qualified-commercial.csv.gz"
    schedule_hour_utc: int = Field(default=10, ge=0, le=23)
    brand: str = Field(default="Qualified Commercial", min_length=1, max_length=150)
    seller_name: str = Field(default="Qualified Commercial", min_length=1, max_length=150)
    currency: Literal["USD"] = "USD"
    catalog_overrides: dict[str, CatalogOverride] = Field(default_factory=dict, max_length=500)

    @field_validator("sftp_host", "sftp_username", "server_fingerprint", "remote_directory", "staging_directory")
    @classmethod
    def no_controls(cls, value: str) -> str:
        value = value.strip()
        if any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("Connection details cannot contain control characters.")
        return value

    @field_validator("filename")
    @classmethod
    def stable_filename(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,90}\.csv\.gz", value):
            raise ValueError("Use a filename ending in .csv.gz, without a directory.")
        return value

    @model_validator(mode="after")
    def approval_reference(self):
        if self.acceptance_confirmed and not self.acceptance_reference.strip():
            raise ValueError("Record OpenAI's acceptance and agreed pricing treatment before enabling delivery.")
        return self


class PreviewRequest(BaseModel):
    settings: FeedSettings | None = None


class KeyPairRequest(BaseModel):
    rotate: bool = False


class SyncRequest(BaseModel):
    idempotency_key: str | None = Field(default=None, min_length=8, max_length=160)
