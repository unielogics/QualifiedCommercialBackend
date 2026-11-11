"""Paginated, source-bound PPM PDFs using the existing production PDF engine."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.services.investor_presentation_pdf import _clean, _logo_image

WIDTH, HEIGHT = 612, 792
LEFT, RIGHT, TOP, BOTTOM = 42, 570, 94, 722
NAVY = (.04, .11, .23)
TEAL = (.04, .48, .44)
INK = (.09, .13, .19)
MUTE = (.37, .43, .51)
PALE = (.95, .97, .98)
AMBER = (.58, .36, .09)


def _text(value: Any) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", _clean(value)).strip()


def _wrap(value: Any, width: float, size: float = 10, font: str = "helv") -> list[str]:
    import fitz

    lines = []
    for paragraph in _text(value).splitlines() or [""]:
        current = ""
        for word in paragraph.split():
            candidate = f"{current} {word}".strip()
            if fitz.get_text_length(candidate, fontname=font, fontsize=size) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
                current = ""
            while fitz.get_text_length(word, fontname=font, fontsize=size) > width:
                cut = max(1, int(len(word) * width / fitz.get_text_length(word, fontname=font, fontsize=size)))
                while cut > 1 and fitz.get_text_length(word[:cut], fontname=font, fontsize=size) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            current = word
        lines.append(current)
    return lines


class _Pages:
    def __init__(self, document, *, title, version, status, stamp):
        self.document, self.title, self.version, self.status, self.stamp = document, title, version, status, stamp
        self.page = None
        self.current_section = None
        self.y = TOP
        self.new_page()

    def new_page(self):
        import fitz

        self.page = self.document.new_page(width=WIDTH, height=HEIGHT)
        self.page.draw_rect(fitz.Rect(0, 0, WIDTH, 66), fill=NAVY, color=NAVY)
        self.page.insert_image(fitz.Rect(LEFT, 16, LEFT + 31, 47), stream=_logo_image())
        self.page.insert_text((LEFT + 43, 28), "QUALIFIED COMMERCIAL", fontname="hebo", fontsize=10, color=(1, 1, 1))
        self.page.insert_text((LEFT + 43, 44), "PRIVATE PLACEMENT MEMORANDUM", fontsize=7.4, color=(.64, .85, .83))
        badge = "STAFF-REVIEWED VERSION" if self.status in {"ready", "reviewed", "approved"} else "DRAFT - STAFF REVIEW REQUIRED"
        self.page.insert_text((LEFT, 82), f"{badge}  |  Version {self.version}", fontname="hebo", fontsize=8, color=TEAL if self.status in {"ready", "reviewed", "approved"} else AMBER)
        self.y = TOP
        if self.current_section:
            heading = _wrap(f"{self.current_section} (continued)", RIGHT - LEFT, 9, "hebo")[0]
            self.page.insert_text((LEFT, self.y + 9), heading, fontname="hebo", fontsize=9, color=TEAL)
            self.y += 22

    def room(self, height):
        if self.y + height > BOTTOM:
            self.new_page()

    def line(self, value, *, size=10, font="helv", color=INK, indent=0, leading=None):
        leading = leading or size * 1.45
        self.room(leading)
        self.page.insert_text((LEFT + indent, self.y + size), value, fontname=font, fontsize=size, color=color)
        self.y += leading

    def paragraph(self, value, *, size=10, font="helv", color=INK, indent=0, gap=8):
        for line in _wrap(value, RIGHT - LEFT - indent, size, font):
            self.line(line, size=size, font=font, color=color, indent=indent)
        self.y += gap

    def heading(self, value):
        lines = _wrap(value, RIGHT - LEFT, 12.5, "hebo")
        self.current_section = None
        self.room(len(lines) * 18 + 32)
        self.y += 9
        for line in lines:
            self.line(line, size=12.5, font="hebo", color=TEAL, leading=18)
        self.y += 5
        self.current_section = _text(value)

    def facts(self, facts):
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            label, value, note = _text(fact.get("label")), _text(fact.get("value")), _text(fact.get("note"))
            self.room(32)
            self.paragraph(label, size=8, font="hebo", color=MUTE, gap=1)
            self.paragraph(value or "Not confirmed", size=10, gap=3 if note else 8)
            if note:
                self.paragraph(note, size=8.5, color=MUTE, gap=8)

    def table(self, table):
        import fitz

        columns = list(table.get("columns") or [])[:6]
        if not columns:
            return
        width = (RIGHT - LEFT) / len(columns)
        headers = [_wrap(value, width - 12, 8, "hebo") for value in columns]
        header_height = max(map(len, headers)) * 12 + 12

        def header():
            self.room(header_height + 24)
            self.page.draw_rect(fitz.Rect(LEFT, self.y, RIGHT, self.y + header_height), fill=PALE, color=PALE)
            for col, lines in enumerate(headers):
                for row, line in enumerate(lines):
                    self.page.insert_text((LEFT + col * width + 6, self.y + 12 + row * 12), line, fontname="hebo", fontsize=8, color=INK)
            self.y += header_height

        header()
        for raw in table.get("rows") or []:
            if not isinstance(raw, (list, tuple)):
                continue
            cells = [_wrap(raw[col] if col < len(raw) else "", width - 12, 8.5) for col in range(len(columns))]
            rows = max(map(len, cells))
            offset = 0
            while offset < rows:
                if self.y + 25 > BOTTOM:
                    self.new_page()
                    header()
                count = min(rows - offset, max(1, int((BOTTOM - self.y - 10) / 12)))
                for col, lines in enumerate(cells):
                    for row, line in enumerate(lines[offset:offset + count]):
                        self.page.insert_text((LEFT + col * width + 6, self.y + 12 + row * 12), line, fontsize=8.5, color=INK)
                self.y += count * 12 + 10
                self.page.draw_line(fitz.Point(LEFT, self.y), fitz.Point(RIGHT, self.y), color=(.84, .88, .91), width=.5)
                offset += count
        self.y += 10

    def finish(self):
        import fitz

        count = len(self.document)
        for index, page in enumerate(self.document, 1):
            page.draw_line(fitz.Point(LEFT, 744), fitz.Point(RIGHT, 744), color=(.78, .84, .89), width=.6)
            short_title = _wrap(self.title, 315, 7.5)[0]
            page.insert_text((LEFT, 758), short_title, fontsize=7.5, color=MUTE)
            page.insert_text((LEFT, 772), f"CONFIDENTIAL  |  Generated {self.stamp}", fontsize=7, color=MUTE)
            page.insert_text((RIGHT - 63, 772), f"Page {index} of {count}", fontsize=7, color=MUTE)


def render_ppm_pdf(document: dict, *, version: int, status: str = "draft", generated_at=None, reviewed_at=None) -> bytes:
    """Render formatted saved facts; no model, network, or financial math here."""
    import fitz

    if not isinstance(document, dict):
        raise ValueError("A saved PPM document is required")
    title = _text(document.get("title") or "Private placement memorandum")
    stamp = generated_at or datetime.now(UTC)
    stamp_label = stamp.strftime("%Y-%m-%d %H:%M UTC") if hasattr(stamp, "strftime") else _text(stamp)
    pdf = fitz.open()
    try:
        pages = _Pages(pdf, title=title, version=version, status=status, stamp=stamp_label)
        pages.paragraph(title, size=22, font="hebo", gap=13)
        issuer = document.get("issuer")
        if isinstance(issuer, dict):
            issuer = issuer.get("legal_name") or issuer.get("name")
        pages.paragraph(f"Issuer: {_text(issuer) or 'Not confirmed'}", size=11, gap=6)
        if document.get("listing_ref"):
            pages.paragraph(f"Offering reference: {_text(document['listing_ref'])}", size=9, color=MUTE, gap=12)
        offering = document.get("offering_name") or next((fact.get("value") for section in document.get("sections") or []
            if isinstance(section, dict) for fact in section.get("facts") or []
            if isinstance(fact, dict) and fact.get("label") == "Offering / business"), None)
        if offering and offering != "Not supplied":
            pages.paragraph(f"Offering: {_text(offering)}", size=11, font="hebo", gap=12)
        pages.paragraph("This version is generated from the saved offering record. Its financial figures and documented assumptions are identified below. Staff review applies to this exact version; no filing, exemption qualification or legal approval is established by generation.", size=9, color=MUTE, gap=12)
        if reviewed_at:
            pages.paragraph(f"Staff review recorded: {_text(reviewed_at)}", size=9, color=TEAL)
        missing = document.get("missing") or []
        if missing:
            pages.heading("Items requiring confirmation")
            for item in missing:
                label = item.get("label") or item.get("key") if isinstance(item, dict) else item
                pages.paragraph(f"- {_text(label)}", size=9, color=AMBER, indent=6, gap=4)
        for warning in document.get("warnings") or []:
            pages.paragraph(f"Review note: {_text(warning)}", size=9, color=AMBER, gap=6)
        for section in document.get("sections") or []:
            if not isinstance(section, dict):
                continue
            if not any(_text(value) for value in section.get("paragraphs") or []) and not section.get("facts") and not (section.get("table") or {}).get("rows"):
                continue
            pages.heading(section.get("title") or section.get("key") or "Offering detail")
            for paragraph in section.get("paragraphs") or []:
                pages.paragraph(paragraph)
            pages.facts(section.get("facts") or [])
            if isinstance(section.get("table"), dict):
                pages.table(section["table"])
        pages.finish()
        pdf.set_metadata({"title": title, "author": "Qualified Commercial", "subject": "Source-bound private placement memorandum",
                          "keywords": f"PPM, saved offering facts, versioned review, source {_text(document.get('source_fingerprint'))}"})
        return pdf.tobytes(garbage=4, deflate=True)
    finally:
        pdf.close()
