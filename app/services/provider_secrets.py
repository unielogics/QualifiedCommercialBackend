from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.provider_secret import ProviderSecret


SECRET_KEYS = {
    "rentcast_api_key",
    "google_server_api_key",
    "google_maps_browser_key",
    "google_maps_ios_key",
    "google_maps_android_key",
    "google_maps_mobile_key",
}


@dataclass(frozen=True)
class ProviderRuntimeSettings:
    rentcast_api_key: str | None
    google_server_api_key: str | None
    google_maps_browser_key: str | None
    google_maps_ios_key: str | None
    google_maps_android_key: str | None
    google_maps_mobile_key: str | None
    property_analysis_ai_enabled: bool
    property_intelligence_cache_ttl_hours: int


def _fernet() -> Fernet:
    settings = get_settings()
    raw = (
        settings.provider_secrets_encryption_key
        or settings.clerk_secret_key
        or settings.anthropic_api_key
        or "qualified-commercial-local-provider-secret-key"
    )
    if raw.startswith("gAAAA") or len(raw) == 44:
        try:
            return Fernet(raw.encode())
        except Exception:
            pass
    key = base64.urlsafe_b64encode(hashlib.sha256(raw.encode()).digest())
    return Fernet(key)


def _encrypt_fernet(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def _decrypt_fernet(value: str) -> str:
    return _fernet().decrypt(value.encode()).decode()


def _encrypt_kms(value: str) -> str:
    settings = get_settings()
    import boto3

    kms = boto3.client("kms", region_name=settings.aws_region)
    resp = kms.encrypt(KeyId=settings.provider_secrets_kms_key_id, Plaintext=value.encode())
    return base64.b64encode(resp["CiphertextBlob"]).decode()


def _decrypt_kms(value: str) -> str:
    settings = get_settings()
    import boto3

    kms = boto3.client("kms", region_name=settings.aws_region)
    resp = kms.decrypt(CiphertextBlob=base64.b64decode(value.encode()))
    return resp["Plaintext"].decode()


async def set_secret(
    db: AsyncSession,
    *,
    key: str,
    value: str,
    updated_by_id=None,
) -> ProviderSecret:
    settings = get_settings()
    if settings.provider_secrets_kms_key_id:
        encrypted = _encrypt_kms(value)
        provider = "aws_kms"
        kms_key_id = settings.provider_secrets_kms_key_id
    else:
        encrypted = _encrypt_fernet(value)
        provider = "fernet"
        kms_key_id = None
    row = (await db.execute(select(ProviderSecret).where(ProviderSecret.key == key))).scalar_one_or_none()
    if row is None:
        row = ProviderSecret(key=key, encrypted_value=encrypted, encryption_provider=provider, kms_key_id=kms_key_id)
        db.add(row)
    else:
        row.encrypted_value = encrypted
        row.encryption_provider = provider
        row.kms_key_id = kms_key_id
    row.updated_by_id = updated_by_id
    await db.flush()
    return row


async def get_secret(db: AsyncSession, key: str) -> str | None:
    row = (await db.execute(select(ProviderSecret).where(ProviderSecret.key == key))).scalar_one_or_none()
    if row is None:
        return None
    try:
        if row.encryption_provider == "aws_kms":
            return _decrypt_kms(row.encrypted_value)
        return _decrypt_fernet(row.encrypted_value)
    except Exception:
        return None


async def is_configured(db: AsyncSession, key: str) -> bool:
    row = (await db.execute(select(ProviderSecret.key).where(ProviderSecret.key == key))).scalar_one_or_none()
    return row is not None


async def runtime_settings(db: AsyncSession) -> ProviderRuntimeSettings:
    from app.models.app_settings import AppSettings
    from app.schemas.settings import AppSettingsData

    settings = get_settings()
    app_row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    data = AppSettingsData.model_validate(app_row.data if app_row else {})
    pi = data.property_intelligence
    return ProviderRuntimeSettings(
        rentcast_api_key=await get_secret(db, "rentcast_api_key") or settings.rentcast_api_key or None,
        google_server_api_key=await get_secret(db, "google_server_api_key") or None,
        google_maps_browser_key=await get_secret(db, "google_maps_browser_key") or None,
        google_maps_ios_key=await get_secret(db, "google_maps_ios_key") or None,
        google_maps_android_key=await get_secret(db, "google_maps_android_key") or None,
        google_maps_mobile_key=await get_secret(db, "google_maps_mobile_key") or None,
        property_analysis_ai_enabled=pi.ai_report_enabled,
        property_intelligence_cache_ttl_hours=pi.cache_ttl_hours,
    )


async def provider_settings_status(db: AsyncSession) -> dict[str, Any]:
    runtime = await runtime_settings(db)
    return {
        "rentcast_configured": bool(runtime.rentcast_api_key),
        "google_server_configured": bool(runtime.google_server_api_key),
        "google_maps_browser_key_configured": bool(runtime.google_maps_browser_key),
        "google_maps_ios_key_configured": bool(runtime.google_maps_ios_key),
        "google_maps_android_key_configured": bool(runtime.google_maps_android_key),
        "google_maps_mobile_key_configured": bool(runtime.google_maps_mobile_key),
        "google_maps_browser_key": runtime.google_maps_browser_key,
        "google_maps_ios_key": runtime.google_maps_ios_key,
        "google_maps_android_key": runtime.google_maps_android_key,
        "google_maps_mobile_key": runtime.google_maps_mobile_key,
        "property_analysis_ai_enabled": runtime.property_analysis_ai_enabled,
        "property_intelligence_cache_ttl_hours": runtime.property_intelligence_cache_ttl_hours,
    }
