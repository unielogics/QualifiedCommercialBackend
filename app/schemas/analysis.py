from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ORMModel
from app.schemas.prequal import PrequalRequestRead


AnalysisProduct = Literal["dscr_purchase", "dscr_refi", "fix_flip"]
AnalysisSource = Literal["deal_analyzer", "simulator", "loan_recalc"]


class ProviderSettingsRead(BaseModel):
    rentcast_configured: bool
    google_server_configured: bool
    google_maps_browser_key_configured: bool
    google_maps_ios_key_configured: bool = False
    google_maps_android_key_configured: bool = False
    google_maps_mobile_key_configured: bool = False
    rentcast_api_key: str | None = None
    google_server_api_key: str | None = None
    google_maps_browser_key: str | None = None
    google_maps_ios_key: str | None = None
    google_maps_android_key: str | None = None
    google_maps_mobile_key: str | None = None
    property_analysis_ai_enabled: bool = True
    property_intelligence_cache_ttl_hours: int = 24


class ProviderSettingsUpdate(BaseModel):
    rentcast_api_key: str | None = None
    google_server_api_key: str | None = None
    google_maps_browser_key: str | None = None
    google_maps_ios_key: str | None = None
    google_maps_android_key: str | None = None
    google_maps_mobile_key: str | None = None
    property_analysis_ai_enabled: bool | None = None
    property_intelligence_cache_ttl_hours: int | None = Field(default=None, ge=1, le=720)


class AddressParts(BaseModel):
    street: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    full: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class AddressAutocompleteRequest(BaseModel):
    input: str = Field(min_length=2, max_length=240)
    session_token: str | None = None


class AddressSuggestion(BaseModel):
    place_id: str
    text: str
    secondary_text: str | None = None


class AddressResolveRequest(BaseModel):
    place_id: str | None = None
    address: str | None = None
    session_token: str | None = None


class AddressResolveResponse(BaseModel):
    address: AddressParts
    google_place: dict[str, Any] | None = None


class PropertyIntelligenceLookupRequest(BaseModel):
    address: AddressParts
    client_id: UUID | None = None
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    property_type: str | None = None
    bedrooms: float | None = None
    bathrooms: float | None = None
    square_footage: float | None = None
    force_refresh: bool = False


class PropertyIntelligenceSnapshotRead(ORMModel):
    id: UUID
    created_by_id: UUID | None
    client_id: UUID | None
    deal_id: UUID | None
    loan_id: UUID | None
    normalized_address: str
    address_hash: str
    source_status: dict[str, Any] | None
    address: dict[str, Any]
    google_place: dict[str, Any] | None
    rentcast_property: dict[str, Any] | None
    rentcast_value: dict[str, Any] | None
    rentcast_rent: dict[str, Any] | None
    rentcast_market: dict[str, Any] | None
    fema_flood: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class AnalysisRunCreate(BaseModel):
    product: AnalysisProduct
    tool_source: AnalysisSource = "deal_analyzer"
    title: str | None = Field(default=None, max_length=180)
    client_id: UUID | None = None
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    property_snapshot_id: UUID | None = None
    target_property_address: str | None = Field(default=None, max_length=500)
    inputs: dict[str, Any] = Field(default_factory=dict)
    calculator_output: dict[str, Any] | None = None


class AnalysisRunUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=180)
    client_id: UUID | None = None
    deal_id: UUID | None = None
    loan_id: UUID | None = None
    property_snapshot_id: UUID | None = None
    target_property_address: str | None = Field(default=None, max_length=500)
    inputs: dict[str, Any] | None = None
    calculator_output: dict[str, Any] | None = None
    status: str | None = Field(default=None, max_length=24)


class AnalysisRunRead(ORMModel):
    id: UUID
    created_by_id: UUID | None
    client_id: UUID | None
    deal_id: UUID | None
    loan_id: UUID | None
    property_snapshot_id: UUID | None
    prequal_request_id: UUID | None
    product: str
    tool_source: str
    status: str
    title: str
    target_property_address: str | None
    inputs: dict[str, Any]
    calculator_output: dict[str, Any] | None
    ai_report: dict[str, Any] | None
    sanitized_client_report: dict[str, Any] | None
    report_version: int
    shared_at: datetime | None
    shared_by_id: UUID | None
    created_at: datetime
    updated_at: datetime


class ManualCreditOverrideIn(BaseModel):
    fico: int = Field(ge=300, le=850)
    property_count: int = Field(default=0, ge=0)
    has_year_of_ownership: bool = False


class AnalysisRunPrequalRequest(BaseModel):
    expected_closing_date: str | None = None
    borrower_entity: str | None = Field(default=None, max_length=500)
    manual_credit_override: ManualCreditOverrideIn | None = None
    notes: str | None = Field(default=None, max_length=2000)


class AnalysisRunPrequalResponse(BaseModel):
    analysis_run: AnalysisRunRead
    prequal_request: PrequalRequestRead


class ShareAnalysisResponse(BaseModel):
    analysis_run: AnalysisRunRead
    shared: bool = True
