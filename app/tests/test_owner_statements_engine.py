"""A statement for each owner, through the real form engine and the real checklist.

`test_owner_statements.py` pins the rules with plain objects. These run the same
rules where they have to hold: the versioned form engine (`financial_workspace`),
the borrower's own submit path, the older readers that used to hand back "the
file's statement", and `get_program_readiness`, on a SQLite copy of the schema
with the real ORM.

Every person here is made up.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID

import pytest
from sqlalchemy import select, text

from app.enums import Role
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.application_profile import ApplicationOwner, ApplicationProfile
from app.models.bucket import Bucket, BucketFile, BucketFileAnalysis, BucketRequestedDocument
from app.models.financial_form_link import FinancialFormLink
from app.models.financial_form_version import FinancialFormFamily, FinancialFormVersion
from app.models.user import User
from app.services import (
    application_programs,
    client_financial_forms,
    financial_statements,
    pfs_schema,
)
from app.services import financial_workspace as ws
from app.services import owner_statements as os_
from app.tests._sqlite_schema import sqlite_session, sqlite_template


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    return sqlite_template(tmp_path_factory, "owner_statements")


@pytest.fixture
async def db(template_database, tmp_path):
    async with sqlite_session(template_database, tmp_path) as session:
        # The owner link table's check constraint is written in Postgres's cast
        # syntax, so the shared SQLite copy of the schema leaves the table out.
        await session.execute(text(
            "CREATE TABLE IF NOT EXISTS financial_statement_owners ("
            "id VARCHAR(36) PRIMARY KEY, statement_id VARCHAR(36) NOT NULL, "
            "application_owner_id VARCHAR(36), dealer_owner_id VARCHAR(36))"
        ))
        await session.commit()
        yield session


@pytest.fixture(autouse=True)
def no_storage_and_no_side_effects():
    """A saved version draws a PDF and writes it to storage, then refreshes advisory caches. None of that is under test."""
    with (
        patch.object(ws, "_render", return_value=b"%PDF-1.4 made-up statement"),
        patch.object(ws, "_write_pdf", return_value=None),
        patch("app.routers.buckets._bucket_storage_config", return_value=("made-up-bucket", "made-up-prefix", "made-up-key")),
        patch("app.services.financial_publication.after_publication", AsyncMock()),
    ):
        yield


async def _file(db, *, shares=(50, 50), on_checklist=True, level="required"):
    """A Main Street file with owners. Its initial checklist lists the personal
    statement at `level`; as "optional" the file asks for none."""
    desk = User(clerk_id="desk", email="desk@example.com", name="Sam Desk", role=Role.LOAN_EXEC)
    bucket = Bucket(name="Sample Fabrication LLC")
    db.add_all([desk, bucket])
    await db.flush()
    profile = ApplicationProfile(vertical="main_street", funding_category="general", primary_bucket_id=bucket.id)
    policy = AIPlaybookTemplate(
        owner_type="platform", playbook_type="evidence_policy", product_key="business_baseline",
        name="Initial evidence checklist", version=1, status="published", is_active=True,
        published_at=datetime.now(UTC) - timedelta(days=10),
    )
    db.add_all([profile, policy])
    await db.flush()
    db.add(AICollectionRequirement(
        playbook_id=policy.id, requirement_key=os_.REQUIREMENT_KEY, requirement_kind="document",
        label="Owner personal financial statement", category="financials", required_level=level,
        visibility=["borrower", "underwriter"], verification_required=False, can_underwriter_waive=True,
        completion_mode="ai_can_complete", objective_text="Review required owners.", completion_criteria="One per owner.",
    ))
    names = [("Jordan", "Sample"), ("Alex", "Rivera"), ("Riley", "Chen")]
    owners = []
    for index, share in enumerate(shares):
        first, last = names[index]
        person = ApplicationOwner(
            profile_id=profile.id, first_name=first, last_name=last, ownership_pct=share,
            email=f"{first.lower()}@example.com", phone="555-0100", is_primary=index == 0,
            street="1 Sample Way", city="Springfield", state="NJ", zip="07001",
            created_at=datetime.now(UTC) - timedelta(days=30 - index),
        )
        owners.append(person)
    db.add_all(owners)
    slot = None
    if on_checklist:
        slot = BucketRequestedDocument(
            bucket_id=bucket.id, name="Personal financial statement", category="Personal Financials",
            description="Upload a completed personal financial statement (PFS) for each owner.", required=True, status="requested",
        )
        db.add(slot)
    await db.flush()
    # The checklist as the desk first opens it.
    await application_programs.get_program_readiness(db, profile)
    return SimpleNamespace(desk=desk, bucket=bucket, profile=profile, owners=owners, slot=slot)


def _answers(cash):
    """Every liquid line answered: the engine states a liquidity figure only when none is left blank."""
    body = pfs_schema.empty_body()
    for key in pfs_schema.LIQUID_ASSET_KEYS:
        body["assets"][key] = 0
    body["assets"]["cash_on_hand"] = cash
    body["as_of"] = "2026-11-01"
    return body


async def _submit(db, profile, link, cash, key="first-submit"):
    """The borrower pressing Save on their own page, as the public route does it."""
    snapshot = await client_financial_forms.ensure_snapshot(db, profile, "pfs", statement_id=link.statement_id, family_id=link.financial_family_id)
    body = {**_answers(cash), "applicant": snapshot["body"]["applicant"]}
    base = snapshot["draft"]["base_version_id"]
    return await client_financial_forms.save_submission(
        db, profile, snapshot, body=body, expected_revision=snapshot["draft"]["revision"],
        base_version_id=UUID(base) if base else None,
        idempotency_key=client_financial_forms.submission_key(link.id, f"{key}-{cash}"),
        actor=SimpleNamespace(id=None, name="Borrower", email=""),
    )


@pytest.mark.asyncio
async def test_each_owner_gets_their_own_statement_seeded_with_their_own_details(db):
    file = await _file(db)
    jordan, alex = file.owners
    mine = await os_.statement_for(db, file.profile, jordan, actor=file.desk)
    theirs = await os_.statement_for(db, file.profile, alex, actor=file.desk)
    assert mine.id != theirs.id
    assert (await os_.statement_for(db, file.profile, alex, actor=file.desk)).id == theirs.id
    assert theirs.identity["owner_ids"] == [str(alex.id)] and theirs.label == "Personal financial statement · Alex Rivera"
    opened = await ws.load_workspace(db, file.profile, kind="pfs", family_id=theirs.id)
    assert opened["body"]["applicant"]["name"] == "Alex Rivera"
    assert opened["body"]["applicant"]["home_address"] == "1 Sample Way, Springfield, NJ 07001"
    model = await os_.read(db, file.profile)
    assert (model["required_count"], model["received_count"]) == (2, 0)
    assert [item["status"] for item in model["owners"]] == ["not_started", "not_started"]


@pytest.mark.asyncio
async def test_a_reader_that_names_nobody_never_gets_a_co_owners_statement(db):
    file = await _file(db)
    jordan, alex = file.owners
    theirs = await os_.statement_for(db, file.profile, alex, actor=file.desk)
    # The engine's own "the file's statement" lookups, with only the co-owner's on file.
    assert await ws.existing_family(db, file.profile.id, "pfs") is None
    assert await financial_statements.latest_for_profile(db, file.profile.id) is None
    family, _draft = await ws.resolve_family(db, file.profile, kind="pfs", actor=file.desk)
    assert family.id != theirs.id and family.identity["owner_ids"] == [str(jordan.id)]
    # And from then on it is the applicant's own, whichever was made last.
    assert (await ws.existing_family(db, file.profile.id, "pfs")).id == family.id
    assert await os_.link_target(db, file.profile) == (None, family.id)
    assert await os_.link_target(db, file.profile, email="alex@example.com") == (None, theirs.id)


@pytest.mark.asyncio
async def test_an_owners_link_opens_and_files_only_their_statement_and_the_row_waits_for_the_other(db):
    file = await _file(db)
    jordan, alex = file.owners
    link, token = await os_.mint_link(db, file.profile, alex, user=file.desk, label="Emailed to Alex Rivera", invitee_email="alex@example.com")
    assert token and link.financial_family_id is not None and link.invitee_email == "alex@example.com"
    assert (await os_.person_for_family(db, file.profile, link.financial_family_id)).id == alex.id

    saved = await _submit(db, file.profile, link, 42000)
    assert saved["saved_version"]["version_number"] == 1 and saved["saved_version"]["status"] == "filed"
    # One owner's statement does not close a row that asks every owner for one.
    assert file.slot.status == "requested"
    model = await os_.read(db, file.profile)
    assert [item["status"] for item in model["owners"]] == ["not_started", "received"]
    assert model["missing_owner_ids"] == [str(jordan.id)] and not model["complete"]
    theirs = model["owners"][1]["statement"]
    assert theirs["versions"][0]["version_number"] == 1 and theirs["link"]["sent_to"] == "alex@example.com"

    mine, _ = await os_.mint_link(db, file.profile, jordan, user=file.desk, label="Copied for Jordan Sample")
    await _submit(db, file.profile, mine, 9000)
    assert file.slot.status == "uploaded"
    model = await os_.read(db, file.profile)
    assert model["complete"] and model["received_count"] == 2
    # Two people, two statements: each figure stays with its own owner.
    figures = [item["statement"]["figures"]["liquid_assets"] for item in model["owners"]]
    assert figures == [9000.0, 42000.0]


@pytest.mark.asyncio
async def test_every_saved_version_stays_in_that_owners_history(db):
    file = await _file(db)
    _jordan, alex = file.owners
    link, _ = await os_.mint_link(db, file.profile, alex, user=file.desk, label="Emailed to Alex Rivera")
    await _submit(db, file.profile, link, 42000)
    await _submit(db, file.profile, link, 55000, key="correction")
    model = await os_.read(db, file.profile)
    history = model["owners"][1]["statement"]["versions"]
    assert [(row["version_number"], row["is_current"]) for row in history] == [(2, True), (1, False)]
    assert [row["figures"]["liquid_assets"] for row in history] == [55000.0, 42000.0]
    assert model["owners"][0]["statement"] is None
    families = (await db.execute(select(FinancialFormFamily).where(FinancialFormFamily.profile_id == file.profile.id))).scalars().all()
    assert len(families) == 1


@pytest.mark.asyncio
async def test_the_checklist_row_reads_one_of_two_until_both_owners_are_in(db):
    file = await _file(db)
    jordan, alex = file.owners
    link, _ = await os_.mint_link(db, file.profile, alex, user=file.desk, label="Emailed to Alex Rivera")
    await _submit(db, file.profile, link, 42000)

    readiness = await application_programs.get_program_readiness(db, file.profile)
    row = next(item for item in readiness.requirements if item.requirement_key == os_.REQUIREMENT_KEY)
    assert row.status != "verified"
    assert row.coverage["unit"] == "owner statements" and (row.coverage["current"], row.coverage["required"]) == (1, 2)
    assert row.provenance["owner_statements"]["missing"] == ["Jordan Sample"]
    assert row.state_reason.startswith("1 of 2 owner statements received. Still needed from Jordan Sample.")
    slot = await db.get(BucketRequestedDocument, row.requested_document_id)
    assert slot.status == "requested" and "Still needed from Jordan Sample." in slot.description

    mine, _ = await os_.mint_link(db, file.profile, jordan, user=file.desk, label="Copied for Jordan Sample")
    await _submit(db, file.profile, mine, 9000)
    readiness = await application_programs.get_program_readiness(db, file.profile)
    row = next(item for item in readiness.requirements if item.requirement_key == os_.REQUIREMENT_KEY)
    assert (row.coverage["current"], row.coverage["required"], row.coverage["complete"]) == (2, 2, True)
    assert row.provenance["owner_statements"]["missing"] == []


@pytest.mark.asyncio
async def test_the_desk_can_say_a_statement_is_not_needed_from_one_owner_and_take_it_back(db):
    file = await _file(db)
    jordan, alex = file.owners
    link, _ = await os_.mint_link(db, file.profile, jordan, user=file.desk, label="Copied for Jordan Sample")
    await _submit(db, file.profile, link, 9000)
    await os_.set_requirement(db, file.profile, alex, required=False, reason="Silent partner, the lender waived it", user=file.desk)
    model = await os_.read(db, file.profile)
    assert (model["required_count"], model["complete"]) == (1, True)
    assert model["owners"][1]["status"] == "not_needed" and model["owners"][1]["request_by"] == "Sam Desk"
    await os_.set_requirement(db, file.profile, alex, required=True, reason=None, user=file.desk)
    model = await os_.read(db, file.profile)
    assert (model["required_count"], model["complete"]) == (2, False)


@pytest.mark.asyncio
async def test_an_owner_under_twenty_percent_owes_one_only_once_the_desk_asks(db):
    file = await _file(db, shares=(90, 10))
    _jordan, minor = file.owners
    assert (await os_.read(db, file.profile))["required_count"] == 1
    await os_.mint_link(db, file.profile, minor, user=file.desk, label="Copied for Alex Rivera")
    model = await os_.read(db, file.profile)
    assert model["required_count"] == 2 and model["owners"][1]["reason"] == "requested"


async def _upload(db, file, name, *, applicant=None):
    document = BucketFile(
        bucket_id=file.bucket.id, file_name=name, s3_key=f"made-up/{name}", content_type="application/pdf",
        size_bytes=10, status="uploaded", content_hash="a" * 64,
    )
    db.add(document)
    await db.flush()
    facts = {"statement_date": "2026-10-01", "net_worth": 250000, "liquid_assets": 15000, **({"applicant_name": applicant} if applicant else {})}
    db.add(BucketFileAnalysis(
        bucket_file_id=document.id, bucket_id=file.bucket.id, content_hash="a" * 64, analysis_version=1,
        provider="test", status="completed", classification="personal_financial_statement", confidence="high",
        summary="Made-up statement", analysis={"key_facts": facts},
    ))
    await db.flush()
    return document


@pytest.mark.asyncio
async def test_an_upload_tied_to_no_statement_is_listed_and_joins_the_history_of_the_owner_the_desk_names(db):
    file = await _file(db)
    _jordan, alex = file.owners
    document = await _upload(db, file, "statement-scan.pdf")
    model = await os_.read(db, file.profile)
    assert [item["file_name"] for item in model["loose_files"]] == ["statement-scan.pdf"]
    assert model["received_count"] == 0
    await os_.adopt_file(db, file.profile, document.id, alex, user=file.desk)
    model = await os_.read(db, file.profile)
    assert model["loose_files"] == [] and model["owners"][1]["status"] == "received"
    version = model["owners"][1]["statement"]["versions"][0]
    assert version["source"] == "original" and version["awaiting_review"] and version["figures"]["net_worth"] == 250000.0
    with pytest.raises(Exception) as refused:
        await os_.adopt_file(db, file.profile, document.id, alex, user=file.desk)
    assert getattr(refused.value, "status_code", None) == 409


@pytest.mark.asyncio
async def test_the_desk_can_move_a_statement_to_another_owner_make_it_joint_or_set_it_aside(db):
    file = await _file(db)
    jordan, alex = file.owners
    family = await os_.statement_for(db, file.profile, jordan, actor=file.desk)
    link, _ = await os_.mint_link(db, file.profile, jordan, user=file.desk, label="Copied for Jordan Sample")
    await _submit(db, file.profile, link, 9000)

    await os_.assign(db, file.profile, family.id, [jordan.id, alex.id], user=file.desk)
    model = await os_.read(db, file.profile)
    assert model["complete"] and [item["joint_with"] for item in model["owners"]] == [["Alex Rivera"], ["Jordan Sample"]]
    assert (await db.get(FinancialFormFamily, family.id)).label == "Personal financial statement · Jordan Sample & Alex Rivera"

    await os_.assign(db, file.profile, family.id, [alex.id], user=file.desk)
    model = await os_.read(db, file.profile)
    assert [item["status"] for item in model["owners"]] == ["not_started", "received"]

    # "Not theirs": it leaves the owner and waits to be assigned, rather than being matched back by its name.
    await os_.assign(db, file.profile, family.id, [], user=file.desk)
    model = await os_.read(db, file.profile)
    assert model["received_count"] == 0 and [item["family_id"] for item in model["unassigned"]] == [str(family.id)]
    # "Not an owner's": set aside for good, and no longer listed.
    await os_.assign(db, file.profile, family.id, [], user=file.desk, set_aside=True)
    model = await os_.read(db, file.profile)
    assert model["received_count"] == 0 and model["unassigned"] == []
    await os_.assign(db, file.profile, family.id, [jordan.id], user=file.desk)
    assert "set_aside" not in (await db.get(FinancialFormFamily, family.id)).identity


@pytest.mark.asyncio
async def test_a_statement_filled_in_before_owners_were_tracked_is_adopted_as_the_applicants(db):
    file = await _file(db)
    jordan, _alex = file.owners
    # The older path: a stored statement with no family behind it yet.
    old = await financial_statements.save_statement(db, file.profile, body=_answers(7000), status="draft")
    assert (await financial_statements.latest_for_profile(db, file.profile.id)).id == old.id
    assert await os_.link_target(db, file.profile) == (old.id, None)
    family, _draft = await ws.resolve_family(db, file.profile, kind="pfs", statement_id=old.id)
    tracker = await os_.load(db, file.profile)
    row = tracker.row_for(jordan.id)
    assert row.statement.family.id == family.id and row.how == "name"
    assert (await ws.existing_family(db, file.profile.id, "pfs")).id == family.id


@pytest.mark.asyncio
async def test_a_file_with_no_owner_rows_behaves_as_it_always_did(db):
    file = await _file(db, shares=())
    assert await os_.link_target(db, file.profile) == (None, None)
    family, _draft = await ws.resolve_family(db, file.profile, kind="pfs")
    assert (await ws.existing_family(db, file.profile.id, "pfs")).id == family.id
    link, _token = await financial_statements.mint_link(db, file.profile, kind="pfs")
    link.financial_family_id = family.id
    await _submit(db, file.profile, link, 3000)
    assert file.slot.status == "uploaded"
    model = await os_.read(db, file.profile)
    assert model["owners"] == [] and model["required_count"] == 0
    readiness = await application_programs.get_program_readiness(db, file.profile)
    row = next(item for item in readiness.requirements if item.requirement_key == os_.REQUIREMENT_KEY)
    assert "owner_statements" not in row.provenance and row.coverage.get("unit") == "documents"


@pytest.mark.asyncio
async def test_the_link_is_stored_hashed_and_bound_to_the_owners_statement(db):
    file = await _file(db)
    _jordan, alex = file.owners
    link, token = await os_.mint_link(db, file.profile, alex, user=file.desk, label="Emailed to Alex Rivera", invitee_email="alex@example.com")
    stored = (await db.execute(select(FinancialFormLink).where(FinancialFormLink.id == link.id))).scalar_one()
    assert stored.token_hash == financial_statements.hash_token(token) and token not in stored.token_hash
    family = await db.get(FinancialFormFamily, stored.financial_family_id)
    assert family.identity["owner_ids"] == [str(alex.id)]
    versions = (await db.execute(select(FinancialFormVersion).where(FinancialFormVersion.family_id == family.id))).scalars().all()
    assert versions == []


@pytest.mark.asyncio
async def test_emailing_an_owner_sends_their_own_link_to_their_own_address_once(db):
    from app.config import get_settings
    from app.models.application_profile import ApplicationRoomDelivery

    file = await _file(db)
    _jordan, alex = file.owners
    sent = AsyncMock(return_value=SimpleNamespace(ok=True, detail="Sent", message_id="made-up-message"))
    with patch("app.services.email.user_mailer.send_as_user", sent):
        delivery = await os_.email_link(db, file.profile, alex, user=file.desk, slot=file.slot)
        again = await os_.email_link(db, file.profile, alex, user=file.desk, slot=file.slot)
    assert again.id == delivery.id and sent.await_count == 1
    call = sent.await_args.kwargs
    assert call["to_emails"] == ["alex@example.com"]
    assert call["subject"] == "Action needed: your personal financial statement"
    assert f"{get_settings().frontend_app_url.rstrip('/')}/forms/pfs/" in call["body_text"] and "Hello Alex," in call["body_text"]
    stored = (await db.execute(select(ApplicationRoomDelivery).where(ApplicationRoomDelivery.id == delivery.id))).scalar_one()
    assert (stored.action_kind, stored.status, stored.recipient_email) == ("owner_statement_link", "sent", "alex@example.com")
    assert stored.provider_result["owner_id"] == str(alex.id) and "token" not in str(stored.provider_result)
    links = (await db.execute(select(FinancialFormLink).where(FinancialFormLink.profile_id == file.profile.id))).scalars().all()
    assert len(links) == 1 and links[0].invitee_email == "alex@example.com" and links[0].financial_family_id is not None
    model = await os_.read(db, file.profile)
    assert [item["status"] for item in model["owners"]] == ["not_started", "sent"]


@pytest.mark.asyncio
async def test_an_uploaded_statement_lands_in_the_history_of_the_owner_it_names_and_an_unnamed_one_waits(db):
    import hashlib

    file = await _file(db)
    jordan, alex = file.owners
    mine = await os_.statement_for(db, file.profile, jordan, actor=file.desk)
    theirs = await os_.statement_for(db, file.profile, alex, actor=file.desk)
    raw = b"%PDF-1.4 made-up upload"
    digest = hashlib.sha256(raw).hexdigest()

    async def analysed(name, applicant):
        document = await _upload(db, file, name, applicant=applicant)
        document.content_hash = digest
        analysis = (await db.execute(select(BucketFileAnalysis).where(BucketFileAnalysis.bucket_file_id == document.id))).scalar_one()
        analysis.content_hash = digest
        await db.flush()
        with patch.object(ws, "_read_file", return_value=raw):
            assert await ws.register_uploaded_financial_document(db, document, analysis) is True
        return document

    named = await analysed("alex-statement.pdf", "Alex Rivera")
    version = (await db.execute(select(FinancialFormVersion).where(FinancialFormVersion.source_file_id == named.id))).scalar_one()
    assert version.family_id == theirs.id and version.family_id != mine.id

    unnamed = await analysed("scan-0042.pdf", None)
    version = (await db.execute(select(FinancialFormVersion).where(FinancialFormVersion.source_file_id == unnamed.id))).scalar_one()
    assert version.family_id not in {mine.id, theirs.id}
    model = await os_.read(db, file.profile)
    assert [item["status"] for item in model["owners"]] == ["not_started", "received"]
    assert len(model["unassigned"]) == 1 and model["unassigned"][0]["received"]
    # The desk says whose it is, and it counts.
    await os_.assign(db, file.profile, version.family_id, [jordan.id], user=file.desk)
    model = await os_.read(db, file.profile)
    assert model["complete"] and model["unassigned"] == []


@pytest.mark.asyncio
async def test_the_older_copy_link_path_gives_the_applicant_their_own_statement_on_a_two_owner_file(db):
    """The desk's generic "copy link", the packet, the room's "fill in online" and the signed-in
    client's page still go through the older lookups. With only the co-owner's statement on file
    they get the applicant's own versioned statement, started for them: never Alex's, and never a
    legacy statement row beside the versioned ones."""
    from app.services.financial_workspace_legacy import managed_snapshot

    file = await _file(db)
    jordan, alex = file.owners
    theirs = await os_.statement_for(db, file.profile, alex, actor=file.desk)
    link_for_alex, _ = await os_.mint_link(db, file.profile, alex, user=file.desk, label="Emailed to Alex Rivera")
    await _submit(db, file.profile, link_for_alex, 42000)

    # What `mint_financial_form_link`, `_mint_packet` and the room's form-open route do, step for step.
    assert await financial_statements.latest_for_profile(db, file.profile.id) is None
    managed = await managed_snapshot(db, file.profile, "pfs")
    assert managed is not None and managed["family_id"] != str(theirs.id)
    assert managed["body"]["applicant"]["name"] == "Jordan Sample" and managed["versions"] == []
    tracker = await os_.load(db, file.profile)
    assert str(tracker.row_for(jordan.id).statement.family.id) == managed["family_id"]
    assert tracker.row_for(alex.id).statement.family.id == theirs.id
    # Asked again, it is the same statement, not a third.
    assert (await managed_snapshot(db, file.profile, "pfs"))["family_id"] == managed["family_id"]
    families = (await db.execute(select(FinancialFormFamily).where(FinancialFormFamily.profile_id == file.profile.id))).scalars().all()
    assert len(families) == 2


@pytest.mark.asyncio
async def test_a_file_that_keeps_versions_for_any_owner_refuses_legacy_statement_writes(db):
    """The old statement writers and PDF refresher are kept off a file that keeps versions,
    whichever owner's statement those versions are."""
    from fastapi import HTTPException

    file = await _file(db)
    _jordan, alex = file.owners
    assert await ws.has_managed_family(db, file.profile, "pfs") is False
    await os_.statement_for(db, file.profile, alex, actor=file.desk)
    # Only the co-owner has one, and the applicant's lookup finds nothing of theirs ...
    assert await ws.existing_family(db, file.profile.id, "pfs") is None
    # ... but the file keeps versions, so a legacy write is refused rather than written beside them.
    assert await ws.has_managed_family(db, file.profile, "pfs") is True
    with pytest.raises(HTTPException) as refused:
        await financial_statements.save_statement(db, file.profile, body=pfs_schema.empty_body(), status="draft")
    assert refused.value.status_code == 409


@pytest.mark.asyncio
async def test_the_ai_review_counts_each_person_once_and_holds_the_row_until_every_owner_is_in(db):
    from app.services import bucket_ai

    file = await _file(db)
    jordan, alex = file.owners
    link, _ = await os_.mint_link(db, file.profile, alex, user=file.desk, label="Emailed to Alex Rivera")
    await _submit(db, file.profile, link, 42000)
    owners_in, uncounted = await os_.review_context(db, file.profile)
    assert owners_in is False and uncounted == set()
    file.slot.status = "requested"
    held = bucket_ai._mark_satisfied_requests([file.slot], {"personal_financial_statement"}, held_classes={"personal_financial_statement"})
    assert held == 0 and file.slot.status == "requested"

    # An older upload of Alex's that arrived without a name started its own statement, and the desk
    # then said it is Alex's: the same person twice, in two statements.
    document = await _upload(db, file, "statement-scan.pdf")
    document.created_at = datetime.now(UTC) - timedelta(days=40)
    second = await os_._start(db, file.profile, alex, actor=file.desk)
    old = FinancialFormVersion(
        family_id=second.id, version_number=1, schema_version="sba413.v1", body={}, key_facts={"liquid_assets": 15000},
        body_sha256=ws.body_hash({}), source="original", status="original", source_file_id=document.id, pdf_file_id=document.id,
        created_at=datetime.now(UTC) - timedelta(days=40),
    )
    db.add(old)
    await db.flush()
    second.current_version_id = old.id
    await db.flush()
    tracker = await os_.load(db, file.profile)
    assert len(tracker.row_for(alex.id).statements) == 2
    # The newer statement counts for Alex; the older one is left out of any combined figure.
    assert tracker.row_for(alex.id).statement.family.id != second.id
    owners_in, uncounted = await os_.review_context(db, file.profile)
    assert str(document.id) in uncounted
    rows = [
        {"file_id": fid, "ai_classification": "personal_financial_statement", "key_facts": {"liquid_assets": cash}}
        for fid, cash in ((str(document.id), 15000), ("online-statement", 42000))
    ]
    result = {"key_metrics": {}}
    bucket_ai._compute_key_metrics_from_cache(result, rows, pfs_uncounted_file_ids=uncounted)
    assert result["key_metrics"]["pfs_total_liquid_assets"] == 42000


# ---------------------------------------------------------------------------
# Only when the file asks for statements
# ---------------------------------------------------------------------------


async def _pfs_rows(db, bucket_id):
    """Every room row for a personal statement: the room's own category, or the requirement's."""
    from sqlalchemy import or_

    return list((await db.execute(select(BucketRequestedDocument).where(
        BucketRequestedDocument.bucket_id == bucket_id,
        or_(BucketRequestedDocument.category == "Personal Financials",
            BucketRequestedDocument.requirement_key == os_.REQUIREMENT_KEY),
    ))).scalars().all())


def _keys(readiness):
    return {item.requirement_key for item in readiness.requirements}


@pytest.mark.asyncio
async def test_a_checklist_that_lists_the_statement_as_optional_asks_for_none(db):
    file = await _file(db, level="optional", on_checklist=False)
    # The row the dealer intake used to seed on every file, before the checklist adopted it.
    seeded = BucketRequestedDocument(
        bucket_id=file.bucket.id, name="Personal financial statement", category="Personal Financials",
        requirement_key=os_.REQUIREMENT_KEY, required=True, status="requested",
    )
    db.add(seeded)
    await db.flush()
    readiness = await application_programs.get_program_readiness(db, file.profile)
    assert os_.REQUIREMENT_KEY not in _keys(readiness)
    assert seeded.status == "not_applicable"
    model = await os_.read(db, file.profile)
    assert model["needed"] is False and model["required_count"] == 0 and model["asked_by"] == []
    assert model["on_checklist"] is False and model["loose_files"] == []
    assert await os_.all_received(db, file.profile)
    assert (await os_.review_context(db, file.profile))[0] is True


@pytest.mark.asyncio
async def test_the_checklist_drops_the_statement_when_its_last_asker_does_and_takes_it_back(db):
    file = await _file(db, level="recommended")
    readiness = await application_programs.get_program_readiness(db, file.profile)
    row = next(item for item in readiness.requirements if item.requirement_key == os_.REQUIREMENT_KEY)
    slot = await db.get(BucketRequestedDocument, row.requested_document_id)
    assert slot.status == "requested" and (await os_.need(db, file.profile)).needed

    definition = (await db.execute(select(AICollectionRequirement).where(
        AICollectionRequirement.requirement_key == os_.REQUIREMENT_KEY))).scalar_one()
    definition.required_level = "optional"
    await db.flush()
    readiness = await application_programs.get_program_readiness(db, file.profile)
    assert os_.REQUIREMENT_KEY not in _keys(readiness)
    state = await os_._state(db, file.profile)
    assert state.status == "stale" and state.state_reason == os_.DORMANT_REASON
    assert state.provenance["inactive_definition"] is True and slot.status == "not_applicable"
    assert not (await os_.need(db, file.profile)).needed

    definition.required_level = "required"
    await db.flush()
    readiness = await application_programs.get_program_readiness(db, file.profile)
    row = next(item for item in readiness.requirements if item.requirement_key == os_.REQUIREMENT_KEY)
    assert row.requested_document_id == slot.id and slot.status == "requested"
    assert (await os_.read(db, file.profile))["required_count"] == 2


@pytest.mark.asyncio
async def test_the_desk_asking_puts_statements_on_a_file_that_asked_for_none(db):
    from app.services.requirement_library import STAFF_POLICY_KEY

    file = await _file(db, level="optional", on_checklist=False)
    assert not (await os_.need(db, file.profile)).needed
    slot, already = await os_.ensure_request(db, file.profile, actor=file.desk)
    assert already is False and slot.status == "requested"
    readiness = await application_programs.get_program_readiness(db, file.profile)
    row = next(item for item in readiness.requirements if item.requirement_key == os_.REQUIREMENT_KEY)
    assert row.required_level == "required" and STAFF_POLICY_KEY in row.source_policy_keys
    model = await os_.read(db, file.profile)
    assert model["needed"] is True and model["asked_by"] == ["Added by Sam Desk"]
    assert model["required_count"] == 2 and model["on_checklist"] is True
    again, already = await os_.ensure_request(db, file.profile, actor=file.desk)
    assert already is True and again.id == slot.id
    assert [row.id for row in await _pfs_rows(db, file.bucket.id)] == [slot.id]
    # A statement filed now lands on that same row rather than opening a second.
    jordan, _alex = file.owners
    link, _ = await os_.mint_link(db, file.profile, jordan, user=file.desk, label="Copied for Jordan Sample")
    await _submit(db, file.profile, link, 12000)
    assert [row.id for row in await _pfs_rows(db, file.bucket.id)] == [slot.id]


@pytest.mark.asyncio
async def test_sending_an_owner_a_link_is_the_desk_asking(db):
    file = await _file(db, level="optional", on_checklist=False)
    _jordan, alex = file.owners
    await os_.mint_link(db, file.profile, alex, user=file.desk, label="Copied for Alex Rivera")
    await application_programs.get_program_readiness(db, file.profile)
    model = await os_.read(db, file.profile)
    assert model["needed"] is True and model["required_count"] == 2


@pytest.mark.asyncio
async def test_a_statement_saved_on_a_file_that_asks_for_none_is_kept_and_opens_no_row(db):
    file = await _file(db, level="optional", on_checklist=False)
    jordan, _alex = file.owners
    family = await os_.statement_for(db, file.profile, jordan, actor=file.desk)
    link, _token = await financial_statements.mint_link(db, file.profile, kind="pfs")
    link.financial_family_id = family.id
    await _submit(db, file.profile, link, 7000)
    assert await _pfs_rows(db, file.bucket.id) == []
    saved = (await db.execute(select(FinancialFormVersion).where(FinancialFormVersion.family_id == family.id))).scalars().all()
    assert saved
    for row in saved:
        assert (await db.get(BucketFile, row.pdf_file_id)).requested_document_id is None
    assert not (await os_.need(db, file.profile)).needed


@pytest.mark.asyncio
async def test_the_forms_packet_leaves_the_statement_out_unless_the_file_asks_for_one(db):
    from app.routers.application_profiles import _mint_packet

    async def kinds(file):
        packet = await _mint_packet(db, file.profile, file.desk)
        rows = (await db.execute(select(FinancialFormLink).where(FinancialFormLink.packet_id == packet["packet_id"]))).scalars().all()
        return {row.kind for row in rows}

    file = await _file(db, level="optional", on_checklist=False)
    assert await kinds(file) == {"p_and_l", "balance_sheet", "debt_schedule"}
    assert await _pfs_rows(db, file.bucket.id) == []
    await os_.ensure_request(db, file.profile, actor=file.desk)
    assert await kinds(file) == {"p_and_l", "balance_sheet", "debt_schedule", "pfs"}
