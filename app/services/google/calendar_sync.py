"""Two-way Google Calendar sync.

Internal `calendar_events` is the source of truth. This module mirrors events to
each connected user's primary Google Calendar (push) and folds their Google
changes back in (pull), with a loop guard so our own echoes don't re-create rows.

Design:
- **Push** (internal → Google): on create/update/delete of an event whose
  `owner_user_id` has a connected calendar. Stores `google_event_id` + `google_etag`
  and stamps `sync_origin="internal"`.
- **Pull** (Google → internal): incremental `events.list(syncToken=...)`. A change
  we already have (by `google_event_id`) updates the row; anything else creates a
  `CalendarEvent(source=MANUAL, external_ref_kind="google_pull")`. `410 Gone`
  (expired token) triggers a full resync.
- **Loop guard**: when pulling, if the incoming event's `etag` equals the
  `google_etag` we last stored, it's our own write echoed back — skip it.

All Google API work runs in a thread (google-api-python-client is sync). Push is
best-effort: callers wrap it so a Google outage never breaks the local write.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.enums import CalendarEventKind, CalendarEventSource, CalendarEventStatus
from app.models.event import CalendarEvent
from app.models.google_account import GoogleAccount
from app.services.google import google_oauth_client
from app.services.google.google_oauth_client import CALENDAR_SCOPES

log = logging.getLogger(__name__)

_GOOGLE_PULL_KIND = "google_pull"
_DEFAULT_DURATION_MIN = 30


@dataclass(frozen=True)
class CalendarBusySnapshot:
    """Live busy periods from the connected Google primary calendar.

    `status` is deliberately small and safe to expose to booking clients. It
    never includes OAuth/provider error text.
    """

    status: str
    intervals: list[tuple[datetime, datetime]]


async def busy_periods(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    time_min: datetime,
    time_max: datetime,
) -> CalendarBusySnapshot:
    """Read the owner's current Google FreeBusy state.

    Availability calls use this in addition to the local event ledger. This is
    intentionally on-demand: the five-minute incremental pull is useful for the
    in-app calendar, but is not sufficiently fresh to prevent a booking race
    immediately after a personal Google event is created.
    """
    try:
        creds = await google_oauth_client.credentials_for_user(
            db, user_id, CALENDAR_SCOPES
        )
    except (
        google_oauth_client.GoogleNotConnected,
        google_oauth_client.GoogleScopeMissing,
        google_oauth_client.GoogleTokenRevoked,
    ):
        return CalendarBusySnapshot(status="disconnected", intervals=[])
    except Exception:  # noqa: BLE001
        log.exception("calendar freebusy: credential resolution failed user=%s", user_id)
        return CalendarBusySnapshot(status="unavailable", intervals=[])

    start = time_min if time_min.tzinfo else time_min.replace(tzinfo=UTC)
    end = time_max if time_max.tzinfo else time_max.replace(tzinfo=UTC)

    def _query() -> dict:
        return (
            _service(creds)
            .freebusy()
            .query(
                body={
                    "timeMin": start.astimezone(UTC).isoformat(),
                    "timeMax": end.astimezone(UTC).isoformat(),
                    "items": [{"id": "primary"}],
                }
            )
            .execute()
        )

    try:
        import asyncio

        response = await asyncio.to_thread(_query)
    except Exception as exc:  # noqa: BLE001
        log.warning("calendar freebusy failed user=%s: %s", user_id, exc)
        return CalendarBusySnapshot(status="unavailable", intervals=[])

    intervals: list[tuple[datetime, datetime]] = []
    for item in ((response.get("calendars") or {}).get("primary") or {}).get("busy") or []:
        busy_start = _parse_google_dt({"dateTime": item.get("start")})
        busy_end = _parse_google_dt({"dateTime": item.get("end")})
        if busy_start is None or busy_end is None or busy_end <= busy_start:
            continue
        intervals.append((busy_start.astimezone(UTC), busy_end.astimezone(UTC)))
    return CalendarBusySnapshot(status="connected", intervals=intervals)


def _event_body(ev: CalendarEvent, *, color_id: str | None = None) -> dict:
    """Map an internal CalendarEvent to a Google Calendar event resource."""
    start = ev.starts_at
    if start.tzinfo is None:
        start = start.replace(tzinfo=UTC)
    end = start + timedelta(minutes=ev.duration_min or _DEFAULT_DURATION_MIN)
    body: dict = {
        "summary": ev.title,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }
    if ev.description:
        body["description"] = ev.description
    if color_id:
        body["colorId"] = color_id
    # Cancelled internal events map to a Google 'cancelled' status on patch.
    if ev.status == CalendarEventStatus.CANCELLED:
        body["status"] = "cancelled"
    return body


def _meet_url(resp: dict) -> str | None:
    """The Meet link out of a Google event resource. hangoutLink is the direct
    field; conferenceData is the fallback for resources that only carry the
    entry points."""
    direct = resp.get("hangoutLink")
    if direct:
        return direct
    for entry in (resp.get("conferenceData") or {}).get("entryPoints") or []:
        if entry.get("entryPointType") == "video" and entry.get("uri"):
            return entry["uri"]
    return None


def _service(creds):
    from googleapiclient.discovery import build

    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def _deterministic_google_id(ev: CalendarEvent) -> str:
    """A stable Google event id derived from our row UUID so a repeated insert of
    the SAME internal event is idempotent (Google returns 409 instead of making a
    second event). uuid.hex is [0-9a-f], all valid base32hex chars Google accepts."""
    return "qc" + ev.id.hex


async def push_event(
    db: AsyncSession,
    ev: CalendarEvent,
    *,
    deleted: bool = False,
    attendees: list[dict] | None = None,
    want_conference: bool = False,
    send_updates: str | None = None,
    color_id: str | None = None,
) -> str | None:
    """Mirror one internal event to the owner's Google primary calendar.

    Best-effort and self-silencing: does nothing when the owner has no connected
    calendar, and never raises to the caller (logs instead) so a Google outage
    can't break the local calendar write. Local edits to a Google-origin event
    ARE propagated back (echo-suppressed on pull by the etag guard).

    attendees / send_updates / want_conference exist for the public booking
    path. Putting the invitee on the event is what makes Google issue its own
    invitation with Accept and Decline, and send_updates="all" is what actually
    posts it; without that pair Google records the guest silently and mails
    nobody. want_conference asks Google to mint a Meet link on insert.

    Returns the Meet URL when one was created, else None. Existing callers pass
    none of the new arguments and behave exactly as before."""
    if ev.owner_user_id is None:
        return None

    try:
        creds = await google_oauth_client.credentials_for_user(db, ev.owner_user_id, CALENDAR_SCOPES)
    except (google_oauth_client.GoogleNotConnected, google_oauth_client.GoogleScopeMissing, google_oauth_client.GoogleTokenRevoked):
        return None
    except Exception:  # noqa: BLE001
        log.exception("calendar push: credential resolution failed owner=%s", ev.owner_user_id)
        return None

    cal_id = ev.google_calendar_id or "primary"
    # Use a deterministic id on the FIRST push so a concurrent/retried insert of
    # the same internal row can't create two Google events. Google-origin rows
    # already carry their real google_event_id.
    gid = ev.google_event_id or _deterministic_google_id(ev)
    body = _event_body(ev, color_id=color_id)
    if attendees:
        body["attendees"] = attendees
    if want_conference and not ev.google_event_id:
        # Only on insert. Asking again on patch replaces the existing link and
        # invalidates the one already sitting in the invitee's invitation.
        body["conferenceData"] = {
            "createRequest": {
                "requestId": _deterministic_google_id(ev),
                "conferenceSolutionKey": {"type": "hangoutsMeet"},
            }
        }

    def _do() -> dict | None:
        svc = _service(creds)
        extra: dict = {}
        if send_updates:
            extra["sendUpdates"] = send_updates
        if deleted:
            if ev.google_event_id:
                try:
                    svc.events().delete(calendarId=cal_id, eventId=ev.google_event_id, **extra).execute()
                except Exception:  # noqa: BLE001
                    pass
            return None
        if ev.google_event_id:
            return svc.events().patch(calendarId=cal_id, eventId=ev.google_event_id, body=body, **extra).execute()
        # First push: insert with our deterministic id. If it already exists
        # (409, e.g. a racing duplicate push), patch it instead of duplicating.
        insert_body = {**body, "id": gid}
        insert_extra = dict(extra)
        if want_conference:
            # Without this version flag Google drops the createRequest silently.
            insert_extra["conferenceDataVersion"] = 1
        try:
            return svc.events().insert(calendarId=cal_id, body=insert_body, **insert_extra).execute()
        except Exception as exc:  # noqa: BLE001
            if "409" in str(exc) or "duplicate" in str(exc).lower():
                return svc.events().patch(calendarId=cal_id, eventId=gid, body=body, **extra).execute()
            raise

    try:
        import asyncio

        resp = await asyncio.to_thread(_do)
    except Exception as exc:  # noqa: BLE001
        log.warning("calendar push failed event=%s owner=%s: %s", ev.id, ev.owner_user_id, exc)
        return None

    if resp is None:
        return None

    ev.google_event_id = resp.get("id") or gid
    ev.google_calendar_id = cal_id
    ev.google_etag = resp.get("etag")
    ev.synced_at = datetime.now(UTC)
    ev.sync_origin = "internal"
    await db.flush()
    return _meet_url(resp)


_ALL_DAY_HOUR_UTC = 14  # ≈ 9am ET — matches calendar_emitter's default hour


def _parse_google_dt(node: dict | None) -> datetime | None:
    if not node:
        return None
    raw = node.get("dateTime") or node.get("date")
    if not raw:
        return None
    try:
        if "T" not in raw:
            # All-day (date-only) event. Anchoring at midnight UTC would render on
            # the PREVIOUS day for any US timezone; anchor at the default morning
            # hour (14:00 UTC) so it stays on the intended date for US users.
            d = datetime.fromisoformat(raw).date()
            return datetime(d.year, d.month, d.day, _ALL_DAY_HOUR_UTC, tzinfo=UTC)
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


async def pull_events(db: AsyncSession, user_id: uuid.UUID, *, max_pages: int = 40) -> int:
    """Fold the user's Google Calendar changes into internal calendar_events.

    Uses an incremental sync token stored on google_accounts. Returns the number
    of events applied. Best-effort: logs and returns 0 on any failure."""
    row = (await db.execute(select(GoogleAccount).where(GoogleAccount.user_id == user_id))).scalar_one_or_none()
    if row is None or not row.calendar_connected or row.status != "active":
        return 0
    try:
        creds = await google_oauth_client.credentials_for_user(db, user_id, CALENDAR_SCOPES)
    except (google_oauth_client.GoogleNotConnected, google_oauth_client.GoogleScopeMissing, google_oauth_client.GoogleTokenRevoked):
        return 0
    except Exception:  # noqa: BLE001
        log.exception("calendar pull: credential resolution failed user=%s", user_id)
        return 0

    sync_token = row.calendar_sync_token

    def _list_page(page_token: str | None, use_sync: bool) -> dict:
        svc = _service(creds)
        params: dict = {"calendarId": "primary", "maxResults": 250, "singleEvents": True, "showDeleted": True}
        if page_token:
            params["pageToken"] = page_token
        elif use_sync and sync_token:
            params["syncToken"] = sync_token
        else:
            # Full sync: bound the window so we don't ingest years of history.
            params["timeMin"] = (datetime.now(UTC) - timedelta(days=30)).isoformat()
        return svc.events().list(**params).execute()

    import asyncio

    applied = 0
    page_token: str | None = None
    use_sync = True
    next_sync_token: str | None = None
    pages = 0
    try:
        while pages < max_pages:
            pages += 1
            try:
                resp = await asyncio.to_thread(_list_page, page_token, use_sync)
            except Exception as exc:  # noqa: BLE001
                # 410 Gone → sync token expired → restart a full sync once.
                if "410" in str(exc) and use_sync:
                    log.info("calendar pull: sync token expired user=%s, full resync", user_id)
                    use_sync = False
                    page_token = None
                    row.calendar_sync_token = None
                    await db.flush()
                    continue
                log.warning("calendar pull: list failed user=%s: %s", user_id, exc)
                return applied
            for item in resp.get("items", []):
                if await _apply_pulled_event(db, user_id, item):
                    applied += 1
            page_token = resp.get("nextPageToken")
            next_sync_token = resp.get("nextSyncToken") or next_sync_token
            if not page_token:
                break
        # Advance the sync token ONLY when we drained the whole changeset (Google
        # emits nextSyncToken only on the final page). If we hit max_pages with a
        # page_token still pending, do NOT persist a token — leave it so the next
        # run continues; log so truncation is observable rather than silent.
        if next_sync_token:
            row.calendar_sync_token = next_sync_token
        elif page_token:
            log.warning(
                "calendar pull: changeset exceeded max_pages=%s user=%s (applied=%s); "
                "sync token not advanced, will continue next run",
                max_pages, user_id, applied,
            )
        await db.flush()
    except Exception:  # noqa: BLE001
        log.exception("calendar pull failed user=%s", user_id)
    return applied


async def _apply_pulled_event(db: AsyncSession, user_id: uuid.UUID, item: dict) -> bool:
    """Apply a single Google event to THIS user's internal state. Returns True if
    changed. The lookup is scoped to (owner_user_id, google_event_id): Google
    gives every attendee the same event id, so two connected users attending one
    meeting must keep independent internal rows, not clobber each other's."""
    gid = item.get("id")
    if not gid:
        return False
    etag = item.get("etag")
    existing = (
        await db.execute(
            select(CalendarEvent).where(
                CalendarEvent.google_event_id == gid,
                CalendarEvent.owner_user_id == user_id,
            )
        )
    ).scalar_one_or_none()

    # Loop guard: this is our own echo (etag matches what we last pushed) → skip.
    if existing is not None and etag and existing.google_etag == etag:
        return False

    if item.get("status") == "cancelled":
        if existing is not None and existing.status != CalendarEventStatus.CANCELLED:
            existing.status = CalendarEventStatus.CANCELLED
            existing.google_etag = etag
            existing.synced_at = datetime.now(UTC)
            existing.sync_origin = "google"
            await db.flush()
            return True
        return False

    start = _parse_google_dt(item.get("start"))
    if start is None:
        return False
    title = item.get("summary") or "(no title)"
    description = item.get("description")

    if existing is not None:
        existing.title = title
        existing.description = description
        existing.starts_at = start
        existing.google_etag = etag
        existing.synced_at = datetime.now(UTC)
        existing.sync_origin = "google"
        await db.flush()
        return True

    ev = CalendarEvent(
        id=uuid.uuid4(),
        kind=CalendarEventKind.MILESTONE,
        title=title,
        description=description,
        starts_at=start,
        status=CalendarEventStatus.PENDING,
        source=CalendarEventSource.MANUAL,
        owner_user_id=user_id,
        # Per-user external_ref_id: the (kind, id) partial-unique index is global,
        # so a shared Google event id must be namespaced by user to avoid a
        # cross-user IntegrityError.
        external_ref_kind=_GOOGLE_PULL_KIND,
        external_ref_id=f"{user_id}:{gid}"[:64],
        google_event_id=gid,
        google_calendar_id="primary",
        google_etag=etag,
        synced_at=datetime.now(UTC),
        sync_origin="google",
    )
    db.add(ev)
    await db.flush()
    return True
