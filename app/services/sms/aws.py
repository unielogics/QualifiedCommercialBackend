"""AWS End User Messaging (pinpoint-sms-voice-v2).

Lifted from `consent_delivery._send_sms` without behaviour change. Dormant until
the account leaves the SMS sandbox and the toll-free number finishes
verification; in the sandbox only pre-verified destinations receive anything,
which for reaching a stranger is the same as not working.
"""

from __future__ import annotations

import logging

from app.config import get_settings

from .base import SmsResult

log = logging.getLogger(__name__)

name = "aws"


def available() -> bool:
    """Requires an origination number AND production access."""
    s = get_settings()
    return bool(
        getattr(s, "sms_origination_number", "") and getattr(s, "sms_production", False)
    )


def unavailable_reason() -> str:
    s = get_settings()
    if not getattr(s, "sms_origination_number", ""):
        return "AWS SMS has no origination number configured."
    return "AWS SMS is still in the sandbox — production access not granted yet."


def send(to_phone: str, body: str) -> SmsResult:
    try:
        import boto3

        s = get_settings()
        client = boto3.client(
            "pinpoint-sms-voice-v2", region_name=s.ses_region or "us-east-1"
        )
        resp = client.send_text_message(
            DestinationPhoneNumber=to_phone,
            OriginationIdentity=s.sms_origination_number,
            MessageBody=body,
            MessageType="TRANSACTIONAL",
        )
        return SmsResult(True, name, resp.get("MessageId", "sent"))
    except Exception as exc:  # noqa: BLE001
        log.warning("sms(aws): send failed to=%s: %s", to_phone, exc)
        return SmsResult(False, name, detail=f"Text could not be sent: {exc}")
