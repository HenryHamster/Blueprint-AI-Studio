"""Tests for the bounded activation-phase retry contract."""

import pytest

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
