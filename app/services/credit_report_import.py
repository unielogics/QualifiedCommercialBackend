"""Import an existing bureau report. Never creates a new bureau inquiry."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from app.models.application_profile import ApplicationOwner
from app.models.credit_pull import CreditPull
from app.services import credit_report_archive, isoftpull_report_parser
from app.services.isoftpull_session import IsoftpullSessionError


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
    from app.dealer_os.models import DealerOwner
    from app.models.client import Client
    from app.services.credit_links import pull_facts, tier_for

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
                     and node.get("status") == "success" and node.get("link")), None)
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

    raw["parsed_report"] = parsed
    raw["report_imported_at"] = datetime.now(UTC).isoformat()
    pull.bureau_response = raw
    if parsed.get("best_score") is not None:
        pull.fico = parsed["best_score"]
        notes = [part.strip() for part in (pull.notes or "").split(";")
                 if part.strip() and not part.strip().startswith("fico_source=")]
        notes.append(f"fico_source={source}:{parsed['best_score_model']}")
        pull.notes = "; ".join(notes)
    await db.flush()

    facts = pull_facts(pull)
    for model in (ApplicationOwner, DealerOwner):
        owners = (await db.scalars(select(model).where(model.credit_pull_id == pull.id))).all()
        for owner in owners:
            owner.credit_score = pull.fico
            owner.credit_tier = tier_for(pull.fico)
            owner.credit_summary = {**(owner.credit_summary or {}), **facts}
    latest = await db.scalar(select(CreditPull.id).where(
        CreditPull.client_id == pull.client_id, CreditPull.status == "completed",
    ).order_by(CreditPull.pulled_at.desc()).limit(1))
    if latest == pull.id:
        client = await db.get(Client, pull.client_id)
        if client is not None:
            client.fico = pull.fico
    await db.flush()
    return parsed
