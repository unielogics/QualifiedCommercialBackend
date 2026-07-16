"""Per-user Google Drive access (drive.file scope — picker/app-created files only).

Used to (a) attach a user's Drive files to outbound emails and (b) share a Drive
file into the AI. All calls resolve the user's OAuth credentials via
credentials_for_user(DRIVE_SCOPES); the drive.file scope means we only ever see
files the user explicitly picked/opened with this app — never their whole Drive.
"""

from __future__ import annotations

import io
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.google import google_oauth_client
from app.services.google.google_oauth_client import DRIVE_SCOPES

log = logging.getLogger(__name__)

# Google Docs editor MIME types must be exported (they have no direct bytes).
_EXPORT_MAP = {
    "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
    "application/vnd.google-apps.spreadsheet": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".xlsx",
    ),
    "application/vnd.google-apps.presentation": ("application/pdf", ".pdf"),
}


def _service(creds):
    from googleapiclient.discovery import build

    return build("drive", "v3", credentials=creds, cache_discovery=False)


async def list_files(db: AsyncSession, user_id: uuid.UUID, *, query: str | None = None, limit: int = 50) -> list[dict]:
    """List files this app can see for the user (drive.file scope → app-picked
    files). Returns [{id, name, mimeType, size, modifiedTime}]. Best-effort: []
    when not connected."""
    try:
        creds = await google_oauth_client.credentials_for_user(db, user_id, DRIVE_SCOPES)
    except (google_oauth_client.GoogleNotConnected, google_oauth_client.GoogleScopeMissing, google_oauth_client.GoogleTokenRevoked):
        return []
    except Exception:  # noqa: BLE001
        log.exception("drive list: credential resolution failed user=%s", user_id)
        return []

    q_parts = ["trashed = false"]
    if query:
        # Escape backslashes BEFORE single quotes so a trailing/odd backslash can't
        # produce a malformed (unterminated) Drive query literal.
        safe = query.replace("\\", "\\\\").replace("'", "\\'")
        q_parts.append(f"name contains '{safe}'")

    def _do() -> list[dict]:
        svc = _service(creds)
        resp = svc.files().list(
            q=" and ".join(q_parts),
            pageSize=min(limit, 100),
            orderBy="modifiedTime desc",
            fields="files(id,name,mimeType,size,modifiedTime)",
        ).execute()
        return resp.get("files", [])

    try:
        import asyncio

        return await asyncio.to_thread(_do)
    except Exception as exc:  # noqa: BLE001
        log.warning("drive list failed user=%s: %s", user_id, exc)
        return []


async def download_file_bytes(
    db: AsyncSession, user_id: uuid.UUID, file_id: str, *, max_bytes: int | None = None
) -> tuple[str, bytes, str] | None:
    """Fetch a Drive file as (filename, bytes, content_type). Google Docs/Sheets/
    Slides are exported (PDF/XLSX). Returns None on any failure — including when
    the file exceeds ``max_bytes`` (checked BOTH against the declared metadata
    size before downloading and against the running buffer during the streaming
    loop, so an oversized file is never fully materialized in memory). Raises the
    typed Google errors up so the caller can distinguish 'not connected'."""
    creds = await google_oauth_client.credentials_for_user(db, user_id, DRIVE_SCOPES)

    def _do() -> tuple[str, bytes, str] | None:
        from googleapiclient.http import MediaIoBaseDownload

        svc = _service(creds)
        meta = svc.files().get(fileId=file_id, fields="id,name,mimeType,size").execute()
        name = meta.get("name") or "drive-file"
        mime = meta.get("mimeType") or "application/octet-stream"

        # Pre-download guard: reject on the declared size for binary files (Google
        # editor exports report no size, so they're only bounded by the loop below).
        if max_bytes is not None:
            try:
                declared = int(meta.get("size") or 0)
            except (TypeError, ValueError):
                declared = 0
            if declared > max_bytes:
                log.info("drive download: file %s too large (declared %d > %d)", file_id, declared, max_bytes)
                return None

        buf = io.BytesIO()
        if mime in _EXPORT_MAP:
            export_mime, ext = _EXPORT_MAP[mime]
            request = svc.files().export_media(fileId=file_id, mimeType=export_mime)
            out_mime, out_name = export_mime, (name if name.endswith(ext) else name + ext)
        else:
            request = svc.files().get_media(fileId=file_id)
            out_mime, out_name = mime, name
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
            # Mid-stream guard: abort as soon as the buffer crosses the cap so a
            # huge file (or an export with no declared size) can't OOM the worker.
            if max_bytes is not None and buf.tell() > max_bytes:
                log.info("drive download: file %s exceeded %d mid-stream — aborting", file_id, max_bytes)
                return None
        return out_name, buf.getvalue(), out_mime

    try:
        import asyncio

        return await asyncio.to_thread(_do)
    except Exception as exc:  # noqa: BLE001
        log.warning("drive download failed user=%s file=%s: %s", user_id, file_id, exc)
        return None
