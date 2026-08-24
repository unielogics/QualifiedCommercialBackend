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
from datetime import date, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.constants import SOFT_PULL_VALIDITY_DAYS
from app.enums import CreditPullStatus
from app.models.client import Client
from app.models.credit_pull import CreditPull
from app.models.user import User
from app.services import calendar_emitter, isoftpull_client, isoftpull_report_parser

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
        await db.flush()
        raise SoftPullValidationError(str(exc)) from exc
    except isoftpull_client.IsoftpullDeniedError as exc:
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"denied: {exc}"
        await db.flush()
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
        await db.flush()
        raise SoftPullRateLimited(str(exc)) from exc
    except isoftpull_client.IsoftpullTransportError as exc:
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"transport: {exc}"
        await db.flush()
        raise SoftPullUnavailable("Credit bureau is temporarily unavailable — please retry.") from exc
    except Exception as exc:  # noqa: BLE001 — broad on purpose, mirrors the original endpoint
        log.exception("Unexpected error during iSoftPull bureau call")
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"unexpected: {type(exc).__name__}: {exc}"
        await db.flush()
        raise SoftPullUnavailable("Credit pull failed — please retry. Our team has been notified.") from exc

    try:
        # FICO sourcing — three-tier fallback: Full Feed JSON (preferred) →
        # scraped report viewer HTML → intelligence-passed proxy (720).
        effective_fico = result.fico
        fico_source = "full_feed" if result.fico is not None else None

        scraped = None
        if effective_fico is None and result.report_link:
            scraped = await isoftpull_report_parser.fetch_and_parse(result.report_link)
            if scraped and scraped.best_score is not None:
                effective_fico = scraped.best_score
                fico_source = f"scraped:{scraped.best_score_model}"

        if effective_fico is None and result.intelligence_passed:
            effective_fico = INTELLIGENCE_PASSED_PROXY_FICO
            fico_source = "intelligence-proxy"

        persisted_response = dict(result.raw or {})
        if scraped is not None:
            persisted_response["parsed_report"] = scraped.to_dict()
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
    except Exception as exc:  # noqa: BLE001
        log.exception("Failed to persist credit pull result")
        pull.status = CreditPullStatus.REVOKED
        pull.notes = f"persist_error: {type(exc).__name__}: {exc}"
        await db.flush()
        raise SoftPullUnavailable("Credit pull succeeded but failed to save — please retry.") from exc

    return pull
