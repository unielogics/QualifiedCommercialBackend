"""PDF page chunking shared by model-backed document ingestion paths."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PDFChunk:
    raw: bytes
    first_page: int
    last_page: int
    total_pages: int


def split_pdf_pages(raw: bytes, *, pages_per_chunk: int = 8) -> list[PDFChunk]:
    """Return valid PDFs containing bounded, consecutive page ranges."""
    if pages_per_chunk < 1:
        raise ValueError("pages_per_chunk must be at least 1")
    try:
        import fitz

        source = fitz.open(stream=raw, filetype="pdf")
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"PDF could not be opened: {exc}") from exc

    try:
        if source.needs_pass:
            raise ValueError("PDF is password protected; upload an unlocked copy")
        total = source.page_count
        if total < 1:
            raise ValueError("PDF has no readable pages")
        chunks: list[PDFChunk] = []
        for first in range(0, total, pages_per_chunk):
            last = min(first + pages_per_chunk, total) - 1
            part = fitz.open()
            try:
                part.insert_pdf(source, from_page=first, to_page=last)
                chunks.append(
                    PDFChunk(
                        raw=part.tobytes(garbage=3, deflate=True),
                        first_page=first + 1,
                        last_page=last + 1,
                        total_pages=total,
                    )
                )
            finally:
                part.close()
        return chunks
    finally:
        source.close()
