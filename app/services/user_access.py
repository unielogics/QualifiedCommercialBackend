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
