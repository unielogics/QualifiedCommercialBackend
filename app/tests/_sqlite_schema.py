"""An on-disk SQLite copy of the schema for tests that need the real ORM.

Most tests here hand the code a session double. That cannot show what a real
session does to rows around a commit or a rollback, or whether a query that is
assembled from several helpers actually runs. These helpers build the tables
once per module (foreign keys left out of the DDL; only the queries matter) and
give each test its own copy.

Use it as:

    from app.tests._sqlite_schema import sqlite_session, sqlite_template

    @pytest.fixture(scope="module")
    def template_database(tmp_path_factory):
        return sqlite_template(tmp_path_factory, "my_tests")

    @pytest.fixture
    async def db(template_database, tmp_path):
        async with sqlite_session(template_database, tmp_path) as session:
            yield session
"""

from __future__ import annotations

import asyncio
import re
import shutil
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import Base


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _sqlite_array(_type, _compiler, **_kwargs):
    return "JSON"


def _sqlite_functions(dbapi_connection, _record) -> None:
    # Expressions written for Postgres that SQLite has no word for.
    dbapi_connection.create_function(
        "regexp_replace", 4, lambda value, pattern, repl, _flags: re.sub(pattern, repl, value or "")
    )


async def _create_schema(path) -> None:
    import app.models  # noqa: F401  (registers every table)

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    metadata = sa.MetaData()
    for table in Base.metadata.tables.values():
        clone = table.to_metadata(metadata)
        for constraint in list(clone.foreign_key_constraints):
            clone.constraints.discard(constraint)
        for column in clone.columns:
            column.foreign_keys.clear()
            # SQLite gives an unrecognized UUID DDL type NUMERIC affinity, and
            # an all-numeric hex prefix is then read back as a number.
            if isinstance(column.type, PG_UUID):
                column.type = sa.String(36)
    for table in list(metadata.tables.values()):
        for column in table.columns:
            if column.server_default is not None:
                literal = str(getattr(column.server_default, "arg", ""))
                if "::jsonb" in literal:
                    column.server_default = sa.DefaultClause(sa.text(literal.replace("::jsonb", "")))
                elif "now()" in literal:
                    column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
                elif "gen_random_uuid" in literal:
                    column.server_default = None
        for index in table.indexes:
            condition = index.dialect_options["postgresql"].get("where")
            if condition is not None:
                index.dialect_options["sqlite"]["where"] = condition
        async with engine.begin() as connection:
            try:
                await connection.run_sync(lambda sync, current=table: metadata.create_all(sync, tables=[current]))
            except Exception:  # noqa: BLE001  a few tables use Postgres-only functions; none are used here
                pass
    await engine.dispose()


def sqlite_template(tmp_path_factory, name: str):
    """Build the schema once; tests copy the file rather than rebuild it."""
    path = tmp_path_factory.mktemp(name) / "template.db"
    asyncio.run(_create_schema(path))
    return path


@asynccontextmanager
async def sqlite_session(template, tmp_path, *, expire_on_commit: bool = False):
    """A real AsyncSession on this test's own copy of the schema.

    `expire_on_commit` defaults to the application's own setting (app/db.py),
    so a row behaves around commit and rollback as it does in production.
    """
    copy = tmp_path / "test.db"
    shutil.copy(template, copy)
    engine = create_async_engine(f"sqlite+aiosqlite:///{copy}")
    event.listen(engine.sync_engine, "connect", _sqlite_functions)
    try:
        async with AsyncSession(engine, expire_on_commit=expire_on_commit) as session:
            yield session
    finally:
        await engine.dispose()
