"""Tiered Gmail connectivity test.

Run from qcbackend/:
    .venv/Scripts/python.exe scripts/test_gmail.py
    .venv/Scripts/python.exe scripts/test_gmail.py --as user@workspace.com
    .venv/Scripts/python.exe scripts/test_gmail.py --as user@workspace.com --send-to dest@example.com

Tier 1: parse the service-account JSON + acquire an OAuth2 access token.
Tier 2: call Gmail users.getProfile + labels.list with `userId='me'` for the
        delegated user. Requires the SA to have domain-wide delegation enabled
        and the relevant Gmail scopes authorized in Workspace admin.
Tier 3: send a plaintext test message via the delegated mailbox. Only runs
        when --send-to is supplied (avoids unintended sending).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

# Make `from app.*` work when running directly.
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.services.email.gmail_client import (  # noqa: E402
    GmailConfig,
    acquire_token,
    explain_http_error,
    get_profile,
    list_labels,
    send_message,
)
from googleapiclient.errors import HttpError  # noqa: E402

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")


def _ok(label: str) -> None:
    print(f"  [OK] {label}")


def _fail(label: str, reason: str) -> None:
    print(f"  [FAIL] {label}")
    for line in str(reason).splitlines():
        print(f"         {line}")


def _info(label: str, value: str) -> None:
    print(f"        {label}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Gmail connectivity test")
    parser.add_argument(
        "--as",
        dest="delegated_user",
        default=None,
        help="Workspace email to impersonate (overrides GMAIL_DELEGATED_USER)",
    )
    parser.add_argument(
        "--send-to",
        default=None,
        help="If set, sends a plaintext test message to this address (Tier 3)",
    )
    parser.add_argument(
        "--sa-path",
        default=None,
        help="Path to the service-account JSON (overrides GMAIL_SERVICE_ACCOUNT_PATH)",
    )
    args = parser.parse_args()

    settings = get_settings()
    sa_path = Path(args.sa_path or settings.gmail_service_account_path or "").resolve()
    delegated = args.delegated_user or settings.gmail_delegated_user or None

    print("=" * 70)
    print(" Gmail connectivity test")
    print("=" * 70)
    _info("service account file", str(sa_path) if sa_path else "(not set)")
    _info("delegated user", delegated or "(not set)")
    print()

    if not sa_path or str(sa_path) == "." or not sa_path.exists():
        _fail("Tier 0: service account file present", f"Not found: {sa_path}")
        print()
        print("Set GMAIL_SERVICE_ACCOUNT_PATH in qcbackend/.env or pass --sa-path.")
        return 1

    cfg = GmailConfig(service_account_path=sa_path, delegated_user=delegated)

    # ---- Tier 1: acquire token --------------------------------------------
    print("Tier 1: OAuth2 token acquisition")
    try:
        result = acquire_token(cfg)
    except Exception as e:  # noqa: BLE001
        _fail("Token acquisition", e)
        return 2
    _ok("Token acquired")
    _info("service_account_email", result["service_account_email"])
    _info("scopes", ", ".join(result["scopes"]))
    _info("subject (delegated user)", result["subject"] or "(none - calls to /me will fail)")
    _info("token expiry", result["expiry"] or "(unknown)")
    print()

    if not delegated:
        print("Tier 2 skipped: no delegated user configured.")
        print()
        print("Service accounts have no inbox of their own; to call Gmail APIs you must")
        print("(a) enable domain-wide delegation on the SA in Google Workspace admin,")
        print("(b) authorize the Gmail scopes for the SA's client_id, and")
        print("(c) re-run with --as someone@yourworkspace.com.")
        return 0

    # ---- Tier 2: profile + labels -----------------------------------------
    print("Tier 2: Gmail API calls (profile, labels)")
    try:
        profile = get_profile(cfg)
        _ok(f"users.getProfile -> {profile.get('emailAddress')}")
        _info("messagesTotal", str(profile.get("messagesTotal")))
        _info("threadsTotal", str(profile.get("threadsTotal")))
        _info("historyId", str(profile.get("historyId")))
    except HttpError as e:
        _fail("users.getProfile", explain_http_error(e))
        print()
        print("Common cause: domain-wide delegation isn't enabled for this SA, or")
        print("the Gmail scopes aren't authorized in admin.google.com.")
        return 3
    except Exception as e:  # noqa: BLE001
        _fail("users.getProfile", e)
        return 3

    try:
        labels = list_labels(cfg)
        _ok(f"users.labels.list -> {len(labels)} labels")
        # Print the system-y labels so the operator can see things like INBOX, SENT etc.
        sample = [lbl.get("name") for lbl in labels[:10]]
        _info("first 10", ", ".join(sample))
    except HttpError as e:
        _fail("users.labels.list", explain_http_error(e))
        return 4
    except Exception as e:  # noqa: BLE001
        _fail("users.labels.list", e)
        return 4

    print()

    # ---- Tier 3: send test message ----------------------------------------
    if not args.send_to:
        print("Tier 3 skipped: --send-to not provided.")
        return 0

    print("Tier 3: send test message")
    try:
        sent = send_message(
            cfg,
            to=args.send_to,
            subject="[QC-CONN-TEST] Connectivity test from QC backend",
            body=(
                "This is the Qualified Commercial backend confirming Gmail send works "
                "via the configured service account + domain-wide delegation. You can "
                "delete this message safely.\n"
            ),
        )
        _ok(f"Message sent — id={sent.get('id')}, threadId={sent.get('threadId')}")
    except HttpError as e:
        _fail("messages.send", explain_http_error(e))
        return 5
    except Exception as e:  # noqa: BLE001
        _fail("messages.send", e)
        return 5

    return 0


if __name__ == "__main__":
    sys.exit(main())
