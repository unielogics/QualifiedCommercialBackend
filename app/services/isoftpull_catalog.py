"""Read-only provider catalog. Never visits the new-report or refresh-pull routes."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from urllib.parse import urlencode, urlsplit
from uuid import UUID

from bs4 import BeautifulSoup

from app.services.isoftpull_report_parser import parse_report_html
from app.services.isoftpull_session import IsoftpullSessionError, get_session

REPORT_PATH = re.compile(r"^/client/reports/([a-f0-9-]{36})$")


async def page(path: str):
    response = await get_session().fetch(path)
    if response.status_code != 200:
        raise IsoftpullSessionError("The existing iSoftPull catalog could not be loaded. No new credit pull was made.")
    return BeautifulSoup(response.text, "html.parser"), response.text


def parse_applicants(soup) -> list[dict]:
    rows = []
    for row in soup.select("tbody tr"):
        anchor = next((a for a in row.select("a[href]") if re.fullmatch(r"/client/applicants/\d+", a["href"])), None)
        if not anchor:
            continue
        cells = row.find_all(["td", "th"], recursive=False)
        rows.append({"id": anchor["href"].rsplit("/", 1)[1], "name": anchor.get_text(" ", strip=True),
                     "status": cells[3].get_text(" ", strip=True) if len(cells) > 3 else "",
                     "updated": cells[-1].get_text(" ", strip=True) if cells else ""})
    return rows


async def applicants(query: str = "", page_number: int = 1, archived: bool = False) -> dict:
    params = [("query", query[:120]), ("page", str(page_number)), ("query_archive[]", "archived" if archived else "unarchived")]
    soup, _ = await page("/client/applicants?" + urlencode(params))
    return {"items": parse_applicants(soup), "page": page_number,
            "has_next": bool(soup.select_one('a[rel="next"], .pagination .next a'))}


async def reports(applicant_id: str, pull_type: str = "soft_pull") -> tuple[list[dict], str]:
    if not applicant_id.isdigit() or pull_type not in ("soft_pull", "hard_pull"):
        raise ValueError("Invalid provider applicant or report type")
    soup, html = await page(f"/client/applicants/{applicant_id}/{pull_type}")
    out, seen = [], set()
    for a in soup.select("a[href]"):
        match = REPORT_PATH.fullmatch(urlsplit(a["href"]).path)
        if not match:
            continue
        report_id = str(UUID(match[1]))
        if report_id in seen:
            continue
        seen.add(report_id)
        row = a.find_parent("tr")
        label = row.get_text(" ", strip=True)[:180] if row else a.get_text(" ", strip=True)
        out.append({"id": report_id, "label": label or "Existing bureau report", "pull_type": pull_type})
    return out, html


async def retrieve(applicant_id: str, report_id: str, pull_type: str) -> tuple[dict, str, datetime]:
    report_id = str(UUID(report_id))
    options, tab_html = await reports(applicant_id, pull_type)
    if report_id not in {r["id"] for r in options}:
        raise ValueError("This report is not available for the selected provider applicant")
    soup, html = await page(f"/client/reports/{report_id}")
    parsed = parse_report_html(html).to_dict()
    if not parsed["scores"] and not parsed["trade_accounts"] and not parsed["personal_info"]:
        raise IsoftpullSessionError("The existing provider report was not readable. No new pull was made.")
    # Never substitute today's date for the date of an existing inquiry.
    text = soup.get_text(" ", strip=True)
    date_match = re.search(r"(?:last\s+pull\s+date|report\s+date|date\s+pulled|pull\s+date)\s*:?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{4}|\d{4}-\d{2}-\d{2})", text, re.I)
    if not date_match and len(options) == 1:
        date_match = re.search(r"Last\s+Pull\s+Date\s*:?\s*(\d{1,2}[-/]\d{1,2}[-/]\d{4})", BeautifulSoup(tab_html, "html.parser").get_text(" ", strip=True), re.I)
    if not date_match:
        raise IsoftpullSessionError("The report's original pull date could not be verified. Contact support; no new pull was made.")
    value = date_match[1].replace("/", "-")
    pulled = datetime.strptime(value, "%Y-%m-%d" if len(value.split("-")[0]) == 4 else "%m-%d-%Y").replace(tzinfo=UTC)
    return parsed, html, pulled
