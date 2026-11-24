"""Staff permission workflows and recipient-operated confirmation pages."""
from datetime import UTC, datetime
from html import escape
from typing import Annotated, Literal

from fastapi import APIRouter, Form, HTTPException, Query, Response
from fastapi.responses import HTMLResponse

from app.routers.email_marketing import Admin, Db
from app.services.email_marketing import _utc_instant
from app.services.email_marketing_directory import requestable_contact
from app.services.marketing_permission_requests import (
    PermissionAction,
    activate,
    active_consent,
    invitation,
    list_requests,
    respond,
)

admin = APIRouter(prefix="/admin/email-marketing", tags=["email-marketing"])
public = APIRouter(prefix="/public/email-marketing", tags=["email-marketing"])
PRIVATE = {"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer",
           "X-Robots-Tag": "noindex, nofollow",
           "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'"}
COPY = {
    "en": {
        "title": "Email preferences", "question": "Would you like emails from Qualified Commercial?",
        "body": "Choose whether to receive educational financing guides and lending updates by email. "
                "Participation is optional. Your choice does not affect your application. You can unsubscribe at any time.",
        "agree": "Yes, send me financing guides and lending updates", "decline": "Decline marketing emails",
        "declined": "Your preference has been saved. You will not receive marketing emails.",
        "confirmed": "You are subscribed to financing guides and lending updates.",
        "unavailable": "This invitation is no longer available for subscribing.",
    },
    "es": {
        "title": "Preferencias de correo", "question": "¿Desea recibir correos de Qualified Commercial?",
        "body": "Elija si desea recibir guías educativas de financiamiento y novedades sobre préstamos por correo. "
                "Su participación es opcional. Su decisión no afecta su solicitud. Puede cancelar cuando quiera.",
        "agree": "Sí, quiero recibir guías de financiamiento y novedades sobre préstamos", "decline": "Rechazar correos de marketing",
        "declined": "Su preferencia se ha guardado. No recibirá correos de marketing.",
        "confirmed": "Su suscripción a las guías de financiamiento y novedades sobre préstamos está confirmada.",
        "unavailable": "Esta invitación ya no está disponible para suscribirse.",
    },
}


@admin.post("/permission-requests")
async def create_permission_requests(payload: PermissionAction, db: Db, user: Admin, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return await activate(db, user.id, payload)


@admin.get("/permission-requests")
async def permission_history(db: Db, user: Admin, response: Response, limit: Annotated[int, Query(ge=1, le=100)] = 25):
    response.headers["Cache-Control"] = "private, no-store"
    return await list_requests(db, limit)


def _page(locale, body, status_code=200):
    title = COPY[locale]["title"]
    return HTMLResponse(
        f"<!doctype html><html lang='{locale}'><head><meta charset='utf-8'><title>{title} | Qualified Commercial</title>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'><style>"
        "body{font:18px/1.55 system-ui;color:#193048;background:#f7f9fc}main{max-width:32rem;margin:8vh auto;padding:24px}"
        "button{font:inherit;padding:.6em 1em;margin:.4em 0;cursor:pointer;color:#123;border:1px solid #8394a5;"
        "border-radius:6px;background:white}button.primary{background:#173b53;color:white}h1{line-height:1.2}</style>"
        f"</head><body><main><p>Qualified Commercial</p>{body}</main></body></html>",
        status_code=status_code, headers=PRIVATE,
    )


@public.get("/permission/{token}", response_class=HTMLResponse)
@public.get("/permission/{token}/confirm", response_class=HTMLResponse)
@public.get("/permission/{token}/decline", response_class=HTMLResponse)
async def permission_landing(token: str, db: Db):
    row = await invitation(db, token)
    locale = "es" if row.locale == "es" else "en"
    copy = COPY[locale]
    if row.status == "declined":
        return _page(locale, f"<h1>{copy['declined']}</h1>")
    path = "/api/v1/public/email-marketing/permission/" + escape(token, quote=True)
    decline = f"<form method='post' action='{path}/decline'><button>{copy['decline']}</button></form>"
    if row.status == "confirmed" and await active_consent(db, row.email):
        return _page(locale, f"<h1>{copy['confirmed']}</h1>{decline}")
    if (row.status not in {"sent", "sending", "uncertain"} or not row.expires_at
        or _utc_instant(row.expires_at) <= datetime.now(UTC)
        or not await requestable_contact(db, row.email, ignore_prior_request=True)):
        return _page(locale, f"<h1>{copy['title']}</h1><p>{copy['unavailable']}</p>{decline}")
    return _page(locale, f"<h1>{copy['question']}</h1><p>{copy['body']}</p>"
                 f"<form method='post' action='{path}/confirm'><button class='primary' name='decision' value='confirm'>"
                 f"{copy['agree']}</button></form>{decline}")


async def _submit(db, token, agree):
    row = await invitation(db, token)
    locale = "es" if row.locale == "es" else "en"
    try:
        result = await respond(db, token, agree=agree)
    except HTTPException as exc:
        if exc.status_code != 410:
            raise
        return _page(locale, f"<h1>{COPY[locale]['unavailable']}</h1>", 410)
    return _page(locale, f"<h1>{COPY[locale][result]}</h1>")


@public.post("/permission/{token}/confirm", response_class=HTMLResponse)
async def confirm_permission(token: str, db: Db, decision: Annotated[Literal["confirm"], Form()]):
    return await _submit(db, token, True)


@public.post("/permission/{token}/decline", response_class=HTMLResponse)
async def decline_permission(token: str, db: Db):
    return await _submit(db, token, False)
