"""Lossless recovery snapshots for Field Desk evidence removal.

Field Desk documents are hidden rather than physically destroyed, but some of
their normalized facts must be removed from the live underwriting ledger while
the document is hidden.  This module serializes those rows into the document's
JSON archive so a coordinated restore can put back the *same* facts rather than
running a newer extractor and silently changing the historical result.

The archive intentionally lives on ``DealerDocument.doc_meta``.  It avoids a
schema change, stays in the same database transaction as the removal, and is
not exposed through the generic Bucket restore endpoint.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy.orm import DeclarativeBase

from ..models import DealerDocument

ARCHIVE_KEY = "_field_desk_deletion"
ARCHIVE_VERSION = 1

def _json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (UUID, date, datetime, Decimal)):
        return str(value)
    return value


def row_snapshot(row: DeclarativeBase) -> dict[str, Any]:
    """Serialize every durable column, including the original audit stamps."""
    return {
        column.name: _json_value(getattr(row, column.name))
        for column in row.__table__.columns
    }


def _python_value(column: Any, value: Any) -> Any:
    if value is None:
        return None
    try:
        python_type = column.type.python_type
    except (AttributeError, NotImplementedError):
        return value
    if python_type is UUID and not isinstance(value, UUID):
        return UUID(str(value))
    if python_type is datetime and not isinstance(value, datetime):
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if python_type is date and not isinstance(value, date):
        return date.fromisoformat(str(value))
    if python_type is Decimal and not isinstance(value, Decimal):
        return Decimal(str(value))
    return value


def row_from_snapshot[T: DeclarativeBase](model: type[T], payload: dict[str, Any]) -> T:
    columns = {column.name: column for column in model.__table__.columns}
    values = {
        name: _python_value(columns[name], value)
        for name, value in payload.items()
        if name in columns
    }
    return model(**values)


def snapshots_match(
    row: DeclarativeBase,
    payload: dict[str, Any],
    *,
    fields: set[str] | None = None,
) -> bool:
    current = row_snapshot(row)
    keys = fields if fields is not None else set(payload)
    return all(current.get(key) == payload.get(key) for key in keys)


def archive_for(document: DealerDocument) -> dict[str, Any] | None:
    metadata = document.doc_meta if isinstance(document.doc_meta, dict) else {}
    archive = metadata.get(ARCHIVE_KEY)
    return archive if isinstance(archive, dict) else None


def set_archive(document: DealerDocument, archive: dict[str, Any]) -> None:
    metadata = dict(document.doc_meta) if isinstance(document.doc_meta, dict) else {}
    metadata[ARCHIVE_KEY] = archive
    document.doc_meta = metadata


def clear_archive(document: DealerDocument) -> None:
    metadata = dict(document.doc_meta) if isinstance(document.doc_meta, dict) else {}
    metadata.pop(ARCHIVE_KEY, None)
    document.doc_meta = metadata or None

