"""Keep JSONB invalidation metadata inside objects, including for JSON null."""

from copy import deepcopy

from sqlalchemy import case, func, literal, or_
from sqlalchemy.dialects.postgresql import JSONB

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


def sql_mark_snapshot(column, marker):
    return sql_snapshot_object(column).op("||")(literal(marker, type_=JSONB))
