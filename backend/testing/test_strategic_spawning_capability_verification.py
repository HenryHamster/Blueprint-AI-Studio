"""strategic_spawning: capability proof/verification and cycle-start reconciliation.

Covers ``_annotate_capability_proof_status``, ``_verify_capability_backing_paths``,
``_build_cycle_start_reconciliation``, ``_build_prune_candidates``, and the
onboarding goal / operator context refs that reach the first cycle's payload.

Shared scaffolding lives in ``_strategic_spawning_test_helpers.py``.
"""

import json
from datetime import datetime

import pytest
from unittest.mock import AsyncMock, patch

import app.drift_detection  # noqa: F401 — registers DirectionDriftSnapshot in SQLModel.metadata for create_all
from app.models import (
    MessageType,
    ProjectMessage,
)
from app.strategic_spawning import (
    _spawn_design_doc_update,
    _spawn_strategic_review,
    _OPERATOR_CONTEXT_REFS_KEEP,
)
from _strategic_spawning_test_helpers import (  # noqa: F401 — _ensure_test_account_pool is an autouse fixture
    _ensure_test_account_pool,
    _make_project,
    _select_agent_tasks,
)


class TestAnnotateCapabilityProofStatus:
    """_annotate_capability_proof_status decorates the vocab list with
    ``proof_status: 'proven'`` when the project has a proof record for that
    capability and ``'unproven'`` when it does not.

    This is the production code path exercised when a task with
    ``provides_capability`` lands a verified acceptance-evidence verdict and
    ``record_capability_proof`` stamps a durable entry on
    ``project.capability_proof_records``.
    """

    def test_proven_capability_gets_proven_status(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [{"name": "language-aware-repo-map", "status": "working"}]
        proof_records = {
            "language-aware-repo-map": [
                {
                    "task_id": "task-abc",
                    "proved_at": "2026-06-09T00:00:00",
                    "proof_kind": "named_test",
                    "proof_ref": "testing/test_prompt.py::test_map",
                    "commit_sha": "abc123",
                }
            ]
        }
        out = _annotate_capability_proof_status(vocab, proof_records)
        assert out[0]["proof_status"] == "proven"
        assert out[0]["name"] == "language-aware-repo-map"

    def test_unproven_capability_gets_unproven_status(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [{"name": "language-aware-repo-map", "status": "working"}]
        out = _annotate_capability_proof_status(vocab, {})
        assert out[0]["proof_status"] == "unproven"

    def test_none_proof_records_yields_unproven(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [{"name": "cap-a", "status": "planned"}]
        out = _annotate_capability_proof_status(vocab, None)
        assert out[0]["proof_status"] == "unproven"

    def test_does_not_mutate_input(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        original = [{"name": "cap-a", "status": "working"}]
        proof_records = {
            "cap-a": [
                {
                    "task_id": "t1",
                    "proved_at": "",
                    "proof_kind": "named_test",
                    "proof_ref": "",
                    "commit_sha": "",
                }
            ]
        }
        out = _annotate_capability_proof_status(original, proof_records)
        # Input dict must not have been mutated.
        assert "proof_status" not in original[0]
        assert out[0]["proof_status"] == "proven"

    def test_empty_proof_list_is_unproven(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [{"name": "cap-a", "status": "working"}]
        out = _annotate_capability_proof_status(vocab, {"cap-a": []})
        # An empty list means the key exists but has no proof entries.
        assert out[0]["proof_status"] == "unproven"

    def test_multiple_capabilities_mixed_proof(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [
            {"name": "cap-proven", "status": "working"},
            {"name": "cap-claimed", "status": "partial"},
        ]
        proof_records = {
            "cap-proven": [
                {
                    "task_id": "t1",
                    "proved_at": "",
                    "proof_kind": "named_test",
                    "proof_ref": "",
                    "commit_sha": "",
                }
            ]
        }
        out = _annotate_capability_proof_status(vocab, proof_records)
        assert out[0]["proof_status"] == "proven"
        assert out[1]["proof_status"] == "unproven"

    def test_non_list_passthrough(self):
        from app.strategic_spawning import _annotate_capability_proof_status

        assert _annotate_capability_proof_status(None, {}) is None  # type: ignore[arg-type]
        assert _annotate_capability_proof_status("invalid", {}) == "invalid"  # type: ignore[arg-type]

    def test_alias_is_shared_registry_public_fn(self):
        # The annotation body now lives in the IO-free capability_registry leaf
        # so every spawn path (strategic, design, research, plan) can call it;
        # the module-local name is a back-compat alias for the same function.
        from app.capability_registry import annotate_capability_proof_status
        from app.strategic_spawning import _annotate_capability_proof_status

        assert _annotate_capability_proof_status is annotate_capability_proof_status

    def test_capability_proof_badge_reflects_reverified_evidence_status(self):
        """evidence_refs_map overrides proof_records for the badge.

        A capability that has durable proof_records (historically proven) but
        whose evidence_refs have been re-verified and show no proven=True ref
        must NOT show 'proven' — the re-verified status takes precedence over
        the write-once record-existence check.
        """
        from app.strategic_spawning import _annotate_capability_proof_status

        vocab = [
            {"name": "ci-pipeline", "status": "working"},
            {"name": "repo-map", "status": "working"},
            {"name": "untracked", "status": "planned"},
        ]
        # proof_records says both ci-pipeline and repo-map are "proven" (old logic)
        proof_records = {
            "ci-pipeline": [{"task_id": "t1", "proof_kind": "named_test", "proof_ref": "test_ci.py::test_merge", "commit_sha": "abc"}],
            "repo-map": [{"task_id": "t2", "proof_kind": "named_test", "proof_ref": "test_map.py::test_map", "commit_sha": "def"}],
        }
        # evidence_refs_map: ci-pipeline re-verified — backing code removed, proven=False
        #                    repo-map re-verified — still present, proven=True
        evidence_refs_map = {
            "ci-pipeline": [{"kind": "named_test", "ref": "test_ci.py::test_merge", "proven": False}],
            "repo-map": [{"kind": "named_test", "ref": "test_map.py::test_map", "proven": True}],
        }
        out = _annotate_capability_proof_status(vocab, proof_records, evidence_refs_map)

        # repo-map: evidence_refs has proven=True → "proven" (consistent with old logic)
        assert out[1]["proof_status"] == "proven"
        # ci-pipeline: evidence_refs has proven=False only → classify_evidence → "partial"
        # (refs exist but none is proven=True), overriding the stale proof_record
        assert out[0]["proof_status"] == "partial"
        assert out[0]["proof_status"] != "proven"
        # untracked: no entry in evidence_refs_map → falls back to proof_records → "unproven"
        assert out[2]["proof_status"] == "unproven"


# ── _verify_capability_backing_paths: advisory capability verification ────────


class TestVerifyCapabilityBackingPaths:
    """The strategic payload annotates each capability with advisory backing-
    path verification, never mutating the durable problem_model.

    classify_paths_tristate is patched per-test so the cases are deterministic
    and don't depend on the integration-branch file tree.
    """

    @pytest.mark.asyncio
    async def test_empty_pm_returns_empty(self):
        from app.strategic_spawning import _verify_capability_backing_paths

        assert await _verify_capability_backing_paths(None) == {}
        assert await _verify_capability_backing_paths({}) == {}

    @pytest.mark.asyncio
    async def test_no_backing_paths_is_unverified(self):
        from app import strategic_spawning

        pm = {"capabilities": [{"name": "CapA", "status": "working"}]}
        # classify must never be called when there are no paths to check.
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "unverified"
        assert ver["present"] == [] and ver["missing"] == []

    @pytest.mark.asyncio
    async def test_all_present_preserves_status(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py", "b.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(return_value=(["a.py", "b.py"], [], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "working"
        assert ver["present"] == ["a.py", "b.py"]
        assert ver["missing"] == []

    @pytest.mark.asyncio
    async def test_mixed_presence_is_partial(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py", "gone.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(return_value=(["a.py"], ["gone.py"], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "partial"
        assert ver["present"] == ["a.py"]
        assert ver["missing"] == ["gone.py"]

    @pytest.mark.asyncio
    async def test_all_missing_is_absent(self):
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["gone1.py", "gone2.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(return_value=([], ["gone1.py", "gone2.py"], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        ver = out["capabilities"][0]["verification"]
        assert ver["effective_status"] == "absent"
        assert ver["present"] == []
        assert ver["missing"] == ["gone1.py", "gone2.py"]

    @pytest.mark.asyncio
    async def test_does_not_mutate_input(self):
        """The durable problem_model dict passed in is never mutated — only a
        copy is annotated."""
        from app import strategic_spawning

        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py"],
                }
            ]
        }
        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(return_value=(["a.py"], [], [])),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(pm)
        # Input capability is untouched (no verification key added in place).
        assert "verification" not in pm["capabilities"][0]
        assert out is not pm
        assert out["capabilities"][0] is not pm["capabilities"][0]

    @pytest.mark.asyncio
    async def test_passes_repo_branch_context_to_classifier(self):
        from app import strategic_spawning

        seen_kwargs = {}
        pm = {
            "capabilities": [
                {
                    "name": "CapA",
                    "status": "working",
                    "backing_paths": ["a.py"],
                }
            ]
        }

        async def _classify(paths, **kwargs):
            seen_kwargs.update(kwargs)
            return (paths, [], [])

        with patch.object(
            strategic_spawning,
            "classify_paths_tristate",
            new=AsyncMock(side_effect=_classify),
        ):
            out = await strategic_spawning._verify_capability_backing_paths(
                pm,
                token="ghs_test",
                owner="acme",
                repo="widget",
                branch="automain",
                project_id="proj-123",
            )
        assert out["capabilities"][0]["verification"]["effective_status"] == "working"
        assert seen_kwargs == {
            "token": "ghs_test",
            "owner": "acme",
            "repo": "widget",
            "branch": "automain",
            "project_id": "proj-123",
        }


# ── _build_cycle_start_reconciliation: conflict-focused starting state ─────────


class TestBuildCycleStartReconciliation:
    """The cycle-start reconciliation surfaces only the CONFLICTS between the
    four strategic surfaces (capabilities vs grounding, gaps vs directions,
    per-direction research pipeline). Pure function — directly unit-testable."""

    def test_returns_none_when_nothing_to_reconcile(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        # No active directions, no gaps, no capabilities → nothing to surface.
        assert _build_cycle_start_reconciliation([], {}, {}) is None
        assert _build_cycle_start_reconciliation([], None, None) is None

    def test_capability_contradiction_flags_stored_vs_grounded(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "capabilities": [
                # Contradiction: stored working, code shows absent.
                {
                    "name": "payments",
                    "status": "working",
                    "verification": {
                        "effective_status": "absent",
                        "present": [],
                        "missing": ["backend/app/payments.py"],
                    },
                },
                # Not a contradiction: stored matches grounded.
                {
                    "name": "search",
                    "status": "partial",
                    "verification": {
                        "effective_status": "partial",
                        "present": ["s.py"],
                        "missing": ["t.py"],
                    },
                },
                # Not a contradiction: unverified (no backing paths).
                {
                    "name": "memos",
                    "status": "working",
                    "verification": {
                        "effective_status": "unverified",
                        "present": [],
                        "missing": [],
                    },
                },
            ]
        }
        out = _build_cycle_start_reconciliation([], pm, {})
        # An active-direction-less project with a contradiction still returns a
        # block (the contradiction is content worth surfacing).
        assert out is not None
        contradictions = out["capability_contradictions"]
        assert len(contradictions) == 1
        assert contradictions[0]["name"] == "payments"
        assert contradictions[0]["stored_status"] == "working"
        assert contradictions[0]["effective_status"] == "absent"
        assert contradictions[0]["missing"] == ["backend/app/payments.py"]
        assert out["counts"]["capability_contradictions"] == 1

    def test_orphaned_gap_excludes_staffed_and_deferred(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "gaps": [
                # Orphan: active, no deferral, no live direction.
                {
                    "id": "g-orphan",
                    "name": "drifted",
                    "status": "active",
                    "priority": "high",
                },
                # Stale active_moves alone do NOT prove staffing.
                {
                    "id": "g-moved",
                    "name": "worked",
                    "status": "active",
                    "active_moves": ["d-9"],
                },
                # Staffed via a direction pointing back (gap_id) → not an orphan.
                {"id": "g-linked", "name": "linked", "status": "active"},
                # Intentionally deferred → counted separately, not an orphan.
                {
                    "id": "g-deferred",
                    "name": "parked",
                    "status": "active",
                    "deferral": {"rationale": "next version", "created_at": "x"},
                },
                # Closed gaps never count.
                {"id": "g-closed", "name": "done", "status": "closed"},
            ]
        }
        direction_stats = [
            {"id": "d-1", "status": "active", "gap_id": "g-linked"},
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        orphan_ids = [g["gap_id"] for g in out["orphaned_gaps"]]
        assert orphan_ids == ["g-orphan", "g-moved"]
        assert out["orphaned_gaps"][0]["priority"] == "high"
        assert out["orphaned_gaps"][0]["status"] == "active"
        assert out["counts"]["deferred_gaps"] == 1

    def test_orphaned_gap_includes_blocked_open_gaps(self):
        # A blocked gap is still an open problem; if no active direction works
        # it (and it is not deferred) it is orphan-eligible, flagged by status.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "gaps": [
                # Blocked + unstaffed → orphan, carries status="blocked".
                {
                    "id": "g-blocked",
                    "name": "blocked orphan",
                    "status": "blocked",
                    "priority": "medium",
                },
                # Blocked but staffed by a live direction → not an orphan.
                {"id": "g-blocked-linked", "name": "worked", "status": "blocked"},
                # Blocked + deferred → counted as deferred, not orphaned.
                {
                    "id": "g-blocked-deferred",
                    "name": "parked",
                    "status": "blocked",
                    "deferral": {"rationale": "later", "created_at": "x"},
                },
            ]
        }
        direction_stats = [
            {"id": "d-1", "status": "active", "gap_id": "g-blocked-linked"},
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        orphans = {g["gap_id"]: g["status"] for g in out["orphaned_gaps"]}
        assert orphans == {"g-blocked": "blocked"}
        assert out["counts"]["deferred_gaps"] == 1

    def test_staffed_but_deferred_gap_surfaced(self):
        """A gap that is both staffed (active direction via gap_id) and carries a
        deferral is a self-contradicting state — it must appear in
        ``staffed_but_deferred``, not be silently skipped."""
        from app.strategic_spawning import _build_cycle_start_reconciliation

        deferral = {"rationale": "pending review", "created_at": "2026-01-01"}
        pm = {
            "gaps": [
                # Staffed AND deferred → contradiction, must be surfaced.
                {
                    "id": "g-contradiction",
                    "name": "needs attention",
                    "status": "active",
                    "priority": "high",
                    "deferral": deferral,
                },
                # Staffed, no deferral → not a contradiction, not orphaned.
                {"id": "g-clean", "name": "clean", "status": "active"},
                # Deferred, not staffed → counted as deferred_gap, not contradiction.
                {
                    "id": "g-deferred-only",
                    "name": "parked",
                    "status": "active",
                    "deferral": {"rationale": "later", "created_at": "x"},
                },
            ]
        }
        direction_stats = [
            {"id": "d-1", "status": "active", "gap_id": "g-contradiction"},
            {"id": "d-2", "status": "active", "gap_id": "g-clean"},
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        # The contradiction is surfaced.
        assert len(out["staffed_but_deferred"]) == 1
        entry = out["staffed_but_deferred"][0]
        assert entry["gap_id"] == "g-contradiction"
        assert entry["deferral"] == deferral
        assert out["counts"]["staffed_but_deferred"] == 1
        # Staffed-no-deferral and deferred-unstaffed gaps are NOT in the list.
        assert out["orphaned_gaps"] == []
        assert out["counts"]["deferred_gaps"] == 1

    def test_ungrounded_directions_by_reason(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {"gaps": [{"id": "g-live", "name": "x", "status": "active"}]}
        direction_stats = [
            {"id": "d-nogap", "status": "active", "text": "no link"},
            {
                "id": "d-dangling",
                "status": "active",
                "text": "stale",
                "gap_id": "g-gone",
            },
            {"id": "d-ok", "status": "active", "text": "fine", "gap_id": "g-live"},
            # Paused direction is ignored (only active directions reconcile).
            {"id": "d-paused", "status": "paused", "text": "n/a"},
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        by_id = {d["direction_id"]: d["reason"] for d in out["ungrounded_directions"]}
        assert by_id == {"d-nogap": "no_gap", "d-dangling": "gap_missing"}

    def test_research_pipeline_states(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            # In flight → tasks_in_progress (regardless of findings).
            {
                "id": "d-busy",
                "status": "active",
                "text": "busy",
                "recent_findings": ["f1"],
            },
            # No tasks but has findings → research_waiting_on_action.
            {
                "id": "d-wait",
                "status": "active",
                "text": "wait",
                "recent_findings": ["f1", "f2"],
            },
            # Live-held work is distinct from waiting-on-action.
            {
                "id": "d-held",
                "status": "active",
                "text": "held",
                "recent_findings": ["f1"],
            },
            # Fresh completion before cycle start is also distinct.
            {
                "id": "d-recent",
                "status": "active",
                "text": "recent",
                "recent_findings": ["f1"],
            },
            # No tasks, no findings → idle.
            {"id": "d-idle", "status": "active", "text": "idle"},
        ]
        active_by_direction = {"d-busy": 2}
        held_by_direction = {"d-held": 1}
        per_direction_delta = [
            {
                "direction_id": "d-recent",
                "direction_text": "recent",
                "recent_merged_titles": [
                    {"title": "fresh merge", "merge_verdict": "verified_landed"},
                ],
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats,
            {},
            active_by_direction,
            held_by_direction,
            per_direction_delta,
        )
        states = {p["direction_id"]: p["state"] for p in out["direction_pipeline"]}
        assert states == {
            "d-busy": "tasks_in_progress",
            "d-wait": "research_waiting_on_action",
            "d-held": "held_on_dependency",
            "d-recent": "recently_converted",
            "d-idle": "idle",
        }
        busy = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-busy"
        )
        assert busy["in_flight"] == 2
        wait = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-wait"
        )
        assert wait["findings"] == 2
        held = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-held"
        )
        assert held["held"] == 1
        assert held["state"] == "held_on_dependency"
        recent = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-recent"
        )
        assert recent["recently_completed"] == 1
        assert recent["state"] == "recently_converted"
        assert out["counts"]["active_directions"] == 5
        assert out["counts"]["pipeline_waiting_on_action"] == 1
        assert out["counts"]["pipeline_tasks_in_progress"] == 1
        assert out["counts"]["pipeline_held_on_dependency"] == 1
        assert out["counts"]["pipeline_recently_converted"] == 1
        assert out["counts"]["pipeline_idle"] == 1

    def test_cycle_start_reconciliation_distinguishes_held_and_recently_converted_work(
        self,
    ):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {
                "id": "d-held",
                "status": "active",
                "text": "held lane",
                "recent_findings": ["f1"],
            },
            {
                "id": "d-recent",
                "status": "active",
                "text": "fresh lane",
                "recent_findings": ["f1", "f2"],
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats,
            {},
            {},
            {"d-held": 1},
            [
                {
                    "direction_id": "d-recent",
                    "direction_text": "fresh lane",
                    "recent_merged_titles": [
                        {"title": "fresh merge", "merge_verdict": "verified_landed"},
                    ],
                },
            ],
        )
        assert out is not None
        states = {p["direction_id"]: p["state"] for p in out["direction_pipeline"]}
        assert states == {
            "d-held": "held_on_dependency",
            "d-recent": "recently_converted",
        }
        assert out["counts"]["pipeline_waiting_on_action"] == 0
        assert out["counts"]["pipeline_held_on_dependency"] == 1
        assert out["counts"]["pipeline_recently_converted"] == 1

    def test_non_pr_completions_count_as_recently_converted(self):
        # Directions that produce only design/web_research/compute_experiment
        # findings never set pr_merged=True, so recent_merged_titles is empty.
        # The reconciliation should still classify them as recently_converted when
        # recent_non_pr_completed > 0.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {
                "id": "d-design",
                "status": "active",
                "text": "design direction",
                "recent_findings": ["f1"],
            },
            {
                "id": "d-wait",
                "status": "active",
                "text": "waiting direction",
                "recent_findings": ["f1"],
            },
        ]
        per_direction_delta = [
            {
                "direction_id": "d-design",
                "direction_text": "design direction",
                # No recent_merged_titles — direction only produced design docs.
                "recent_non_pr_completed": 2,
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats,
            {},
            {},
            {},
            per_direction_delta,
        )
        assert out is not None
        states = {p["direction_id"]: p["state"] for p in out["direction_pipeline"]}
        assert states["d-design"] == "recently_converted", (
            "non-PR completions should yield recently_converted, not research_waiting_on_action"
        )
        assert states["d-wait"] == "research_waiting_on_action"
        design_entry = next(
            p for p in out["direction_pipeline"] if p["direction_id"] == "d-design"
        )
        assert design_entry["recently_completed"] == 2
        assert out["counts"]["pipeline_recently_converted"] == 1
        assert out["counts"]["pipeline_waiting_on_action"] == 1

    def test_lists_are_capped_but_counts_carry_true_total(self):
        from app.strategic_spawning import (
            _build_cycle_start_reconciliation,
            _RECON_LIST_CAP,
        )

        gaps = [
            {"id": f"g-{i}", "name": f"orphan {i}", "status": "active"}
            for i in range(_RECON_LIST_CAP + 5)
        ]
        out = _build_cycle_start_reconciliation([], {"gaps": gaps}, {})
        assert len(out["orphaned_gaps"]) == _RECON_LIST_CAP
        # The count carries the true (uncapped) total so the renderer shows "+N more".
        assert out["counts"]["orphaned_gaps"] == _RECON_LIST_CAP + 5

    def test_pipeline_counts_survive_list_cap(self):
        from app.strategic_spawning import (
            _build_cycle_start_reconciliation,
            _RECON_LIST_CAP,
        )

        direction_stats = []
        for i in range(_RECON_LIST_CAP + 3):
            direction_stats.append(
                {
                    "id": f"d-w-{i}",
                    "status": "active",
                    "text": f"wait {i}",
                    "recent_findings": ["f1"],
                }
            )
        for i in range(2):
            direction_stats.append(
                {
                    "id": f"d-p-{i}",
                    "status": "active",
                    "text": f"progress {i}",
                    "recent_findings": [],
                }
            )
        active_by_direction = {"d-p-0": 1, "d-p-1": 2}
        out = _build_cycle_start_reconciliation(
            direction_stats, {}, active_by_direction
        )
        assert len(out["direction_pipeline"]) == _RECON_LIST_CAP
        assert out["counts"]["direction_pipeline"] == _RECON_LIST_CAP + 5
        assert out["counts"]["direction_pipeline_overflow"] == 5
        assert out["counts"]["pipeline_waiting_on_action"] == _RECON_LIST_CAP + 3
        assert out["counts"]["pipeline_tasks_in_progress"] == 2
        assert out["counts"]["pipeline_idle"] == 0

    def test_merge_accounting_flags_only_verified_not_landed(self):
        # Three merged titles within the recent window for one active direction:
        # one ancestry-verified landed, one badge-asserted-but-NOT-landed, one
        # unverifiable. Only the verified-not-landed one is a real conflict; the
        # unverifiable one must NOT manufacture a contradiction.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        # Clean gap linkage so the four pre-existing classes stay empty and the
        # only signal is merge-accounting drift.
        pm = {"gaps": [{"id": "g-1", "name": "x", "status": "active"}]}
        direction_stats = [
            {"id": "d-1", "status": "active", "text": "shipping dir", "gap_id": "g-1"},
        ]
        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "shipping dir",
                "recent_merged_titles": [
                    {"title": "landed feat", "merge_verdict": "verified_landed"},
                    {"title": "ghost feat", "merge_verdict": "verified_not_landed"},
                    {"title": "maybe feat", "merge_verdict": "unverifiable"},
                ],
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, None, per_direction_delta
        )
        assert out is not None
        acct = out["merge_accounting"]
        assert len(acct) == 1
        entry = acct[0]
        assert entry["direction_id"] == "d-1"
        assert entry["not_landed"] == 1
        assert entry["landed"] == 1
        assert entry["unverifiable"] == 1
        assert entry["not_landed_titles"] == ["ghost feat"]
        # The four pre-existing classes are unchanged by the extension.
        assert out["capability_contradictions"] == []
        assert out["orphaned_gaps"] == []
        assert out["ungrounded_directions"] == []
        # Aggregate tallies carry every bucket so nothing is silently truncated.
        assert out["counts"]["merge_accounting"] == 1
        assert out["counts"]["merge_not_landed"] == 1
        assert out["counts"]["merge_landed"] == 1
        assert out["counts"]["merge_unverifiable"] == 1

    def test_merge_accounting_landed_and_unverifiable_only_no_conflict(self):
        # A direction whose recent merges are all verified-landed or unverifiable
        # has NO accounting conflict — it must not appear in merge_accounting,
        # though its tallies still ride in counts.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-1", "status": "active", "text": "clean dir"},
        ]
        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "clean dir",
                "recent_merged_titles": [
                    {"title": "a", "merge_verdict": "verified_landed"},
                    {"title": "b", "merge_verdict": "unverifiable"},
                    {"title": "c", "merge_verdict": "unverifiable"},
                ],
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats, {}, {}, None, per_direction_delta
        )
        assert out is not None
        assert out["merge_accounting"] == []
        assert out["counts"]["merge_accounting"] == 0
        assert out["counts"]["merge_not_landed"] == 0
        assert out["counts"]["merge_landed"] == 1
        assert out["counts"]["merge_unverifiable"] == 2

    def test_merge_accounting_absent_when_no_delta(self):
        # Back-compatible: omitting per_direction_delta yields an empty
        # merge_accounting list and zero tallies (no second pass invented).
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [{"id": "d-1", "status": "active", "text": "x"}]
        out = _build_cycle_start_reconciliation(direction_stats, {}, {})
        assert out is not None
        assert out["merge_accounting"] == []
        assert out["counts"]["merge_accounting"] == 0
        assert out["counts"]["merge_not_landed"] == 0

    def test_merge_accounting_caps_titles_with_true_overflow(self):
        # A direction with more not-landed titles than the list cap keeps the
        # true count and reports the overflow on the entry.
        from app.strategic_spawning import (
            _build_cycle_start_reconciliation,
            _RECON_LIST_CAP,
        )

        titles = [
            {"title": f"ghost {i}", "merge_verdict": "verified_not_landed"}
            for i in range(_RECON_LIST_CAP + 4)
        ]
        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "noisy dir",
                "recent_merged_titles": titles,
            },
        ]
        out = _build_cycle_start_reconciliation([], {}, {}, None, per_direction_delta)
        assert out is not None
        entry = out["merge_accounting"][0]
        assert entry["not_landed"] == _RECON_LIST_CAP + 4
        assert len(entry["not_landed_titles"]) == _RECON_LIST_CAP
        assert entry["not_landed_titles_overflow"] == 4
        assert out["counts"]["merge_not_landed"] == _RECON_LIST_CAP + 4

    def test_merge_accounting_uses_exact_counts_beyond_visible_merge_sample(self):
        from app.strategic_spawning import (
            _build_cycle_start_reconciliation,
            _RECENT_MERGED_TITLES_CAP,
        )

        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "noisy dir",
                "recent_merged_titles": [
                    {"title": f"landed {i}", "merge_verdict": "verified_landed"}
                    for i in range(_RECENT_MERGED_TITLES_CAP)
                ],
                "recent_merge_verdict_counts": {
                    "verified_landed": _RECENT_MERGED_TITLES_CAP,
                    "verified_not_landed": 3,
                    "unverifiable": 0,
                },
                "recent_not_landed_titles": ["ghost 10", "ghost 11", "ghost 12"],
            },
        ]
        out = _build_cycle_start_reconciliation([], {}, {}, None, per_direction_delta)
        assert out is not None
        entry = out["merge_accounting"][0]
        assert entry["landed"] == _RECENT_MERGED_TITLES_CAP
        assert entry["not_landed"] == 3
        assert entry["unverifiable"] == 0
        assert entry["not_landed_titles"] == ["ghost 10", "ghost 11", "ghost 12"]
        assert entry["not_landed_titles_overflow"] == 0
        assert out["counts"]["merge_landed"] == _RECENT_MERGED_TITLES_CAP
        assert out["counts"]["merge_not_landed"] == 3

    def test_pending_in_open_rollup_emitted_and_not_treated_as_conflict(self):
        # PRs merged into an open rollup branch yield pending_in_open_rollup verdict.
        # They must appear in recon.pending_in_open_rollup (not merge_accounting),
        # counts.merge_pending_in_open_rollup must carry the title count, and
        # counts.merge_pending_in_open_rollup_accounting must carry the direction count.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-1", "status": "active", "text": "rollup dir"},
        ]
        per_direction_delta = [
            {
                "direction_id": "d-1",
                "direction_text": "rollup dir",
                "recent_merged_titles": [
                    {"title": "feat A", "merge_verdict": "pending_in_open_rollup"},
                    {"title": "feat B", "merge_verdict": "pending_in_open_rollup"},
                    {"title": "landed C", "merge_verdict": "verified_landed"},
                ],
                "recent_merge_verdict_counts": {
                    "verified_landed": 1,
                    "verified_not_landed": 0,
                    "unverifiable": 0,
                    "pending_in_open_rollup": 2,
                },
            },
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats, {}, {}, None, per_direction_delta
        )
        assert out is not None
        # Pending rollup items are NOT merge conflicts.
        assert out["merge_accounting"] == []
        assert out["counts"]["merge_accounting"] == 0
        assert out["counts"]["merge_not_landed"] == 0
        # They appear in the dedicated pending section.
        pending = out["pending_in_open_rollup"]
        assert len(pending) == 1
        entry = pending[0]
        assert entry["direction_id"] == "d-1"
        assert entry["pending_in_open_rollup"] == 2
        assert "feat A" in entry["pending_in_open_rollup_titles"]
        assert "feat B" in entry["pending_in_open_rollup_titles"]
        assert entry["pending_in_open_rollup_titles_overflow"] == 0
        # Aggregate counts match.
        assert out["counts"]["merge_pending_in_open_rollup"] == 2
        assert out["counts"]["merge_pending_in_open_rollup_accounting"] == 1
        # Landed count is unaffected.
        assert out["counts"]["merge_landed"] == 1

    def test_pending_in_open_rollup_only_cycle_renders_section(self):
        # A cycle whose ONLY merge signal is pending rollup work must not hit the
        # nothing-to-reconcile early return and must still emit the section.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        per_direction_delta = [
            {
                "direction_id": "d-2",
                "direction_text": "pending only dir",
                "recent_merged_titles": [
                    {"title": "feat X", "merge_verdict": "pending_in_open_rollup"},
                ],
                "recent_merge_verdict_counts": {
                    "verified_landed": 0,
                    "verified_not_landed": 0,
                    "unverifiable": 0,
                    "pending_in_open_rollup": 1,
                },
            },
        ]
        out = _build_cycle_start_reconciliation(
            [], {}, {}, None, per_direction_delta
        )
        assert out is not None, "pending-only cycle must not return None"
        assert len(out["pending_in_open_rollup"]) == 1
        assert out["counts"]["merge_pending_in_open_rollup"] == 1
        assert out["counts"]["merge_pending_in_open_rollup_accounting"] == 1

    def test_phantom_direction_refs_flagged_in_current_focus(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Advance d-4890df9e which is in flight and d-aaaabbbb also.",
            }
        }
        # Only d-deadbeef exists in direction_stats; the two cited refs are phantoms.
        direction_stats = [{"id": "d-deadbeef", "status": "active", "gap_id": None}]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update=""
        )
        assert out is not None
        phantoms = out["phantom_direction_refs"]
        phantom_ids = {p["direction_id"] for p in phantoms}
        assert "d-4890df9e" in phantom_ids
        assert "d-aaaabbbb" in phantom_ids
        assert "d-deadbeef" not in phantom_ids
        for p in phantoms:
            assert p["cited_in"] == "intent.current_focus"
        assert out["counts"]["phantom_direction_refs"] == 2

    def test_real_direction_ref_not_falsely_flagged(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Working on d-12345678 and d-abcdef01.",
            }
        }
        direction_stats = [
            {"id": "d-12345678", "status": "active", "gap_id": None},
            {"id": "d-abcdef01", "status": "retired", "gap_id": None},
        ]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update=""
        )
        # Both cited IDs exist — phantom_direction_refs must be empty.
        if out is not None:
            assert out["phantom_direction_refs"] == []
            assert out["counts"]["phantom_direction_refs"] == 0

    def test_stale_active_moves_conflict(self):
        # gap.active_moves references a direction that is no longer active → surfaced
        # as stale_active_moves. An active direction in active_moves is NOT stale.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "gaps": [
                # Stale: active_moves points at a retired direction not in active_dirs.
                {
                    "id": "g-stale",
                    "name": "stale gap",
                    "status": "active",
                    "active_moves": ["d-retired"],
                },
                # Clean: active_moves points at a live active direction.
                {
                    "id": "g-clean",
                    "name": "clean gap",
                    "status": "active",
                    "active_moves": ["d-live"],
                },
                # Closed gaps are never checked.
                {
                    "id": "g-closed",
                    "name": "closed gap",
                    "status": "closed",
                    "active_moves": ["d-retired"],
                },
            ]
        }
        direction_stats = [
            {"id": "d-live", "status": "active", "gap_id": "g-clean"},
            # d-retired is NOT in direction_stats at all (deleted/missing).
        ]
        out = _build_cycle_start_reconciliation(direction_stats, pm, {})
        assert out is not None
        stale = out["stale_active_moves"]
        assert len(stale) == 1
        assert stale[0]["gap_id"] == "g-stale"
        assert stale[0]["gap_name"] == "stale gap"
        assert stale[0]["stale_direction_ids"] == ["d-retired"]
        assert out["counts"]["stale_active_moves"] == 1
        # Clean gap is not surfaced.
        assert not any(s["gap_id"] == "g-clean" for s in stale)

    def test_qualification_prose_drift_flags_stale_sha_in_current_focus(self):
        # A stale candidate SHA embedded in current_focus is surfaced as
        # qualification_prose_drift when last_qual carries a different candidate.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Audit findings on candidate f0224ab4 are blocking.",
            }
        }
        last_qual = {"candidate_sha": "e587a4d0aabbccdd"}
        direction_stats = [{"id": "d-aaaa1111", "status": "active", "gap_id": None}]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update="", last_qual=last_qual
        )
        assert out is not None
        drift = out["qualification_prose_drift"]
        assert len(drift) == 1
        assert drift[0]["stale_sha"] == "f0224ab4"
        assert drift[0]["active_candidate_sha"] == "e587a4d0"
        assert drift[0]["cited_in"] == "intent.current_focus"
        assert out["counts"]["qualification_prose_drift"] == 1

    def test_qualification_prose_drift_no_flag_when_sha_matches_active(self):
        # When prose cites the CURRENT candidate SHA, no drift is flagged.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Candidate e587a4d0 is the active qualification target.",
            }
        }
        last_qual = {"candidate_sha": "e587a4d0aabbccdd"}
        direction_stats = [{"id": "d-aaaa1111", "status": "active", "gap_id": None}]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update="", last_qual=last_qual
        )
        assert out is not None
        assert out["qualification_prose_drift"] == []
        assert out["counts"]["qualification_prose_drift"] == 0

    def test_qualification_prose_drift_absent_without_last_qual(self):
        # Without last_qual, no SHA detection runs — the field is empty but present.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        pm = {
            "intent": {
                "current_focus": "Working on f0224ab4 and other things.",
            }
        }
        direction_stats = [{"id": "d-aaaa1111", "status": "active", "gap_id": None}]
        out = _build_cycle_start_reconciliation(
            direction_stats, pm, {}, running_update="", last_qual=None
        )
        assert out is not None
        assert out["qualification_prose_drift"] == []
        assert out["counts"]["qualification_prose_drift"] == 0

    def test_qualification_surface_conflicts_stale_candidate_with_pending_status(self):
        # A stale candidate with status="pending" (active qualification in flight)
        # should NOT raise a conflict — integration-branch drift during active
        # qualification is expected, not a surface disagreement.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "pending",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "qualifying"},
            # Production shape emitted by _resolve_candidate_currency: verdict
            # is "stale" (resolved=True, current=False) when the pinned SHA is
            # behind integration HEAD. There is no "is_stale" key.
            "last_qualification_candidate_currency": {
                "verdict": "stale",
                "resolved": True,
                "current": False,
                "candidate_sha": "abc123def456789",
                "head_sha": "9998887776665554443",
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        stale_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "stale_candidate_with_promotion_failed_status"
        ]
        # pending + stale is normal during active qualification; NOT a conflict
        assert len(stale_conflicts) == 0

    def test_qualification_surface_conflicts_stale_candidate_with_promotion_failed(self):
        # A stale candidate with status="promotion_failed" IS a genuine conflict
        # — the version should have advanced its candidate after failure.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "promotion_failed",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "promotion_failed"},
            "last_qualification_candidate_currency": {
                "verdict": "stale",
                "resolved": True,
                "current": False,
                "candidate_sha": "abc123def456789",
                "head_sha": "9998887776665554443",
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        stale_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "stale_candidate_with_promotion_failed_status"
        ]
        assert len(stale_conflicts) == 1
        assert "stale" in stale_conflicts[0]["description"].lower()
        assert "promotion_failed" in stale_conflicts[0]["description"]

    def test_qualification_surface_conflicts_status_state_mismatch(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "pending",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            # Deliberately mismatched: status="pending" but state="promoted"
            # (promoted is not a valid state for pending status)
            "qualification_state": {"state": "promoted"},
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        status_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "status_state_mismatch"
        ]
        assert len(status_conflicts) == 1
        assert "status" in status_conflicts[0]["description"].lower()
        assert "state" in status_conflicts[0]["description"].lower()

    def test_status_state_mismatch_not_flagged_for_finalized_verdict_substitution(self):
        """When qualification_state_disagreement is present the resolver already
        documented the status→state divergence (e.g. pending→promoted via a
        finalized QualificationVerdict).  _build_cycle_start_reconciliation must
        NOT flag it as a status_state_mismatch conflict — that would be a false
        positive against the resolver's own deliberate substitution.

        Acceptance evidence: named_test requirement from direction task."""
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        # Simulate what the resolver produces when a merge-test verdict with
        # terminal_status="promoted" exists while last_qualification.status is
        # still "pending" (the stale-pending window):
        #   - last_qualification.status = "pending"  (raw, unmodified)
        #   - qualification_state.state = "promoted"  (resolver-applied)
        #   - qualification_state_disagreement = {...}  (resolver note)
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "pending",
                "promotion_pending": {
                    "pr_number": 9001,
                    "pr_url": "https://github.com/x/y/pull/9001",
                },
                "confidence": 0.92,
                "threshold": 0.80,
            },
            # Resolver substituted "pending" → "promoted" via QualificationVerdict
            "qualification_state": {"state": "promoted", "summary": "Promoted."},
            # Resolver documented the substitution here:
            "qualification_state_disagreement": {
                "last_qualification_status": "pending",
                "verdict_terminal_status": "promoted",
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        status_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "status_state_mismatch"
        ]
        assert status_conflicts == [], (
            "status_state_mismatch must NOT fire when qualification_state_disagreement "
            f"is present (resolver already documented the substitution); got: {conflicts}"
        )

    def test_qualification_surface_conflicts_ignores_partial_evidence_coverage(self):
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "pending",
                # Candidate-bound qualification remains confident while monotonic
                # evidence coverage is still partial.
                "confidence": 0.85,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "qualifying"},
            # But only 2 of 5 criteria are covered
            "criterion_coverage": {
                "total": 5,
                "covered": 2,
                "partial": 1,
                "uncovered": 2,
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        coverage_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "criterion_coverage_vs_confidence"
        ]
        assert coverage_conflicts == []
        assert out["counts"]["qualification_surface_conflicts"] == 0

    def test_qualification_surface_conflicts_none_when_surfaces_agree(self):
        # A genuinely agreeing set of surfaces should produce NO conflicts
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "promoted",
                "confidence": 0.95,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "promoted"},
            "last_qualification_candidate_currency": {
                "verdict": "fresh",
                "resolved": True,
                "current": True,
                "candidate_sha": "abc123def456789",
                "head_sha": "abc123def456789",
            },
            # All criteria are covered
            "criterion_coverage": {
                "total": 5,
                "covered": 5,
                "partial": 0,
                "uncovered": 0,
            },
            "promotion_readiness": {
                "ready": True,
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        assert out["qualification_surface_conflicts"] == []
        assert out["counts"]["qualification_surface_conflicts"] == 0

    def test_qualification_surface_conflicts_promotion_readiness_unready(self):
        # promotion_readiness.ready=False while last_qualification.status=
        # "promotion_failed" should produce a promotion_readiness_unready
        # conflict when is_current_readiness=True (pre-go-live calibration mode).
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "promotion_failed",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            # State kept consistent with status so only the promotion-readiness
            # conflict fires (promotion_failed is a valid state for this status).
            "qualification_state": {"state": "promotion_failed"},
            "last_qualification_candidate_currency": {
                "verdict": "fresh",
                "resolved": True,
                "current": True,
                "candidate_sha": "abc123def456789",
                "head_sha": "abc123def456789",
            },
            "criterion_coverage": {
                "total": 5,
                "covered": 5,
                "partial": 0,
                "uncovered": 0,
            },
            "promotion_readiness": {
                "ready": False,
                # is_current_readiness=True: pre-go-live calibration regime
                # where ready=False reflects actual track-record failure.
                "is_current_readiness": True,
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        pr_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "promotion_readiness_unready"
        ]
        assert len(pr_conflicts) == 1
        assert "not ready" in pr_conflicts[0]["description"].lower()
        assert "promotion_failed" in pr_conflicts[0]["description"]

    def test_promotion_readiness_unready_suppressed_in_live_regime(self):
        # When is_current_readiness=False (live regime), promotion_readiness
        # forced ready=False by _annotate_regime is a regime relabel, not a
        # genuine track-record failure — the conflict must NOT be raised.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        version_phase_block = {
            "last_qualification": {
                "candidate_sha": "abc123def456789",
                "status": "promotion_failed",
                "confidence": 0.85,
                "threshold": 0.80,
            },
            "qualification_state": {"state": "promotion_failed"},
            "last_qualification_candidate_currency": {
                "verdict": "fresh",
                "resolved": True,
                "current": True,
                "candidate_sha": "abc123def456789",
                "head_sha": "abc123def456789",
            },
            "criterion_coverage": {
                "total": 5,
                "covered": 5,
                "partial": 0,
                "uncovered": 0,
            },
            "promotion_readiness": {
                "ready": False,
                # is_current_readiness=False: live regime, ready=False is a
                # regime relabel by _annotate_regime, not a real conflict.
                "is_current_readiness": False,
                # live_regime_calibration_artifact=True: machine-checkable signal
                # that ready=False is a regime artifact, not a real failure.
                "live_regime_calibration_artifact": True,
            },
        }
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=version_phase_block,
        )
        assert out is not None
        conflicts = out["qualification_surface_conflicts"]
        pr_conflicts = [
            c
            for c in conflicts
            if c.get("conflict_type") == "promotion_readiness_unready"
        ]
        # Live-regime forced ready=False must not surface as a conflict
        assert len(pr_conflicts) == 0
        # The promotion_readiness block carries the machine-checkable artifact flag.
        pr_block = version_phase_block["promotion_readiness"]
        assert pr_block.get("live_regime_calibration_artifact") is True

    def test_qualification_surface_conflicts_absent_without_version_phase_block(self):
        # When version_phase_block is None (no LTS split), the check simply
        # doesn't run, so the field is empty even if surfaces would disagree.
        from app.strategic_spawning import _build_cycle_start_reconciliation

        direction_stats = [
            {"id": "d-working", "status": "active", "gap_id": "g-1"}
        ]
        pm = {"gaps": [{"id": "g-1", "name": "gap1", "status": "active"}]}
        out = _build_cycle_start_reconciliation(
            direction_stats,
            pm,
            {},
            version_phase_block=None,
        )
        assert out is not None
        assert out["qualification_surface_conflicts"] == []
        assert out["counts"]["qualification_surface_conflicts"] == 0


# ── _build_prune_candidates: concrete cleanup list when the model is heavy ─────


class TestBuildPruneCandidates:
    """Concrete prune candidates are surfaced ONLY when the model is large or
    oversized, drawn from signals the backend already has. Pure function."""

    _LARGE = {"model_size_status": "large"}

    def test_returns_none_when_model_not_heavy(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g1",
                    "name": "x",
                    "status": "active",
                    "active_moves": ["d-retired"],
                },
            ],
        }
        directions = [{"id": "d-retired", "status": "retired"}]
        for status in (
            None,
            {},
            {"model_size_status": "healthy"},
            {"model_size_status": "growing"},
        ):
            assert (
                _build_prune_candidates(
                    pm, directions, status, "2026-06-04T00:00:00+00:00"
                )
                is None
            )

    def test_returns_none_when_no_candidates_even_if_heavy(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                # Active gap worked by a still-active, never-merged direction.
                {
                    "id": "g1",
                    "name": "x",
                    "status": "active",
                    "active_moves": ["d-live"],
                },
            ],
            "capabilities": [
                {
                    "name": "ok",
                    "status": "working",
                    "verification": {"effective_status": "working"},
                },
            ],
        }
        directions = [{"id": "d-live", "status": "active", "research_history": []}]
        assert (
            _build_prune_candidates(
                pm, directions, self._LARGE, "2026-06-04T00:00:00+00:00"
            )
            is None
        )

    def test_surfaces_gap_linked_to_retired_direction(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g-ret",
                    "name": "retired work",
                    "status": "active",
                    "active_moves": ["d-ret"],
                },
            ],
        }
        directions = [{"id": "d-ret", "status": "retired"}]
        out = _build_prune_candidates(
            pm,
            directions,
            {"model_size_status": "oversized"},
            "2026-06-04T00:00:00+00:00",
        )
        assert out is not None
        assert out["model_size_status"] == "oversized"
        assert len(out["resolved_gaps"]) == 1
        rg = out["resolved_gaps"][0]
        assert rg["gap_id"] == "g-ret"
        assert rg["reason"] == "direction_retired"
        assert rg["directions"] == ["d-ret"]
        assert out["counts"]["resolved_gaps"] == 1

    def test_surfaces_gap_linked_to_shipped_direction(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g-ship",
                    "name": "shipped work",
                    "status": "active",
                    "active_moves": ["d-ship"],
                },
            ],
        }
        # Still active, but has merged PRs in its history → shipped.
        directions = [
            {
                "id": "d-ship",
                "status": "active",
                "research_history": [{"prs_merged": 2}],
            },
        ]
        out = _build_prune_candidates(
            pm, directions, self._LARGE, "2026-06-04T00:00:00+00:00"
        )
        assert len(out["resolved_gaps"]) == 1
        assert out["resolved_gaps"][0]["reason"] == "direction_shipped"

    def test_verified_merge_ships_gap_but_degraded_does_not(self):
        """Gap retirement is a ground-truth decision: only a verified merge
        retires the gap; a direction whose only merges are unverified_degraded
        (verified_prs_merged == 0) must NOT mark the gap direction_shipped."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g-deg",
                    "name": "degraded only",
                    "status": "active",
                    "active_moves": ["d-deg"],
                },
                {
                    "id": "g-ver",
                    "name": "verified",
                    "status": "active",
                    "active_moves": ["d-ver"],
                },
            ],
        }
        directions = [
            # Raw prs_merged>0 but the verification-aware count is 0 → not shipped.
            {
                "id": "d-deg",
                "status": "active",
                "research_history": [{"prs_merged": 3, "verified_prs_merged": 0}],
            },
            # A genuine verified merge → still ships the gap.
            {
                "id": "d-ver",
                "status": "active",
                "research_history": [{"prs_merged": 1, "verified_prs_merged": 1}],
            },
        ]
        out = _build_prune_candidates(
            pm, directions, self._LARGE, "2026-06-04T00:00:00+00:00"
        )
        resolved = {rg["gap_id"]: rg["reason"] for rg in out["resolved_gaps"]}
        assert resolved == {"g-ver": "direction_shipped"}
        assert "g-deg" not in resolved

    def test_surfaces_claimed_but_absent_capabilities(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {
                    "name": "ghost",
                    "status": "working",
                    "verification": {"effective_status": "absent"},
                },
                # partial is NOT absent → not a prune candidate here.
                {
                    "name": "partial-cap",
                    "status": "working",
                    "verification": {"effective_status": "partial"},
                },
                {
                    "name": "unverified-cap",
                    "status": "working",
                    "verification": {"effective_status": "unverified"},
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert [c["name"] for c in out["absent_capabilities"]] == ["ghost"]
        assert out["absent_capabilities"][0]["stored_status"] == "working"
        assert out["counts"]["absent_capabilities"] == 1

    def test_surfaces_expired_hold_deferrals(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                # Hold elapsed.
                {
                    "id": "g-exp",
                    "name": "expired hold",
                    "status": "active",
                    "deferral": {
                        "rationale": "later",
                        "created_at": "x",
                        "hold_until": "2026-01-01T00:00:00+00:00",
                    },
                },
                # Hold still in the future → NOT a candidate.
                {
                    "id": "g-fut",
                    "name": "future hold",
                    "status": "active",
                    "deferral": {
                        "rationale": "later",
                        "created_at": "x",
                        "hold_until": "2027-01-01T00:00:00+00:00",
                    },
                },
                # Deferral with no hold_until → NOT a candidate.
                {
                    "id": "g-noh",
                    "name": "no hold",
                    "status": "active",
                    "deferral": {"rationale": "later", "created_at": "x"},
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert [g["gap_id"] for g in out["expired_holds"]] == ["g-exp"]
        assert out["expired_holds"][0]["hold_until"] == "2026-01-01T00:00:00+00:00"

    def test_tolerates_malformed_hold_until(self):
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "gaps": [
                {
                    "id": "g-bad",
                    "name": "bad ts",
                    "status": "active",
                    "deferral": {
                        "rationale": "x",
                        "created_at": "x",
                        "hold_until": "not-a-timestamp",
                    },
                },
                {
                    "id": "g-num",
                    "name": "num ts",
                    "status": "active",
                    "deferral": {
                        "rationale": "x",
                        "created_at": "x",
                        "hold_until": 12345,
                    },
                },
            ],
        }
        # Malformed hold_until is ignored — no candidates, so None.
        assert (
            _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
            is None
        )

    def test_respects_cap_and_carries_true_totals(self):
        from app.strategic_spawning import _build_prune_candidates, _RECON_LIST_CAP

        gaps = [
            {
                "id": f"g-{i}",
                "name": f"resolved {i}",
                "status": "active",
                "active_moves": ["d-ret"],
            }
            for i in range(_RECON_LIST_CAP + 4)
        ]
        directions = [{"id": "d-ret", "status": "retired"}]
        out = _build_prune_candidates(
            {"gaps": gaps},
            directions,
            self._LARGE,
            "2026-06-04T00:00:00+00:00",
        )
        assert len(out["resolved_gaps"]) == _RECON_LIST_CAP
        assert out["counts"]["resolved_gaps"] == _RECON_LIST_CAP + 4

    def test_surfaces_ungroundable_capabilities(self):
        """Capabilities with no backing_paths, no source, no verification appear
        in ungroundable_capabilities; a capability with backing_paths is excluded."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                # No backing_paths, no source, no verification → ungroundable.
                {"name": "ungrounded-cap", "status": "planned"},
                # Has backing_paths → NOT ungroundable.
                {
                    "name": "grounded-cap",
                    "status": "working",
                    "backing_paths": ["backend/app/foo.py"],
                },
                # Has source → NOT ungroundable (even without backing_paths).
                {"name": "sourced-cap", "status": "working", "source": "documentation"},
                # Has verification → NOT ungroundable.
                {
                    "name": "verified-cap",
                    "status": "working",
                    "verification": {"effective_status": "working"},
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        names = [c["name"] for c in out["ungroundable_capabilities"]]
        assert names == ["ungrounded-cap"]
        assert out["counts"]["ungroundable_capabilities"] == 1

    def test_prune_candidates_names_ungrounded_capabilities(self):
        """Acceptance gate: ungroundable caps surface and counts are correct."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {"name": "no-ground-cap", "status": "working"},
                {"name": "another-no-ground", "status": "planned"},
                # excluded
                {
                    "name": "grounded",
                    "status": "working",
                    "backing_paths": ["path/to/file.py"],
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        names = [c["name"] for c in out["ungroundable_capabilities"]]
        assert "no-ground-cap" in names
        assert "another-no-ground" in names
        assert "grounded" not in names
        assert out["counts"]["ungroundable_capabilities"] == 2

    def test_does_not_surface_cap_with_source_as_ungroundable(self):
        """A capability with a source field is NOT ungroundable even without
        backing_paths or verification."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {
                    "name": "cap-with-source",
                    "status": "working",
                    "source": "design_doc",
                },
            ],
        }
        # source is present → not in ungroundable_capabilities → only candidate class
        # available is none → returns None.
        assert (
            _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
            is None
        )

    def test_domain_merge_groups_keyword_bucketing(self):
        """Capabilities sharing a keyword bucket form a merge group."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {"name": "strategic-planning-cap", "status": "working"},
                {"name": "strategic-review-cap", "status": "working"},
                {"name": "strategic-spawning-cap", "status": "working"},
                # different bucket — no match
                {"name": "compute-scheduler", "status": "working"},
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        groups = {g["bucket"]: g["names"] for g in out["domain_merge_groups"]}
        assert "strategic" in groups
        assert len(groups["strategic"]) == 3
        # compute bucket: only one entry → no group
        assert "compute" not in groups

    def test_domain_merge_groups_no_groups_when_singletons(self):
        """When each keyword bucket has at most one capability, no merge groups are emitted."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                {"name": "strategic-review-cap", "status": "working"},
                {"name": "task-runner", "status": "working"},
                {"name": "operator-admin", "status": "working"},
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        # Each bucket has exactly one entry → no domain_merge_groups.
        # But long_names/ungroundable/heavy may fire — check domain_merge_groups specifically.
        if out is not None:
            assert out["domain_merge_groups"] == []
            assert out["counts"]["domain_merge_groups"] == 0

    def test_heavy_gaps_ranked_by_history_size(self):
        """Active gaps are ranked largest-first by serialized history weight."""
        from app.strategic_spawning import _build_prune_candidates

        def _history(n: int) -> list:
            return [
                {"event": f"entry-{i}", "ts": f"2026-0{i + 1}-01"} for i in range(n)
            ]

        pm = {
            "gaps": [
                {
                    "id": "g-small",
                    "name": "small gap",
                    "status": "active",
                    "history": _history(2),
                },
                {
                    "id": "g-large",
                    "name": "large gap",
                    "status": "active",
                    "history": _history(10),
                },
                {
                    "id": "g-medium",
                    "name": "medium gap",
                    "status": "active",
                    "history": _history(5),
                },
                # archived gap — not included in heavy_gaps
                {
                    "id": "g-arch",
                    "name": "archived gap",
                    "status": "archived",
                    "history": _history(20),
                },
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        ids = [g["gap_id"] for g in out["heavy_gaps"]]
        assert ids[0] == "g-large"
        assert ids[1] == "g-medium"
        assert ids[2] == "g-small"
        assert "g-arch" not in ids
        assert out["counts"]["heavy_gaps"] == 3

    def test_long_names_capabilities_and_gaps(self):
        """Names exceeding 80 chars appear in long_names; shorter names are excluded."""
        from app.strategic_spawning import _build_prune_candidates

        long_cap = "a" * 81
        long_gap = "b" * 90
        short_cap = "short-cap"
        short_gap = "short-gap"

        pm = {
            "capabilities": [
                {"name": long_cap, "status": "working"},
                {"name": short_cap, "status": "working"},
            ],
            "gaps": [
                {"id": "g-long", "name": long_gap, "status": "active"},
                {"id": "g-short", "name": short_gap, "status": "active"},
            ],
        }
        out = _build_prune_candidates(pm, [], self._LARGE, "2026-06-04T00:00:00+00:00")
        assert out is not None
        kinds_and_names = [(e["kind"], e["name"]) for e in out["long_names"]]
        long_entries = {name for _, name in kinds_and_names}
        assert long_cap[:120] in long_entries
        assert long_gap[:120] in long_entries
        assert short_cap not in long_entries
        assert short_gap not in long_entries
        assert out["counts"]["long_names"] == 2
        for e in out["long_names"]:
            assert e["length"] > 80

    def test_counts_includes_new_candidate_classes(self):
        """counts dict carries totals for all 7 candidate classes."""
        from app.strategic_spawning import _build_prune_candidates

        pm = {
            "capabilities": [
                # ungroundable
                {"name": "no-ground", "status": "planned"},
                # absent
                {
                    "name": "ghost",
                    "status": "working",
                    "verification": {"effective_status": "absent"},
                },
            ],
            "gaps": [
                # resolved
                {
                    "id": "g1",
                    "name": "g1",
                    "status": "active",
                    "active_moves": ["d-ret"],
                },
            ],
        }
        directions = [{"id": "d-ret", "status": "retired"}]
        out = _build_prune_candidates(
            pm, directions, self._LARGE, "2026-06-04T00:00:00+00:00"
        )
        assert out is not None
        for key in (
            "resolved_gaps",
            "absent_capabilities",
            "expired_holds",
            "ungroundable_capabilities",
            "domain_merge_groups",
            "heavy_gaps",
            "long_names",
        ):
            assert key in out["counts"], f"counts missing key: {key}"


# ── Onboarding goal reaches the first strategic cycle's payload ─────────────────
#
# The New Project onboarding flow seeds the user's goal into BOTH channels the
# strategic agent's bootstrap reads on cycle 1: problem_model.intent (purpose +
# current_focus) and a from_type='user' ProjectMessage that lands in the
# conversation thread. These tests lock in that the seeded intent and message
# actually reach the spawned strategic-cycle payloads, and that an empty goal
# leaves today's blank-slate behavior intact.

_ONBOARD_GOAL = "Ship a self-serve billing portal for small teams"


def _seeded_problem_model(goal: str) -> dict:
    """Build a problem_model dict shaped exactly like onboarding's seed.

    Mirrors admin_onboard._seed_strategic_intent: purpose + current_focus set
    to the goal with a user_message provenance source, validated through the
    real model so the test exercises the same payload the agent receives.
    """
    from app.problem_model import parse_problem_model, Source

    pm = parse_problem_model(None)
    pm.intent.purpose = goal
    pm.intent.current_focus = goal
    pm.intent.source = Source(type="user_message", excerpt=goal[:120])
    pm.last_updated_by = "user"
    return pm.model_dump()


@pytest.mark.asyncio
async def test_seeded_goal_reaches_strategic_review_payload(db_session):
    """Goal seeded at onboarding is visible to the first strategic_review cycle.

    Asserts both bootstrap channels carry it: problem_model.intent.purpose and
    the from_type='user' message in conversation_thread + open_messages.
    """
    project = _make_project(
        vision={},  # blank-slate vision, as a freshly-onboarded project has
        problem_model=_seeded_problem_model(_ONBOARD_GOAL),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMessage(project_id=project.id, from_type="user", content=_ONBOARD_GOAL)
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # Channel 1: intent/purpose
    assert payload["problem_model"]["intent"]["purpose"] == _ONBOARD_GOAL
    assert payload["effective_purpose"] == _ONBOARD_GOAL
    # Channel 2: user-message conversation history
    thread_contents = [m["content"] for m in payload["conversation_thread"]]
    assert _ONBOARD_GOAL in thread_contents
    open_contents = [m["content"] for m in payload["open_messages"]]
    assert _ONBOARD_GOAL in open_contents


@pytest.mark.asyncio
async def test_seeded_goal_reaches_design_doc_update_payload(db_session):
    """Goal seeded at onboarding is visible to the Phase-1 design_doc_update cycle."""
    project = _make_project(
        vision={},
        problem_model=_seeded_problem_model(_ONBOARD_GOAL),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(
        ProjectMessage(project_id=project.id, from_type="user", content=_ONBOARD_GOAL)
    )
    await db_session.commit()

    spawned = await _spawn_design_doc_update(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    assert payload["problem_model"]["intent"]["purpose"] == _ONBOARD_GOAL
    assert payload["effective_purpose"] == _ONBOARD_GOAL
    open_contents = [m["content"] for m in payload["open_messages"]]
    assert _ONBOARD_GOAL in open_contents


@pytest.mark.asyncio
async def test_operator_context_refs_promoted_to_strategic_review_payload(db_session):
    """Selected Strategic workbench refs become first-class pointer context."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    msg = ProjectMessage(
        project_id=project.id,
        from_type="user",
        message_type=MessageType.FEEDBACK,
        content="Please review the release blocker with the linked memo.",
        created_at=datetime(2026, 6, 3, 1, 0, 0),
        context={
            "strategic_session": {
                "version": 1,
                "interaction_mode": "continuous",
                "active_surface": "pipeline",
                "session_started_at": "2026-06-29T12:00:00Z",
                "captured_at": "2026-06-29T12:05:00Z",
                "context_package_id": "strategic-context:test",
                "selected_refs": [
                    {"kind": "task", "id": "task-1", "label": "Release blocker"},
                    {"kind": "task", "id": "task-1", "label": "Duplicate"},
                    {
                        "kind": "knowledge_document",
                        "id": "knowledge:doc-1",
                        "label": "Prior memo",
                    },
                ],
                "selected_ref_descriptors": [
                    {
                        "kind": "task",
                        "id": "task-1",
                        "label": "Release blocker task",
                        "summary": "Small task descriptor",
                        "status": "blocked",
                        "target": {
                            "surface": "task",
                            "task_id": "task-1",
                            "logs": "must not be copied",
                        },
                    }
                ],
                "canvas_body": "must not be copied",
            }
        },
    )
    db_session.add(msg)
    await db_session.commit()
    await db_session.refresh(msg)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    entries = payload["operator_context_refs"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["source_message_id"] == str(msg.id)
    assert entry["context_package_id"] == "strategic-context:test"
    assert entry["session_started_at"] == "2026-06-29T12:00:00Z"
    assert entry["captured_at"] == "2026-06-29T12:05:00Z"

    selected_refs = entry["selected_refs"]
    assert selected_refs[:2] == [
        {
            "kind": "task",
            "id": "task-1",
            "label": "Release blocker",
            "descriptor": {
                "kind": "task",
                "id": "task-1",
                "label": "Release blocker task",
                "summary": "Small task descriptor",
                "status": "blocked",
                "target": {"surface": "task", "task_id": "task-1"},
            },
        },
        {
            "kind": "knowledge_document",
            "id": "knowledge:doc-1",
            "label": "Prior memo",
        },
    ]
    serialized = json.dumps(entries)
    assert "canvas_body" not in serialized
    assert "must not be copied" not in serialized
    assert "logs" not in serialized


@pytest.mark.asyncio
async def test_operator_context_refs_ignore_malformed_and_cap(db_session):
    """Malformed snapshots are skipped and promoted refs stay tightly bounded."""
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="agent",
            message_type=MessageType.NOTE,
            content="Agent-selected context should not be treated as operator context.",
            created_at=datetime(2026, 6, 3, 3, 0, 0),
            context={
                "strategic_session": {
                    "context_package_id": "strategic-context:agent",
                    "selected_refs": [{"kind": "task", "id": "agent-only"}],
                }
            },
        )
    )
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.NOTE,
            content="Malformed context is ignored.",
            created_at=datetime(2026, 6, 3, 2, 0, 0),
            context={"strategic_session": "not-a-snapshot"},
        )
    )
    refs = [
        {"kind": "task", "id": "task-with-large-label", "label": "L" * 2000},
        {"kind": "task", "id": "x" * 1000, "label": "oversized id"},
        {"kind": "", "id": "missing-kind"},
        "not-a-ref",
        *[
            {"kind": "task", "id": f"task-{i}", "label": f"Task {i}"}
            for i in range(_OPERATOR_CONTEXT_REFS_KEEP + 3)
        ],
    ]
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="user",
            message_type=MessageType.FEEDBACK,
            content="Use the selected bounded context.",
            created_at=datetime(2026, 6, 3, 1, 0, 0),
            context={
                "strategic_session": {
                    "context_package_id": "strategic-context:bounded",
                    "selected_refs": refs,
                }
            },
        )
    )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]
    entries = payload["operator_context_refs"]

    assert len(entries) == 1
    selected_refs = entries[0]["selected_refs"]
    assert len(selected_refs) == _OPERATOR_CONTEXT_REFS_KEEP
    assert selected_refs[0] == {"kind": "task", "id": "task-with-large-label"}
    ref_ids = [ref["id"] for ref in selected_refs]
    assert "agent-only" not in ref_ids
    assert "x" * 1000 not in ref_ids
    assert ref_ids[-1] == f"task-{_OPERATOR_CONTEXT_REFS_KEEP - 2}"


@pytest.mark.asyncio
async def test_empty_goal_leaves_strategic_payload_blank(db_session):
    """A skipped goal preserves today's blank-slate behavior — no seeded
    purpose and no seeded user message reach the strategic payload."""
    project = _make_project(vision={}, problem_model={})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # No purpose was seeded (empty/absent problem_model intent).
    assert not (payload.get("problem_model") or {}).get("intent", {}).get("purpose")
    assert payload["conversation_thread"] == []
    assert payload["open_messages"] == []


@pytest.mark.asyncio
async def test_open_bug_report_survives_thread_cap_in_strategic_review(db_session):
    """An OPEN BUG_REPORT created before 15+ newer messages must still appear in
    the strategic_review open_messages inbox — not only in the architecture cycle.

    Regression guard for the thread-slice behavior: previously open_messages was
    derived by filtering the conversation_thread (capped at the 15 newest rows),
    so an old open BUG_REPORT aged out of the strategist's view entirely and
    could never be resolved.
    """
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    # Oldest row: an OPEN BUG_REPORT the strategist must keep seeing.
    bug_text = "Bug: strategist never sees this aged-out open report"
    db_session.add(
        ProjectMessage(
            project_id=project.id,
            from_type="agent",
            message_type=MessageType.BUG_REPORT,
            content=bug_text,
            status="open",
            created_at=datetime(2026, 5, 1, 0, 0, 0),
        )
    )
    # 30 newer messages — well beyond the 15-row conversation_thread cap.
    for i in range(30):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                from_type="user",
                message_type=MessageType.NOTE,
                content=f"newer chatter #{i}",
                status="open",
                created_at=datetime(2026, 6, 1, 0, 0, i),
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # The aged-out bug report is gone from the recency-capped thread...
    thread_contents = [m["content"] for m in payload["conversation_thread"]]
    assert bug_text not in thread_contents
    assert len(payload["conversation_thread"]) == 15
    # ...but the authoritative open-message inbox still surfaces it.
    open_contents = [m["content"] for m in payload["open_messages"]]
    assert bug_text in open_contents


@pytest.mark.asyncio
async def test_open_messages_not_derived_from_capped_thread(db_session):
    """The open-message inbox can exceed the 15-row conversation_thread cap.

    Proves open_messages is its own query, not a byproduct of the capped thread.
    """
    project = _make_project(vision={"purpose": "test purpose"})
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    for i in range(25):
        db_session.add(
            ProjectMessage(
                project_id=project.id,
                from_type="user",
                message_type=MessageType.NOTE,
                content=f"open message #{i}",
                status="open",
                created_at=datetime(2026, 6, 1, 0, 0, i),
            )
        )
    await db_session.commit()

    spawned = await _spawn_strategic_review(project, db_session)
    assert spawned is True

    result = await db_session.execute(_select_agent_tasks(project.id))
    payload = result.scalars().all()[0].payloads[0]

    # All 25 open messages surface, far more than the thread cap of 15.
    assert len(payload["open_messages"]) == 25
    assert len(payload["conversation_thread"]) == 15


def test_summarize_open_messages_preserves_critical_and_oldest():
    """The budget-pressure fallback keeps BUG_REPORT/BLOCKER + oldest-open items
    and trims the rest, instead of dropping the whole inbox."""
    from app.strategic_spawning import (
        _summarize_open_messages,
        _OPEN_MESSAGES_KEEP_WHEN_TRIMMED,
        _OPEN_MESSAGE_EXCERPT_MAX_CHARS,
    )

    messages = []
    # One old critical BUG_REPORT (oldest) and one BLOCKER.
    messages.append(
        {
            "id": "bug",
            "type": "BUG_REPORT",
            "content": "x" * 5000,
            "created_at": "2026-05-01T00:00:00",
        }
    )
    messages.append(
        {
            "id": "blk",
            "type": "BLOCKER",
            "content": "blocked",
            "created_at": "2026-05-02T00:00:00",
        }
    )
    # Many newer NOTEs that should be trimmed away first.
    for i in range(50):
        messages.append(
            {
                "id": f"note{i}",
                "type": "NOTE",
                "content": f"note {i}",
                "created_at": f"2026-06-01T00:00:{i:02d}",
            }
        )

    trimmed = _summarize_open_messages(messages)

    assert len(trimmed) == _OPEN_MESSAGES_KEEP_WHEN_TRIMMED
    ids = {m["id"] for m in trimmed}
    # Critical items survive trimming.
    assert "bug" in ids
    assert "blk" in ids
    # Per-message excerpt is capped.
    bug = next(m for m in trimmed if m["id"] == "bug")
    assert len(bug["content"]) == _OPEN_MESSAGE_EXCERPT_MAX_CHARS


def test_reproducing_bug_survives_over_older_stale_one():
    """A reproducing bug report ranks higher than an older stale one.

    When the inbox must be trimmed under budget pressure, reproducing bug
    reports (citation_state != "absent") should rank higher in survival priority
    than stale ones (citation_state == "absent"). This ensures fresh, actionable
    bugs are preserved while stale ones (likely already fixed/refactored away)
    are deprioritized.
    """
    from app.strategic_spawning import _open_message_survival_key, _bound_open_messages

    messages = []
    # Old STALE bug report (citation_state == "absent")
    messages.append(
        {
            "id": "old_stale",
            "type": "BUG_REPORT",
            "content": "This bug is stale (cited code is absent)",
            "created_at": "2026-05-01T00:00:00",
            "citation_state": "absent",
        }
    )
    # Newer REPRODUCING bug report (no citation_state = not stale)
    messages.append(
        {
            "id": "new_reproducing",
            "type": "BUG_REPORT",
            "content": "This bug is fresh and reproducing",
            "created_at": "2026-06-01T00:00:00",
        }
    )

    # Verify the reproducing bug ranks higher (lower tuple value) than the stale one.
    stale_key = _open_message_survival_key(messages[0])
    reproducing_key = _open_message_survival_key(messages[1])

    # Reproducing should have tier=0, stale should have tier=1.
    assert reproducing_key[0] == 0  # tier 0: reproducing critical
    assert stale_key[0] == 1  # tier 1: stale critical
    # So reproducing_key < stale_key, meaning reproducing survives first.
    assert reproducing_key < stale_key

    # When both fit in the budget, both survive.
    trimmed = _bound_open_messages(messages, keep=2)
    ids = {m["id"] for m in trimmed}
    assert "old_stale" in ids
    assert "new_reproducing" in ids

    # When we must trim to 1 item, the reproducing bug survives.
    trimmed_strict = _bound_open_messages(messages, keep=1)
    assert len(trimmed_strict) == 1
    assert trimmed_strict[0]["id"] == "new_reproducing"


def test_open_message_citation_summary_promotes_absent_state_with_refs():
    """A fully absent citation signal is promoted with its broken/ungrounded refs."""
    from app.strategic_spawning import _open_message_citation_summary

    context = {
        "_provenance_warnings": {
            "citation_state": "absent",
            "broken_refs": ["backend/app/x.py"],
            "ungrounded_refs": ["backend/app/y.py:foo"],
        }
    }
    summary = _open_message_citation_summary(context)
    assert summary["citation_state"] == "absent"
    assert summary["broken_refs"] == ["backend/app/x.py"]
    assert summary["ungrounded_refs"] == ["backend/app/y.py:foo"]


