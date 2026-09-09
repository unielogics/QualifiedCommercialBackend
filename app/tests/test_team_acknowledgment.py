"""The team's platform-document acknowledgment, and what the Team table says.

One backend module decides currency for every console; the apps read a flag
from /auth/me and the screen's content from /legal/acknowledgment, and POST
back the versions they were handed. The Team table shows, per person, their
own acknowledgment and — for a dealer partner — their signed Platform Access
Agreement, one query per table.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy.dialects import postgresql

from app.enums import ContractType, Role
from app.models.referral_partner_company import KIND_HOUSE
from app.routers import users as users_router
from app.services import user_acknowledgment as ack


def _user(role, **kw):
    base = dict(id=uuid4(), role=role, name="Rita Moss", email="rita@example.com", deleted_at=None,
                account_status="active", account_access_types=[], referral_partner_company_id=None, clerk_id=None, phone=None, title=None)
    base.update(kw)
    return SimpleNamespace(**base)


def _row(terms=ack.TERMS_VERSION, privacy=ack.PRIVACY_VERSION, disclosure=ack.DISCLOSURE_VERSION, user_id=None):
    return SimpleNamespace(user_id=user_id or uuid4(), terms_version=terms, privacy_version=privacy,
                           disclosure_version=disclosure, created_at=datetime(2026, 9, 9, tzinfo=UTC), ip_address="203.0.113.9", user_agent="pytest", id=uuid4())


def test_the_current_versions_are_effective_dates():
    current = ack.current_versions()
    assert set(current) == {"terms_version", "privacy_version", "disclosure_version"}
    for value in current.values():
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), value


def test_is_current_requires_all_three_versions():
    assert ack.is_current(_row()) is True
    assert ack.is_current(_row(disclosure=None)) is False
    assert ack.is_current(_row(terms="2026-05-19")) is False
    assert ack.is_current(None) is False


def test_needs_acknowledgment_matrix_by_role_and_version():
    for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC, Role.REGIONAL_MANAGER, Role.BROKER, Role.FIELD_REP):
        assert ack.needs_acknowledgment(_user(role), None) is True, role
        assert ack.needs_acknowledgment(_user(role), _row(privacy="2026-05-19")) is True, role
        assert ack.needs_acknowledgment(_user(role), _row()) is False, role
    for role in (Role.DEALER_PARTNER, Role.DEALER, Role.CLIENT, Role.LENDER, Role.VENDOR):
        assert ack.needs_acknowledgment(_user(role), None) is False, role
    # Gating is by role, not by console grants.
    assert ack.needs_acknowledgment(_user(Role.BROKER, account_access_types=["field_desk"]), None) is True


def test_status_is_missing_for_staff_and_not_asked_for_everyone_else():
    assert ack.acknowledgment_status(_user(Role.FIELD_REP), None) == "missing"
    assert ack.acknowledgment_status(_user(Role.BROKER), _row(terms="2026-05-19")) == "out_of_date"
    assert ack.acknowledgment_status(_user(Role.SUPER_ADMIN), _row()) == "current"
    # A partner who came in through /sign-up holds a row, but is never re-prompted: say so.
    assert ack.acknowledgment_status(_user(Role.DEALER_PARTNER), _row()) == "not_asked"
    assert ack.acknowledgment_status(_user(Role.DEALER_PARTNER), None) == "not_asked"


@pytest.mark.asyncio
async def test_latest_acceptances_is_one_query_keyed_by_user():
    ids = {uuid4(), uuid4()}
    rows = [_row(user_id=i) for i in ids]
    db = SimpleNamespace(execute=AsyncMock(return_value=SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))))
    out = await ack.latest_acceptances(db, ids)
    assert db.execute.await_count == 1 and set(out) == ids
    assert await ack.latest_acceptances(db, set()) == {} and db.execute.await_count == 1


def test_the_batch_statements_compile_to_distinct_on_with_the_right_order():
    sql = str(ack.latest_acceptances_stmt({uuid4()}).compile(dialect=postgresql.dialect()))
    assert "DISTINCT ON (legal_acceptances.user_id)" in sql
    assert "ORDER BY legal_acceptances.user_id, legal_acceptances.created_at DESC" in sql
    # The Platform Access helper, the same way, and only signed rows.
    import inspect

    source = inspect.getsource(users_router._platform_access_by_user)
    assert ".distinct(ContractAgreement.subject_id)" in source
    assert "order_by(ContractAgreement.subject_id, ContractAgreement.created_at.desc())" in source
    assert "ContractAgreement.signed_at.is_not(None)" in source


@pytest.mark.asyncio
async def test_auth_me_says_when_acknowledgment_is_still_needed():
    from app.routers.auth import me

    settings = SimpleNamespace(frontend_app_url="https://f", rep_app_url="https://r", audit_app_url="https://a")
    with patch("app.routers.auth.account_types", return_value=[]), patch("app.routers.auth.has_product_access", return_value=False), \
         patch("app.services.user_access.get_settings", return_value=settings):
        with patch.object(ack, "latest_acceptance", AsyncMock(return_value=None)):
            assert (await me(_user(Role.SUPER_ADMIN), SimpleNamespace())).needs_acknowledgment is True
            assert (await me(_user(Role.DEALER_PARTNER), SimpleNamespace())).needs_acknowledgment is False
        with patch.object(ack, "latest_acceptance", AsyncMock(return_value=_row())):
            assert (await me(_user(Role.SUPER_ADMIN), SimpleNamespace())).needs_acknowledgment is False
        with patch.object(ack, "latest_acceptance", AsyncMock(return_value=_row(disclosure=None))):
            assert (await me(_user(Role.FIELD_REP), SimpleNamespace())).needs_acknowledgment is True


@pytest.mark.asyncio
async def test_company_agreement_on_file_names_the_signed_company_and_skips_the_house():
    company = SimpleNamespace(id=uuid4(), name="Choice Car Care", kind="referral_partner")
    agreement = SimpleNamespace(contract_number="QC-RPA-2026-00042", signed_at=datetime(2026, 8, 12, tzinfo=UTC))

    async def get(_model, key, **_kw):
        return company if key == company.id else None

    db = SimpleNamespace(get=get, execute=AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: agreement)))
    out = await ack.company_agreement_on_file(db, _user(Role.BROKER, referral_partner_company_id=company.id))
    assert out is not None and out.company_name == "Choice Car Care" and out.contract_number == "QC-RPA-2026-00042"
    assert out.title.startswith("Strategic Referral")
    company.kind = KIND_HOUSE
    assert await ack.company_agreement_on_file(db, _user(Role.SUPER_ADMIN, referral_partner_company_id=company.id)) is None
    assert await ack.company_agreement_on_file(db, _user(Role.LOAN_EXEC)) is None
    assert db.execute.await_count == 1


def test_the_acknowledgment_route_is_registered():
    from app.routers.legal import router

    contract = {(r.path, m) for r in router.routes for m in getattr(r, "methods", set())}
    assert ("/legal/acknowledgment", "GET") in contract
    assert ("/legal/accept", "POST") in contract and ("/legal/acceptance", "GET") in contract


@pytest.mark.asyncio
async def test_the_acknowledgment_read_carries_what_the_screen_needs():
    from app.routers import legal

    settings = SimpleNamespace(frontend_app_url="https://app.example/")
    stale = _row(terms="2026-05-19")
    company = ack.CompanyAgreementOnFile(company_name="Choice Car Care", title="RPA", contract_number="QC-RPA-2026-00042", signed_at=None)
    with patch.object(legal, "get_settings", return_value=settings), \
         patch.object(ack, "latest_acceptance", AsyncMock(return_value=stale)), \
         patch.object(ack, "company_agreement_on_file", AsyncMock(return_value=company)):
        out = await legal.acknowledgment(_user(Role.BROKER), SimpleNamespace())
    assert out.status == "out_of_date" and out.current == ack.current_versions()
    assert [d.key for d in out.documents] == ["terms", "privacy", "disclosure"]
    assert all(d.url.startswith("https://app.example/") for d in out.documents)
    assert out.latest is not None and out.latest.terms_version == "2026-05-19"
    assert out.company_agreement is not None and out.company_agreement.contract_number == "QC-RPA-2026-00042"


def test_the_user_reads_carry_the_acknowledgment_and_platform_access_fields():
    fields = set(users_router.UserRead.model_fields)
    assert {"acknowledgment_status", "acknowledged_at", "platform_access_signed_at", "platform_access_contract_number", "inherited_account_types"} <= fields


@pytest.mark.asyncio
async def test_list_users_batches_one_query_per_table():
    rep = _user(Role.FIELD_REP)
    broker = _user(Role.BROKER)
    partner_signed = _user(Role.DEALER_PARTNER)
    partner_bare = _user(Role.DEALER_PARTNER)
    rows = [rep, broker, partner_signed, partner_bare]
    acceptance = _row(terms="2026-05-19", user_id=broker.id)
    paa = SimpleNamespace(subject_id=partner_signed.id, signed_at=datetime(2026, 8, 12, tzinfo=UTC), contract_number="QC-PAA-2026-00007")
    calls = {"users": 0, "companies": 0, "rpa": 0, "paa": 0, "acks": 0}

    async def execute(stmt):
        sql = str(stmt.compile(dialect=postgresql.dialect()))
        if "FROM users" in sql:
            calls["users"] += 1
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: rows))
        if "FROM referral_partner_companies" in sql:
            calls["companies"] += 1
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
        if "FROM legal_acceptances" in sql:
            calls["acks"] += 1
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [acceptance]))
        if "DISTINCT ON" in sql and "signed_at IS NOT NULL" in sql:
            calls["paa"] += 1
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: [paa]))
        calls["rpa"] += 1
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    out = await users_router.list_users(SimpleNamespace(execute=execute))
    by_id = {u.id: u for u in out}
    assert by_id[rep.id].acknowledgment_status == "missing" and by_id[rep.id].acknowledged_at is None
    assert by_id[broker.id].acknowledgment_status == "out_of_date" and by_id[broker.id].acknowledged_at == acceptance.created_at
    assert by_id[partner_signed.id].acknowledgment_status == "not_asked"
    assert by_id[partner_signed.id].platform_access_signed_at == paa.signed_at and by_id[partner_signed.id].platform_access_contract_number == "QC-PAA-2026-00007"
    assert by_id[partner_bare.id].platform_access_signed_at is None
    assert by_id[rep.id].inherited_account_types == ["audit", "field_desk"]
    assert calls["acks"] == 1 and calls["paa"] == 1 and calls["users"] == 1


def test_the_platform_access_type_is_the_one_the_gate_checks():
    assert ContractType.PLATFORM_ACCESS.value == "platform_access"
