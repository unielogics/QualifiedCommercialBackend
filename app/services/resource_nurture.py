"""Four consented resource emails, independently claimed before provider handoff."""
from __future__ import annotations

import hashlib
import hmac
import html
from datetime import UTC, datetime, timedelta
from urllib.parse import quote
from uuid import UUID, uuid5

from fastapi import HTTPException
from sqlalchemy import select, update

from app.config import get_settings
from app.models.activity import Activity
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.models.resource_assessment import ResourceAssessment
from app.services import resource_assessments
from app.services.email.ses_client import ses_configured
from app.services.messaging import outbox
from app.services.prospect_outreach import is_suppressed, set_suppression

OFFSETS = (0, 1, 3, 7)
GOAL_LABELS = {
    "en": {"mca_refinance": "MCA refinancing", "merchant_cash_advance": "RBF/MCA funding", "term_loan": "a business term loan"},
    "es": {"mca_refinance": "refinanciamiento MCA", "merchant_cash_advance": "financiamiento RBF/MCA", "term_loan": "un préstamo comercial a plazo"},
}


def mint_unsubscribe_token(assessment: ResourceAssessment) -> str:
    identifier = assessment.id.hex
    signature = hmac.new(resource_assessments._secret(), ("unsubscribe:" + identifier).encode(), hashlib.sha256).hexdigest()
    return identifier + "." + signature


def unsubscribe_id(token: str) -> UUID:
    try:
        identifier, signature = token.split(".", 1)
        result = UUID(hex=identifier)
    except (ValueError, AttributeError):
        raise HTTPException(404, "This unsubscribe link is invalid.") from None
    expected = hmac.new(resource_assessments._secret(), ("unsubscribe:" + result.hex).encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(404, "This unsubscribe link is invalid.")
    return result


async def unsubscribe_resource(db, token: str) -> None:
    row = await db.get(ResourceAssessment, unsubscribe_id(token))
    if row is None:
        raise HTTPException(404, "This unsubscribe link is invalid.")
    email = row.contact["email"]
    await set_suppression(db, email=email, reason="unsubscribe", source="resource_nurture",
                          details={"assessment_id": str(row.id)})
    # Consent withdrawal applies to all resource enrollments at this address,
    # including captures attached to a different CRM record.
    await db.execute(update(ResourceAssessment).where(ResourceAssessment.contact["email"].astext == email)
                     .values(email_marketing_consent=False))
    await db.commit()


def email_content(row: ResourceAssessment, step: int) -> tuple[str, str]:
    es = row.locale == "es"
    label = GOAL_LABELS["es" if es else "en"][row.goal]
    if es:
        subjects = ["Sus escenarios preliminares de financiamiento", "Cómo interpretar su comparación de pagos",
                    "Qué preparar para su solicitud", "Continúe su solicitud de financiamiento con IA"]
        messages = [
            f"Gracias por explorar {label}. Su resultado reúne sus cifras, supuestos y próximos pasos. Puede revisarlo y ajustar los escenarios con el enlace de abajo.",
            "Compare el pago periódico, el plazo, el pago total y los cargos. Un factor MCA no es una APR, y un pago menor puede aumentar el costo total. Sus cifras siguen siendo declaradas, no verificadas.",
            "Prepare sus estados bancarios completos, la declaración comercial correspondiente y un calendario de deudas con liquidaciones actuales. La estimación usa tres meses; la solicitud RBF/MCA pide seis meses completos. Los documentos finales dependen del programa.",
            f"Si desea continuar con {label}, abra su resultado y seleccione iniciar la solicitud con IA. Se trasladarán sus respuestas para que pueda revisarlas y completar los datos que faltan. No necesita volver a empezar.",
        ]
        disclosure = "Estos escenarios son ilustrativos. No constituyen una oferta, aprobación ni compromiso de crédito. La solicitud y la evaluación del prestamista determinan los términos disponibles."
    else:
        subjects = ["Your preliminary business funding scenarios", "How to read your payment comparison",
                    "What to prepare for your application", "Continue your AI funding application"]
        messages = [
            f"Thanks for exploring {label}. Your result brings together your numbers, assumptions and next steps. Open the link below to revisit your result and adjust the scenarios.",
            "Compare the periodic payment, term, total repayment and fees. An MCA factor is not APR, and a lower payment can increase total cost. Your entered figures remain self-reported, not verified.",
            "Gather complete business bank statements, the relevant business tax return and a debt schedule with current payoffs. The estimate uses three months; the RBF/MCA application requests six complete months. Final documents depend on the program.",
            f"If you want to continue with {label}, open your result and choose to start the AI application. Your answers will carry over for you to review and complete. You do not need to start again.",
        ]
        disclosure = "These are illustrative scenarios, not an offer, approval or credit commitment. The application and lender review determine available terms."
    payment_guides = {
        "en": {
            "mca_refinance": "Compare the MCA withdrawals you plan to replace with the full replacement payment, fees and total repayment. Relief is not available unless the planned payoffs are covered. A lower payment can extend the debt and increase total cost.",
            "merchant_cash_advance": "For RBF/MCA, advance amount multiplied by the factor gives total factor-based repayment before additional fees. A factor is not APR. Duration and debit frequency must be known before relying on a payment schedule.",
            "term_loan": "Compare monthly payments, total repayment, fees and the payments on debt that would remain. Your calculator's coverage buffer and rates are editable assumptions, not lender requirements or an approval.",
        },
        "es": {
            "mca_refinance": "Compare los débitos MCA a reemplazar con el nuevo pago completo, los cargos y el pago total. No hay alivio si no se cubren las liquidaciones. Un pago menor puede extender la deuda y aumentar el costo total.",
            "merchant_cash_advance": "En RBF/MCA, monto por factor indica el pago total antes de cargos adicionales. El factor no es APR. Debe conocer duración y frecuencia de débito antes de confiar en un calendario de pagos.",
            "term_loan": "Compare pagos mensuales, pago total, cargos y las deudas que permanecerán. El margen de cobertura y las tasas de la calculadora son supuestos editables, no requisitos del prestamista ni aprobación.",
        },
    }
    document_guides = {
        "en": {
            "mca_refinance": "Prepare current payoff letters for every MCA, six complete months of every operating account, the business tax return and a current debt schedule. Open your secure application for the complete checklist and additional requests.",
            "merchant_cash_advance": "The RBF/MCA application requests six complete months of business bank statements, a complete business debt schedule and the prior-calendar-year business tax return. Your calculator used three months for orientation; document review needs the full requested period.",
            "term_loan": "Prepare the most recently filed business tax return, complete bank statements for every operating account and the current business debt schedule. Your secure application lists the periods and any additional financials required by the selected term-loan program.",
        },
        "es": {
            "mca_refinance": "Prepare liquidaciones vigentes de cada MCA, seis meses completos de cada cuenta operativa, la declaración comercial y el calendario actual de deudas. Abra la solicitud segura para ver la lista completa y solicitudes adicionales.",
            "merchant_cash_advance": "La solicitud RBF/MCA pide seis meses completos de estados bancarios comerciales, un calendario completo de deudas y la declaración del año calendario anterior. La calculadora usó tres meses orientativos; la revisión necesita todo el período solicitado.",
            "term_loan": "Prepare la última declaración comercial presentada, estados completos de cada cuenta operativa y el calendario actual de deudas. Su solicitud segura indica los períodos y estados adicionales requeridos por el programa a plazo seleccionado.",
        },
    }
    language = "es" if es else "en"
    if step == 1:
        messages[step] = payment_guides[language][row.goal]
    elif step == 2:
        messages[step] = document_guides[language][row.goal]
    return subjects[step], messages[step] + "\n\n" + disclosure


def compose_email(row: ResourceAssessment, step: int) -> outbox.Draft:
    settings = get_settings()
    token = resource_assessments.token_for(row.id)
    unsub_token = mint_unsubscribe_token(row)
    # Marketing origin is deliberately separate from the authenticated app.
    origin = getattr(settings, "resource_marketing_url", "https://qualifiedcommercial.com").rstrip("/")
    path = "/es/business-funding-check" if row.locale == "es" else "/business-funding-check"
    result_url = f"{origin}{path}#resource={quote(token, safe='')}"
    unsubscribe_url = f"{settings.public_api_url.rstrip('/')}/api/v1/public/resources/unsubscribe?token={quote(unsub_token, safe='')}"
    address = " ".join(settings.prospect_mailing_address.split())
    if not address:
        raise ValueError("A mailing address is required for resource email.")
    subject, message = email_content(row, step)
    action = "Ver mi resultado y continuar" if row.locale == "es" else "View my result and continue"
    unsubscribe = "Cancelar suscripción" if row.locale == "es" else "Unsubscribe"
    footer = f"Qualified Commercial · {address}\n{unsubscribe}: {unsubscribe_url}"
    text = f"{message}\n\n{action}: {result_url}\n\n{footer}"
    body = "<div style='font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#16283c'><h2>Qualified Commercial</h2>"
    body += "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in message.split("\n\n"))
    body += f"<p><a href='{html.escape(result_url, quote=True)}'>{html.escape(action)}</a></p><hr><p>{html.escape(address)}<br>"
    body += f"<a href='{html.escape(unsubscribe_url, quote=True)}'>{html.escape(unsubscribe)}</a></p></div>"
    return outbox.Draft(to=row.contact["email"], subject=subject, body_text=text, body_html=body,
                        from_name="Qualified Commercial", artifact_locale=row.locale,
                        headers={"List-Unsubscribe": f"<{unsubscribe_url}>", "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"},
                        secrets=(token, unsub_token))


def eligible(row: ResourceAssessment, now: datetime) -> bool:
    return bool(row.email_marketing_consent and row.application_started_at is None and row.token_expires_at > now)


def due_at(row: ResourceAssessment, step: int, previous: Activity | None) -> datetime:
    scheduled = row.created_at + timedelta(days=OFFSETS[step])
    if previous is not None and step:
        # Do not burst all missed emails after provider downtime.
        scheduled = max(scheduled, previous.occurred_at + timedelta(days=OFFSETS[step] - OFFSETS[step - 1]))
    return scheduled


async def dispatch_one(db, row: ResourceAssessment, *, now: datetime | None = None) -> bool:
    now = now or datetime.now(UTC)
    if not eligible(row, now) or await is_suppressed(db, row.contact["email"]):
        row.nurture_next_at = None
        return False
    started = (await db.execute(select(PublicUnderwritingIntake.id).where(
        PublicUnderwritingIntake.client_id == row.client_id,
        PublicUnderwritingIntake.created_at >= row.created_at).limit(1))).scalar_one_or_none()
    if started is not None:
        row.application_started_at = now
        row.nurture_next_at = None
        await db.flush()
        return False
    previous = None
    for step in range(4):
        claim_id = uuid5(row.id, f"resource.nurture:{step}")
        prior = await db.get(Activity, claim_id)
        if prior is not None:
            previous = prior
            continue
        if due_at(row, step, previous) > now:
            row.nurture_next_at = due_at(row, step, previous)
            return False
        draft = compose_email(row, step)
        claim = Activity(id=claim_id, client_id=row.client_id, actor_label="system", kind="resource.nurture_step",
                         summary=f"Resource email step {step + 1} claimed",
                         payload={"assessment_id": str(row.id), "step": step, "state": "claimed"}, occurred_at=now)
        db.add(claim)
        await db.flush()
        # The caller holds a FOR UPDATE lock on this assessment. The unique
        # Activity id survives the outbox's commit before external transport.
        outcome = await outbox.deliver_email(db, draft, context="resource_nurture",
            template_key=f"resource_{row.goal}_{step}", subject=outbox.Subject(client_id=row.client_id), durable_handoff=True)
        claim.payload = {**claim.payload, "state": "sent" if outcome.ok else "uncertain",
                         "message_send_id": str(outcome.row.id) if outcome.row else None}
        row.nurture_next_at = now + timedelta(days=OFFSETS[step + 1] - OFFSETS[step]) if step < 3 else None
        await db.commit()
        return outcome.ok
    row.nurture_next_at = None
    return False


async def drain_pending() -> dict:
    settings = get_settings()
    if not getattr(settings, "resource_nurture_enabled", False) or not ses_configured():
        return {"checked": 0, "sent": 0}
    from app.db import SessionLocal

    checked = sent = 0
    async with SessionLocal() as db:
        rows = (await db.execute(select(ResourceAssessment).where(
            ResourceAssessment.email_marketing_consent.is_(True),
            ResourceAssessment.application_started_at.is_(None),
            ResourceAssessment.nurture_next_at <= datetime.now(UTC),
            ResourceAssessment.token_expires_at > datetime.now(UTC))
            .order_by(ResourceAssessment.nurture_next_at.asc()).limit(100))).scalars().all()
        identifiers = [row.id for row in rows]
    for identifier in identifiers:
        async with SessionLocal() as db:
            row = (await db.execute(select(ResourceAssessment).where(ResourceAssessment.id == identifier)
                                   .with_for_update(skip_locked=True))).scalar_one_or_none()
            if row is None:
                continue
            checked += 1
            try:
                sent += int(await dispatch_one(db, row))
                await db.commit()
            except Exception:
                await db.rollback()
                import logging
                logging.getLogger(__name__).exception("Resource nurture attempt failed assessment=%s", identifier)
    return {"checked": checked, "sent": sent}
