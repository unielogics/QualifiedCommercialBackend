"""Bounded file-opening telemetry without customer data or request contents."""
from __future__ import annotations

import json
import logging
import re

log = logging.getLogger("qc.file.metrics")

_FILE_PATH = re.compile(
    r"^/api/v1/(?P<family>admin/ai-underwriter-leads|application-profiles|loans|deals)/"
    r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}(?P<suffix>/[^?#]*)?$"
)
_PROFILE_READS = {
    "": "profile.detail",
    "/underwriting": "profile.underwriting",
    "/client-terms": "profile.client_terms",
    "/program-readiness": "profile.readiness",
    "/evidence-workspace": "profile.evidence_workspace",
    "/financial-cockpit": "profile.financial_cockpit",
    "/financial-plan": "profile.financial_plan",
    "/evidence": "profile.evidence",
    "/bank-accounts": "profile.bank_accounts",
    "/verification": "profile.verification",
    "/financial-forms": "profile.financial_forms",
}


def route_name(path: str, method: str) -> str | None:
    """Return a fixed read-route label; never return raw paths or identifiers."""
    if method not in {"GET", "HEAD"}:
        return None
    match = _FILE_PATH.fullmatch(path)
    if match is None:
        return None
    family, suffix = match["family"], (match["suffix"] or "").rstrip("/")
    if family == "application-profiles":
        return _PROFILE_READS.get(suffix)
    if family == "admin/ai-underwriter-leads":
        return "lead.detail" if not suffix else None
    return {"": "loan.detail", "/documents": "loan.documents", "/activity": "loan.activity",
            "/workspace": "loan.workspace"}.get(suffix)


def emit(*, route: str, method: str, status_code: int, duration_ms: float,
         ttfb_ms: float | None, response_bytes: int) -> None:
    """Use the existing log stream; timing must never fail a file request."""
    try:
        log.info("%s", json.dumps({
            "event": "file_loading_metric", "metric": "file.http.duration", "unit": "milliseconds",
            "route": route, "method": method, "status_code": status_code,
            "value": duration_ms, "ttfb_ms": ttfb_ms, "response_bytes": response_bytes,
        }, separators=(",", ":"), sort_keys=True))
    except Exception:  # pragma: no cover - telemetry is strictly best effort
        log.debug("file loading metric emission failed", exc_info=True)
