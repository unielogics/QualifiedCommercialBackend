from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.property_intelligence import PropertyIntelligenceSnapshot
from app.models.provider_usage_event import ProviderUsageEvent
from app.schemas.analysis import AddressParts, PropertyIntelligenceLookupRequest
from app.services.provider_secrets import runtime_settings

RENTCAST_BASE = "https://api.rentcast.io/v1"
GOOGLE_PLACES_BASE = "https://places.googleapis.com/v1"
GOOGLE_GEOCODE_BASE = "https://maps.googleapis.com/maps/api/geocode/json"
GOOGLE_STATIC_MAP = "https://maps.googleapis.com/maps/api/staticmap"
GEOAPIFY_AUTOCOMPLETE = "https://api.geoapify.com/v1/geocode/autocomplete"
GEOAPIFY_GEOCODE = "https://api.geoapify.com/v1/geocode/search"
GEOAPIFY_PLACE_DETAILS = "https://api.geoapify.com/v2/place-details"
GEOAPIFY_STATIC_MAP = "https://maps.geoapify.com/v1/staticmap"
FEMA_NFHL_LAYER = "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
logger = logging.getLogger(__name__)


def normalize_address(address: AddressParts | dict[str, Any]) -> str:
    data = address.model_dump() if isinstance(address, AddressParts) else address
    full = (data.get("full") or "").strip()
    if full:
        return " ".join(full.lower().split())
    parts = [data.get("street"), data.get("city"), data.get("state"), data.get("zip")]
    return " ".join(str(p).strip().lower() for p in parts if p)


def address_hash(normalized: str) -> str:
    return hashlib.sha256(normalized.encode()).hexdigest()


async def log_provider_usage(
    db: AsyncSession,
    *,
    provider: str,
    feature: str,
    request_type: str,
    user=None,
    client_id=None,
    loan_id=None,
    address_hash_value: str | None = None,
    status: str = "ok",
    http_status: int | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    db.add(
        ProviderUsageEvent(
            provider=provider,
            feature=feature,
            request_type=request_type,
            status=status,
            http_status=http_status,
            address_hash=address_hash_value,
            user_id=getattr(user, "id", None),
            broker_id=getattr(getattr(user, "broker", None), "id", None),
            client_id=client_id,
            loan_id=loan_id,
            metadata_json=metadata,
        )
    )


async def google_autocomplete(db: AsyncSession, input_text: str, session_token: str | None) -> list[dict[str, Any]]:
    settings = await runtime_settings(db)
    api_key = settings.google_server_api_key
    if not api_key:
        return []
    body: dict[str, Any] = {
        "input": input_text,
        "includedRegionCodes": ["us"],
        "includeQueryPredictions": False,
    }
    if session_token:
        body["sessionToken"] = session_token
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.post(
                f"{GOOGLE_PLACES_BASE}/places:autocomplete",
                headers={"X-Goog-Api-Key": api_key},
                json=body,
            )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google address autocomplete failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for raw in resp.json().get("suggestions", []) or []:
        place = raw.get("placePrediction") or {}
        text = place.get("text") or {}
        main = text.get("text") or ""
        place_id = str(place.get("placeId") or place.get("place") or "")
        if place_id.startswith("places/"):
            place_id = place_id.split("/", 1)[1]
        if main:
            out.append(
                {
                    "place_id": place_id,
                    "text": main,
                    "secondary_text": None,
                    "provider": "google",
                }
            )
    return out


def _address_from_geoapify_properties(properties: dict[str, Any]) -> AddressParts:
    street = (properties.get("address_line1") or "").strip()
    if not street:
        street = " ".join(
            str(value).strip()
            for value in (properties.get("housenumber"), properties.get("street"))
            if value
        )
    city = next(
        (
            str(properties.get(key)).strip()
            for key in ("city", "town", "village", "municipality", "county")
            if properties.get(key)
        ),
        "",
    )
    state = str(properties.get("state_code") or properties.get("state") or "").strip()
    if state.lower().startswith("us-"):
        state = state[3:]
    lat = properties.get("lat")
    lon = properties.get("lon")
    return AddressParts(
        street=street or None,
        city=city or None,
        state=state.upper() if len(state) == 2 else state or None,
        zip=str(properties.get("postcode") or "").strip() or None,
        full=str(properties.get("formatted") or "").strip() or None,
        latitude=float(lat) if isinstance(lat, (int, float)) else None,
        longitude=float(lon) if isinstance(lon, (int, float)) else None,
    )


async def geoapify_autocomplete(
    db: AsyncSession,
    input_text: str,
    session_token: str | None,  # kept for the stable provider-neutral API contract
) -> list[dict[str, Any]]:
    del session_token
    settings = await runtime_settings(db)
    if not settings.geoapify_api_key:
        return []
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(
                GEOAPIFY_AUTOCOMPLETE,
                params={
                    "text": input_text,
                    "format": "json",
                    "filter": "countrycode:us",
                    "lang": "en",
                    "limit": 8,
                    "apiKey": settings.geoapify_api_key,
                },
            )
        resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Geoapify address autocomplete failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for properties in resp.json().get("results", []) or []:
        place_id = str(properties.get("place_id") or "").strip()
        text = str(properties.get("formatted") or properties.get("address_line1") or "").strip()
        if not place_id or not text:
            continue
        out.append(
            {
                "place_id": f"geoapify:{place_id}",
                "text": text,
                "secondary_text": properties.get("address_line2"),
                "provider": "geoapify",
            }
        )
    return out


def _address_from_google_components(components: list[dict[str, Any]], fallback: str = "") -> AddressParts:
    street_number = route = city = state = zip_code = ""
    for c in components or []:
        types = c.get("types") or []
        name = c.get("longText") or c.get("long_name") or ""
        short = c.get("shortText") or c.get("short_name") or name
        if "street_number" in types:
            street_number = name
        elif "route" in types:
            route = name
        elif "locality" in types or "postal_town" in types:
            city = name
        elif "administrative_area_level_1" in types:
            state = short
        elif "postal_code" in types:
            zip_code = name
    street = " ".join(x for x in [street_number, route] if x).strip() or None
    return AddressParts(street=street, city=city or None, state=state or None, zip=zip_code or None, full=fallback or None)


async def google_resolve(
    db: AsyncSession,
    *,
    place_id: str | None,
    address: str | None,
    session_token: str | None,
) -> tuple[AddressParts, dict[str, Any] | None]:
    settings = await runtime_settings(db)
    api_key = settings.google_server_api_key
    if not api_key:
        return AddressParts(full=address), None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if place_id:
                normalized_place_id = place_id.split("/", 1)[1] if place_id.startswith("places/") else place_id
                resp = await client.get(
                    f"{GOOGLE_PLACES_BASE}/places/{normalized_place_id}",
                    params={"sessionToken": session_token} if session_token else None,
                    headers={
                        "X-Goog-Api-Key": api_key,
                        "X-Goog-FieldMask": "id,formattedAddress,location,addressComponents,displayName",
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                formatted = data.get("formattedAddress") or address or ""
                parts = _address_from_google_components(data.get("addressComponents") or [], formatted)
                loc = data.get("location") or {}
                parts.latitude = loc.get("latitude")
                parts.longitude = loc.get("longitude")
                return parts, data
            if address:
                resp = await client.get(
                    GOOGLE_GEOCODE_BASE,
                    params={"address": address, "key": api_key, "region": "us", "components": "country:US"},
                )
                resp.raise_for_status()
                data = resp.json()
                first = (data.get("results") or [None])[0]
                if not first:
                    return AddressParts(full=address), data
                formatted = first.get("formatted_address") or address
                parts = _address_from_google_components(first.get("address_components") or [], formatted)
                loc = (first.get("geometry") or {}).get("location") or {}
                parts.latitude = loc.get("lat")
                parts.longitude = loc.get("lng")
                return parts, first
    except Exception as exc:  # noqa: BLE001
        logger.warning("Google address resolve failed: %s", exc)
    return AddressParts(full=address), None


async def geoapify_resolve(
    db: AsyncSession,
    *,
    place_id: str | None,
    address: str | None,
) -> tuple[AddressParts, dict[str, Any] | None]:
    settings = await runtime_settings(db)
    if not settings.geoapify_api_key:
        return AddressParts(full=address), None
    normalized_place_id = (place_id or "").removeprefix("geoapify:") or None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            if normalized_place_id:
                resp = await client.get(
                    GEOAPIFY_PLACE_DETAILS,
                    params={
                        "id": normalized_place_id,
                        "features": "details",
                        "lang": "en",
                        "apiKey": settings.geoapify_api_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                feature = next(iter(data.get("features") or []), None)
                if feature:
                    properties = feature.get("properties") or {}
                    parts = _address_from_geoapify_properties(properties)
                    coordinates = (feature.get("geometry") or {}).get("coordinates") or []
                    if parts.longitude is None and len(coordinates) >= 2:
                        parts.longitude = float(coordinates[0])
                        parts.latitude = float(coordinates[1])
                    if not parts.full:
                        parts.full = address
                    return parts, feature
            if address:
                resp = await client.get(
                    GEOAPIFY_GEOCODE,
                    params={
                        "text": address,
                        "format": "json",
                        "filter": "countrycode:us",
                        "lang": "en",
                        "limit": 1,
                        "apiKey": settings.geoapify_api_key,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                properties = next(iter(data.get("results") or []), None)
                if properties:
                    return _address_from_geoapify_properties(properties), properties
                return AddressParts(full=address), data
    except Exception as exc:  # noqa: BLE001
        logger.warning("Geoapify address resolve failed: %s", exc)
    return AddressParts(full=address), None


async def address_autocomplete(
    db: AsyncSession,
    input_text: str,
    session_token: str | None,
) -> tuple[str, list[dict[str, Any]]]:
    settings = await runtime_settings(db)
    if settings.address_provider == "geoapify":
        return "geoapify", await geoapify_autocomplete(db, input_text, session_token)
    return "google", await google_autocomplete(db, input_text, session_token)


async def address_resolve(
    db: AsyncSession,
    *,
    place_id: str | None,
    address: str | None,
    session_token: str | None,
) -> tuple[str, AddressParts, dict[str, Any] | None]:
    settings = await runtime_settings(db)
    provider = "geoapify" if (place_id or "").startswith("geoapify:") else settings.address_provider
    if provider == "geoapify":
        parts, provider_place = await geoapify_resolve(db, place_id=place_id, address=address)
        return provider, parts, provider_place
    parts, provider_place = await google_resolve(
        db,
        place_id=place_id,
        address=address,
        session_token=session_token,
    )
    return "google", parts, provider_place


async def address_static_map(
    db: AsyncSession,
    *,
    latitude: float,
    longitude: float,
    width: int,
    height: int,
    zoom: int,
) -> tuple[bytes, str]:
    settings = await runtime_settings(db)
    async with httpx.AsyncClient(timeout=12.0) as client:
        if settings.address_provider == "geoapify":
            if not settings.geoapify_api_key:
                raise RuntimeError("Geoapify is not configured")
            marker = f"lonlat:{longitude},{latitude};color:%231d4ed8;size:medium"
            response = await client.get(
                GEOAPIFY_STATIC_MAP,
                params={
                    "style": "osm-bright",
                    "width": width,
                    "height": height,
                    "center": f"lonlat:{longitude},{latitude}",
                    "zoom": zoom,
                    "marker": marker,
                    "apiKey": settings.geoapify_api_key,
                },
            )
        else:
            if not settings.google_server_api_key:
                raise RuntimeError("Google is not configured")
            response = await client.get(
                GOOGLE_STATIC_MAP,
                params={
                    "center": f"{latitude},{longitude}",
                    "zoom": zoom,
                    "size": f"{width}x{height}",
                    "scale": 1,
                    "markers": f"color:blue|{latitude},{longitude}",
                    "key": settings.google_server_api_key,
                },
            )
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "image/png")


async def _rentcast_get(
    client: httpx.AsyncClient,
    api_key: str,
    path: str,
    params: dict[str, Any],
) -> tuple[dict[str, Any] | list[Any] | None, int | None, str]:
    try:
        resp = await client.get(
            f"{RENTCAST_BASE}{path}",
            params={k: v for k, v in params.items() if v is not None and v != ""},
            headers={"X-Api-Key": api_key},
        )
        if resp.status_code == 404:
            return None, resp.status_code, "not_found"
        resp.raise_for_status()
        return resp.json(), resp.status_code, "ok"
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}, None, "failed"


async def _fema_lookup(lat: float | None, lng: float | None) -> tuple[dict[str, Any] | None, str]:
    if lat is None or lng is None:
        return None, "skipped"
    params = {
        "f": "json",
        "geometry": f"{lng},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": "FLD_ZONE,ZONE_SUBTY,SFHA_TF,STATIC_BFE,DEPTH,VELOCITY",
        "returnGeometry": "false",
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(FEMA_NFHL_LAYER, params=params)
        resp.raise_for_status()
        data = resp.json()
        features = data.get("features") or []
        attrs = (features[0] or {}).get("attributes") if features else None
        return {"features": features[:3], "primary": attrs}, "ok" if attrs else "not_found"
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}, "failed"


async def lookup_property_intelligence(
    db: AsyncSession,
    *,
    payload: PropertyIntelligenceLookupRequest,
    user,
) -> PropertyIntelligenceSnapshot:
    settings = await runtime_settings(db)
    normalized = normalize_address(payload.address)
    ahash = address_hash(normalized)
    ttl = timedelta(hours=settings.property_intelligence_cache_ttl_hours)
    if not payload.force_refresh:
        cached = (
            await db.execute(
                select(PropertyIntelligenceSnapshot)
                .where(PropertyIntelligenceSnapshot.address_hash == ahash)
                .order_by(PropertyIntelligenceSnapshot.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if cached and cached.created_at and datetime.now(timezone.utc) - cached.created_at < ttl:
            return cached

    full_address = payload.address.full or ", ".join(
        x for x in [payload.address.street, payload.address.city, payload.address.state, payload.address.zip] if x
    )
    rentcast_property = rentcast_value = rentcast_rent = rentcast_market = None
    source_status: dict[str, Any] = {}
    async with httpx.AsyncClient(timeout=12.0) as client:
        if settings.rentcast_api_key:
            common = {
                "address": full_address,
                "propertyType": payload.property_type,
                "bedrooms": payload.bedrooms,
                "bathrooms": payload.bathrooms,
                "squareFootage": payload.square_footage,
            }
            rentcast_property, code, status = await _rentcast_get(client, settings.rentcast_api_key, "/properties", {"address": full_address})
            source_status["rentcast_property"] = status
            await log_provider_usage(db, provider="rentcast", feature="property_intelligence", request_type="property", user=user, client_id=payload.client_id, loan_id=payload.loan_id, address_hash_value=ahash, status=status, http_status=code)
            rentcast_value, code, status = await _rentcast_get(client, settings.rentcast_api_key, "/avm/value", {**common, "compCount": 8})
            source_status["rentcast_value"] = status
            await log_provider_usage(db, provider="rentcast", feature="property_intelligence", request_type="value", user=user, client_id=payload.client_id, loan_id=payload.loan_id, address_hash_value=ahash, status=status, http_status=code)
            rentcast_rent, code, status = await _rentcast_get(client, settings.rentcast_api_key, "/avm/rent/long-term", {**common, "compCount": 8})
            source_status["rentcast_rent"] = status
            await log_provider_usage(db, provider="rentcast", feature="property_intelligence", request_type="rent", user=user, client_id=payload.client_id, loan_id=payload.loan_id, address_hash_value=ahash, status=status, http_status=code)
            if payload.address.zip:
                rentcast_market, code, status = await _rentcast_get(client, settings.rentcast_api_key, "/markets", {"zipCode": payload.address.zip, "propertyType": payload.property_type})
                source_status["rentcast_market"] = status
                await log_provider_usage(db, provider="rentcast", feature="property_intelligence", request_type="market", user=user, client_id=payload.client_id, loan_id=payload.loan_id, address_hash_value=ahash, status=status, http_status=code)
        else:
            source_status["rentcast"] = "not_configured"

    fema_flood, fema_status = await _fema_lookup(payload.address.latitude, payload.address.longitude)
    source_status["fema_flood"] = fema_status
    await log_provider_usage(db, provider="fema", feature="property_intelligence", request_type="flood", user=user, client_id=payload.client_id, loan_id=payload.loan_id, address_hash_value=ahash, status=fema_status)

    row = PropertyIntelligenceSnapshot(
        created_by_id=user.id,
        client_id=payload.client_id,
        loan_id=payload.loan_id,
        deal_id=payload.deal_id,
        normalized_address=normalized,
        address_hash=ahash,
        source_status=source_status,
        address=payload.address.model_dump(),
        google_place=None,
        rentcast_property=rentcast_property if isinstance(rentcast_property, dict) else {"results": rentcast_property} if rentcast_property is not None else None,
        rentcast_value=rentcast_value if isinstance(rentcast_value, dict) else None,
        rentcast_rent=rentcast_rent if isinstance(rentcast_rent, dict) else None,
        rentcast_market=rentcast_market if isinstance(rentcast_market, dict) else {"results": rentcast_market} if rentcast_market is not None else None,
        fema_flood=fema_flood,
    )
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return row
