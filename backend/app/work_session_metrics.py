"""Curated metric registry for the Strategic work session read model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


MetricTarget = dict[str, Any]


def _without_none(payload: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in payload.items() if value is not None}


def _provenance_extras(payload: dict[str, Any] | None) -> dict[str, Any]:
    if not payload:
        return {}
    return {
        key: value
        for key, value in payload.items()
        if key not in {"surface", "path"} and value is not None
    }


@dataclass(frozen=True)
class WorkSessionMetricSpec:
    id: str
    label: str
    source: str
    path: str
    measurement_unit: str
    target: MetricTarget
    stale_behavior: str
    display_unit: str | None = None
    window_type: str = "current"
    window_path: str | None = None
    denominator_kind: str = "none"
    denominator_path: str | None = None
    denominator_label: str | None = None
    transform: str | None = None

    def definition(
        self,
        *,
        target: MetricTarget | None = None,
        provenance_extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        extras = provenance_extras or {}
        window = _without_none(
            {
                "type": self.window_type,
                "path": self.window_path,
                "minutes": extras.get("window_minutes"),
            }
        )
        denominator = _without_none(
            {
                "kind": self.denominator_kind,
                "path": self.denominator_path,
                "label": self.denominator_label,
            }
        )
        definition = {
            "id": self.id,
            "source": self.source,
            "path": self.path,
            "unit": self.measurement_unit,
            "window": window,
            "denominator": denominator,
            "target": target or self.target,
            "stale_behavior": self.stale_behavior,
        }
        transform = extras.get("transform") or self.transform
        if transform:
            definition["transform"] = transform
        return definition


WORK_SESSION_METRIC_SPECS: tuple[WorkSessionMetricSpec, ...] = (
    WorkSessionMetricSpec(
        "attention.needs_operator",
        "needs operator",
        "attention",
        "attention.counts.needs_operator",
        "count",
        {"surface": "messages"},
        "clears_when_underlying_attention_source_clears",
    ),
    WorkSessionMetricSpec(
        "attention.watch",
        "watch",
        "attention",
        "attention.counts.watch",
        "count",
        {"surface": "messages"},
        "clears_when_underlying_attention_source_clears",
    ),
    WorkSessionMetricSpec(
        "messages.open",
        "open messages",
        "messages",
        "messages.counts.open",
        "count",
        {"surface": "messages"},
        "current_open_message_projection",
    ),
    WorkSessionMetricSpec(
        "messages.linked",
        "linked messages",
        "messages",
        "messages.counts.linked",
        "count",
        {"surface": "messages"},
        "current_linked_message_projection",
    ),
    WorkSessionMetricSpec(
        "tasks.running",
        "in-progress tasks",
        "tasks",
        "tasks.counts.running",
        "count",
        {"surface": "pipeline"},
        "current_non_archived_task_projection",
        denominator_kind="path",
        denominator_path="tasks.counts.total",
        denominator_label="non-archived tasks",
    ),
    WorkSessionMetricSpec(
        "tasks.starting",
        "starting tasks",
        "tasks",
        "tasks.counts.starting",
        "count",
        {"surface": "pipeline"},
        "current_non_archived_task_projection",
        denominator_kind="path",
        denominator_path="tasks.counts.total",
        denominator_label="non-archived tasks",
    ),
    WorkSessionMetricSpec(
        "tasks.evaluating",
        "evaluating tasks",
        "tasks",
        "tasks.counts.evaluating",
        "count",
        {"surface": "pipeline"},
        "current_non_archived_task_projection",
        denominator_kind="path",
        denominator_path="tasks.counts.total",
        denominator_label="non-archived tasks",
    ),
    WorkSessionMetricSpec(
        "tasks.blocked",
        "blocked tasks",
        "tasks",
        "tasks.counts.blocked",
        "count",
        {"surface": "pipeline"},
        "current_non_archived_task_projection",
        denominator_kind="path",
        denominator_path="tasks.counts.total",
        denominator_label="non-archived tasks",
    ),
    WorkSessionMetricSpec(
        "tasks.parked",
        "parked tasks",
        "tasks",
        "tasks.counts.parked",
        "count",
        {"surface": "pipeline"},
        "current_non_archived_task_projection",
        denominator_kind="path",
        denominator_path="tasks.counts.total",
        denominator_label="non-archived tasks",
    ),
    WorkSessionMetricSpec(
        "tasks.failed",
        "failed tasks",
        "tasks",
        "tasks.counts.failed",
        "count",
        {"surface": "pipeline"},
        "current_non_archived_task_projection",
        denominator_kind="path",
        denominator_path="tasks.counts.total",
        denominator_label="non-archived tasks",
    ),
    WorkSessionMetricSpec(
        "runtime.compute_active",
        "active compute",
        "runtime",
        "runtime.work.compute_jobs.counts.active",
        "count",
        {"surface": "runtime", "section": "compute"},
        "current_runtime_work_projection",
    ),
    WorkSessionMetricSpec(
        "runtime.compute_failed",
        "failed compute",
        "runtime",
        "runtime.work.compute_jobs.counts.failed_recent",
        "count",
        {"surface": "runtime", "section": "compute"},
        "rolling_recent_compute_window",
        window_type="rolling",
        window_path="runtime.work.compute_jobs.window_minutes",
    ),
    WorkSessionMetricSpec(
        "runtime.held_approvals",
        "held approvals",
        "runtime",
        "runtime.work.held_approvals.count",
        "count",
        {"surface": "pipeline"},
        "current_runtime_work_projection",
    ),
    WorkSessionMetricSpec(
        "runtime.local_strategic_ready",
        "local Strategic ready",
        "runtime",
        "runtime.local_runner.eligible_live_count",
        "count",
        {"surface": "runtime", "section": "devices"},
        "runner_liveness_window",
        denominator_kind="path",
        denominator_path="runtime.local_runner.readiness_capable_runner_count",
        denominator_label="readiness-capable local runners",
    ),
    WorkSessionMetricSpec(
        "runtime.local_runner_holds",
        "local runner holds",
        "runtime",
        "runtime.work.local_runner_holds.count",
        "count",
        {"surface": "runtime", "section": "devices"},
        "clears_when_local_required_task_dispatches_or_expires",
    ),
    WorkSessionMetricSpec(
        "runtime.parked_wakes",
        "parked wakes",
        "runtime",
        "runtime.work.parked_wakes.counts.active",
        "count",
        {"surface": "runtime", "section": "compute"},
        "current_runtime_work_projection",
    ),
    WorkSessionMetricSpec(
        "runtime.wake_problems",
        "wake problems",
        "runtime",
        "runtime.work.parked_wakes.counts.problem",
        "count",
        {"surface": "runtime", "section": "compute"},
        "current_runtime_work_projection",
    ),
    WorkSessionMetricSpec(
        "runtime.compute_spend",
        "compute spend",
        "runtime",
        "runtime.work.compute_jobs.spend.window_total_usd",
        "usd",
        {"surface": "runtime", "section": "compute"},
        "rolling_recent_compute_window",
        display_unit="USD",
        window_type="rolling",
        window_path="runtime.work.compute_jobs.window_minutes",
    ),
    WorkSessionMetricSpec(
        "context.stale",
        "stale context",
        "context",
        "context_package.status",
        "boolean",
        {"surface": "project", "section": "problem-model"},
        "source_status_field",
        transform="equals:stale",
    ),
    WorkSessionMetricSpec(
        "context.items",
        "context items",
        "context",
        "context_package.item_count",
        "count",
        {"surface": "project", "section": "problem-model"},
        "source_status_field",
    ),
)

WORK_SESSION_METRIC_REGISTRY: dict[str, WorkSessionMetricSpec] = {
    spec.id: spec for spec in WORK_SESSION_METRIC_SPECS
}


def build_work_session_metric(
    metric_id: str,
    value: int | float,
    *,
    tone: str = "neutral",
    target: MetricTarget | None = None,
    provenance_extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    spec = WORK_SESSION_METRIC_REGISTRY[metric_id]
    extras = _provenance_extras(provenance_extras)
    metric: dict[str, Any] = {
        "id": spec.id,
        "label": spec.label,
        "value": value,
        "tone": tone,
        "target": target or spec.target,
        "provenance": _without_none(
            {
                "surface": spec.source,
                "path": spec.path,
                **extras,
            }
        ),
        "definition": spec.definition(
            target=target or spec.target,
            provenance_extras=extras,
        ),
    }
    if spec.display_unit:
        metric["unit"] = spec.display_unit
    return metric
