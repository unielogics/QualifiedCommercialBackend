"""Exercise the actual capability SQL with profile and authorization boundaries."""
import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.dialects import sqlite

from app.services.mca_room_capabilities import payment_methods_available


class Database:
    def __init__(self):
        self.connection = sqlite3.connect(":memory:")
        self.connection.execute("CREATE TABLE fee_obligations (application_profile_id TEXT, status TEXT, superseded_at TEXT, authorization_sent_at TEXT)")
        self.connection.execute("CREATE TABLE private_funding_payment_plans (application_profile_id TEXT, status TEXT)")

    async def execute(self, statement):
        sql = str(statement.compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
        value = self.connection.execute(sql).fetchone()[0]
        return SimpleNamespace(scalar_one=lambda: value)


@pytest.fixture
def db():
    database = Database()
    yield database
    database.connection.close()


@pytest.mark.asyncio
async def test_new_mca_room_and_unrelated_application_have_no_payment_navigation(db):
    profile_id, other = uuid4(), uuid4()
    assert not await payment_methods_available(db, profile_id)
    db.connection.execute("INSERT INTO fee_obligations VALUES (?, 'authorization_sent', NULL, '2026-11-09')", (other.hex,))
    db.connection.execute("INSERT INTO private_funding_payment_plans VALUES (?, 'active')", (other.hex,))
    assert not await payment_methods_available(db, profile_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status,superseded,sent,expected", [
    ("prepared", None, None, False), ("authorization_sent", None, "2026-11-09", True),
    ("cancelled", None, "2026-11-09", False), ("superseded", None, "2026-11-09", False),
    ("authorization_sent", "2026-11-10", "2026-11-09", False),
])
async def test_fee_navigation_requires_this_profiles_current_sent_authorization(db, status, superseded, sent, expected):
    profile_id = uuid4()
    db.connection.execute("INSERT INTO fee_obligations VALUES (?, ?, ?, ?)", (profile_id.hex, status, superseded, sent))
    assert await payment_methods_available(db, profile_id) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected", [
    ("draft", False), ("active", True), ("authorized", True), ("paused", True), ("completed", True),
    ("cancelled", False), ("superseded", False),
])
async def test_private_plan_enables_navigation_only_after_assignment_or_authorization(db, status, expected):
    profile_id = uuid4()
    db.connection.execute("INSERT INTO private_funding_payment_plans VALUES (?, ?)", (profile_id.hex, status))
    assert await payment_methods_available(db, profile_id) is expected
