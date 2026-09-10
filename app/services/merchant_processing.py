"""The merchant-processing offer, end to end.

A processing partner prices a client's card processing and sends us a terms
sheet. This module is everything that happens to it after the desk drops it
on the file:

- the marker that tells the rest of the system a bucket file is an offer
  document and not evidence (`OFFER_SOURCE_DETAIL`, `is_offer_document`);
- the extraction prompt the analysis pipeline uses instead of the generic
  one, and `absorb_analysis`, which turns the model's answer into terms on
  the offer row;
- the saving arithmetic (`compute_savings`), done here and never by the model;
- the client's view of the offer — an allowlist, never a subtraction;
- the summary the AI threads and the review read from `intake_state`;
- the email to the partner when the client answers, through the audited
  outbox, with the outcome stored on the row.

Nothing here writes `key_metrics`, `estimated_dscr`, an approved amount or a
program's eligibility. The pro-forma DSCR is computed on read and shown.
"""

from __future__ import annotations

import html
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import ApplicationProfile
from app.models.bucket import BucketFile, BucketFileAnalysis
from app.models.lender import Lender
from app.models.merchant_processing_offer import MerchantProcessingOffer
from app.models.public_underwriting_intake import PublicUnderwritingIntake

log = logging.getLogger(__name__)

#: Printed as the third segment of the file's provenance line, so it reads
#: as a label everywhere a file is listed — and it is the one equality check
#: every exclusion in the system keys on.
OFFER_SOURCE_DETAIL = "Merchant processing offer"
OFFER_CLASSIFICATION = "merchant_processing_offer"

DISCLAIMER_VERSION = "2026-09-v1"
DISCLAIMER_TEXT = (
    "This estimate is based on the statement and pricing your processing partner "
    "provided. Your actual savings depend on your card volume and card mix. "
    "Accepting tells us to move forward with the partner on your behalf; it is not "
    "a contract, and the partner's own agreement will state the final terms."
)

STATUS_UPLOADED = "uploaded"
STATUS_EXTRACTED = "extracted"
STATUS_UNREADABLE = "unreadable"
STATUS_SENT = "sent"
STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
STATUS_WITHDRAWN = "withdrawn"
STATUS_SUPERSEDED = "superseded"

CLOSED_STATUSES = frozenset({STATUS_WITHDRAWN, STATUS_SUPERSEDED})
RESPONDED_STATUSES = frozenset({STATUS_ACCEPTED, STATUS_DECLINED})
CLIENT_VISIBLE_STATUSES = frozenset({STATUS_SENT, STATUS_ACCEPTED, STATUS_DECLINED})
#: Statuses in which a fresh read of the PDF may still replace the terms.
ABSORBABLE_STATUSES = frozenset({STATUS_UPLOADED, STATUS_EXTRACTED, STATUS_UNREADABLE, STATUS_SENT})

#: The numbers the client may see. This is the whole of `terms`; the client
#: view is built by walking this tuple, never by removing keys.
TERM_KEYS: tuple[str, ...] = (
    "provider_name",
    "prepared_for",
    "prepared_on",
    "current_processor",
    "current_monthly_volume",
    "current_monthly_fees",
    "current_effective_rate_pct",
    "proposed_monthly_fees",
    "proposed_effective_rate_pct",
    "proposed_pricing_model",
    "proposed_markup_bps",
    "proposed_per_item_fee",
    "proposed_fixed_monthly_fees",
    "stated_monthly_savings",
    "stated_annual_savings",
    "contract_term_months",
    "early_termination_fee",
    "equipment_notes",
    "options",
    "notes",
)
OPTION_KEYS: tuple[str, ...] = ("label", "effective_rate_pct", "monthly_fees", "monthly_savings")
DESK_ONLY_KEYS: tuple[str, ...] = (
    "agent_residual_pct",
    "agent_residual_monthly",
    "signing_bonus",
    "partner_notes",
    "savings_warning",
)
_NUMERIC_TERM_KEYS = frozenset(
    {
        "current_monthly_volume",
        "current_monthly_fees",
        "current_effective_rate_pct",
        "proposed_monthly_fees",
        "proposed_effective_rate_pct",
        "proposed_markup_bps",
        "proposed_per_item_fee",
        "proposed_fixed_monthly_fees",
        "stated_monthly_savings",
        "stated_annual_savings",
        "contract_term_months",
        "early_termination_fee",
    }
)

# The dedicated extraction prompt. It lives here, not in bucket_ai's
# per-persona builders, and it is chosen by the file's marker before any
# persona is consulted. It says "merchant processing" and never "merchant
# cash advance": the persona-isolation tests fingerprint the latter.
MERCHANT_OFFER_ANALYSIS_SYSTEM = """You are a senior commercial lending underwriter reading ONE document: a merchant processing (card processing) pricing proposal or savings analysis that a processing partner prepared for a business.

Return ONLY JSON in this exact shape. Do not wrap it in markdown fences.
{
  "classification": "merchant_processing_offer",
  "confidence": "high|medium|low",
  "summary": "1-2 sentence plain-English summary: who prepared it, for whom, and the headline saving it claims",
  "red_flags": ["anything an underwriter should check: figures that do not add up, missing volume, a term or early-termination fee, equipment leases, rates that look like a teaser"],
  "limitations": ["what the document does NOT show"],
  "key_facts": {
    "provider_name": "the processing partner that prepared the proposal|null",
    "prepared_for": "the business the proposal is for|null",
    "prepared_on": "date printed on the proposal|null",
    "current_processor": "the business's current processor if named|null",
    "current_monthly_volume": "monthly card volume in dollars today|null",
    "current_monthly_fees": "total processing fees per month today in dollars|null",
    "current_effective_rate_pct": "today's effective rate as a percent number, e.g. 3.12|null",
    "proposed_monthly_fees": "total processing fees per month under the proposal in dollars|null",
    "proposed_effective_rate_pct": "proposed effective rate as a percent number|null",
    "proposed_pricing_model": "interchange_plus|flat_rate|tiered|surcharge|dual_pricing|other|null",
    "proposed_markup_bps": "interchange-plus markup in basis points|null",
    "proposed_per_item_fee": "per-transaction fee in dollars|null",
    "proposed_fixed_monthly_fees": "fixed monthly fees (statement, PCI, gateway) in dollars|null",
    "stated_monthly_savings": "the monthly saving the document itself prints|null",
    "stated_annual_savings": "the annual saving the document itself prints|null",
    "contract_term_months": "contract length in months if stated|null",
    "early_termination_fee": "early termination fee in dollars if stated|null",
    "equipment_notes": "terminal / equipment / gateway terms in one line|null",
    "options": [{"label": "name of the pricing option or tier", "effective_rate_pct": null, "monthly_fees": null, "monthly_savings": null}],
    "notes": "one line the desk should know|null"
  },
  "desk_only": {
    "agent_residual_pct": "the agent's / referral partner's residual share as a percent number, if printed|null",
    "agent_residual_monthly": "the agent's residual in dollars per month, if printed|null",
    "signing_bonus": "any signing or conversion bonus to the agent in dollars, if printed|null",
    "partner_notes": "anything addressed to the referring agent rather than to the business|null"
  }
}

Rules. Copy only figures visibly printed in the document; use null when a figure is not printed. Never compute a saving the document does not print — the system computes savings from the figures you copy. Rates are percent numbers (3.12, not 0.0312 and not "3.12%"); money is a bare number with no currency symbol and no commas. Read every option or tier the document prints and list each one. If the document is not a merchant processing proposal at all, say so in the summary, set confidence to low and leave every figure null. Keep summary under 320 characters and each list <= 6 items."""


def is_offer_document(file: Any) -> bool:
    """Whether a bucket file is the partner's terms PDF rather than evidence."""
    return (getattr(file, "source_detail", None) or "") == OFFER_SOURCE_DETAIL


# ── numbers ─────────────────────────────────────────────────────────────────


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace("$", "").replace(",", "").replace("%", "").strip()
        if not cleaned:
            return None
        try:
            return float(cleaned)
        except ValueError:
            return None
    return None


def clean_terms(raw: Any) -> dict[str, Any]:
    """Only the keys the offer knows, with numbers as numbers. Options are
    reduced to their four fields. Anything else the model volunteered is
    dropped — a key it invents must not become a client-visible figure."""
    source = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in TERM_KEYS:
        if key not in source:
            continue
        value = source.get(key)
        if key == "options":
            options: list[dict[str, Any]] = []
            for item in value if isinstance(value, list) else []:
                if not isinstance(item, dict):
                    continue
                option = {
                    "label": (str(item.get("label") or "").strip() or None),
                    "effective_rate_pct": _num(item.get("effective_rate_pct")),
                    "monthly_fees": _num(item.get("monthly_fees")),
                    "monthly_savings": _num(item.get("monthly_savings")),
                }
                if any(v is not None for v in option.values()):
                    options.append(option)
            out["options"] = options
        elif key in _NUMERIC_TERM_KEYS:
            out[key] = _num(value)
        else:
            out[key] = (str(value).strip() or None) if value is not None else None
    return out


def clean_desk_terms(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {}
    for key in DESK_ONLY_KEYS:
        if key not in source:
            continue
        value = source.get(key)
        if key in ("partner_notes", "savings_warning"):
            out[key] = (str(value).strip() or None) if value is not None else None
        else:
            out[key] = _num(value)
    return out


def compute_savings(terms: dict[str, Any]) -> tuple[float | None, float | None, str | None, str | None]:
    """(monthly, annual, basis, warning).

    Fees today minus fees proposed when both are printed; else volume times
    the rate difference; else the figure the sheet itself states. A stated
    figure that disagrees with our arithmetic by more than a tenth is flagged
    for the desk, and a saving that is not positive is flagged too — it is
    kept, so the desk sees it, and Send asks for confirmation.
    """
    cur_fees = _num(terms.get("current_monthly_fees"))
    prop_fees = _num(terms.get("proposed_monthly_fees"))
    volume = _num(terms.get("current_monthly_volume"))
    cur_rate = _num(terms.get("current_effective_rate_pct"))
    prop_rate = _num(terms.get("proposed_effective_rate_pct"))
    stated_m = _num(terms.get("stated_monthly_savings"))
    stated_a = _num(terms.get("stated_annual_savings"))

    if cur_fees is not None and prop_fees is not None:
        monthly, basis = cur_fees - prop_fees, "fees_diff"
    elif volume is not None and cur_rate is not None and prop_rate is not None:
        monthly, basis = volume * (cur_rate - prop_rate) / 100.0, "rate_x_volume"
    elif stated_m is not None:
        monthly, basis = stated_m, "stated"
    elif stated_a is not None:
        monthly, basis = stated_a / 12.0, "stated"
    else:
        return None, None, None, None

    annual = monthly * 12.0
    warnings: list[str] = []
    stated_annual = stated_a if stated_a is not None else (stated_m * 12.0 if stated_m is not None else None)
    if basis != "stated" and stated_annual is not None and abs(annual - stated_annual) > 0.10 * max(abs(stated_annual), 1.0):
        warnings.append(
            f"The sheet states ${stated_annual:,.0f} a year; our arithmetic from its own figures gives "
            f"${annual:,.0f}. Check the figures before sending."
        )
    if monthly <= 0:
        warnings.append("The proposed pricing does not save money on these figures.")
    return round(monthly, 2), round(annual, 2), basis, (" ".join(warnings) or None)


def apply_savings(offer: MerchantProcessingOffer, *, manual_annual: float | None = None) -> None:
    """Recompute the saving from the offer's terms, or take the desk's figure."""
    desk = dict(offer.desk_terms or {})
    if manual_annual is not None:
        offer.estimated_annual_savings = Decimal(str(round(manual_annual, 2)))
        offer.estimated_monthly_savings = Decimal(str(round(manual_annual / 12.0, 2)))
        offer.savings_basis = "manual"
        desk["savings_warning"] = None if manual_annual > 0 else "The saving entered is not positive."
    else:
        monthly, annual, basis, warning = compute_savings(offer.terms or {})
        offer.estimated_monthly_savings = Decimal(str(monthly)) if monthly is not None else None
        offer.estimated_annual_savings = Decimal(str(annual)) if annual is not None else None
        offer.savings_basis = basis
        desk["savings_warning"] = warning
    offer.desk_terms = desk


def positive_saving(offer: MerchantProcessingOffer) -> bool:
    return offer.estimated_annual_savings is not None and float(offer.estimated_annual_savings) > 0


# ── the row ─────────────────────────────────────────────────────────────────


async def current_offer(db: AsyncSession, profile_id: UUID) -> MerchantProcessingOffer | None:
    """The one open offer on a file, or None."""
    return (
        await db.execute(
            select(MerchantProcessingOffer)
            .where(
                MerchantProcessingOffer.profile_id == profile_id,
                MerchantProcessingOffer.status.not_in(list(CLOSED_STATUSES)),
            )
            .order_by(MerchantProcessingOffer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def offer_count(db: AsyncSession, profile_id: UUID) -> int:
    rows = (
        await db.execute(
            select(MerchantProcessingOffer.id).where(MerchantProcessingOffer.profile_id == profile_id)
        )
    ).scalars().all()
    return len(rows)


async def offer_for_file(db: AsyncSession, file_id: UUID) -> MerchantProcessingOffer | None:
    return (
        await db.execute(
            select(MerchantProcessingOffer)
            .where(
                MerchantProcessingOffer.source_file_id == file_id,
                MerchantProcessingOffer.status.in_(list(ABSORBABLE_STATUSES)),
            )
            .order_by(MerchantProcessingOffer.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def absorb_analysis(
    db: AsyncSession, file: BucketFile, analysis: BucketFileAnalysis | None
) -> MerchantProcessingOffer | None:
    """Turn the file's analysis into terms on its offer row.

    Called from the analysis pipeline after a fresh read and on a cache hit,
    so a re-dropped PDF still lands its numbers. A row that has already been
    answered is never touched. A read that did not produce a proposal marks
    the offer unreadable, and the desk types the numbers.
    """
    if not is_offer_document(file):
        return None
    offer = await offer_for_file(db, file.id)
    if offer is None:
        return None
    data = analysis.analysis if analysis is not None and isinstance(analysis.analysis, dict) else {}
    readable = (
        analysis is not None
        and analysis.status == "completed"
        and analysis.classification == OFFER_CLASSIFICATION
    )
    terms = clean_terms(data.get("key_facts")) if readable else {}
    has_figure = any(terms.get(k) is not None for k in _NUMERIC_TERM_KEYS)
    if not readable or not has_figure:
        offer.status = STATUS_UNREADABLE if offer.status != STATUS_SENT else STATUS_SENT
        offer.extraction_error = (
            (analysis.skip_detail or analysis.error or "The document could not be read as a processing proposal.")
            if analysis is not None
            else "The document has not been analysed."
        )
        offer.extraction_confidence = analysis.confidence if analysis is not None else None
        await _sync_intake_state(db, offer)
        await db.flush()
        return offer
    had_terms = bool(offer.terms)
    offer.terms = terms
    offer.desk_terms = {**(offer.desk_terms or {}), **clean_desk_terms(data.get("desk_only"))}
    offer.extraction_confidence = analysis.confidence
    offer.extraction_error = None
    apply_savings(offer)
    if had_terms:
        offer.terms_version = (offer.terms_version or 1) + 1
    if offer.status != STATUS_SENT:
        offer.status = STATUS_EXTRACTED
    await _sync_intake_state(db, offer)
    await db.flush()
    return offer


async def ensure_extracted(
    db: AsyncSession, offer: MerchantProcessingOffer, file: BucketFile, *, review_type: str | None
) -> None:
    """The re-drop case. An identical file name and size dedups to the
    existing bucket file, and the analysis queue refuses a placeholder when
    a completed read already exists — so nothing would ever absorb. If that
    read is an offer read, absorb it now; if it is a generic evidence read
    (the same PDF was once dropped as ordinary evidence), read it again with
    the offer prompt, inline. With no read yet, the minute-drain does it.
    """
    from app.services.bucket_ai import CURRENT_FILE_ANALYSIS_VERSION, analyze_bucket_file

    existing = (
        await db.execute(
            select(BucketFileAnalysis)
            .where(
                BucketFileAnalysis.bucket_file_id == file.id,
                BucketFileAnalysis.analysis_version == CURRENT_FILE_ANALYSIS_VERSION,
                BucketFileAnalysis.status.in_(["completed", "skipped"]),
            )
            .order_by(BucketFileAnalysis.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing is None:
        return
    if existing.status == "completed" and existing.classification == OFFER_CLASSIFICATION:
        await absorb_analysis(db, file, existing)
        return
    await analyze_bucket_file(db, file, review_type=review_type, force=True)


# ── views ───────────────────────────────────────────────────────────────────


def _money(value: Any) -> float | None:
    return float(value) if value is not None else None


def client_view(offer: MerchantProcessingOffer, *, partner_name: str | None) -> dict[str, Any]:
    """What the client sees. Built by walking TERM_KEYS — `desk_terms` is
    not consulted, so nothing in it can leak by omission."""
    terms = offer.terms or {}
    return {
        "id": str(offer.id),
        "status": offer.status,
        "terms_version": offer.terms_version,
        "partner_name": partner_name or terms.get("provider_name"),
        "terms": {key: terms.get(key) for key in TERM_KEYS},
        "estimated_monthly_savings": _money(offer.estimated_monthly_savings),
        "estimated_annual_savings": _money(offer.estimated_annual_savings),
        "savings_basis": offer.savings_basis,
        "sent_at": offer.sent_at,
        "client_response": offer.client_response,
        "client_response_at": offer.client_response_at,
        "client_response_name": offer.client_response_name,
        "disclaimer_text": DISCLAIMER_TEXT,
        "disclaimer_version": DISCLAIMER_VERSION,
    }


def review_context(offer: MerchantProcessingOffer | None) -> dict[str, Any] | None:
    """The summary the AI threads and the review read from intake_state.
    Client-safe by construction: nothing from desk_terms."""
    if offer is None or offer.status in CLOSED_STATUSES:
        return None
    terms = offer.terms or {}
    return {
        "status": offer.status,
        "provider": terms.get("provider_name"),
        "current_effective_rate_pct": terms.get("current_effective_rate_pct"),
        "proposed_effective_rate_pct": terms.get("proposed_effective_rate_pct"),
        "current_monthly_volume": terms.get("current_monthly_volume"),
        "estimated_monthly_savings": _money(offer.estimated_monthly_savings),
        "estimated_annual_savings": _money(offer.estimated_annual_savings),
        "client_response": offer.client_response,
        "client_response_at": offer.client_response_at.isoformat() if offer.client_response_at else None,
    }


def pro_forma_dscr(key_metrics: dict[str, Any], annual_saving: float | None) -> dict[str, Any] | None:
    """DSCR with the saving added to the numerator, computed on read.

    estimated_dscr is tax net income over annual debt service, so
    dscr × (1 + saving / net income) is the same ratio with the saving in the
    numerator, using two figures that are both annual by construction.
    estimated_debt_burden is not used: when the model authors it, its unit is
    not stated, and a monthly figure would make the answer twelve times off.
    """
    if not annual_saving or annual_saving <= 0:
        return None
    dscr_now = _num(key_metrics.get("estimated_dscr"))
    net_income = _num(key_metrics.get("estimated_ebitda_or_cash_flow"))
    if dscr_now is None or not net_income or net_income <= 0:
        return None
    return {
        "dscr_now": round(dscr_now, 2),
        "dscr_with_saving": round(dscr_now * (1.0 + annual_saving / net_income), 2),
        "annual_saving": round(annual_saving, 2),
        "basis": "estimated_dscr × (1 + annual saving ÷ annual net income); pro-forma, not a decision of record",
    }


# ── the intake's copy ───────────────────────────────────────────────────────


async def _sync_intake_state(db: AsyncSession, offer: MerchantProcessingOffer) -> None:
    """Write the client-safe summary where the AI context builders can read
    it. They are synchronous and hold only the intake, so the offer keeps its
    own copy there, the way loan_program_fit and credit_pull do."""
    profile = await db.get(ApplicationProfile, offer.profile_id)
    if profile is None or profile.intake_id is None:
        return
    intake = await db.get(PublicUnderwritingIntake, profile.intake_id)
    if intake is None:
        return
    state = dict(intake.intake_state or {})
    summary = review_context(offer)
    if summary is None:
        state.pop("merchant_processing_offer", None)
    else:
        state["merchant_processing_offer"] = summary
    intake.intake_state = state


async def sync_intake_state(db: AsyncSession, offer: MerchantProcessingOffer) -> None:
    await _sync_intake_state(db, offer)


# ── telling the partner ─────────────────────────────────────────────────────


async def partner_email_enabled(db: AsyncSession) -> bool:
    from app.models.app_settings import AppSettings
    from app.schemas.settings import AppSettingsData

    row = (await db.execute(select(AppSettings).limit(1))).scalar_one_or_none()
    return AppSettingsData.model_validate(row.data if row else {}).merchant_processing.partner_email_enabled


def _line(value: Any) -> str:
    if value is None or value == "":
        return "-"
    if isinstance(value, float):
        return f"{value:,.2f}"
    return str(value)


def _pct(value: Any) -> str:
    number = _num(value)
    return f"{number:.2f}%" if number is not None else "-"


def _usd(value: Any) -> str:
    number = _num(value)
    return f"${number:,.2f}" if number is not None else "-"


def partner_email_bodies(
    offer: MerchantProcessingOffer,
    *,
    business_name: str,
    contact_name: str | None,
    contact_email: str | None,
    contact_phone: str | None,
) -> tuple[str, str, str]:
    """(subject, text, html). On accept the partner gets the contact they
    need to onboard the merchant; on decline the business and the reason,
    scrubbed through the one lender-facing content filter."""
    from app.services.lender_send import _scrub_sensitive

    terms = offer.terms or {}
    accepted = offer.client_response == STATUS_ACCEPTED
    when = offer.client_response_at.strftime("%b %d, %Y %I:%M %p UTC") if offer.client_response_at else "-"
    subject = f"Processing offer {'accepted' if accepted else 'declined'}: {business_name}"
    headline = (
        f"{business_name} accepted the processing offer you prepared."
        if accepted
        else f"{business_name} declined the processing offer you prepared."
    )
    rows: list[tuple[str, str]] = [("Business", business_name)]
    if accepted:
        rows += [
            ("Contact", _line(contact_name)),
            ("Email", _line(contact_email)),
            ("Phone", _line(contact_phone)),
        ]
    rows += [
        ("Proposal prepared for", _line(terms.get("prepared_for"))),
        ("Proposal date", _line(terms.get("prepared_on"))),
        ("Effective rate today", _pct(terms.get("current_effective_rate_pct"))),
        ("Proposed effective rate", _pct(terms.get("proposed_effective_rate_pct"))),
        ("Monthly card volume", _usd(terms.get("current_monthly_volume"))),
        ("Estimated annual saving", _usd(offer.estimated_annual_savings)),
        ("Answered", f"{when} by {_line(offer.client_response_name)}"),
    ]
    if not accepted:
        reason = _scrub_sensitive(offer.client_response_reason or "")
        rows.append(("Reason given", reason or "No reason given"))
    footer = "Sent by Qualified Commercial on behalf of the business named above. Reply to this email to reach the desk."
    text = headline + "\n\n" + "\n".join(f"{label}: {value}" for label, value in rows) + "\n\n" + footer + "\n"
    html_body = (
        f"<p>{html.escape(headline)}</p><ul>"
        + "".join(f"<li><strong>{html.escape(label)}:</strong> {html.escape(value)}</li>" for label, value in rows)
        + f"</ul><p style=\"color:#64748b;font-size:12px\">{html.escape(footer)}</p>"
    )
    return subject, text, html_body


async def notify_partner(
    db: AsyncSession,
    offer: MerchantProcessingOffer,
    *,
    profile: ApplicationProfile,
    intake: PublicUnderwritingIntake | None,
    business_name: str,
    force: bool = False,
) -> None:
    """Email the processing partner the client's answer, once.

    Records the outcome on the offer row whatever happens, and never raises:
    the client's answer is already committed, and a failed send is a thing
    the desk resends, not a thing that undoes a decision. Goes through the
    outbox so a MessageSend row exists for the audit.
    """
    from app.services.email.ses_client import ses_configured
    from app.services.messaging.outbox import Draft, Subject, deliver_email

    if offer.client_response not in RESPONDED_STATUSES:
        return
    if offer.partner_email_status == "sent" and not force:
        return
    now = datetime.now(UTC)

    def _skip(reason: str) -> None:
        offer.partner_email_status = "skipped"
        offer.partner_email_error = reason
        offer.partner_email_at = now

    try:
        if not await partner_email_enabled(db):
            _skip("Partner email is switched off in settings.")
            return
        if not ses_configured():
            _skip("Email transport is not configured.")
            return
        lender = await db.get(Lender, offer.lender_id) if offer.lender_id else None
        to_email = (lender.submission_email or lender.contact_email) if lender else None
        if not lender:
            _skip("No processing partner is on the offer.")
            return
        if not to_email:
            _skip(f"{lender.name} has no email address on the roster.")
            return
        subject, text, html_body = partner_email_bodies(
            offer,
            business_name=business_name,
            contact_name=(intake.full_name if intake else None),
            contact_email=(intake.email if intake else None),
            contact_phone=(intake.phone if intake else None),
        )
        outcome = await deliver_email(
            db,
            Draft(to=to_email, subject=subject, body_text=text, body_html=html_body),
            context="merchant_offer",
            template_key=f"merchant_offer_{offer.client_response}",
            subject=Subject(
                owner_user_id=offer.sent_by_user_id,
                client_id=profile.client_id,
                profile_id=profile.id,
                intake_id=intake.id if intake else None,
            ),
        )
        offer.partner_email_status = "sent" if outcome.ok else "failed"
        offer.partner_email_message_id = outcome.message_id
        offer.partner_email_error = None if outcome.ok else (outcome.detail or "send failed")[:2000]
        offer.partner_email_at = now
    except Exception as exc:  # noqa: BLE001
        log.exception("merchant offer: partner email failed offer=%s", offer.id)
        offer.partner_email_status = "failed"
        offer.partner_email_error = str(exc)[:2000]
        offer.partner_email_at = now
