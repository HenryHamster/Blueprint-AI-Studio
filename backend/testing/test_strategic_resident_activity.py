from app.strategic_session_activity import (
    resident_activity_contract,
    resident_activity_lifecycle_contract,
    resident_activity_semantics,
    resident_activity_with_server_fences,
)


def test_consultation_activity_requires_a_bounded_correlated_reply_id():
    valid = resident_activity_contract(
        {
            "reason_code": "consultation_running",
            "consultation_id": "11111111-1111-4111-8111-111111111111",
            "autonomy_state": "active",
        },
        phase="working",
    )
    assert valid == {
        "reason_code": "consultation_running",
        "consultation_id": "11111111-1111-4111-8111-111111111111",
        "autonomy_state": "paused",
        "autonomy_reason_code": "consultation_running",
    }

    malformed = resident_activity_contract(
        {
            "reason_code": "consultation_running",
            "consultation_id": "not-a-server-reply-id",
            "autonomy_state": "paused",
            "autonomy_reason_code": "consultation_running",
        },
        phase="working",
    )
    assert malformed["reason_code"] is None
    assert malformed["consultation_id"] is None
    assert malformed["autonomy_state"] == "paused"


def test_activity_lifecycle_precedence_and_semantics_exclude_heartbeat_churn():
    ended = resident_activity_lifecycle_contract(
        {
            "reason_code": "consultation_running",
            "consultation_id": "11111111-1111-4111-8111-111111111111",
            "autonomy_state": "active",
        },
        phase="ended",
    )
    assert ended == {
        "reason_code": None,
        "consultation_id": None,
        "autonomy_state": "paused",
        "autonomy_reason_code": "session_ended",
    }
    first = resident_activity_semantics({"phase": "waiting", **ended, "updated_at": "one"})
    second = resident_activity_semantics({"phase": "waiting", **ended, "updated_at": "two"})
    assert first == second


def test_server_fences_pause_and_rearm_only_their_own_autonomy_reasons():
    base = {
        "reason_code": None,
        "consultation_id": None,
        "autonomy_state": "active",
        "autonomy_reason_code": None,
    }
    authority_paused = resident_activity_with_server_fences(
        base, authority_held=False, foreground_call_active=False
    )
    assert authority_paused["autonomy_reason_code"] == "authority_not_held"
    assert resident_activity_with_server_fences(
        authority_paused, authority_held=True, foreground_call_active=False
    ) == base

    call_paused = resident_activity_with_server_fences(
        base, authority_held=True, foreground_call_active=True
    )
    assert call_paused["autonomy_reason_code"] == "foreground_call"
    assert resident_activity_with_server_fences(
        call_paused, authority_held=True, foreground_call_active=False
    ) == base

    consultation = {**base, "autonomy_state": "paused", "autonomy_reason_code": "consultation_running"}
    assert resident_activity_with_server_fences(
        consultation, authority_held=True, foreground_call_active=False
    ) == consultation
