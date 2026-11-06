import importlib.util
from pathlib import Path
from unittest.mock import Mock

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


@pytest.mark.parametrize("revision", [
    "0267_bucket_document_versions",
    "0268_document_file_versions",
    "0269_dealer_document_versions",
])
def test_document_version_upgrade_sql_has_no_unbound_parameters(revision):
    migration_path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / f"{revision}.py"
    spec = importlib.util.spec_from_file_location(revision, migration_path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    migration.op = Mock()

    migration.upgrade()

    statements = migration.op.execute.call_args_list
    assert statements, "The migration's data backfill and retention SQL must be checked"
    for call in statements:
        statement = call.args[0]
        clause = sa.text(statement) if isinstance(statement, str) else statement
        compiled = clause.compile(dialect=postgresql.dialect())
        assert compiled.params == {}, (
            f"{revision} raw migration SQL contains accidental bind parameters: "
            f"{compiled.params}; SQL: {statement}"
        )
