"""Database-wide marketing audiences are restricted to super administrators."""
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.deps import require_role
from app.enums import Role
from app.models.user import User
from app.services import email_marketing as marketing
from app.services import email_marketing_delivery as delivery
from app.services import email_marketing_directory as directory_service

admin = APIRouter(prefix="/admin/email-marketing", tags=["email-marketing"])
public = APIRouter(prefix="/public/email-marketing", tags=["email-marketing"])
Db = Annotated[AsyncSession, Depends(get_db)]
Admin = Annotated[User, Depends(require_role(Role.SUPER_ADMIN))]
Token = Annotated[str, Query(min_length=1, max_length=120)]


@admin.post("/directory")
async def recipient_directory(payload: directory_service.DirectoryRequest, db: Db, user: Admin, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return await directory_service.directory(db, payload)


@admin.get("/options")
async def options(db: Db, user: Admin):
    result = await marketing.filter_options(db)
    enabled, status = delivery.delivery_readiness()
    return {**result, "delivery_enabled": enabled, "delivery_status": status,
            "sequence": [{"day": day, "subject": template[0]}
                         for day, template in zip(delivery.DAYS, delivery.SEQUENCE["en"], strict=True)]}


@admin.post("/preview")
async def preview(payload: marketing.AudienceFilters, db: Db, user: Admin):
    count = await db.scalar(select(func.count()).select_from(marketing.eligible_ids(payload).subquery()))
    return {"eligible_count": count}


@admin.post("/campaigns")
async def create_campaign(payload: marketing.CampaignInput, db: Db, user: Admin):
    return await marketing.create_campaign(db, user.id, payload)


@admin.post("/campaigns/preview")
async def preview_campaign(payload: marketing.CampaignPreview, db: Db, user: Admin, response: Response):
    response.headers["Cache-Control"] = "private, no-store"
    return await marketing.preview_campaign(db, payload)


@admin.get("/campaigns")
async def list_campaigns(db: Db, user: Admin):
    return await marketing.list_campaigns(db)


def unsubscribe_page(body):
    return HTMLResponse(
        "<!doctype html><html lang='en'><meta charset='utf-8'><meta name='robots' content='noindex'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'><title>Qualified Commercial</title>"
        "<main style='max-width:520px;margin:12vh auto;padding:24px;font:18px system-ui'>"
        f"<p>Qualified Commercial</p>{body}</main></html>",
        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"},
    )


@public.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_confirmation(token: Token):
    marketing.verify_token(token)
    return unsubscribe_page("<h1>Stop marketing emails?</h1><p>¿Desea dejar de recibir correos de marketing?</p>"
                            "<form method='post'><button>Unsubscribe / Cancelar suscripción</button></form>")


@public.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(token: Token, db: Db):
    await marketing.unsubscribe(db, token)
    return unsubscribe_page("<h1>You are unsubscribed.</h1><p>Se canceló su suscripción.</p>")
