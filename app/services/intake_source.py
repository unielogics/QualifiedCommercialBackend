"""Safe, non-credential entry links for the staff intake workspace."""

from __future__ import annotations

from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlsplit, urlunsplit

TRUSTED_HOSTS = {"qualifiedcommercial.com", "www.qualifiedcommercial.com", "app.qualifiedcommercial.com", "rep.qualifiedcommercial.com", "capital.qualifiedcommercial.com"}
SOURCE_PARAMS = {"source", "page", "program", "vertical", "campaign", "utm_campaign", "cta", "locale", "funding_intent"}


def safe_source_url(value: object, *, relative: bool = False) -> str | None:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        return None
    raw = value.strip()
    if relative and raw.startswith("/") and not raw.startswith("//"):
        raw = urljoin("https://qualifiedcommercial.com", raw)
    try:
        url = urlsplit(raw)
        if url.scheme != "https" or url.hostname not in TRUSTED_HOSTS or url.username or url.password or url.port not in (None, 443):
            return None
        # Entry URLs may contain an access token. Never retain one, even in a path.
        if any(part in unquote(url.path).lower() for part in ("/request/", "/room/", "/forms/", "/link/", "/consent/")):
            return None
        params = [(k, v[:300]) for k, v in parse_qsl(url.query) if k in SOURCE_PARAMS]
        return urlunsplit(("https", url.hostname, url.path or "/", urlencode(params), ""))
    except ValueError:
        return None


def source_for_intake(intake) -> dict | None:
    raw = getattr(intake, "intake_state", None)
    state = raw if isinstance(raw, dict) else {}
    attribution = state.get("signup_attribution") or {}
    if not isinstance(attribution, dict):
        attribution = {}
    entry = safe_source_url(attribution.get("entry_url"))
    marketing = safe_source_url(attribution.get("page"), relative=True)
    recorded = bool(entry or marketing)
    if not recorded and str(getattr(intake, "variant", "")).startswith("mca"):
        marketing = "https://qualifiedcommercial.com/mca-refinance"
    if not entry and not marketing:
        return None
    return {"entry_url": entry, "marketing_url": marketing, "recorded": recorded,
            "campaign": str(attribution.get("campaign") or "")[:120] or None,
            "cta": str(attribution.get("cta") or "")[:120] or None}
