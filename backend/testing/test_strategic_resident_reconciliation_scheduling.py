from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.models import StrategicResident
from app.strategic_resident_reconciliation_scheduling import (
    schedule_meaningful_follow_up,
)
from app.strategic_resident_reconciliation_validation import (
    ReconciliationResultInvalid,
)
from app.strategic_resident_turn_results import StrategicResidentTurnResult


_NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc).replace(tzinfo=None)
_TURN_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _resident(*, policy: str = "active") -> StrategicResident:
    return StrategicResident(
        project_id=uuid.uuid4(),
        background_policy=policy,
        scheduling_state={},
    )


def _body(*loops: dict) -> dict:
    return {"working_strategy": {"open_loops": list(loops)}}


def _loop(
    loop_id: str = "loop-1",
    *,
    owner: str | None = "resident",
    due_at: str | None = None,
) -> dict:
    return {
        "loop_id": loop_id,
        "summary": f"Continue {loop_id}",
        "owner": owner,
        "due_at": due_at,
        "evidence_ids": [],
    }


def _result(
    *,
    next_wakeup_at: str | None = None,
    follow_up_summary: str = "Continue",
    required_attention: bool = False,
) -> StrategicResidentTurnResult:
    return StrategicResidentTurnResult.model_validate(
        {
            "follow_up": (
                {
                    "summary": follow_up_summary,
                    "open_loops": [],
                    "next_wakeup_at": next_wakeup_at,
                }
                if next_wakeup_at is not None
                else None
            ),
            "required_attention": (
                [
                    {
                        "attention_id": "operator-input",
                        "severity": "warning",
                        "summary": "Operator input is required.",
                        "evidence_ids": [],
                        "receipt_ids": [],
                    }
                ]
                if required_attention
                else []
            ),
        }
    )


def _schedule(
    resident: StrategicResident,
    body: dict,
    *,
    now: datetime = _NOW,
    next_wakeup_at: str | None = None,
    follow_up_summary: str = "Continue",
    required_attention: bool = False,
) -> datetime | None:
    return schedule_meaningful_follow_up(
        resident,
        body=body,
        result=_result(
            next_wakeup_at=next_wakeup_at,
            follow_up_summary=follow_up_summary,
            required_attention=required_attention,
        ),
        turn_task_id=_TURN_ID,
        now=now,
    )


def test_effective_undated_open_loop_uses_15_30_60_and_capped_backoff():
    resident = _resident()
    body = _body(_loop())
    observed_at = _NOW

    for unchanged_count, delay_minutes in enumerate([15, 30, 60, 120, 240, 360, 360]):
        scheduled = _schedule(resident, body, now=observed_at)
        assert scheduled == observed_at + timedelta(minutes=delay_minutes)
        assert (
            resident.scheduling_state["continuation_unchanged_count"] == unchanged_count
        )
        assert (
            resident.scheduling_state["continuation_backoff_seconds"]
            == delay_minutes * 60
        )
        observed_at = scheduled

    assert resident.scheduling_state["deadline_source"] == "effective_open_loops"


def test_human_owned_attention_without_deadline_suppresses_synthetic_wake():
    resident = _resident()
    resident.scheduling_state = {
        "paused_next_wakeup_at": (_NOW + timedelta(minutes=5)).isoformat(),
        "background_activation_sequence": 2,
    }

    scheduled = _schedule(
        resident,
        _body(
            _loop("operator-loop", owner="operator"),
            _loop("named-actor-loop", owner="Henry"),
        ),
        required_attention=True,
    )

    assert scheduled is None
    assert resident.next_wakeup_at is None
    assert resident.scheduling_state == {"background_activation_sequence": 2}


@pytest.mark.parametrize(
    "loops",
    [
        (_loop(owner=None),),
        (_loop(owner="unknown"),),
        (_loop(owner="unassigned"),),
        (_loop(owner="resident"),),
        (_loop(owner="agent:worker"),),
        (_loop(owner="system"),),
        (_loop("human-loop", owner="operator"), _loop("resident-loop")),
    ],
)
def test_attention_keeps_runnable_or_mixed_undated_work_moving(loops: tuple[dict, ...]):
    resident = _resident()

    scheduled = _schedule(
        resident,
        _body(*loops),
        required_attention=True,
    )

    assert scheduled == _NOW + timedelta(minutes=15)
    assert resident.scheduling_state["deadline_reason"] == "open_loop_continuation"


def test_human_owned_attention_still_honors_real_deadline_and_explicit_wake():
    deadline_resident = _resident()
    deadline = _NOW + timedelta(hours=1)
    deadline_body = _body(_loop(owner="user", due_at=deadline.isoformat() + "Z"))
    assert (
        _schedule(
            deadline_resident,
            deadline_body,
            required_attention=True,
        )
        == deadline
    )
    assert (
        _schedule(
            deadline_resident,
            deadline_body,
            now=deadline,
            required_attention=True,
        )
        is None
    )

    overdue_resident = _resident()
    assert (
        _schedule(
            overdue_resident,
            _body(_loop(owner="Henry", due_at=(_NOW - timedelta(1)).isoformat())),
            required_attention=True,
        )
        is None
    )

    explicit_resident = _resident()
    explicit = _NOW + timedelta(hours=2)
    assert (
        _schedule(
            explicit_resident,
            _body(
                _loop(
                    owner="operator",
                    due_at=(_NOW - timedelta(minutes=1)).isoformat() + "Z",
                )
            ),
            next_wakeup_at=explicit.isoformat() + "Z",
            required_attention=True,
        )
        == explicit
    )


def test_unchanged_overdue_loop_runs_once_immediately_then_backs_off():
    resident = _resident()
    body = _body(_loop(due_at="2026-07-20T11:00:00Z"))

    first = _schedule(resident, body)
    assert first == _NOW
    assert resident.scheduling_state["deadline_reason"] == "overdue_commitment"

    second_now = _NOW + timedelta(minutes=1)
    second = _schedule(resident, body, now=second_now)
    assert second == second_now + timedelta(minutes=15)
    assert resident.scheduling_state["deadline_reason"] == "unchanged_overdue_backoff"


def test_repeated_explicit_overdue_continuation_without_loops_backs_off():
    resident = _resident()

    first = _schedule(
        resident,
        _body(),
        next_wakeup_at="2026-07-20T11:00:00Z",
    )
    assert first == _NOW

    second_now = _NOW + timedelta(minutes=1)
    second = _schedule(
        resident,
        _body(),
        now=second_now,
        next_wakeup_at=second_now.isoformat() + "Z",
    )
    assert second == second_now + timedelta(minutes=15)
    assert resident.scheduling_state["deadline_reason"] == "unchanged_overdue_backoff"


def test_repeated_rolling_one_minute_follow_up_respects_backoff_floor():
    resident = _resident()
    first = _schedule(
        resident,
        _body(),
        next_wakeup_at=(_NOW + timedelta(minutes=1)).isoformat() + "Z",
    )
    assert first == _NOW + timedelta(minutes=1)

    second = _schedule(
        resident,
        _body(),
        now=first,
        next_wakeup_at=(first + timedelta(minutes=1)).isoformat() + "Z",
    )
    assert second == first + timedelta(minutes=15)
    assert resident.scheduling_state["deadline_reason"] == "unchanged_follow_up_backoff"

    third = _schedule(
        resident,
        _body(),
        now=second,
        next_wakeup_at=(second + timedelta(minutes=1)).isoformat() + "Z",
    )
    assert third == second + timedelta(minutes=30)


def test_changed_explicit_only_intent_resets_rolling_backoff():
    resident = _resident()
    first = _schedule(
        resident,
        _body(),
        next_wakeup_at=(_NOW + timedelta(minutes=1)).isoformat() + "Z",
        follow_up_summary="Verify operation A",
    )

    second = _schedule(
        resident,
        _body(),
        now=first,
        next_wakeup_at=(first + timedelta(minutes=1)).isoformat() + "Z",
        follow_up_summary="Verify operation B",
    )

    assert second == first + timedelta(minutes=1)
    assert resident.scheduling_state["continuation_unchanged_count"] == 0
    assert resident.scheduling_state["continuation_backoff_seconds"] == 0


def test_changed_overdue_loop_resets_backoff_and_runs_immediately():
    resident = _resident()
    first_body = _body(_loop("loop-1", due_at="2026-07-20T11:00:00Z"))
    _schedule(resident, first_body)
    _schedule(resident, first_body, now=_NOW + timedelta(minutes=1))

    changed_now = _NOW + timedelta(minutes=2)
    changed = _schedule(
        resident,
        _body(_loop("loop-2", due_at="2026-07-20T11:00:00Z")),
        now=changed_now,
    )
    assert changed == changed_now
    assert resident.scheduling_state["continuation_unchanged_count"] == 0


def test_explicit_future_continuation_is_preserved_over_stale_overdue_loop():
    resident = _resident()
    explicit = _NOW + timedelta(hours=2)
    scheduled = _schedule(
        resident,
        _body(
            _loop("stale", due_at="2026-07-20T11:00:00Z"),
            _loop("future", due_at="2026-07-20T15:00:00Z"),
        ),
        next_wakeup_at=explicit.isoformat() + "Z",
    )
    assert scheduled == explicit
    assert resident.scheduling_state["deadline_reason"] == "follow_up"
    assert resident.scheduling_state["continuation_backoff_seconds"] == 0


def test_earlier_future_open_loop_deadline_precedes_explicit_follow_up():
    resident = _resident()
    loop_deadline = _NOW + timedelta(hours=1)
    explicit = _NOW + timedelta(hours=2)
    scheduled = _schedule(
        resident,
        _body(_loop(due_at=loop_deadline.isoformat() + "Z")),
        next_wakeup_at=explicit.isoformat() + "Z",
    )
    assert scheduled == loop_deadline
    assert resident.scheduling_state["deadline_source"] == "open_loop_deadline"


def test_no_effective_work_clears_stale_deadline_metadata():
    resident = _resident()
    resident.next_wakeup_at = _NOW
    resident.scheduling_state = {
        "deadline_at": _NOW.isoformat(),
        "deadline_reason": "overdue_commitment",
        "deadline_source": "open_loop_deadline",
        "deadline_source_turn_task_id": str(_TURN_ID),
        "deadline_scheduled_at": _NOW.isoformat(),
        "continuation_fingerprint": "old",
        "continuation_unchanged_count": 4,
        "continuation_backoff_seconds": 3600,
        "background_activation_sequence": 3,
    }

    assert _schedule(resident, _body()) is None
    assert resident.next_wakeup_at is None
    assert resident.scheduling_state == {"background_activation_sequence": 3}


@pytest.mark.parametrize(
    ("body", "next_wakeup_at"),
    [
        (_body(), "tomorrow-ish"),
        (_body(_loop(due_at="not-a-date")), None),
    ],
)
def test_malformed_timestamps_are_rejected(body: dict, next_wakeup_at: str | None):
    resident = _resident()
    with pytest.raises(ReconciliationResultInvalid, match="ISO-8601"):
        _schedule(resident, body, next_wakeup_at=next_wakeup_at)


def test_paused_resident_replaces_stale_preserved_wake_without_arming():
    resident = _resident(policy="paused")
    resident.scheduling_state = {
        "paused_next_wakeup_at": (_NOW + timedelta(minutes=5)).isoformat()
    }

    scheduled = _schedule(resident, _body(_loop()))

    assert scheduled is None
    assert resident.next_wakeup_at is None
    assert "paused_next_wakeup_at" not in resident.scheduling_state
    assert (
        resident.scheduling_state["deadline_at"]
        == (_NOW + timedelta(minutes=15)).isoformat()
    )
