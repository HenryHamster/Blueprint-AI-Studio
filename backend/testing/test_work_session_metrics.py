from app.work_session_metrics import (
    WORK_SESSION_METRIC_REGISTRY,
    build_work_session_metric,
)


EXPECTED_WORK_SESSION_METRIC_IDS = {
    "attention.needs_operator",
    "attention.watch",
    "messages.open",
    "messages.linked",
    "tasks.running",
    "tasks.starting",
    "tasks.evaluating",
    "tasks.blocked",
    "tasks.parked",
    "tasks.failed",
    "runtime.compute_active",
    "runtime.compute_failed",
    "runtime.held_approvals",
    "runtime.local_strategic_ready",
    "runtime.local_runner_holds",
    "runtime.parked_wakes",
    "runtime.wake_problems",
    "runtime.compute_spend",
    "context.stale",
    "context.items",
}


def test_work_session_metric_registry_covers_curated_metrics():
    assert set(WORK_SESSION_METRIC_REGISTRY) == EXPECTED_WORK_SESSION_METRIC_IDS
    assert WORK_SESSION_METRIC_REGISTRY["tasks.starting"].path == "tasks.counts.starting"
    assert (
        WORK_SESSION_METRIC_REGISTRY["tasks.evaluating"].path
        == "tasks.counts.evaluating"
    )

    for metric_id, spec in WORK_SESSION_METRIC_REGISTRY.items():
        metric = build_work_session_metric(metric_id, 0)
        definition = metric["definition"]

        assert metric["id"] == metric_id
        assert metric["label"] == spec.label
        assert metric["target"] == spec.target
        assert metric["provenance"]["surface"] == spec.source
        assert metric["provenance"]["path"] == spec.path
        assert definition["source"] == spec.source
        assert definition["path"] == spec.path
        assert definition["unit"] == spec.measurement_unit
        assert definition["target"] == spec.target
        assert definition["stale_behavior"]
        assert definition["window"]["type"]
        assert definition["denominator"]["kind"]


def test_work_session_metric_definition_keeps_dynamic_context():
    compute_spend = build_work_session_metric(
        "runtime.compute_spend",
        3.5,
        provenance_extras={"window_minutes": 360},
    )
    assert compute_spend["unit"] == "USD"
    assert compute_spend["provenance"]["window_minutes"] == 360
    assert compute_spend["definition"]["unit"] == "usd"
    assert compute_spend["definition"]["window"] == {
        "type": "rolling",
        "path": "runtime.work.compute_jobs.window_minutes",
        "minutes": 360,
    }

    local_ready = build_work_session_metric("runtime.local_strategic_ready", 1)
    assert local_ready["definition"]["denominator"] == {
        "kind": "path",
        "path": "runtime.local_runner.readiness_capable_runner_count",
        "label": "readiness-capable local runners",
    }

    stale_context = build_work_session_metric(
        "context.stale",
        1,
        provenance_extras={
            "status": "stale",
            "updated_at": "2026-07-01T10:00:00+00:00",
            "transform": "equals:stale",
        },
    )
    assert stale_context["provenance"]["status"] == "stale"
    assert stale_context["definition"]["transform"] == "equals:stale"
    assert stale_context["definition"]["stale_behavior"] == "source_status_field"


def test_work_session_metric_extras_cannot_override_registry_provenance():
    metric = build_work_session_metric(
        "messages.open",
        2,
        provenance_extras={
            "surface": "runtime",
            "path": "runtime.untrusted",
            "status": "ready",
        },
    )

    assert metric["provenance"] == {
        "surface": "messages",
        "path": "messages.counts.open",
        "status": "ready",
    }
    assert metric["definition"]["source"] == "messages"
    assert metric["definition"]["path"] == "messages.counts.open"
