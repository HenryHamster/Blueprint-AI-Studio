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
    annotate_capability_proof_status,
    carry_proof_records_for_consolidation,
    enrich_problem_model_capability_proofs,
    record_capability_proof,
    should_record_capability_proof,
)
from app.acceptance_gate import CODE_ACCEPTANCE_VERDICT_KEY
from app.capability_trial_verdict import (
    evaluate_trial_verdict,
    get_trial_verdicts,
    record_trial_verdict,
)


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


# ── trial measurement → durable adoption verdict ──────────────────────────────


class TestTrialMeasurementProducesDurableAdoptionVerdict:
    """Acceptance test: the full measurement → verdict → persistence pipeline."""

    def test_trial_measurement_produces_durable_adoption_verdict(self):
        """A merge-test verdict: a digest measurement with enough samples and a
        positive lift evaluates to 'adopt' and is durably stored on the project."""
        project = MagicMock()
        project.capability_adoption_verdicts = {}

        # Simulate the measurement data that _compute_first_attempt_breakdowns returns.
        lift = 0.08
        n_with = 35
        n_without = 32

        # Evaluate the verdict from the measurement.
        verdict_entry = evaluate_trial_verdict(
            lift=lift,
            n_with=n_with,
            n_without=n_without,
            window_hours=72,
            evaluated_at="2026-06-09T12:00:00",
        )

        # Verify the verdict is correct before persisting.
        assert verdict_entry["verdict"] == "adopt"
        assert verdict_entry["lift"] == lift
        assert verdict_entry["n_with"] == n_with
        assert verdict_entry["n_without"] == n_without
        assert verdict_entry["window_hours"] == 72

        # Persist the verdict on the project.
        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", verdict_entry)

        # Verify the verdict is durably stored and retrievable.
        stored = get_trial_verdicts(project)
        assert "repo_map" in stored
        assert stored["repo_map"]["verdict"] == "adopt"
        assert stored["repo_map"]["lift"] == lift

    def test_insufficient_samples_produces_continue_trial(self):
        """Below the minimum sample threshold the verdict is always continue-trial."""
        project = MagicMock()
        project.capability_adoption_verdicts = {}

        verdict_entry = evaluate_trial_verdict(
            lift=0.15,  # high lift but not enough samples
            n_with=5,
            n_without=5,
            evaluated_at="2026-06-09T12:00:00",
        )
        assert verdict_entry["verdict"] == "continue-trial"

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", verdict_entry)

        stored = get_trial_verdicts(project)
        assert stored["repo_map"]["verdict"] == "continue-trial"

    def test_later_verdict_supersedes_earlier(self):
        """A newer measurement supersedes the prior durable verdict."""
        project = MagicMock()
        project.capability_adoption_verdicts = {
            "repo_map": {
                "verdict": "continue-trial",
                "lift": 0.01,
                "n_with": 10,
                "n_without": 10,
            }
        }

        # New measurement with enough data and a positive lift.
        new_entry = evaluate_trial_verdict(
            lift=0.09, n_with=40, n_without=38, evaluated_at="2026-06-09T18:00:00"
        )
        assert new_entry["verdict"] == "adopt"

        with patch("app.capability_trial_verdict.flag_modified"):
            record_trial_verdict(project, "repo_map", new_entry)

        stored = get_trial_verdicts(project)
        assert stored["repo_map"]["verdict"] == "adopt"
        assert stored["repo_map"]["n_with"] == 40


class TestCapabilityProofViews:
    def test_annotate_capability_proof_status_is_fail_open(self):
        vocab = [
            {"name": "cap-proven", "status": "working"},
            {"name": "cap-claimed", "status": "planned"},
        ]
        proof_records = {
            "cap-proven": [
                {
                    "task_id": "task-1",
                    "proved_at": "2026-06-09T00:00:00Z",
                    "proof_kind": "named_test",
                    "proof_ref": "backend/testing/test_capability_proof.py::test_proof_recorded_on_named_test_proven",
                    "commit_sha": "abc123",
                }
            ],
            "cap-claimed": [],
        }

        annotated = annotate_capability_proof_status(vocab, proof_records)

        assert annotated[0]["proof_status"] == "proven"
        assert annotated[1]["proof_status"] == "unproven"
        assert "proof_status" not in vocab[0]

    def test_enrich_problem_model_capability_proofs_adds_evidence(self):
        problem_model = {
            "revision": 7,
            "capabilities": [
                {"name": "cap-proven", "status": "working"},
                {"name": "cap-claimed", "status": "partial"},
            ],
        }
        proof_records = {
            "cap-proven": [
                {
                    "task_id": "task-1",
                    "proved_at": "2026-06-09T00:00:00Z",
                    "proof_kind": "downstream_artifact",
                    "proof_ref": "art-123",
                    "commit_sha": "def456",
                }
            ]
        }

        enriched = enrich_problem_model_capability_proofs(problem_model, proof_records)

        assert enriched is not problem_model
        assert enriched["capabilities"][0]["proof_status"] == "proven"
        assert enriched["capabilities"][0]["proof_records"][0]["proof_ref"] == "art-123"
        assert enriched["capabilities"][1]["proof_status"] == "unproven"
        assert "proof_records" not in enriched["capabilities"][1]


# ── carry_proof_records_for_consolidation ─────────────────────────────────────


def _make_proof_entry(task_id: str, proof_ref: str = "test_foo.py") -> dict:
    return {
        "task_id": task_id,
        "proved_at": "2026-06-01T00:00:00",
        "proof_kind": "named_test",
        "proof_ref": proof_ref,
        "commit_sha": "abc",
    }


class TestCarryProofRecordsForConsolidation:
    def test_consolidation_carries_proof_records_to_survivor(self):
        """Absorbed entries are merged onto the survivor and their keys removed."""
        proof_records = {
            "survivor-cap": [_make_proof_entry("task-s1")],
            "old-cap-a": [_make_proof_entry("task-a1"), _make_proof_entry("task-a2")],
            "old-cap-b": [_make_proof_entry("task-b1")],
        }
        consolidated = [
            {"survivor": "survivor-cap", "absorbed": ["old-cap-a", "old-cap-b"]}
        ]

        result = carry_proof_records_for_consolidation(consolidated, proof_records)

        # Absorbed keys removed.
        assert "old-cap-a" not in result
        assert "old-cap-b" not in result
        # Survivor now holds merged entries.
        assert "survivor-cap" in result
        task_ids = {e["task_id"] for e in result["survivor-cap"]}
        assert task_ids == {"task-s1", "task-a1", "task-a2", "task-b1"}

    def test_consolidation_deduplicates_by_task_id(self):
        """A task_id that proved both absorbed and survivor appears only once."""
        shared_id = "task-shared"
        proof_records = {
            "survivor-cap": [_make_proof_entry(shared_id, "survivor_test.py")],
            "old-cap": [_make_proof_entry(shared_id, "absorbed_test.py")],
        }
        consolidated = [{"survivor": "survivor-cap", "absorbed": ["old-cap"]}]

        result = carry_proof_records_for_consolidation(consolidated, proof_records)

        entries = result["survivor-cap"]
        assert len(entries) == 1
        # The survivor's existing entry is kept (not replaced by absorbed duplicate).
        assert entries[0]["proof_ref"] == "survivor_test.py"

    def test_consolidation_cap_at_max_entries(self):
        """Merged list is bounded at _MAX_PROOF_ENTRIES_PER_CAPABILITY."""
        survivor_entries = [_make_proof_entry(f"s-{i}") for i in range(7)]
        absorbed_entries = [_make_proof_entry(f"a-{i}") for i in range(6)]
        proof_records = {
            "survivor-cap": survivor_entries,
            "old-cap": absorbed_entries,
        }
        consolidated = [{"survivor": "survivor-cap", "absorbed": ["old-cap"]}]

        result = carry_proof_records_for_consolidation(consolidated, proof_records)

        assert len(result["survivor-cap"]) == _MAX_PROOF_ENTRIES_PER_CAPABILITY

    def test_no_consolidation_leaves_records_unchanged(self):
        """An empty consolidation list must not touch the proof records."""
        proof_records = {
            "cap-a": [_make_proof_entry("task-1")],
            "cap-b": [_make_proof_entry("task-2")],
        }
        result = carry_proof_records_for_consolidation([], proof_records)
        assert result is proof_records  # same object — no copy needed

    def test_annotate_reports_survivor_proven_after_carry_through(self):
        """After carry-through, annotate_capability_proof_status sees survivor proven."""
        proof_records = {
            "old-cap": [_make_proof_entry("task-proof-1")],
        }
        consolidated = [{"survivor": "new-cap", "absorbed": ["old-cap"]}]
        merged = carry_proof_records_for_consolidation(consolidated, proof_records)

        vocab = [{"name": "new-cap", "status": "working"}]
        annotated = annotate_capability_proof_status(vocab, merged)
        assert annotated[0]["proof_status"] == "proven"

    def test_survivor_without_prior_entries_gets_absorbed_entries(self):
        """Survivor key is created when it had no prior proof records."""
        proof_records = {
            "old-cap": [_make_proof_entry("task-x")],
        }
        consolidated = [{"survivor": "new-survivor", "absorbed": ["old-cap"]}]

        result = carry_proof_records_for_consolidation(consolidated, proof_records)

        assert "old-cap" not in result
        assert result["new-survivor"][0]["task_id"] == "task-x"

    def test_input_dict_not_mutated(self):
        """The function returns a new dict; the original is unchanged."""
        proof_records = {
            "old-cap": [_make_proof_entry("task-1")],
        }
        original_keys = set(proof_records.keys())
        consolidated = [{"survivor": "new-cap", "absorbed": ["old-cap"]}]

        carry_proof_records_for_consolidation(consolidated, proof_records)

        assert set(proof_records.keys()) == original_keys
