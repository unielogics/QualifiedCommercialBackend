"""Queue source identity stays singular and never guesses a resend profile."""
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Column, MetaData, Table, Uuid, create_engine, select

from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.services import attention_queue as queue


class CaptureDB:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self

    def all(self):
        return self.rows


async def test_profile_selection_is_one_intake_row_and_prefers_only_safe_lineage():
    capture = CaptureDB()
    assert await queue._intakes_without_files(capture, datetime.now(UTC)) == []
    source = capture.statements[0]
    assert len(source.get_final_froms()) == 1
    assert source.get_final_froms()[0].name == "public_underwriting_intakes"

    # Execute the actual correlated CASE expression against an ephemeral SQL
    # database. These minimal tables intentionally permit shared bucket IDs.
    metadata = MetaData()
    intakes = Table("public_underwriting_intakes", metadata,
        Column("id", Uuid, primary_key=True), Column("bucket_id", Uuid, nullable=False))
    profiles = Table("application_profiles", metadata, Column("id", Uuid, primary_key=True),
        Column("intake_id", Uuid, nullable=True), Column("primary_bucket_id", Uuid, nullable=True))
    engine = create_engine("sqlite:///:memory:")
    metadata.create_all(engine)
    scenarios = [dict(id=uuid4(), bucket_id=uuid4()) for _ in range(8)]
    correct_anchor, legacy_unique, wrong_anchor, tempting_fallback, other_intake_profile, none_bucket_anchor = [uuid4() for _ in range(6)]
    rows = [
        # The explicit intake anchor wins even when the bucket is shared.
        dict(id=correct_anchor, intake_id=scenarios[0]["id"], primary_bucket_id=scenarios[0]["bucket_id"]),
        dict(id=uuid4(), intake_id=None, primary_bucket_id=scenarios[0]["bucket_id"]),
        # A genuinely unanchored, unique legacy bucket profile is safe.
        dict(id=legacy_unique, intake_id=None, primary_bucket_id=scenarios[1]["bucket_id"]),
        # An anchor pointing elsewhere disables a tempting bucket fallback.
        dict(id=wrong_anchor, intake_id=scenarios[2]["id"], primary_bucket_id=uuid4()),
        dict(id=tempting_fallback, intake_id=None, primary_bucket_id=scenarios[2]["bucket_id"]),
        # Two unanchored profiles on one bucket provide no safe resend target.
        dict(id=uuid4(), intake_id=None, primary_bucket_id=scenarios[3]["bucket_id"]),
        dict(id=uuid4(), intake_id=None, primary_bucket_id=scenarios[3]["bucket_id"]),
        # A unique profile belonging to a different intake is not a fallback.
        dict(id=other_intake_profile, intake_id=uuid4(), primary_bucket_id=scenarios[4]["bucket_id"]),
        # A missing primary bucket does not prove the current intake's room.
        dict(id=none_bucket_anchor, intake_id=scenarios[5]["id"], primary_bucket_id=None),
        # Several profiles from other intakes are also ambiguous.
        dict(id=uuid4(), intake_id=uuid4(), primary_bucket_id=scenarios[6]["bucket_id"]),
        dict(id=uuid4(), intake_id=uuid4(), primary_bucket_id=scenarios[6]["bucket_id"]),
        # The eighth intake has no profile at all.
    ]
    try:
        with engine.begin() as connection:
            connection.execute(intakes.insert(), scenarios)
            connection.execute(profiles.insert(), rows)
            projection = select(PublicUnderwritingIntake.id, list(source.selected_columns)[-1])
            selected = dict(connection.execute(projection).all())
            assert len(selected) == len(scenarios)
            assert selected[scenarios[0]["id"]] == correct_anchor
            assert selected[scenarios[1]["id"]] == legacy_unique
            for scenario in scenarios[2:]:
                assert selected[scenario["id"]] is None
            # Creating another bucket profile later removes only the fallback;
            # the source remains one intake, with its explicit anchor intact.
            connection.execute(profiles.insert(), dict(id=uuid4(), intake_id=None, primary_bucket_id=scenarios[1]["bucket_id"]))
            after_creation = dict(connection.execute(projection).all())
            assert len(after_creation) == len(scenarios)
            assert after_creation[scenarios[0]["id"]] == correct_anchor
            assert after_creation[scenarios[1]["id"]] is None
    finally:
        engine.dispose()


async def test_bucket_drilldown_uses_only_unique_intake_identity_and_ignores_row_order():
    bucket_a, bucket_b, bucket_c = uuid4(), uuid4(), uuid4()
    intake_a, intake_b, intake_c, intake_d = uuid4(), uuid4(), uuid4(), uuid4()
    rows = [(bucket_a, intake_a), (bucket_b, intake_b), (bucket_b, intake_c),
            (bucket_c, intake_d), (bucket_c, intake_d)]
    expected = {bucket_a: intake_a, bucket_c: intake_d}
    for ordered in (rows, list(reversed(rows))):
        mapping = await queue._intake_by_bucket(CaptureDB(ordered), {bucket_a, bucket_b, bucket_c})
        assert mapping == expected
        assert queue._file_href(bucket_b, mapping.get(bucket_b)) == queue.bucket_href(bucket_b)
        assert queue._file_href(bucket_a, mapping.get(bucket_a)) == queue.lead_href(intake_a)


async def test_empty_bucket_set_performs_no_source_query():
    capture = CaptureDB()
    assert await queue._intake_by_bucket(capture, set()) == {}
    assert capture.statements == []
