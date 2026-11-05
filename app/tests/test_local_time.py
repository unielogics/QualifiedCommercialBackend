"""Outbound mail tells the time the way the reader keeps it.

The owner's upload and activity digests said "04:04 PM UTC" at eleven in the
morning in Florida.
"""

from datetime import UTC, datetime
from types import SimpleNamespace

from app.services.admin_activity import _format_digest
from app.services.file_events import render_digest
from app.services.local_time import FIRM_DEFAULT_TIMEZONE, local_stamp, zone, zone_label

NOON_UTC_NOV = datetime(2026, 11, 3, 16, 4, tzinfo=UTC)  # 11:04 AM in New York (EST)
JULY_UTC = datetime(2026, 7, 3, 16, 4, tzinfo=UTC)  # 12:04 PM in New York (EDT)


def test_a_moment_reads_in_the_firms_zone_with_the_label_the_console_uses() -> None:
    assert local_stamp(NOON_UTC_NOV, zone("America/New_York")) == "Nov 3, 11:04 AM ET"
    assert local_stamp(JULY_UTC, zone("America/New_York")) == "Jul 3, 12:04 PM ET"
    assert local_stamp(NOON_UTC_NOV, zone("America/Chicago"), fmt="%-I:%M %p") == "10:04 AM CT"


def test_a_naive_database_timestamp_is_utc() -> None:
    assert local_stamp(datetime(2026, 11, 3, 16, 4), zone(None)) == "Nov 3, 11:04 AM ET"


def test_unknown_or_blank_zones_fall_back_to_the_firm_default() -> None:
    assert str(zone(None).key) == FIRM_DEFAULT_TIMEZONE
    assert str(zone("Mars/Olympus").key) == FIRM_DEFAULT_TIMEZONE
    assert zone_label(zone("Europe/London"), NOON_UTC_NOV) == "GMT"


def test_the_file_updates_digest_speaks_local_time() -> None:
    event = SimpleNamespace(created_at=NOON_UTC_NOV, title="Uploaded 20260630-statements-1204-.pdf", body=None)
    subject, text, html = render_digest("Body Good Studio LLC", [event], titles_only=True, link=None, tz=zone("America/New_York"))
    assert subject == "1 update on Body Good Studio LLC"
    assert "Nov 3, 11:04 AM ET — Uploaded" in text
    assert "UTC" not in text and "UTC" not in html


def test_the_activity_digest_speaks_local_time() -> None:
    items = [{
        "created_at": NOON_UTC_NOV, "lead_name": "Body Good Studio LLC", "intake_id": None,
        "actor_name": "Linda Louis", "actor_role": "public_lead", "label": "uploaded a file",
        "detail": "20260630-statements-1204-.pdf", "action": "dealer_ai_file_uploaded",
    }]
    _subject, text, _html = _format_digest(items, public_url="https://app.example.com", tz=zone("America/New_York"))
    assert "11:04 AM ET" in text
    assert "UTC" not in text
