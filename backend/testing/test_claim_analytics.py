"""Focused tests for request-local claim analytics helpers."""

from app.claim_analytics import ClaimResponseTiming


def test_claim_response_timing_records_coarse_phases_and_size():
    ticks = iter([10.0, 10.125, 10.5, 10.75, 10.8])
    timing = ClaimResponseTiming(clock=lambda: next(ticks))

    timing.mark("claim_transition")
    timing.mark("project_history")
    response = {"task_id": "abc", "payloads": [{"type": "create"}]}

    metrics = timing.finish(response)

    assert metrics == {
        "total_ms": 800,
        "phase_ms": {
            "claim_transition": 125,
            "project_history": 375,
        },
        "size_measurement_ms": 50,
        "response_bytes_approx": len(
            b'{"task_id":"abc","payloads":[{"type":"create"}]}'
        ),
    }


def test_claim_response_timing_does_not_return_response_contents():
    ticks = iter([1.0, 1.1, 1.2])
    timing = ClaimResponseTiming(clock=lambda: next(ticks))

    metrics = timing.finish({"operator_secret": "do-not-log"})

    assert set(metrics) == {
        "total_ms",
        "phase_ms",
        "response_bytes_approx",
        "size_measurement_ms",
    }
