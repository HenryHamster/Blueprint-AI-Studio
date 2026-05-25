"""Pure layer-observation helpers for the pipeline regression dispatcher."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any


_LAYER_SYMPTOM_BOUND = 10
_LAYER_SYMPTOM_WINDOW = timedelta(hours=48)


def _derive_observation_for_layer_classify(
    *,
    signal_name: str,
    regression_class: str,
    extras: list[str] | None,
    extra_snapshot_fields: dict | None,
) -> tuple[str, str, str]:
    extras = extras or []
    extra_snapshot_fields = extra_snapshot_fields or {}

    if regression_class.startswith("anomaly:"):
        anomaly_key = regression_class.split(":", 1)[1]
        first_token = anomaly_key.split("_", 1)[0]
        failure_class = first_token if first_token.isupper() else anomaly_key
    else:
        failure_class = signal_name.upper()

    stage = ""
    if regression_class.startswith("fix_loop_divergence:"):
        stage = regression_class.split(":", 1)[1]
    elif regression_class.startswith("fix_loop_divergence_fingerprint"):
        stage = "lint"
    elif regression_class.startswith("background_service_crash"):
        stage = "provision"
    elif regression_class.startswith("viewport_regression:"):
        stage = "viewport"
    elif signal_name in ("sr_catch_rate", "sr_blind_spot", "sr_false_positive"):
        stage = "review"
    elif signal_name == "pipeline_success_rate":
        stage = ""

    parts: list[str] = list(extras)
    sample_msg = extra_snapshot_fields.get("sample_message")
    if sample_msg:
        parts.append(str(sample_msg))
    latest = extra_snapshot_fields.get("latest_details")
    if latest:
        parts.append(str(latest))
    message = "\n".join(p for p in parts if p)
    return failure_class, message, stage


def _record_layer_observation(
    symptoms_by_layer: dict[str, Any] | None,
    layers: list[str],
    *,
    fingerprint: str,
    sample: str,
    now: datetime,
) -> dict[str, Any]:
    cutoff = now - _LAYER_SYMPTOM_WINDOW
    base: dict[str, Any] = dict(symptoms_by_layer or {})
    sample_short = (sample or "")[:500]
    observed_at_iso = now.isoformat()

    for layer in layers:
        existing = list(base.get(layer) or [])
        trimmed: list[dict[str, Any]] = []
        for entry in existing:
            if not isinstance(entry, dict):
                continue
            obs_raw = entry.get("observed_at")
            keep = True
            if isinstance(obs_raw, str) and obs_raw:
                try:
                    obs_dt = datetime.fromisoformat(obs_raw.replace("Z", "+00:00"))
                    if obs_dt.tzinfo is not None:
                        obs_dt = obs_dt.replace(tzinfo=None)
                    if obs_dt < cutoff:
                        keep = False
                except ValueError:
                    pass
            if keep:
                trimmed.append(entry)

        merged: list[dict[str, Any]] = []
        replaced = False
        for entry in trimmed:
            if entry.get("fingerprint") == fingerprint:
                replaced = True
                continue
            merged.append(entry)
        merged.append(
            {
                "fingerprint": fingerprint,
                "observed_at": observed_at_iso,
                "sample": sample_short,
            }
        )
        if not replaced and len(merged) > _LAYER_SYMPTOM_BOUND:
            merged = merged[-_LAYER_SYMPTOM_BOUND:]
        base[layer] = merged
    return base


def _distinct_recent_layers(
    symptoms_by_layer: dict[str, Any] | None,
    *,
    now: datetime,
    window: timedelta = _LAYER_SYMPTOM_WINDOW,
) -> list[str]:
    if not symptoms_by_layer:
        return []
    cutoff = now - window
    out: list[str] = []
    for layer, entries in symptoms_by_layer.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            obs_raw = entry.get("observed_at")
            try:
                obs_dt = datetime.fromisoformat(str(obs_raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if obs_dt.tzinfo is not None:
                obs_dt = obs_dt.replace(tzinfo=None)
            if obs_dt >= cutoff:
                out.append(layer)
                break
    return sorted(out)


def _representative_per_layer(
    symptoms_by_layer: dict[str, Any] | None,
    layers: list[str],
    *,
    now: datetime,
    window: timedelta = _LAYER_SYMPTOM_WINDOW,
) -> list[dict[str, Any]]:
    if not symptoms_by_layer:
        return []
    cutoff = now - window
    chosen: list[dict[str, Any]] = []
    for layer in layers:
        entries = symptoms_by_layer.get(layer) or []
        latest: tuple[datetime, dict[str, Any]] | None = None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                obs_dt = datetime.fromisoformat(
                    str(entry.get("observed_at")).replace("Z", "+00:00")
                )
            except (TypeError, ValueError):
                continue
            if obs_dt.tzinfo is not None:
                obs_dt = obs_dt.replace(tzinfo=None)
            if obs_dt < cutoff:
                continue
            if latest is None or obs_dt > latest[0]:
                latest = (obs_dt, entry)
        if latest is not None:
            chosen.append(
                {
                    "layer": layer,
                    "fingerprint": str(latest[1].get("fingerprint") or ""),
                    "sample": str(latest[1].get("sample") or "")[:500],
                    "observed_at": latest[1].get("observed_at"),
                }
            )
    return chosen
