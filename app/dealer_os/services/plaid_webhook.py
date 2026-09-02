"""What to do when Plaid tells us something about a connection.

Before this existed the only way to learn an Item had gone bad was to try to
use it, and with a 30-day refresh cadence that meant up to a month of silence
on a broken connection. Two of the events handled here can never be discovered
by polling at all:

- USER_PERMISSION_REVOKED, when the end user revokes us at my.plaid.com. Our
  privacy policy points users at exactly those controls, so continuing to hold
  a token and schedule refreshes after a revocation would contradict a published
  commitment. This is the event that makes a webhook receiver a compliance
  requirement rather than an operational nicety.
- PENDING_DISCONNECT / PENDING_EXPIRATION, which arrive seven days AHEAD of the
  connection dying. Acted on, they are a week's warning; ignored, the connection
  simply stops one day.

Every handler is idempotent. Plaid retries, and a webhook arriving twice must
not double-queue a sync or resurrect a revoked item.

Unknown webhook codes are logged and accepted, never rejected: returning an
error to Plaid earns a retry storm for an event we were never going to act on.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_profile import (
    ApplicationPlaidItem,
    ApplicationProfile,
    PlaidAssetReport,
)
from app.services import plaid_lifecycle, plaid_policy

from ..models import DealerBusiness, DealerPlaidItem

logger = logging.getLogger(__name__)

__all__ = ["handle"]


def _now() -> datetime:
    return datetime.now(UTC)


async def _item(
    db: AsyncSession, item_id: str, environment: str
) -> DealerPlaidItem | ApplicationPlaidItem | None:
    dealer_item = (
        await db.execute(
            select(DealerPlaidItem).where(
                DealerPlaidItem.item_id == item_id,
                DealerPlaidItem.environment == environment,
            )
        )
    ).scalar_one_or_none()
    if dealer_item is not None:
        return dealer_item
    return (
        await db.execute(
            select(ApplicationPlaidItem).where(
                ApplicationPlaidItem.item_id == item_id,
                ApplicationPlaidItem.environment == environment,
            )
        )
    ).scalar_one_or_none()


async def handle(db: AsyncSession, payload: dict[str, Any]) -> str:
    """Apply one verified webhook. Returns a short outcome for the log.

    The caller has already proven this came from Plaid; this function trusts
    the body and is responsible only for deciding what it means.
    """
    wtype = str(payload.get("webhook_type") or "")
    code = str(payload.get("webhook_code") or "")
    item_id = str(payload.get("item_id") or "")
    environment = str(payload.get("environment") or "sandbox")

    if wtype == "ASSETS":
        report_id = str(payload.get("asset_report_id") or "")
        if not report_id:
            return "ignored: no asset_report_id"
        report = (
            await db.execute(
                select(PlaidAssetReport).where(
                    PlaidAssetReport.asset_report_id == report_id,
                    PlaidAssetReport.environment == environment,
                )
            )
        ).scalar_one_or_none()
        if report is None:
            return "ignored: unknown asset report"
        if code == "PRODUCT_READY":
            if report.ingested_at is not None:
                return "asset report already ingested"
            if report.dealer_id is not None:
                owner = await db.get(DealerBusiness, report.dealer_id)
                enabled = bool(owner and owner.plaid_assets_enabled)
            else:
                profile = await db.get(ApplicationProfile, report.profile_id)
                if profile is None:
                    enabled = False
                else:
                    policy, _owner = await plaid_policy.for_profile(db, profile)
                    enabled = policy.assets_enabled
            if not enabled:
                await plaid_lifecycle.remove_asset_report(report, strict=False)
                return "ignored: assets disabled"
            report.status = "ready"
            report.error = None
            report.ready_at = _now()
            return "asset report ready"
        if code == "ERROR":
            error = payload.get("error") or {}
            report.status = "error"
            report.error = str(
                error.get("display_message")
                or error.get("error_message")
                or error.get("error_code")
                or "Asset Report generation failed"
            )
            return "asset report error recorded"
        return f"unhandled: {wtype}/{code}"

    if not item_id:
        return "ignored: no item_id"

    item = await _item(db, item_id, environment)
    if item is None:
        # Not ours, or already hard-deleted. Not an error — Plaid has no way to
        # know which items we still track.
        return "ignored: unknown item"
    item.last_webhook_at = _now()

    # Webhooks can arrive out of order. A refresh event that was already in
    # flight when the client revoked access must never do policy lookups or
    # schedule work for the revoked Item.
    if item.status == "revoked" and code != "USER_PERMISSION_REVOKED":
        return "ignored: item revoked"

    # ── The connection is gone, by the user's own choice ──
    if code == "USER_PERMISSION_REVOKED":
        item.status = "revoked"
        item.error = "The bank connection was revoked by the account holder."
        item.update_mode_reason = "user_permission_revoked"
        item.update_mode_account_selection = False
        item.auto_refresh = False
        item.next_refresh_at = None
        # The token cannot be used after revocation, and keeping bank
        # credentials we are forbidden to use is the wrong default.
        item.encrypted_access_token = None
        logger.info("plaid webhook: item %s revoked by user", item_id)
        return "revoked"

    if code == "USER_ACCOUNT_REVOKED":
        # This event applies to one account, not the entire Item. Keep the token
        # so the remaining authorized business accounts continue to work, then
        # reconcile the account/statement view on the next scheduler pass.
        account_id = str(payload.get("account_id") or "")
        item.error = "Access to one linked bank account was revoked by the account holder."
        item.update_mode_reason = "user_account_revoked"
        item.update_mode_account_selection = True
        if item.status != "revoked" and item.auto_refresh:
            item.next_refresh_at = _now()
        logger.info(
            "plaid webhook: account %s revoked on item %s",
            account_id or "unknown",
            item_id,
        )
        return "account revocation flagged"

    # ── The connection is broken and needs the user to repair it ──
    if wtype == "ITEM" and code == "ERROR":
        err = payload.get("error") or {}
        error_code = str(err.get("error_code") or "")
        item.status = "error"
        item.error = str(err.get("error_message") or err.get("error_code") or "Connection error")
        item.update_mode_reason = error_code.lower()[:32] or "item_error"
        item.update_mode_account_selection = False
        # Stop the scheduler retrying a connection only the user can fix.
        item.next_refresh_at = None
        return "error recorded"

    if code == "LOGIN_REPAIRED":
        # Resolved without us doing anything; clear the flag rather than leave
        # a stale warning in front of the user.
        item.status = "active"
        item.error = None
        item.update_mode_reason = None
        item.update_mode_account_selection = False
        if item.auto_refresh and item.next_refresh_at is None:
            item.next_refresh_at = _now()
        return "login repaired"

    # ── Seven days' warning. Surface it; do not sever anything yet ──
    if code in {"PENDING_DISCONNECT", "PENDING_EXPIRATION"}:
        when = payload.get("disconnect_time") or payload.get("consent_expiration_time")
        item.error = (
            "This bank connection needs to be renewed"
            + (f" before {when}." if when else ".")
        )
        item.update_mode_reason = code.lower()
        item.update_mode_account_selection = False
        return "pending disconnect flagged"

    if code == "NEW_ACCOUNTS_AVAILABLE":
        # Informational: we pull statements for the accounts already shared, and
        # adding accounts is the user's decision to make in update mode.
        item.update_mode_reason = "new_accounts_available"
        item.update_mode_account_selection = True
        return "new accounts available"

    if code == "WEBHOOK_UPDATE_ACKNOWLEDGED":
        return "webhook acknowledged"

    # ── Statements finished extracting ──
    if wtype == "STATEMENTS" and code == "STATEMENTS_REFRESH_COMPLETE":
        policy, owner = await plaid_policy.for_item(db, item)
        if owner is None or not policy.statements_enabled:
            item.statements_refresh_state = None
            item.statements_refresh_requested_at = None
            return "ignored: statements disabled"
        if str(payload.get("result") or "").upper() == "SUCCESS":
            # Due immediately — the scheduler tick picks it up and does the
            # actual pull, so the webhook stays fast and this stays idempotent.
            item.next_refresh_at = _now()
            item.error = None
            item.statements_refresh_state = "ready"
            return "sync queued"
        item.error = "Plaid could not extract statements from this connection."
        item.statements_refresh_state = "failed"
        item.next_refresh_at = _now() + timedelta(days=1)
        return "refresh failed"

    logger.info("plaid webhook: unhandled %s/%s for item %s", wtype, code, item_id)
    return f"unhandled: {wtype}/{code}"
