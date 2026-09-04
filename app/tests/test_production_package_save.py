"""Saving a Production Package must return the row it just wrote.

Every writing route commits and then serialises the same instance. `updated_at`
is stamped by the database through a server-side `onupdate`, so SQLAlchemy
expires the attribute on the UPDATE flush and reloads it on next access — and in
an async session that lazy reload raises MissingGreenlet. The write landed, the
response 500'd, the client kept its old `version`, and the next PATCH came back
409 "Someone else saved this package." Production ran at two failed PATCHes out
of two before this was fixed.

These tests pin the three facts that made it happen, so it cannot come back
quietly.
"""

from __future__ import annotations

import inspect

from sqlalchemy import inspect as sa_inspect

from app.models._mixins import TimestampMixin
from app.models.production_package import ProductionPackage
from app.services import production_packages as pkgs

# --- the hazard ---------------------------------------------------------------


def test_updated_at_is_stamped_by_the_database_not_by_python():
    """The root cause. A server-side onupdate is what expires the attribute.

    If this ever becomes a Python-side default the expiry stops happening and
    the guard in serialize() is dead weight — delete it then, not before.
    """
    col = ProductionPackage.__table__.columns["updated_at"]
    assert col.onupdate is not None
    assert col.onupdate.is_clause_element, "a Python-side default would not expire the attribute"
    assert ProductionPackage.__mro__.count(TimestampMixin) == 1


def test_an_expired_attribute_reads_as_unloaded():
    """The predicate the guard tests on. Unloaded while expired, loaded once set."""
    package = ProductionPackage()
    assert "updated_at" in sa_inspect(package).unloaded

    package.updated_at = None
    assert "updated_at" not in sa_inspect(package).unloaded


# --- the guard ----------------------------------------------------------------


def test_serialize_reloads_updated_at_before_reading_it():
    source = inspect.getsource(pkgs.serialize)
    assert 'await db.refresh(package, ["updated_at"])' in source

    # Ordering is the whole point: a refresh after the read fixes nothing.
    refresh_at = source.index("db.refresh(package")
    read_at = source.index("updated_at=package.updated_at")
    assert refresh_at < read_at, "the refresh must come before the read"


def test_the_reload_is_skipped_when_nothing_expired_it():
    """A read route serialises without writing; it must not pay for a round trip."""
    source = inspect.getsource(pkgs.serialize)
    guard = 'if "updated_at" in sa_inspect(package).unloaded:'
    assert guard in source
    assert source.index(guard) < source.index("db.refresh(package")


def test_updated_at_is_read_in_exactly_one_place():
    """The guard covers every writing route because they all funnel through
    serialize(). A second read site elsewhere would need its own guard."""
    from pathlib import Path

    root = Path(pkgs.__file__).parent
    hits = [
        f"{path.name}: {line.strip()}"
        for path in sorted(root.glob("production_*.py"))
        for line in path.read_text().splitlines()
        if ".updated_at" in line and "db.refresh" not in line
    ]
    assert len(hits) == 1, hits
    assert hits[0].startswith("production_packages.py: updated_at=package.updated_at"), hits
