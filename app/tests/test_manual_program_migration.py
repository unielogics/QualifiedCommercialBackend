import importlib.util
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations


def migration():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/0277_manual_program_without_criteria.py"
    spec = importlib.util.spec_from_file_location("manual_program_criteria_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_postgresql_upgrade_keeps_real_criteria_foreign_key_and_allows_only_paired_manual_nulls():
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    module = migration()
    module.op = Operations(context)
    module.upgrade()
    sql = output.getvalue()
    assert "playbook_id DROP NOT NULL" in sql and "playbook_version DROP NOT NULL" in sql
    assert "CHECK ((playbook_id IS NULL) = (playbook_version IS NULL))" in sql
    assert "CHECK (source = 'operator' OR playbook_id IS NOT NULL)" in sql
    assert "DROP TABLE" not in sql and "DELETE " not in sql and "DROP CONSTRAINT" not in sql


def test_downgrade_refuses_to_delete_or_fabricate_criteria_for_unbound_operator_choices():
    module = migration()
    statements = []
    module.op = SimpleNamespace(get_bind=lambda: SimpleNamespace(execute=lambda query:
        SimpleNamespace(scalar_one=lambda: 2)),
        drop_constraint=lambda *args, **kwargs: statements.append(args),
        alter_column=lambda *args, **kwargs: statements.append(args))
    with pytest.raises(RuntimeError, match="selections were retained"):
        module.downgrade()
    assert not statements
