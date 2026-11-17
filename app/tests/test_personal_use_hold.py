"""Owner-occupied personal-use requests are held for a person to decide."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import business_purpose


def _intake(**details) -> SimpleNamespace:
    return SimpleNamespace(
        id="intake-1",
        bucket_id="bucket-1",
        business_name=None,
        full_name="Stephen Bronowski",
        intake_state={"funding_review_details": details},
    )


@pytest.fixture
def desk(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    notice = AsyncMock()
    monkeypatch.setattr(business_purpose, "_tell_desk", notice)
    return notice


async def test_the_borrowers_own_form_answer_holds_the_file(desk: AsyncMock) -> None:
    intake = _intake(occupancy="primary_residence", business_purpose=False)
    record = await business_purpose.after_answers(None, intake, source=business_purpose.SOURCE_FORM)
    assert record is not None and record["status"] == business_purpose.HELD
    assert business_purpose.is_held(intake) and business_purpose.is_open(intake)
    assert desk.await_count == 1
    assert "held" in desk.await_args.kwargs["title"].lower()
    context = business_purpose.client_context(intake)
    assert context and "business-purpose loans only" in context["instruction"]
    # Saying it again changes nothing and tells nobody twice.
    assert await business_purpose.after_answers(None, intake, source=business_purpose.SOURCE_FORM) is None
    assert desk.await_count == 1


async def test_an_ai_reading_only_flags_the_file_for_review(desk: AsyncMock) -> None:
    intake = _intake(occupancy="second_home")
    record = await business_purpose.after_answers(
        None, intake, source=business_purpose.SOURCE_CHAT, answers={"business_purpose": False}
    )
    assert record is not None and record["status"] == business_purpose.NEEDS_REVIEW
    assert business_purpose.is_open(intake) and not business_purpose.is_held(intake)
    assert "ask one direct question" in business_purpose.client_context(intake)["instruction"].lower()


async def test_a_later_business_answer_clears_an_ai_reading(desk: AsyncMock) -> None:
    intake = _intake(occupancy="primary_residence", business_purpose=False)
    await business_purpose.after_answers(None, intake, source=business_purpose.SOURCE_CHAT)
    intake.intake_state["funding_review_details"]["business_purpose"] = True
    record = await business_purpose.after_answers(
        None, intake, source=business_purpose.SOURCE_CHAT, answers={"business_purpose": True}
    )
    assert record is not None and record["status"] == business_purpose.CLEARED
    assert not business_purpose.is_open(intake)
    assert business_purpose.client_context(intake) is None
    assert [entry["status"] for entry in intake.intake_state[business_purpose.REVIEW_KEY]["history"]] == [
        business_purpose.NEEDS_REVIEW
    ]


async def test_a_chat_answer_never_clears_the_borrowers_own_form_answer(desk: AsyncMock) -> None:
    intake = _intake(occupancy="primary_residence", business_purpose=False)
    await business_purpose.after_answers(None, intake, source=business_purpose.SOURCE_FORM)
    intake.intake_state["funding_review_details"]["business_purpose"] = True
    record = await business_purpose.after_answers(
        None, intake, source=business_purpose.SOURCE_CHAT, answers={"business_purpose": True}
    )
    assert record is not None and record["status"] == business_purpose.NEEDS_REVIEW
    assert business_purpose.is_open(intake)


async def test_investment_property_or_business_purpose_is_never_held(desk: AsyncMock) -> None:
    for details in (
        {"occupancy": "investment", "business_purpose": False},
        {"occupancy": "primary_residence", "business_purpose": True},
        {"occupancy": "primary_residence"},
    ):
        intake = _intake(**details)
        assert await business_purpose.after_answers(None, intake, source=business_purpose.SOURCE_FORM) is None
        assert not business_purpose.is_open(intake)
    assert desk.await_count == 0


async def test_the_desk_decides_and_its_answer_becomes_the_files(monkeypatch: pytest.MonkeyPatch, desk: AsyncMock) -> None:
    from app.services import file_events

    emit = AsyncMock()
    monkeypatch.setattr(file_events, "emit", emit)
    intake = _intake(occupancy="primary_residence", business_purpose=False)
    await business_purpose.after_answers(None, intake, source=business_purpose.SOURCE_CHAT)
    actor = SimpleNamespace(id="user-1", name="Denise Desk", email="desk@example.com")

    record = await business_purpose.resolve(None, intake, decision="clear", actor=actor)
    assert record["status"] == business_purpose.CLEARED
    details = intake.intake_state["funding_review_details"]
    assert details["business_purpose"] is True
    assert details["sources"]["business_purpose"] == business_purpose.SOURCE_DESK
    assert business_purpose.desk_read(intake)["decided_by_name"] == "Denise Desk"

    record = await business_purpose.resolve(None, intake, decision="hold", actor=actor, note="Confirmed on the call")
    assert record["status"] == business_purpose.HELD
    assert record["note"] == "Confirmed on the call"
    assert intake.intake_state["funding_review_details"]["business_purpose"] is False
    assert emit.await_count == 2

    with pytest.raises(ValueError):
        await business_purpose.resolve(None, intake, decision="approve", actor=actor)
