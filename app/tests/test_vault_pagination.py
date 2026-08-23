from types import SimpleNamespace
from uuid import uuid4

from app.enums import Role
from app.routers.documents import _vault_loan_scope, router


def _route(path: str):
    return next(route for route in router.routes if route.path == path)


def _query_param(path: str, name: str):
    route = _route(path)
    return next(param for param in route.dependant.query_params if param.name == name)


def test_vault_routes_are_bounded_by_default_and_cap_page_size() -> None:
    loan_index_limit = _query_param("/documents/vault", "limit")
    document_limit = _query_param("/documents/vault/{loan_id}", "limit")

    assert loan_index_limit.default == 20
    assert document_limit.default == 25
    assert any(getattr(rule, "le", None) == 50 for rule in loan_index_limit.field_info.metadata)
    assert any(getattr(rule, "le", None) == 50 for rule in document_limit.field_info.metadata)


def test_vault_index_applies_client_scope_before_document_aggregation() -> None:
    client_id = uuid4()
    user = SimpleNamespace(role=Role.CLIENT, client=SimpleNamespace(id=client_id))

    sql = str(_vault_loan_scope(user).compile(compile_kwargs={"literal_binds": False}))

    assert "loans.client_id" in sql
    assert "documents.loan_id = loans.id" in sql
    assert "documents.status" in sql


def test_vault_search_stays_inside_the_scoped_loan_query() -> None:
    broker_id = uuid4()
    user = SimpleNamespace(role=Role.BROKER, broker=SimpleNamespace(id=broker_id))

    sql = str(_vault_loan_scope(user, "QC-100").compile(compile_kwargs={"literal_binds": False}))

    assert "loans.broker_id" in sql
    assert "clients.name" in sql
    assert "loans.deal_id" in sql
    assert "documents.name" in sql


def test_roles_without_a_funding_book_receive_an_empty_scope() -> None:
    user = SimpleNamespace(role=Role.FIELD_REP)

    sql = str(_vault_loan_scope(user).compile(compile_kwargs={"literal_binds": False}))

    assert "false" in sql.lower()
