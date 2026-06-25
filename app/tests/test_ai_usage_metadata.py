from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.services.ai.usage import json_safe_metadata


def test_json_safe_metadata_normalizes_domain_values() -> None:
    user_id = uuid4()
    payload = {
        "loan_id": UUID("3216afa8-dcbd-4cce-a2b6-9e239530c791"),
        "user_id": user_id,
        "nested": {
            "at": datetime(2026, 6, 25, 2, 0, tzinfo=timezone.utc),
            "on": date(2026, 6, 25),
            "amount": Decimal("12.34"),
        },
        "items": {user_id, "plain"},
    }

    safe = json_safe_metadata(payload)

    assert safe["loan_id"] == "3216afa8-dcbd-4cce-a2b6-9e239530c791"
    assert safe["user_id"] == str(user_id)
    assert safe["nested"]["at"] == "2026-06-25T02:00:00+00:00"
    assert safe["nested"]["on"] == "2026-06-25"
    assert safe["nested"]["amount"] == 12.34
    assert sorted(safe["items"]) == sorted([str(user_id), "plain"])
