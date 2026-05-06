"""iSoftPull soft-credit pull HTTP client.

Wire contract source: iSoftPull API Docs 2024 (their public PDF; URL below).

  Endpoint:   POST https://app.isoftpull.com/api/v2/reports
  Auth:       api-key + api-secret HTTP headers
  Required:   first_name, last_name, address, city, state (FULL name), zip
  Optional:   ssn (no dashes), date_of_birth (mm/dd/yyyy)  [optional for soft pulls]

  Response:   { reports: { equifax|transunion|experian: { status, message, link, ... } },
                full_feed_link: "...",
                full_feed: { credit_score: { fico_4|fico_8|vantage_4: { score, ... } }, ... },
                intelligence: { result, name? } }

  When a bureau fails, the per-bureau object carries:
    status:       "failure"
    failure_type: "error" | "no-hit" | "freeze"
    message:      bureau-specific message

  When the *whole request* fails validation, top-level shape is:
    { status: "failure", message: "<reason>" }
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)


class IsoftpullError(Exception):
    code: str = "isoftpull_error"


class IsoftpullTransportError(IsoftpullError):
    code = "transport"


class IsoftpullValidationError(IsoftpullError):
    code = "validation"


class IsoftpullDeniedError(IsoftpullError):
    code = "denied"


class IsoftpullRateLimitedError(IsoftpullError):
    code = "rate_limited"


# US state code → full name. iSoftPull rejects 2-letter abbreviations.
_STATE_FULL_NAME: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware",
    "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii",
    "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine",
    "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska",
    "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island",
    "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas",
    "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}


@dataclass(frozen=True)
class ApplicantPayload:
    """Soft-pull applicant. We only need first/last/address/city/state/zip;
    DOB and SSN are optional for soft pulls. We accept them so callers can
    pass them through when available, but they're not required.

    `state` may be either the 2-letter code ("NJ") or the full name
    ("New Jersey") — the client normalizes to full name before sending.
    `dob` may be ISO-8601 ("1985-01-01") or mm/dd/yyyy — normalized below.
    """
    legal_first_name: str
    legal_last_name: str
    street: str
    city: str
    state: str
    zip: str
    dob: str | None = None       # optional for soft pull
    ssn: str | None = None       # optional for soft pull, no dashes


@dataclass(frozen=True)
class IsoftpullResult:
    """Result of a soft pull.

    `fico` is None when the API token isn't configured for Full Feed —
    iSoftPull only returns the report link + intelligence verdict in that
    mode. Callers should fall back to the intelligence result for gating
    when fico is None.
    """
    fico: int | None
    bureau: str                  # which bureau the score came from
    raw: dict
    pulled_at: datetime
    provider_pull_id: str | None # iSoftPull `applicant_id` if surfaced
    # Intelligence + report links — always present when at least one bureau
    # succeeds, even without Full Feed.
    intelligence_passed: bool
    intelligence_name: str | None
    applicant_link: str | None
    report_link: str | None


def _backoff_seconds(attempt: int) -> float:
    return 0.5 * (2**attempt)


def _normalize_state(state: str) -> str:
    s = state.strip()
    if len(s) == 2:
        return _STATE_FULL_NAME.get(s.upper(), s)
    return s


def _normalize_dob(dob: str) -> str:
    """Accept ISO 'YYYY-MM-DD' or 'mm/dd/yyyy'; emit mm/dd/yyyy."""
    s = dob.strip()
    if "/" in s:
        return s
    # ISO
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
        return d.strftime("%m/%d/%Y")
    except ValueError:
        return s  # unrecognized; pass through and let iSoftPull reject


def _extract_score(body: dict) -> tuple[int | None, str | None]:
    """Walk the credit_score block in the order FICO 8 → FICO 4 → Vantage 4
    and return the first numeric score we find, plus its bureau label."""
    full_feed = body.get("full_feed") or {}
    credit_score = full_feed.get("credit_score") or {}

    for model in ("fico_8", "fico_4", "vantage_4"):
        node = credit_score.get(model) or {}
        score = node.get("score")
        if isinstance(score, (int, float)) and score > 0:
            return int(score), model
        if isinstance(score, str) and score.isdigit():
            return int(score), model

    # Sometimes the score lives at the top level under `score` (older docs)
    top = body.get("score")
    if isinstance(top, (int, float)) and top > 0:
        return int(top), "unknown"
    return None, None


def _extract_bureau_status(body: dict) -> str | None:
    """Return the first successful bureau's name, else None."""
    reports = body.get("reports") or {}
    for bureau in ("transunion", "equifax", "experian"):
        node = reports.get(bureau) or {}
        if node.get("status") == "success":
            return bureau
    return None


def _extract_provider_id(body: dict) -> str | None:
    for key in ("applicant_id", "id", "report_id", "request_id"):
        v = body.get(key)
        if isinstance(v, (str, int)) and v:
            return str(v)
    # iSoftPull encodes the applicant id in the link path:
    #   "https://app.isoftpull.com/client/applicants/view_only/123/soft_pull"
    link = (body.get("reports") or {}).get("link")
    if isinstance(link, str) and "/applicants/" in link:
        try:
            return link.split("/applicants/")[1].split("/")[1]
        except IndexError:
            return None
    return None


def _extract_links(body: dict) -> tuple[str | None, str | None]:
    """Return (applicant_link, report_link). The applicant link is at
    reports.link; the per-bureau report link is reports.{bureau}.link."""
    reports = body.get("reports") or {}
    applicant_link = reports.get("link") if isinstance(reports.get("link"), str) else None
    report_link = None
    for bureau in ("experian", "transunion", "equifax"):
        node = reports.get(bureau) or {}
        if node.get("status") == "success" and isinstance(node.get("link"), str):
            report_link = node["link"]
            break
    return applicant_link, report_link


def _intelligence(body: dict) -> tuple[bool, str | None]:
    intel = body.get("intelligence") or {}
    return intel.get("result") == "passed", intel.get("name")


def _safe_msg(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        if isinstance(body, dict):
            for key in ("message", "error", "detail"):
                v = body.get(key)
                if isinstance(v, str):
                    return v
        return str(body)[:200]
    except ValueError:
        return resp.text[:200]


async def pull(
    *,
    public_key: str,
    private_key: str,
    base_url: str,
    applicant: ApplicantPayload,
    timeout_seconds: float = 15.0,
    max_retries: int = 2,
) -> IsoftpullResult:
    """Run a soft credit pull through iSoftPull.

    Raises:
        IsoftpullValidationError: 4xx with field-level errors (don't retry).
        IsoftpullDeniedError: bureau-level no-hit / freeze / no score returned.
        IsoftpullRateLimitedError: 429 after retries exhausted.
        IsoftpullTransportError: network / 5xx after retries exhausted.
    """
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "api-key": public_key,
        "api-secret": private_key,
    }

    body: dict = {
        "first_name": applicant.legal_first_name,
        "last_name": applicant.legal_last_name,
        "address": applicant.street,
        "city": applicant.city,
        "state": _normalize_state(applicant.state),
        "zip": applicant.zip,
    }
    if applicant.dob:
        body["date_of_birth"] = _normalize_dob(applicant.dob)
    if applicant.ssn:
        body["ssn"] = applicant.ssn.replace("-", "")

    url = base_url.rstrip("/") + "/reports"

    timeout = httpx.Timeout(connect=5.0, read=timeout_seconds, write=5.0, pool=5.0)
    last_transport_exc: Exception | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(max_retries + 1):
            try:
                resp = await client.post(url, json=body, headers=headers)
            except (httpx.TransportError, httpx.ReadTimeout) as exc:
                last_transport_exc = exc
                if attempt < max_retries:
                    await asyncio.sleep(_backoff_seconds(attempt))
                    continue
                raise IsoftpullTransportError(f"network error: {exc}") from exc

            # Retry on transient upstream/rate errors only
            if resp.status_code in (502, 503, 504):
                if attempt < max_retries:
                    await asyncio.sleep(_backoff_seconds(attempt))
                    continue
                raise IsoftpullTransportError(f"upstream {resp.status_code}: {resp.text[:200]}")
            if resp.status_code == 429:
                if attempt < max_retries:
                    await asyncio.sleep(_backoff_seconds(attempt))
                    continue
                raise IsoftpullRateLimitedError("rate limited")

            # 4xx → validation. iSoftPull returns 200 with status=failure for
            # most validation errors, so the 4xx branch is mostly auth issues.
            if 400 <= resp.status_code < 500:
                raise IsoftpullValidationError(f"{resp.status_code}: {_safe_msg(resp)}")
            if resp.status_code >= 500:
                raise IsoftpullTransportError(f"upstream {resp.status_code}: {resp.text[:200]}")

            try:
                data = resp.json()
            except ValueError as exc:
                raise IsoftpullValidationError(f"non-JSON body: {exc}") from exc

            # Top-level validation failure ({status, message})
            if isinstance(data, dict) and data.get("status") == "failure":
                raise IsoftpullValidationError(data.get("message") or "validation failed")

            fico, model = _extract_score(data)
            bureau = _extract_bureau_status(data)
            intelligence_passed, intelligence_name = _intelligence(data)
            applicant_link, report_link = _extract_links(data)

            # Success when ANY bureau succeeded OR intelligence passed. We
            # used to require a numeric FICO, but tokens without Full Feed
            # only return links + intelligence verdict — those pulls are
            # still successful, the score just isn't parseable from the
            # body and lives behind the report_link.
            if bureau is None and not intelligence_passed:
                reasons = []
                reports = data.get("reports") or {}
                for b, node in reports.items():
                    if isinstance(node, dict) and node.get("status") == "failure":
                        ft = node.get("failure_type") or "failure"
                        reasons.append(f"{b}={ft}")
                detail = "; ".join(reasons) or "no bureau succeeded"
                raise IsoftpullDeniedError(detail)

            return IsoftpullResult(
                fico=fico,
                bureau=bureau or model or "unknown",
                raw=data,
                pulled_at=datetime.now(timezone.utc),
                provider_pull_id=_extract_provider_id(data),
                intelligence_passed=intelligence_passed,
                intelligence_name=intelligence_name,
                applicant_link=applicant_link,
                report_link=report_link,
            )

    raise IsoftpullTransportError(
        f"unexpected: retries exhausted (last={last_transport_exc})"
    )
