"""Keep JSONB invalidation metadata inside objects, including for JSON null."""

from copy import deepcopy

from sqlalchemy import case, func, literal, or_
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.functions import FunctionElement

STALE_MARKERS = frozenset({"_funding_request_stale", "funding_request_stale",
                           "_document_versions_stale", "document_versions_stale"})


def known_null_wrapper(value, markers=STALE_MARKERS):
    """The precise null || object shape created by the former invalidators."""
    return (isinstance(value, list) and len(value) >= 2 and value[0] is None
            and all(isinstance(part, dict) for part in value[1:])
            and any(part.get(key) is True for part in value[1:] for key in markers))


def snapshot_object(value):
    if isinstance(value, dict):
        return deepcopy(value)
    if value is None:
        return {}
    if known_null_wrapper(value):
        result = {}
        for part in value[1:]:
            result.update(deepcopy(part))
        return result
    # Preserve malformed, unrelated values without presenting their contents as
    # financial facts. The repair migration never rewrites arbitrary arrays.
    return {"_legacy_snapshot_value": deepcopy(value)}


def sql_snapshot_object(column):
    empty = literal({}, type_=JSONB)
    return case((func.jsonb_typeof(column) == "object", column),
                (or_(column.is_(None), func.jsonb_typeof(column) == "null"), empty),
                else_=func.jsonb_build_object("_legacy_snapshot_value", column))


class _SnapshotMarker(FunctionElement):
    type = JSONB()
    inherit_cache = True


@compiles(_SnapshotMarker)
def _postgres_snapshot_marker(element, compiler, **kwargs):
    column, marker = list(element.clauses)
    return compiler.process(sql_snapshot_object(column).op("||")(marker), **kwargs)


@compiles(_SnapshotMarker, "sqlite")
def _sqlite_snapshot_marker(element, compiler, **kwargs):
    # SQLite is also used by real application workflows in the test suite.
    # json_patch merges these non-null stale/revision markers as objects;
    # SQLite's || operator would merely concatenate two JSON strings.
    column, marker = list(element.clauses)
    base = case((func.json_type(column) == "object", column),
                (or_(column.is_(None), func.json_type(column) == "null"), literal("{}")),
                else_=func.json_object("_legacy_snapshot_value", func.json(column)))
    return compiler.process(func.json_patch(base, func.json(marker)), **kwargs)


def sql_mark_snapshot(column, marker):
    return _SnapshotMarker(column, literal(marker, type_=JSONB))
