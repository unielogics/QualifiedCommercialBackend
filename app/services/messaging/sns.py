"""Verify that an SNS notification really came from Amazon.

The two SMS webhooks in this app authenticate differently — one by a shared
token in the query string, one by Twilio's HMAC — and neither helps here. SNS
signs its payloads with a per-topic X.509 certificate, so verification means
rebuilding the canonical string, fetching the signing certificate and checking
an RSA signature over it.

The dangerous part is the certificate URL, which arrives *inside the payload we
are trying to authenticate*. An attacker who can point that at a host they
control can sign anything. So the host is checked against Amazon's own domain
before it is fetched, and nothing else about the message is trusted until the
signature verifies.
"""

from __future__ import annotations

import base64
import logging
import re
from urllib.parse import ParseResult, urlparse

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

log = logging.getLogger(__name__)

#: The fields SNS signs, in the order it signs them, per message type.
_SIGNED_FIELDS = {
    "Notification": ("Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"),
    "SubscriptionConfirmation": (
        "Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type",
    ),
    "UnsubscribeConfirmation": (
        "Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type",
    ),
}

_CERT_CACHE: dict[str, bytes] = {}
_SNS_HOST_RE = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?$")
_SNS_CERT_PATH_RE = re.compile(r"^/SimpleNotificationService-[A-Za-z0-9_-]+\.pem$")


def _sns_url(url: str) -> ParseResult | None:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port is not None
        or not _SNS_HOST_RE.fullmatch(host)
    ):
        return None
    return parsed


def _cert_url_is_amazon(url: str) -> bool:
    """The certificate URL travels inside the unverified payload, so this is the
    check that stops an attacker signing their own notifications."""
    parsed = _sns_url(url)
    return bool(
        parsed is not None
        and not parsed.query
        and not parsed.fragment
        and _SNS_CERT_PATH_RE.fullmatch(parsed.path)
    )


def _subscribe_url_is_amazon(url: str) -> bool:
    """Allow subscription confirmation only through the regional SNS API."""
    parsed = _sns_url(url)
    return bool(parsed is not None and not parsed.fragment)


def _canonical(message: dict) -> bytes:
    fields = _SIGNED_FIELDS.get(str(message.get("Type") or ""))
    if not fields:
        raise ValueError(f"unsigned message type: {message.get('Type')!r}")
    out: list[str] = []
    for key in fields:
        if key not in message:
            # Subject is genuinely optional; everything else missing means the
            # payload is malformed and must not verify.
            continue
        out.append(key)
        out.append(str(message[key]))
    return ("\n".join(out) + "\n").encode("utf-8")


async def _fetch_cert(url: str) -> bytes:
    if url in _CERT_CACHE:
        return _CERT_CACHE[url]
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(url)
        response.raise_for_status()
    _CERT_CACHE[url] = response.content
    return response.content


async def verify(message: dict) -> bool:
    """True when the message carries a valid Amazon signature. Never raises."""
    try:
        cert_url = str(message.get("SigningCertURL") or message.get("SigningCertUrl") or "")
        if not _cert_url_is_amazon(cert_url):
            log.warning("sns: refused signing certificate from %r", cert_url)
            return False
        signature = base64.b64decode(str(message.get("Signature") or ""))
        if not signature:
            return False
        algorithm = (
            hashes.SHA256() if str(message.get("SignatureVersion") or "1") == "2" else hashes.SHA1()
        )
        certificate = x509.load_pem_x509_certificate(await _fetch_cert(cert_url))
        certificate.public_key().verify(
            signature, _canonical(message), padding.PKCS1v15(), algorithm
        )
        return True
    except Exception:  # noqa: BLE001
        log.warning("sns: signature verification failed", exc_info=True)
        return False


async def confirm_subscription(message: dict) -> bool:
    """Complete an SNS subscription handshake by fetching its SubscribeURL.

    Only ever called on a message whose signature already verified, so the URL
    is Amazon's own.
    """
    url = str(message.get("SubscribeURL") or "")
    if not _subscribe_url_is_amazon(url):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            (await client.get(url)).raise_for_status()
        log.info("sns: subscription confirmed for topic %s", message.get("TopicArn"))
        return True
    except Exception:  # noqa: BLE001
        log.warning("sns: subscription confirmation failed", exc_info=True)
        return False
