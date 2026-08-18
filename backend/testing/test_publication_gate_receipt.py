"""Tests for the bounded, durable local publication-gate receipt."""

import uuid

from app.models import Task, TaskStatus
from app.schemas import PublicationGateReceipt
from app.task_blocked import (
    PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH,
    PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES,
    PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH,
    record_publication_gate_receipt,
    sanitize_publication_gate_receipt,
)


def _legacy_receipt() -> dict:
    return {"status": "passed", "command": "./check test", "timeout_seconds": 600,
            "hard_timeout_seconds": 1800, "elapsed_seconds": 42,
            "deadline_extended": False, "exit_code": 0,
            "authoritative_ci_required": False, "recorded_at": "2026-08-18T00:00:00Z"}


def _attributed_receipt() -> dict:
    return {**_legacy_receipt(), "attribution_state": "preexisting_test_failure",
            "new_failures": {"new::failure": "AssertionError: new"},
            "suppressed_failures": {"old::failure": {
                "detail": "AssertionError: inherited",
                "reason": "same route/test/failure kind failed on baseline"}},
            "disappeared_failures": {}, "skipped_failures": {}}


def _task() -> Task:
    return Task(id=uuid.uuid4(), project_id=uuid.uuid4(),
                payloads=[{"type": "create", "prompt": "test receipt"}],
                status=TaskStatus.IN_PROGRESS, context={})


def test_older_nine_key_receipt_still_validates_unchanged():
    raw = _legacy_receipt()
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized == raw
    assert PublicationGateReceipt.model_validate(sanitized).model_dump(exclude_none=True) == raw


def test_attribution_fields_round_trip_through_sanitize_and_record():
    task = _task()
    raw = {**_attributed_receipt(), "unknown": "drop me"}
    assert record_publication_gate_receipt(task, raw) is True
    persisted = task.context["publication_gate_receipt"]
    assert persisted["attribution_state"] == "preexisting_test_failure"
    assert persisted["suppressed_failures"] == raw["suppressed_failures"]
    assert "unknown" not in persisted
    assert sanitize_publication_gate_receipt(persisted) == persisted


def test_over_cap_identity_count_and_string_lengths_are_deterministically_truncated():
    raw = {**_legacy_receipt(),
           "attribution_state": "s" * (PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH + 10),
           "new_failures": {
               f"identity-{index:03d}-{'x' * PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH}":
               "detail-" + "d" * (PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH + 10)
               for index in range(PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES + 2)}}
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized is not None
    assert sanitized["attribution_state"] == "s" * PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH
    identities = sanitized["new_failures"]
    expected = [identity[:PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH]
                for identity in sorted(raw["new_failures"])][:PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES]
    assert len(identities) == PUBLICATION_GATE_ATTRIBUTION_MAX_IDENTITIES
    assert list(identities) == expected
    assert all(len(identity) <= PUBLICATION_GATE_ATTRIBUTION_IDENTITY_MAX_LENGTH for identity in identities)
    assert all(len(detail) <= PUBLICATION_GATE_ATTRIBUTION_STRING_MAX_LENGTH for detail in identities.values())


def test_non_string_or_non_dict_attribution_members_are_rejected():
    for value in (None, 7, ["not an identity map"], {1: "non-string detail key"}):
        assert sanitize_publication_gate_receipt(
            {**_legacy_receipt(), "new_failures": {"test": value}}
        ) is None


def test_unknown_keys_are_dropped_from_receipt_and_identity_details():
    raw = {**_attributed_receipt(), "unknown_receipt_key": "drop me",
           "suppressed_failures": {"test": {"detail": "kept", "reason": "kept too",
                                              "unknown_detail_key": "drop me"}}}
    sanitized = sanitize_publication_gate_receipt(raw)
    assert sanitized is not None
    assert "unknown_receipt_key" not in sanitized
    assert sanitized["suppressed_failures"] == {"test": {"detail": "kept", "reason": "kept too"}}


def test_attribution_only_change_archives_prior_receipt():
    task = _task()
    first = _attributed_receipt()
    second = {**first, "suppressed_failures": {}, "new_failures": {"test": "new"}}
    assert record_publication_gate_receipt(task, first) is True
    assert record_publication_gate_receipt(task, second) is True
    assert task.context["publication_gate_receipt"] == second
    assert task.context["publication_gate_receipt_history"] == [first]
