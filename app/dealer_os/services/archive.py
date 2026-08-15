"""ZIP archive intake — pure, zip-bomb-safe entry planning (doc hub, 0114).

The Documents tab accepts a ZIP of mixed documents; the router expands it
IN MEMORY with stdlib zipfile into one child DealerDocument per usable entry.
Everything here is metadata-only and pure (unit-testable): plan_zip_entries
decides keep/skip/reject from ZipInfo alone — declared sizes are enforced
BEFORE any entry bytes are read, so a hostile archive can never make us
decompress its payload just to discover it is oversized.

Guard contract:
- hard REJECT (ValueError -> 422): more than MAX_ZIP_ENTRIES usable entries,
  total declared uncompressed size over MAX_ZIP_TOTAL_BYTES, or any usable
  entry that is encrypted (password-protected archives are unreadable anyway).
- per-entry SKIP (reported in the parent's extracted.skipped): directories are
  dropped silently (structure, not content); __MACOSX/* resource forks, hidden
  dotfiles, nested .zip archives, unsupported extensions, oversized or empty
  declared entries are skipped with a reason.
"""

from __future__ import annotations

import zipfile

# Formats the extraction pipeline can actually process (CSV/XLSX pure-parse,
# PDF/image via the vision model). Everything else is skipped, not failed.
ALLOWED_ENTRY_EXTENSIONS = {
    ".pdf", ".csv", ".tsv", ".txt", ".xlsx", ".png", ".jpg", ".jpeg", ".gif", ".webp",
}

MAX_ZIP_ENTRIES = 40                       # usable entries per archive
MAX_ZIP_TOTAL_BYTES = 120 * 1024 * 1024    # declared uncompressed total
MAX_ZIP_LISTED_ENTRIES = 2000              # central-directory rows we'll even look at
MAX_SKIPPED_REPORTED = 60                  # skip rows persisted/reported per archive

_ZIP_CONTENT_TYPES = ("application/zip", "application/x-zip-compressed")

_ENTRY_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def _extension(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    dot = base.rfind(".")
    return base[dot:].lower() if dot > 0 else ""


def is_zip_upload(content_type: str, filename: str) -> bool:
    """Detect a ZIP upload by declared content-type or .zip filename."""
    ct = (content_type or "").lower().split(";")[0].strip()
    return ct in _ZIP_CONTENT_TYPES or (filename or "").lower().endswith(".zip")


def content_type_for(entry_name: str) -> str:
    """Content type for an allowed archive entry, by extension."""
    return _ENTRY_CONTENT_TYPES.get(_extension(entry_name), "application/octet-stream")


def plan_zip_entries(
    infos: list[zipfile.ZipInfo], max_entry_bytes: int
) -> tuple[list[zipfile.ZipInfo], list[dict[str, str]]]:
    """Pure keep/skip plan over an archive's ZipInfo metadata.

    Returns (usable_infos, skipped=[{"name", "reason"}...]). Raises ValueError
    for whole-archive rejections (entry-count / total-size limits, encrypted
    entries). No entry bytes are ever read here — every decision comes from
    declared metadata (ZipInfo.file_size, flag_bits, the entry name).
    """
    if len(infos) > MAX_ZIP_LISTED_ENTRIES:
        raise ValueError(
            f"The ZIP lists {len(infos)} entries — archives over "
            f"{MAX_ZIP_LISTED_ENTRIES} entries are not accepted. Split it up."
        )
    usable: list[zipfile.ZipInfo] = []
    skipped: list[dict[str, str]] = []
    skipped_overflow = 0

    def _skip(name: str, reason: str) -> None:
        nonlocal skipped_overflow
        if len(skipped) < MAX_SKIPPED_REPORTED:
            skipped.append({"name": name, "reason": reason})
        else:
            skipped_overflow += 1

    for info in infos:
        name = info.filename
        if info.is_dir():
            continue  # structure, not content — not worth reporting
        base = name.rsplit("/", 1)[-1]
        if name.startswith("__MACOSX/") or "/__MACOSX/" in name:
            _skip(name, "macos_metadata")
            continue
        if base.startswith("."):
            _skip(name, "hidden_file")
            continue
        ext = _extension(name)
        if ext == ".zip":
            _skip(name, "nested_archive")
            continue
        if ext not in ALLOWED_ENTRY_EXTENSIONS:
            _skip(name, "unsupported_type")
            continue
        if info.file_size > max_entry_bytes:
            _skip(name, "exceeds_size_limit")
            continue
        if info.file_size == 0:
            _skip(name, "empty")
            continue
        if info.flag_bits & 0x1:
            raise ValueError(
                "The ZIP archive is password-protected — remove the password and upload it again."
            )
        usable.append(info)

    if len(usable) > MAX_ZIP_ENTRIES:
        raise ValueError(
            f"The ZIP contains {len(usable)} usable documents — the limit is "
            f"{MAX_ZIP_ENTRIES} per archive. Split it into smaller archives."
        )
    total_declared = sum(i.file_size for i in usable)
    if total_declared > MAX_ZIP_TOTAL_BYTES:
        raise ValueError(
            "The ZIP declares more than "
            f"{MAX_ZIP_TOTAL_BYTES // (1024 * 1024)}MB of uncompressed content — "
            "split it into smaller archives."
        )
    if skipped_overflow:
        skipped.append({"name": f"(+{skipped_overflow} more)", "reason": "not_listed"})
    return usable, skipped
