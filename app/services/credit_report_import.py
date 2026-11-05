"""Import an existing bureau report. Never creates a new bureau inquiry."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import String, cast, func, or_, select

from app.models.application_profile import ApplicationOwner
from app.models.credit_pull import CreditPull
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import credit_report_archive, isoftpull_report_parser
from app.services.isoftpull_session import IsoftpullSessionError

log = logging.getLogger(__name__)
IMPORT_VERSION = 2


class ReportImportUnavailable(Exception):
    pass


def stored_report(raw: dict) -> dict | None:
    report = isoftpull_report_parser.parse_full_feed(raw)
    if report is not None:
        return report.to_dict()
    saved = raw.get("parsed_report")
    if not isinstance(saved, dict) or not saved:
        return None
    # Older imports may contain FICO 4 in scores but lack the headline alias.
    report = isoftpull_report_parser.ScrapedReport(scores=[
        isoftpull_report_parser.CreditScore(
            model=str(item.get("model") or ""), score=item.get("score"),
            reason_codes=item.get("reason_codes") or [],
        ) for item in saved.get("scores", []) if isinstance(item, dict)
    ])
    isoftpull_report_parser.select_headline_score(report)
    return {**saved, **{key: getattr(report, key) for key in (
        "fico_8", "fico_4", "fico_2", "vantage_4", "best_score", "best_score_model",
    )}}


async def import_existing_report(db, pull) -> dict:
    from app.config import get_settings
    from app.models.client import Client

    raw = dict(pull.bureau_response or {})
    parsed = stored_report(raw)
    source = "full_feed" if raw.get("full_feed") else "scraped"
    if parsed is None or (not parsed.get("scores") and not raw.get("full_feed")):
        settings = get_settings()
        if not settings.isoftpull_login_email or not settings.isoftpull_login_password:
            raise ReportImportUnavailable(
                "The existing report is linked, but report access is not configured. "
                "Enable iSoftPull Full Feed or configure the approved report-access service account. "
                "No new credit pull was made."
            )
        reports = raw.get("reports") or {}
        link = next((node.get("link") for bureau in ("transunion", "equifax", "experian")
                     if isinstance(node := reports.get(bureau), dict)
                     and node.get("status") == "success" and node.get("link")), None) or raw.get("provider_report_url")
        if not link:
            raise ReportImportUnavailable("No provider report link is stored for this pull. No new credit pull was made.")
        try:
            report, html = await isoftpull_report_parser.fetch_report(link, raise_access_errors=True)
        except IsoftpullSessionError as exc:
            raise ReportImportUnavailable(str(exc)) from exc
        if report is None:
            raise ReportImportUnavailable("The existing provider report could not be retrieved. Check report access and retry; no new credit pull was made.")
        if not report.scores and not report.trade_accounts and not report.personal_info:
            raise ReportImportUnavailable("The provider page did not contain a readable credit report. No new credit pull was made.")
        parsed = report.to_dict()
        if html:
            key = credit_report_archive.archive_report_html(pull.id, html)
            if key:
                raw["report_s3_key"] = key

    from app.services.credit_risk_review import analyze_report

    parsed["intelligence"] = analyze_report(parsed, as_of=pull.pulled_at)
    raw["parsed_report"] = parsed
    raw["report_imported_at"] = datetime.now(UTC).isoformat()
    raw.update(report_import_version=IMPORT_VERSION, report_import_status="completed",
               report_import_error=None, report_import_next_retry_at=None,
               report_import_access_blocked=False)
    pull.bureau_response = raw
    if parsed.get("best_score") is not None:
        pull.fico = parsed["best_score"]
        notes = [part.strip() for part in (pull.notes or "").split(";")
                 if part.strip() and not part.strip().startswith("fico_source=")]
        notes.append(f"fico_source={source}:{parsed['best_score_model']}")
        pull.notes = "; ".join(notes)
    await db.flush()

    await sync_linked_owners(db, pull)
    latest = await db.scalar(select(CreditPull.id).where(
        CreditPull.client_id == pull.client_id, CreditPull.status == "completed",
    ).order_by(CreditPull.pulled_at.desc()).limit(1))
    if latest == pull.id:
        client = await db.get(Client, pull.client_id)
        if client is not None:
            client.fico = pull.fico
    await db.flush()
    return parsed


async def sync_linked_owners(db, pull) -> None:
    from app.dealer_os.models import DealerOwner
    from app.dealer_os.services import credit_quality
    from app.services.credit_links import pull_facts, tier_for
    from app.services.credit_pull_core import intake_credit_state

    facts = pull_facts(pull)
    for model in (ApplicationOwner, DealerOwner):
        owners = (await db.scalars(select(model).where(model.credit_pull_id == pull.id))).all()
        for owner in owners:
            owner.credit_score = pull.fico
            owner.credit_tier = tier_for(pull.fico)
            owner.credit_summary = {**(owner.credit_summary or {}), **facts}
            if isinstance(owner, DealerOwner):
                quality = credit_quality.summary(pull.fico)
                owner.credit_tier = quality["quality_tier"] if quality else None
                owner.credit_summary = {**owner.credit_summary, **(quality or {})}
    intakes = (await db.scalars(select(PublicUnderwritingIntake).where(
        PublicUnderwritingIntake.intake_state["credit_pull"]["pull_id"].astext == str(pull.id),
    ))).all()
    for intake in intakes:
        state = dict(intake.intake_state or {})
        state["credit_pull"] = {**(state.get("credit_pull") or {}), **intake_credit_state(pull)}
        intake.intake_state = state
    await db.flush()


async def ensure_report_imported(db, pull) -> dict | None:
    """Every linking path imports, or records a retryable provider failure."""
    now = datetime.now(UTC)
    try:
        return await import_existing_report(db, pull)
    except ReportImportUnavailable as exc:
        raw = dict(pull.bureau_response or {})
        attempts = int(raw.get("report_import_attempts") or 0) + 1
        delay = min(60, 5 * 2 ** min(attempts - 1, 4))
        raw.update(report_import_status="pending", report_import_error=str(exc)[:600],
                   report_import_access_blocked=isinstance(exc.__cause__, IsoftpullSessionError),
                   report_import_attempts=attempts, report_import_attempted_at=now.isoformat(),
                   report_import_next_retry_at=(now + timedelta(minutes=delay)).isoformat())
        pull.bureau_response = raw
        await db.flush()
        await sync_linked_owners(db, pull)
        return None


async def run_pending_report_imports(batch_size: int = 20) -> dict[str, int]:
    """Backfill/retry linked reports in any linking mode; never run new pulls."""
    from app.db import SessionLocal
    from app.dealer_os.models import DealerOwner

    now = datetime.now(UTC).isoformat()
    linked = or_(
        select(ApplicationOwner.id).where(ApplicationOwner.credit_pull_id == CreditPull.id,
            ApplicationOwner.archived_at.is_(None)).exists(),
        select(DealerOwner.id).where(DealerOwner.credit_pull_id == CreditPull.id).exists(),
        select(PublicUnderwritingIntake.id).where(
            PublicUnderwritingIntake.intake_state["credit_pull"]["pull_id"].astext == cast(CreditPull.id, String),
        ).exists(),
    )
    statement = select(CreditPull.id).where(
        CreditPull.status == "completed", linked,
        or_(func.coalesce(CreditPull.bureau_response["report_import_version"].astext, "") != str(IMPORT_VERSION),
            CreditPull.bureau_response["report_import_status"].astext == "pending"),
        func.coalesce(CreditPull.bureau_response["report_import_next_retry_at"].astext, "") <= now,
    ).order_by(func.coalesce(CreditPull.bureau_response["report_import_attempted_at"].astext, ""), CreditPull.created_at).limit(batch_size)
    async with SessionLocal() as db:
        ids = list((await db.scalars(statement)).all())
    result = {"checked": 0, "imported": 0, "pending": 0, "failed": 0}
    for pull_id in ids:
        try:
            async with SessionLocal() as db:
                pull = await db.scalar(select(CreditPull).where(CreditPull.id == pull_id, linked,
                    CreditPull.status == "completed").with_for_update(skip_locked=True))
                if pull is None:
                    continue
                raw = pull.bureau_response or {}
                if raw.get("report_import_version") == IMPORT_VERSION and raw.get("report_import_status") == "completed":
                    continue
                if (raw.get("report_import_next_retry_at") or "") > now:
                    continue
                result["checked"] += 1
                report = await ensure_report_imported(db, pull)
                await db.commit()
                result["imported" if report is not None else "pending"] += 1
                if report is None and (pull.bureau_response or {}).get("report_import_access_blocked"):
                    # A service-account login/terms block affects the whole batch.
                    break
        except Exception:
            result["failed"] += 1
            log.exception("Existing credit report import failed for pull %s", pull_id)
    return result
