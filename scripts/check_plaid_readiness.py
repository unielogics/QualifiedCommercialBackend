"""Sanitized operational checks; creates Link tokens but no Items or money movement."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal
from app.dealer_os.services import plaid_client
from app.services import plaid_transfer


async def run() -> dict:
    settings = get_settings()
    result = {
        "checked_at": datetime.now(UTC).isoformat(),
        "evidence": {
            "configured": plaid_client.enabled(), "environment": plaid_client.environment(),
            "products": plaid_client.products(),
            "dedicated_encryption_key": bool(settings.provider_secrets_encryption_key),
            "webhook_configured": bool(plaid_client.webhook_url()),
        },
        "payments": {
            "configured": plaid_transfer.enabled(), "environment": plaid_transfer.environment(),
            "dispatch_enabled": settings.payments_enabled,
            "legal_approved": settings.payments_ach_legal_approved,
            "refunds_enabled": settings.payment_refunds_enabled,
            "private_schedules_enabled": settings.private_funding_payments_enabled,
        },
    }
    if plaid_client.enabled():
        for label, redirect in (
            ("team_link", plaid_client.redirect_uri()), ("room_link", plaid_client.room_redirect_uri()),
        ):
            try:
                if not redirect:
                    raise plaid_client.PlaidUnavailable("OAuth redirect missing", code="REDIRECT_MISSING")
                await plaid_client.create_link_token(
                    dealer_id="qc-readiness-probe", dealer_name="Qualified Commercial",
                    redirect_override=redirect,
                )
                result["evidence"][label] = "passed"
            except plaid_client.PlaidUnavailable as exc:
                result["evidence"][label] = exc.code or "failed"
    if plaid_transfer.enabled():
        try:
            await plaid_transfer.create_link_token(client_user_id="qc-payment-readiness-probe")
            result["payments"]["link"] = "passed"
        except plaid_transfer.PlaidTransferError as exc:
            result["payments"]["link"] = exc.code or "failed"
    async with SessionLocal() as db:
        for label, query in {
            "evidence_items": "SELECT environment, status, count(*) AS count FROM dos_plaid_items GROUP BY environment, status",
            "application_items": "SELECT environment, status, count(*) AS count FROM application_plaid_items GROUP BY environment, status",
            "reports": "SELECT environment, status, count(*) AS count, count(encrypted_asset_report_token) AS provider_tokens, count(provider_removed_at) AS provider_removed, count(provider_removal_error) AS cleanup_errors FROM plaid_asset_reports GROUP BY environment, status",
            "payment_sources": "SELECT plaid_environment, status, count(*) AS count, count(access_token_ciphertext) AS provider_tokens FROM payment_funding_sources GROUP BY plaid_environment, status",
            "transfers": "SELECT status, count(*) AS count FROM payment_transfers GROUP BY status",
        }.items():
            result[label] = [dict(row) for row in (await db.execute(text(query))).mappings().all()]
    return result


if __name__ == "__main__":
    print(json.dumps(asyncio.run(run()), indent=2))
