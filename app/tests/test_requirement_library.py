"""The desk can add a requirement to one file from the library, and the file keeps it.

A file's checklist came only from its evidence policy and its selected programs;
a requirement the engine did not know was marked stale and dropped, so there was
nowhere to put one more document or one of the online forms for one file. These
tests pin the library (what is offered, one row per key, funding evidence only),
the add and remove routes, and the engine itself: the real `get_program_readiness`
runs against an in-memory SQLite copy of the schema (foreign keys left out of the
DDL, as only the ORM relationships matter here), so an added requirement is seen
the way production sees it, as a policy-sourced requirement under `staff_added`.
"""

from __future__ import annotations

import asyncio
import shutil
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles

from app.db import Base
from app.enums import Role
from app.models.ai_playbook import AICollectionRequirement, AIPlaybookTemplate
from app.models.application_profile import ApplicationProfile, ApplicationRequirementState
from app.models.user import User
from app.schemas.requirement_library import RequirementAddWrite
from app.services import application_programs
from app.services import requirement_library as lib


@compiles(JSONB, "sqlite")
def _sqlite_jsonb(_type, _compiler, **_kwargs):
    return "JSON"


@compiles(ARRAY, "sqlite")
def _sqlite_array(_type, _compiler, **_kwargs):
    return "JSON"


# ---------------------------------------------------------------------------
# A real engine on an in-memory-sized SQLite file, built once and copied per test.
# ---------------------------------------------------------------------------


async def _create_schema(path) -> None:
    import app.models  # noqa: F401  (registers every table)

    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    metadata = sa.MetaData()
    for table in Base.metadata.tables.values():
        clone = table.to_metadata(metadata)
        for constraint in list(clone.foreign_key_constraints):
            clone.constraints.discard(constraint)
        for column in clone.columns:
            column.foreign_keys.clear()
    for table in list(metadata.tables.values()):
        for column in table.columns:
            if column.server_default is not None:
                literal = str(getattr(column.server_default, "arg", ""))
                if "::jsonb" in literal:
                    column.server_default = sa.DefaultClause(sa.text(literal.replace("::jsonb", "")))
                elif "now()" in literal:
                    column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
                elif "gen_random_uuid" in literal:
                    column.server_default = None
        for index in table.indexes:
            condition = index.dialect_options["postgresql"].get("where")
            if condition is not None:
                index.dialect_options["sqlite"]["where"] = condition
        async with engine.begin() as connection:
            try:
                await connection.run_sync(lambda sync, current=table: metadata.create_all(sync, tables=[current]))
            except Exception:  # noqa: BLE001  a few tables use Postgres-only functions; none are used here
                pass
    await engine.dispose()


@pytest.fixture(scope="module")
def template_database(tmp_path_factory):
    path = tmp_path_factory.mktemp("requirement_library") / "template.db"
    asyncio.run(_create_schema(path))
    return path


@pytest.fixture
async def db(template_database, tmp_path):
    copy = tmp_path / "test.db"
    shutil.copy(template_database, copy)
    engine = create_async_engine(f"sqlite+aiosqlite:///{copy}")
    async with AsyncSession(engine, expire_on_commit=False) as session:
        yield session
    await engine.dispose()


def _requirement(playbook, key, label, *, category="financials", kind="document", level="required",
                 visibility=("borrower", "underwriter"), verification=True, objective=None):
    return AICollectionRequirement(
        playbook_id=playbook.id,
        requirement_key=key,
        requirement_kind=kind,
        label=label,
        category=category,
        required_level=level,
        visibility=list(visibility),
        verification_required=verification,
        can_underwriter_waive=True,
        completion_mode="requires_human_verify" if verification else "ai_can_complete",
        objective_text=objective if objective is not None else f"Review {label.lower()}.",
        completion_criteria="Confirm it is complete.",
    )


def _playbook(owner, kind, product_key, *, version=1, status="published", name=None, published_days_ago=10):
    return AIPlaybookTemplate(
        owner_type=owner,
        playbook_type=kind,
        product_key=product_key,
        name=name or f"{product_key} v{version}",
        version=version,
        status=status,
        is_active=True,
        published_at=datetime.now(UTC) - timedelta(days=published_days_ago),
    )


async def _seed(db):
    """A file with one baseline requirement, and a library around it."""
    desk = User(clerk_id="desk", email="desk@example.com", name="Franco", role=Role.LOAN_EXEC)
    profile = ApplicationProfile(vertical="main_street", funding_category="general")
    db.add_all([desk, profile])
    policy = _playbook("platform", "evidence_policy", "business_baseline", name="Initial evidence checklist")
    funding_old = _playbook("funding", "loan_product", "sba_7a", version=2, published_days_ago=60)
    funding_new = _playbook("funding", "loan_product", "sba_7a", version=3, published_days_ago=5)
    platform = _playbook("platform", "loan_product", "bridge")
    buyer = _playbook("platform", "buyer", None, name="Buyer playbook")
    draft = _playbook("funding", "loan_product", "ez_term", status="draft")
    db.add_all([policy, funding_old, funding_new, platform, buyer, draft])
    await db.flush()
    db.add_all([
        # the file's own baseline
        _requirement(policy, "business_debt_schedule", "Business debt schedule"),
        # two versions of one key: the newer funding playbook wins, with its label
        _requirement(funding_old, "business_tax_returns_2_years", "Last 2 years business tax returns"),
        _requirement(funding_new, "business_tax_returns_2_years", "Last 2 filed business tax returns"),
        _requirement(funding_new, "business_license", "Business / dealer license and ownership", category="compliance"),
        _requirement(funding_new, "internal_underwriter_review", "Staff review of capital uses", category="financials", visibility=("underwriter",)),
        # a platform loan product that only agents and underwriters see
        _requirement(platform, "insurance_declarations", "Insurance declarations page", category="insurance", visibility=("agent", "underwriter")),
        # never offered: wrong kind, wrong category, nobody who can see it, CRM playbook, unpublished
        _requirement(funding_new, "business_phone", "Business phone", kind="field", category="borrower_info"),
        _requirement(funding_new, "reminder_cadence", "Reminder cadence", category="communication"),
        _requirement(funding_new, "working_state", "AI working state", category="ai_internal"),
        _requirement(funding_new, "nobody_sees_this", "Hidden item", visibility=()),
        _requirement(buyer, "client_email", "Email", category="borrower_info", visibility=("agent", "underwriter")),
        _requirement(draft, "draft_only_item", "Only in a draft"),
    ])
    await db.flush()
    return SimpleNamespace(desk=desk, profile=profile, policy=policy)


def _add(key=None, **kwargs):
    return RequirementAddWrite(requirement_key=key, **kwargs)


# ---------------------------------------------------------------------------
# The library: what is offered.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_library_offers_funding_documents_one_per_key_and_never_the_agent_crm(db):
    await _seed(db)
    candidates = {item.key: item for item in await lib.library_candidates(db)}
    # one row per key, the newest funding playbook's wording
    assert candidates["business_tax_returns_2_years"].label == "Last 2 filed business tax returns"
    assert candidates["business_tax_returns_2_years"].source == "library"
    assert candidates["business_tax_returns_2_years"].client_visible is True
    # visible to underwriters (an internal review) or agents-and-underwriters (not the client) is still offered, flagged
    assert candidates["internal_underwriter_review"].client_visible is False
    assert candidates["insurance_declarations"].client_visible is False
    # not funding evidence, not collectable, not visible to anyone, or not published
    for key in ("business_phone", "reminder_cadence", "working_state", "nobody_sees_this", "client_email", "draft_only_item"):
        assert key not in candidates, key


@pytest.mark.asyncio
async def test_baseline_documents_no_playbook_carries_are_offered_so_the_forms_can_be_collected(db):
    await _seed(db)
    candidates = {item.key: item for item in await lib.library_candidates(db)}
    pnl = candidates["ytd_p_and_l_balance_sheet"]
    assert (pnl.source, pnl.requirement_id, pnl.kind) == ("baseline", None, "document")
    assert pnl.form_kinds == ("p_and_l", "balance_sheet")
    assert candidates["owner_personal_financial_statement"].form_kinds == ("pfs",)
    assert candidates["business_debt_schedule"].form_kinds == ("debt_schedule",)
    assert candidates["business_debt_schedule"].source == "library"
    assert candidates["ytd_profit_and_loss"].form_kinds == ("p_and_l",)


@pytest.mark.asyncio
async def test_the_library_is_grouped_by_category_then_label(db):
    await _seed(db)
    items = await lib.library_candidates(db)
    order = {category: index for index, category in enumerate(lib.CATEGORY_ORDER)}
    keys = [(order.get(item.category, 99), item.label.casefold(), item.key) for item in items]
    assert keys == sorted(keys)
    assert items[0].category == "financials"
    assert len({item.key for item in items}) == len(items)
    assert not {item.category for item in items} & lib.EXCLUDED_CATEGORIES


def test_a_funding_playbook_outranks_the_platform_and_a_newer_version_outranks_an_older_one():
    def book(**kw):
        base = dict(owner_type="platform", playbook_type="loan_product", version=1, published_at=None, created_at=None, id=uuid.uuid4())
        return SimpleNamespace(**{**base, **kw})

    assert lib.rank_playbook(book(owner_type="funding")) > lib.rank_playbook(book(version=9))
    assert lib.rank_playbook(book(version=3)) > lib.rank_playbook(book(version=2))
    assert lib.rank_playbook(book(playbook_type="evidence_policy")) > lib.rank_playbook(book())


def test_custom_keys_are_slugs_and_never_collide():
    assert lib.slugify("Voided check (business)!") == "voided_check_business"
    assert lib.slugify("   ") == "requirement"
    assert len(lib.slugify("x" * 200)) <= 56
    assert lib.next_free_key("custom_voided_check", set()) == "custom_voided_check"
    assert lib.next_free_key("custom_voided_check", {"custom_voided_check", "custom_voided_check_2"}) == "custom_voided_check_3"


def test_malformed_entries_on_a_profile_are_ignored_not_fatal():
    profile = SimpleNamespace(extra_requirements=[{"key": "a"}, "junk", {"nokey": 1}, {"key": "a"}, {"key": " b "}, None])
    assert [entry["key"] for entry in lib.clean_entries(profile.extra_requirements)] == ["a", "b"]
    assert lib.find_entry(profile, "a") == {"key": "a"}
    assert lib.find_entry(SimpleNamespace(extra_requirements=None), "a") is None
    assert lib.without_entry(profile, "a") == [entry for entry in lib.clean_entries(profile.extra_requirements) if entry["key"] != "a"]


def test_the_engine_works_on_a_detached_copy_with_the_files_own_level_and_leaves_the_library_row_alone():
    row = AICollectionRequirement(
        id=uuid.uuid4(), playbook_id=uuid.uuid4(), requirement_key="k", label="L", category="financials",
        required_level="required", visibility=["borrower"], review_checks=[{"key": "c"}],
    )
    copy = lib.transient_copy(row, required_level="optional")
    assert copy.required_level == "optional" and row.required_level == "required"
    assert (copy.id, copy.playbook_id, copy.label, copy.visibility) == (row.id, row.playbook_id, "L", ["borrower"])
    assert sa_inspect(copy).transient and sa_inspect(copy).session is None
    assert lib.transient_copy(row).required_level == "required"
    assert lib.transient_copy(row, required_level="nonsense").required_level == "required"


def test_one_library_item_or_a_custom_one_never_both_and_never_neither():
    assert _add("business_license").requirement_key == "business_license"
    custom = {"label": "Voided check", "category": "financials"}
    assert RequirementAddWrite(custom=custom).custom.label == "Voided check"
    with pytest.raises(ValidationError):
        RequirementAddWrite()
    with pytest.raises(ValidationError):
        RequirementAddWrite(requirement_key="x", custom=custom)
    with pytest.raises(ValidationError):
        RequirementAddWrite(custom={"label": "x", "category": "financials"})
    with pytest.raises(ValidationError):
        RequirementAddWrite(custom={"label": "Voided check", "category": "ai_internal"})
    with pytest.raises(ValidationError):
        RequirementAddWrite(requirement_key="x", required_level="critical")
    with pytest.raises(ValidationError):
        RequirementAddWrite(requirement_key="x", note="n" * 501)


# ---------------------------------------------------------------------------
# The engine: an added requirement is a policy-sourced requirement of the file.
# ---------------------------------------------------------------------------


def _by_key(readiness):
    return {item.requirement_key: item for item in readiness.requirements}


@pytest.mark.asyncio
async def test_adding_a_library_requirement_puts_it_on_the_checklist_like_the_initial_one(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    before = _by_key(await application_programs.get_program_readiness(db, seed.profile))
    assert set(before) == {"business_debt_schedule"}

    readiness = await route.add_application_requirement(
        seed.profile.id, _add("business_tax_returns_2_years", note="Needed for the refinance"), seed.desk, db
    )

    added = _by_key(readiness)["business_tax_returns_2_years"]
    assert added.source_policy_keys == ["staff_added"] and added.source_program_keys == []
    assert added.label == "Last 2 filed business tax returns"
    assert (added.required_level, added.status, added.client_visible, added.can_waive) == ("required", "missing", True, True)
    assert added.verification_required is True
    note = added.provenance["staff_added"]
    assert (note["added_by"], note["added_by_name"], note["note"], note["custom"]) == (str(seed.desk.id), "Franco", "Needed for the refinance", False)
    assert added.provenance["definition_version"] == "3"
    # the file's own baseline requirement is untouched
    assert _by_key(readiness)["business_debt_schedule"].source_policy_keys == ["business_baseline"]
    entries = seed.profile.extra_requirements
    assert [entry["key"] for entry in entries] == ["business_tax_returns_2_years"]
    assert entries[0]["added_by"] == str(seed.desk.id) and entries[0]["custom"] is False
    # an added requirement counts toward readiness: it is requestable like the initial checklist
    policy_keys = {item.requirement_key for item in readiness.requirements if item.source_policy_keys}
    assert "business_tax_returns_2_years" in policy_keys


@pytest.mark.asyncio
async def test_adding_the_same_requirement_twice_changes_nothing(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    first = await route.add_application_requirement(seed.profile.id, _add("business_license"), seed.desk, db)
    again = await route.add_application_requirement(seed.profile.id, _add("business_license", required_level="optional"), seed.desk, db)
    assert len(seed.profile.extra_requirements) == 1
    assert _by_key(first)["business_license"].required_level == _by_key(again)["business_license"].required_level == "required"
    states = (await db.execute(sa.select(ApplicationRequirementState).where(ApplicationRequirementState.requirement_key == "business_license"))).scalars().all()
    assert len(states) == 1


@pytest.mark.asyncio
async def test_a_requirement_the_file_already_has_through_its_policy_is_refused(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    with pytest.raises(HTTPException) as caught:
        await route.add_application_requirement(seed.profile.id, _add("business_debt_schedule"), seed.desk, db)
    assert caught.value.status_code == 409 and "already requires" in caught.value.detail
    assert seed.profile.extra_requirements == []


@pytest.mark.asyncio
async def test_the_baseline_or_program_definition_wins_if_a_key_is_added_and_later_required(db):
    """Added first, then the policy starts requiring the same key: one requirement, the policy's."""
    seed = await _seed(db)
    from app.routers import application_profiles as route

    await route.add_application_requirement(seed.profile.id, _add("business_license"), seed.desk, db)
    db.add(_requirement(seed.policy, "business_license", "Business license (policy wording)", category="compliance"))
    await db.flush()
    readiness = await application_programs.get_program_readiness(db, seed.profile)
    only = [item for item in readiness.requirements if item.requirement_key == "business_license"]
    assert len(only) == 1
    assert only[0].source_policy_keys == ["business_baseline"] and only[0].label == "Business license (policy wording)"


@pytest.mark.asyncio
async def test_the_file_can_make_an_added_requirement_optional_without_touching_the_library_row(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    readiness = await route.add_application_requirement(seed.profile.id, _add("business_license", required_level="optional"), seed.desk, db)
    assert _by_key(readiness)["business_license"].required_level == "optional"
    library_row = (await db.execute(sa.select(AICollectionRequirement).where(AICollectionRequirement.requirement_key == "business_license"))).scalars().one()
    assert library_row.required_level == "required"


@pytest.mark.asyncio
async def test_an_unknown_key_is_a_404_and_writes_nothing(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    with pytest.raises(HTTPException) as caught:
        await route.add_application_requirement(seed.profile.id, _add("not_in_the_library"), seed.desk, db)
    assert caught.value.status_code == 404
    assert seed.profile.extra_requirements == []
    assert (await db.execute(sa.select(AIPlaybookTemplate).where(AIPlaybookTemplate.product_key == lib.STAFF_PLAYBOOK_PRODUCT_KEY))).first() is None


@pytest.mark.asyncio
async def test_a_baseline_item_no_playbook_carries_is_written_to_the_staff_playbook_and_pinned(db):
    """The year-to-date P&L and balance sheet: one of the four forms, defined in code only."""
    from app.routers import application_profiles as route

    seed = await _seed(db)
    readiness = await route.add_application_requirement(seed.profile.id, _add("ytd_p_and_l_balance_sheet"), seed.desk, db)
    added = _by_key(readiness)["ytd_p_and_l_balance_sheet"]
    assert added.source_policy_keys == ["staff_added"] and added.label == "Year-to-date P&L and balance sheet"
    assert added.provenance["staff_added"]["form_kinds"] == ["p_and_l", "balance_sheet"]
    assert added.provenance["canonical_key"] == "ytd_p_and_l_balance_sheet"

    staff_book = (await db.execute(sa.select(AIPlaybookTemplate).where(AIPlaybookTemplate.product_key == lib.STAFF_PLAYBOOK_PRODUCT_KEY))).scalars().one()
    assert (staff_book.owner_type, staff_book.playbook_type, staff_book.status, staff_book.is_active) == ("platform", "evidence_policy", "published", True)
    rows = (await db.execute(sa.select(AICollectionRequirement).where(AICollectionRequirement.playbook_id == staff_book.id))).scalars().all()
    assert [row.requirement_key for row in rows] == ["ytd_p_and_l_balance_sheet"]
    assert seed.profile.extra_requirements[0]["source_requirement_id"] == str(rows[0].id)
    # from now on the library offers the persisted row, as a baseline item
    offered = {item.key: item for item in await lib.library_candidates(db)}
    assert (offered["ytd_p_and_l_balance_sheet"].source, offered["ytd_p_and_l_balance_sheet"].requirement_id) == ("baseline", rows[0].id)


@pytest.mark.asyncio
async def test_a_custom_requirement_joins_the_library_for_every_file(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    custom = {"label": "Voided business check", "category": "financials", "instructions": "A check marked VOID from the operating account."}
    readiness = await route.add_application_requirement(seed.profile.id, RequirementAddWrite(custom=custom, note="For ACH setup"), seed.desk, db)
    key = "custom_voided_business_check"
    added = _by_key(readiness)[key]
    assert (added.label, added.category, added.required_level, added.client_visible) == ("Voided business check", "financials", "required", True)
    assert added.provenance["staff_added"]["custom"] is True and added.provenance["staff_added"]["note"] == "For ACH setup"
    assert "A check marked VOID" in (added.provenance["request_message"] or "")

    offered = {item.key: item for item in await lib.library_candidates(db)}
    assert offered[key].source == "custom" and offered[key].label == "Voided business check"

    # a second file asking for the same words gets its own key, and the staff playbook is created once
    other = ApplicationProfile(vertical="main_street", funding_category="general")
    db.add(other)
    await db.flush()
    again = await route.add_application_requirement(other.id, RequirementAddWrite(custom=custom), seed.desk, db)
    assert key + "_2" in _by_key(again)
    books = (await db.execute(sa.select(AIPlaybookTemplate).where(AIPlaybookTemplate.product_key == lib.STAFF_PLAYBOOK_PRODUCT_KEY))).scalars().all()
    assert len(books) == 1


@pytest.mark.asyncio
async def test_removing_an_added_requirement_takes_it_off_the_checklist_and_only_that(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    await route.add_application_requirement(seed.profile.id, _add("business_license"), seed.desk, db)
    await route.add_application_requirement(seed.profile.id, _add("business_tax_returns_2_years"), seed.desk, db)
    readiness = await route.remove_added_application_requirement(seed.profile.id, "business_license", seed.desk, db)
    assert set(_by_key(readiness)) == {"business_debt_schedule", "business_tax_returns_2_years"}
    assert [entry["key"] for entry in seed.profile.extra_requirements] == ["business_tax_returns_2_years"]
    state = (await db.execute(sa.select(ApplicationRequirementState).where(ApplicationRequirementState.requirement_key == "business_license"))).scalars().one()
    assert state.status == "not_applicable" and state.provenance["inactive_definition"] is True
    assert state.provenance["staff_removal"]["removed_at"]
    assert [item.requirement_key for item in readiness.inactive_requirements] == ["business_license"]
    # and it can be added again
    again = await route.add_application_requirement(seed.profile.id, _add("business_license"), seed.desk, db)
    assert "business_license" in _by_key(again) and _by_key(again)["business_license"].status != "stale"


@pytest.mark.asyncio
async def test_only_what_the_desk_added_can_be_removed(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    for key in ("business_debt_schedule", "never_added"):
        with pytest.raises(HTTPException) as caught:
            await route.remove_added_application_requirement(seed.profile.id, key, seed.desk, db)
        assert caught.value.status_code == 404
    assert "business_debt_schedule" in _by_key(await application_programs.get_program_readiness(db, seed.profile))


@pytest.mark.asyncio
async def test_legacy_staff_refinance_alias_and_policy_schedule_materialize_once_after_each_refresh(db):
    from app.services import requirement_lifecycle

    seed = await _seed(db)
    await application_programs.get_program_readiness(db, seed.profile)
    staff = await lib.ensure_staff_playbook(db)
    definition = _requirement(staff, "refinance_debt_schedule", "Debt schedule if refinancing", level="recommended")
    db.add(definition)
    await db.flush()
    entry = lib.build_entry(key=definition.requirement_key, requirement_id=definition.id, required_level="recommended",
                            note="Needed for refinance", user_id=seed.desk.id, custom=False)
    seed.profile.extra_requirements = [entry]
    duplicate = ApplicationRequirementState(profile_id=seed.profile.id, requirement_key=definition.requirement_key,
        label=definition.label, category="financials", required_level="recommended", status="requested",
        source_program_keys=[], source_policy_keys=[lib.STAFF_POLICY_KEY],
        provenance={"staff_added": lib.provenance_note(entry), "collection_config": {}})
    db.add(duplicate)
    await db.flush()
    old_id = duplicate.id
    for _ in range(2):
        readiness = await application_programs.get_program_readiness(db, seed.profile)
        assert list(_by_key(readiness)) == ["business_debt_schedule"]
        canonical = readiness.requirements[0]
        assert canonical.required_level == "required" and canonical.derived_required and not canonical.can_remove
        assert lib.STAFF_POLICY_KEY in canonical.source_policy_keys
        assert "refinance_debt_schedule" in canonical.provenance["requirement_aliases"]
    assert duplicate.id == old_id and duplicate.provenance["redirect_requirement_key"] == "business_debt_schedule"
    with pytest.raises(HTTPException) as result:
        await requirement_lifecycle.change(db, seed.profile, "refinance_debt_schedule", action="remove", reason="Remove duplicate request", actor=seed.desk)
    assert result.value.status_code == 409
    assert list(_by_key(await application_programs.get_program_readiness(db, seed.profile))) == ["business_debt_schedule"]


@pytest.mark.asyncio
async def test_same_form_for_another_business_period_remains_a_separate_requirement(db):
    seed = await _seed(db)
    staff = await lib.ensure_staff_playbook(db)
    definition = _requirement(staff, "refinance_debt_schedule", "Other LLC prior-year debt schedule")
    definition.collection_config = {"subject": "Other LLC", "period": "2025", "quantity": 2, "unit": "files"}
    db.add(definition)
    await db.flush()
    seed.profile.extra_requirements = [lib.build_entry(key=definition.requirement_key, requirement_id=definition.id,
        required_level="required", note=None, user_id=seed.desk.id, custom=False)]
    readiness = await application_programs.get_program_readiness(db, seed.profile)
    assert len(readiness.requirements) == 2
    context = next(item for item in readiness.requirements if item.requirement_key != "business_debt_schedule")
    assert context.collection_config == definition.collection_config
    assert context.can_remove and not context.derived_required


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_waiver", [False, True])
async def test_new_policy_requirement_reactivates_prior_staff_removal_without_inventing_a_waiver(db, explicit_waiver):
    from app.routers import application_profiles as route
    from app.schemas.application_profile import ApplicationRequirementPatch

    seed = await _seed(db)
    await route.add_application_requirement(seed.profile.id, _add("business_license"), seed.desk, db)
    if explicit_waiver:
        await application_programs.patch_requirement(db, seed.profile, "business_license",
            ApplicationRequirementPatch(action="waive", reason="Existing authorized underwriting waiver", confirmed=True), seed.desk)
    await route.remove_added_application_requirement(seed.profile.id, "business_license", seed.desk, db)
    db.add(_requirement(seed.policy, "business_license", "Required business license"))
    await db.flush()
    for _ in range(2):
        readiness = await application_programs.get_program_readiness(db, seed.profile)
        current = _by_key(readiness)["business_license"]
        assert current.status == ("waived" if explicit_waiver else "missing")
        assert current.derived_required and not current.can_remove
        assert "staff_removal" not in current.provenance
        assert len(current.provenance["staff_removal_history"]) == 1
        assert current.provenance["staff_removal_history"][0]["reactivated_by_derived_requirement"]


@pytest.mark.asyncio
async def test_an_added_requirement_can_be_set_aside_like_any_checklist_item(db):
    from app.routers import application_profiles as route
    from app.schemas.application_profile import ApplicationRequirementPatch

    seed = await _seed(db)
    await route.add_application_requirement(seed.profile.id, _add("business_license"), seed.desk, db)
    await application_programs.patch_requirement(
        db, seed.profile, "business_license",
        ApplicationRequirementPatch(action="not_applicable", reason="Not licensed in this state", confirmed=True), seed.desk,
    )
    readiness = await application_programs.get_program_readiness(db, seed.profile)
    assert _by_key(readiness)["business_license"].status == "not_applicable"


@pytest.mark.asyncio
async def test_the_staff_playbook_is_never_mistaken_for_a_files_baseline_policy(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    await route.add_application_requirement(
        seed.profile.id, RequirementAddWrite(custom={"label": "Voided business check", "category": "financials"}), seed.desk, db
    )
    policies = await application_programs.ensure_evidence_policy(db, seed.profile)
    assert [policy.policy_key for policy in policies] == ["business_baseline"]
    assert all(policy.policy_name != lib.STAFF_PLAYBOOK_NAME for policy in policies)


@pytest.mark.asyncio
async def test_a_requirement_whose_library_row_has_gone_is_skipped_instead_of_breaking_the_file(db):
    seed = await _seed(db)
    seed.profile.extra_requirements = [{"key": "ghost", "source_requirement_id": str(uuid.uuid4()), "required_level": "required"}, {"key": "bad", "source_requirement_id": "not-a-uuid"}]
    await db.flush()
    readiness = await application_programs.get_program_readiness(db, seed.profile)
    assert set(_by_key(readiness)) == {"business_debt_schedule"}


@pytest.mark.asyncio
async def test_the_library_route_says_what_is_already_on_the_file_and_what_the_desk_added(db):
    from app.routers import application_profiles as route

    seed = await _seed(db)
    await route.add_application_requirement(seed.profile.id, _add("business_license"), seed.desk, db)
    body = await route.get_requirement_library(seed.profile.id, seed.desk, db)
    items = {item.key: item for item in body.items}
    assert (items["business_debt_schedule"].on_file, items["business_debt_schedule"].added) == (True, False)
    assert (items["business_license"].on_file, items["business_license"].added) == (True, True)
    assert (items["business_tax_returns_2_years"].on_file, items["business_tax_returns_2_years"].added) == (False, False)
    assert items["ytd_p_and_l_balance_sheet"].form_kind == "p_and_l"
    assert items["ytd_p_and_l_balance_sheet"].form_kinds == ["p_and_l", "balance_sheet"]
    assert items["business_license"].category_label == "Compliance"
    assert [category.key for category in body.categories][0] == "financials"
    assert body.profile_id == seed.profile.id


@pytest.mark.asyncio
async def test_removing_an_entry_after_the_state_was_waived_does_not_resurrect_it(db):
    from app.routers import application_profiles as route
    from app.schemas.application_profile import ApplicationRequirementPatch

    seed = await _seed(db)
    await route.add_application_requirement(seed.profile.id, _add("business_license"), seed.desk, db)
    await application_programs.patch_requirement(
        db, seed.profile, "business_license",
        ApplicationRequirementPatch(action="waive", reason="Not needed for this borrower", confirmed=True), seed.desk,
    )
    readiness = await route.remove_added_application_requirement(seed.profile.id, "business_license", seed.desk, db)
    assert "business_license" not in _by_key(readiness)


# ---------------------------------------------------------------------------
# Who may use it.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("role", [Role.BROKER, Role.CLIENT, Role.DEALER_PARTNER, Role.FIELD_REP])
async def test_only_the_underwriting_desk_can_read_or_change_a_files_requirements(role):
    from app.routers import application_profiles as route

    user = SimpleNamespace(id=uuid.uuid4(), role=role)
    profile_id = uuid.uuid4()
    for call in (
        route.get_requirement_library(profile_id, user, db=None),
        route.add_application_requirement(profile_id, _add("business_license"), user, db=None),
        route.remove_added_application_requirement(profile_id, "business_license", user, db=None),
    ):
        with pytest.raises(HTTPException) as caught:
            await call
        assert caught.value.status_code == 403
