"""Durable, debounced saved-file preparation. Never approves or publishes.

Intent and leases are stored outside all material source facts. Rendering is
performed without a listing lock; the lease and saved inputs are checked again
before any attachment or financial snapshot can replace its predecessor.
"""
from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.application_profile import ApplicationProfile

from .. import enums as E
from ..models import CpListing
from . import listings, ppm, prefill, sync

log = logging.getLogger(__name__)
KEY = "refresh_job"
DEBOUNCE_SECONDS = 45
LEASE_SECONDS = 600
LIVE = {E.ListingStatus.DRAFT, E.ListingStatus.OPEN}
_lock = asyncio.Lock()
_cursor = None
PUBLIC_FIELDS = ("job_id", "status", "source_fingerprint", "requested_at", "started_at", "completed_at",
    "error_code", "message", "figures_updated", "documents_updated", "ppm_updated", "review_required", "publication_required")


def _now():
    return datetime.now(UTC)


def _date(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _job(listing):
    return deepcopy((listing.source_snapshot or {}).get(KEY) or {})


def _put(listing, job):
    listing.source_snapshot = {**(listing.source_snapshot or {}), KEY: deepcopy(job)}


def state(listing):
    job = _job(listing)
    return {"listing_id": str(listing.id), **{key: job.get(key) for key in PUBLIC_FIELDS},
        "status": job.get("status", "idle"),
        **{key: bool(job.get(key)) for key in ("figures_updated", "documents_updated", "ppm_updated", "review_required", "publication_required")}}


async def read_state(db, listing):
    result = state(listing)
    if result["status"] != "succeeded":
        return result
    current = await ppm.read_state(db, listing)
    result["review_required"] = current["status"] != "approved"
    candidate, active = current.get("current_approved") or {}, current.get("active_version") or {}
    pending = sync.publication_changed(listing)
    if current["managed"]:
        result["publication_required"] = bool(listing.published_at and (
            pending or
            active.get("source_fingerprint") != current["source_fingerprint"]
            or active.get("document_id") != candidate.get("document_id")))
    else:
        result["publication_required"] = bool(listing.published_at and pending)
    return result


async def _load(db, listing_id, *, lock=False):
    query = select(CpListing).where(CpListing.id == listing_id).options(selectinload(CpListing.documents))
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one_or_none()


def _enqueue(listing, *, actor_id=None, source_fingerprint=None, immediate=False, reason="saved_file"):
    now = _now()
    old = _job(listing)
    if source_fingerprint and old.get("source_fingerprint") == source_fingerprint and old.get("status") in {"queued", "running", "succeeded"}:
        return state(listing)
    job = {"job_id": str(uuid4()), "status": "queued", "source_fingerprint": source_fingerprint,
        "requested_at": now.isoformat(), "due_at": (now + timedelta(seconds=0 if immediate else DEBOUNCE_SECONDS)).isoformat(),
        "actor_id": str(actor_id) if actor_id else None, "reason": reason,
        "previous_source": old.get("applied_source") or old.get("previous_source"),
        "requests": old.get("requests", {}), "attempts": 0,
        "message": "Preparing the latest saved figures and PDFs. PPM review and publication remain separate."}
    _put(listing, job)
    return state(listing)


async def queue_profile(db, profile_id, *, actor_id=None, reason="saved_file"):
    """Called in a real saved-source transaction; no browser or GET needed."""
    rows = (await db.execute(select(CpListing).where(CpListing.profile_id == profile_id,
        CpListing.status.in_(LIVE)).with_for_update())).scalars().all()
    for listing in rows:
        _enqueue(listing, actor_id=actor_id, reason=reason)
    return len(rows)


async def after_saved_source(db, profile_id, *, actor_id=None, reason="saved_file"):
    """An advisory hook cannot reject the authoritative source save.

    The bounded recovery sweep independently detects changes if a previous
    application process or an older writer missed this hook.
    """
    if not isinstance(db, AsyncSession):
        return
    try:
        async with db.begin_nested():
            await queue_profile(db, profile_id, actor_id=actor_id, reason=reason)
    except Exception as exc:
        log.debug("Marketplace refresh enqueue deferred error=%s", type(exc).__name__)


async def after_package_generated(db, intake_id, *, actor_id=None):
    """Called only after the older explicit package route released its lock."""
    if not isinstance(db, AsyncSession):
        return
    try:
        ids = list((await db.execute(select(ApplicationProfile.id).where(ApplicationProfile.intake_id == intake_id))).scalars())
        for profile_id in ids:
            await after_saved_source(db, profile_id, actor_id=actor_id, reason="saved_package")
        await db.commit()
    except Exception as exc:
        await db.rollback()
        log.debug("Marketplace package enqueue deferred error=%s", type(exc).__name__)


async def _source(db, profile):
    from app.services import marketplace_package_refresh as packages

    await db.refresh(profile)
    package_input = await packages.current_input_fingerprint(db, profile)
    fresh = await prefill.readiness_snapshot(db, profile)
    material = {key: value for key, value in fresh.get("file_fingerprints", {}).items() if key != "package"}
    return sync.saved_source_fingerprint({"file": material, "package_input": package_input}), fresh


async def request(db, listing, *, actor_id=None, idempotency_key=None):
    if not listing.profile_id:
        raise HTTPException(422, {"code": "refresh_source_missing", "message": "This listing has no linked saved file to refresh."})
    if listing.status not in LIVE:
        raise HTTPException(409, {"code": "refresh_closed", "message": "Only draft or open offerings can prepare an update."})
    profile = await db.get(ApplicationProfile, listing.profile_id)
    if profile is None or not profile.intake_id:
        raise HTTPException(422, {"code": "refresh_source_missing", "message": "The linked file needs an underwriting package source."})
    source, _fresh = await _source(db, profile)
    listing = await _load(db, listing.id, lock=True)
    if listing.status not in LIVE:
        raise HTTPException(409, {"code": "refresh_closed", "message": "The offering closed before its refresh was queued."})
    job = _job(listing)
    key = sync._fingerprint({"actor": str(actor_id), "key": idempotency_key}) if idempotency_key else None
    prior = (job.get("requests") or {}).get(key) if key else None
    if prior:
        if prior["source"] != source:
            raise HTTPException(409, {"code": "refresh_key_reused", "message": "This refresh retry belongs to different saved facts. Refresh again with a new request."})
        if job.get("status") in {"failed", "blocked"}:
            job.update(status="queued", due_at=_now().isoformat(), retry_at=None, completed_at=None, error_code=None)
            _put(listing, job)
            await db.flush()
            return state(listing)
        return await read_state(db, listing)
    result = _enqueue(listing, actor_id=actor_id, source_fingerprint=source, immediate=True, reason="manual")
    if key:
        job = _job(listing)
        job["requests"] = {**(job.get("requests") or {}), key: {"source": source, "job_id": job["job_id"]}}
        _put(listing, job)
    await db.flush()
    return await read_state(db, listing) if result["status"] == "succeeded" else result


async def _claim(db, listing_id):
    listing = await _load(db, listing_id, lock=True)
    if listing is None or listing.status not in LIVE:
        return None
    job = _job(listing)
    now = _now()
    eligible = job.get("status") == "queued" and (_date(job.get("due_at")) or now) <= now
    expired = job.get("status") == "running" and (_date(job.get("lease_until")) or now) <= now
    if not eligible and not expired:
        return None
    job.update(status="running", started_at=now.isoformat(), lease_until=(now + timedelta(seconds=LEASE_SECONDS)).isoformat(),
        claim_token=str(uuid4()), attempts=int(job.get("attempts") or 0) + 1, error_code=None, completed_at=None)
    _put(listing, job)
    await db.commit()
    return {**job, "profile_id": listing.profile_id}


async def _finish_failure(session_factory, listing_id, claimed, exc):
    async with session_factory() as db:
        listing = await _load(db, listing_id, lock=True)
        if listing is None or any(_job(listing).get(key) != claimed.get(key) for key in ("job_id", "claim_token")):
            return "superseded"
        job = _job(listing)
        if job.get("status") != "running":
            return job.get("status")
        detail = exc.detail if isinstance(exc, HTTPException) else None
        code = detail.get("code") if isinstance(detail, dict) else None
        blocked = isinstance(exc, HTTPException) and exc.status_code in {404, 409, 422}
        job.update(status="blocked" if blocked else "failed", error_code=code or ("refresh_requires_review" if blocked else "refresh_failed"),
            message="Saved facts changed or need staff review. Existing documents and approvals were retained." if blocked else "The update could not finish. Existing documents were retained; retry the refresh.",
            completed_at=_now().isoformat(), lease_until=None,
            retry_at=(_now() + timedelta(minutes=5)).isoformat())
        _put(listing, job)
        await db.commit()
        return job["status"]


async def run_one(session_factory, listing_id):
    from app.services import marketplace_package_refresh as packages

    from .package_documents import attach_generated_documents

    async with session_factory() as db:
        claimed = await _claim(db, listing_id)
    if claimed is None:
        return "skipped"
    prepared = None
    commit_attempted = False
    try:
        async with session_factory() as db:
            profile = await db.get(ApplicationProfile, claimed["profile_id"])
            if profile is None:
                raise HTTPException(404, {"code": "refresh_source_missing"})
            await packages.lock_source(db, profile)
            source, fresh = await _source(db, profile)
            # Source intent is established before rendering, not by a read time.
            prepared = await packages.prepare(db, profile, actor_id=UUID(claimed["actor_id"]) if claimed.get("actor_id") else None)
            listing = await _load(db, listing_id, lock=True)
            job = _job(listing) if listing else {}
            if listing is None or any(job.get(key) != claimed.get(key) for key in ("job_id", "claim_token")) or job.get("status") != "running":
                raise HTTPException(409, {"code": "refresh_superseded"})
            if listing.status not in LIVE:
                raise HTTPException(409, {"code": "refresh_closed"})
            latest, latest_fresh = await _source(db, profile)
            if latest != source:
                raise HTTPException(409, {"code": "refresh_source_changed"})
            # Use the same existing financing/commitment guard as explicit refresh.
            from ..router_admin import _guard_financing_change

            await _guard_financing_change(db, listing, latest_fresh)
            actor_id = UUID(claimed["actor_id"]) if claimed.get("actor_id") else None
            sync.preserve_active_financials(listing)
            previous = listing.source_snapshot or {}
            figures_changed = any((previous.get("file_fingerprints") or {}).get(key) != value
                for key, value in fresh.get("file_fingerprints", {}).items() if key != "package")
            # Preserve the explicitly published economic view while preparing a
            # candidate. Members see a separate, clearly conditional sidecar.
            published = await packages.publish_prepared(db, prepared, actor_id=actor_id)
            artifacts = published["artifacts"] if isinstance(published, dict) else published
            before_documents = {str(doc.id) for doc in listing.documents}
            now = _now()
            receipts = await attach_generated_documents(db, listing, artifacts, actor_id=actor_id, now=now)
            documents_changed = any(str(receipt["document_id"]) not in before_documents for receipt in receipts)
            if figures_changed:
                sync.apply_canonical_financing(listing, fresh)
                listing.source_snapshot = {**previous, **fresh, "refreshed_at": now.isoformat()}
            elif listing.source_snapshot != previous:
                listing.source_snapshot = previous
            current, _sections, _cockpit = await sync.current_fingerprints(db, profile)
            package = await sync.current_package(db, profile)
            if figures_changed or documents_changed:
                sync.mark_published(listing, current, by=actor_id,
                    package={**(package or {}), "documents": receipts, "at": now.isoformat()})
            refreshed = await ppm.refresh_managed_listing(db, listing.id, actor_id=None)
            ppm_state = await ppm.read_state(db, listing)
            job.update(status="succeeded", source_fingerprint=source, applied_source=source, completed_at=now.isoformat(), lease_until=None,
                figures_updated=figures_changed, documents_updated=documents_changed, ppm_updated=refreshed.get("status") == "refreshed",
                review_required=ppm_state.get("status") != "approved",
                publication_required=bool(listing.published_at and (sync.publication_changed(listing) or refreshed.get("status") == "refreshed")),
                message="Saved figures and PDFs are current. Review the PPM and explicitly publish any offering update.")
            _put(listing, job)
            if figures_changed or documents_changed:
                await listings.log_event(db, listing.id, "listing.refresh_prepared", actor_user_id=actor_id,
                    detail={"job_id": job["job_id"], "figures_updated": figures_changed, "documents_updated": documents_changed})
            commit_attempted = True
            await db.commit()
        return "succeeded"
    except Exception as exc:
        if prepared is not None and not commit_attempted:
            await packages.discard(prepared)
        outcome = await _finish_failure(session_factory, listing_id, claimed, exc)
        if outcome == "succeeded":
            return "succeeded"
        log.warning("Marketplace preparation failed listing=%s error=%s", listing_id, type(exc).__name__)
        return "failed"


async def recover_sources(session_factory, *, limit=3):
    """Bounded cursor sweep catches saves from older/missed hook paths."""
    global _cursor
    async with session_factory() as db:
        query = select(CpListing.id).where(CpListing.profile_id.is_not(None), CpListing.status.in_(LIVE))
        page = query.where(CpListing.id > _cursor) if _cursor else query
        ids = list((await db.execute(page.order_by(CpListing.id).limit(limit))).scalars())
        if not ids and _cursor:
            _cursor = None
            ids = list((await db.execute(query.order_by(CpListing.id).limit(limit))).scalars())
    for listing_id in ids:
        _cursor = listing_id
        async with session_factory() as db:
            try:
                listing = await _load(db, listing_id)
                job = _job(listing)
                if job.get("status") in {"queued", "running"}:
                    continue
                if job.get("retry_at") and _date(job["retry_at"]) > _now():
                    continue
                profile = await db.get(ApplicationProfile, listing.profile_id)
                if not profile or not profile.intake_id:
                    continue
                source, _fresh = await _source(db, profile)
                if source != job.get("applied_source") or job.get("status") == "failed":
                    listing = await _load(db, listing_id, lock=True)
                    # A source save may have queued newer intent while reading.
                    if _job(listing).get("job_id") == job.get("job_id"):
                        _enqueue(listing, source_fingerprint=source, reason="recovery")
                        await db.commit()
            except Exception as exc:
                await db.rollback()
                log.warning("Marketplace source recovery failed listing=%s error=%s", listing_id, type(exc).__name__)


async def drain_pending(*, limit=3, session_factory=None):
    if _lock.locked():
        return {"checked": 0, "succeeded": 0, "failed": 0}
    if session_factory is None:
        from app.db import SessionLocal

        session_factory = SessionLocal
    limit = min(5, max(1, int(limit)))
    async with _lock:
        await recover_sources(session_factory, limit=limit)
        async with session_factory() as db:
            ids = list((await db.execute(select(CpListing.id).where(CpListing.status.in_(LIVE),
                CpListing.source_snapshot[KEY]["status"].as_string().in_(["queued", "running"]))
                .order_by(CpListing.id).limit(50))).scalars())
        counts = {"checked": 0, "succeeded": 0, "failed": 0}
        for listing_id in ids:
            if counts["checked"] >= limit:
                break
            try:
                result = await asyncio.wait_for(run_one(session_factory, listing_id), timeout=LEASE_SECONDS - 30)
            except TimeoutError:
                result = "failed"
            if result != "skipped":
                counts["checked"] += 1
                counts[result] += 1
        return counts
