"""Generic PDF signature stamper (PyMuPDF), shared by every signature path:
the dealer's fresh signature, the signatures on file placed at send, and
the manual fallback record.

Two schemes:

* ``template_v1`` — the agreement templates carry invisible anchor tokens
  in their text layer: ``[[SIG:{party}:{n}]]`` inside each "By (signature)"
  underline, ``[[DATE:{party}:{n}]]`` in that column's Date slot and
  ``[[INI:{party}:{n}]]`` on every initials line. Stamping a party fills
  every one of its tokens on every page; other parties' tokens are left
  for their own pass, and whatever is still standing at execution is
  whited out by :func:`redact_remaining_anchors`.
* ``legacy`` — the heading-based layout of the hand-built stage-one
  agreement (``SIGNATURE - DEALER AUTHORIZED REPRESENTATIVE`` + the
  ``Electronic signature`` / ``Recorded signature`` placeholders). Kept so a
  revision that was out for signature at deploy still signs.

PyMuPDF is imported lazily: the callers translate ``ImportError`` into a
503 the way the presentation renderer does.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from typing import Any

STAMP_SCHEME_TEMPLATE = "template_v1"
STAMP_SCHEME_LEGACY = "legacy"
STAMP_SCHEMES: tuple[str, ...] = (STAMP_SCHEME_TEMPLATE, STAMP_SCHEME_LEGACY)

ANCHOR_KINDS: tuple[str, ...] = ("SIG", "DATE", "INI")
# [[SIG:dealer:1]] — kind, party, ordinal.
ANCHOR_RE = re.compile(r"\[\[(SIG|DATE|INI):([a-z]+):(\d+)\]\]")
# Anything bracketed that survived stamping (a party never stamped, e.g. the
# funding party's joinder) — whited out at execution.
LEFTOVER_RE = re.compile(r"\[\[[A-Za-z]+:[a-z]+:\d+\]\]")

INK = (0.08, 0.15, 0.36)
SIGNATURE_FONT_SIZE = 11
DATE_FONT_SIZE = 8
INITIALS_FONT_SIZE = 9
SIGNATURE_MAX_HEIGHT = 30.0
SIGNATURE_MAX_WIDTH = 190.0
PAGE_RIGHT_MARGIN = 36.0


def _fitz():
    import fitz  # PyMuPDF — optional on dev boxes, present in the prod image

    return fitz


def anchor_token(kind: str, party: str, n: int) -> str:
    return f"[[{kind}:{party}:{n}]]"


def _page_anchors(page: Any) -> dict[str, list[Any]]:
    """Every anchor token on one page → its rects (a token normally appears once)."""
    found: dict[str, list[Any]] = {}
    text = page.get_text("text") or ""
    for token in sorted({m.group(0) for m in ANCHOR_RE.finditer(text)}):
        rects = page.search_for(token)
        if rects:
            found[token[2:-2]] = list(rects)
    return found


def find_anchors(pdf_bytes: bytes) -> dict[str, list[tuple[int, Any]]]:
    """``{"SIG:dealer:1": [(page_index, Rect), ...], ...}`` for every anchor
    token in the document's text layer."""
    fitz = _fitz()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    out: dict[str, list[tuple[int, Any]]] = {}
    for index, page in enumerate(doc):
        for key, rects in _page_anchors(page).items():
            out.setdefault(key, []).extend((index, r) for r in rects)
    return out


def has_template_anchors(pdf_bytes: bytes) -> bool:
    """True when the PDF carries at least one ``[[SIG:…]]`` token — the
    signal that the template scheme applies to this revision."""
    fitz = _fitz()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    for page in doc:
        if "[[SIG:" in (page.get_text("text") or ""):
            return True
    return False


def parties_in(anchors: dict[str, list[tuple[int, Any]]]) -> dict[str, dict[str, int]]:
    """``{"dealer": {"SIG": 2, "DATE": 1, "INI": 1}, ...}`` from :func:`find_anchors`."""
    out: dict[str, dict[str, int]] = {}
    for key, hits in anchors.items():
        kind, party, _n = key.split(":")
        out.setdefault(party, {})[kind] = out.get(party, {}).get(kind, 0) + len(hits)
    return out


def _image_size(fitz: Any, png: bytes) -> tuple[float, float] | None:
    try:
        pix = fitz.Pixmap(png)
        if pix.width and pix.height:
            return float(pix.width), float(pix.height)
    except Exception:  # noqa: BLE001 — a bad image falls back to the block rect
        return None
    return None


def _draw_signature(fitz: Any, page: Any, *, x: float, baseline: float, typed_name: str,
                    signature_png: bytes | None, max_width: float) -> None:
    """Draw the PNG left-aligned on the line (height ≤ 30pt, proportions
    kept) or the typed adoption in ink."""
    if signature_png:
        size = _image_size(fitz, signature_png)
        if size:
            w, h = size
            scale = min(max_width / w, SIGNATURE_MAX_HEIGHT / h)
            rect = fitz.Rect(x, baseline - h * scale, x + w * scale, baseline)
        else:
            rect = fitz.Rect(x, baseline - SIGNATURE_MAX_HEIGHT, x + max_width, baseline)
        page.insert_image(rect, stream=signature_png, keep_proportion=True)
    else:
        page.insert_text((x, baseline - 1), f"/s/ {typed_name}", fontname="helv", fontsize=SIGNATURE_FONT_SIZE, color=INK)


def _stamp_template(
    fitz: Any, doc: Any, *, party: str, typed_name: str, signature_png: bytes | None,
    signed_at: datetime | date, initials: str | None,
) -> dict[str, int]:
    when = signed_at.strftime("%B %d, %Y")
    counts = {"blocks": 0, "dates": 0, "initials": 0}
    for page in doc:
        anchors = _page_anchors(page)
        sig_hits = [r for key, rects in anchors.items() if key.startswith(f"SIG:{party}:") for r in rects]
        date_hits = [r for key, rects in anchors.items() if key.startswith(f"DATE:{party}:") for r in rects]
        ini_hits = [r for key, rects in anchors.items() if key.startswith(f"INI:{party}:") for r in rects] if initials else []
        if not (sig_hits or date_hits or ini_hits):
            continue
        width = page.rect.width
        # Clear first, then draw: a redaction applied after insertion would
        # take the freshly drawn signature with it.
        for r in sig_hits:
            page.add_redact_annot(fitz.Rect(r.x0 - 2, r.y0 - 26, min(r.x0 + SIGNATURE_MAX_WIDTH, width - PAGE_RIGHT_MARGIN), r.y1 + 3),
                                  fill=(1, 1, 1))
        for r in date_hits + ini_hits:
            page.add_redact_annot(fitz.Rect(r.x0 - 1, r.y0 - 1, r.x1 + 1, r.y1 + 1), fill=(1, 1, 1))
        page.apply_redactions()
        for r in sig_hits:
            max_width = max(24.0, min(SIGNATURE_MAX_WIDTH, width - PAGE_RIGHT_MARGIN - r.x0))
            _draw_signature(fitz, page, x=r.x0, baseline=r.y1 + 1, typed_name=typed_name,
                            signature_png=signature_png, max_width=max_width)
            counts["blocks"] += 1
        for r in date_hits:
            page.insert_text((r.x0, r.y1 + 1), when, fontname="helv", fontsize=DATE_FONT_SIZE, color=INK)
            counts["dates"] += 1
        for r in ini_hits:
            page.insert_text((r.x0, r.y1 + 1), initials or "", fontname="helv", fontsize=INITIALS_FONT_SIZE, color=INK)
            counts["initials"] += 1
    if not counts["blocks"]:
        raise ValueError(f"Signature block not found for party: {party}")
    return counts


def _stamp_legacy(
    fitz: Any, doc: Any, *, anchor: str, placeholder: str, date_placeholder: str,
    typed_name: str, signature_png: bytes | None, signed_at: datetime | date,
) -> dict[str, int]:
    """The heading-based layout, verbatim from the stage-one signer."""
    for page in doc:
        marks = page.search_for(anchor)
        if not marks:
            continue
        a = marks[0]

        def below(text: str, *, page: Any = page, a: Any = a) -> Any:
            cands = [r for r in page.search_for(text) if r.y0 > a.y1 - 2]
            return min(cands, key=lambda r: r.y0) if cands else None

        sig_rect = below(placeholder) or fitz.Rect(a.x0, a.y1 + 40, min(a.x0 + 210, page.rect.width - 260), a.y1 + 82)
        date_rect = below(date_placeholder) or fitz.Rect(page.rect.width - 230, sig_rect.y0, page.rect.width - 40, sig_rect.y1)
        for r in (sig_rect, date_rect):
            page.add_redact_annot(fitz.Rect(r.x0 - 2, r.y0 - 2, r.x1 + 2, r.y1 + 2), fill=(1, 1, 1))
        page.apply_redactions()
        sx, sy = sig_rect.x0, sig_rect.y1 - 2
        if signature_png:
            rect = fitz.Rect(sx, sy - 38, min(sx + 190, page.rect.width - 36), sy + 2)
            page.insert_image(rect, stream=signature_png, keep_proportion=True)
        else:
            page.insert_text((sx, sy), f"/s/ {typed_name}", fontname="helv", fontsize=SIGNATURE_FONT_SIZE, color=INK)
        when = signed_at.strftime("%B %d, %Y")
        page.insert_text((date_rect.x0, date_rect.y1 - 2), when, fontname="helv", fontsize=DATE_FONT_SIZE, color=INK)
        return {"blocks": 1, "dates": 1, "initials": 0}
    raise ValueError(f"Signature block not found: {anchor}")


def stamp_party(
    pdf_bytes: bytes,
    *,
    party: str,
    typed_name: str,
    signature_png: bytes | None,
    signed_at: datetime | date,
    initials: str | None = None,
    scheme: str = STAMP_SCHEME_TEMPLATE,
    legacy: dict | None = None,
) -> tuple[bytes, dict]:
    """Stamp one party's signature, date and initials onto every one of its
    blocks. Returns the new bytes and ``{"blocks", "dates", "initials"}``
    counts. Raises ``ValueError`` when the party has no signature block.

    ``legacy`` = ``{"anchor", "placeholder", "date_placeholder"}`` selects
    the heading-based layout (``scheme="legacy"``).
    """
    fitz = _fitz()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    if scheme == STAMP_SCHEME_LEGACY or legacy:
        spec = legacy or {}
        missing = [k for k in ("anchor", "placeholder", "date_placeholder") if not spec.get(k)]
        if missing:
            raise ValueError(f"legacy stamping needs {', '.join(missing)}")
        counts = _stamp_legacy(
            fitz, doc, anchor=spec["anchor"], placeholder=spec["placeholder"], date_placeholder=spec["date_placeholder"],
            typed_name=typed_name, signature_png=signature_png, signed_at=signed_at,
        )
    elif scheme == STAMP_SCHEME_TEMPLATE:
        counts = _stamp_template(
            fitz, doc, party=party, typed_name=typed_name, signature_png=signature_png, signed_at=signed_at,
            initials=(initials or "").strip() or None,
        )
    else:
        raise ValueError(f"Unknown stamping scheme: {scheme}")
    return doc.tobytes(deflate=True), counts


def redact_remaining_anchors(pdf_bytes: bytes) -> bytes:
    """White out every anchor token still standing (parties never stamped),
    so the executed copy carries no markers. Unchanged bytes when there is
    nothing to clear."""
    fitz = _fitz()
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    touched = False
    for page in doc:
        text = page.get_text("text") or ""
        tokens = sorted({m.group(0) for m in LEFTOVER_RE.finditer(text)})
        if not tokens:
            continue
        for token in tokens:
            for r in page.search_for(token):
                page.add_redact_annot(fitz.Rect(r.x0 - 1, r.y0 - 1, r.x1 + 1, r.y1 + 1), fill=(1, 1, 1))
                touched = True
        if touched:
            page.apply_redactions()
    return doc.tobytes(deflate=True) if touched else pdf_bytes
