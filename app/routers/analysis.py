from __future__ import annotations

import uuid
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import CurrentUser, require_role
from app.enums import Role
from app.models.activity import Activity
from app.models.analysis_run import AnalysisRun
from app.models.app_settings import AppSettings
from app.models.client import Client
from app.models.deal import Deal
from app.models.loan import Loan
from app.models.prequal_request import PrequalRequest
from app.models.property_intelligence import PropertyIntelligenceSnapshot
from app.schemas.analysis import (
    AddressAutocompleteRequest,
    AddressResolveRequest,
    AddressResolveResponse,
    AddressSuggestion,
    AnalysisRunCreate,
    AnalysisRunPrequalRequest,
    AnalysisRunPrequalResponse,
    AnalysisRunRead,
    AnalysisRunUpdate,
    ProviderSettingsRead,
    ProviderSettingsUpdate,
    PropertyIntelligenceLookupRequest,
    PropertyIntelligenceSnapshotRead,
    ShareAnalysisResponse,
)
from app.schemas.prequal import PrequalRequestRead
from app.schemas.settings import AppSettingsData
from app.scoping import scope_client_query, scope_loan_query
from app.services.analysis_reports import generate_analysis_report
from app.services.property_intelligence import (
    google_autocomplete,
    google_resolve,
    lookup_property_intelligence,
    log_provider_usage,
)
from app.services.provider_secrets import provider_settings_status, set_secret


router = APIRouter(prefix="/analysis-runs", tags=["analysis-runs"])
property_router = APIRouter(prefix="/property-intelligence", tags=["property-intelligence"])


def _is_operator(user) -> bool:
    return user.role in {Role.SUPER_ADMIN, Role.LOAN_EXEC}


def _actor_label(user) -> str:
    role = getattr(user, "role", None)
    return role.value if hasattr(role, "value") else str(role or "user")


def _to_read(row: AnalysisRun) -> AnalysisRunRead:
    return AnalysisRunRead.model_validate(row)


def _float_from(*values: Any) -> float | None:
    for value in values:
        if isinstance(value, (int, float)):
            return float(value)
        if value is None:
            continue
        try:
            raw = str(value).replace(",", "").strip()
            if raw:
                return float(raw)
        except (TypeError, ValueError):
            continue
    return None


def _non_empty_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        raw = str(value).strip()
        if raw:
            return raw
    return None


def _parse_date(value: str | date | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "expected_closing_date must be YYYY-MM-DD") from exc


async def _get_app_settings(db: AsyncSession) -> AppSettings:
    row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    if row is None:
        row = AppSettings(id=uuid.uuid4(), singleton=True, data=AppSettingsData().model_dump(mode="json"))
        db.add(row)
        await db.flush()
        await db.refresh(row)
    return row


async def _require_client_access(db: AsyncSession, user, client_id: UUID | None) -> Client | None:
    if client_id is None:
        return None
    stmt = scope_client_query(user, select(Client).where(Client.id == client_id))
    client = (await db.execute(stmt)).scalar_one_or_none()
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")
    return client


async def _require_loan_access(db: AsyncSession, user, loan_id: UUID | None) -> Loan | None:
    if loan_id is None:
        return None
    stmt = scope_loan_query(user, select(Loan).where(Loan.id == loan_id))
    loan = (await db.execute(stmt)).scalar_one_or_none()
    if loan is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Loan not found")
    return loan


async def _require_deal_access(db: AsyncSession, user, deal_id: UUID | None) -> Deal | None:
    if deal_id is None:
        return None
    deal = (await db.execute(select(Deal).where(Deal.id == deal_id))).scalar_one_or_none()
    if deal is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Deal not found")
    if _is_operator(user):
        return deal
    client = await db.get(Client, deal.client_id)
    if user.role == Role.CLIENT and user.client is not None and deal.client_id == user.client.id:
        return deal
    if user.role == Role.BROKER and user.broker is not None and client is not None and client.broker_id == user.broker.id:
        return deal
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Deal is not visible to this user")


async def _require_snapshot_access(
    db: AsyncSession,
    user,
    snapshot_id: UUID | None,
) -> PropertyIntelligenceSnapshot | None:
    if snapshot_id is None:
        return None
    row = await db.get(PropertyIntelligenceSnapshot, snapshot_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Property intelligence snapshot not found")
    if _is_operator(user) or row.created_by_id == user.id:
        return row
    if row.client_id is not None:
        await _require_client_access(db, user, row.client_id)
        return row
    if row.loan_id is not None:
        await _require_loan_access(db, user, row.loan_id)
        return row
    if row.deal_id is not None:
        await _require_deal_access(db, user, row.deal_id)
        return row
    raise HTTPException(status.HTTP_403_FORBIDDEN, "Snapshot is not visible to this user")


def _scope_analysis_query(user, stmt):
    if user.role == Role.CLIENT:
        if user.client is None:
            return stmt.where(False)
        return stmt.where(AnalysisRun.client_id == user.client.id, AnalysisRun.shared_at.is_not(None))
    if user.role == Role.BROKER:
        if user.broker is None:
            return stmt.where(False)
        broker_client_ids = select(Client.id).where(Client.broker_id == user.broker.id)
        broker_loan_ids = select(Loan.id).where(Loan.broker_id == user.broker.id)
        return stmt.where(
            or_(
                AnalysisRun.created_by_id == user.id,
                AnalysisRun.client_id.in_(broker_client_ids),
                AnalysisRun.loan_id.in_(broker_loan_ids),
            )
        )
    return stmt


async def _load_analysis_run(db: AsyncSession, user, run_id: UUID) -> AnalysisRun:
    row = (
        await db.execute(
            _scope_analysis_query(user, select(AnalysisRun).where(AnalysisRun.id == run_id))
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Analysis run not found")
    return row


async def _validate_links(
    db: AsyncSession,
    user,
    *,
    client_id: UUID | None,
    deal_id: UUID | None,
    loan_id: UUID | None,
    property_snapshot_id: UUID | None,
) -> tuple[Client | None, Deal | None, Loan | None, PropertyIntelligenceSnapshot | None]:
    client = await _require_client_access(db, user, client_id)
    deal = await _require_deal_access(db, user, deal_id)
    loan = await _require_loan_access(db, user, loan_id)
    snapshot = await _require_snapshot_access(db, user, property_snapshot_id)
    if client is not None:
        if loan is not None and loan.client_id != client.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Loan does not belong to linked client")
        if deal is not None and deal.client_id != client.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Deal does not belong to linked client")
        if snapshot is not None and snapshot.client_id is not None and snapshot.client_id != client.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Snapshot does not belong to linked client")
    return client, deal, loan, snapshot


def _default_title(product: str, address: str | None) -> str:
    label = {
        "dscr_purchase": "DSCR purchase",
        "dscr_refi": "DSCR refinance",
        "fix_flip": "Fix & Flip",
    }.get(product, "Analysis")
    return f"{label} - {address}"[:180] if address else label


async def _refresh_report(db: AsyncSession, user, row: AnalysisRun, snapshot: PropertyIntelligenceSnapshot | None) -> None:
    ai_report, client_report = await generate_analysis_report(
        db,
        product=row.product,
        inputs=row.inputs or {},
        calculator_output=row.calculator_output,
        snapshot=snapshot,
        user=user,
        client_id=row.client_id,
        loan_id=row.loan_id,
    )
    row.ai_report = ai_report
    row.sanitized_client_report = client_report


def _prequal_payload_from_run(row: AnalysisRun) -> dict[str, Any]:
    inputs = row.inputs or {}
    calc = row.calculator_output or {}
    report = row.ai_report or {}
    product = row.product
    purchase = _float_from(
        inputs.get("purchase_price"),
        inputs.get("market_value"),
        inputs.get("property_value"),
        inputs.get("as_is_value"),
        report.get("purchase_or_value"),
    )
    requested = _float_from(
        inputs.get("requested_loan_amount"),
        inputs.get("loan_amount"),
        calc.get("requested_loan_amount"),
        calc.get("loan_amount"),
        report.get("requested_loan_amount"),
    )
    arv = _float_from(inputs.get("arv"), inputs.get("arv_estimate"), calc.get("arv"))
    if product == "fix_flip":
        purchase = _float_from(inputs.get("brv"), inputs.get("purchase_price"), inputs.get("as_is_value"), purchase)
        requested = _float_from(inputs.get("requested_loan_amount"), inputs.get("loan_amount"), calc.get("total_loan_amount"), requested)
    if not purchase or purchase <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Analysis run is missing a purchase/value number")
    if not requested or requested <= 0:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Analysis run is missing a requested loan amount")
    address = _non_empty_text(
        row.target_property_address,
        inputs.get("target_property_address"),
        inputs.get("address"),
        inputs.get("property_address"),
    )
    if not address:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Analysis run is missing a target property address")
    notes = _non_empty_text(
        report.get("narrative"),
        inputs.get("notes"),
        "Created from saved analysis run.",
    )
    sow_items = inputs.get("sow_items")
    if product == "fix_flip" and not sow_items:
        rehab = _float_from(inputs.get("rehab_cost"), inputs.get("rehab_budget"), calc.get("total_construction"))
        if rehab and rehab > 0:
            sow_items = [{"category": "Rehab", "description": "Estimated rehab budget", "total_usd": rehab}]
    return {
        "target_property_address": address,
        "purchase_price": purchase,
        "requested_loan_amount": requested,
        "loan_type": product,
        "borrower_notes": notes[:2000] if notes else None,
        "arv_estimate": arv if product == "fix_flip" else None,
        "sow_items": sow_items if product == "fix_flip" and isinstance(sow_items, list) else None,
    }


def _manual_credit_from_run(row: AnalysisRun, explicit: dict[str, Any] | None) -> dict[str, Any] | None:
    if explicit:
        return explicit
    inputs = row.inputs or {}
    fico = _float_from(inputs.get("fico"), inputs.get("borrower_fico"), inputs.get("credit_score"), inputs.get("effective_fico"))
    if fico is None:
        return None
    return {
        "fico": int(fico),
        "property_count": int(_float_from(inputs.get("property_count"), inputs.get("owned_property_count")) or 0),
        "has_year_of_ownership": bool(inputs.get("has_year_of_ownership") or inputs.get("year_of_ownership")),
    }


@property_router.get("/provider-settings", response_model=ProviderSettingsRead)
async def get_provider_settings(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProviderSettingsRead:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    return ProviderSettingsRead(**await provider_settings_status(db))


@property_router.patch(
    "/provider-settings",
    response_model=ProviderSettingsRead,
    dependencies=[Depends(require_role(Role.SUPER_ADMIN))],
)
async def update_provider_settings(
    payload: ProviderSettingsUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ProviderSettingsRead:
    data = payload.model_dump(exclude_unset=True)
    for key in (
        "rentcast_api_key",
        "google_server_api_key",
        "google_maps_browser_key",
        "google_maps_ios_key",
        "google_maps_android_key",
        "google_maps_mobile_key",
    ):
        value = data.pop(key, None)
        if isinstance(value, str) and value.strip():
            await set_secret(db, key=key, value=value.strip(), updated_by_id=user.id)

    if data:
        row = await _get_app_settings(db)
        current = AppSettingsData.model_validate(row.data or {}).model_dump(mode="json")
        pi = current.get("property_intelligence") or {}
        if "property_analysis_ai_enabled" in data:
            pi["ai_report_enabled"] = bool(data["property_analysis_ai_enabled"])
        if "property_intelligence_cache_ttl_hours" in data and data["property_intelligence_cache_ttl_hours"] is not None:
            pi["cache_ttl_hours"] = int(data["property_intelligence_cache_ttl_hours"])
        current["property_intelligence"] = pi
        row.data = current
        db.add(
            Activity(
                loan_id=None,
                actor_id=user.id,
                actor_label=_actor_label(user),
                kind="settings.updated",
                summary="Updated property intelligence provider settings",
                payload={"property_intelligence": pi, "provider_secret_keys": list(payload.model_fields_set)},
            )
        )
    await db.flush()
    return ProviderSettingsRead(**await provider_settings_status(db))


@property_router.post("/address/autocomplete", response_model=list[AddressSuggestion])
async def autocomplete_address(
    payload: AddressAutocompleteRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[AddressSuggestion]:
    rows = await google_autocomplete(db, payload.input, payload.session_token)
    await log_provider_usage(
        db,
        provider="google",
        feature="property_intelligence",
        request_type="places_autocomplete",
        user=user,
        metadata={"result_count": len(rows), "session_token": bool(payload.session_token)},
    )
    return [AddressSuggestion(**r) for r in rows]


@property_router.post("/address/resolve", response_model=AddressResolveResponse)
async def resolve_address(
    payload: AddressResolveRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AddressResolveResponse:
    if not payload.place_id and not payload.address:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "place_id or address is required")
    address, google_place = await google_resolve(
        db,
        place_id=payload.place_id,
        address=payload.address,
        session_token=payload.session_token,
    )
    await log_provider_usage(
        db,
        provider="google",
        feature="property_intelligence",
        request_type="place_resolve",
        user=user,
        metadata={"place_id": payload.place_id, "session_token": bool(payload.session_token)},
    )
    return AddressResolveResponse(address=address, google_place=google_place)


@property_router.post("/lookup", response_model=PropertyIntelligenceSnapshotRead)
async def property_lookup(
    payload: PropertyIntelligenceLookupRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> PropertyIntelligenceSnapshotRead:
    await _validate_links(
        db,
        user,
        client_id=payload.client_id,
        deal_id=payload.deal_id,
        loan_id=payload.loan_id,
        property_snapshot_id=None,
    )
    row = await lookup_property_intelligence(db, payload=payload, user=user)
    return PropertyIntelligenceSnapshotRead.model_validate(row)


@router.post("", response_model=AnalysisRunRead, status_code=status.HTTP_201_CREATED)
async def create_analysis_run(
    payload: AnalysisRunCreate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AnalysisRunRead:
    client, deal, loan, snapshot = await _validate_links(
        db,
        user,
        client_id=payload.client_id,
        deal_id=payload.deal_id,
        loan_id=payload.loan_id,
        property_snapshot_id=payload.property_snapshot_id,
    )
    client_id = client.id if client is not None else loan.client_id if loan is not None else deal.client_id if deal is not None else None
    address = _non_empty_text(
        payload.target_property_address,
        (snapshot.address or {}).get("full") if snapshot else None,
        getattr(loan, "address", None),
        getattr(deal, "address", None),
        payload.inputs.get("address"),
    )
    row = AnalysisRun(
        created_by_id=user.id,
        client_id=client_id,
        deal_id=deal.id if deal is not None else payload.deal_id,
        loan_id=loan.id if loan is not None else payload.loan_id,
        property_snapshot_id=snapshot.id if snapshot is not None else None,
        product=payload.product,
        tool_source=payload.tool_source,
        status="saved" if payload.calculator_output else "draft",
        title=payload.title or _default_title(payload.product, address),
        target_property_address=address,
        inputs=payload.inputs,
        calculator_output=payload.calculator_output,
    )
    db.add(row)
    await db.flush()
    await _refresh_report(db, user, row, snapshot)
    db.add(
        Activity(
            loan_id=row.loan_id,
            client_id=row.client_id if row.loan_id is None else None,
            actor_id=user.id,
            actor_label=_actor_label(user),
            kind="analysis.created",
            summary=f"Saved {row.product.replace('_', ' ')} analysis",
            payload={"analysis_run_id": str(row.id), "product": row.product, "tool_source": row.tool_source},
        )
    )
    await db.flush()
    await db.refresh(row)
    return _to_read(row)


@router.get("", response_model=list[AnalysisRunRead])
async def list_analysis_runs(
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
    client_id: UUID | None = Query(None),
    loan_id: UUID | None = Query(None),
    product: str | None = Query(None),
) -> list[AnalysisRunRead]:
    stmt = _scope_analysis_query(user, select(AnalysisRun))
    if client_id is not None:
        await _require_client_access(db, user, client_id)
        stmt = stmt.where(AnalysisRun.client_id == client_id)
    if loan_id is not None:
        await _require_loan_access(db, user, loan_id)
        stmt = stmt.where(AnalysisRun.loan_id == loan_id)
    if product:
        stmt = stmt.where(AnalysisRun.product == product)
    rows = (await db.execute(stmt.order_by(AnalysisRun.updated_at.desc()))).scalars().all()
    return [_to_read(r) for r in rows]


@router.get("/{run_id}", response_model=AnalysisRunRead)
async def get_analysis_run(
    run_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AnalysisRunRead:
    return _to_read(await _load_analysis_run(db, user, run_id))


@router.patch("/{run_id}", response_model=AnalysisRunRead)
async def update_analysis_run(
    run_id: UUID,
    payload: AnalysisRunUpdate,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AnalysisRunRead:
    row = await _load_analysis_run(db, user, run_id)
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client users cannot edit analysis runs")
    data = payload.model_dump(exclude_unset=True)
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "No updates supplied")
    client, deal, loan, snapshot = await _validate_links(
        db,
        user,
        client_id=data.get("client_id", row.client_id),
        deal_id=data.get("deal_id", row.deal_id),
        loan_id=data.get("loan_id", row.loan_id),
        property_snapshot_id=data.get("property_snapshot_id", row.property_snapshot_id),
    )
    regen = False
    for key in ("title", "status", "target_property_address"):
        if key in data:
            setattr(row, key, data[key])
    if "client_id" in data:
        row.client_id = client.id if client is not None else None
    if "deal_id" in data:
        row.deal_id = deal.id if deal is not None else None
    if "loan_id" in data:
        row.loan_id = loan.id if loan is not None else None
    if "property_snapshot_id" in data:
        row.property_snapshot_id = snapshot.id if snapshot is not None else None
        regen = True
    if "inputs" in data:
        row.inputs = data["inputs"] or {}
        regen = True
    if "calculator_output" in data:
        row.calculator_output = data["calculator_output"]
        regen = True
    if regen:
        await _refresh_report(db, user, row, snapshot)
        row.report_version += 1
    await db.flush()
    await db.refresh(row)
    return _to_read(row)


@router.post("/{run_id}/share-to-client", response_model=ShareAnalysisResponse)
async def share_analysis_to_client(
    run_id: UUID,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ShareAnalysisResponse:
    if user.role == Role.CLIENT:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Client users cannot share analysis runs")
    row = await _load_analysis_run(db, user, run_id)
    if row.client_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Link an owned client before sharing this analysis")
    await _require_client_access(db, user, row.client_id)
    if row.sanitized_client_report is None:
        snapshot = await _require_snapshot_access(db, user, row.property_snapshot_id)
        await _refresh_report(db, user, row, snapshot)
    row.shared_at = datetime.now(timezone.utc)
    row.shared_by_id = user.id
    row.status = "shared"
    db.add(
        Activity(
            loan_id=row.loan_id,
            client_id=row.client_id if row.loan_id is None else None,
            actor_id=user.id,
            actor_label=_actor_label(user),
            kind="analysis.shared_to_client",
            summary=f"Shared {row.product.replace('_', ' ')} analysis with client",
            payload={"analysis_run_id": str(row.id), "product": row.product},
        )
    )
    await db.flush()
    await db.refresh(row)
    return ShareAnalysisResponse(analysis_run=_to_read(row), shared=True)


@router.post("/{run_id}/prequal-request", response_model=AnalysisRunPrequalResponse, status_code=status.HTTP_201_CREATED)
async def convert_analysis_to_prequal(
    run_id: UUID,
    payload: AnalysisRunPrequalRequest,
    user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AnalysisRunPrequalResponse:
    if user.role not in {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.BROKER}:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Operator role required")
    row = await _load_analysis_run(db, user, run_id)
    if row.client_id is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Link an owned client before creating a prequalification")
    client = await _require_client_access(db, user, row.client_id)
    if client is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Client not found")

    if row.prequal_request_id is not None:
        existing = await db.get(PrequalRequest, row.prequal_request_id)
        if existing is not None:
            return AnalysisRunPrequalResponse(analysis_run=_to_read(row), prequal_request=PrequalRequestRead.model_validate(existing))

    mapped = _prequal_payload_from_run(row)
    notes = _non_empty_text(payload.notes, mapped.get("borrower_notes"))
    req = PrequalRequest(
        loan_id=row.loan_id,
        requester_id=client.user_id or user.id,
        client_id=client.id,
        target_property_address=mapped["target_property_address"],
        purchase_price=mapped["purchase_price"],
        requested_loan_amount=mapped["requested_loan_amount"],
        loan_type=mapped["loan_type"],
        expected_closing_date=_parse_date(payload.expected_closing_date),
        borrower_notes=notes,
        borrower_entity=payload.borrower_entity,
        arv_estimate=mapped.get("arv_estimate"),
        sow_items=mapped.get("sow_items"),
        total_construction=(
            sum(_float_from(item.get("total_usd")) or 0 for item in mapped["sow_items"])
            if isinstance(mapped.get("sow_items"), list)
            else None
        ),
        manual_credit_override=_manual_credit_from_run(
            row,
            payload.manual_credit_override.model_dump() if payload.manual_credit_override else None,
        ),
        status="pending",
        source_analysis_run_id=row.id,
    )
    db.add(req)
    await db.flush()
    row.prequal_request_id = req.id
    row.status = "prequal_requested"
    db.add(
        Activity(
            loan_id=row.loan_id,
            client_id=row.client_id if row.loan_id is None else None,
            actor_id=user.id,
            actor_label=_actor_label(user),
            kind="analysis.prequal_requested",
            summary=f"Created pending prequalification from {row.product.replace('_', ' ')} analysis",
            payload={"analysis_run_id": str(row.id), "prequal_request_id": str(req.id), "product": row.product},
        )
    )
    await db.flush()
    await db.refresh(req)
    await db.refresh(row)
    return AnalysisRunPrequalResponse(
        analysis_run=_to_read(row),
        prequal_request=PrequalRequestRead.model_validate(req),
    )
