from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, Request
from sqlalchemy import select

from app.dealer_os.deps import resolve_dealer_scope
from app.deps import (
    _enforce_external_product_boundary,
    require_audit_access,
    require_funding_access,
)
from app.enums import ProductAccountType, Role
from app.models.client import Client
from app.models.loan import Loan
from app.routers.client_access import ClientAccessDirectoryRow, _matches_filters
from app.routers.dealer_ai_intake import McaRefiStart, _public_intake_attribution
from app.scoping import scope_client_query, scope_loan_query
from app.services import clerk as clerk_service
from app.services.user_access import (
    account_types,
    enabled_product_values,
    is_audit_client,
    is_funding_client,
    synchronize_external_compatibility_role,
)


def _external_user(
    *,
    role: Role = Role.CLIENT,
    products: tuple[str, ...] = (),
    account_status: str = "active",
    client_id=None,
):
    return SimpleNamespace(
        id=uuid4(),
        role=role,
        account_status=account_status,
        deleted_at=None,
        product_accesses=[SimpleNamespace(product=value, enabled=True) for value in products],
        client=SimpleNamespace(id=client_id) if client_id else None,
    )


def _request(path: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "root_path": "",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 443),
        }
    )


def test_loaded_entitlements_are_authoritative_for_external_users() -> None:
    user = _external_user(role=Role.CLIENT)

    assert enabled_product_values(user) == set()
    assert account_types(user) == []
    assert is_funding_client(user) is False


def test_dual_access_keeps_funding_compatibility_role() -> None:
    user = _external_user(role=Role.DEALER, products=("funding", "audit"))

    synchronize_external_compatibility_role(user, enabled_product_values(user))

    assert user.role == Role.CLIENT
    assert is_funding_client(user) is True
    assert is_audit_client(user) is True
    assert account_types(user) == [ProductAccountType.FUNDING, ProductAccountType.AUDIT]


def test_audit_only_access_uses_dealer_compatibility_role() -> None:
    user = _external_user(role=Role.CLIENT, products=("audit",))

    synchronize_external_compatibility_role(user, enabled_product_values(user))

    assert user.role == Role.DEALER
    assert is_funding_client(user) is False
    assert is_audit_client(user) is True


def test_suspension_disables_every_product() -> None:
    user = _external_user(products=("funding", "audit"), account_status="suspended")

    assert enabled_product_values(user) == set()
    assert account_types(user) == [ProductAccountType.FUNDING, ProductAccountType.AUDIT]
    assert is_funding_client(user) is False
    assert is_audit_client(user) is False


@pytest.mark.asyncio
async def test_central_product_guards_deny_missing_entitlements() -> None:
    funding_only = _external_user(products=("funding",))
    audit_only = _external_user(role=Role.DEALER, products=("audit",))

    assert await require_funding_access(funding_only) is funding_only
    assert await require_audit_access(audit_only) is audit_only
    with pytest.raises(HTTPException) as funding_error:
        await require_funding_access(audit_only)
    with pytest.raises(HTTPException) as audit_error:
        await require_audit_access(funding_only)
    assert funding_error.value.status_code == 403
    assert audit_error.value.status_code == 403


def test_audit_only_identity_is_default_denied_from_funding_operator_routes() -> None:
    audit_only = _external_user(role=Role.DEALER, products=("audit",))

    _enforce_external_product_boundary(
        audit_only, _request("/api/v1/dealer-os/dealers")
    )
    _enforce_external_product_boundary(
        audit_only, _request("/api/v1/application-profiles/resolve")
    )
    _enforce_external_product_boundary(audit_only, _request("/api/v1/auth/me"))
    with pytest.raises(HTTPException) as exc:
        _enforce_external_product_boundary(audit_only, _request("/api/v1/ai-tasks"))

    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "product_access_required"


def test_dual_identity_reaches_funding_routes_then_uses_record_scopes() -> None:
    dual = _external_user(products=("funding", "audit"))

    _enforce_external_product_boundary(dual, _request("/api/v1/loans"))


@pytest.mark.asyncio
async def test_clerk_session_revocation_uses_the_supported_session_list_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    class Response:
        status_code = 200
        text = ""

        def __init__(self, payload=None):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, *, headers, params):
            calls.append(("GET", url, params))
            return Response(
                {
                    "data": [
                        {"id": "sess_active", "status": "active"},
                        {"id": "sess_revoked", "status": "revoked"},
                    ],
                    "total_count": 2,
                }
            )

        async def post(self, url, *, headers):
            calls.append(("POST", url, None))
            return Response()

    monkeypatch.setattr(
        clerk_service, "get_settings", lambda: SimpleNamespace(clerk_secret_key="secret")
    )
    monkeypatch.setattr(clerk_service.httpx, "AsyncClient", lambda **_kwargs: FakeClient())

    assert await clerk_service.revoke_user_sessions("user_123") is True
    assert calls == [
        (
            "GET",
            "https://api.clerk.com/v1/sessions",
            {"user_id": "user_123", "limit": 500, "offset": 0},
        ),
        ("POST", "https://api.clerk.com/v1/sessions/sess_active/revoke", None),
    ]


def test_funding_queries_remain_confined_to_the_linked_client() -> None:
    client_id = uuid4()
    user = _external_user(products=("funding", "audit"), client_id=client_id)

    client_sql = str(scope_client_query(user, select(Client)).compile())
    loan_sql = str(scope_loan_query(user, select(Loan)).compile())

    assert "clients.id" in client_sql
    assert "loans.client_id" in loan_sql


def test_audit_only_role_has_no_funding_book() -> None:
    user = _external_user(role=Role.DEALER, products=("audit",))

    client_sql = str(scope_client_query(user, select(Client)).compile())
    loan_sql = str(scope_loan_query(user, select(Loan)).compile())

    assert "false" in client_sql.lower()
    assert "false" in loan_sql.lower()


@pytest.mark.asyncio
async def test_audit_scope_mismatch_returns_not_found() -> None:
    user = _external_user(products=("funding", "audit"))
    dealer = SimpleNamespace(
        id=uuid4(), dealer_user_id=uuid4(), owner_user_id=None, archived_at=None
    )
    db = SimpleNamespace(get=AsyncMock(return_value=dealer))

    with pytest.raises(HTTPException) as exc:
        await resolve_dealer_scope(db, user, dealer.id)

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_dual_access_can_open_only_its_explicit_audit_business() -> None:
    user = _external_user(products=("funding", "audit"))
    dealer = SimpleNamespace(
        id=uuid4(), dealer_user_id=user.id, owner_user_id=None, archived_at=None
    )
    db = SimpleNamespace(get=AsyncMock(return_value=dealer))

    assert await resolve_dealer_scope(db, user, dealer.id) is dealer


def test_directory_filters_distinguish_product_combinations() -> None:
    row = ClientAccessDirectoryRow(
        subject_kind="client",
        subject_id=uuid4(),
        client_name="Example Client",
        origin="public_site",
        login_state="active",
        account_types=[ProductAccountType.FUNDING, ProductAccountType.AUDIT],
        file_count=2,
        status="active",
    )

    assert _matches_filters(row, source="public_site", login_state="active", account_type="both")
    assert not _matches_filters(row, source=None, login_state=None, account_type="funding")
    assert not _matches_filters(row, source="public_intake", login_state=None, account_type=None)


def test_mca_public_intake_preserves_normalized_signup_attribution() -> None:
    payload = McaRefiStart(
        full_name="Example Owner",
        email="owner@example.com",
        source=" public_site ",
        page=" /mca-refinance ",
        program="mca-refinance",
        vertical="mca",
        campaign="summer-recovery",
        cta="start_mca_refinance",
    )

    assert _public_intake_attribution(payload) == {
        "source": "public_site",
        "page": "/mca-refinance",
        "program": "mca-refinance",
        "vertical": "mca",
        "campaign": "summer-recovery",
        "cta": "start_mca_refinance",
    }
