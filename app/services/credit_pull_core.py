"""Bureau-pull core, extracted out of credit.py's `initiate_pull` so BOTH the
client self-serve endpoint (POST /credit/pull) and the admin-on-a-lead
endpoint (POST /admin/ai-underwriter-leads/{id}/credit-pull) share the exact
same FICO-fallback chain, parsing, and CreditPull persistence — no duplicated
bureau logic, no drift between the two callers.

This module intentionally has NO opinion on consent/authorization gating —
that stays the caller's responsibility (client-role Stripe pre-auth for the
self-serve endpoint; a signed BucketDocumentSignature for the admin/lead
endpoint). run_soft_pull() only does the mechanical bureau-pull + persist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import SOFT_PULL_VALIDITY_DAYS
from app.enums import CreditPullStatus
from app.models.client import Client
from app.models.credit_pull import CreditPull
from app.models.user import User
from app.services import (
    calendar_emitter,
    credit_report_archive,
    isoftpull_client,
    isoftpull_report_parser,
)

log = logging.getLogger(__name__)

INTELLIGENCE_PASSED_PROXY_FICO = 720


@dataclass
class SoftPullApplicant:
    legal_first_name: str
    legal_last_name: str
    dob: date
    street: str
    city: str
    state: str
    zip: str
    ssn: str | None = None


class SoftPullDenied(Exception):
    """Raised with a structured (code, message) the caller maps to its own
    HTTP response — keeps this module free of FastAPI/HTTPException coupling
    so both an authed client endpoint and an admin endpoint can wrap it
    however fits their response contract."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class SoftPullValidationError(Exception):
    """Bad request shape rejected by the bureau before any pull attempt."""


class SoftPullRateLimited(Exception):
    """Bureau rate-limited this pull — caller should surface 429."""


class SoftPullUnavailable(Exception):
    """Transport failure, missing credentials, or an unexpected error during
    the bureau call/persist — caller should surface 503."""


async def _keep_attempt(db: AsyncSession) -> None:
    """A failed attempt is part of the record.

    The caller answers the failure with an error, and the request's session
    rolls back with it; without this commit the REVOKED row the docstring
    below promises never reached the table, and nobody could count failures.
    """
    await db.flush()
    await db.commit()


PROXY_FICO_SOURCE = "intelligence-proxy"


def fico_source_of(pull: CreditPull | None) -> str | None:
    """Where the score came from, as run_soft_pull recorded it in the notes."""
    notes = str(getattr(pull, "notes", "") or "")
    for part in notes.split(";"):
        part = part.strip()
        if part.startswith("fico_source="):
            return part[len("fico_source="):] or None
    return None


def is_proxy_score(pull: CreditPull | None) -> bool:
    """True when the "score" is the 720 stand-in: the bureau returned none."""
    return fico_source_of(pull) == PROXY_FICO_SOURCE


def credit_status_extras(pull: CreditPull | None) -> dict:
    """The honesty fields the desk's credit status carries beside the score."""
    if pull is None:
        return {"fico_source": None, "score_verified": False, "report_available": False, "attention": None}
    source = fico_source_of(pull)
    response = pull.bureau_response if isinstance(pull.bureau_response, dict) else {}
    verified = pull.fico is not None and source != PROXY_FICO_SOURCE
    if pull.fico is None:
        attention = (
            "The linked report has not been imported. Import the existing report to retrieve its score; do not run a new pull."
            if not response.get("parsed_report") and not response.get("full_feed")
            else "No score was extracted from the imported report. Review the report and score model before requesting another pull."
        )
    elif source == PROXY_FICO_SOURCE:
        attention = "The bureau returned no score; the 720 shown is a stand-in from the identity check, not a bureau score."
    else:
        attention = None
    if response.get("report_import_status") == "pending":
        attention = response.get("report_import_error") or "The linked report import is pending and will retry automatically."
    return {
        "fico_source": source,
        "score_verified": verified,
        "report_available": bool(response.get("report_s3_key") or response.get("parsed_report") or response.get("full_feed")),
        "attention": attention,
    }


def intake_credit_state(pull: CreditPull, *, status: str = "completed") -> dict:
    """The compact cross-reference the intake carries (admin/AI only)."""
    return {
        "pull_id": str(pull.id),
        "fico": pull.fico,
        "fico_source": fico_source_of(pull),
        "pulled_at": pull.pulled_at.isoformat() if pull.pulled_at else None,
        "expires_at": pull.expires_at.isoformat() if pull.expires_at else None,
        "status": status,
    }


async def stamp_primary_owner(db: AsyncSession, intake, pull: CreditPull) -> bool:
    """Record the pull on the application's primary owner, so a booked file
    stops asking the borrower for a credit check they already completed."""
    from sqlalchemy import select

    from app.models.application_profile import ApplicationOwner, ApplicationProfile

    profile = (
        await db.execute(select(ApplicationProfile).where(ApplicationProfile.intake_id == intake.id).limit(1))
    ).scalar_one_or_none()
    if profile is None:
        return False
    owner = (
        await db.execute(
            select(ApplicationOwner)
            .where(
                ApplicationOwner.profile_id == profile.id,
                ApplicationOwner.is_primary.is_(True),
                ApplicationOwner.archived_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if owner is None or owner.credit_pull_id is not None:
        return False
    from app.services import credit_links

    await credit_links.link_pull(db, profile, owner, pull, how=credit_links.HOW_SIGNATURE)
    return True


async def _alert_desk(db: AsyncSession, intake, *, title: str, body: str) -> None:
    from app.enums import Role
    from app.services.notifications import notify_users, users_with_roles

    admins = await users_with_roles(db, Role.SUPER_ADMIN)
    await notify_users(
        db,
        recipient_ids={user.id for user in admins},
        event_type="credit_pull_attention",
        category="underwriting",
        priority="high",
        title=title,
        body=body,
        target_type="intake",
        target_id=str(intake.id),
        deep_link=f"/files/{intake.id}?section=review",
        batch_key=f"credit_pull:{intake.id}",
    )


async def auto_pull_after_signature(db: AsyncSession, intake, *, applicant_data: dict, request=None) -> dict:
    """Run the soft pull the moment the authorization is signed.

    The signature carries name, date of birth and address; the bureau can
    match on those alone, so nothing waits for a second click. A no-hit asks
    the client for the SSN in the room; a failure, a manual-review file, or a
    pull that comes back without a score goes to the desk. The intake's
    credit state is updated in place; the caller commits.
    """
    from app.services import mca_refinance_policy
    from app.services.bucket_ai import log_bucket_ai_activity

    state = dict(intake.intake_state or {})
    current = state.get("credit_pull") if isinstance(state.get("credit_pull"), dict) else {}
    if current.get("pull_id") or current.get("fico") is not None:
        return current
    if intake.client_id is None:
        return current

    def remember(**fields) -> dict:
        record = {**current, **fields, "attempted_at": datetime.now(UTC).isoformat()}
        state["credit_pull"] = record
        intake.intake_state = state
        return record

    if str(getattr(intake, "variant", "") or "").startswith("mca"):
        policy = mca_refinance_policy.persist(intake)
        if not policy.get("auto_underwriting_allowed"):
            record = remember(status="deferred", reason=policy.get("review_mode"))
            await log_bucket_ai_activity(
                db, intake.bucket_id, "credit_pull_deferred", target_type="intake", target_id=str(intake.id),
                detail="Authorization signed; the file is on manual review, so the desk runs the soft pull",
            )
            await _alert_desk(
                db, intake,
                title=f"Credit pull to run: {intake.business_name or intake.full_name}",
                body="The client signed the credit authorization. This file is on manual review, so the automatic soft pull did not run; run it from the file's credit panel.",
            )
            return record

    client = await db.get(Client, intake.client_id)
    if client is None:
        return current
    try:
        dob = date.fromisoformat(str(applicant_data.get("dob")))
    except (TypeError, ValueError):
        return remember(status="failed", error_code="invalid_dob")

    applicant = SoftPullApplicant(
        legal_first_name=str(applicant_data.get("legal_first_name") or ""),
        legal_last_name=str(applicant_data.get("legal_last_name") or ""),
        dob=dob,
        street=str(applicant_data.get("street") or ""),
        city=str(applicant_data.get("city") or ""),
        state=str(applicant_data.get("state") or ""),
        zip=str(applicant_data.get("zip") or ""),
        ssn=None,
    )
    try:
        pull = await run_soft_pull(db, client=client, applicant=applicant, actor=None)
    except SoftPullDenied as exc:
        if exc.code == "no_hit_provide_ssn":
            record = remember(status="needs_ssn", error_code=exc.code)
            await log_bucket_ai_activity(
                db, intake.bucket_id, "credit_pull_needs_ssn", target_type="intake", target_id=str(intake.id),
                detail="Soft pull ran on signature: no bureau match on name, address and date of birth; the client is asked for the SSN",
            )
            return record
        record = remember(status="failed", error_code=exc.code)
        await log_bucket_ai_activity(
            db, intake.bucket_id, "credit_pull_failed", target_type="intake", target_id=str(intake.id),
            detail=f"Soft pull on signature was denied by the bureau: {exc.code}",
        )
        await _alert_desk(
            db, intake,
            title=f"Credit pull needs attention: {intake.business_name or intake.full_name}",
            body=f"The soft pull after signing was denied by the bureau ({exc.code}). {exc.message}",
        )
        return record
    except (SoftPullValidationError, SoftPullRateLimited, SoftPullUnavailable) as exc:
        record = remember(status="failed", error_code=type(exc).__name__)
        await log_bucket_ai_activity(
            db, intake.bucket_id, "credit_pull_failed", target_type="intake", target_id=str(intake.id),
            detail=f"Soft pull on signature failed: {type(exc).__name__}",
        )
        await _alert_desk(
            db, intake,
            title=f"Credit pull needs attention: {intake.business_name or intake.full_name}",
            body=f"The soft pull after signing did not complete ({type(exc).__name__}). Run it from the file's credit panel.",
        )
        return record

    record = intake_credit_state(pull, status="completed")
    state["credit_pull"] = record
    intake.intake_state = state
    await stamp_primary_owner(db, intake, pull)
    await log_bucket_ai_activity(
        db, intake.bucket_id, "credit_pull_completed", target_type="intake", target_id=str(intake.id),
        detail=("Soft pull ran on signature" + (f" · FICO {pull.fico}" if pull.fico is not None and not is_proxy_score(pull) else " · no bureau score returned")),
    )
    if pull.fico is None or is_proxy_score(pull):
        await _alert_desk(
            db, intake,
            title=f"Credit pull returned no score: {intake.business_name or intake.full_name}",
            body="The soft pull completed but the bureau returned no score. Open the archived report on the file's credit panel, or re-run with the SSN.",
        )
    return record


async def run_soft_pull(
    db: AsyncSession,
    *,
    client: Client,
    applicant: SoftPullApplicant,
    actor: User | None = None,
) -> CreditPull:
    """Runs the bureau pull and persists a CreditPull row keyed to `client.id`.
    Raises SoftPullDenied for structured deny outcomes (no_hit_provide_ssn,
    bureau_freeze, generic) and RuntimeError for transport/validation/
    unexpected failures — callers translate these into their own HTTP shape.
    Always leaves a CreditPull row behind (REVOKED on failure) so operators
    can see the attempt in the audit trail either way."""
    settings = get_settings()
    private_key = settings.isoftpull_private_key or settings.isoftpull_api_key
    public_key = settings.isoftpull_public_key

    pull = CreditPull(
        client_id=client.id,
        status=CreditPullStatus.PENDING,
        legal_first_name=applicant.legal_first_name,
        legal_last_name=applicant.legal_last_name,
        dob=applicant.dob,
        street=applicant.street,
        city=applicant.city,
        state=applicant.state,
        zip=applicant.zip,
        last4_ssn=applicant.ssn[-4:] if applicant.ssn else None,
        fcra_consent=True,
    )
    db.add(pull)
    await db.flush()

    if not private_key or not public_key:
        pull.status = CreditPullStatus.REVOKED
        pull.notes = "iSoftPull credentials not configured"
        await db.flush()
        log.error("Credit pull attempted but the iSoftPull public/private key pair is incomplete")
        raise SoftPullUnavailable("Credit pull service is temporarily unavailable. Please contact support.")

    try:
        result = await isoftpull_client.pull(
            public_key=public_key,
            private_key=private_key,
            base_url=settings.isoftpull_api_url,
            applicant=isoftpull_client.ApplicantPayload(
                legal_first_name=applicant.legal_first_name,
                legal_last_name=applicant.legal_last_name,
                street=applicant.street,
                city=applicant.city,
                state=applicant.state,
                zip=applicant.zip,
                dob=applicant.dob.isoformat(),
                ssn=applicant.ssn,
            ),
            timeout_seconds=settings.isoftpull_timeout_seconds,
            max_retries=settings.isoftpull_max_retries,
        )
    except isoftpull_client.IsoftpullValidationError as exc:
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"validation: {exc}"
        await _keep_attempt(db)
        raise SoftPullValidationError(str(exc)) from exc
    except isoftpull_client.IsoftpullDeniedError as exc:
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"denied: {exc}"
        await _keep_attempt(db)
        detail_str = str(exc).lower()
        if "no-hit" in detail_str and not applicant.ssn:
            raise SoftPullDenied(
                "no_hit_provide_ssn",
                "We couldn't match this file on name + address + DOB alone. Add the SSN and try again.",
            ) from exc
        if "freeze" in detail_str or "frozen" in detail_str:
            raise SoftPullDenied(
                "bureau_freeze",
                "This credit file is frozen at the bureau. Ask the applicant to lift the freeze with "
                "Experian, Equifax, or TransUnion and try again.",
            ) from exc
        raise SoftPullDenied("bureau_denied", str(exc)) from exc
    except isoftpull_client.IsoftpullRateLimitedError as exc:
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"rate_limited: {exc}"
        await _keep_attempt(db)
        raise SoftPullRateLimited(str(exc)) from exc
    except isoftpull_client.IsoftpullTransportError as exc:
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"transport: {exc}"
        await _keep_attempt(db)
        raise SoftPullUnavailable("Credit bureau is temporarily unavailable — please retry.") from exc
    except Exception as exc:  # noqa: BLE001 — broad on purpose, mirrors the original endpoint
        log.exception("Unexpected error during iSoftPull bureau call")
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"unexpected: {type(exc).__name__}: {exc}"
        await _keep_attempt(db)
        raise SoftPullUnavailable("Credit pull failed — please retry. Our team has been notified.") from exc

    try:
        # FICO sourcing — three-tier fallback: Full Feed JSON (preferred) →
        # scraped report viewer HTML → intelligence-passed proxy (720).
        effective_fico = result.fico
        fico_source = "full_feed" if result.fico is not None else None

        scraped = isoftpull_report_parser.parse_full_feed(result.raw or {})
        report_html = None
        if result.report_link and scraped is None:
            # The page is fetched whether or not a score is still needed: it
            # is the report, and the desk wants it on file either way.
            scraped, report_html = await isoftpull_report_parser.fetch_report(result.report_link)
            if effective_fico is None and scraped and scraped.best_score is not None:
                effective_fico = scraped.best_score
                fico_source = f"scraped:{scraped.best_score_model}"

        if effective_fico is None and scraped and scraped.best_score is not None:
            effective_fico = scraped.best_score
            fico_source = f"full_feed:{scraped.best_score_model}" if (result.raw or {}).get("full_feed") else f"scraped:{scraped.best_score_model}"

        if effective_fico is None and result.intelligence_passed:
            effective_fico = INTELLIGENCE_PASSED_PROXY_FICO
            fico_source = "intelligence-proxy"

        persisted_response = dict(result.raw or {})
        if scraped is not None:
            persisted_response["parsed_report"] = scraped.to_dict()
        if report_html:
            archived = credit_report_archive.archive_report_html(pull.id, report_html)
            if archived:
                persisted_response["report_s3_key"] = archived
        pull.fico = effective_fico
        pull.bureau_response = persisted_response
        pull.status = CreditPullStatus.COMPLETED
        pull.pulled_at = result.pulled_at
        pull.expires_at = result.pulled_at + timedelta(days=SOFT_PULL_VALIDITY_DAYS)

        note_parts: list[str] = []
        if result.provider_pull_id:
            note_parts.append(f"isoftpull_id={result.provider_pull_id}")
        if result.intelligence_name:
            note_parts.append(
                f"intelligence={result.intelligence_name}:{'pass' if result.intelligence_passed else 'fail'}"
            )
        if fico_source:
            note_parts.append(f"fico_source={fico_source}")
        if scraped:
            detail = []
            if scraped.fico_8 is not None:
                detail.append(f"fico8={scraped.fico_8}")
            if scraped.fico_2 is not None:
                detail.append(f"fico2={scraped.fico_2}")
            if scraped.vantage_4 is not None:
                detail.append(f"vantage4={scraped.vantage_4}")
            if detail:
                note_parts.append("scraped:" + ",".join(detail))
        if note_parts:
            pull.notes = "; ".join(note_parts)

        previous_fico = client.fico
        client.fico = effective_fico
        await db.flush()
        await db.refresh(pull)

        from app.services.activity_log import log_change

        await log_change(
            db,
            kind="credit.pulled",
            summary=(
                f"Credit pulled · FICO {effective_fico}"
                + (f" (was {previous_fico})" if previous_fico is not None and previous_fico != effective_fico else "")
            ),
            client_id=pull.client_id,
            actor=actor,
            payload={
                "pull_id": str(pull.id),
                "fico": effective_fico,
                "previous_fico": previous_fico,
                "fico_source": fico_source,
                "expires_at": pull.expires_at.isoformat() if pull.expires_at else None,
            },
        )
        await calendar_emitter.emit_for_credit_pull(db, pull)
        try:
            # Automatic mode attaches the pull to its owner right away; manual
            # mode leaves it for the desk to link from the file.
            from app.services import credit_links

            await credit_links.after_pull(db, pull)
        except Exception:  # noqa: BLE001 - linking never fails the pull itself
            log.exception("credit_links.after_pull failed pull=%s", pull.id)
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to persist credit pull result")
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"persist_error: {type(exc).__name__}: {exc}"
        await db.flush()
        raise SoftPullUnavailable("Credit pull succeeded but failed to save — please retry.") from exc

    return pull
