"""Unit tests for retry budget history helpers."""
from datetime import datetime, timedelta

from app.failure_classification import CODE_ERROR
from app.retry_budget import (
    FAILURE_CLASS_HISTORY_TTL,
    append_failure_class_history,
    failure_class_history_classes,
)


def test_failure_class_history_expires_timestamped_entries():
    now = datetime(2026, 4, 26, 12, 0, 0)
    raw = [
        {"class": CODE_ERROR, "at": (now - FAILURE_CLASS_HISTORY_TTL - timedelta(minutes=1)).isoformat()},
        {"class": "AGENT_TIMEOUT", "at": (now - timedelta(hours=1)).isoformat()},
    ]

    assert failure_class_history_classes(raw, now=now) == ["AGENT_TIMEOUT"]


def test_append_failure_class_history_converts_legacy_strings_to_timestamped_entries():
    now = datetime(2026, 4, 26, 12, 0, 0)
    history = append_failure_class_history([CODE_ERROR], "AGENT_TIMEOUT", now=now)

    assert [entry["class"] for entry in history] == [CODE_ERROR, "AGENT_TIMEOUT"]
    assert all("at" in entry for entry in history)
