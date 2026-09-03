"""Production Package agreement templates: the two print-ready HTML documents
extracted from the owner's design (``scripts/extract_production_agreements.py``)
and the pure helpers that fill them.

* ``commitment_v1`` -- Production Commitment and Capital Engagement Agreement
  (stage one, signed at approval);
* ``activation_v1`` -- Program Activation and Production Agreement (stage two,
  the closing instrument).

Every blank on a template is an empty leaf element carrying ``data-field``;
every checkbox carries ``data-check="<group>.<slug>"``; every signature,
signature-date and initials line carries an invisible anchor token
(``[[SIG:party:n]]`` / ``[[DATE:party:n]]`` / ``[[INI:party:n]]``) that the
PDF stamper locates in the text layer. ``manifest.json`` inventories all of
them and pins each template's sha256, so a template edited by hand without
re-running the extraction is refused at load time.

The module is pure: it reads the committed files, never the database or the
clock. Values are supplied by ``app.services.production_fields``.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, NavigableString, Tag

TEMPLATE_KEYS: tuple[str, ...] = ("commitment_v1", "activation_v1")
TEMPLATE_DIR = Path(__file__).resolve().parent
PARTIES: tuple[str, ...] = ("qc", "dealer", "sponsor", "fp", "rm")
ANCHOR_KINDS: tuple[str, ...] = ("SIG", "DATE", "INI")
ANCHOR_RE = re.compile(r"\[\[(SIG|DATE|INI):([a-z]+):(\d+)\]\]")
FOOTER_TOKEN = "{{FOOTER}}"


class TemplateIntegrityError(RuntimeError):
    """The template on disk does not match the sha256 pinned in the manifest."""


def anchor(kind: str, party: str, n: int) -> str:
    """The literal token placed on a template for a signature, date or initials line."""
    if kind not in ANCHOR_KINDS:
        raise ValueError(f"unknown anchor kind {kind!r}")
    return f"[[{kind}:{party}:{n}]]"


@functools.lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    return json.loads((TEMPLATE_DIR / "manifest.json").read_text(encoding="utf-8"))


def manifest() -> dict[str, Any]:
    """The template manifest (a copy; the cached original is never handed out)."""
    return copy.deepcopy(_manifest())


def template_entry(key: str) -> dict[str, Any]:
    templates = _manifest().get("templates") or {}
    if key not in templates:
        raise KeyError(f"unknown agreement template {key!r}; known: {sorted(templates)}")
    return templates[key]


@functools.lru_cache(maxsize=len(TEMPLATE_KEYS))
def _load(key: str) -> tuple[str, str]:
    entry = template_entry(key)
    raw = (TEMPLATE_DIR / f"{key}.html").read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if sha != entry["sha256"]:
        raise TemplateIntegrityError(
            f"template {key} on disk ({sha[:16]}) does not match the manifest ({entry['sha256'][:16]}); "
            "re-run scripts/extract_production_agreements.py"
        )
    return raw.decode("utf-8"), sha


def load_template(key: str) -> tuple[str, str]:
    """(html, sha256) of a template, verified against the manifest."""
    return _load(key)


def template_field_keys(key: str) -> list[str]:
    """Every ``data-field`` key on the template, in document order (read from the file, not the manifest)."""
    html, _ = _load(key)
    soup = BeautifulSoup(html, "html.parser")
    return [str(tag["data-field"]) for tag in soup.find_all(attrs={"data-field": True})]


def template_check_keys(key: str) -> list[str]:
    """Every ``data-check`` key on the template, in document order."""
    html, _ = _load(key)
    soup = BeautifulSoup(html, "html.parser")
    return [str(tag["data-check"]) for tag in soup.find_all(attrs={"data-check": True})]


def anchor_tokens(key: str) -> dict[str, list[str]]:
    """Per party, every anchor token the template carries: SIG and DATE pairs,
    then INI. Parties with no anchors on this template are omitted."""
    entry = template_entry(key)
    out: dict[str, list[str]] = {}
    for party in PARTIES:
        tokens: list[str] = []
        for n in range(1, int((entry.get("anchors") or {}).get(party, 0)) + 1):
            tokens.append(anchor("SIG", party, n))
            tokens.append(anchor("DATE", party, n))
        for n in range(1, int((entry.get("initials") or {}).get(party, 0)) + 1):
            tokens.append(anchor("INI", party, n))
        if tokens:
            out[party] = tokens
    return out


def strip_anchors(text: str) -> str:
    """Remove every anchor token from extracted text (the rendered-text record must not carry them)."""
    return ANCHOR_RE.sub("", text or "")


def _css_string(text: str) -> str:
    """A value safe inside a double-quoted CSS string."""
    return (text or "").replace("\\", "\\\\").replace('"', '\\"').replace("\r", " ").replace("\n", " ")


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    return str(value).replace("\r\n", "\n").replace("\r", "\n")


def _is_anchor(node: Any) -> bool:
    return isinstance(node, Tag) and "anc" in (node.get("class") or [])


def fill_template(key: str, values: dict[str, Any], checks: Iterable[str] | None = None, *, footer: str) -> str:
    """Fill a template: every ``[data-field]`` slot receives its value as text
    (HTML-escaped by construction; newlines become ``<br>``; missing keys print
    blank; anchors inside a slot survive), every ``[data-check]`` in ``checks``
    is ticked, ``{{FOOTER}}`` is replaced and the ``data-*`` hooks are stripped.

    Raises ``KeyError`` for a value or check key the template does not carry --
    a silently dropped figure on an agreement is worse than a crash.
    """
    html, _ = _load(key)
    entry = template_entry(key)
    known_fields = set(entry["fields"])
    known_checks = set(entry["checks"])
    unknown = sorted(str(k) for k in values if k not in known_fields)
    if unknown:
        raise KeyError(f"{key} has no slot for: {', '.join(unknown)}")
    ticked = {str(c) for c in (checks or ())}
    unknown_checks = sorted(ticked - known_checks)
    if unknown_checks:
        raise KeyError(f"{key} has no checkbox for: {', '.join(unknown_checks)}")

    soup = BeautifulSoup(html, "html.parser")
    for slot in soup.find_all(attrs={"data-field": True}):
        field = str(slot["data-field"])
        anchors = [node for node in list(slot.contents) if _is_anchor(node)]
        for node in list(slot.contents):
            node.extract()
        for i, line in enumerate(_as_text(values.get(field)).split("\n")):
            if i:
                slot.append(soup.new_tag("br"))
            if line:
                slot.append(NavigableString(line))
        for node in anchors:
            slot.append(node)
        del slot["data-field"]
    for box in soup.find_all(attrs={"data-check": True}):
        box["class"] = ["chk", "on"] if str(box["data-check"]) in ticked else ["chk"]
        del box["data-check"]
    return str(soup).replace(FOOTER_TOKEN, _css_string(footer))


def content_sha256(key: str, values: dict[str, Any], checks: Iterable[str] | None = None) -> str:
    """A hash of what was filled -- template sha256 + values + checks -- that
    never depends on how BeautifulSoup serialises the document."""
    _, template_sha = _load(key)
    payload = {
        "template": key,
        "template_sha256": template_sha,
        "values": {str(k): _as_text(v) for k, v in sorted(values.items(), key=lambda kv: str(kv[0]))},
        "checks": sorted(str(c) for c in (checks or ())),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()
