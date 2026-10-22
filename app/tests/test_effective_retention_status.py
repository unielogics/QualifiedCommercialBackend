from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.models.application_profile import ApplicationProfile
from app.models.bucket import Bucket, BucketFile
from app.models.operator_file import BucketIntakeLink
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.routers import application_profiles, buckets, dealer_ai_intake
from app.services import retention_holds


class _Rows:
    def __init__(self, rows: list[tuple]):
        self._rows = rows

    def all(self) -> list[tuple]:
        return self._rows


class _RetentionGraphSession:
    """Small query-aware fake for the effective-retention graph walker."""

    def __init__(
        self,
        *,
        buckets: list[tuple],
        intakes: list[tuple],
        profiles: list[tuple],
        files: list[tuple],
        links: list[tuple],
    ) -> None:
        self.buckets = buckets
        self.intakes = intakes
        self.profiles = profiles
        self.files = files
        self.links = links

    @staticmethod
    def _ids(statement, prefix: str) -> set[UUID]:
        params = statement.compile().params
        values: set[UUID] = set()
        for key, value in params.items():
            if key == prefix or key.startswith(f"{prefix}_"):
                if isinstance(value, (list, tuple, set)):
                    values.update(value)
                else:
                    values.add(value)
        return values

    async def execute(self, statement) -> _Rows:
        entity = statement.column_descriptions[0]["entity"]
        if entity is BucketIntakeLink:
            intake_ids = self._ids(statement, "intake_id")
            bucket_ids = self._ids(statement, "bucket_id")
            return _Rows(
                [
                    row
                    for row in self.links
                    if row[0] in intake_ids or row[1] in bucket_ids
                ]
            )
        if entity is PublicUnderwritingIntake:
            intake_ids = self._ids(statement, "id")
            bucket_ids = self._ids(statement, "bucket_id")
            return _Rows(
                [
                    row
                    for row in self.intakes
                    if row[0] in intake_ids or row[1] in bucket_ids
                ]
            )
        if entity is ApplicationProfile:
            profile_ids = self._ids(statement, "id")
            intake_ids = self._ids(statement, "intake_id")
            bucket_ids = self._ids(statement, "primary_bucket_id")
            return _Rows(
                [
                    row
                    for row in self.profiles
                    if row[0] in profile_ids
                    or row[1] in intake_ids
                    or row[2] in bucket_ids
                ]
            )
        if entity is Bucket:
            bucket_ids = self._ids(statement, "id")
            return _Rows([row for row in self.buckets if row[0] in bucket_ids])
        if entity is BucketFile:
            bucket_ids = self._ids(statement, "bucket_id")
            return _Rows([row[1:] for row in self.files if row[0] in bucket_ids])
        raise AssertionError(f"Unexpected retention graph query: {statement}")


def _record(record_id: UUID, **links):
    return SimpleNamespace(
        id=record_id,
        retention_class="ai_intake_record",
        protected_until=datetime(2028, 1, 1, tzinfo=UTC),
        legal_hold=False,
        **links,
    )


def _run_without_event_loop(coroutine):
    """Drive coroutines whose test doubles complete without yielding."""

    try:
        coroutine.send(None)
    except StopIteration as completed:
        return completed.value
    raise AssertionError("Retention graph unexpectedly awaited external I/O")


def test_effective_retention_is_consistent_across_indirect_graph() -> None:
    bucket_one_id = uuid4()
    bucket_two_id = uuid4()
    intake_one_id = uuid4()
    intake_two_id = uuid4()
    profile_id = uuid4()
    held_file_id = uuid4()
    historical_bucket_id = uuid4()
    historical_intake_id = uuid4()
    ordinary_deadline = datetime(2028, 1, 1, tzinfo=UTC)
    held_deadline = datetime(2032, 6, 1, tzinfo=UTC)

    session_data = {
        "buckets": [
            (bucket_one_id, ordinary_deadline, False),
            (bucket_two_id, ordinary_deadline, False),
            (historical_bucket_id, ordinary_deadline, False),
        ],
        "intakes": [
            (intake_one_id, bucket_one_id, ordinary_deadline, False),
            (intake_two_id, bucket_two_id, ordinary_deadline, False),
            (
                historical_intake_id,
                historical_bucket_id,
                ordinary_deadline,
                False,
            ),
        ],
        # The profile bridges intake one's Bucket to bucket two. A one-pass
        # traversal misses the legal hold on bucket two's file.
        "profiles": [
            (
                profile_id,
                intake_one_id,
                bucket_two_id,
                ordinary_deadline,
                False,
            )
        ],
        "files": [
            (historical_bucket_id, held_file_id, held_deadline, True),
        ],
        # Historical links remain in the retained graph after unlinked_at is
        # populated. The query intentionally has no active-link predicate.
        "links": [
            (intake_two_id, historical_bucket_id),
        ],
    }
    roots = [
        (
            _record(bucket_one_id),
            "bucket",
        ),
        (
            _record(intake_one_id, bucket_id=bucket_one_id),
            "ai_intake",
        ),
        (
            _record(
                profile_id,
                intake_id=intake_one_id,
                primary_bucket_id=bucket_two_id,
            ),
            "application_profile",
        ),
    ]

    results = []
    for record, resource_type in roots:
        results.append(
            _run_without_event_loop(
                retention_holds.effective_status_read(
                    _RetentionGraphSession(**session_data),
                    record,
                    resource_type=resource_type,
                )
            )
        )

    expected_source = f"bucket_file:{held_file_id}"
    for result in results:
        assert result["protected_until"] == held_deadline
        assert result["effective_legal_hold"] is True
        assert result["hold_sources"] == [expected_source]
        assert result["retention_state"] == "legal_hold"


def _route_contract(router) -> set[tuple[str, str]]:
    return {
        (route.path, method)
        for route in router.routes
        for method in getattr(route, "methods", set())
    }


def test_effective_retention_status_has_reloadable_get_routes() -> None:
    contracts = (
        _route_contract(buckets.router)
        | _route_contract(application_profiles.router)
        | _route_contract(dealer_ai_intake.admin_router)
    )
    assert ("/buckets/admin/{bucket_id}/retention", "GET") in contracts
    assert ("/application-profiles/{profile_id}/retention", "GET") in contracts
    assert (
        "/admin/ai-underwriter-leads/{intake_id}/retention",
        "GET",
    ) in contracts

