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
from urllib.parse import urlparse

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


def _cert_url_is_amazon(url: str) -> bool:
    """The certificate URL travels inside the unverified payload, so this is the
    check that stops an attacker signing their own notifications."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "amazonaws.com" or host.endswith(".amazonaws.com")


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
    if not _cert_url_is_amazon(url):
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            (await client.get(url)).raise_for_status()
        log.info("sns: subscription confirmed for topic %s", message.get("TopicArn"))
        return True
    except Exception:  # noqa: BLE001
        log.warning("sns: subscription confirmation failed", exc_info=True)
        return False
