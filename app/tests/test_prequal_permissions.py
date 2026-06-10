import sys
from types import ModuleType
from types import SimpleNamespace
from uuid import uuid4

from app.enums import Role

prequal_pdf_stub = ModuleType("app.services.prequal_pdf")
prequal_pdf_stub.presign_get = lambda *args, **kwargs: None
calendar_emitter_stub = ModuleType("app.services.calendar_emitter")
sys.modules.setdefault("app.services.prequal_pdf", prequal_pdf_stub)
sys.modules.setdefault("app.services.calendar_emitter", calendar_emitter_stub)

from app.routers.prequal import (
    _can_manage_prequal_queue,
    _can_underwrite_prequal,
    _scope_loan_for_borrower,
)


def _user(role: Role, **attrs):
    base = {"role": role, "client": None, "broker": None}
    base.update(attrs)
    return SimpleNamespace(**base)


def test_broker_can_manage_but_not_underwrite_prequals():
    user = _user(Role.BROKER)

    assert _can_manage_prequal_queue(user)
    assert not _can_underwrite_prequal(user)


def test_internal_roles_can_manage_and_underwrite_prequals():
    for role in (Role.SUPER_ADMIN, Role.LOAN_EXEC):
        user = _user(role)

        assert _can_manage_prequal_queue(user)
        assert _can_underwrite_prequal(user)


def test_client_cannot_manage_or_underwrite_prequals():
    user = _user(Role.CLIENT)

    assert not _can_manage_prequal_queue(user)
    assert not _can_underwrite_prequal(user)


def test_broker_loan_scope_requires_matching_broker_id():
    broker_id = uuid4()
    other_broker_id = uuid4()
    user = _user(Role.BROKER, broker=SimpleNamespace(id=broker_id))

    assert _scope_loan_for_borrower(SimpleNamespace(broker_id=broker_id), user)
    assert not _scope_loan_for_borrower(SimpleNamespace(broker_id=other_broker_id), user)
