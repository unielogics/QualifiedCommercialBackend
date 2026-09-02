"""Per-file Plaid product policy and Item authorization reconciliation.

The environment variable says which products this deployment may call. The
DealerBusiness/ApplicationProfile row says which products this file selected,
and /item/get says which selected products each bank has actually authorized.
Keeping those three states separate prevents both accidental billing and
incorrectly treating a file preference as client consent.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.dealer_os.models import DealerBusiness, DealerPlaidItem
from app.dealer_os.services import plaid_client
from app.models.application_profile import (
    ApplicationBankConsent,
    ApplicationPlaidItem,
    ApplicationProfile,
)

PlaidItem = DealerPlaidItem | ApplicationPlaidItem
PRODUCT_ORDER = ("assets", "statements")


class InvalidPlaidPolicy(ValueError):
    pass


class PlaidProductUnavailable(RuntimeError):
    def __init__(self, unavailable: Iterable[str], available: Iterable[str]):
        self.unavailable = sorted(set(unavailable))
        self.available = sorted(set(available))
        super().__init__("Selected Plaid product is unavailable in this deployment")


def _clean_products(values: Iterable[object] | None) -> list[str]:
    found = {str(value).strip().lower() for value in (values or [])}
    return [product for product in PRODUCT_ORDER if product in found]


@dataclass(frozen=True)
class PlaidProductPolicy:
    assets_enabled: bool
    statements_enabled: bool

    @property
    def selected_products(self) -> list[str]:
        return [
            product
            for product, selected in (
                ("assets", self.assets_enabled),
                ("statements", self.statements_enabled),
            )
            if selected
        ]

    @property
    def available_products(self) -> list[str]:
        return plaid_client.products()

    def validate(self) -> None:
        if not self.selected_products:
            raise InvalidPlaidPolicy("At least one Plaid product must remain enabled")
        unavailable = set(self.selected_products) - set(self.available_products)
        if unavailable:
            raise PlaidProductUnavailable(unavailable, self.available_products)


def from_owner(owner: DealerBusiness | ApplicationProfile) -> PlaidProductPolicy:
    return PlaidProductPolicy(
        assets_enabled=bool(owner.plaid_assets_enabled),
        statements_enabled=bool(owner.plaid_statements_enabled),
    )


async def for_profile(
    db: AsyncSession, profile: ApplicationProfile
) -> tuple[PlaidProductPolicy, DealerBusiness | ApplicationProfile]:
    """Resolve the authoritative policy owner for a Funding profile."""
    if profile.dealer_id is not None:
        dealer = await db.get(DealerBusiness, profile.dealer_id)
        if dealer is None:
            raise InvalidPlaidPolicy("Linked Field Desk file no longer exists")
        return from_owner(dealer), dealer
    return from_owner(profile), profile


async def for_item(
    db: AsyncSession, item: PlaidItem
) -> tuple[PlaidProductPolicy, DealerBusiness | ApplicationProfile | None]:
    if isinstance(item, DealerPlaidItem):
        owner = await db.get(DealerBusiness, item.dealer_id)
        return (from_owner(owner), owner) if owner else (PlaidProductPolicy(True, False), None)
    owner = await db.get(ApplicationProfile, item.profile_id)
    if owner is None:
        return PlaidProductPolicy(True, False), None
    return await for_profile(db, owner)


async def has_required_consent(
    db: AsyncSession,
    item: PlaidItem,
    policy: PlaidProductPolicy,
) -> bool:
    if isinstance(item, DealerPlaidItem):
        from app.dealer_os.services import bank_consent

        return await bank_consent.has_consent(
            db, item.dealer_id, policy.selected_products
        )
    row = (
        await db.execute(
            select(ApplicationBankConsent)
            .where(
                ApplicationBankConsent.profile_id == item.profile_id,
                ApplicationBankConsent.granted.is_(True),
                ApplicationBankConsent.revoked_at.is_(None),
            )
            .order_by(ApplicationBankConsent.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None:
        return False
    required = set(policy.selected_products)
    version_allowed = row.disclosure_version == "2026-09-02-products-v1" or (
        row.disclosure_version == "2026-08-24-assets-v1" and required <= {"assets"}
    )
    return version_allowed and required <= set(row.product_scope or [])


def item_products(item: PlaidItem) -> list[str]:
    return _clean_products(getattr(item, "plaid_products", None))


def unavailable_products(item: PlaidItem) -> list[str]:
    return _clean_products(getattr(item, "plaid_unavailable_products", None))


def pending_products(item: PlaidItem, policy: PlaidProductPolicy) -> list[str]:
    initialized = set(item_products(item))
    unavailable = set(unavailable_products(item))
    return [
        value
        for value in policy.selected_products
        if value not in initialized and value not in unavailable
    ]


def mark_optional_statements_unavailable(
    item: PlaidItem, policy: PlaidProductPolicy
) -> None:
    """Record the PDF-upload fallback when this institution omits Statements.

    Combined Link treats Statements as required-if-supported, while a
    Statements-only Link requests it directly. In either flow, a completed
    Link session that still omits Statements means retrying update mode would
    loop the client through an authorization the institution cannot provide.
    """
    if not policy.statements_enabled:
        return
    unavailable = set(unavailable_products(item))
    if "statements" not in item_products(item):
        unavailable.add("statements")
        item.update_mode_reason = "statements_unavailable"
    else:
        unavailable.discard("statements")
    item.plaid_unavailable_products = _clean_products(unavailable)


def authorization_state(item: PlaidItem, policy: PlaidProductPolicy) -> str:
    if item.status == "removed":
        return "removed"
    if item.plaid_products_checked_at is None:
        return "checking"
    if pending_products(item, policy):
        return "client_authorization_required"
    if set(unavailable_products(item)) & set(policy.selected_products):
        return "fallback_required"
    if item.status == "error":
        return "attention_required"
    return "authorized"


def update_reason(missing: Iterable[str]) -> str | None:
    values = _clean_products(missing)
    if values == ["assets"]:
        return "add_assets"
    if values == ["statements"]:
        return "add_statements"
    if values:
        return "add_products"
    return None


async def reconcile_item(db: AsyncSession, item: PlaidItem) -> dict:
    """Persist /item/get's authoritative product and error state."""
    token = plaid_client.decrypt_token(item.encrypted_access_token)
    if not token:
        item.status = "error"
        item.error = "Stored bank credentials are unavailable; reconnect this bank"
        item.update_mode_reason = "item_login_required"
        await db.flush()
        return {}
    response = await plaid_client.item_get(token)
    state = response.get("item") or {}
    item.plaid_products = _clean_products(state.get("products"))
    item.plaid_consented_products = _clean_products(state.get("consented_products"))
    item.plaid_billed_products = _clean_products(state.get("billed_products"))
    item.plaid_unavailable_products = [
        product
        for product in unavailable_products(item)
        if product not in item.plaid_products
    ]
    item.plaid_products_checked_at = datetime.now(UTC)

    error = state.get("error") or response.get("error")
    if error:
        item.status = "error"
        item.error = str(
            error.get("display_message")
            or error.get("error_message")
            or "Bank connection needs attention"
        )[:500]
        item.update_mode_reason = str(error.get("error_code") or "item_error").lower()[:32]
    else:
        policy, _owner = await for_item(db, item)
        missing = pending_products(item, policy)
        item.status = "active"
        item.error = None
        item.update_mode_reason = update_reason(missing)
        item.update_mode_account_selection = False
    await db.flush()
    return response


async def copy_latest_policy_on_handoff(
    dealer: DealerBusiness, profile: ApplicationProfile
) -> None:
    """Preserve the newest explicit policy when linking the two file models.

    Dealer becomes authoritative after handoff. A standalone Funding policy is
    copied only when it was explicitly changed more recently than the dealer's.
    """
    profile_time = profile.plaid_policy_updated_at
    dealer_time = dealer.plaid_policy_updated_at
    if profile_time is not None and (dealer_time is None or profile_time > dealer_time):
        dealer.plaid_assets_enabled = profile.plaid_assets_enabled
        dealer.plaid_statements_enabled = profile.plaid_statements_enabled
        dealer.plaid_policy_updated_at = profile_time
        dealer.plaid_policy_updated_by_user_id = profile.plaid_policy_updated_by_user_id
    else:
        profile.plaid_assets_enabled = dealer.plaid_assets_enabled
        profile.plaid_statements_enabled = dealer.plaid_statements_enabled
        profile.plaid_policy_updated_at = dealer_time
        profile.plaid_policy_updated_by_user_id = dealer.plaid_policy_updated_by_user_id
