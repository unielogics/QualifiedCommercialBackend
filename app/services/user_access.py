"""Product access and immutable access-history helpers.

``User.role`` remains the staff permission boundary. External client product
entry is controlled by explicit rows in ``user_product_access`` so one Clerk
identity can enter Funding, Audit, or both without broadening record scope.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.enums import ProductAccountType, Role
from app.models.user import User
from app.models.user_access import UserAccessEvent, UserProductAccess

_AUDIT_STAFF_ROLES = {Role.SUPER_ADMIN, Role.LOAN_EXEC}
_FUNDING_STAFF_ROLES = {
    Role.SUPER_ADMIN,
    Role.LOAN_EXEC,
    Role.REGIONAL_MANAGER,
    Role.BROKER,
}
_EXTERNAL_PRODUCT_ROLES = {Role.CLIENT, Role.DEALER}

# ---------------------------------------------------------------------------
# Consoles: which sign-ins a login may use.
#
# A console is a sign-in — Funding (app.), Field Desk (rep.), Audit (audit.).
# The role says what a person may do inside one; the console list says which
# ones they may open. The list is inherited from the role, and a super admin
# may grant one more from the Team table (``users.account_access_types``).
# Nothing here widens what a role may do inside a console: the only grant that
# changes a server-side tier is the one it always changed — a broker with
# Field Desk is rep-tier (owner-scoped) in the dealer-OS backend, and because
# that backend serves both rep. and audit., Field Desk implies Audit for them
# the way it does for a field rep.
# ---------------------------------------------------------------------------

CONSOLE_KEYS: frozenset[str] = frozenset({"funding", "field_desk", "audit"})
CONSOLE_ORDER: tuple[str, ...] = ("funding", "field_desk", "audit")
CONSOLE_LABELS: dict[str, str] = {"funding": "Funding", "field_desk": "Field Desk", "audit": "Audit"}
# The roles that live in the operator consoles at all.
OPERATOR_CONSOLE_ROLES: frozenset[Role] = frozenset(
    {Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.REGIONAL_MANAGER, Role.BROKER, Role.FIELD_REP}
)
_ALL_CONSOLES = frozenset(CONSOLE_KEYS)
_INHERITED_CONSOLES: dict[Role, frozenset[str]] = {
    Role.SUPER_ADMIN: _ALL_CONSOLES,
    Role.LOAN_EXEC: _ALL_CONSOLES,
    Role.REGIONAL_MANAGER: frozenset({"funding"}),
    Role.BROKER: frozenset({"funding"}),
    # A field rep already reaches audit. (assigned_product_values below); the
    # Team table used to pretend otherwise.
    Role.FIELD_REP: frozenset({"field_desk", "audit"}),
}
# What a super admin may toggle per role, beyond what the role inherits.
# Audit is never a standalone grant for a broker or a regional manager: it
# arrives with Field Desk or not at all. A regional manager's Field Desk is
# offered but is not rep-tier (see _REP_TIER_GRANT_ROLES) — one constant if
# the owner wants it.
_STANDALONE_GRANTS: dict[Role, frozenset[str]] = {
    Role.BROKER: frozenset({"field_desk"}),
    Role.REGIONAL_MANAGER: frozenset({"field_desk"}),
    Role.FIELD_REP: frozenset({"funding"}),
}
CONSOLE_GRANT_ROLES: frozenset[Role] = frozenset(_STANDALONE_GRANTS)
# Exactly today's rule: a broker with Field Desk is rep-tier.
_REP_TIER_GRANT_ROLES: frozenset[Role] = frozenset({Role.BROKER})


def inherited_console_keys(role: Role | str | None) -> set[str]:
    return set(_INHERITED_CONSOLES.get(role, frozenset()))  # type: ignore[arg-type]


def grantable_console_keys(role: Role | str | None) -> set[str]:
    return set(_STANDALONE_GRANTS.get(role, frozenset()))  # type: ignore[arg-type]


def allowed_console_keys(role: Role | str | None) -> set[str]:
    """Everything a stored grant list may contain for this role: the inherited
    keys (the Team table echoes them back) plus the standalone grants."""
    return inherited_console_keys(role) | grantable_console_keys(role)


def granted_console_keys(user: Any) -> set[str]:
    """The stored grants that mean something for this role. A stale value on a
    dealer or partner row, or an inherited key echoed back, opens nothing."""
    stored = set(getattr(user, "account_access_types", None) or [])
    return stored & grantable_console_keys(getattr(user, "role", None))


def has_rep_tier_grant(user: Any) -> bool:
    """A broker with Field Desk is rep-tier in the dealer-OS backend — the one
    grant that has always changed a server-side tier. Lives here for import
    layering (dealer_os.deps imports this module, never the reverse)."""
    return getattr(user, "role", None) in _REP_TIER_GRANT_ROLES and "field_desk" in granted_console_keys(user)


def console_keys(user: Any) -> list[str]:
    """The consoles this role plus its grants may open, sorted."""
    keys = inherited_console_keys(getattr(user, "role", None)) | granted_console_keys(user)
    if has_rep_tier_grant(user):
        keys.add("audit")
    return sorted(keys)


def enterable_console_keys(user: Any) -> set[str]:
    """The consoles this login may sign in to right now: nothing when
    suspended or deleted; operators from their role and grants; external
    clients from their product entitlements; every other role lives on app."""
    if getattr(user, "deleted_at", None) is not None:
        return set()
    if (getattr(user, "account_status", None) or "active") != "active":
        return set()
    role = getattr(user, "role", None)
    if role in OPERATOR_CONSOLE_ROLES:
        return set(console_keys(user))
    if role in _EXTERNAL_PRODUCT_ROLES:
        return set(enabled_product_values(user)) & CONSOLE_KEYS
    return {"funding"}


def console_links(user: Any) -> list[dict[str, str]]:
    """What the switcher renders: the consoles this login may enter, in fixed
    order, with the URLs from Settings. No frontend carries a host literal."""
    settings = get_settings()
    urls = {"funding": settings.frontend_app_url, "field_desk": settings.rep_app_url, "audit": settings.audit_app_url}
    allowed = enterable_console_keys(user)
    return [{"key": k, "label": CONSOLE_LABELS[k], "url": urls[k].rstrip("/")} for k in CONSOLE_ORDER if k in allowed]


def console_state(user: Any) -> dict[str, Any]:
    """The Team-row analogue of access_state, for the access-event trail."""
    role = getattr(user, "role", None)
    return {
        "account_status": getattr(user, "account_status", None) or "active",
        "account_types": console_keys(user),
        "role": role.value if isinstance(role, Role) else str(role),
    }


def _product_value(product: ProductAccountType | str) -> str:
    return product.value if isinstance(product, ProductAccountType) else str(product)


def assigned_product_values(user: User) -> set[str]:
    """Return assigned products without performing database I/O.

    Staff product entry remains role-controlled. External users require an
    enabled entitlement row. The legacy role fallback is used only when the
    relationship is unavailable (older tests/processes during rollout), not
    when an explicitly loaded empty/disabled entitlement set is present.
    """

    if getattr(user, "deleted_at", None) is not None:
        return set()

    products: set[str] = set()
    if user.role in _FUNDING_STAFF_ROLES:
        products.add(ProductAccountType.FUNDING.value)
    if user.role in _AUDIT_STAFF_ROLES or user.role == Role.FIELD_REP:
        products.add(ProductAccountType.AUDIT.value)
    # A field rep granted the Funding console can sign in to app.; the thin
    # rep nav there is what "Funding entry with a rep's permissions" means.
    if user.role == Role.FIELD_REP and "funding" in granted_console_keys(user):
        products.add(ProductAccountType.FUNDING.value)

    accesses = getattr(user, "product_accesses", None)
    if accesses is not None:
        products.update(row.product for row in accesses if row.enabled)
    elif user.role == Role.CLIENT:
        products.add(ProductAccountType.FUNDING.value)
    elif user.role == Role.DEALER:
        products.add(ProductAccountType.AUDIT.value)
    return products


def enabled_product_values(user: User) -> set[str]:
    """Return products that may be entered in the current account state."""

    if getattr(user, "account_status", "active") != "active":
        return set()
    return assigned_product_values(user)


def account_types(user: User) -> list[ProductAccountType]:
    assigned = assigned_product_values(user)
    return [product for product in ProductAccountType if product.value in assigned]


def has_product_access(user: User, product: ProductAccountType | str) -> bool:
    return _product_value(product) in enabled_product_values(user)


def is_audit_client(user: User) -> bool:
    return user.role in _EXTERNAL_PRODUCT_ROLES and has_product_access(
        user, ProductAccountType.AUDIT
    )


def is_funding_client(user: User) -> bool:
    return user.role in _EXTERNAL_PRODUCT_ROLES and has_product_access(
        user, ProductAccountType.FUNDING
    )


async def ensure_legacy_product_access(db: AsyncSession, user: User) -> bool:
    """Create the compatibility entitlement for a legacy external role.

    Returns True when a row was created. This is idempotent and intentionally
    does not infer products from names or email addresses.
    """

    product: ProductAccountType | None = None
    if user.role == Role.CLIENT:
        product = ProductAccountType.FUNDING
    elif user.role == Role.DEALER:
        product = ProductAccountType.AUDIT
    if product is None:
        return False

    row = (
        await db.execute(
            select(UserProductAccess).where(
                UserProductAccess.user_id == user.id,
                UserProductAccess.product == product.value,
            )
        )
    ).scalar_one_or_none()
    if row is not None:
        return False
    loaded_accesses = getattr(user, "product_accesses", None)
    before_products = sorted(
        access.product for access in (loaded_accesses or []) if access.enabled
    )
    row = UserProductAccess(user_id=user.id, product=product.value, enabled=True)
    db.add(row)
    await db.flush()
    if "product_accesses" in user.__dict__:
        user.product_accesses.append(row)
    db.add(
        UserAccessEvent(
            user_id=user.id,
            actor_user_id=None,
            action="client_access.initialized",
            reason="Initialized the legacy client product entitlement",
            before_state={
                "account_status": getattr(user, "account_status", "active"),
                "account_types": before_products,
                "role": user.role.value,
            },
            after_state={
                "account_status": getattr(user, "account_status", "active"),
                "account_types": sorted({*before_products, product.value}),
                "role": user.role.value,
            },
            request_metadata={"source": "authenticated_compatibility"},
        )
    )
    return True


async def set_product_access(
    db: AsyncSession,
    *,
    user: User,
    product: ProductAccountType | str,
    enabled: bool,
    actor_user_id: UUID | None,
    reason: str | None,
) -> UserProductAccess:
    value = _product_value(product)
    row = (
        await db.execute(
            select(UserProductAccess).where(
                UserProductAccess.user_id == user.id,
                UserProductAccess.product == value,
            )
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        row = UserProductAccess(
            user_id=user.id,
            product=value,
            enabled=enabled,
            granted_at=now,
            granted_by_user_id=actor_user_id if enabled else None,
            revoked_at=None if enabled else now,
            revoked_by_user_id=None if enabled else actor_user_id,
            reason=reason,
        )
        db.add(row)
        if "product_accesses" in user.__dict__:
            user.product_accesses.append(row)
    else:
        row.enabled = enabled
        row.reason = reason
        if enabled:
            row.granted_at = now
            row.granted_by_user_id = actor_user_id
            row.revoked_at = None
            row.revoked_by_user_id = None
        else:
            row.revoked_at = now
            row.revoked_by_user_id = actor_user_id
    await db.flush()
    return row


def synchronize_external_compatibility_role(user: User, products: Iterable[str]) -> None:
    """Keep old client/dealer checks operational while routes migrate.

    Funding wins for dual access because existing Funding scoping depends on
    ``Role.CLIENT``. Audit routes use ``is_audit_client`` plus an explicit
    DealerBusiness link, so a dual-access client remains safely scoped.
    """

    if user.role not in _EXTERNAL_PRODUCT_ROLES:
        return
    values = set(products)
    if ProductAccountType.FUNDING.value in values:
        user.role = Role.CLIENT
    elif ProductAccountType.AUDIT.value in values:
        user.role = Role.DEALER


def access_state(user: User) -> dict[str, Any]:
    return {
        "account_status": getattr(user, "account_status", "active"),
        "account_types": sorted(assigned_product_values(user)),
        "effective_account_types": sorted(enabled_product_values(user)),
        "role": user.role.value,
    }


def request_metadata(*, ip_address: str | None, user_agent: str | None) -> dict[str, str | None]:
    return {
        "ip_address": (ip_address or "")[:80] or None,
        "user_agent": (user_agent or "")[:400] or None,
    }


def record_access_event(
    db: AsyncSession,
    *,
    user_id: UUID | None,
    actor_user_id: UUID | None,
    action: str,
    reason: str | None,
    before_state: dict[str, Any] | None,
    after_state: dict[str, Any] | None,
    metadata: dict[str, Any] | None,
) -> UserAccessEvent:
    event = UserAccessEvent(
        user_id=user_id,
        actor_user_id=actor_user_id,
        action=action,
        reason=reason,
        before_state=before_state,
        after_state=after_state,
        request_metadata=metadata,
    )
    db.add(event)
    return event
