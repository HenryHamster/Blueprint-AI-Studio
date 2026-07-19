"""Pure helpers for recovering numeric compute-result metrics from command logs."""

from __future__ import annotations

from typing import Any

from app.json_extract import (
    scan_all_balanced_json_objects,
    scan_balanced_json_objects,
)


def coerce_metric_number(value: Any) -> float | int | None:
    """Coerce a reported metric to a finite number, mirroring agent ``numOrNull``."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return (
            value
            if value == value and value not in (float("inf"), float("-inf"))
            else None
        )
    if isinstance(value, str) and value.strip():
        try:
            num = float(value)
        except ValueError:
            return None
        return num if num == num and num not in (float("inf"), float("-inf")) else None
    return None


# Only scan the tail of noisy command logs. Experiment result objects are printed last.
_STDOUT_METRICS_SCAN_MAX_CHARS = 100_000


def stdout_metrics_fallback(meta: dict) -> dict | None:
    """Recover the last numeric result object, plus repeated-metric series, from stdout."""
    command_log = meta.get("command_log")
    if not isinstance(command_log, dict):
        return None
    text = command_log.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    if len(text) > _STDOUT_METRICS_SCAN_MAX_CHARS:
        text = text[-_STDOUT_METRICS_SCAN_MAX_CHARS:]
    obj = scan_balanced_json_objects(text, last=True)
    if not isinstance(obj, dict):
        return None
    numeric = {k: v for k, v in obj.items() if coerce_metric_number(v) is not None}
    if not numeric:
        return None

    # Keep all distinct repeated values so multi-arm experiments remain comparable.
    series: dict[str, list[float | int]] = {}
    all_objs = scan_all_balanced_json_objects(text)
    for key in numeric:
        values = [
            num
            for candidate in all_objs
            if isinstance(candidate, dict)
            for num in (coerce_metric_number(candidate.get(key)),)
            if num is not None
        ]
        if len(values) >= 2 and len(set(values)) >= 2:
            series[f"{key}__series"] = values
    return {**numeric, **series}
