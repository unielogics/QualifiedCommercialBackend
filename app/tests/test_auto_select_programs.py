"""Contract item 8: automatic routing pins up to AUTO_SELECT_LIMIT programs."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from app.schemas.application_profile import ProgramFitCandidate
from app.services import application_programs
from app.services.application_programs import (
    AUTO_SELECT_LIMIT,
    _auto_select,
    _automatic_candidate,
    _automatic_candidates,
)


def _candidate(
    key: str,
    *,
    eligible: bool,
    confidence: float = 0.5,
    playbook: bool = True,
    preference_score: int = 0,
) -> ProgramFitCandidate:
    return ProgramFitCandidate(
        program_key=key,
        program_name=key.replace("_", " ").title(),
        catalog_id=uuid4(),
        public_slug=key.replace("_", "-"),
        playbook_id=uuid4() if playbook else None,
        playbook_version=3 if playbook else None,
        eligible=eligible,
        recommendation_status="recommended" if eligible else "not_eligible",
        fit_score=round(confidence * 100, 2),
        confidence=confidence,
        priority=0,
        preference_score=preference_score,
        preference_reasons=[f"{key} preferred"] if preference_score else [],
        reasons=[f"{key} matched"],
    )


def _profile(mode: str = "auto") -> SimpleNamespace:
    return SimpleNamespace(id=uuid4(), vertical="dealer", program_selection_mode=mode)


def _run_auto_select(profile, candidates, *, active=None):
    db = SimpleNamespace(add=Mock(), flush=AsyncMock())
    log = AsyncMock()
    with patch.object(
        application_programs, "active_selections", AsyncMock(return_value=list(active or []))
    ), patch.object(application_programs.profiles, "log_profile_action", log):
        rows = asyncio.run(_auto_select(db, profile, candidates))
    return rows, db, log


def test_auto_select_limit_is_three() -> None:
    assert AUTO_SELECT_LIMIT == 3


def test_automatic_candidates_returns_up_to_the_limit_in_the_given_order() -> None:
    ranked = [_candidate(f"program_{index}", eligible=True) for index in range(5)]

    picked = _automatic_candidates(_profile(), ranked)

    assert [item.program_key for item in picked] == ["program_0", "program_1", "program_2"]
    assert len(picked) == AUTO_SELECT_LIMIT


def test_automatic_candidates_skips_ineligible_rows_and_keeps_rank_order() -> None:
    ranked = [
        _candidate("needs_info", eligible=False),
        _candidate("first_fit", eligible=True),
        _candidate("not_eligible", eligible=False),
        _candidate("second_fit", eligible=True),
    ]

    picked = _automatic_candidates(_profile(), ranked)

    assert [item.program_key for item in picked] == ["first_fit", "second_fit"]


def test_automatic_candidates_pins_each_program_key_once() -> None:
    first = _candidate("equipment_financing", eligible=True)
    duplicate = _candidate("equipment_financing", eligible=True)
    other = _candidate("ez_term", eligible=True)

    picked = _automatic_candidates(_profile(), [first, duplicate, other])

    assert picked == [first, other]


def test_automatic_candidates_is_empty_when_nothing_is_eligible() -> None:
    assert _automatic_candidates(_profile(), [_candidate("baseline", eligible=False)]) == []
    assert _automatic_candidates(_profile(), []) == []


def test_automatic_candidate_wrapper_returns_the_first_pick_or_none() -> None:
    fit = _candidate("equipment_financing", eligible=True)
    later = _candidate("ez_term", eligible=True)
    baseline = _candidate("business_baseline", eligible=False)

    assert _automatic_candidate(_profile(), [baseline, fit, later]) is fit
    assert _automatic_candidate(_profile(), [baseline]) is None


def test_auto_select_pins_three_candidates_each_with_the_same_audit() -> None:
    profile = _profile("auto")
    ranked = [
        _candidate("program_a", eligible=True, confidence=0.9, preference_score=20),
        _candidate("program_b", eligible=True, confidence=0.8),
        _candidate("program_c", eligible=True, confidence=0.7),
        _candidate("program_d", eligible=True, confidence=0.6),
        _candidate("program_e", eligible=False),
    ]

    rows, db, log = _run_auto_select(profile, ranked)

    assert [row.program_key for row in rows] == ["program_a", "program_b", "program_c"]
    assert db.add.call_count == 3
    assert [call.args[0] for call in db.add.call_args_list] == rows
    db.flush.assert_awaited_once()
    for row, candidate in zip(rows, ranked[:3], strict=True):
        assert row.profile_id == profile.id
        assert row.source == "ai_auto"
        assert row.playbook_id == candidate.playbook_id
        assert row.playbook_version == candidate.playbook_version
        assert row.program_name == candidate.program_name
        assert float(row.fit_score) == candidate.fit_score
        assert float(row.fit_confidence) == candidate.confidence
        assert row.fit_reasons == [*candidate.reasons, *candidate.preference_reasons]

    assert log.await_count == 3
    for call, candidate in zip(log.await_args_list, ranked[:3], strict=True):
        assert call.args[1] is profile
        assert call.args[2] is None
        assert call.args[3] == "programs.auto_selected"
        assert call.args[4] == (
            f"Pinned {candidate.program_name} version {candidate.playbook_version} "
            "from published criteria"
        )
        assert call.kwargs["target_type"] == "application_program"
        assert call.kwargs["metadata"] == {
            "program_key": candidate.program_key,
            "playbook_id": str(candidate.playbook_id),
            "playbook_version": candidate.playbook_version,
            "fit_confidence": candidate.confidence,
            "preference_score": candidate.preference_score,
            "preference_reasons": candidate.preference_reasons,
        }


def test_auto_select_pins_fewer_when_fewer_candidates_are_eligible() -> None:
    ranked = [
        _candidate("program_a", eligible=True),
        _candidate("program_b", eligible=False),
        _candidate("program_c", eligible=True),
        _candidate("program_d", eligible=False),
    ]

    rows, db, log = _run_auto_select(_profile("auto"), ranked)

    assert [row.program_key for row in rows] == ["program_a", "program_c"]
    assert db.add.call_count == 2
    assert log.await_count == 2


def test_auto_select_respects_manual_mode() -> None:
    manual_pick = SimpleNamespace(program_key="operator_choice")
    ranked = [_candidate(f"program_{index}", eligible=True) for index in range(3)]

    rows, db, log = _run_auto_select(_profile("manual"), ranked, active=[manual_pick])

    assert rows == [manual_pick]
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    log.assert_not_awaited()


def test_auto_select_in_manual_mode_pins_nothing_even_with_no_active_rows() -> None:
    ranked = [_candidate(f"program_{index}", eligible=True) for index in range(3)]

    rows, db, log = _run_auto_select(_profile("manual"), ranked, active=[])

    assert rows == []
    db.add.assert_not_called()
    log.assert_not_awaited()


def test_auto_select_keeps_existing_active_selections_in_auto_mode() -> None:
    existing = SimpleNamespace(program_key="already_pinned")
    ranked = [_candidate(f"program_{index}", eligible=True) for index in range(3)]

    rows, db, log = _run_auto_select(_profile("auto"), ranked, active=[existing])

    assert rows == [existing]
    db.add.assert_not_called()
    log.assert_not_awaited()


def test_auto_select_skips_an_eligible_candidate_without_a_published_playbook() -> None:
    ranked = [
        _candidate("program_a", eligible=True, playbook=False),
        _candidate("program_b", eligible=True),
    ]

    rows, db, log = _run_auto_select(_profile("auto"), ranked)

    assert [row.program_key for row in rows] == ["program_b"]
    assert db.add.call_count == 1
    assert log.await_count == 1


def test_auto_select_writes_nothing_when_nothing_is_eligible() -> None:
    rows, db, log = _run_auto_select(_profile("auto"), [_candidate("baseline", eligible=False)])

    assert rows == []
    db.add.assert_not_called()
    db.flush.assert_not_awaited()
    log.assert_not_awaited()
