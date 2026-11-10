"""Staff-disclosed security offered for this file, separate from asset ownership."""
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Amount = Annotated[Decimal, Field(ge=0, le=999_999_999_999, decimal_places=2)]


class CollateralAddress(BaseModel):
    model_config = ConfigDict(extra="forbid")
    street: str = Field(default="", max_length=300)
    city: str = Field(default="", max_length=120)
    state: str = Field(default="", max_length=80)
    postal_code: str = Field(default="", max_length=20)

    @field_validator("street", "city", "state", "postal_code")
    @classmethod
    def trim(cls, value):
        return value.strip()


class CollateralProperty(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: UUID
    address: CollateralAddress
    owner_name: str | None = Field(default=None, max_length=180)
    property_type: str | None = Field(default=None, max_length=80)
    estimated_value: Amount | None = None
    existing_liens_total: Amount | None = None
    available_lien_amount: Amount | None = None
    proposed_lien_position: int | None = Field(default=None, ge=1, le=10)
    valuation_date: date | None = None
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    notes: str | None = Field(default=None, max_length=2000)
    source_file_ids: list[UUID] = Field(default_factory=list, max_length=20)
    real_estate_property_id: UUID | None = None

    @field_validator("source_file_ids")
    @classmethod
    def distinct_sources(cls, value):
        return list(dict.fromkeys(value))


class CollateralPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=0)
    enabled: bool
    properties: list[CollateralProperty] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_properties(self):
        if len({row.id for row in self.properties}) != len(self.properties):
            raise ValueError("Collateral properties require distinct row IDs")
        if self.enabled and not self.properties:
            raise ValueError("Add a property before including real estate collateral")
        if self.enabled and any(not all((row.address.street, row.address.city, row.address.state, row.address.postal_code)) for row in self.properties):
            raise ValueError("Enter the complete property address, including city, state and postal code")
        return self


class CollateralRead(BaseModel):
    profile_id: UUID
    enabled: bool = False
    properties: list[CollateralProperty] = Field(default_factory=list)
    revision: int = 0
    updated_at: datetime | None = None
    updated_by_user_id: UUID | None = None
    can_edit: bool = False
    property_options: list[dict] = Field(default_factory=list)
    source_file_options: list[dict] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
