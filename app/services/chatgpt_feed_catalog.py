"""Public-only discovery projection. Financing terms are never a shopping price.

This module deliberately does not query applications, borrower evidence, the
legacy DOS catalog, or static marketing fallbacks. Missing source data stops an
export rather than resurrecting a retired or unapproved offer.
"""
from __future__ import annotations

import csv
import gzip
import hashlib
import io
import ipaddress
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy.ext.asyncio import AsyncSession

from app.services import funding_programs

SITE = "https://qualifiedcommercial.com"
SCHEMA_VERSION = "openai-products-stable-v1"
EMPTY_CATALOG = "The approved funding catalog is empty; review the source before replacing the full snapshot."
NO_SELECTED = "Select at least one approved offer; an empty snapshot will not be uploaded."
FIELDS = (
    "item_id", "title", "description", "url", "brand", "seller_name",
    "image_url", "availability", "price", "is_eligible_search", "is_eligible_checkout",
)
# Only verified product detail routes get defaults. Unknown products need an
# explicit canonical mapping; a generic application link is not a detail page.
PRODUCT_PATHS = {
    "merchant_cash_advance": "/merchant-cash-advance",
    "mca_refinance": "/mca-refinance",
    "ez_term": "/ez-term-loan",
    "quidity_10_year_term": "/programs/10-year-term-loan",
    "capital_os": "/audit",
    "sba_7a": "/programs/sba-7a",
    "sba_504": "/programs/sba-504",
    "sba_express": "/programs/sba-express",
    "foreclosure_bailout": "/foreclosure-bailout",
    "dscr": "/programs/dscr-rental",
    "fix_flip": "/programs/fix-and-flip",
    "bridge_purchase": "/programs/bridge",
    "construction_capital": "/programs/construction",
    "portfolio_lending": "/programs/portfolio",
    "dealer_working_capital": "/programs/dealer-working-capital",
    "dealer_real_estate_capital": "/programs/dealer-real-estate-backed",
    "floorplan_support": "/programs/floorplan-support",
    "revenue_based_financing": "/revenue-based-financing",
    "microcap": "/micro-loan",
    "line_of_credit": "/programs/line-of-credit",
    "equipment_financing": "/equipment-financing",
    "jumbo_term": "/programs/jumbo-term-loan",
    "hybrid_term_loc": "/programs/hybrid-term-loc",
    "transportation_finance": "/programs/transportation-finance",
    "sba_grocery": "/programs/sba-grocery",
    "sba_made_in_america": "/programs/sba-made-in-america",
}
PRIORITY_COPY = {
    "merchant_cash_advance": (
        "Merchant Cash Advance — Working Capital",
        "Business-purpose working capital with QC's internal underwriting and cash-flow review. "
        "Repayment, remittance and factor pricing are disclosed with the specific offer; "
        "a factor rate is not an interest rate or APR. Funding is subject to qualification and final terms.",
    ),
    "mca_refinance": (
        "MCA Refinance",
        "Review existing merchant cash advances, current payoffs and business cash flow to assess "
        "a structured replacement. QC reconciles the obligations and models the proposed payment. "
        "Refinancing and payment savings depend on qualification and final terms.",
    ),
    "ez_term": (
        "EZ Term Business Financing",
        "Business term financing supported by QC's internal underwriting, financial analysis and "
        "a coordinated application file. Amount, pricing, repayment and eligibility depend on "
        "the published program and final underwriting.",
    ),
    "quidity_10_year_term": (
        "10-Year Business Term Loan",
        "Explore longer-term business financing with QC's financial review and application support. "
        "The team reviews cash flow, existing debt and use of funds before presenting a qualifying "
        "file. Final amount, rate, maturity and approval are determined through underwriting.",
    ),
    "capital_os": (
        "Capital OS — Funding Readiness and Financial Consulting",
        "Software, financial monitoring and working sessions with QC's team to organize evidence, "
        "review operating metrics and develop a practical funding-readiness plan. Scope and billing "
        "are confirmed with the team. This service does not guarantee financing or approval.",
    ),
}


def _dict(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return {}


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _https_url(value: str) -> bool:
    """Syntax only; transport verifies DNS, TLS and accessibility before upload."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (parsed.scheme != "https" or not host or parsed.username or parsed.password
                or parsed.fragment or parsed.query or parsed.port not in (None, 443)):
            return False
        if host.lower() == "localhost" or host.lower().endswith((".local", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return "." in host and bool(re.fullmatch(r"[a-zA-Z0-9.-]+", host))
    except (ValueError, TypeError):
        return False


def _money(value: Any) -> str | None:
    # Exact payable USD only: never round an amount into a different price.
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999.99"):
            return None
        if amount != amount.quantize(Decimal("0.01")):
            return None
        return f"{amount:.2f} USD"
    except (ValueError, InvalidOperation):
        return None


def _priority(key: str) -> int:
    if key == "merchant_cash_advance":
        return 10
    if key in {"ez_term", "quidity_10_year_term"}:
        return 20 if key == "ez_term" else 21
    if key == "capital_os":
        return 30
    if key.startswith("sba_"):
        return 40
    return 50


def catalog_preview(programs: list[Any], settings_data: dict) -> dict:
    config = _dict(settings_data)
    overrides = _dict(config.get("catalog_overrides"))
    source: dict[str, dict] = {}
    for value in programs:
        row = _dict(value)
        key = _text(row.get("program_key"))
        if not key or key.startswith("arf_") or row.get("status", "active") != "active":
            continue
        # The canonical public projection should be unique. Fail closed if it
        # is not, instead of silently choosing one of two conflicting offers.
        if key in source:
            raise ValueError("The approved funding catalog contains duplicate program identifiers.")
        source[key] = row
    funding_catalog_empty = not source
    source["capital_os"] = {
        "program_key": "capital_os", "public_slug": "audit", "name": PRIORITY_COPY["capital_os"][0],
        "short_description": PRIORITY_COPY["capital_os"][1],
        "service_reference": "/audit", "service_revision": "capital-os-existing-quoted-service-v1",
    }
    items = []
    for key, row in source.items():
        metadata = _dict(overrides.get(key))
        terms = _dict(row.get("commercial_terms"))
        published = terms.get("status") == "published"
        kind = "service" if key == "capital_os" else "financing"
        default_title, default_description = PRIORITY_COPY.get(key, (
            _text(row.get("name")), _text(row.get("short_description")),
        ))
        path = PRODUCT_PATHS.get(key)
        default_url = f"{SITE}{path}" if path else ""
        # Only known assets receive defaults; no invented image URL for a new
        # program which the website has not published yet.
        default_image = f"{SITE}/catalog-images/{key}.png" if path else ""
        title = _text(metadata.get("title")) or default_title
        description = _text(metadata.get("description")) or default_description
        url = _text(metadata.get("canonical_url")) or default_url
        image_url = _text(metadata.get("image_url")) or default_image
        # Hash only the approved public source. Superseded terms invalidate
        # previous staff approval even when the stable program key is unchanged.
        source_revision = hashlib.sha256(json.dumps(
            row, sort_keys=True, default=str, separators=(",", ":"),
        ).encode()).hexdigest()
        included = metadata.get("included", True) is True
        price = _money(metadata.get("actual_price"))
        issues = []
        if kind == "financing" and not published:
            issues.append("No published commercial terms; draft terms cannot be exported.")
        if not title or len(title) > 150:
            issues.append("Provide a product title of 1–150 characters.")
        if not description or len(description) > 5000:
            issues.append("Provide a factual description of 1–5,000 characters.")
        if "<" in description or ">" in description:
            issues.append("Use plain-text product descriptions.")
        if not _https_url(url):
            issues.append("Provide a public HTTPS product detail URL without credentials or query parameters.")
        if not _https_url(image_url):
            issues.append("Provide a public HTTPS PNG or JPEG image URL without credentials or query parameters.")
        if not metadata.get("content_approved"):
            issues.append("Review and approve the description, image, product page and claims.")
        elif metadata.get("approved_source_revision") != source_revision:
            issues.append("The source has changed; preview and approve the current published information again.")
        if price is None:
            issues.append("An actual positive purchase price is missing. Loan principal, APR and factor rates are not prices.")
        if not metadata.get("pricing_confirmed"):
            issues.append("Confirm OpenAI's accepted price treatment and the offer's actual payable price.")
        if not included:
            issues.append("Excluded from delivery by staff.")
        brand = _text(config.get("brand")) or "Qualified Commercial"
        seller = _text(config.get("seller_name")) or "Qualified Commercial"
        items.append({
            "item_id": key, "kind": kind, "priority": _priority(key), "title": title,
            "description": description, "url": url, "brand": brand, "seller_name": seller,
            "image_url": image_url, "availability": "unknown", "price": price,
            "included": included, "eligible": included and not issues, "issues": issues,
            "source_revision": source_revision,
            "commercial_terms_version": str(terms["version"]) if published and terms.get("version") is not None else None,
            "is_eligible_search": True, "is_eligible_checkout": False,
        })
    items.sort(key=lambda item: (item["priority"], item["title"], item["item_id"]))
    blockers = []
    if funding_catalog_empty:
        blockers.append(EMPTY_CATALOG)
    if not config.get("acceptance_confirmed") or not _text(config.get("acceptance_reference")):
        blockers.append("OpenAI must confirm acceptance and price treatment for financing and consulting offers before delivery.")
    included_items = [item for item in items if item["included"]]
    eligible_count = sum(item["eligible"] for item in items)
    if not included_items:
        blockers.append(NO_SELECTED)
    elif any(not item["eligible"] for item in included_items):
        blockers.append("Resolve the selected offers' validation issues or explicitly exclude them before delivery.")
    return {
        "schema_version": SCHEMA_VERSION, "generated_at": datetime.now(UTC).isoformat(),
        "source_count": len(items), "eligible_count": eligible_count,
        "excluded_count": len(items) - eligible_count, "ready_for_upload": not blockers and eligible_count > 0,
        "blockers": blockers, "items": items,
        "links_verified": False,
    }


async def build_preview(db: AsyncSession, settings_data: dict) -> dict:
    return catalog_preview(await funding_programs.public_catalog(db), settings_data)


def retirement_snapshot_preview(
    current: dict, previous: list[dict], config: dict, retired_ids: set[str],
) -> dict:
    """Disable withdrawn offers using old valid data, never infer removal from absence.

    A nonempty disable-only snapshot can remove the last offer, but an empty
    source response or an unapproved replacement cannot withdraw good records.
    """
    overrides = _dict(config.get("catalog_overrides"))
    active = {item["item_id"] for item in current["items"] if item.get("eligible")}
    disabled = []
    unproven = []
    for old in previous:
        key = old.get("item_id")
        if key in active:
            continue
        if (old.get("is_eligible_search") is False
                or _dict(overrides.get(key)).get("included") is False or key in retired_ids):
            disabled.append({**old, "eligible": True, "included": False,
                             "is_eligible_search": False, "is_eligible_checkout": False,
                             "issues": [], "retired_record": True})
        else:
            unproven.append(key)
    blocked = list(current.get("blockers", []))
    if unproven:
        blocked.append("Previously uploaded offers are absent or invalid without a confirmed retirement or explicit exclusion. Review the catalog before replacing it.")
    selected_invalid = any(item.get("included") and not item.get("eligible") for item in current["items"])
    accepted = config.get("acceptance_confirmed") and _text(config.get("acceptance_reference"))
    if disabled and not unproven and not selected_invalid and accepted:
        blocked = [message for message in blocked if message not in {EMPTY_CATALOG, NO_SELECTED}]
    disabled_ids = {item["item_id"] for item in disabled}
    items = [item for item in current["items"] if item["item_id"] not in disabled_ids] + disabled
    export_count = len(active) + len(disabled)
    return {**current, "items": items, "blockers": blocked,
            "disabled_count": len(disabled), "export_count": export_count,
            "ready_for_upload": bool(export_count) and not blocked}


def snapshot_bytes(preview: dict) -> bytes:
    if not preview.get("ready_for_upload"):
        raise ValueError("Resolve catalog validation and acceptance before downloading an import-ready snapshot.")
    rows = [item for item in preview.get("items", []) if item.get("eligible")]
    if not rows:
        raise ValueError("An empty snapshot cannot replace the current catalog.")
    keys = [row.get("item_id") for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("Snapshot product identifiers must be unique.")
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        exported = {key: row.get(key, "") for key in FIELDS}
        for required in FIELDS[:9]:
            if not exported[required]:
                raise ValueError(f"Required feed field {required} is missing.")
        exported["is_eligible_search"] = "false" if row.get("is_eligible_search") is False else "true"
        exported["is_eligible_checkout"] = "false"
        writer.writerow(exported)
    return gzip.compress(output.getvalue().encode("utf-8"), mtime=0)
