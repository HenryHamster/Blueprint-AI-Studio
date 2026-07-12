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

import pytest

from app.capability_proof import (
    _MAX_PROOF_ENTRIES_PER_CAPABILITY,
    _PROOF_KIND_BACKING_PATH,
    _PROOF_KIND_TRIAL_ADOPTION,
    _extract_proof_ref,
    _proof_kind_from_verdict,
    annotate_capability_proof_status,
    carry_proof_records_for_consolidation,
    commission_proof_earn_loop,
    enrich_problem_model_capability_proofs,
    record_capability_backing_path_proof,
    record_capability_proof,
    record_capability_proof_from_trial,
    record_gap_evidence,
    should_record_capability_proof,
)
from app.capability_registry import (
    capability_need_blocked_by_proof_gate,
    registry_capability_evidence_refs_map,
    working_capability_proof_status,
)
from app.evidence_primitive import classify_evidence_tiered
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
    # Matches the real stored shape written by record_code_acceptance_verdict.
    return {
        "proven": proven,
        "satisfied": proven,
        "kind": "named_test",
        "reason": f"named_test {test_path!r} was added/modified, ran green",
        "proof_ref": test_path if proven else None,
    }


def _artifact_verdict(artifact_id="art-123", proven=True):
    # Matches the real stored shape written by record_code_acceptance_verdict.
    return {
        "proven": proven,
        "satisfied": proven,
        "kind": "downstream_artifact",
        "reason": f"downstream_artifact {artifact_id!r} is reflected in shipped files",
        "proof_ref": artifact_id if proven else None,
    }


# ── should_record_capability_proof ────────────────────────────────────────────


class TestShouldRecordCapabilityProof:
    def test_proven_with_capability_returns_true(self):
        task = _make_task(
            provides_capability="language-aware-repo-map",
            verdict=_named_test_verdict(),
        )
        assert should_record_capability_proof(
            task, task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        )

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


def test_proof_ref_and_kind_from_production_verdict_shape():
    """proof_kind and proof_ref must reflect the real stored verdict shape.

    The stored verdict (written by record_code_acceptance_verdict) carries
    top-level ``kind`` and ``proof_ref`` keys — no ``evidence`` sub-object.
    This test exercises _proof_kind_from_verdict and _extract_proof_ref with
    that real shape, then drives the full record_capability_proof pipeline to
    confirm the stamped entry carries correct provenance.
    """
    named_test_selector = "backend/testing/test_capability_proof.py::test_proof_ref_and_kind_from_production_verdict_shape"

    # Real stored shape for a named_test proven verdict.
    named_test_stored = {
        "satisfied": True,
        "proven": True,
        "kind": "named_test",
        "reason": f"named_test {named_test_selector!r} was added/modified, ran green",
        "proof_ref": named_test_selector,
    }
    assert _proof_kind_from_verdict(named_test_stored) == "named_test"
    assert _extract_proof_ref(named_test_stored) == named_test_selector

    # Real stored shape for a downstream_artifact proven verdict.
    artifact_stored = {
        "satisfied": True,
        "proven": True,
        "kind": "downstream_artifact",
        "reason": "downstream_artifact 'release-deploy-artifact' is reflected in shipped files",
        "proof_ref": "release-deploy-artifact",
    }
    assert _proof_kind_from_verdict(artifact_stored) == "downstream_artifact"
    assert _extract_proof_ref(artifact_stored) == "release-deploy-artifact"

    # Real stored shape for a visual proven verdict.
    visual_stored = {
        "satisfied": True,
        "proven": True,
        "kind": "visual",
        "reason": "visual acceptance evidence matches the uploaded viewport screenshot",
        "proof_ref": "DashboardHome@375",
    }
    assert _proof_kind_from_verdict(visual_stored) == "visual"
    assert _extract_proof_ref(visual_stored) == "DashboardHome@375"

    # Drive the full record_capability_proof pipeline with a named_test verdict.
    task = _make_task(
        provides_capability="capability-proof-provenance",
        verdict=named_test_stored,
        commit_sha="sha-prod-shape",
    )
    project = _make_project()
    with patch("app.capability_proof.flag_modified"):
        recorded = record_capability_proof(task, project)

    assert recorded == ["capability-proof-provenance"]
    entry = project.capability_proof_records["capability-proof-provenance"][0]
    assert entry["proof_kind"] == "named_test"
    assert entry["proof_ref"] == named_test_selector
    assert entry["commit_sha"] == "sha-prod-shape"


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

    def test_enrich_backing_path_only_not_proven(self):
        """A capability with only backing_path proof records must read 'path_proven', not 'proven'.

        backing_path proofs assert only that the declared files exist at HEAD —
        they are self-reported structural evidence, not machine-verified.
        The enforce gate must not be cleared by backing_path alone.
        """
        problem_model = {
            "revision": 1,
            "capabilities": [
                {"name": "cap-backing-only", "status": "working"},
                {"name": "cap-named-test", "status": "working"},
                {"name": "cap-mixed", "status": "working"},
                {"name": "cap-empty", "status": "planned"},
            ],
        }
        proof_records = {
            "cap-backing-only": [
                {
                    "task_id": "task-1",
                    "proved_at": "2026-07-01T00:00:00Z",
                    "proof_kind": "backing_path",
                    "proof_ref": "backend/app/capability_proof.py",
                    "commit_sha": "abc123",
                }
            ],
            "cap-named-test": [
                {
                    "task_id": "task-2",
                    "proved_at": "2026-07-01T00:00:00Z",
                    "proof_kind": "named_test",
                    "proof_ref": "backend/testing/test_capability_proof.py::test_enrich_backing_path_only_not_proven",
                    "commit_sha": "abc123",
                }
            ],
            "cap-mixed": [
                {
                    "task_id": "task-3a",
                    "proved_at": "2026-07-01T00:00:00Z",
                    "proof_kind": "backing_path",
                    "proof_ref": "backend/app/capability_proof.py",
                    "commit_sha": "abc123",
                },
                {
                    "task_id": "task-3b",
                    "proved_at": "2026-07-01T00:00:00Z",
                    "proof_kind": "named_test",
                    "proof_ref": "backend/testing/test_capability_proof.py::test_enrich_backing_path_only_not_proven",
                    "commit_sha": "abc123",
                },
            ],
        }

        enriched = enrich_problem_model_capability_proofs(problem_model, proof_records)

        caps = {c["name"]: c for c in enriched["capabilities"]}
        # backing_path-only proof is classified as path_proven by classify_evidence_tiered
        assert caps["cap-backing-only"]["proof_status"] == "path_proven"
        # named_test proof is fully proven
        assert caps["cap-named-test"]["proof_status"] == "proven"
        # mixed (backing_path + named_test) is proven (corroborated)
        assert caps["cap-mixed"]["proof_status"] == "proven"
        # no proof records → unproven
        assert caps["cap-empty"]["proof_status"] == "unproven"


class TestCapabilityBadgeReflectsEvidence:
    """annotate_capability_proof_status: evidence_refs_map (tier-1) overrides
    proof_records (tier-2) for capability badge accuracy.

    The named test ``test_spawn_badge_reflects_reverified_capability_evidence``
    exercises the exact defect: a capability with proof_records claiming proven
    status but re-verified evidence_refs showing no proven refs must read
    ``partial`` in the badge, not ``proven``.
    """

    def test_spawn_badge_reflects_reverified_capability_evidence(self):
        """evidence_refs_map classification wins over proof_records existence check."""
        vocab = [
            {"name": "ci-pipeline", "status": "working"},
            {"name": "repo-map", "status": "working"},
            {"name": "unproven-cap", "status": "working"},
        ]
        proof_records = {
            "ci-pipeline": [
                {
                    "task_id": "task-1",
                    "proved_at": "2026-06-01T00:00:00Z",
                    "proof_kind": "named_test",
                    "proof_ref": "testing/test_ci.py::test_ci",
                    "commit_sha": "abc",
                }
            ],
            "repo-map": [
                {
                    "task_id": "task-2",
                    "proved_at": "2026-06-02T00:00:00Z",
                    "proof_kind": "named_test",
                    "proof_ref": "testing/test_repo_map.py::test_map",
                    "commit_sha": "def",
                }
            ],
        }
        evidence_refs_map = {
            # ci-pipeline has evidence but none proven → partial
            "ci-pipeline": [
                {
                    "kind": "named_test",
                    "ref": "test_ci.py::test_ci",
                    "proven": False,
                    "source_id": "task-1",
                }
            ],
            # repo-map has a proven ref → proven
            "repo-map": [
                {
                    "kind": "named_test",
                    "ref": "test_repo_map.py::test_map",
                    "proven": True,
                    "source_id": "task-2",
                }
            ],
        }

        annotated = annotate_capability_proof_status(
            vocab, proof_records, evidence_refs_map
        )

        # ci-pipeline: evidence_refs_map has no proven ref → partial (tier-1 wins
        # over tier-2 proof_records which would say "proven")
        assert annotated[0]["proof_status"] == "partial", (
            "ci-pipeline: evidence_refs shows only proven=False refs → partial, "
            "despite proof_records marking it proven"
        )
        # repo-map: evidence_refs_map has a proven=True ref → proven
        assert annotated[1]["proof_status"] == "proven", (
            "repo-map: evidence_refs has proven=True → proven"
        )
        # unproven-cap: no entry in evidence_refs_map, falls back to proof_records
        # which has no record → unproven
        assert annotated[2]["proof_status"] == "unproven", (
            "unproven-cap: no evidence_refs_map entry and no proof_record → unproven"
        )
        # Original vocab must be unmutated (fail-open contract)
        assert "proof_status" not in vocab[0]

    def test_registry_capability_evidence_refs_map_builds_correctly(self):
        """registry_capability_evidence_refs_map extracts evidence_refs from problem_model."""
        problem_model = {
            "capabilities": [
                {
                    "name": "ci-pipeline",
                    "status": "working",
                    "evidence_refs": [
                        {
                            "kind": "named_test",
                            "ref": "test_ci.py::test_ci",
                            "proven": True,
                        }
                    ],
                },
                {"name": "empty-ev", "status": "working", "evidence_refs": []},
                {"name": "no-refs", "status": "working"},
                {"name": "planned-cap", "status": "planned"},
            ]
        }
        refs_map = registry_capability_evidence_refs_map(problem_model)

        assert "ci-pipeline" in refs_map, (
            "ci-pipeline with non-empty evidence_refs must be present"
        )
        assert "empty-ev" not in refs_map, (
            "empty-ev with empty evidence_refs must be omitted"
        )
        assert "no-refs" not in refs_map, (
            "no-refs with missing evidence_refs must be omitted"
        )
        assert "planned-cap" not in refs_map, (
            "planned-cap with no evidence_refs must be omitted"
        )
        assert len(refs_map["ci-pipeline"]) == 1
        assert refs_map["ci-pipeline"][0]["proven"] is True


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


# ── record_capability_proof_from_trial ───────────────────────────────────────


class TestRecordCapabilityProofFromTrial:
    def test_adopt_verdict_records_proof_entry(self):
        project = _make_project()
        entry = {
            "verdict": "adopt",
            "verdict_reason": "sufficient_data",
            "lift": 0.08,
            "n_with": 40,
            "n_without": 38,
        }
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "repo_map", entry)

        assert "repo-map" in project.capability_proof_records
        entries = project.capability_proof_records["repo-map"]
        assert len(entries) == 1
        e = entries[0]
        assert e["proof_kind"] == _PROOF_KIND_TRIAL_ADOPTION
        assert e["lift"] == 0.08
        assert e["n_with"] == 40
        assert e["n_without"] == 38
        assert e["proof_ref"] == "repo-map"
        assert e["task_id"] == "trial:repo-map"
        assert "proved_at" in e

    def test_adopt_uppercase_records_proof_entry(self):
        project = _make_project()
        entry = {"verdict": "ADOPT", "lift": 0.06, "n_with": 35, "n_without": 33}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "module_interfaces", entry)

        assert "module-interfaces" in project.capability_proof_records

    def test_reject_verdict_records_nothing(self):
        project = _make_project()
        entry = {"verdict": "reject", "lift": -0.05, "n_with": 40, "n_without": 40}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "repo_map", entry)

        assert project.capability_proof_records == {}

    def test_no_effect_verdict_records_nothing(self):
        project = _make_project()
        entry = {"verdict": "no_effect", "lift": 0.01, "n_with": 90, "n_without": 90}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "repo_map", entry)

        assert project.capability_proof_records == {}

    def test_continue_trial_records_nothing(self):
        project = _make_project()
        entry = {"verdict": "continue-trial", "lift": None, "n_with": 5, "n_without": 5}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "repo_map", entry)

        assert project.capability_proof_records == {}

    def test_dedup_key_is_idempotent_on_re_evaluation(self):
        project = _make_project()
        entry = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 38}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "repo_map", entry)
            record_capability_proof_from_trial(project, "repo_map", entry)

        # Second call must not duplicate the entry.
        assert len(project.capability_proof_records["repo-map"]) == 1

    def test_cap_bounded_at_max_entries(self):
        cap_key = "repo-map"
        existing = [
            {
                "task_id": f"task-{i}",
                "proved_at": "2026-01-01T00:00:00",
                "proof_kind": "named_test",
                "proof_ref": cap_key,
                "commit_sha": "",
            }
            for i in range(_MAX_PROOF_ENTRIES_PER_CAPABILITY)
        ]
        project = _make_project()
        project.capability_proof_records = {cap_key: existing[:]}

        entry = {"verdict": "adopt", "lift": 0.09, "n_with": 50, "n_without": 48}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "repo_map", entry)

        result = project.capability_proof_records[cap_key]
        assert len(result) == _MAX_PROOF_ENTRIES_PER_CAPABILITY
        assert result[-1]["task_id"] == "trial:repo-map"

    def test_proof_flips_registry_capability_to_proven(self):
        project = _make_project()
        entry = {"verdict": "adopt", "lift": 0.08, "n_with": 40, "n_without": 38}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "repo_map", entry)

        vocab = [{"name": "repo-map", "status": "working"}]
        annotated = annotate_capability_proof_status(
            vocab, project.capability_proof_records
        )
        assert annotated[0]["proof_status"] == "proven"

    def test_normalizes_cap_name_with_underscores(self):
        project = _make_project()
        entry = {"verdict": "adopt", "lift": 0.07, "n_with": 35, "n_without": 33}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, "related_tests", entry)

        assert "related-tests" in project.capability_proof_records


# ── record_capability_backing_path_proof ─────────────────────────────────────


class TestRecordCapabilityBackingPathProof:
    """record_capability_backing_path_proof records backing_path proof entries."""

    def test_records_proof_entry_with_correct_shape(self):
        """A proof entry is recorded with the correct fields."""
        project = _make_project()
        with patch("app.capability_proof.flag_modified"):
            result = record_capability_backing_path_proof(project, "repo-map")

        assert result is True
        assert "repo-map" in project.capability_proof_records
        entries = project.capability_proof_records["repo-map"]
        assert len(entries) == 1
        e = entries[0]
        assert e["proof_kind"] == _PROOF_KIND_BACKING_PATH
        assert e["proof_ref"] == "repo-map"
        assert e["task_id"] == "backing:repo-map"
        assert "proved_at" in e
        assert e["commit_sha"] == ""

    def test_dedup_returns_false_on_repeat_call(self):
        """A second call with the same capability name returns False (no duplicate)."""
        project = _make_project()
        with patch("app.capability_proof.flag_modified"):
            first = record_capability_backing_path_proof(project, "repo-map")
            second = record_capability_backing_path_proof(project, "repo-map")

        assert first is True
        assert second is False
        assert len(project.capability_proof_records["repo-map"]) == 1

    def test_capped_at_max_entries(self):
        """The proof entry list is bounded at _MAX_PROOF_ENTRIES_PER_CAPABILITY."""
        cap_key = "repo-map"
        existing = [
            {
                "task_id": f"task-{i}",
                "proved_at": "2026-01-01T00:00:00",
                "proof_kind": "named_test",
                "proof_ref": cap_key,
                "commit_sha": "",
            }
            for i in range(_MAX_PROOF_ENTRIES_PER_CAPABILITY)
        ]
        project = _make_project()
        project.capability_proof_records = {cap_key: existing[:]}

        with patch("app.capability_proof.flag_modified"):
            result = record_capability_backing_path_proof(project, "repo-map")

        assert result is True
        result_entries = project.capability_proof_records[cap_key]
        assert len(result_entries) == _MAX_PROOF_ENTRIES_PER_CAPABILITY
        assert result_entries[-1]["task_id"] == "backing:repo-map"

    def test_normalizes_cap_name_with_underscores(self):
        """Capability names with underscores are normalized to hyphens."""
        project = _make_project()
        with patch("app.capability_proof.flag_modified"):
            result = record_capability_backing_path_proof(project, "related_tests")

        assert result is True
        # Normalized: "related_tests" → "related-tests"
        assert "related-tests" in project.capability_proof_records
        assert "related_tests" not in project.capability_proof_records

    def test_normalize_returns_false_when_name_is_invalid(self):
        """A capability name that cannot be normalized returns False."""
        project = _make_project()
        with patch("app.capability_proof.flag_modified") as mock_fm:
            result = record_capability_backing_path_proof(project, "")

        assert result is False
        mock_fm.assert_not_called()

    def test_appends_evidence_refs(self):
        """An EvidenceRef with kind=backing_path is appended to problem_model."""
        cap_name = "repo-map"
        project = _make_project_with_problem_model(cap_name)

        with patch("app.capability_proof.flag_modified"):
            result = record_capability_backing_path_proof(project, cap_name)

        assert result is True
        cap = project.problem_model["capabilities"][0]
        refs = cap.get("evidence_refs", [])
        assert len(refs) == 1
        r = refs[0]
        assert r["proven"] is True
        assert r["kind"] == _PROOF_KIND_BACKING_PATH
        assert r["ref"] == cap_name
        assert r["source_id"] == "backing:repo-map"
        assert "recorded_at" in r

    def test_fail_open_on_malformed_project(self):
        """If capability_proof_records is absent, the function does not raise."""
        # A MagicMock with no real dict attribute — the isinstance guard
        # initializes an empty dict, so proof recording still works.
        project = MagicMock()
        project.capability_proof_records = None  # explicitly broken
        project.problem_model = None

        # Must not raise regardless of state
        result = record_capability_backing_path_proof(project, "repo-map")
        # The isinstance guard repairs None → {}, so it still works
        assert result is True


# ── evidence_refs wiring tests ────────────────────────────────────────────────


def _make_project_with_problem_model(cap_name: str, extra_cap_fields: dict | None = None) -> MagicMock:
    """Build a project with a problem_model containing one capability."""
    project = MagicMock()
    project.capability_proof_records = {}
    cap = {"name": cap_name, "status": "working"}
    if extra_cap_fields:
        cap.update(extra_cap_fields)
    project.problem_model = {"capabilities": [cap]}
    return project


class TestRecordCapabilityProofEvidenceRefs:
    """record_capability_proof also writes EvidenceRef entries to problem_model."""

    def test_record_capability_proof_appends_evidence_refs(self):
        """Named acceptance test: record_capability_proof must append an EvidenceRef
        entry to the matching Capability's evidence_refs in problem_model."""
        cap_name = "language-aware-repo-map"
        project = _make_project_with_problem_model(cap_name)
        task = _make_task(
            provides_capability=cap_name,
            verdict=_named_test_verdict("testing/test_prompt.py::test_map"),
            commit_sha="abc123",
        )

        with patch("app.capability_proof.flag_modified") as mock_fm:
            recorded = record_capability_proof(task, project)

        assert recorded == [cap_name]
        cap = project.problem_model["capabilities"][0]
        refs = cap.get("evidence_refs", [])
        assert len(refs) == 1, f"expected 1 evidence_ref, got {refs}"
        r = refs[0]
        assert r["proven"] is True
        assert r["kind"] == "named_test"
        assert r["ref"] == "testing/test_prompt.py::test_map"
        assert r["source_id"] == task.id
        assert "recorded_at" in r
        # flag_modified must be called for both columns.
        called_args = [call.args[1] for call in mock_fm.call_args_list]
        assert "capability_proof_records" in called_args
        assert "problem_model" in called_args

    def test_record_capability_proof_evidence_refs_capped(self):
        """evidence_refs is capped at _MAX_PROOF_ENTRIES_PER_CAPABILITY entries."""
        cap_name = "language-aware-repo-map"
        existing_refs = [
            {"kind": "named_test", "ref": f"test_{i}.py", "proven": True, "source_id": f"task-{i}"}
            for i in range(_MAX_PROOF_ENTRIES_PER_CAPABILITY)
        ]
        project = _make_project_with_problem_model(cap_name, {"evidence_refs": existing_refs[:]})

        task = _make_task(
            task_id="new-task-xyz",
            provides_capability=cap_name,
            verdict=_named_test_verdict(),
        )

        with patch("app.capability_proof.flag_modified"):
            record_capability_proof(task, project)

        refs = project.problem_model["capabilities"][0].get("evidence_refs", [])
        assert len(refs) == _MAX_PROOF_ENTRIES_PER_CAPABILITY
        assert refs[-1]["source_id"] == "new-task-xyz"

    def test_record_capability_proof_evidence_refs_dedup_by_source_id(self):
        """evidence_refs deduplicates by source_id (task_id)."""
        cap_name = "language-aware-repo-map"
        task_id = "task-dup-ref"
        existing_refs = [
            {"kind": "named_test", "ref": "test_foo.py", "proven": True, "source_id": task_id}
        ]
        project = _make_project_with_problem_model(cap_name, {"evidence_refs": existing_refs[:]})
        project.capability_proof_records = {cap_name: [
            {"task_id": "other-task", "proved_at": "2026-01-01T00:00:00",
             "proof_kind": "named_test", "proof_ref": "test_foo.py", "commit_sha": ""}
        ]}

        task = _make_task(
            task_id=task_id,
            provides_capability=cap_name,
            verdict=_named_test_verdict(),
        )

        with patch("app.capability_proof.flag_modified"):
            record_capability_proof(task, project)

        # proof_records: same task_id → deduped (returns [])
        # evidence_refs: same source_id → not duplicated
        refs = project.problem_model["capabilities"][0].get("evidence_refs", [])
        assert len(refs) == 1

    def test_record_capability_proof_fail_open_on_malformed_model(self):
        """If problem_model is absent/malformed, record_capability_proof does not raise."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = None  # malformed

        task = _make_task(
            provides_capability="some-cap",
            verdict=_named_test_verdict(),
        )

        with patch("app.capability_proof.flag_modified"):
            recorded = record_capability_proof(task, project)

        # proof_records still recorded even though problem_model is absent
        assert "some-cap" in recorded

    def test_record_capability_proof_from_trial_appends_evidence_refs(self):
        """record_capability_proof_from_trial appends a trial_adoption EvidenceRef."""
        cap_name = "repo-map"
        project = _make_project_with_problem_model(cap_name)

        entry = {"verdict": "adopt", "lift": 0.08, "n_with": 35, "n_without": 32}
        with patch("app.capability_proof.flag_modified"):
            record_capability_proof_from_trial(project, cap_name, entry)

        cap = project.problem_model["capabilities"][0]
        refs = cap.get("evidence_refs", [])
        assert len(refs) == 1
        r = refs[0]
        assert r["proven"] is True
        assert r["kind"] == _PROOF_KIND_TRIAL_ADOPTION
        assert r["ref"] == cap_name


# ── record_gap_evidence ───────────────────────────────────────────────────────


def _make_gap_project(gap_id: str, direction_id: str, extra_gap_fields: dict | None = None) -> MagicMock:
    """Build a project with a direction linked to a gap."""
    project = MagicMock()
    project.capability_proof_records = {}
    gap: dict = {"id": gap_id, "name": f"gap-{gap_id}", "description": "test gap"}
    if extra_gap_fields:
        gap.update(extra_gap_fields)
    project.problem_model = {"gaps": [gap], "capabilities": []}
    project.directions = [{"id": direction_id, "gap_id": gap_id, "status": "active"}]
    return project


class TestRecordGapEvidence:
    def test_stamps_evidence_ref_on_gap_with_direction_id(self):
        """A gap-linked task with a proven verdict produces evidence_refs on the right gap."""
        gap_id = "gap-001"
        direction_id = "dir-abc"
        task = _make_task(
            task_id="task-gap-001",
            verdict=_named_test_verdict("testing/test_foo.py::test_gap"),
        )
        task.context["direction_id"] = direction_id
        project = _make_gap_project(gap_id, direction_id)

        with patch("app.capability_proof.flag_modified") as mock_fm:
            record_gap_evidence(task, project)

        gap = project.problem_model["gaps"][0]
        refs = gap.get("evidence_refs", [])
        assert len(refs) == 1
        r = refs[0]
        assert r["proven"] is True
        assert r["kind"] == "named_test"
        assert r["ref"] == "testing/test_foo.py::test_gap"
        assert r["source_id"] == "task-gap-001"
        assert "recorded_at" in r
        mock_fm.assert_called_with(project, "problem_model")

    def test_skips_when_verdict_not_proven(self):
        """A task with an unproven verdict leaves evidence_refs untouched."""
        gap_id = "gap-002"
        direction_id = "dir-def"
        task = _make_task(
            task_id="task-gap-002",
            verdict=_named_test_verdict(proven=False),
        )
        task.context["direction_id"] = direction_id
        project = _make_gap_project(gap_id, direction_id)

        with patch("app.capability_proof.flag_modified") as mock_fm:
            record_gap_evidence(task, project)

        gap = project.problem_model["gaps"][0]
        assert gap.get("evidence_refs", []) == []
        mock_fm.assert_not_called()

    def test_skips_when_no_direction_id_in_context(self):
        """A task with no direction_id in context is a no-op."""
        project = _make_gap_project("gap-003", "dir-ghi")
        task = _make_task(task_id="task-no-dir", verdict=_named_test_verdict())
        # No direction_id in context.

        with patch("app.capability_proof.flag_modified") as mock_fm:
            record_gap_evidence(task, project)

        gap = project.problem_model["gaps"][0]
        assert gap.get("evidence_refs", []) == []
        mock_fm.assert_not_called()

    def test_skips_when_direction_not_found(self):
        """A direction_id that does not exist in project.directions is a no-op."""
        project = _make_gap_project("gap-004", "dir-jkl")
        task = _make_task(task_id="task-bad-dir", verdict=_named_test_verdict())
        task.context["direction_id"] = "dir-nonexistent"

        with patch("app.capability_proof.flag_modified") as mock_fm:
            record_gap_evidence(task, project)

        gap = project.problem_model["gaps"][0]
        assert gap.get("evidence_refs", []) == []
        mock_fm.assert_not_called()

    def test_skips_when_gap_not_found(self):
        """A direction with gap_id that does not exist in problem_model.gaps is a no-op."""
        project = _make_gap_project("gap-005", "dir-mno")
        # Direction points to a gap_id not in problem_model.
        project.directions = [{"id": "dir-mno", "gap_id": "gap-nonexistent"}]
        task = _make_task(task_id="task-bad-gap", verdict=_named_test_verdict())
        task.context["direction_id"] = "dir-mno"

        with patch("app.capability_proof.flag_modified") as mock_fm:
            record_gap_evidence(task, project)

        gap = project.problem_model["gaps"][0]
        assert gap.get("evidence_refs", []) == []
        mock_fm.assert_not_called()

    def test_dedup_by_source_id(self):
        """Calling record_gap_evidence twice with the same task is idempotent."""
        gap_id = "gap-006"
        direction_id = "dir-pqr"
        task = _make_task(task_id="task-dup-gap", verdict=_named_test_verdict())
        task.context["direction_id"] = direction_id
        project = _make_gap_project(gap_id, direction_id)

        with patch("app.capability_proof.flag_modified"):
            record_gap_evidence(task, project)
            record_gap_evidence(task, project)

        gap = project.problem_model["gaps"][0]
        refs = gap.get("evidence_refs", [])
        assert len(refs) == 1

    def test_capped_at_max_entries(self):
        """evidence_refs on a gap is capped at _MAX_PROOF_ENTRIES_PER_CAPABILITY."""
        gap_id = "gap-007"
        direction_id = "dir-stu"
        existing_refs = [
            {"kind": "named_test", "ref": f"test_{i}.py", "proven": True, "source_id": f"old-task-{i}"}
            for i in range(_MAX_PROOF_ENTRIES_PER_CAPABILITY)
        ]
        project = _make_gap_project(gap_id, direction_id, {"evidence_refs": existing_refs[:]})
        task = _make_task(task_id="task-new-gap", verdict=_named_test_verdict())
        task.context["direction_id"] = direction_id

        with patch("app.capability_proof.flag_modified"):
            record_gap_evidence(task, project)

        gap = project.problem_model["gaps"][0]
        refs = gap.get("evidence_refs", [])
        assert len(refs) == _MAX_PROOF_ENTRIES_PER_CAPABILITY
        assert refs[-1]["source_id"] == "task-new-gap"

    def test_fail_open_on_malformed_problem_model(self):
        """If problem_model is None, record_gap_evidence does not raise."""
        project = MagicMock()
        project.problem_model = None
        project.directions = [{"id": "dir-vwx", "gap_id": "gap-xyz"}]
        task = _make_task(task_id="task-malformed", verdict=_named_test_verdict())
        task.context["direction_id"] = "dir-vwx"

        # Must not raise.
        with patch("app.capability_proof.flag_modified") as mock_fm:
            record_gap_evidence(task, project)

        mock_fm.assert_not_called()

    def test_evidence_ref_fields_are_complete(self):
        """Verify all required EvidenceRef fields are present: kind, ref, proven, source_id, recorded_at."""
        gap_id = "gap-008"
        direction_id = "dir-yza"
        task = _make_task(
            task_id="task-fields",
            verdict=_named_test_verdict("testing/test_gap_evidence.py::test_fields"),
        )
        task.context["direction_id"] = direction_id
        project = _make_gap_project(gap_id, direction_id)

        with patch("app.capability_proof.flag_modified"):
            record_gap_evidence(task, project)

        gap = project.problem_model["gaps"][0]
        r = gap["evidence_refs"][0]
        assert set(r.keys()) >= {"kind", "ref", "proven", "source_id", "recorded_at"}
        assert r["proven"] is True


# ── commission_proof_earn_loop ───────────────────────────────────────────────


class TestCommissionProofEarnLoop:
    """Unit tests for commission_proof_earn_loop.

    The function is async and accepts an injected ``verify_path`` so we can
    test each branch without touching the filesystem.
    """

    @pytest.mark.asyncio
    async def test_no_demand_returns_empty(self):
        """No demanded capabilities → empty summary."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {}

        result = await commission_proof_earn_loop(
            project,
            demand_result={"total": 0, "demands": []},
            problem_model={"capabilities": [
                {"name": "my-cap", "status": "working"},
            ]},
        )
        assert result["total_demanded_unproven"] == 0
        assert result["earned"] == 0

    @pytest.mark.asyncio
    async def test_all_paths_present_records_proof(self):
        """When demanded unproven capability has all backing_paths present at
        HEAD, proof is earned."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {"capabilities": [
            {"name": "my-cap", "status": "working",
             "backing_paths": ["backend/app/capability_proof.py"]},
        ]}

        async def _verify(p: str) -> bool:
            return True

        with patch("app.capability_proof.flag_modified"):
            result = await commission_proof_earn_loop(
                project,
                demand_result={"total": 1, "demands": [
                    {"kind": "capability", "ref": "my-cap",
                     "dependent_count": 2},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
        assert result["earned"] >= 1
        # The proof should now be recorded on the project.
        records = project.capability_proof_records
        assert isinstance(records, dict)
        assert "my-cap" in records

    @pytest.mark.asyncio
    async def test_path_absent_no_proof(self):
        """When a backing_path does NOT exist at HEAD, proof is not recorded."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {"capabilities": [
            {"name": "my-cap", "status": "working",
             "backing_paths": ["backend/app/missing.py"]},
        ]}

        async def _verify(p: str) -> bool:
            return False

        with patch("app.capability_proof.flag_modified"):
            result = await commission_proof_earn_loop(
                project,
                demand_result={"total": 1, "demands": [
                    {"kind": "capability", "ref": "my-cap",
                     "dependent_count": 2},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
        assert result["earned"] == 0
        assert "my-cap" in result["still_unproven"]

    @pytest.mark.asyncio
    async def test_path_indeterminate_no_proof(self):
        """When path existence is indeterminate (None), proof is not recorded."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {"capabilities": [
            {"name": "my-cap", "status": "working",
             "backing_paths": ["backend/app/uncheckable.py"]},
        ]}

        async def _verify(p: str) -> None:
            return None

        with patch("app.capability_proof.flag_modified"):
            result = await commission_proof_earn_loop(
                project,
                demand_result={"total": 1, "demands": [
                    {"kind": "capability", "ref": "my-cap",
                     "dependent_count": 2},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
        assert result["earned"] == 0
        assert "my-cap" in result["still_unproven"]

    @pytest.mark.asyncio
    async def test_no_backing_paths_reported(self):
        """A demanded unproven cap with NO backing_paths is reported."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {"capabilities": [
            {"name": "no-backing-cap", "status": "working"},
        ]}

        async def _verify(p: str) -> bool:
            return True

        with patch("app.capability_proof.flag_modified"):
            result = await commission_proof_earn_loop(
                project,
                demand_result={"total": 1, "demands": [
                    {"kind": "capability", "ref": "no-backing-cap",
                     "dependent_count": 2},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
        assert result["earned"] == 0
        assert "no-backing-cap" in result["no_backing_paths"]
        assert result["no_backing_paths"] == ["no-backing-cap"]

    @pytest.mark.asyncio
    async def test_mixed_backing_paths_and_no_backing_paths(self):
        """Mixed results: some caps have backing_paths, some don't."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {"capabilities": [
            {"name": "has-paths", "status": "working",
             "backing_paths": ["backend/app/capability_proof.py"]},
            {"name": "no-paths", "status": "working"},
        ]}

        async def _verify(p: str) -> bool:
            return True

        with patch("app.capability_proof.flag_modified"):
            result = await commission_proof_earn_loop(
                project,
                demand_result={"total": 2, "demands": [
                    {"kind": "capability", "ref": "has-paths",
                     "dependent_count": 3},
                    {"kind": "capability", "ref": "no-paths",
                     "dependent_count": 1},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
        assert result["total_demanded_unproven"] == 2
        assert result["earned"] >= 1
        assert "has-paths" not in result["no_backing_paths"]
        assert "no-paths" in result["no_backing_paths"]

    @pytest.mark.asyncio
    async def test_mixed_verdicts_per_cap(self):
        """Some backing_paths fail, some succeed."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {"capabilities": [
            {"name": "good-cap", "status": "working",
             "backing_paths": ["backend/app/capability_proof.py"]},
            {"name": "bad-cap", "status": "working",
             "backing_paths": ["backend/app/missing.py"]},
        ]}

        async def _verify(p: str) -> bool:
            return p in {"backend/app/capability_proof.py"}

        with patch("app.capability_proof.flag_modified"):
            result = await commission_proof_earn_loop(
                project,
                demand_result={"total": 2, "demands": [
                    {"kind": "capability", "ref": "good-cap",
                     "dependent_count": 2},
                    {"kind": "capability", "ref": "bad-cap",
                     "dependent_count": 2},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
        assert result["earned"] == 1
        assert "bad-cap" in result["still_unproven"]

    @pytest.mark.asyncio
    async def test_already_proven_cap_excluded(self):
        """A demanded capability that is already proven is excluded from earning."""
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {"capabilities": [
            {"name": "proven-cap", "status": "working",
             "evidence_refs": [{"proven": True, "ref": "t1", "source_id": "s1"}]},
            {"name": "unproven-cap", "status": "working",
             "backing_paths": ["backend/app/capability_proof.py"]},
        ]}

        async def _verify(p: str) -> bool:
            return True

        with patch("app.capability_proof.flag_modified"):
            result = await commission_proof_earn_loop(
                project,
                demand_result={"total": 2, "demands": [
                    {"kind": "capability", "ref": "proven-cap",
                     "dependent_count": 1},
                    {"kind": "capability", "ref": "unproven-cap",
                     "dependent_count": 2},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
        assert result["total_demanded_unproven"] == 1
        assert result["earned"] == 1

    @pytest.mark.asyncio
    async def test_dedup_hit_not_counted_as_earned(self):
        """ACCEPTANCE EVIDENCE (fix: earn loop masks no-progress, reports dedup as earned).

        A second call to commission_proof_earn_loop for a capability that already
        has a backing_path proof record (dedup hit from a prior sweep) must NOT
        count the dedup as 'earned'. The fix ensures earned=0 so the reaper sweep
        does not commit unnecessarily on no-op re-runs.
        """
        project = MagicMock()
        project.capability_proof_records = {}
        project.problem_model = {"capabilities": [
            {"name": "my-cap", "status": "working",
             "backing_paths": ["backend/app/capability_proof.py"]},
        ]}

        async def _verify(p: str) -> bool:
            return True

        with patch("app.capability_proof.flag_modified"):
            # First call: genuinely earns the proof.
            result1 = await commission_proof_earn_loop(
                project,
                demand_result={"total": 1, "demands": [
                    {"kind": "capability", "ref": "my-cap", "dependent_count": 2},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
            assert result1["earned"] == 1, "first call must earn the proof"

            # Second call: dedup hit — must NOT count as earned.
            result2 = await commission_proof_earn_loop(
                project,
                demand_result={"total": 1, "demands": [
                    {"kind": "capability", "ref": "my-cap", "dependent_count": 2},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
            assert result2["earned"] == 0, (
                "dedup hit (backing_path already recorded) must NOT be counted "
                "as earned — the sweep must not commit on no-op re-runs"
            )
            # Not a failure either: the dedup is not in still_unproven.
            assert "my-cap" not in result2["still_unproven"], (
                "dedup hit must not be reported as still_unproven — "
                "paths are present and a backing_path record already exists"
            )


# ── proof_exempt capability exemption ──────────────────────────────────────────


def _proven_ref() -> dict:
    return {"kind": "named_test", "ref": "test_foo.py::test_bar", "proven": True}


def test_exempt_capability_not_blocked_by_gate():
    """An exempt working capability is NOT blocked by the proof gate in enforce mode.

    This is the load-bearing acceptance test for the proof_exempt feature:
    a capability marked ``proof_exempt=True`` classifies as ``"proven"`` in
    ``working_capability_proof_status()``, so the enforce-mode gate predicate
    sees it as proven and returns ``False`` (not blocked).

    Without proof_exempt=True, the same capability with no evidence_refs
    classifies as ``"unproven"`` and the gate WOULD block it.
    """
    from app.capability_registry import (
        capability_need_blocked_by_proof_gate,
        working_capability_proof_status,
    )

    # Exempt working capability with zero evidence.
    model = {
        "capabilities": [
            {
                "name": "human-code-review",
                "status": "working",
                "evidence_refs": [],
                "proof_exempt": True,
            }
        ]
    }
    proof = working_capability_proof_status(model)

    # Exempt cap classifies as proven — the core semantic.
    assert proof.get("human-code-review") == "proven", (
        "exempt working capability must classify as 'proven'"
    )

    # In enforce mode, the gate predicate returns False (not blocked).
    need = {"kind": "capability", "ref": "human-code-review"}
    assert not capability_need_blocked_by_proof_gate(
        need, proof, mode="enforce"
    ), "exempt cap must not be blocked in enforce mode"

    # Same capability without proof_exempt WOULD be blocked.
    model_no_exempt = {
        "capabilities": [
            {
                "name": "human-code-review",
                "status": "working",
                "evidence_refs": [],
            }
        ]
    }
    proof_no_exempt = working_capability_proof_status(model_no_exempt)
    assert proof_no_exempt.get("human-code-review") == "unproven", (
        "non-exempt working cap with no evidence must classify as 'unproven'"
    )
    assert capability_need_blocked_by_proof_gate(
        need, proof_no_exempt, mode="enforce"
    ), "non-exempt unproven cap must be blocked in enforce mode"


def test_exempt_capability_not_in_unproven_count():
    """Exempt working caps classify as 'proven' and do NOT appear in the unproven set."""
    from app.capability_registry import working_capability_proof_status

    model = {
        "capabilities": [
            {"name": "real-proven", "status": "working",
             "evidence_refs": [_proven_ref()]},
            {"name": "exempt-cap", "status": "working",
             "evidence_refs": [], "proof_exempt": True},
            {"name": "unproven-cap", "status": "working",
             "evidence_refs": []},
        ]
    }
    proof = working_capability_proof_status(model)

    # All three map to 'proven', 'proven', and 'unproven' respectively.
    assert proof == {
        "real-proven": "proven",
        "exempt-cap": "proven",
        "unproven-cap": "unproven",
    }

    # Exempt cap is NOT in the unproven set.
    unproven = {n for n, s in proof.items() if s != "proven"}
    assert "exempt-cap" not in unproven
    assert unproven == {"unproven-cap"}


def test_exempt_capability_names_returns_correct_set():
    """exempt_capability_names returns working+exempt names, excluding non-working/unexempt."""
    from app.capability_registry import exempt_capability_names

    model = {
        "capabilities": [
            {"name": "exempt-working", "status": "working", "proof_exempt": True},
            {"name": "non-exempt-working", "status": "working", "proof_exempt": False},
            {"name": "exempt-planned", "status": "planned", "proof_exempt": True},
            {"name": "no-exempt-field", "status": "working"},
        ]
    }
    exempt = exempt_capability_names(model)
    assert exempt == {"exempt-working"}, (
        "only working+exempt caps should be returned; got: " + str(exempt)
    )

    # Empty model returns empty set.
    assert exempt_capability_names({}) == set()
    assert exempt_capability_names(None) == set()


def test_exempt_capability_skipped_in_earn_loop():
    """commission_proof_earn_loop skips exempt caps (defensive guard).

    ``working_capability_proof_status`` classifies exempt working caps as
    ``"proven"``, so they never appear in the ``unproven_working`` set that
    ``compute_unproven_demanded_capabilities`` builds. The earn loop never
    tries to prove them — the exemption is enforced at the proof-status
    layer, not inside the earn loop itself.
    """
    import asyncio
    from unittest.mock import patch

    from app.capability_proof import commission_proof_earn_loop

    # An exempt working cap with no evidence_refs and no proof_records.
    # working_capability_proof_status classifies it as "proven", so
    # compute_unproven_demanded_capabilities returns an empty list.
    project = MagicMock()
    project.problem_model = {
        "capabilities": [
            {"name": "exempt-no-proof", "status": "working",
             "proof_exempt": True, "backing_paths": ["backend/app/main.py"]},
        ]
    }
    project.capability_proof_records = {}

    async def _verify(p: str) -> bool:
        return True

    with patch("app.capability_proof.flag_modified"):
        result = asyncio.get_event_loop().run_until_complete(
            commission_proof_earn_loop(
                project,
                demand_result={"total": 1, "demands": [
                    {"kind": "capability", "ref": "exempt-no-proof",
                     "dependent_count": 1},
                ]},
                problem_model=project.problem_model,
                verify_path=_verify,
            )
        )

    # Exempt caps are "proven", so compute_unproven_demanded_capabilities
    # returns []. Earn loop reports zero demanded unproven — the exempt
    # cap was never a target for proof earning.
    assert result["total_demanded_unproven"] == 0, (
        "exempt cap should not appear in demanded_unproven because "
        "working_capability_proof_status classifies it as 'proven'"
    )
    assert result["earned"] == 0

    # Now test with a mix: one exempt unproven cap, one real unproven cap.
    # The exempt cap is never demanded (proven), the real one is.
    project2 = MagicMock()
    project2.problem_model = {
        "capabilities": [
            {"name": "exempt-no-proof", "status": "working",
             "proof_exempt": True},
            {"name": "real-unproven", "status": "working",
             "backing_paths": ["backend/app/main.py"]},
        ]
    }
    project2.capability_proof_records = {}

    with patch("app.capability_proof.flag_modified"):
        result2 = asyncio.get_event_loop().run_until_complete(
            commission_proof_earn_loop(
                project2,
                demand_result={"total": 2, "demands": [
                    {"kind": "capability", "ref": "exempt-no-proof",
                     "dependent_count": 1},
                    {"kind": "capability", "ref": "real-unproven",
                     "dependent_count": 1},
                ]},
                problem_model=project2.problem_model,
                verify_path=_verify,
            )
        )

    # Only the non-exempt unproven cap is demanded.
    assert result2["total_demanded_unproven"] == 1, (
        "only the non-exempt unproven cap should be in demanded_unproven"
    )
    assert result2["earned"] == 1, (
        "the non-exempt cap with backing_paths should be earned"
    )


def test_release_qualifier_exempt_not_blocked():
    """The release qualifier does NOT set capability_proof_blocked when the only
    unproven caps are exempt."""
    from app.capability_registry import working_capability_proof_status

    # Scenario: 2 working caps. One exempt with no evidence, one non-exempt
    # with no evidence. The non-exempt is the only real unproven.
    model = {
        "capabilities": [
            {"name": "exempt-unproven", "status": "working",
             "proof_exempt": True, "evidence_refs": []},
            {"name": "real-unproven", "status": "working",
             "evidence_refs": []},
        ]
    }
    proof = working_capability_proof_status(model)

    # Exempt classifies as proven, non-exempt as unproven.
    assert proof["exempt-unproven"] == "proven"
    assert proof["real-unproven"] == "unproven"

    # Only real-unproven blocks.
    unproven = [n for n, s in proof.items() if s != "proven"]
    assert unproven == ["real-unproven"]

    # Scenario 2: ONLY exempt caps are unproven → no blockage.
    model2 = {
        "capabilities": [
            {"name": "exempt-only", "status": "working",
             "proof_exempt": True, "evidence_refs": []},
            {"name": "existent-proven", "status": "working",
             "evidence_refs": [_proven_ref()]},
        ]
    }
    proof2 = working_capability_proof_status(model2)
    unproven2 = [n for n, s in proof2.items() if s != "proven"]
    assert unproven2 == [], (
        "exempt caps are classified as proven, so no unproven remain"
    )


# ── backing_path proof tiering: enforce-gate clearance ────────────────────────


def _backing_path_ref(ref: str = "backend/app/foo.py") -> dict:
    """A proven backing_path evidence ref — files exist at HEAD, nothing ran."""
    return {"kind": "backing_path", "ref": ref, "proven": True}


def _named_test_ref(ref: str = "testing/test_foo.py::test_bar") -> dict:
    """A proven, machine-verified named_test evidence ref."""
    return {"kind": "named_test", "ref": ref, "proven": True}


class TestBackingPathTieringInEnforceGate:
    """A backing_path-only proof must be tiered below machine-verified proof.

    ``classify_evidence_tiered`` demotes a proven set whose ONLY proof is
    ``backing_path`` to ``path_proven`` (not ``proven``), so
    ``working_capability_proof_status`` reads ``path_proven`` for such a capability
    and ``capability_need_blocked_by_proof_gate`` keeps the dependent BLOCKED under
    enforce mode — the exact self-report vector the gate exists to close.
    """

    def test_classify_evidence_tiered_backing_path_only_returns_path_proven(self):
        assert classify_evidence_tiered([_backing_path_ref()]) == "path_proven"

    def test_classify_evidence_tiered_mixed_returns_proven(self):
        assert (
            classify_evidence_tiered([_backing_path_ref(), _named_test_ref()])
            == "proven"
        )

    def test_classify_evidence_tiered_no_refs_returns_unproven(self):
        assert classify_evidence_tiered([]) == "unproven"

    def test_classify_evidence_tiered_partial_unchanged(self):
        # No proven entries → partial (unchanged from classify_evidence).
        refs = [{"kind": "backing_path", "ref": "backend/app/foo.py", "proven": False}]
        assert classify_evidence_tiered(refs) == "partial"

    def test_backing_path_only_does_not_clear_enforce_gate(self):
        """ACCEPTANCE EVIDENCE. A working capability whose ONLY evidence is a
        proven ``backing_path`` maps to ``path_proven`` in
        ``working_capability_proof_status`` and stays BLOCKED under enforce."""
        problem_model = {
            "capabilities": [
                {
                    "name": "backing-only-cap",
                    "status": "working",
                    "evidence_refs": [_backing_path_ref()],
                },
            ]
        }
        proof = working_capability_proof_status(problem_model)
        # The backing-only proof is tiered down to path_proven (NOT proven).
        assert proof["backing-only-cap"] == "path_proven"

        need = {"kind": "capability", "ref": "backing-only-cap"}
        # Enforce mode: path_proven does NOT clear the gate → blocked.
        assert (
            capability_need_blocked_by_proof_gate(need, proof, mode="enforce") is True
        )
        # Off/observe modes never gate (behavior preserved).
        assert (
            capability_need_blocked_by_proof_gate(need, proof, mode="off") is False
        )
        assert (
            capability_need_blocked_by_proof_gate(need, proof, mode="observe") is False
        )

    def test_backing_path_plus_named_test_clears_enforce_gate(self):
        """A working capability with BOTH a proven backing_path AND a proven
        named_test maps to ``proven`` and clears the enforce gate."""
        problem_model = {
            "capabilities": [
                {
                    "name": "corroborated-cap",
                    "status": "working",
                    "evidence_refs": [_backing_path_ref(), _named_test_ref()],
                },
            ]
        }
        proof = working_capability_proof_status(problem_model)
        assert proof["corroborated-cap"] == "proven"

        need = {"kind": "capability", "ref": "corroborated-cap"}
        assert (
            capability_need_blocked_by_proof_gate(need, proof, mode="enforce") is False
        )

    def test_named_test_only_still_clears_enforce_gate(self):
        """A lone machine-verified named_test proof is unaffected — still proven."""
        problem_model = {
            "capabilities": [
                {
                    "name": "verified-cap",
                    "status": "working",
                    "evidence_refs": [_named_test_ref()],
                },
            ]
        }
        proof = working_capability_proof_status(problem_model)
        assert proof["verified-cap"] == "proven"
        need = {"kind": "capability", "ref": "verified-cap"}
        assert (
            capability_need_blocked_by_proof_gate(need, proof, mode="enforce") is False
        )

    def test_backing_paths_field_upgrade_still_partial(self):
        """ACCEPTANCE EVIDENCE (finding design:capability-proof-earning-loop):
        working_capability_proof_status now consults the capability's ``backing_paths``
        field. Before the fix, any capability declaring backing_paths but carrying
        zero evidence_refs was classified as ``unproven`` — the field was silently
        ignored. The fix upgrades ``unproven`` to ``partial`` when backing_paths is
        a non-empty list, so capabilities that declare where their code lives are no
        longer penalised as having zero evidence.
        """
        problem_model = {
            "capabilities": [
                {
                    "name": "paths-field-cap",
                    "status": "working",
                    "backing_paths": ["backend/app/foo.py"],
                },
            ]
        }
        proof = working_capability_proof_status(problem_model)
        assert proof["paths-field-cap"] == "partial"


# ── Earn loop is not flag-only: proof is written, not just computed ───────────


@pytest.mark.asyncio
async def test_commission_proof_earn_loop_writes_proof_not_flag_only():
    """ACCEPTANCE EVIDENCE (finding design:capability-proof-earning-loop):
    commission_proof_earn_loop WRITES proof records to project.capability_proof_records
    when backing_paths resolve at HEAD, demonstrating the system is not flag-only.

    Before the fix, check_capability_proof_gate_gap was the only pipeline mechanism
    that observed the capability-proof gap — it computed the delta and wrote findings
    but nothing in the system then commissioned work to actually earn the proof.
    commission_proof_earn_loop (called by sweep_proof_earn_loop / Step 22b in the
    reaper, and on-demand at spawn time in autonomous_research.py) closes this gap:
    it verifies declared backing_paths at HEAD and records a ``backing_path`` proof
    entry, so the capability advances from ``unproven`` to ``partial``.

    The test asserts that after the earn loop runs with all paths present:
    • ``earned == 1`` — one proof was committed, not merely noted
    • The capability name appears in project.capability_proof_records
    • ``no_backing_paths`` and ``still_unproven`` are both empty
    """
    project = MagicMock()
    project.capability_proof_records = {}
    problem_model = {
        "capabilities": [
            {
                "name": "unproven-demanded",
                "status": "working",
                "backing_paths": ["backend/app/capability_proof.py"],
            }
        ]
    }
    project.problem_model = problem_model
    demand_result = {
        "total": 1,
        "demands": [
            {"kind": "capability", "ref": "unproven-demanded", "dependent_count": 3},
        ],
    }

    async def _all_present(p: str) -> bool:
        return True

    with patch("app.capability_proof.flag_modified"):
        result = await commission_proof_earn_loop(
            project,
            demand_result=demand_result,
            problem_model=problem_model,
            verify_path=_all_present,
        )

    assert result["earned"] == 1, "earn loop must write proof, not just flag the gap"
    assert "unproven-demanded" in project.capability_proof_records
    assert result["no_backing_paths"] == []
    assert result["still_unproven"] == []


# ── TRIAL_DEFINITIONS disjointness from gating registry capabilities ──────────


def test_trial_cap_names_disjoint_from_gating_registry_caps():
    """ACCEPTANCE EVIDENCE (finding design:capability-proof-earning-loop):
    TRIAL_DEFINITIONS cap_names are internal agent behaviors and do NOT overlap
    with the business-capability names a strategist would register as ``working``
    in the project's problem_model.  Trial adoption therefore can NEVER prove a
    gating registry capability — only ``record_capability_proof`` (a task
    advertises ``provides_capability`` + proven acceptance verdict) or
    ``record_capability_backing_path_proof`` (reaper Step 22 / earn loop) can.

    This test verifies the structural invariant: ``working_capability_proof_status``
    on a model containing ONLY business-style registry caps returns zero proof
    from a trial adoption record keyed under a TRIAL_DEFINITIONS cap_name.
    """
    from app.capability_trial_verdict import get_trial_definitions
    from app.capability_registry import normalize_capability_name
    from unittest.mock import MagicMock, patch

    trial_cap_names = {
        normalize_capability_name(td["cap_name"])
        for td in get_trial_definitions()
        if normalize_capability_name(td["cap_name"])
    }

    # Typical business-capability registry: these MUST NOT overlap with
    # TRIAL_DEFINITIONS cap_names for the gate invariant to hold.
    registry_cap_names = [
        "code-review", "test-generation", "documentation-writing",
        "dependency-management", "security-scanning", "performance-profiling",
        "database-migration", "api-design", "deployment-automation",
    ]
    registry_normalized = {
        normalize_capability_name(n) for n in registry_cap_names
        if normalize_capability_name(n)
    }

    overlap = trial_cap_names & registry_normalized
    assert not overlap, (
        f"TRIAL_DEFINITIONS cap_names must be disjoint from registry capability "
        f"names — overlap detected: {overlap}. Trial adoption can only prove a "
        f"capability that shares its exact name."
    )

    # Mechanical verification: a trial ADOPT for a trial cap_name does NOT
    # stamp evidence on a registry capability with a different name.
    project = MagicMock()
    project.capability_proof_records = {}
    project.problem_model = {
        "capabilities": [
            {"name": cap, "status": "working"} for cap in registry_cap_names
        ]
    }

    trial_cap = next(iter(trial_cap_names))  # e.g. "repo-map"
    with patch("app.capability_proof.flag_modified"):
        record_capability_proof_from_trial(
            project,
            trial_cap,
            {"verdict": "adopt", "lift": 0.1, "n_with": 50, "n_without": 50},
        )

    # Proof was recorded under the trial cap_name key only.
    assert trial_cap in project.capability_proof_records
    # No registry capability received a proof record.
    for reg_cap in registry_normalized:
        assert reg_cap not in project.capability_proof_records, (
            f"trial adoption for {trial_cap!r} must not stamp proof "
            f"on registry capability {reg_cap!r}"
        )


# ── Earn-loop end-to-end: demand detection → proof earning ────────────────────


@pytest.mark.asyncio
async def test_earn_loop_demand_driven_intersection_and_proof():
    """ACCEPTANCE EVIDENCE (finding feat:earn-proof-for-demanded-unproven):
    commission_proof_earn_loop computes the intersection of demanded capabilities
    and unproven working capabilities and earns backing_path proof for demanded
    unproven caps that declare backing_paths.

    Specifically verifies:
    - compute_unproven_demanded_capabilities is used to filter to the intersection
    - proof is earned (via record_capability_backing_path_proof) only for the
      intersection, not for all demanded caps
    - caps NOT in the intersection (already proven) are excluded
    - the return summary reports earned, no_backing_paths, still_unproven correctly
    """
    project = MagicMock()
    project.capability_proof_records = {}
    problem_model = {
        "capabilities": [
            # demanded + unproven working + has backing_paths → WILL be earned
            {
                "name": "demanded-unproven",
                "status": "working",
                "backing_paths": ["backend/app/capability_proof.py"],
            },
            # demanded + already proven → excluded from earn loop
            {
                "name": "demanded-proven",
                "status": "working",
                "evidence_refs": [_named_test_ref()],
            },
            # demanded + unproven working + NO backing_paths → reported as gap
            {
                "name": "demanded-no-paths",
                "status": "working",
            },
        ]
    }
    project.problem_model = problem_model
    demand_result = {
        "total": 3,
        "demands": [
            {"kind": "capability", "ref": "demanded-unproven", "dependent_count": 5},
            {"kind": "capability", "ref": "demanded-proven", "dependent_count": 2},
            {"kind": "capability", "ref": "demanded-no-paths", "dependent_count": 1},
        ],
    }

    async def _all_paths_present(p: str) -> bool:
        return True

    with patch("app.capability_proof.flag_modified"):
        result = await commission_proof_earn_loop(
            project,
            demand_result=demand_result,
            problem_model=problem_model,
            verify_path=_all_paths_present,
        )

    # Only the 2 unproven working demanded caps are in the intersection.
    assert result["total_demanded_unproven"] == 2

    # demanded-unproven was earned (backing_paths present + all resolve).
    assert result["earned"] >= 1

    # demanded-no-paths is reported as needing backing_paths.
    assert "demanded-no-paths" in result["no_backing_paths"]

    # demanded-proved is NOT in any failure list (it was excluded from the
    # intersection by compute_unproven_demanded_capabilities, not counted at all).
    assert "demanded-proven" not in result["no_backing_paths"]
    assert "demanded-proven" not in result["still_unproven"]

    # Proof was recorded on the project for demanded-unproven.
    assert "demanded-unproven" in project.capability_proof_records
