"""A one-time email code in front of a client form link.

The application shows a date of birth, the last four of an SSN and the
borrower's declarations, so a bare link is not enough: opening it asks for a
6-digit code sent to the email the link was made for. The code lives in the
link's own PIN columns (hash only, 15 minutes), is burned on use, and is
exchanged for a 12-hour signed session the page sends back in
``X-Form-Session``. A room the client already signed into hands over a session
directly, so nobody enters a code twice.

Guessing is bounded on the link row, where a restart cannot forget it: wrong
tries are counted across codes (asking for a new code never buys more tries),
every fifth one locks the link, and each lockout is twice the last (15 minutes
up to a day). Only a correct code clears the count. Callers lock the row
first, so parallel guesses cannot share one count. Codes are also rationed per
link (five an hour, twelve a day), so a leaked link cannot be used to flood
the client's inbox.

Nothing secret is stored: the code is hashed and the session is a signature
over the link id and its expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import HTTPException, status

from app.config import get_settings

CODE_TTL = timedelta(minutes=15)
MAX_ATTEMPTS = 5
LOCKOUT = timedelta(minutes=15)
MAX_LOCKOUT = timedelta(hours=24)
SESSION_TTL = timedelta(hours=12)
RESEND_GAP = timedelta(seconds=45)
SENDS_PER_HOUR = 5
SENDS_PER_DAY = 12


def _now() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _secret() -> bytes:
    s = get_settings()
    raw = s.clerk_secret_key or getattr(s, "provider_secrets_encryption_key", "")
    if not raw:
        # A session signed with a constant anyone can read is no session: refuse rather than fall back.
        if str(getattr(s, "app_env", "") or "").lower() == "production":
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Form links are not available right now.")
        raw = "qc-client-form"
    return hashlib.sha256(f"client-form-session:{raw}".encode()).digest()


def lockout_after(attempts: int) -> timedelta:
    """How long the link locks once ``attempts`` wrong tries have piled up: 15 minutes, doubling, up to a day."""
    rounds = max(1, int(attempts) // MAX_ATTEMPTS)
    return min(MAX_LOCKOUT, LOCKOUT * 2 ** min(rounds - 1, 10))


def derive_token(link_id: Any) -> str:
    """The token of a standing link, recomputed from the link's id.

    Only the hash is stored, as for every link; the desk can still copy the
    same address again because the token is a keyed digest of the row's id,
    which nobody can compute without the server's secret.
    """
    digest = hmac.new(_secret(), f"standing-link:{link_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _code_hash(link: Any, code: str) -> str:
    return hmac.new(_secret(), f"{link.id}:{code}".encode(), hashlib.sha256).hexdigest()


def mask_email(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    name, domain = email.split("@", 1)
    return f"{name[:1]}{'*' * max(1, min(len(name) - 1, 6))}@{domain}"


def issue_session(link: Any, *, now: datetime | None = None, email_verified: bool = False) -> tuple[str, datetime]:
    expires = (now or _now()) + SESSION_TTL
    payload = {"lid": str(link.id), "exp": int(expires.timestamp())}
    if email_verified:
        payload["email_verified"] = True
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")
    signature = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{signature}", expires


def session_valid(session: str | None, link: Any, *, now: datetime | None = None, require_email: bool = False) -> bool:
    try:
        body, signature = (session or "").split(".", 1)
    except ValueError:
        return False
    expected = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        payload = json.loads(base64.urlsafe_b64decode((body + "=" * (-len(body) % 4)).encode()).decode())
    except (ValueError, UnicodeDecodeError):
        return False
    return (not require_email or payload.get("email_verified") is True) and payload.get("lid") == str(link.id) and int(payload.get("exp", 0) or 0) > int((now or _now()).timestamp())


def require_session(session: str | None, link: Any) -> None:
    if not session_valid(session, link):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, {"code": "code_required", "message": "Enter the code we emailed you to open this form."})


def new_code(link: Any, *, now: datetime | None = None) -> str:
    """Store a fresh code's hash on the link and return the code (for the email only)."""
    moment = now or _now()
    if _aware(link.pin_locked_until) and _aware(link.pin_locked_until) > moment:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many tries. Wait a few minutes and ask for a new code.")
    if link.pin_set_at and link.pin_hash and moment - _aware(link.pin_set_at) < RESEND_GAP:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "A code was just sent. Check your email, or ask again in a minute.")
    from app.services import link_throttle

    key = f"client-form-code:{link.id}"
    if (
        link_throttle.count(key, window=timedelta(hours=1)) >= SENDS_PER_HOUR
        or link_throttle.count(f"{key}:day", window=timedelta(days=1)) >= SENDS_PER_DAY
    ):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many codes were requested for this link. Try again later, or ask your advisor for a new link.")
    link_throttle.note(key, window=timedelta(hours=1))
    link_throttle.note(f"{key}:day", window=timedelta(days=1))
    code = f"{secrets.randbelow(1_000_000):06d}"
    link.pin_hash = _code_hash(link, code)
    link.pin_set_at = moment
    # pin_attempts is left alone: wrong tries count across codes until one is right.
    return code


def verify_code(link: Any, code: str, *, now: datetime | None = None) -> tuple[str, datetime]:
    """Check a code; burn it on success and return a session."""
    moment = now or _now()
    if _aware(link.pin_locked_until) and _aware(link.pin_locked_until) > moment:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Too many tries. Wait a few minutes and ask for a new code.")
    if not link.pin_hash or not link.pin_set_at or moment - _aware(link.pin_set_at) > CODE_TTL:
        raise HTTPException(status.HTTP_410_GONE, "That code has expired. Ask for a new one.")
    if not hmac.compare_digest(link.pin_hash, _code_hash(link, (code or "").strip())):
        link.pin_attempts = min(int(link.pin_attempts or 0) + 1, 10_000)
        if link.pin_attempts % MAX_ATTEMPTS == 0:
            link.pin_locked_until = moment + lockout_after(link.pin_attempts)
            link.pin_hash = None
        raise HTTPException(status.HTTP_403_FORBIDDEN, "That code is not right. Check the latest email and try again.")
    link.pin_hash = None
    link.pin_attempts = 0
    link.pin_locked_until = None
    return issue_session(link, now=moment, email_verified=True)


def send_code_email(*, to_email: str, code: str, form_title: str) -> bool:
    from app.services.email.ses_client import send_email

    result = send_email(
        to_email=to_email,
        subject=f"Your code for the {form_title}",
        body_text=(
            f"Your code to open the {form_title} is {code}.\n\n"
            "It works once and expires in 15 minutes. If you did not ask for it, you can ignore this email.\n\n"
            "Qualified Commercial"
        ),
    )
    return bool(getattr(result, "ok", False))
