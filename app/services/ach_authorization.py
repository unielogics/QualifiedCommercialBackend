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

from app.models.payments import AchMandate, PaymentFundingSource

AUTHORIZATION_TEXT_VERSION = "ach-fee-2026-10-05-v1"
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
    terms = html.escape(authorization_text(
        ach_class=mandate.ach_class,
        maximum_amount_cents=mandate.authorized_amount_cents,
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
