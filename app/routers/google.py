"""Per-user Google connection endpoints (OAuth connect + status + disconnect).

Every user type can connect their own Google account here. Phase 1 wires Gmail
send-as-user; Calendar (Phase 2) and Drive (Phase 3) reuse the same grant via
incremental auth (services= query param).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db
from app.deps import CurrentUser
from app.services.google import google_oauth_client as goog

log = logging.getLogger(__name__)

router = APIRouter(prefix="/google", tags=["google"])

_VALID_SERVICES = {"gmail", "calendar", "drive"}


class AuthUrlResponse(BaseModel):
    auth_url: str


class ConnectionStatus(BaseModel):
    connected: bool
    oauth_configured: bool = False
    oauth_configuration_message: str | None = None
    google_email: str | None = None
    gmail_connected: bool = False
    calendar_connected: bool = False
    drive_connected: bool = False
    scopes: list[str] = []
    status: str | None = None
    last_error: str | None = None


@router.get("/oauth/start", response_model=AuthUrlResponse)
async def google_oauth_start(
    user: CurrentUser,
    services: str = Query("gmail", description="comma-separated: gmail,calendar,drive"),
) -> AuthUrlResponse:
    if not goog._oauth_configured():
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Google OAuth is not configured on the server yet.")
    requested = [s.strip() for s in services.split(",") if s.strip() in _VALID_SERVICES]
    if not requested:
        requested = ["gmail"]
    return AuthUrlResponse(auth_url=goog.build_auth_url(user.id, requested))


@router.get("/oauth/callback")
async def google_oauth_callback(
    state: str = Query(...),
    code: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Google redirects here after consent. Identity comes from the signed state
    (this endpoint has NO session auth — Google calls it directly)."""
    settings = get_settings()
    base = settings.frontend_app_url.rstrip("/")

    def _redirect(ok: bool, reason: str | None = None) -> RedirectResponse:
        suffix = "connected=1" if ok else f"error={reason or 'failed'}"
        return RedirectResponse(url=f"{base}/settings?section=connections&{suffix}", status_code=302)

    if error:
        return _redirect(False, error)
    if not code:
        return _redirect(False, "missing_code")
    try:
        payload = goog.verify_state(state)
    except ValueError:
        return _redirect(False, "bad_state")

    import uuid as _uuid

    try:
        user_id = _uuid.UUID(payload["uid"])
        tokens = await goog.exchange_code(code)
        await goog.save_grant(db, user_id, tokens)
        await db.commit()
    except Exception:  # noqa: BLE001
        log.exception("google oauth callback failed")
        await db.rollback()
        return _redirect(False, "exchange_failed")
    return _redirect(True)


@router.get("/connection", response_model=ConnectionStatus)
async def google_connection_status(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> ConnectionStatus:
    oauth_configured = goog._oauth_configured()
    configuration_message = None if oauth_configured else (
        "Google OAuth requires a web client ID, client secret, and production callback URI in the backend secret."
    )
    row = await goog.get_account(db, user.id)
    if row is None:
        return ConnectionStatus(
            connected=False,
            oauth_configured=oauth_configured,
            oauth_configuration_message=configuration_message,
        )
    return ConnectionStatus(
        connected=row.status == "active" and bool(row.encrypted_refresh_token),
        oauth_configured=oauth_configured,
        oauth_configuration_message=configuration_message,
        google_email=row.google_email,
        gmail_connected=row.gmail_connected,
        calendar_connected=row.calendar_connected,
        drive_connected=row.drive_connected,
        scopes=list(row.scopes or []),
        status=row.status,
        last_error=row.last_error,
    )


@router.delete("/connection")
async def google_disconnect(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> dict:
    removed = await goog.disconnect(db, user.id)
    await db.commit()
    return {"disconnected": removed}


class AutomationSettings(BaseModel):
    re_agent_auto_emails: bool = False
    merged_updates_auto: bool = False
    status_change_emails: bool = False


@router.get("/connection/settings", response_model=AutomationSettings)
async def get_automation_settings(user: CurrentUser, db: AsyncSession = Depends(get_db)) -> AutomationSettings:
    row = await goog.get_account(db, user.id)
    data = (row.automation_settings if row and isinstance(row.automation_settings, dict) else {}) or {}
    return AutomationSettings(**{k: bool(data.get(k, False)) for k in AutomationSettings.model_fields})


@router.put("/connection/settings", response_model=AutomationSettings)
async def put_automation_settings(
    payload: AutomationSettings, user: CurrentUser, db: AsyncSession = Depends(get_db)
) -> AutomationSettings:
    row = await goog.get_account(db, user.id)
    if row is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Connect Google before configuring automations.")
    row.automation_settings = payload.model_dump()
    await db.commit()
    return payload


class DriveFile(BaseModel):
    id: str
    name: str
    mime_type: str | None = None
    size: str | None = None
    modified_time: str | None = None


class DriveListResponse(BaseModel):
    files: list[DriveFile] = []


@router.get("/drive/files", response_model=DriveListResponse)
async def google_drive_files(
    user: CurrentUser,
    q: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> DriveListResponse:
    """List the user's Drive files this app can see (drive.file scope — only files
    the user picked/created with the app), for the email attach + share-to-AI picker."""
    from app.services.google.drive_client import list_files

    files = await list_files(db, user.id, query=q)
    return DriveListResponse(
        files=[
            DriveFile(
                id=f.get("id"),
                name=f.get("name") or "file",
                mime_type=f.get("mimeType"),
                size=f.get("size"),
                modified_time=f.get("modifiedTime"),
            )
            for f in files
            if f.get("id")
        ]
    )
