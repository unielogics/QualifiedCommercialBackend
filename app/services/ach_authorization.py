"""Immutable ACH authorization language and certificate rendering.

This is deliberately separate from the existing Stripe file-expense
authorization.  An ACH mandate created here is limited to the exact fee
obligation (or fixed private schedule) identified on its certificate.
"""

from __future__ import annotations

import hashlib
import html
from datetime import datetime
from decimal import Decimal

from app.models.payments import AchMandate, PaymentDebitNotice, PaymentFundingSource

AUTHORIZATION_TEXT_VERSION = "ach-fee-2026-10-05-v1"
ONE_TIME_CCD_AUTHORIZATION_TEXT_VERSION = "ach-one-time-ccd-2026-10-06-v1"
PRIVATE_SCHEDULE_AUTHORIZATION_TEXT_VERSION = "ach-private-schedule-2026-10-05-v1"


def authorization_text(*, ach_class: str, maximum_amount_cents: int) -> str:
    amount = f"${maximum_amount_cents / 100:,.2f}"
    account_kind = "business (CCD)" if ach_class.upper() == "CCD" else "consumer (WEB)"
    return (
        "I authorize Qualified Commercial LLC to originate a one-time ACH debit "
        f"from the connected {account_kind} account for no more than {amount}, "
        "and only for the fee lines shown in this authorization. No debit may be "
        "submitted until the related financing has actually funded, Qualified "
        "Commercial has recorded an authoritative funding confirmation, and an "
        "authorized staff member separately releases the collection. An estimated "
        "closing date, accepted amount, or pipeline status does not authorize a "
        "debit. I consent to electronic records and signatures and may request a "
        "copy of this authorization. Once a transfer has been submitted it cannot "
        "be recalled through revocation of this authorization."
    )


def one_time_fee_authorization_text(
    *,
    amount_cents: int,
    scheduled_debit_at: datetime,
    debit_window_start_at: datetime,
    debit_window_end_at: datetime,
    account_mask: str | None,
    revocation_cutoff_at: datetime,
    agreement_sha256: str,
    obligation_sha256: str,
    business_name: str | None = None,
    payer_name: str | None = None,
) -> str:
    """Exact CCD consent: one amount, one account, and one dated window."""

    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    scheduled = scheduled_debit_at.astimezone(eastern)
    window_start = debit_window_start_at.astimezone(eastern)
    window_end = debit_window_end_at.astimezone(eastern)
    cutoff = revocation_cutoff_at.astimezone(eastern)
    return (
        f"I, {payer_name or 'the authorized signer'}, certify that I am authorized to act for "
        f"{business_name or 'the payer business'}, that the connected account is its business "
        "bank account, and authorize Qualified Commercial LLC, as originator, to originate "
        "exactly one CCD ACH debit of "
        f"${amount_cents / 100:,.2f} from the business account ending "
        f"{account_mask or '----'} on {scheduled.strftime('%B %d, %Y')}. The debit may be "
        f"submitted only during the business-day Eastern Time window from "
        f"{window_start.isoformat()} through {window_end.isoformat()}. This is not a recurring, "
        "variable, installment, or consumer authorization, and it cannot be used for any other "
        f"amount, date, account, agreement, or obligation. Agreement SHA-256: {agreement_sha256}. "
        f"Obligation SHA-256: {obligation_sha256}. I may revoke this unsubmitted debit in the "
        f"secure application room or by emailing support@qualifiedcommercial.com through "
        f"{cutoff.isoformat()} (5:00 PM Eastern Time one business day before the scheduled debit). "
        "Revocation cannot recall a debit already submitted and does not cancel an underlying fee "
        "that has otherwise been earned and remains due under the signed agreement. The account "
        "was connected through Plaid; Qualified Commercial does not receive or store my online-"
        "banking credentials. Qualified Commercial must separately send advance notice and an "
        "authorized staff member must separately release the debit after the notice period. I "
        "consent to electronic records and signatures and may download or request another copy."
    )


def private_schedule_authorization_text(*, total_amount_cents: int) -> str:
    amount = f"${total_amount_cents / 100:,.2f}"
    return (
        "I authorize Qualified Commercial LLC to originate the fixed business-to-business "
        f"CCD debits listed in this schedule, totaling no more than {amount}, from the connected "
        "business bank account. This authorization applies only to the exact schedule version "
        "identified on this certificate and becomes usable only after actual funding is confirmed, "
        "the executed private-funding agreement is verified, any required servicing authority is "
        "active, and authorized staff activates the schedule. There are no percentage-of-revenue "
        "debits, automatic late fees, or automatic retries. I may revoke future unclaimed payments, "
        "but revocation cannot recall a transfer already submitted. I consent to electronic records "
        "and signatures and may request a copy of this authorization."
    )


def obligation_hash(payload: dict[str, object]) -> str:
    import json

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def signature_hash(*, typed_name: str, obligation_sha256: str, signed_at: datetime, ip_address: str | None) -> str:
    value = "|".join((typed_name.strip(), obligation_sha256, signed_at.isoformat(), ip_address or ""))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def render_certificate_pdf(
    *,
    mandate: AchMandate,
    funding_source: PaymentFundingSource,
    business_name: str | None,
    client_name: str | None,
    client_email: str | None,
    fee_lines: list[tuple[str, int, str | None]],
    accepted_amount: Decimal | None,
    origination_points: Decimal | None,
    origination_fee_cents: int,
    consulting_fee_cents: int,
    agreement_reference: str,
    agreement_sha256: str,
) -> bytes:
    from weasyprint import HTML

    rows = [
        ("Business", business_name or "Application file"),
        ("Authorized payer", mandate.payer_name),
        ("Client contact", client_name or ""),
        ("Client email", client_email or mandate.payer_email or ""),
        ("Authorization ID", str(mandate.id)),
        ("Authorization version", mandate.authorization_text_version),
        ("Obligation SHA-256", mandate.obligation_sha256),
        ("Funding-source evidence SHA-256", mandate.funding_source_sha256),
        ("Signed at", mandate.signed_at.isoformat()),
        ("IP address", mandate.ip_address or ""),
        ("User agent", mandate.user_agent or ""),
        ("ACH classification", mandate.ach_class.upper()),
        ("Account", f"{funding_source.account_name or 'Bank account'} ending {funding_source.account_mask or '----'}"),
        ("Maximum authorized amount", f"${mandate.authorized_amount_cents / 100:,.2f}"),
        ("Accepted financing amount", f"${accepted_amount:,.2f}" if accepted_amount is not None else "Not recorded"),
        ("Origination rate", f"{origination_points.normalize()}%" if origination_points is not None else "0%"),
        (
            "Origination calculation",
            (
                f"${accepted_amount:,.2f} × {origination_points.normalize()}% = "
                f"${origination_fee_cents / 100:,.2f}"
                if accepted_amount is not None and origination_points is not None
                else f"${origination_fee_cents / 100:,.2f}"
            ),
        ),
        ("Fixed consulting fee", f"${consulting_fee_cents / 100:,.2f}"),
        ("Executed fee agreement", agreement_reference),
        ("Agreement SHA-256", agreement_sha256),
    ]
    row_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    fee_html = "".join(
        "<tr>"
        f"<td>{html.escape(label)}</td>"
        f"<td>${amount_cents / 100:,.2f}</td>"
        f"<td>{html.escape(reference or '—')}</td>"
        "</tr>"
        for label, amount_cents, reference in fee_lines
    )
    terms = html.escape(
        mandate.authorization_text_snapshot
        or authorization_text(
            ach_class=mandate.ach_class,
            maximum_amount_cents=mandate.authorized_amount_cents,
        )
    )
    body = f"""
    <html><head><style>
      @page {{ size: letter; margin: 0.65in; }}
      body {{ font-family: Arial, sans-serif; color: #111827; font-size: 11px; }}
      h1 {{ color: #1e3a8a; font-size: 22px; margin: 0 0 4px; }}
      h2 {{ font-size: 14px; margin: 24px 0 8px; }}
      .muted {{ color: #64748b; }}
      table {{ width: 100%; border-collapse: collapse; }}
      th {{ width: 34%; text-align: left; background: #f1f5f9; }}
      th, td {{ border: 1px solid #cbd5e1; padding: 7px 9px; vertical-align: top; }}
      .terms {{ border: 1px solid #cbd5e1; background: #f8fafc; padding: 14px; line-height: 1.5; }}
    </style></head><body>
      <h1>ACH Authorization Certificate</h1>
      <div class="muted">Qualified Commercial LLC · secure fee collection</div>
      <h2>Authorization record</h2><table>{row_html}</table>
      <h2>Authorized fee lines</h2>
      <table><tr><th>Fee component</th><th>ACH amount</th><th>Agreement</th></tr>{fee_html}</table>
      <h2>Terms accepted</h2><div class="terms">{terms}</div>
    </body></html>
    """
    pdf = HTML(string=body).write_pdf()
    if not pdf:
        raise RuntimeError("ACH authorization certificate generation failed")
    return pdf


def render_debit_notice_pdf(
    *,
    notice: PaymentDebitNotice,
    mandate: AchMandate,
    funding_source: PaymentFundingSource,
    authorization_text: str,
) -> bytes:
    """Render the immutable notice sent separately from the signed mandate."""

    from zoneinfo import ZoneInfo

    from weasyprint import HTML

    eastern = ZoneInfo("America/New_York")
    rows = [
        ("Notice ID", str(notice.id)),
        ("Authorization ID", str(mandate.id)),
        ("Notice type", "Exact one-time business CCD debit"),
        ("Amount", f"${notice.amount_cents / 100:,.2f} {notice.currency.upper()}"),
        ("Business account", f"{funding_source.account_name or 'Bank account'} ending {notice.account_mask or '----'}"),
        ("Scheduled debit date", notice.scheduled_debit_at.astimezone(eastern).strftime("%B %d, %Y")),
        ("Submission window starts", notice.debit_window_start_at.astimezone(eastern).isoformat()),
        ("Submission window ends", notice.debit_window_end_at.astimezone(eastern).isoformat()),
        ("Advance-notice period", f"{notice.notice_business_days} business days"),
        ("Revocation cutoff", notice.revocation_cutoff_at.astimezone(eastern).isoformat()),
        ("Authorization text SHA-256", notice.authorization_text_sha256 or ""),
        ("Agreement SHA-256", mandate.agreement_sha256 or ""),
        ("Obligation SHA-256", mandate.obligation_sha256),
        ("Notice snapshot SHA-256", notice.notice_sha256),
    ]
    row_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    terms = html.escape(authorization_text)
    body = f"""
    <html><head><style>
      @page {{ size: letter; margin: 0.7in; }}
      body {{ font-family: Arial, sans-serif; color: #111827; font-size: 11px; }}
      h1 {{ color: #1e3a8a; font-size: 21px; margin: 0 0 4px; }}
      h2 {{ font-size: 14px; margin: 24px 0 8px; }}
      .muted {{ color: #64748b; }}
      .warning {{ border: 2px solid #1e3a8a; padding: 14px; margin-top: 18px; font-size: 13px; }}
      table {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
      th {{ width: 34%; text-align: left; background: #f1f5f9; }}
      th, td {{ border: 1px solid #cbd5e1; padding: 7px 9px; vertical-align: top; }}
      .terms {{ border: 1px solid #cbd5e1; background: #f8fafc; padding: 14px; line-height: 1.5; }}
    </style></head><body>
      <h1>Advance Notice of One-Time ACH Debit</h1>
      <div class="muted">Qualified Commercial LLC · separate debit notice</div>
      <div class="warning">Qualified Commercial intends to submit exactly one business CCD debit
      of <strong>${notice.amount_cents / 100:,.2f}</strong> on
      <strong>{notice.scheduled_debit_at.astimezone(eastern).strftime('%B %d, %Y')}</strong>.</div>
      <h2>Exact notice record</h2><table>{row_html}</table>
      <h2>Authorization tied to this notice</h2><div class="terms">{terms}</div>
    </body></html>
    """
    pdf = HTML(string=body).write_pdf()
    if not pdf:
        raise RuntimeError("ACH advance-notice PDF generation failed")
    return pdf


def render_private_schedule_certificate_pdf(
    *,
    mandate: AchMandate,
    funding_source: PaymentFundingSource,
    business_name: str | None,
    client_name: str | None,
    client_email: str | None,
    creditor_name: str,
    payee_name: str,
    settlement_destination_ref: str,
    agreement_reference: str,
    production_term_sheet_id: object,
    production_term_sheet_version: int,
    schedule_sha256: str,
    cadence: str,
    installments: list[tuple[int, str, int]],
) -> bytes:
    from weasyprint import HTML

    rows = [
        ("Business", business_name or "Application file"),
        ("Authorized payer", mandate.payer_name),
        ("Client contact", client_name or ""),
        ("Client email", client_email or mandate.payer_email or ""),
        ("Creditor", creditor_name),
        ("Payee", payee_name),
        ("Settlement destination", settlement_destination_ref),
        ("Executed agreement", agreement_reference),
        ("Production Term Sheet ID", str(production_term_sheet_id)),
        ("Production Term Sheet version", production_term_sheet_version),
        ("Authorized schedule SHA-256", schedule_sha256),
        ("Authorization ID", str(mandate.id)),
        ("Authorization version", mandate.authorization_text_version),
        ("Funding-source evidence SHA-256", mandate.funding_source_sha256),
        ("Mandate schedule SHA-256", mandate.obligation_sha256),
        ("Signed at", mandate.signed_at.isoformat()),
        ("IP address", mandate.ip_address or ""),
        ("User agent", mandate.user_agent or ""),
        ("ACH classification", "CCD"),
        ("Account", f"{funding_source.account_name or 'Bank account'} ending {funding_source.account_mask or '----'}"),
        ("Cadence", cadence.replace("_", " ").title()),
        ("Maximum schedule total", f"${mandate.authorized_amount_cents / 100:,.2f}"),
    ]
    row_html = "".join(
        f"<tr><th>{html.escape(label)}</th><td>{html.escape(str(value))}</td></tr>"
        for label, value in rows
    )
    schedule_html = "".join(
        "<tr>"
        f"<td>{sequence}</td><td>{html.escape(due_date)}</td><td>${amount_cents / 100:,.2f}</td>"
        "</tr>"
        for sequence, due_date, amount_cents in installments
    )
    terms = html.escape(private_schedule_authorization_text(
        total_amount_cents=mandate.authorized_amount_cents,
    ))
    body = f"""
    <html><head><style>
      @page {{ size: letter; margin: 0.65in; }}
      body {{ font-family: Arial, sans-serif; color: #111827; font-size: 11px; }}
      h1 {{ color: #1e3a8a; font-size: 22px; margin: 0 0 4px; }}
      h2 {{ font-size: 14px; margin: 24px 0 8px; }}
      .muted {{ color: #64748b; }}
      table {{ width: 100%; border-collapse: collapse; }}
      th {{ width: 34%; text-align: left; background: #f1f5f9; }}
      th, td {{ border: 1px solid #cbd5e1; padding: 7px 9px; vertical-align: top; }}
      .terms {{ border: 1px solid #cbd5e1; background: #f8fafc; padding: 14px; line-height: 1.5; }}
    </style></head><body>
      <h1>Standing ACH Authorization Certificate</h1>
      <div class="muted">Qualified Commercial LLC · fixed private-funding schedule</div>
      <h2>Authorization record</h2><table>{row_html}</table>
      <h2>Authorized fixed schedule</h2>
      <table><tr><th>Payment</th><th>Banking date</th><th>Amount</th></tr>{schedule_html}</table>
      <h2>Terms accepted</h2><div class="terms">{terms}</div>
    </body></html>
    """
    pdf = HTML(string=body).write_pdf()
    if not pdf:
        raise RuntimeError("Private-schedule ACH certificate generation failed")
    return pdf
