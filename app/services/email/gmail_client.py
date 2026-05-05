"""Real Gmail client backed by a Google service account.

Authenticates via a service-account JSON key. To read or send mail on
behalf of a Google Workspace user the SA must have **domain-wide
delegation** enabled, with the relevant Gmail scopes authorized in the
Workspace admin console. Without delegation, the SA can still acquire an
OAuth2 access token (Tier-1 connectivity) but Gmail API calls will fail
because a service account has no inbox of its own.

The module is structured so the connectivity test can report exactly which
tier passes — token acquisition, getProfile, labels listing, etc.
"""

from __future__ import annotations

import base64
import logging
from dataclasses import dataclass
from email.message import EmailMessage
from functools import lru_cache
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)


# Scopes — request the minimum set the platform actually uses.
# Gmail send + read are kept narrow; the service account needs each of these
# explicitly authorized in the Workspace admin console (Security → API
# controls → Domain-wide delegation) for the delegated user to use them.
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.labels",
]


@dataclass(frozen=True)
class GmailConfig:
    service_account_path: Path
    delegated_user: str | None  # workspace email to impersonate, or None


class GmailNotConfigured(RuntimeError):
    """Raised when Gmail is invoked but no SA path is configured."""


def _load_credentials(cfg: GmailConfig) -> service_account.Credentials:
    if not cfg.service_account_path.exists():
        raise FileNotFoundError(f"Service account JSON not found at {cfg.service_account_path}")
    creds = service_account.Credentials.from_service_account_file(
        str(cfg.service_account_path),
        scopes=GMAIL_SCOPES,
    )
    if cfg.delegated_user:
        creds = creds.with_subject(cfg.delegated_user)
    return creds


def acquire_token(cfg: GmailConfig) -> dict[str, Any]:
    """Tier-1: prove the SA private key is valid by minting an access token.

    Doesn't talk to Gmail — only the OAuth2 token endpoint. Returns metadata
    suitable for the connectivity test report (NEVER includes the token
    itself, which is a bearer credential).
    """
    creds = _load_credentials(cfg)
    creds.refresh(Request())
    return {
        "ok": True,
        "scopes": list(creds.scopes or []),
        "service_account_email": creds.service_account_email,
        # google-auth's `subject` attribute only exists on creds created via
        # with_subject(); fall back to the configured delegated_user.
        "subject": getattr(creds, "_subject", None) or cfg.delegated_user,
        "token_acquired": creds.token is not None,
        "expiry": creds.expiry.isoformat() if creds.expiry else None,
    }


def get_gmail_service(cfg: GmailConfig):
    """Return a Gmail API client. Caller decides which userId to query."""
    creds = _load_credentials(cfg)
    # cache_discovery=False avoids the noisy ImportError warning in environments
    # without `oauth2client` installed (we use google-auth instead).
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def get_profile(cfg: GmailConfig) -> dict[str, Any]:
    """Tier-2: call users.getProfile. Requires DWD + a delegated user."""
    if not cfg.delegated_user:
        raise GmailNotConfigured(
            "GMAIL_DELEGATED_USER is unset. A workspace user email is required "
            "to call Gmail APIs via a service account."
        )
    svc = get_gmail_service(cfg)
    return svc.users().getProfile(userId="me").execute()


def list_labels(cfg: GmailConfig) -> list[dict[str, Any]]:
    """Tier-2b: enumerate labels — proves read scope works."""
    if not cfg.delegated_user:
        raise GmailNotConfigured("GMAIL_DELEGATED_USER required for labels.list")
    svc = get_gmail_service(cfg)
    resp = svc.users().labels().list(userId="me").execute()
    return resp.get("labels", [])


def send_message(cfg: GmailConfig, *, to: str, subject: str, body: str) -> dict[str, Any]:
    """Tier-3: send a plaintext message via the delegated mailbox.

    Subject is left as-is — the QC deal-id injection happens at the call
    site (see app.services.email.parser.inject_deal_id) so this client
    stays generic.
    """
    if not cfg.delegated_user:
        raise GmailNotConfigured("GMAIL_DELEGATED_USER required to send mail")
    msg = EmailMessage()
    msg["To"] = to
    msg["From"] = cfg.delegated_user
    msg["Subject"] = subject
    msg.set_content(body)
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("ascii")

    svc = get_gmail_service(cfg)
    return svc.users().messages().send(userId="me", body={"raw": raw}).execute()


# --- App integration --------------------------------------------------------

@lru_cache
def gmail_config() -> GmailConfig | None:
    """Build a GmailConfig from app settings, or None if SA path is unset.

    Lazy-loaded so importing this module doesn't read the filesystem.
    """
    from app.config import get_settings  # local import to avoid cycle

    settings = get_settings()
    if not settings.gmail_service_account_path:
        return None
    return GmailConfig(
        service_account_path=Path(settings.gmail_service_account_path).resolve(),
        delegated_user=settings.gmail_delegated_user or None,
    )


def explain_http_error(err: HttpError) -> str:
    """Render a googleapiclient HttpError into a single human-readable line."""
    try:
        content = err.error_details if hasattr(err, "error_details") else None
        if content:
            return f"{err.resp.status} — {content}"
        return f"{err.resp.status} — {err._get_reason() if hasattr(err, '_get_reason') else err}"
    except Exception:
        return str(err)
