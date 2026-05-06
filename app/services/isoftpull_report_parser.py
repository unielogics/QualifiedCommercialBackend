"""Parse the iSoftPull report viewer HTML into structured data.

Bridge module — see services/isoftpull_session.py for context. Once
iSoftPull's API token grants Full Feed access, this scraper goes away
and we ingest the JSON directly.

The HTML is regular: every field renders as
    <div class="padXX w-auto">
        <span>Label:</span>
        <strong>Value</strong>
    </div>
…inside section blocks identified either by `<h3>` headers or by
`id="credit_score" / "trade_accounts" / "inquiries"` etc.

Strategy: do a couple of focused walks per section rather than one giant
pass. Costs a bit of redundancy but keeps each function easy to debug
and easy to delete when the bridge goes away.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from bs4 import BeautifulSoup, Tag

from app.services.isoftpull_session import (
    IsoftpullSessionError,
    get_session,
)

log = logging.getLogger(__name__)


@dataclass
class CreditScore:
    model: str
    score: int | None
    reason_codes: list[str] = field(default_factory=list)


@dataclass
class AddressRecord:
    period: str  # "current" | "previous"
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class EmploymentRecord:
    period: str
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class TradeAccount:
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class Inquiry:
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class IdentityRisk:
    ofac: dict[str, str] = field(default_factory=dict)
    mla: dict[str, str] = field(default_factory=dict)
    fraud_shield: dict[str, str] = field(default_factory=dict)


@dataclass
class ScrapedReport:
    # Identity
    personal_info: dict[str, str] = field(default_factory=dict)
    addresses: list[AddressRecord] = field(default_factory=list)
    employment: list[EmploymentRecord] = field(default_factory=list)

    # Credit signal
    scores: list[CreditScore] = field(default_factory=list)
    identity_risk: IdentityRisk = field(default_factory=IdentityRisk)
    inquiries: list[Inquiry] = field(default_factory=list)
    trade_accounts: list[TradeAccount] = field(default_factory=list)
    public_records: list[dict[str, str]] = field(default_factory=list)
    collections: list[dict[str, str]] = field(default_factory=list)

    # Convenience aliases for the gating layer
    fico_8: int | None = None
    fico_2: int | None = None
    vantage_4: int | None = None
    best_score: int | None = None
    best_score_model: str | None = None

    # Diagnostics
    raw_html_length: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── DOM helpers ────────────────────────────────────────────────────────────

_LABEL_VAL_CONTAINER = re.compile(r"^pad\d+\s+w-auto$")


def _normalize(s: str | None) -> str:
    if not s:
        return ""
    return re.sub(r"\s+", " ", s).strip()


def _label_key(span_text: str) -> str:
    return span_text.rstrip(":").strip().lower().replace(" ", "_")


def _extract_pairs(scope: Tag) -> dict[str, str]:
    """Collect every <span>Label:</span><strong>Value</strong> pair within
    the given DOM scope, returning {snake_label: value}. Empty values are
    skipped — iSoftPull renders blank rows for missing data and we don't
    want them cluttering the output."""
    out: dict[str, str] = {}
    # Containers like <div class="padXX w-auto"> wrap each pair.
    containers = scope.find_all(
        "div", class_=lambda c: bool(c) and bool(_LABEL_VAL_CONTAINER.match(c))
    )
    for box in containers:
        span = box.find("span")
        strong = box.find("strong")
        if not span or not strong:
            continue
        label = _normalize(span.get_text())
        value = _normalize(strong.get_text())
        if not label or not value:
            continue
        out[_label_key(label)] = value
    return out


def _section(soup: BeautifulSoup, section_id: str) -> Tag | None:
    # iSoftPull sections each have an `id` on their wrapping `reportInner` div.
    return soup.find("div", id=section_id)


def _section_by_header(soup: BeautifulSoup, header_text: str) -> Tag | None:
    """Some sections (Address History → Current/Previous, Identity Risk →
    OFAC/MLA/Fraud shield) use H3/H4 headers rather than IDs. Return the
    Tag containing the next sibling content, or None."""
    for tag in soup.find_all(re.compile(r"^h[1-6]$"), recursive=True):
        text = _normalize(tag.get_text())
        if text.lower() == header_text.lower():
            # Walk up to a reasonable container (parent reportInner / column)
            return tag.parent
    return None


# ── Section parsers ────────────────────────────────────────────────────────


def _parse_personal(soup: BeautifulSoup) -> dict[str, str]:
    sec = _section(soup, "personal_information")
    if sec is None:
        return {}
    return _extract_pairs(sec)


def _parse_period_block(scope: Tag, period_label: str) -> dict[str, str]:
    """Inside an Address History or Employment History section, find the
    column whose H4 header matches `period_label` (e.g. "Current" /
    "Previous") and extract its label/value pairs."""
    headers = scope.find_all(re.compile(r"^h[1-6]$"))
    for h in headers:
        if _normalize(h.get_text()).lower() == period_label.lower():
            # Walk forward until the next header — that span is this period's content.
            block = []
            for sib in h.next_siblings:
                if getattr(sib, "name", None) and re.match(r"^h[1-6]$", sib.name):
                    break
                block.append(sib)
            if not block:
                # Sometimes the column is the parent itself
                block = [h.parent]
            wrapper = BeautifulSoup("<div></div>", "html.parser").div
            for b in block:
                wrapper.append(b if isinstance(b, Tag) else BeautifulSoup(str(b), "html.parser"))
            return _extract_pairs(wrapper)
    return {}


def _parse_addresses(soup: BeautifulSoup) -> list[AddressRecord]:
    sec = _section(soup, "address_history")
    if sec is None:
        return []
    out: list[AddressRecord] = []
    for period in ("Current", "Previous"):
        fields = _parse_period_block(sec, period)
        if fields:
            out.append(AddressRecord(period=period.lower(), fields=fields))
    return out


def _parse_employment(soup: BeautifulSoup) -> list[EmploymentRecord]:
    sec = _section(soup, "employment_history")
    if sec is None:
        return []
    out: list[EmploymentRecord] = []
    for period in ("Current", "Previous"):
        fields = _parse_period_block(sec, period)
        if fields:
            out.append(EmploymentRecord(period=period.lower(), fields=fields))
    return out


def _parse_credit_scores(soup: BeautifulSoup) -> list[CreditScore]:
    sec = _section(soup, "credit_score")
    if sec is None:
        return []
    scores: list[CreditScore] = []
    # Each score lives in its own ".div-row" block with two adjacent
    # label/value pairs (Score Model + Score) and an optional Reason Codes
    # block. Walk the div-row children in order.
    for row in sec.find_all("div", class_="div-row"):
        pairs = _extract_pairs(row)
        model = pairs.get("score_model")
        score_str = pairs.get("score")
        if not model:
            continue
        score: int | None = None
        if score_str and score_str.isdigit():
            score = int(score_str)
        # Reason codes — list of <strong> entries inside a div whose preceding
        # text is "Reason Codes:".
        reasons: list[str] = []
        for div in row.find_all("div"):
            txt = _normalize(div.get_text(separator="\n"))
            if "Reason Codes" in txt:
                # Pull only the <strong> children that aren't the score itself
                for s in div.find_all("strong"):
                    val = _normalize(s.get_text())
                    if val and val != score_str and val not in reasons:
                        reasons.append(val)
                break
        scores.append(CreditScore(model=model, score=score, reason_codes=reasons))
    return scores


def _parse_identity_risk(soup: BeautifulSoup) -> IdentityRisk:
    sec = _section(soup, "identity_risk")
    if sec is None:
        return IdentityRisk()
    risk = IdentityRisk()
    for sub_id, attr in (
        ("Ofac", "ofac"),
        ("Mla", "mla"),
        ("Fraud shield", "fraud_shield"),
    ):
        for h in sec.find_all(re.compile(r"^h[1-6]$")):
            if _normalize(h.get_text()).lower() == sub_id.lower():
                # All siblings until the next header form this sub-section.
                wrapper = BeautifulSoup("<div></div>", "html.parser").div
                for sib in h.next_siblings:
                    if getattr(sib, "name", None) and re.match(r"^h[1-6]$", sib.name):
                        break
                    if isinstance(sib, Tag):
                        wrapper.append(sib.__copy__())
                fields = _extract_pairs(wrapper)
                if fields:
                    setattr(risk, attr, fields)
                break
    return risk


def _parse_inquiries(soup: BeautifulSoup) -> list[Inquiry]:
    sec = _section(soup, "inquiries")
    if sec is None:
        return []
    inquiries: list[Inquiry] = []
    # Each inquiry is a <div class="column"> inside the section.
    for col in sec.find_all("div", class_="column"):
        fields = _extract_pairs(col)
        if fields:
            inquiries.append(Inquiry(fields=fields))
    return inquiries


def _parse_trade_accounts(soup: BeautifulSoup) -> list[TradeAccount]:
    sec = _section(soup, "trade_accounts")
    if sec is None:
        return []
    accounts: list[TradeAccount] = []
    # Each tradeline spans 2-4 .column blocks side-by-side. iSoftPull
    # doesn't put them in a wrapping container, so we detect a new account
    # by the appearance of a "Company:" pair — that's the first label.
    columns = sec.find_all("div", class_="column")
    if not columns:
        return []

    current: dict[str, str] = {}
    for col in columns:
        pairs = _extract_pairs(col)
        if not pairs:
            continue
        if "company" in pairs and current:
            accounts.append(TradeAccount(fields=current))
            current = {}
        current.update(pairs)
    if current:
        accounts.append(TradeAccount(fields=current))
    return accounts


def _parse_simple_list(soup: BeautifulSoup, section_id: str) -> list[dict[str, str]]:
    sec = _section(soup, section_id)
    if sec is None:
        return []
    out: list[dict[str, str]] = []
    for col in sec.find_all("div", class_="column"):
        pairs = _extract_pairs(col)
        if pairs:
            out.append(pairs)
    return out


# ── Top-level entry points ─────────────────────────────────────────────────


def parse_report_html(html: str) -> ScrapedReport:
    """Parse a complete iSoftPull report viewer HTML into ScrapedReport."""
    soup = BeautifulSoup(html, "html.parser")
    report = ScrapedReport(raw_html_length=len(html))

    try:
        report.personal_info = _parse_personal(soup)
    except Exception:  # noqa: BLE001
        log.exception("personal_information parse failed")
    try:
        report.addresses = _parse_addresses(soup)
    except Exception:  # noqa: BLE001
        log.exception("address_history parse failed")
    try:
        report.employment = _parse_employment(soup)
    except Exception:  # noqa: BLE001
        log.exception("employment_history parse failed")
    try:
        report.scores = _parse_credit_scores(soup)
    except Exception:  # noqa: BLE001
        log.exception("credit_score parse failed")
    try:
        report.identity_risk = _parse_identity_risk(soup)
    except Exception:  # noqa: BLE001
        log.exception("identity_risk parse failed")
    try:
        report.inquiries = _parse_inquiries(soup)
    except Exception:  # noqa: BLE001
        log.exception("inquiries parse failed")
    try:
        report.trade_accounts = _parse_trade_accounts(soup)
    except Exception:  # noqa: BLE001
        log.exception("trade_accounts parse failed")
    try:
        report.public_records = _parse_simple_list(soup, "public_records")
    except Exception:  # noqa: BLE001
        log.exception("public_records parse failed")
    try:
        report.collections = _parse_simple_list(soup, "collections")
    except Exception:  # noqa: BLE001
        log.exception("collections parse failed")

    # Score summary aliases
    for s in report.scores:
        m = s.model.upper().replace(" ", "_")
        if m == "FICO_8" and report.fico_8 is None:
            report.fico_8 = s.score
        elif m == "FICO_2" and report.fico_2 is None:
            report.fico_2 = s.score
        elif m == "VANTAGE_4" and report.vantage_4 is None:
            report.vantage_4 = s.score

    if report.fico_8 is not None:
        report.best_score, report.best_score_model = report.fico_8, "fico_8"
    elif report.fico_2 is not None:
        report.best_score, report.best_score_model = report.fico_2, "fico_2"
    elif report.vantage_4 is not None:
        report.best_score, report.best_score_model = report.vantage_4, "vantage_4"

    return report


async def fetch_and_parse(report_link: str) -> ScrapedReport | None:
    """Fetch a report URL via the cached dashboard session and parse.

    Returns None when the parser couldn't load the page at all (session
    failure / network / non-200). A parsed report with empty fields is
    still a valid result and gets returned — callers should check
    `best_score is None` to detect a parser break, not the return value.
    """
    if not report_link:
        return None
    try:
        session = get_session()
        resp = await session.fetch(report_link)
    except IsoftpullSessionError as exc:
        log.warning("iSoftPull session unavailable while scraping report: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001
        log.warning("Network error while scraping iSoftPull report: %s", exc)
        return None

    if resp.status_code != 200:
        log.warning(
            "iSoftPull report fetch returned %d for %s", resp.status_code, report_link
        )
        return None

    return parse_report_html(resp.text)
