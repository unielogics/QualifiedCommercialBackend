"""The buttons under client-facing AI messages, and the form links they open.

Fakes, not a database: the session is a small router that answers each
statement by the entity it selects, the way `test_missing_item_automation.py`
answers them by position, and hands out ids on flush so rows built without
Postgres still link to one another.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from fastapi import HTTPException

from app.models.application_profile import ApplicationRequirementState
from app.models.bucket import BucketAIChatAction
from app.models.client import Client
from app.models.financial_form_link import FinancialFormLink
from app.models.financial_statement import FinancialStatement
from app.models.financial_worksheet import FinancialFormLinkSheet, FinancialWorksheet
from app.models.public_underwriting_intake import PublicUnderwritingIntake
from app.schemas.bucket import IntakeChatActionRead, IntakeChatActionResult
from app.services import chat_form_links, intake_chat_actions, pfs_schema, worksheet_links
from app.services.intake_chat_actions import (
    ACTIONABLE_REQUIREMENTS,
    author_actions_for_message,
    execute_room_action,
)

PFS = "owner_personal_financial_statement"
DEBT = "business_debt_schedule"
PNL = "ytd_p_and_l_balance_sheet"
ALL_SHEETS = ("p_and_l", "balance_sheet", "debt_schedule", "pfs")
LABELS = {
    PFS: "Personal financial statement",
    DEBT: "Business debt schedule",
    PNL: "Year-to-date P&L and balance sheet",
}
#: The dealer gatekeeper (the only variant that used to get "Complete now"),
#: the funding-review and the MCA-refinance intakes, as the router names them.
VARIANTS = ("dealer_gatekeeper_v1", "real_estate_dscr_v1", "mca_refi_v1")
FOUR = ["upload_own", "complete_now", "download_template", "email_template"]


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, *, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar

    def scalar_one(self):
        if self._scalar is None:
            raise AssertionError("scalar_one on an empty result")
        return self._scalar


class FakeDB:
    """Answers each statement by the entity it selects; ids are handed out on flush."""

    def __init__(self) -> None:
        self.requirement = None
        self.available_action_id = None
        self.actions_by_key: dict[str, BucketAIChatAction] = {}
        self.links: dict[UUID, FinancialFormLink] = {}
        self.link_sheets: dict[UUID, list[str]] = {}
        self.statement = None
        self.worksheet = None
        self.objects: dict[tuple[type, object], object] = {}
        self.added: list = []
        self.flushes = 0

    def add(self, obj) -> None:
        self.added.append(obj)
        if isinstance(obj, BucketAIChatAction):
            self.actions_by_key[obj.idempotency_key] = obj

    async def flush(self) -> None:
        self.flushes += 1
        for obj in self.added:
            if getattr(obj, "id", "no-id") is None:
                obj.id = uuid4()
            # A minted link becomes visible to the next statement, the way a
            # flushed row is to the next query in the same transaction.
            if isinstance(obj, FinancialFormLink):
                self.links[obj.id] = obj
            elif isinstance(obj, FinancialFormLinkSheet):
                kinds = self.link_sheets.setdefault(obj.link_id, [])
                if obj.sheet_kind not in kinds:
                    kinds.append(obj.sheet_kind)

    async def get(self, model, key):
        if model is FinancialFormLink:
            return self.links.get(key)
        return self.objects.get((model, key))

    async def execute(self, stmt):
        entity = stmt.column_descriptions[0]["entity"]
        sql = str(stmt)
        params = stmt.compile().params
        if entity is ApplicationRequirementState:
            return _Result(scalar=self.requirement)
        if entity is BucketAIChatAction:
            # Route on the WHERE clause: a `select(BucketAIChatAction)` lists
            # every column in its SELECT list, idempotency_key included.
            where = sql.split("WHERE", 1)[-1]
            if "idempotency_key" in where:
                key = next(v for k, v in params.items() if k.startswith("idempotency_key"))
                return _Result(scalar=self.actions_by_key.get(key))
            return _Result(scalar=self.available_action_id)
        if entity is FinancialFormLink:
            # Apply the predicates `retire_chat_links` writes, so a link on
            # another file, of another kind, with the desk's label or with an
            # author is filtered by the query itself, as Postgres would.
            where = sql.split("WHERE", 1)[-1]
            rows = list(self.links.values())
            for column in ("profile_id", "kind", "label"):
                if f"financial_form_links.{column} = " in where:
                    wanted = next(v for k, v in params.items() if k.startswith(column))
                    rows = [row for row in rows if getattr(row, column) == wanted]
            if "financial_form_links.created_by IS NULL" in where:
                rows = [row for row in rows if row.created_by is None]
            if "financial_form_links.revoked_at IS NULL" in where:
                rows = [row for row in rows if row.revoked_at is None]
            return _Result(rows=rows)
        if entity is FinancialFormLinkSheet:
            link_id = next(v for k, v in params.items() if k.startswith("link_id"))
            return _Result(rows=list(self.link_sheets.get(link_id, [])))
        if entity is FinancialStatement:
            return _Result(scalar=self.statement)
        if entity is FinancialWorksheet:
            return _Result(scalar=self.worksheet)
        raise AssertionError(f"unexpected query: {sql}")

    def added_of(self, model) -> list:
        return [obj for obj in self.added if isinstance(obj, model)]


def _profile(**over):
    base = dict(
        id=uuid4(),
        intake_id=uuid4(),
        client_id=None,
        dealer_id=None,
        primary_bucket_id=uuid4(),
        program_selection_mode="auto",
    )
    return SimpleNamespace(**{**base, **over})


def _intake(variant: str = "dealer_gatekeeper_v1", language: str = "en", email="Client@Example.com"):
    return SimpleNamespace(variant=variant, preferred_language=language, email=email)


def _item(key: str, *, status: str = "missing", client_visible: bool = True,
          required_level: str = "required", source_program_keys=(), program_overrides=None):
    return SimpleNamespace(
        requirement_key=key,
        label=LABELS.get(key, key),
        status=status,
        client_visible=client_visible,
        required_level=required_level,
        source_program_keys=list(source_program_keys),
        program_overrides=dict(program_overrides or {}),
    )


def _row(key: str, *, status: str = "missing"):
    return SimpleNamespace(
        requirement_key=key,
        label=LABELS[key],
        status=status,
        requested_document_id=uuid4(),
        last_requested_at=None,
    )


def _message(**over):
    base = dict(
        id=uuid4(),
        role="assistant",
        audience="uploader",
        bucket_id=uuid4(),
        upload_link_id=uuid4(),
        content="Thanks, that helps.",
    )
    return SimpleNamespace(**{**base, **over})


def _readiness(*items):
    return SimpleNamespace(requirements=list(items))


def _link(
    kind: str = "pfs",
    *,
    profile_id: UUID | None = None,
    label: str | None = chat_form_links.CHAT_LINK_LABEL,
    created_by: UUID | None = None,
    revoked: bool = False,
    expired: bool = False,
) -> FinancialFormLink:
    """A link as the chat mints it unless told otherwise: chat label, no author."""
    return FinancialFormLink(
        id=uuid4(),
        profile_id=profile_id or uuid4(),
        kind=kind,
        token_hash=uuid4().hex,
        label=label,
        created_by=created_by,
        expires_at=datetime.now(UTC) + (timedelta(days=-1) if expired else timedelta(days=20)),
        revoked_at=datetime.now(UTC) if revoked else None,
    )


async def _author(db: FakeDB, *, profile, intake, readiness, message=None, upload_link=None):
    message = message or _message()
    if intake is not None:
        db.objects[(PublicUnderwritingIntake, profile.intake_id)] = intake
    with patch.object(
        intake_chat_actions, "profile_for_chat_scope", AsyncMock(return_value=profile)
    ), patch.object(
        intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)
    ):
        rows = await author_actions_for_message(db, message=message, upload_link=upload_link)
    return rows, message


def _action(action_type: str, requirement_key: str, *, status: str = "available", result=None):
    profile_id = uuid4()
    return BucketAIChatAction(
        id=uuid4(),
        bucket_id=uuid4(),
        profile_id=profile_id,
        source_message_id=uuid4(),
        upload_link_id=uuid4(),
        requirement_key=requirement_key,
        action_type=action_type,
        label="x",
        status=status,
        recipient_email="client@example.com",
        idempotency_key=f"chat:{uuid4().hex}",
        expires_at=datetime.now(UTC) + timedelta(days=7),
        executed_at=datetime.now(UTC) if status == "executed" else None,
        result=result,
    )


def _room_link():
    return SimpleNamespace(token="room-token", id=uuid4(), bucket_id=uuid4(), recipient_email=None)


def _execute_env(db: FakeDB, action: BucketAIChatAction, *, requirement_status="missing", readiness=None):
    profile = _profile(id=action.profile_id)
    db.objects[(intake_chat_actions.ApplicationProfile, profile.id)] = profile
    db.requirement = _row(action.requirement_key, status=requirement_status)
    readiness = readiness or _readiness(_item(action.requirement_key, status=requirement_status))
    return profile, readiness


def _mint_fakes(pin: str | None = "482913"):
    form = AsyncMock(
        side_effect=lambda db, profile, *, kind, recipient, retired_previous=0: {
            "form_url": f"https://app.example/forms/{kind}/new-token",
            "access_code": None,
            "link_id": str(uuid4()),
            "expires_at": "2026-11-23T00:00:00+00:00",
        }
    )
    worksheet = AsyncMock(
        side_effect=lambda db, profile, *, sheets, recipient, retired_previous=0: {
            "form_url": "https://app.example/forms/worksheet/new-token",
            "access_code": pin,
            "link_id": str(uuid4()),
            "expires_at": "2026-11-23T00:00:00+00:00",
        }
    )
    return form, worksheet


#: Exactly what a form action keeps on its row: enough for the desk to see
#: which link a click produced and that it was pinned, and nothing that opens it.
STORED_KEYS = {"detail", "room_url", "link_id", "pin_set", "expires_at"}


# ---------------------------------------------------------------------------
# offering: which buttons appear under a message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("key", sorted(ACTIONABLE_REQUIREMENTS))
async def test_complete_now_offered_on_every_variant_for_every_actionable_requirement(
    variant: str, key: str
) -> None:
    db = FakeDB()
    profile = _profile()
    db.requirement = _row(key)
    rows, _ = await _author(db, profile=profile, intake=_intake(variant), readiness=_readiness(_item(key)))

    assert [row.action_type for row in rows] == FOUR + ["open_worksheet"]
    assert all(row.requirement_key == key for row in rows)
    assert all(row.profile_id == profile.id for row in rows)


async def test_actions_offered_without_an_intake_row_at_all() -> None:
    # A room reached from a bucket with no public intake (no variant, no
    # language) still gets the full set, in English.
    db = FakeDB()
    profile = _profile(intake_id=None)
    db.requirement = _row(DEBT)
    rows, message = await _author(db, profile=profile, intake=None, readiness=_readiness(_item(DEBT)))

    assert [row.action_type for row in rows] == FOUR + ["open_worksheet"]
    assert [row.label for row in rows] == [
        "Upload my document", "Complete now", "Download template", "Email me", "Open the worksheet",
    ]
    assert "complete it now" in message.content


async def test_open_worksheet_offered_once_after_the_four_and_only_once_per_message() -> None:
    db = FakeDB()
    profile = _profile()
    db.requirement = _row(PFS)
    readiness = _readiness(_item(PFS), _item(DEBT))
    message = _message()
    rows, _ = await _author(db, profile=profile, intake=_intake(), readiness=readiness, message=message)

    assert [row.action_type for row in rows].count("open_worksheet") == 1
    assert rows[-1].action_type == "open_worksheet"
    assert rows[-1].label == "Open the worksheet"
    assert rows[-1].requirement_key == PFS

    # The same message authored again returns the same rows: no second button.
    again, _ = await _author(db, profile=profile, intake=_intake(), readiness=readiness, message=message)
    assert [row.id for row in again] == [row.id for row in rows]
    assert len(db.added_of(BucketAIChatAction)) == 5


async def test_one_requirement_at_a_time_while_buttons_are_still_available() -> None:
    db = FakeDB()
    profile = _profile()
    db.requirement = _row(PFS)
    db.available_action_id = uuid4()  # an earlier message's buttons are still live
    rows, message = await _author(db, profile=profile, intake=_intake(), readiness=_readiness(_item(PFS)))

    assert rows == []
    assert "will you upload" not in message.content


async def test_first_outstanding_actionable_requirement_is_the_subject() -> None:
    db = FakeDB()
    profile = _profile()
    db.requirement = _row(DEBT)
    readiness = _readiness(
        _item("business_bank_statements_6_months"),  # outstanding, not actionable
        _item(PFS, status="verified"),  # actionable, done
        _item(DEBT),  # first outstanding actionable
        _item(PNL),
    )
    rows, _ = await _author(db, profile=profile, intake=_intake(), readiness=readiness)

    assert {row.requirement_key for row in rows} == {DEBT}
    assert [row.action_type for row in rows] == FOUR + ["open_worksheet"]


@pytest.mark.parametrize(
    "items",
    [
        [_item(PFS, status="verified"), _item(DEBT, status="waived"), _item(PNL, status="not_applicable")],
        [_item(PFS, source_program_keys=["a", "b"], program_overrides={"a": "waived", "b": "verified"})],
        [_item(PFS, client_visible=False), _item(DEBT, required_level="recommended")],
        [_item("business_bank_statements_6_months")],
        [],
    ],
)
async def test_nothing_offered_including_the_worksheet_when_nothing_actionable_is_outstanding(items) -> None:
    db = FakeDB()
    profile = _profile()
    rows, message = await _author(db, profile=profile, intake=_intake(), readiness=_readiness(*items))

    assert rows == []
    assert db.added == []
    assert message.content == "Thanks, that helps."


async def test_spanish_labels_and_question_stay_in_step_with_english() -> None:
    db_es = FakeDB()
    profile = _profile()
    db_es.requirement = _row(PNL)
    rows_es, message_es = await _author(
        db_es, profile=profile, intake=_intake(language="es"), readiness=_readiness(_item(PNL))
    )
    db_en = FakeDB()
    db_en.requirement = _row(PNL)
    rows_en, message_en = await _author(
        db_en, profile=_profile(), intake=_intake(language="en"), readiness=_readiness(_item(PNL))
    )

    assert [row.action_type for row in rows_es] == [row.action_type for row in rows_en]
    assert [row.label for row in rows_es] == [
        "Subir mi documento", "Completar ahora", "Descargar plantilla", "Enviar por email",
        "Abrir la hoja de trabajo",
    ]
    assert message_es.content.endswith(
        f"Para {LABELS[PNL]}, puede subir su propio documento, completarlo ahora, "
        "descargar la plantilla o pedir que se la enviemos por email."
    )
    assert message_en.content.endswith(
        f"For {LABELS[PNL]}, will you upload your own document, complete it now, "
        "download the template, or have us email it to you?"
    )


async def test_rows_expire_in_seven_days_and_carry_the_normalised_recipient() -> None:
    db = FakeDB()
    profile = _profile(client_id=uuid4())
    db.requirement = _row(PFS)
    db.objects[(Client, profile.client_id)] = SimpleNamespace(email="ignored@example.com")
    before = datetime.now(UTC)
    rows, _ = await _author(db, profile=profile, intake=_intake(email="  Client@Example.COM "),
                            readiness=_readiness(_item(PFS)))

    for row in rows:
        assert timedelta(days=7) - timedelta(minutes=1) <= row.expires_at - before <= timedelta(days=7, minutes=1)
        assert row.recipient_email == "client@example.com"
        assert row.template_kind == "pfs_pdf"


# ---------------------------------------------------------------------------
# executing: what a click returns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("key", "expect_kind", "expect_sheets", "expect_pin", "legacy_param"),
    [
        (PFS, "pfs", None, None, "pfs"),
        (DEBT, "debt_schedule", None, None, "debt_schedule"),
        (PNL, None, ("p_and_l", "balance_sheet"), "482913", "worksheet"),
    ],
)
async def test_complete_now_targets_per_requirement(
    key, expect_kind, expect_sheets, expect_pin, legacy_param
) -> None:
    db = FakeDB()
    action = _action("complete_now", key)
    profile, readiness = _execute_env(db, action)
    form, worksheet = _mint_fakes()
    with patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)), \
            patch.object(chat_form_links, "mint_form_link", form), \
            patch.object(chat_form_links, "mint_worksheet_link", worksheet):
        result = await execute_room_action(db, action=action, link=_room_link())

    assert isinstance(result, IntakeChatActionResult)
    assert result.status == "executed"
    assert result.detail == "Secure form opened"
    if expect_kind:
        form.assert_awaited_once()
        assert form.await_args.kwargs == {
            "kind": expect_kind, "recipient": "client@example.com", "retired_previous": 0,
        }
        assert form.await_args.args[1] is profile
        worksheet.assert_not_awaited()
        assert result.form_url == f"https://app.example/forms/{expect_kind}/new-token"
    else:
        worksheet.assert_awaited_once()
        assert worksheet.await_args.kwargs == {
            "sheets": expect_sheets, "recipient": "client@example.com", "retired_previous": 0,
        }
        form.assert_not_awaited()
        assert result.form_url == "https://app.example/forms/worksheet/new-token"
    assert result.access_code == expect_pin
    # room_url still populated, with the shape old clients expect.
    assert result.room_url is not None
    assert result.room_url.startswith("https://") and "/buckets/request/room-token?" in result.room_url
    assert result.room_url.endswith(f"&form={legacy_param}")
    assert f"request={db.requirement.requested_document_id}" in result.room_url
    # The row records the link, never what opens it.
    assert action.status == "executed"
    assert action.executed_at is not None
    assert set(action.result) == STORED_KEYS
    assert action.result["pin_set"] is (expect_pin is not None)
    assert UUID(action.result["link_id"])
    assert action.result["expires_at"] == "2026-11-23T00:00:00+00:00"


async def test_open_worksheet_execute_opens_all_four_sheets_with_pin() -> None:
    db = FakeDB()
    action = _action("open_worksheet", PFS)
    _, readiness = _execute_env(db, action)
    form, worksheet = _mint_fakes(pin="135790")
    with patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)), \
            patch.object(chat_form_links, "mint_form_link", form), \
            patch.object(chat_form_links, "mint_worksheet_link", worksheet):
        result = await execute_room_action(db, action=action, link=_room_link())

    worksheet.assert_awaited_once()
    assert worksheet.await_args.kwargs == {
        "sheets": ALL_SHEETS, "recipient": "client@example.com", "retired_previous": 0,
    }
    form.assert_not_awaited()
    assert result.detail == "Worksheet opened"
    assert result.form_url == "https://app.example/forms/worksheet/new-token"
    assert result.access_code == "135790"
    assert result.room_url and result.room_url.endswith("&form=worksheet")
    assert set(action.result) == STORED_KEYS
    assert action.result["pin_set"] is True


async def test_open_worksheet_still_executes_while_another_actionable_item_is_outstanding() -> None:
    db = FakeDB()
    action = _action("open_worksheet", PFS)
    _, _ = _execute_env(db, action, requirement_status="verified")
    readiness = _readiness(_item(PFS, status="verified"), _item(DEBT))
    form, worksheet = _mint_fakes()
    with patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)), \
            patch.object(chat_form_links, "mint_worksheet_link", worksheet):
        result = await execute_room_action(db, action=action, link=_room_link())

    assert result.status == "executed"
    worksheet.assert_awaited_once()


async def test_open_worksheet_disabled_once_nothing_actionable_remains() -> None:
    db = FakeDB()
    action = _action("open_worksheet", PFS)
    _execute_env(db, action, requirement_status="verified")
    readiness = _readiness(_item(PFS, status="verified"), _item(DEBT, status="waived"),
                           _item("business_bank_statements_6_months"))
    form, worksheet = _mint_fakes()
    with patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)), \
            patch.object(chat_form_links, "mint_worksheet_link", worksheet), \
            pytest.raises(HTTPException) as caught:
        await execute_room_action(db, action=action, link=_room_link())

    assert caught.value.status_code == 409
    assert action.status == "disabled"
    worksheet.assert_not_awaited()


async def test_complete_now_disabled_when_its_requirement_is_already_satisfied() -> None:
    db = FakeDB()
    action = _action("complete_now", DEBT)
    _, readiness = _execute_env(db, action, requirement_status="verified")
    form, worksheet = _mint_fakes()
    with patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)), \
            patch.object(chat_form_links, "mint_form_link", form), \
            pytest.raises(HTTPException) as caught:
        await execute_room_action(db, action=action, link=_room_link())

    assert caught.value.status_code == 409
    assert action.status == "disabled"
    form.assert_not_awaited()


# ---------------------------------------------------------------------------
# nothing secret is stored: the row the room re-reads has no URL and no PIN
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action_type", "key", "pin"),
    [
        ("complete_now", PFS, None),
        ("complete_now", DEBT, None),
        ("complete_now", PNL, "482913"),
        ("open_worksheet", PFS, "135790"),
    ],
)
async def test_result_row_never_holds_the_form_url_pin_or_a_reuse_flag(action_type, key, pin) -> None:
    db = FakeDB()
    action = _action(action_type, key)
    _, readiness = _execute_env(db, action)
    form, worksheet = _mint_fakes(pin=pin)
    with patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)), \
            patch.object(chat_form_links, "mint_form_link", form), \
            patch.object(chat_form_links, "mint_worksheet_link", worksheet):
        result = await execute_room_action(db, action=action, link=_room_link())

    # The response carries the URL and the PIN, this once.
    assert result.form_url and result.form_url.endswith("/new-token")
    assert result.access_code == pin
    # The row carries neither: an exact key set, and no trace of either value.
    assert set(action.result) == STORED_KEYS
    assert action.result["pin_set"] is (pin is not None)
    assert UUID(action.result["link_id"])
    serialised = repr(action.result)
    forbidden = ["form_url", "access_code", "link_reused", "new-token"] + ([pin] if pin else [])
    for value in forbidden:
        assert value not in serialised, value
    # And what the room is served on every load is that row, unchanged.
    served = IntakeChatActionRead.model_validate(action)
    assert set(served.result) == STORED_KEYS
    assert "new-token" not in repr(served.result)


# ---------------------------------------------------------------------------
# re-execution: a second click mints again and retires the earlier chat link
# ---------------------------------------------------------------------------


def _real_mint(readiness, *, pins: list[str], audit: AsyncMock | None = None):
    """Patch everything around the real mint path: readiness, the audit writer
    and the PIN generator, which hands out `pins` in order."""
    return (
        patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)),
        patch.object(chat_form_links.profiles, "log_profile_action", audit or AsyncMock()),
        patch.object(worksheet_links, "set_pin", AsyncMock(side_effect=list(pins))),
    )


@pytest.mark.parametrize(
    ("key", "kind", "pins"),
    [(DEBT, "debt_schedule", []), (PNL, "worksheet", ["111111", "222222"])],
)
async def test_second_complete_now_click_mints_a_new_link_and_retires_the_first(key, kind, pins) -> None:
    db = FakeDB()
    action = _action("complete_now", key)
    _, readiness = _execute_env(db, action)
    audit = AsyncMock()
    readiness_patch, audit_patch, pin_patch = _real_mint(readiness, pins=pins, audit=audit)
    with readiness_patch, audit_patch, pin_patch as set_pin:
        first = await execute_room_action(db, action=action, link=_room_link())
        first_link = db.links[UUID(action.result["link_id"])]
        second = await execute_room_action(db, action=action, link=_room_link())
    second_link = db.links[UUID(action.result["link_id"])]

    assert first.status == second.status == "executed"
    assert f"/forms/{kind}/" in first.form_url and f"/forms/{kind}/" in second.form_url
    assert first.form_url != second.form_url
    assert first_link is not second_link
    assert first_link.revoked_at is not None and not first_link.is_open
    assert second_link.revoked_at is None and second_link.is_open
    assert second_link.label == chat_form_links.CHAT_LINK_LABEL and second_link.created_by is None
    assert action.result["link_id"] == str(second_link.id)
    assert action.executed_at is not None
    if pins:
        assert (first.access_code, second.access_code) == ("111111", "222222")
        assert set_pin.await_count == 2
        assert db.link_sheets[second_link.id] == ["p_and_l", "balance_sheet"]
    else:
        assert first.access_code is None and second.access_code is None
        set_pin.assert_not_awaited()
    # The audit counts what each mint retired and never names the credential.
    assert [call.kwargs["metadata"]["retired_previous"] for call in audit.await_args_list] == [0, 1]
    for call in audit.await_args_list:
        assert _token_of(first.form_url) not in repr(call)
        assert _token_of(second.form_url) not in repr(call)
        for pin in pins:
            assert pin not in repr(call)


async def test_second_open_worksheet_click_mints_a_new_link_and_pin_and_retires_the_first() -> None:
    db = FakeDB()
    action = _action("open_worksheet", PFS)
    _, readiness = _execute_env(db, action)
    readiness_patch, audit_patch, pin_patch = _real_mint(readiness, pins=["135790", "975310"])
    with readiness_patch, audit_patch, pin_patch:
        first = await execute_room_action(db, action=action, link=_room_link())
        first_id = UUID(action.result["link_id"])
        second = await execute_room_action(db, action=action, link=_room_link())
    second_id = UUID(action.result["link_id"])

    assert first_id != second_id
    assert db.links[first_id].revoked_at is not None
    assert db.links[second_id].is_open
    assert db.link_sheets[second_id] == list(ALL_SHEETS)
    assert (first.access_code, second.access_code) == ("135790", "975310")
    assert first.form_url != second.form_url
    assert set(action.result) == STORED_KEYS and action.result["pin_set"] is True
    assert "135790" not in repr(action.result) and "975310" not in repr(action.result)


async def test_open_worksheet_does_not_retire_the_two_sheet_complete_now_link() -> None:
    # Different sheet sets are different targets: the four-sheet worksheet
    # click leaves the borrower's two-sheet P&L link working.
    db = FakeDB()
    narrow = _action("complete_now", PNL)
    _, readiness = _execute_env(db, narrow)
    readiness_patch, audit_patch, pin_patch = _real_mint(readiness, pins=["111111", "222222"])
    with readiness_patch, audit_patch, pin_patch:
        await execute_room_action(db, action=narrow, link=_room_link())
        narrow_id = UUID(narrow.result["link_id"])
        wide = _action("open_worksheet", PNL)
        wide.profile_id = narrow.profile_id
        await execute_room_action(db, action=wide, link=_room_link())

    assert db.links[narrow_id].is_open
    assert db.links[UUID(wide.result["link_id"])].is_open
    assert len(db.added_of(FinancialFormLink)) == 2


async def test_retire_chat_links_touches_only_open_chat_links_on_the_same_file_and_kind() -> None:
    db = FakeDB()
    me, other, staff = uuid4(), uuid4(), uuid4()
    mine = _link("pfs", profile_id=me)
    wrong_profile = _link("pfs", profile_id=other)
    other_kind = _link("debt_schedule", profile_id=me)
    staff_minted = _link("pfs", profile_id=me, label="Sent to the accountant", created_by=staff)
    staff_with_chat_label = _link("pfs", profile_id=me, created_by=staff)
    unlabelled = _link("pfs", profile_id=me, label=None)
    already_revoked = _link("pfs", profile_id=me, revoked=True)
    already_expired = _link("pfs", profile_id=me, expired=True)
    rows = [mine, wrong_profile, other_kind, staff_minted, staff_with_chat_label,
            unlabelled, already_revoked, already_expired]
    db.links = {row.id: row for row in rows}
    revoked_before = already_revoked.revoked_at

    retired = await chat_form_links.retire_chat_links(db, profile_id=me, target=("pfs", ()))

    assert retired == 1
    assert mine.revoked_at is not None and not mine.is_open
    for untouched in (wrong_profile, other_kind, staff_minted, staff_with_chat_label,
                      unlabelled, already_expired):
        assert untouched.revoked_at is None, untouched.label
    assert already_revoked.revoked_at == revoked_before
    # Nothing left: a second pass retires nothing and says so.
    assert await chat_form_links.retire_chat_links(db, profile_id=me, target=("pfs", ())) == 0


async def test_retire_chat_links_matches_a_worksheet_by_its_own_sheet_rows() -> None:
    db = FakeDB()
    me = uuid4()
    two = _link("worksheet", profile_id=me)
    two_other_order = _link("worksheet", profile_id=me)
    four = _link("worksheet", profile_id=me)
    one = _link("worksheet", profile_id=me)
    staff_two = _link("worksheet", profile_id=me, label="Shared with the bookkeeper", created_by=uuid4())
    elsewhere_two = _link("worksheet", profile_id=uuid4())
    db.links = {row.id: row for row in (two, two_other_order, four, one, staff_two, elsewhere_two)}
    db.link_sheets = {
        two.id: ["p_and_l", "balance_sheet"],
        two_other_order.id: ["balance_sheet", "p_and_l"],
        four.id: list(ALL_SHEETS),
        one.id: ["p_and_l"],
        staff_two.id: ["p_and_l", "balance_sheet"],
        elsewhere_two.id: ["p_and_l", "balance_sheet"],
    }

    retired = await chat_form_links.retire_chat_links(
        db, profile_id=me, target=("worksheet", ("p_and_l", "balance_sheet"))
    )

    assert retired == 2
    assert two.revoked_at is not None and two_other_order.revoked_at is not None
    for untouched in (four, one, staff_two, elsewhere_two):
        assert untouched.revoked_at is None

    assert await chat_form_links.retire_chat_links(db, profile_id=me, target=("worksheet", ALL_SHEETS)) == 1
    assert four.revoked_at is not None
    for untouched in (one, staff_two, elsewhere_two):
        assert untouched.revoked_at is None


async def test_replay_still_answers_download_and_email_template_from_the_stored_result() -> None:
    db = FakeDB()
    download = _action(
        "download_template", PFS, status="executed",
        result={"detail": "Template ready to download", "download_url": "/api/v1/first"},
    )
    _, readiness = _execute_env(db, download)
    with patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)):
        result = await execute_room_action(
            db, action=download, link=_room_link(), download_url_override="/api/v1/second"
        )
    assert result.status == "executed"
    assert result.download_url == "/api/v1/first"
    assert download.result["download_url"] == "/api/v1/first"

    stored = {
        "ok": True,
        "detail": "Template email accepted by the provider",
        "delivery": {"id": str(uuid4()), "status": "sent"},
    }
    email = _action("email_template", PFS, status="executed", result=stored)
    _, readiness = _execute_env(db, email)
    sender = AsyncMock()
    with patch.object(intake_chat_actions, "get_program_readiness", AsyncMock(return_value=readiness)), \
            patch.object(intake_chat_actions, "send_as_user", sender):
        result = await execute_room_action(db, action=email, link=_room_link())
    sender.assert_not_awaited()
    assert result.detail == stored["detail"]
    assert result.delivery == stored["delivery"]
    assert result.form_url is None and result.access_code is None
    assert email.result == stored


def test_target_for_maps_each_action_to_its_form_or_sheet_set() -> None:
    assert chat_form_links.target_for("complete_now", PFS) == ("pfs", ())
    assert chat_form_links.target_for("complete_now", DEBT) == ("debt_schedule", ())
    assert chat_form_links.target_for("complete_now", PNL) == ("worksheet", ("p_and_l", "balance_sheet"))
    assert chat_form_links.target_for("open_worksheet", PFS) == ("worksheet", ALL_SHEETS)
    assert chat_form_links.target_for("open_worksheet", None) == ("worksheet", ALL_SHEETS)
    for other in ("upload_own", "download_template", "email_template"):
        assert chat_form_links.target_for(other, PFS) is None
    assert chat_form_links.target_for("complete_now", "business_bank_statements_6_months") is None


# ---------------------------------------------------------------------------
# minting: exactly what the desk's router does, attributed to the chat
# ---------------------------------------------------------------------------


def _token_of(url: str) -> str:
    return url.rsplit("/", 1)[1]


async def test_mint_form_link_pfs_creates_the_empty_statement_first() -> None:
    db = FakeDB()
    profile = _profile()
    audit = AsyncMock()
    with patch.object(chat_form_links.profiles, "log_profile_action", audit):
        out = await chat_form_links.mint_form_link(db, profile, kind="pfs", recipient="client@example.com")

    statements = db.added_of(FinancialStatement)
    assert len(statements) == 1
    assert statements[0].body == pfs_schema.empty_body()
    assert statements[0].profile_id == profile.id
    assert statements[0].created_by_user_id is None
    assert statements[0].status == "draft"

    (link,) = db.added_of(FinancialFormLink)
    assert link.kind == "pfs"
    assert link.statement_id == statements[0].id
    assert link.label == "From the secure room chat"
    assert link.invitee_email == "client@example.com"
    assert link.created_by is None
    assert link.profile_id == profile.id
    assert timedelta(days=29) < link.expires_at - datetime.now(UTC) <= timedelta(days=30)

    assert set(out) == {"form_url", "access_code", "link_id", "expires_at"}
    assert out["access_code"] is None
    assert out["link_id"] == str(link.id)
    assert out["expires_at"] == link.expires_at.isoformat()
    assert "/forms/pfs/" in out["form_url"] and out["form_url"].startswith("http")
    assert hashlib.sha256(_token_of(out["form_url"]).encode()).hexdigest() == link.token_hash

    audit.assert_awaited_once()
    args, kwargs = audit.await_args
    assert args[0] is db and args[1] is profile and args[2] is None
    assert args[3] == "financial_form.link_minted_by_chat"
    assert kwargs["target_type"] == "financial_form" and kwargs["target_id"] == link.id


async def test_mint_form_link_pfs_resumes_an_existing_statement() -> None:
    db = FakeDB()
    db.statement = SimpleNamespace(id=uuid4())
    with patch.object(chat_form_links.profiles, "log_profile_action", AsyncMock()):
        await chat_form_links.mint_form_link(db, _profile(), kind="pfs", recipient=None)

    assert db.added_of(FinancialStatement) == []
    (link,) = db.added_of(FinancialFormLink)
    assert link.statement_id == db.statement.id
    assert link.invitee_email is None


async def test_mint_form_link_debt_schedule_needs_no_statement() -> None:
    db = FakeDB()
    with patch.object(chat_form_links.profiles, "log_profile_action", AsyncMock()):
        out = await chat_form_links.mint_form_link(db, _profile(), kind="debt_schedule", recipient="c@x.io")

    assert db.added_of(FinancialStatement) == []
    (link,) = db.added_of(FinancialFormLink)
    assert link.kind == "debt_schedule" and link.statement_id is None
    assert "/forms/debt_schedule/" in out["form_url"]
    assert out["access_code"] is None


async def test_mint_form_link_rejects_unknown_kinds() -> None:
    with pytest.raises(ValueError):
        await chat_form_links.mint_form_link(FakeDB(), _profile(), kind="worksheet", recipient=None)
    with pytest.raises(ValueError):
        await chat_form_links.mint_form_link(FakeDB(), _profile(), kind="packet", recipient=None)


async def test_mint_worksheet_link_scopes_sheets_sets_edit_and_pins_it() -> None:
    db = FakeDB()
    profile = _profile()
    audit = AsyncMock()
    set_pin = AsyncMock(return_value="482913")
    with patch.object(chat_form_links.profiles, "log_profile_action", audit), \
            patch.object(worksheet_links, "set_pin", set_pin):
        out = await chat_form_links.mint_worksheet_link(
            db, profile, sheets=["balance_sheet", "p_and_l"], recipient="client@example.com"
        )

    (worksheet,) = db.added_of(FinancialWorksheet)
    assert worksheet.profile_id == profile.id and worksheet.created_by is None
    (link,) = db.added_of(FinancialFormLink)
    assert link.kind == "worksheet"
    assert link.worksheet_id == worksheet.id
    assert link.packet_id == worksheet.packet_id
    assert link.permission == "edit"
    assert link.label == "From the secure room chat"
    assert link.invitee_email == "client@example.com"
    assert link.created_by is None
    assert link.statement_id is None
    # Workbook order, whatever order was asked for.
    assert [row.sheet_kind for row in db.added_of(FinancialFormLinkSheet)] == ["p_and_l", "balance_sheet"]
    assert all(row.link_id == link.id for row in db.added_of(FinancialFormLinkSheet))

    set_pin.assert_awaited_once_with(link)
    assert out["access_code"] == "482913"
    assert out["link_id"] == str(link.id)
    assert out["expires_at"] == link.expires_at.isoformat()
    assert "/forms/worksheet/" in out["form_url"]
    token = _token_of(out["form_url"])
    assert "." not in token and len(token) >= 40
    assert hashlib.sha256(token.encode()).hexdigest() == link.token_hash

    args, kwargs = audit.await_args
    assert args[2] is None and args[3] == "financial_form.link_minted_by_chat"
    assert kwargs["target_type"] == "financial_worksheet" and kwargs["target_id"] == worksheet.id
    assert kwargs["metadata"]["sheets"] == ["p_and_l", "balance_sheet"]
    assert kwargs["metadata"]["permission"] == "edit"
    assert kwargs["metadata"]["pin_set"] is True
    assert "482913" not in repr(kwargs) and "482913" not in args[4]


async def test_mint_worksheet_link_reuses_the_files_worksheet_and_opens_all_four() -> None:
    db = FakeDB()
    db.worksheet = SimpleNamespace(id=uuid4(), packet_id=uuid4())
    with patch.object(chat_form_links.profiles, "log_profile_action", AsyncMock()), \
            patch.object(worksheet_links, "set_pin", AsyncMock(return_value="123987")):
        out = await chat_form_links.mint_worksheet_link(db, _profile(), sheets=ALL_SHEETS, recipient=None)

    assert db.added_of(FinancialWorksheet) == []
    (link,) = db.added_of(FinancialFormLink)
    assert link.worksheet_id == db.worksheet.id and link.packet_id == db.worksheet.packet_id
    assert [row.sheet_kind for row in db.added_of(FinancialFormLinkSheet)] == list(ALL_SHEETS)
    assert out["access_code"] == "123987"


async def test_pin_present_only_where_pin_required_for_says_so() -> None:
    # The real rule: every edit link carries a PIN, as does anything opening
    # the PFS, so both chat worksheet links are pinned and kind links never are.
    assert worksheet_links.pin_required_for("edit", ("p_and_l", "balance_sheet")) is True
    assert worksheet_links.pin_required_for("edit", ALL_SHEETS) is True
    assert worksheet_links.pin_required_for("view", ("p_and_l", "balance_sheet")) is False

    db = FakeDB()
    set_pin = AsyncMock(return_value="482913")
    with patch.object(chat_form_links.profiles, "log_profile_action", AsyncMock()), \
            patch.object(worksheet_links, "set_pin", set_pin), \
            patch.object(worksheet_links, "pin_required_for", return_value=False) as gate:
        out = await chat_form_links.mint_worksheet_link(db, _profile(), sheets=["p_and_l"], recipient=None)

    gate.assert_called_once_with("edit", ("p_and_l",))
    set_pin.assert_not_awaited()
    assert out["access_code"] is None

    db = FakeDB()
    set_pin = AsyncMock(return_value="482913")
    with patch.object(chat_form_links.profiles, "log_profile_action", AsyncMock()), \
            patch.object(worksheet_links, "set_pin", set_pin), \
            patch.object(worksheet_links, "pin_required_for", return_value=True):
        out = await chat_form_links.mint_worksheet_link(db, _profile(), sheets=["p_and_l"], recipient=None)
    set_pin.assert_awaited_once()
    assert out["access_code"] == "482913"


async def test_mint_worksheet_link_refuses_an_empty_or_unknown_scope() -> None:
    for sheets in ([], ["packet"], ["pfs_pdf"]):
        with pytest.raises(ValueError):
            await chat_form_links.mint_worksheet_link(FakeDB(), _profile(), sheets=sheets, recipient=None)


async def test_link_for_action_retires_then_mints_with_the_actions_recipient() -> None:
    db = FakeDB()
    action = _action("complete_now", PFS)
    action.recipient_email = "someone@example.com"
    stale = _link("pfs", profile_id=action.profile_id)
    db.links[stale.id] = stale
    form, worksheet = _mint_fakes()
    with patch.object(chat_form_links, "mint_form_link", form), \
            patch.object(chat_form_links, "mint_worksheet_link", worksheet):
        out = await chat_form_links.link_for_action(db, profile=_profile(id=action.profile_id), action=action)

    assert form.await_args.kwargs == {"kind": "pfs", "recipient": "someone@example.com", "retired_previous": 1}
    worksheet.assert_not_awaited()
    assert stale.revoked_at is not None
    assert set(out) == {"form_url", "access_code", "link_id", "expires_at"}
    with pytest.raises(ValueError):
        await chat_form_links.link_for_action(
            db, profile=_profile(), action=_action("upload_own", PFS)
        )


# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------


def test_result_schema_gains_form_url_and_access_code_and_read_is_unchanged() -> None:
    result = IntakeChatActionResult(action_id=uuid4(), status="executed", detail="ok")
    assert result.form_url is None and result.access_code is None
    assert set(IntakeChatActionResult.model_fields) == {
        "action_id", "status", "detail", "download_url", "room_url", "delivery",
        "form_url", "access_code",
    }
    assert set(IntakeChatActionRead.model_fields) == {
        "id", "source_message_id", "requested_document_id", "requirement_key", "action_type",
        "template_kind", "label", "status", "expires_at", "executed_at", "result",
    }


def test_model_constraint_admits_open_worksheet() -> None:
    constraint = next(
        item for item in BucketAIChatAction.__table_args__
        if getattr(item, "name", None) == "ck_bucket_ai_chat_action_type"
    )
    assert "open_worksheet" in str(constraint.sqltext)


@pytest.mark.parametrize("action_type", ["complete_now", "open_worksheet"])
async def test_an_expired_form_button_cannot_keep_minting(action_type: str) -> None:
    # Form buttons run again on every click, so the seven-day life is enforced
    # at execute time too: `load_action_for_room` only expires rows that are
    # still available, and an executed one would otherwise mint for as long as
    # the room exists.
    db = FakeDB()
    action = _action(action_type, PFS, status="executed", result={"detail": "Secure form opened"})
    action.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    _, readiness = _execute_env(db, action)
    readiness_patch, audit_patch, pin_patch = _real_mint(readiness, pins=["135790"])
    with readiness_patch, audit_patch, pin_patch, pytest.raises(HTTPException) as caught:
        await execute_room_action(db, action=action, link=_room_link())

    assert caught.value.status_code == 409
    assert action.status == "expired"
    assert db.added_of(FinancialFormLink) == []
