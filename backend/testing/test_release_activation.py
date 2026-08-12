"""Tests for the bounded activation-phase retry contract."""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.operator_attention import _promoted_awaiting_activation_attention_item
from app.release_activation import ActivationPhaseFailure, retry_activation_phase


def test_activation_retries_failed_phase_then_reports_substantiated_failure():
    """A transient phase failure retries, while exhaustion remains explicit."""
    successful_calls = []
    successful_sleeps = []

    def succeeds_on_third_attempt():
        successful_calls.append(len(successful_calls) + 1)
        if len(successful_calls) < 3:
            raise RuntimeError("temporary root release failure")
        return "activated"

    assert retry_activation_phase(
        "activation",
        succeeds_on_third_attempt,
        max_attempts=3,
        backoff_seconds=(5.0, 10.0),
        retry_if=lambda _exc: True,
        sleep=successful_sleeps.append,
    ) == "activated"
    assert successful_calls == [1, 2, 3]
    assert successful_sleeps == [5.0, 10.0]

    exhausted_calls = []
    exhausted_sleeps = []

    def always_fails():
        exhausted_calls.append(len(exhausted_calls) + 1)
        raise RuntimeError("root release exit 17")

    with pytest.raises(ActivationPhaseFailure) as raised:
        retry_activation_phase(
            "activation",
            always_fails,
            max_attempts=3,
            backoff_seconds=(5.0, 10.0),
            retry_if=lambda _exc: True,
            sleep=exhausted_sleeps.append,
        )

    failure = raised.value
    assert exhausted_calls == [1, 2, 3]
    assert exhausted_sleeps == [5.0, 10.0]
    assert failure.phase == "activation"
    assert failure.attempts == 3
    assert str(failure.final_exception) == "root release exit 17"
    assert "activation failed after 3 attempts" in str(failure)


@pytest.mark.parametrize("approved", [False, True])
def test_activation_attention_uses_the_single_approval_path(approved):
    """Attention guidance never adds a hand-typed deploy step."""
    version = SimpleNamespace(
        id="version-1",
        version_number=7,
        deploy_confirm_deadline=datetime(
            2026, 1, 1, 11, 0, tzinfo=timezone.utc
        ),
        activation_requested_at=(
            datetime(2026, 1, 1, 10, 5, tzinfo=timezone.utc) if approved else None
        ),
        activation_requested_by="operator@example.com" if approved else None,
        promoted_at=datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc),
    )

    item = _promoted_awaiting_activation_attention_item("project-1", version)

    assert "blueprint deploy" not in item["detail"]
    if approved:
        assert "No further operator action is required." in item["detail"]
        assert item["activation_state"] == "approved_pending_execution"
    else:
        assert "approve activation" in item["detail"]
        assert item["activation_state"] == "awaiting_approval"
