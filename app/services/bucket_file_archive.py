"""Package a selection of a room's files into one zip for an operator download.

The buckets router reads each file's bytes (S3 for ordinary evidence, the
private store for protected ACH artifacts) and hands them in through
``read_bytes``; this module only decides the archive layout and the size
cap, so it stays testable without storage.
"""

from __future__ import annotations

import io
import re
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

ARCHIVE_MAX_BYTES = 200 * 1024 * 1024
ARCHIVE_MAX_FILES = 200


class ArchiveTooLargeError(ValueError):
    """The selection does not fit the in-memory archive cap."""


@dataclass(frozen=True)
class ArchiveResult:
    data: bytes
    files_packaged: int
    bytes_total: int
    entry_names: tuple[str, ...]


def entry_name(file_name: str | None, *, fallback: str) -> str:
    """A flat, safe name for one entry: no directories, no control characters."""

    raw = (file_name or "").replace("\\", "/").rsplit("/", 1)[-1]
    raw = "".join(char for char in raw if ord(char) >= 32 and not 127 <= ord(char) <= 159)
    raw = raw.strip().lstrip(".").strip()
    return raw or fallback


def unique_entry_names(names: Iterable[str]) -> list[str]:
    """Keep every name; a repeat gets " (2)", " (3)"... before its extension."""

    seen: set[str] = set()
    out: list[str] = []
    for base in names:
        name = base
        counter = 2
        while name.casefold() in seen:
            stem, dot, ext = base.rpartition(".")
            name = f"{stem} ({counter}).{ext}" if dot and stem else f"{base} ({counter})"
            counter += 1
        seen.add(name.casefold())
        out.append(name)
    return out


def archive_file_name(room_name: str | None) -> str:
    """ "<room> files.zip", keeping only the characters every OS accepts in a name."""

    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "", room_name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ._")[:80].strip(" ._")
    return f"{cleaned or 'room'} files.zip"


def _too_large() -> ArchiveTooLargeError:
    limit_mb = ARCHIVE_MAX_BYTES // (1024 * 1024)
    return ArchiveTooLargeError(
        f"The selection is larger than {limit_mb} MB. Download fewer files at a time."
    )


def _entry_info(name: str, created_at: Any) -> zipfile.ZipInfo:
    stamped = isinstance(created_at, datetime) and created_at.year >= 1980
    date_time = created_at.timetuple()[:6] if stamped else (1980, 1, 1, 0, 0, 0)
    info = zipfile.ZipInfo(name, date_time=date_time)
    info.compress_type = zipfile.ZIP_DEFLATED
    # A hand-built ZipInfo carries no permissions; without these, unzip on
    # Linux extracts the files unreadable.
    info.external_attr = 0o644 << 16
    return info


def build_archive(files: Iterable[Any], read_bytes: Callable[[Any], bytes]) -> ArchiveResult:
    """Zip ``files`` in the order given, each under its own disambiguated name.

    Refuses before reading anything when the declared sizes already exceed the
    cap, and again while reading if the bytes turn out larger than declared.
    """

    rows = list(files)
    if not rows:
        raise ValueError("No files to package.")
    if len(rows) > ARCHIVE_MAX_FILES:
        raise ArchiveTooLargeError(f"Select at most {ARCHIVE_MAX_FILES} files per download.")
    declared = sum(int(getattr(row, "size_bytes", 0) or 0) for row in rows)
    if declared > ARCHIVE_MAX_BYTES:
        raise _too_large()
    names = unique_entry_names(
        entry_name(
            getattr(row, "file_name", None),
            fallback=f"file-{str(getattr(row, 'id', '') or '')[:8] or 'unnamed'}",
        )
        for row in rows
    )
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for row, name in zip(rows, names, strict=True):
            body = read_bytes(row)
            total += len(body)
            if total > ARCHIVE_MAX_BYTES:
                raise _too_large()
            archive.writestr(_entry_info(name, getattr(row, "created_at", None)), body)
    return ArchiveResult(
        data=buffer.getvalue(),
        files_packaged=len(names),
        bytes_total=total,
        entry_names=tuple(names),
    )
