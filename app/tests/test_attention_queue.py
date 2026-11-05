"""The desk's work queue: what stalls, how old it is, and where to click.

Fourteen of seventeen collecting intakes had no documents and nobody could see
it. These tests pin the queue's judgement — what counts as stalled, how
severity scales, which row comes first — and that a full load composes the
four loaders without any of them needing a real database.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

from app.schemas.operator_file import AttentionItem
from app.services import attention_queue as aq

NOW = datetime(2026, 10, 30, 15, 0, tzinfo=UTC)


class _Result:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return list(self._rows)

    def scalars(self) -> "_Result":
        return self


class _DB:
    """Hands back canned results in the order the loaders ask."""

    def __init__(self, results: list[list[object]]) -> None:
        self._results = list(results)
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        return _Result(self._results.pop(0) if self._results else [])


def test_age_and_severity_scale_from_the_threshold() -> None:
    assert aq._age_days(NOW - timedelta(days=9), NOW) == 9
    assert aq._age_days(None, NOW) == 0
    assert aq._severity(8, aq.INTAKE_NO_FILES_DAYS) == "warn"
    assert aq._severity(14, aq.INTAKE_NO_FILES_DAYS) == "bad"


def test_the_last_link_delivery_is_read_from_the_intake_audit() -> None:
    state = {
        "resume_email": {"ok": True, "sent_at": "2026-10-01T10:00:00+00:00"},
        "dealer_login_email_deliveries": [
            {"ok": True, "sent_at": "2026-10-20T10:00:00+00:00"},
            {"ok": False, "sent_at": "2026-10-28T10:00:00+00:00"},
        ],
    }
    assert aq._resume_sent_at(state) == datetime(2026, 10, 20, 10, tzinfo=UTC)
    assert aq._resume_sent_at({"resume_email": {"ok": False}}) is None
    assert aq._resume_sent_at(None) is None


def test_client_waiting_rows_come_first_then_the_oldest() -> None:
    items = [
        AttentionItem(kind="action_items_stale", severity="warn", title="C", detail="", age_days=40, href="/x"),
        AttentionItem(kind="intake_no_files", severity="warn", title="B", detail="", age_days=8, href="/x"),
        AttentionItem(kind="intake_no_files", severity="bad", title="A", detail="", age_days=20, href="/x"),
        AttentionItem(kind="request_overdue", severity="warn", title="D", detail="", age_days=31, href="/x"),
    ]
    assert [item.title for item in aq.sort_items(items)] == ["A", "B", "D", "C"]


def test_hrefs_open_the_file_when_there_is_one_and_the_room_otherwise() -> None:
    intake_id, bucket_id = uuid4(), uuid4()
    assert aq._file_href(bucket_id, intake_id) == f"/admin/ai-underwriter-leads?lead={intake_id}"
    assert aq._file_href(bucket_id, None) == f"/admin/buckets?bucket={bucket_id}"


async def test_a_full_load_composes_every_loader_without_a_database() -> None:
    intake_id, bucket_id, profile_id = uuid4(), uuid4(), uuid4()
    intake = SimpleNamespace(
        id=intake_id, bucket_id=bucket_id, full_name="Pat Lee", business_name="Lee Motors",
        created_at=NOW - timedelta(days=12),
        intake_state={"resume_email": {"ok": True, "sent_at": "2026-10-20T10:00:00+00:00"}},
    )
    orphan_bucket = SimpleNamespace(id=uuid4(), name="Loan File", client_name=None, created_at=NOW - timedelta(days=3))
    db = _DB([
        [(intake, profile_id)],                                   # intakes without files
        [orphan_bucket],                                          # buckets with no client entry
        [],                                                       #   their intake map
        [(bucket_id, "Lee Motors room", "Lee Motors", 6, NOW - timedelta(days=45))],  # overdue requests
        [(bucket_id, intake_id)],                                 #   their intake map
        [(bucket_id, "Lee Motors room", "Lee Motors", 3, NOW - timedelta(days=16))],  # stale action items
        [(bucket_id, intake_id)],                                 #   their intake map
    ])

    queue = await aq.load_attention_queue(db, now=NOW)

    assert queue.counts == {"intake_no_files": 1, "no_client_entry": 1, "request_overdue": 1, "action_items_stale": 1}
    first, second, third, fourth = queue.items
    assert first.kind == "intake_no_files" and first.title == "Lee Motors"
    assert first.actions == ["resend_link"] and first.profile_id == profile_id
    assert "link sent 10d ago" in first.detail
    assert first.href == f"/admin/ai-underwriter-leads?lead={intake_id}"
    assert second.kind == "no_client_entry" and second.href.startswith("/admin/buckets?bucket=")
    assert third.kind == "request_overdue" and third.count == 6 and third.severity == "warn"
    assert fourth.kind == "action_items_stale" and fourth.count == 3 and fourth.severity == "bad"


async def test_an_intake_with_no_profile_cannot_offer_a_resend() -> None:
    intake = SimpleNamespace(
        id=uuid4(), bucket_id=uuid4(), full_name=None, business_name=None,
        created_at=NOW - timedelta(days=9), intake_state=None,
    )
    db = _DB([[(intake, None)], [], [], [], [], [], []])
    queue = await aq.load_attention_queue(db, now=NOW)
    (item,) = queue.items
    assert item.title == "Unnamed intake"
    assert item.actions == []
    assert "no link delivery recorded" in item.detail
