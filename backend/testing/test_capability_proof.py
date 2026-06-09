"""Tests for capability_proof.py.

Covers:
  - should_record_capability_proof: returns True only when both a human capability
    is advertised AND verdict is proven.
  - record_capability_proof: stamps proof entries on project.capability_proof_records
    only when acceptance evidence is verified; skips when not proven.
  - record_capability_proof: proof entries contain expected fields.
  - record_capability_proof: entries are capped at 10 per capability.
  - record_capability_proof: deduplicates entries by task_id.
"""

import uuid
from unittest.mock import MagicMock, patch

from app.capability_proof import (
    _MAX_PROOF_ENTRIES_PER_CAPABILITY,
    record_capability_proof,
    should_record_capability_proof,
)
from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY


def _make_task(
    provides_capability=None,
    verdict=None,
    task_id=None,
    commit_sha=None,
):
    """Build a minimal Task-like object for capability proof tests."""
    task = MagicMock()
    task.id = task_id or str(uuid.uuid4())
    ctx = {}
    if provides_capability is not None:
        ctx["provides_capability"] = provides_capability
    if verdict is not None:
        ctx[CODE_ACCEPTANCE_VERDICT_KEY] = verdict
    if commit_sha:
        ctx["merge_commit_sha"] = commit_sha
    task.context = ctx
    return task


def _make_project():
    """Build a minimal Project-like object."""
    project = MagicMock()
    project.capability_proof_records = {}
    return project


def _named_test_verdict(test_path="testing/test_foo.py::test_bar", proven=True):
    return {
        "proven": proven,
        "satisfied": proven,
        "evidence": {"test_path": test_path},
    }


def _artifact_verdict(artifact_id="art-123", proven=True):
    return {
        "proven": proven,
        "satisfied": proven,
        "consumed": proven,
        "evidence": {"artifact_id": artifact_id},
    }


# ── should_record_capability_proof ────────────────────────────────────────────


class TestShouldRecordCapabilityProof:
    def test_proven_with_capability_returns_true(self):
        task = _make_task(
            provides_capability="language-aware-repo-map",
            verdict=_named_test_verdict(),
        )
        assert should_record_capability_proof(task, task.context[CODE_ACCEPTANCE_VERDICT_KEY])

    def test_not_proven_returns_false(self):
        task = _make_task(
            provides_capability="language-aware-repo-map",
            verdict=_named_test_verdict(proven=False),
        )
        assert not should_record_capability_proof(
            task, task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        )

    def test_no_capability_returns_false(self):
        task = _make_task(verdict=_named_test_verdict())
        assert not should_record_capability_proof(
            task, task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        )

    def test_no_verdict_returns_false(self):
        task = _make_task(provides_capability="language-aware-repo-map")
        assert not should_record_capability_proof(task, None)

    def test_list_capability_one_nonempty_returns_true(self):
        task = _make_task(
            provides_capability=["", "some-capability"],
            verdict=_named_test_verdict(),
        )
        assert should_record_capability_proof(
            task, task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        )

    def test_list_capability_all_blank_returns_false(self):
        task = _make_task(
            provides_capability=["", "  "],
            verdict=_named_test_verdict(),
        )
        assert not should_record_capability_proof(
            task, task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        )


# ── record_capability_proof ───────────────────────────────────────────────────


class TestRecordCapabilityProof:
    def test_proof_recorded_on_named_test_proven(self):
        task = _make_task(
            provides_capability="language-aware-repo-map",
            verdict=_named_test_verdict("testing/test_prompt.py::test_map"),
            commit_sha="abc123",
        )
        project = _make_project()

        with patch("app.capability_proof.flag_modified"):
            recorded = record_capability_proof(task, project)

        assert recorded == ["language-aware-repo-map"]
        entries = project.capability_proof_records["language-aware-repo-map"]
        assert len(entries) == 1
        e = entries[0]
        assert e["task_id"] == task.id
        assert e["proof_kind"] == "named_test"
        assert e["proof_ref"] == "testing/test_prompt.py::test_map"
        assert e["commit_sha"] == "abc123"
        assert "proved_at" in e

    def test_proof_recorded_on_downstream_artifact_proven(self):
        task = _make_task(
            provides_capability="compute-experiment-resource",
            verdict=_artifact_verdict("art-xyz"),
        )
        project = _make_project()

        with patch("app.capability_proof.flag_modified"):
            recorded = record_capability_proof(task, project)

        assert recorded == ["compute-experiment-resource"]
        e = project.capability_proof_records["compute-experiment-resource"][0]
        assert e["proof_kind"] == "downstream_artifact"
        assert e["proof_ref"] == "art-xyz"

    def test_proof_not_recorded_when_not_proven(self):
        task = _make_task(
            provides_capability="language-aware-repo-map",
            verdict=_named_test_verdict(proven=False),
        )
        project = _make_project()

        with patch("app.capability_proof.flag_modified"):
            recorded = record_capability_proof(task, project)

        assert recorded == []
        assert project.capability_proof_records == {}

    def test_proof_not_recorded_when_no_capability_marker(self):
        task = _make_task(verdict=_named_test_verdict())
        project = _make_project()

        with patch("app.capability_proof.flag_modified"):
            recorded = record_capability_proof(task, project)

        assert recorded == []
        assert project.capability_proof_records == {}

    def test_proof_capped_per_capability(self):
        cap = "language-aware-repo-map"
        existing_entries = [
            {
                "task_id": f"old-task-{i}",
                "proved_at": "2026-01-01T00:00:00",
                "proof_kind": "named_test",
                "proof_ref": "testing/test_foo.py",
                "commit_sha": "",
            }
            for i in range(_MAX_PROOF_ENTRIES_PER_CAPABILITY)
        ]
        project = _make_project()
        project.capability_proof_records = {cap: existing_entries[:]}

        task = _make_task(
            task_id="new-task-abc",
            provides_capability=cap,
            verdict=_named_test_verdict(),
        )

        with patch("app.capability_proof.flag_modified"):
            recorded = record_capability_proof(task, project)

        assert cap in recorded
        entries = project.capability_proof_records[cap]
        assert len(entries) == _MAX_PROOF_ENTRIES_PER_CAPABILITY
        # newest entry must be present (the cap keeps most recent).
        assert entries[-1]["task_id"] == "new-task-abc"

    def test_proof_deduplicated_by_task_id(self):
        cap = "language-aware-repo-map"
        task_id = "task-dup"
        project = _make_project()
        project.capability_proof_records = {
            cap: [
                {
                    "task_id": task_id,
                    "proved_at": "2026-01-01T00:00:00",
                    "proof_kind": "named_test",
                    "proof_ref": "testing/test_foo.py",
                    "commit_sha": "",
                }
            ]
        }
        task = _make_task(
            task_id=task_id,
            provides_capability=cap,
            verdict=_named_test_verdict(),
        )

        with patch("app.capability_proof.flag_modified"):
            recorded = record_capability_proof(task, project)

        # Same task_id should not create a second entry.
        assert recorded == []
        assert len(project.capability_proof_records[cap]) == 1

    def test_multiple_capabilities_all_recorded(self):
        project = _make_project()
        task = _make_task(verdict=_named_test_verdict())
        task.context["provides_capability"] = ["cap-alpha", "cap-beta"]

        with patch("app.capability_proof.flag_modified"):
            recorded = record_capability_proof(task, project)

        assert "cap-alpha" in recorded
        assert "cap-beta" in recorded
        assert len(project.capability_proof_records) == 2
