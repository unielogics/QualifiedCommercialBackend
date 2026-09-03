#!/usr/bin/env python
"""Extract the two Production Package agreement templates from the owner's
Claude Design artifacts and commit them to the tree.

    .venv/bin/python scripts/extract_production_agreements.py [ARTIFACT_DIR]

Inputs are the local copies of the design artifacts (stage one *Production
Commitment and Capital Engagement Agreement*, stage two *Program Activation and
Production Agreement*). Each artifact embeds the document as one JSON string
literal holding an ``<x-dc>`` element; this script pulls that document out and
turns it into a print-ready template WeasyPrint can render on the prod image:

1. the ``<helmet>`` (Google Fonts ``@font-face``, preconnects, the bundle
   script) is dropped; the second ``<style>`` (the document CSS) is kept minus
   the custom-element visibility guard, and a Letter ``@page`` rule with the
   branded footer is prepended;
2. ``<doc-page size="letter">`` becomes ``<body class="doc">`` of a full HTML
   document; ``sc-raw-table|thead|tbody|tr|th|td`` become real table tags and
   ``sc-camel-view-box`` becomes ``viewBox``;
3. the design fonts fall back to DejaVu / Arial (the prod image carries
   ``fonts-dejavu-core`` only);
4. the sponsor logo ``<img>`` (a bundle asset) becomes a text slot,
   ``data-field="sponsor_logo_text"``, printing the sponsor's legal name;
5. every checkbox span is keyed ``class="chk" data-check="<group>.<slug>"``
   from an explicit label table -- an unexpected label aborts the run;
6. signature, date and initials anchors (``[[SIG:party:n]]`` etc.) are placed
   for the PDF stamper, invisible in print but present in the text layer;
7. page breaks are added before every Schedule / Addendum / signature page
   caption, tables and signature grids never split, headings stay with their
   body.

Outputs ``app/services/production_agreements/{commitment_v1,activation_v1}.html``
and ``manifest.json`` (sha256, field / check inventories, anchor counts). The
script is deterministic and re-runnable; the outputs are committed.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "app" / "services" / "production_agreements"
DEFAULT_ARTIFACT_DIR = Path(
    "/home/ubuntu/.claude/projects/-home-ubuntu/5c67e146-a1cd-4d3f-8270-197ef660bb67/tool-results"
)
MANIFEST_VERSION = "2026-09-03-1"

ARCHIVO = 'Archivo, "DejaVu Sans", Arial, sans-serif'
PLEX = '"IBM Plex Sans", "DejaVu Sans", Arial, sans-serif'

PAGE_CSS = (
    '@page { size: Letter; margin: 0.55in 0.6in 0.65in; '
    '@bottom-left { content: "Qualified Commercial | {{FOOTER}}"; color:#667085; font-size:8px } '
    '@bottom-right { content: "Page " counter(page) " of " counter(pages); color:#667085; font-size:8px } }'
)
EXTRA_CSS = "\n".join([
    '.chk.on::after{content:"\\2713";font-size:9px;line-height:11px;display:block;text-align:center}',
    ".anc{color:#fff;font-size:3px;line-height:0}",
    ".pb{page-break-before:always}",
    "table,.keep{page-break-inside:avoid}",
    "h2,h3{break-after:avoid}",
])

CHECK_GROUPS: dict[str, tuple[tuple[str, str], ...]] = {
    "products": (
        ("vsc", "Vehicle service contracts"),
        ("gap", "GAP products"),
        ("theft", "Anti-theft products"),
        ("appearance", "Appearance protection"),
        ("key", "Key replacement"),
        ("tire", "Tire and wheel"),
        ("maint", "Maintenance products"),
        ("power", "Powertrain products"),
        ("other", "Other"),
    ),
    "support": (
        ("application_packaging", "Application and packaging support"),
        ("reporting_technology", "Reporting technology"),
        ("ongoing_monitoring", "Ongoing monitoring"),
        ("first_risk_reserve", "First-risk or reserve support"),
        ("capital_health", "Capital Health Services"),
        ("controlled_account", "Controlled-account support"),
        ("product_admin_platform", "Product-administration platform"),
        ("preferential_economics", "Preferential program economics"),
        ("other", "Other"),
    ),
    "rm_comp": (
        ("salary", "Salary"),
        ("fixed_recurring", "Fixed recurring account-management compensation"),
        ("hourly", "Hourly compensation"),
        ("disclosed_product", "Disclosed Covered Product sales or servicing compensation"),
        ("fixed_implementation", "Fixed implementation compensation for documented services"),
        ("other", "Other lawful compensation"),
    ),
    "financing_cost": (
        ("no", "No"),
        ("yes", "Yes"),
    ),
    "sba": (
        ("not_sba", "Not an SBA transaction"),
        ("sba", "SBA transaction; required SBA compensation documentation attached"),
    ),
}

SOURCES: dict[str, dict[str, Any]] = {
    "commitment_v1": {
        "artifact": "artifact-633d324a-1788403760-21c1.html",
        "title": "Production Commitment and Capital Engagement Agreement",
        "source_fields": 147,
        "check_groups": ("products", "support", "rm_comp", "financing_cost", "sba"),
        "captions": 6,   # Schedules A-E + Signature Page
        "signature_blocks": 3,
    },
    "activation_v1": {
        "artifact": "artifact-2dd2d613-1788403699-c6f9.html",
        "title": "Program Activation and Production Agreement",
        "source_fields": 150,
        "check_groups": ("support", "rm_comp", "financing_cost", "sba"),
        "captions": 7,   # Addendum A + Schedules 1-5 + Master Signature Page
        "signature_blocks": 7,
    },
}

PARTIES = ("qc", "dealer", "sponsor", "fp", "rm")
PARTY_HEADERS = {"qualified commercial llc": "qc", "dealer": "dealer", "sponsor": "sponsor"}
INITIALS_LABELS = {"qc initials": "qc", "dealer initials": "dealer", "sponsor initials": "sponsor"}
SIGNATURE_LABELS = ("by (signature)", "signature")
CAPTION_RE = re.compile(r"^(Schedule [A-E1-5]|Addendum A|Signature Page|Master Signature Page)$")
SC_RAW = {"sc-raw-table": "table", "sc-raw-thead": "thead", "sc-raw-tbody": "tbody",
          "sc-raw-tr": "tr", "sc-raw-th": "th", "sc-raw-td": "td"}
XDC_LITERAL = re.compile(r'"(?:[^"\\]|\\.)*?x-dc(?:[^"\\]|\\.)*?"')

DOCUMENT = (
    "<!doctype html>\n<html><head><meta charset=\"utf-8\"><title>{title}</title>\n"
    "<style>\n{css}\n</style></head>\n<body class=\"doc\">{body}</body></html>\n"
)


class ExtractionError(SystemExit):
    def __init__(self, message: str) -> None:
        super().__init__(f"extract_production_agreements: {message}")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def extract_xdc(raw: str) -> str:
    """The longest JSON string literal mentioning x-dc, decoded, clipped to the element."""
    candidates = [m.group(0) for m in XDC_LITERAL.finditer(raw) if len(m.group(0)) > 2000]
    if not candidates:
        raise ExtractionError("no x-dc JSON literal found in the artifact")
    decoded = json.loads(max(candidates, key=len))
    start = decoded.find("<x-dc")
    end = decoded.rfind("</x-dc>")
    if start < 0 or end < 0:
        raise ExtractionError("the decoded literal does not contain an <x-dc> element")
    return decoded[start:end + len("</x-dc>")]


def fix_fonts(css: str) -> str:
    css = css.replace("'Archivo',sans-serif", ARCHIVO)
    css = css.replace("'IBM Plex Sans',system-ui,sans-serif", PLEX)
    css = css.replace("'Archivo'", ARCHIVO).replace("'IBM Plex Sans'", PLEX)
    return css


def style_dict(style: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in style.split(";"):
        if ":" not in part:
            continue
        prop, _, value = part.partition(":")
        out[prop.strip().lower()] = re.sub(r"\s+", "", value.strip().lower())
    return out


def is_checkbox(tag: Tag) -> bool:
    if tag.name != "span" or not tag.get("style") or tag.contents:
        return False
    st = style_dict(tag["style"])
    return (
        st.get("width") == "11px" and st.get("height") == "11px"
        and st.get("border") == "1pxsolid#0b1d3a" and st.get("display") == "inline-block"
    )


def own_text(tag: Tag) -> str:
    """Text of an element that is meant to be a leaf label."""
    return norm(tag.get_text(" "))


def add_class(tag: Tag, name: str) -> None:
    classes = tag.get("class") or []
    if name not in classes:
        tag["class"] = [*classes, name]


def anchor_span(soup: BeautifulSoup, token: str) -> Tag:
    span = soup.new_tag("span")
    span["class"] = ["anc"]
    span.string = token
    return span


def underline_before(label: Tag) -> Tag:
    """The blank underline div that sits right above a caption div."""
    line = label.find_previous_sibling("div")
    if line is None or line.get_text(strip=True) or "border-bottom" not in (line.get("style") or ""):
        raise ExtractionError(f"no underline before label {own_text(label)!r}")
    return line


# ---------------------------------------------------------------------------
# transforms
# ---------------------------------------------------------------------------

def key_checkboxes(docpage: Tag, groups: tuple[str, ...]) -> list[str]:
    expected = [(group, slug, label) for group in groups for slug, label in CHECK_GROUPS[group]]
    spans = [t for t in docpage.find_all("span") if is_checkbox(t)]
    if len(spans) != len(expected):
        raise ExtractionError(f"expected {len(expected)} checkboxes, found {len(spans)}")
    keys: list[str] = []
    for span, (group, slug, label) in zip(spans, expected, strict=True):
        found = own_text(span.parent)
        if found != label:
            raise ExtractionError(f"checkbox label {found!r} where {label!r} ({group}.{slug}) was expected")
        span["class"] = ["chk"]
        span["data-check"] = f"{group}.{slug}"
        keys.append(f"{group}.{slug}")
    return keys


def party_for_signature(cell: Tag) -> tuple[str, Tag]:
    """The party a "By (signature)" cell belongs to and the container holding its Date slot."""
    column = cell.parent
    block = column.parent
    head = block.find("div", recursive=False) if isinstance(block, Tag) else None
    if head is not None and own_text(head).lower() in PARTY_HEADERS:
        return PARTY_HEADERS[own_text(head).lower()], column
    if column.find(attrs={"data-field": "s2_ack_name"}) is not None:
        return "rm", column
    heading = column.find_previous_sibling("div")
    if heading is not None and "funding party acknowledgment" in own_text(heading).lower():
        return "fp", column
    raise ExtractionError("could not attribute a signature block to a party")


def date_slot(container: Tag) -> Tag:
    for cell in container.find_all("div", recursive=False):
        label = cell.find_all("div", recursive=False)
        if len(label) >= 2 and own_text(label[-1]).lower() == "date":
            slot = label[0]
            if not slot.get("data-field"):
                raise ExtractionError("the Date slot next to a signature has no data-field")
            return slot
    raise ExtractionError("no Date slot beside a signature block")


def place_anchors(soup: BeautifulSoup, docpage: Tag) -> tuple[dict[str, int], dict[str, int]]:
    sig: dict[str, int] = defaultdict(int)
    ini: dict[str, int] = defaultdict(int)
    for label in docpage.find_all("div"):
        text = own_text(label).lower() if label.string is not None else ""
        if text in SIGNATURE_LABELS:
            line = underline_before(label)
            party, container = party_for_signature(label.parent)
            sig[party] += 1
            n = sig[party]
            line.append(anchor_span(soup, f"[[SIG:{party}:{n}]]"))
            date_slot(container).append(anchor_span(soup, f"[[DATE:{party}:{n}]]"))
            grid = container.parent if party in PARTY_HEADERS.values() else container
            add_class(grid, "keep")
        elif text in INITIALS_LABELS:
            line = underline_before(label)
            party = INITIALS_LABELS[text]
            ini[party] += 1
            line.append(anchor_span(soup, f"[[INI:{party}:{ini[party]}]]"))
            add_class(label.parent.parent, "keep")
    return {p: sig.get(p, 0) for p in PARTIES}, {p: ini.get(p, 0) for p in PARTIES}


def page_breaks(docpage: Tag) -> int:
    count = 0
    for div in docpage.find_all("div"):
        if div.string is None or not CAPTION_RE.match(own_text(div)):
            continue
        block = div.parent
        if "border-top" not in (block.get("style") or ""):
            continue
        add_class(block, "pb")
        count += 1
    return count


def replace_sponsor_logo(soup: BeautifulSoup, docpage: Tag) -> None:
    images = docpage.find_all("img")
    if len(images) != 1:
        raise ExtractionError(f"expected exactly one sponsor <img>, found {len(images)}")
    span = soup.new_tag("span")
    span["data-field"] = "sponsor_logo_text"
    # The wrapper paints the sponsor badge navy (#0B1D3A); the name prints white on it.
    span["style"] = f"font-family:{ARCHIVO};font-weight:700;font-size:10px;line-height:1.3;color:#fff"
    images[0].replace_with(span)


def build(key: str, spec: dict[str, Any], artifact_dir: Path) -> tuple[str, dict[str, Any]]:
    artifact = artifact_dir / spec["artifact"]
    if not artifact.exists():
        raise ExtractionError(f"artifact not found: {artifact}")
    xdc = extract_xdc(artifact.read_text(encoding="utf-8"))
    soup = BeautifulSoup(xdc, "html.parser")
    root = soup.find("x-dc")
    if root is None:
        raise ExtractionError("no <x-dc> root")

    source_fields = [t["data-field"] for t in root.find_all(attrs={"data-field": True})]
    if len(source_fields) != spec["source_fields"] or len(set(source_fields)) != len(source_fields):
        raise ExtractionError(f"{key}: expected {spec['source_fields']} unique data-field slots, "
                              f"found {len(source_fields)} ({len(set(source_fields))} unique)")

    helmet = root.find("helmet")
    if helmet is None:
        raise ExtractionError("no <helmet>")
    styles = helmet.find_all("style")
    if len(styles) != 2:
        raise ExtractionError(f"expected two <style> blocks in the helmet, found {len(styles)}")
    doc_css = styles[1].get_text()
    if "@font-face" in doc_css:
        raise ExtractionError("the document CSS unexpectedly carries @font-face rules")
    doc_css = doc_css.replace("doc-page:not(:defined){visibility:hidden}", "")
    doc_css = doc_css.replace("body,doc-page{", "body{")
    helmet.decompose()

    docpage = root.find("doc-page")
    if docpage is None:
        raise ExtractionError("no <doc-page>")

    for tag in docpage.find_all(list(SC_RAW)):
        tag.name = SC_RAW[tag.name]
    for svg in docpage.find_all("svg"):
        if svg.get("sc-camel-view-box"):
            svg["viewBox"] = svg["sc-camel-view-box"]
            del svg["sc-camel-view-box"]
    for tag in docpage.find_all(style=True):
        tag["style"] = fix_fonts(tag["style"])

    replace_sponsor_logo(soup, docpage)
    checks = key_checkboxes(docpage, spec["check_groups"])
    anchors, initials = place_anchors(soup, docpage)
    if sum(anchors.values()) != spec["signature_blocks"] + 1:  # + the RM acknowledgment
        raise ExtractionError(f"{key}: expected {spec['signature_blocks'] + 1} signature anchors, got {anchors}")
    if initials != {"qc": 1, "dealer": 2, "sponsor": 1, "fp": 0, "rm": 0}:
        raise ExtractionError(f"{key}: unexpected initials layout {initials}")
    captions = page_breaks(docpage)
    if captions != spec["captions"]:
        raise ExtractionError(f"{key}: expected {spec['captions']} page-break captions, marked {captions}")

    css = "\n".join([PAGE_CSS, fix_fonts(doc_css).strip(), EXTRA_CSS])
    html = DOCUMENT.format(title=spec["title"], css=css, body=docpage.decode_contents())

    for forbidden in ("<sc-raw-", "@font-face", "<img", "<helmet", "<doc-page", "<script", "<link", "sc-camel-view-box"):
        if forbidden in html:
            raise ExtractionError(f"{key}: {forbidden!r} survived the transform")

    check = BeautifulSoup(html, "html.parser")
    fields = [t["data-field"] for t in check.find_all(attrs={"data-field": True})]
    if [f for f in fields if f != "sponsor_logo_text"] != source_fields or len(fields) != len(source_fields) + 1:
        raise ExtractionError(f"{key}: {len(fields)} slots after the transform, expected {spec['source_fields'] + 1}")
    if [t["data-check"] for t in check.find_all(attrs={"data-check": True})] != checks:
        raise ExtractionError(f"{key}: the check inventory did not round-trip")
    for slot in check.find_all(attrs={"data-field": True}):
        stray = [c for c in slot.contents if not (isinstance(c, Tag) and c.get("class") == ["anc"])]
        if stray:
            raise ExtractionError(f"{key}: slot {slot['data-field']} is not an empty leaf")

    entry = {
        "sha256": hashlib.sha256(html.encode("utf-8")).hexdigest(),
        "fields": fields,
        "checks": checks,
        "anchors": anchors,
        "initials": initials,
        "source_artifact": spec["artifact"],
    }
    return html, entry


def main(argv: list[str]) -> int:
    artifact_dir = Path(argv[1]) if len(argv) > 1 else DEFAULT_ARTIFACT_DIR
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {"templates": {}, "version": MANIFEST_VERSION}
    for key, spec in SOURCES.items():
        html, entry = build(key, spec, artifact_dir)
        (OUT_DIR / f"{key}.html").write_text(html, encoding="utf-8")
        manifest["templates"][key] = entry
        print(f"{key}: {len(entry['fields'])} fields, {len(entry['checks'])} checks, "
              f"anchors {entry['anchors']}, initials {entry['initials']}, sha256 {entry['sha256'][:16]}")
    (OUT_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT_DIR / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
