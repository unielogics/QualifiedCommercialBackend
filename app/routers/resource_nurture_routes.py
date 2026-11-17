"""Resource email unsubscribe: GET confirms; POST performs one-click withdrawal."""
from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.services.resource_nurture import unsubscribe_id, unsubscribe_resource

router = APIRouter(prefix="/public/resources", tags=["public-resources"])


@router.get("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe_confirmation(token: str):
    unsubscribe_id(token)
    return HTMLResponse("<!doctype html><html lang='en'><meta name='robots' content='noindex'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                        "<title>Unsubscribe | Qualified Commercial</title><body style='font-family:Arial;max-width:520px;margin:80px auto;padding:24px'>"
                        "<h1>Stop educational funding emails?</h1><p>Cancelar correos educativos de financiamiento.</p>"
                        "<form method='post'><button type='submit'>Unsubscribe / Cancelar suscripción</button></form></body></html>",
                        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


@router.post("/unsubscribe", response_class=HTMLResponse)
async def unsubscribe(token: str, db: Annotated[AsyncSession, Depends(get_db)]):
    await unsubscribe_resource(db, token)
    return HTMLResponse("<html lang='en'><meta name='robots' content='noindex'><h1>You are unsubscribed.</h1>"
                        "<p>Se canceló su suscripción a correos educativos.</p></html>", headers={"Cache-Control": "no-store"})
