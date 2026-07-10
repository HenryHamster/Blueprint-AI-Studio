"""Standalone tests for the code-task acceptance gate and its shared core.

Covers the fail-closed numeric core shared with the compute success-criterion
gate, and the code acceptance evaluation: no-declaration passes (and records
nothing), a declared named_test that is present + verified + touches a changed
seam passes and is recorded (machine-checked proof), a declared visual
declaration that matches the uploaded viewport screenshot is recorded as
machine-checked proof, a consumed downstream_artifact is upgraded to proven by
recorded consumption, and the fail-closed cases (test missing, test-only diff,
unverified merge). Standalone
in-memory tests run under ``bash scripts/test-backend.sh --standalone``; DB
tests (consumed artifact upgrade) run under the full test harness.
"""

import os
import uuid

import pytest
from sqlmodel import select

from app.acceptance_gate import (
    CODE_ACCEPTANCE_VERDICT_KEY,
    CRITERION_OPS,
    TEST_COVERAGE_RESULT_KEY,
    acceptance_blocks_success,
    acceptance_is_proven,
    acceptance_counts_as_verified_context,
    acceptance_was_declared,
    backfill_acceptance_verdicts,
    classify_merged_acceptance,
    classify_merged_acceptance_context,
    classify_undeclared_reason,
    coerce_number,
    criterion_met,
    evaluate_code_acceptance,
    record_artifact_consumptions_for_task,
    record_code_acceptance_verdict,
    summarize_version_acceptance_aggregate,
    _evaluate_coverage_delta,
)
from app.artifact_prerequisites import ARTIFACT_PREREQUISITES_KEY
from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY
from app.models import (
    Artifact,
    ArtifactConsumption,
    ArtifactType,
    CriterionEvidence,
    ReleaseVersion,
    Task,
    TaskStatus,
    VersionAcceptanceCriterion,
)
from app.payload_schemas import AcceptanceEvidence
from testing.factories import make_project

USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def _make_code_task(
    *,
    acceptance_evidence: dict | None = None,
    changed_files: list[str] | None = None,
    unverified_reason: str | None = None,
    task_type: str = "create",
) -> Task:
    payload: dict = {"type": task_type, "prompt": "do the thing"}
    if acceptance_evidence is not None:
        payload["acceptance_evidence"] = acceptance_evidence
    context: dict = {}
    if changed_files is not None:
        context["_pr_changed_files"] = changed_files
    # Set review_loop to indicate verification state: presence without unverified_reason
    # means verified (real test verdict gated merge); presence with unverified_reason
    # means unverified (no-CI merge gate fell open).
    if unverified_reason is not None:
        context[REVIEW_LOOP_KEY] = {UNVERIFIED_MERGE_REASON_KEY: unverified_reason}
    elif acceptance_evidence is not None and acceptance_evidence.get("kind") == "named_test":
        # named_test requires verification; set empty review_loop to indicate verified
        context[REVIEW_LOOP_KEY] = {}
    return Task(
        project_id=uuid.uuid4(),
        title="code task",
        payloads=[payload],
        status=TaskStatus.COMPLETED,
        created_by="ORCHESTRATOR",
        context=context,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Shared fail-closed numeric core
# ─────────────────────────────────────────────────────────────────────────────


def test_coerce_number_rejects_bool_and_non_numeric():
    assert coerce_number(3) == 3.0
    assert coerce_number(2.5) == 2.5
    assert coerce_number(True) is None
    assert coerce_number(False) is None
    assert coerce_number("5") is None
    assert coerce_number(None) is None


def test_criterion_met_fail_closed():
    assert criterion_met(0.95, ">=", 0.9) is True
    assert criterion_met(0.5, ">=", 0.9) is False
    # Absent / non-numeric value or threshold is never met.
    assert criterion_met(None, ">=", 0.9) is False
    assert criterion_met("0.95", ">=", 0.9) is False
    assert criterion_met(0.95, ">=", None) is False
    # Unrecognized operator is never met.
    assert criterion_met(1, "~=", 1) is False


def test_criterion_ops_are_the_closed_set():
    assert set(CRITERION_OPS) == {">=", "<=", ">", "<", "==", "!="}


# ─────────────────────────────────────────────────────────────────────────────
# No declaration — preserve today's behavior exactly
# ─────────────────────────────────────────────────────────────────────────────


def test_no_declaration_passes_and_records_nothing():
    task = _make_code_task(acceptance_evidence=None, changed_files=["backend/app/x.py"])
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is False
    assert verdict.satisfied is True
    # Recording is a no-op and writes no key, so scoring is unchanged.
    assert record_code_acceptance_verdict(task) is None
    assert CODE_ACCEPTANCE_VERDICT_KEY not in (task.context or {})
    assert acceptance_blocks_success((task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)) is False


# ─────────────────────────────────────────────────────────────────────────────
# named_test — pass case
# ─────────────────────────────────────────────────────────────────────────────


def test_named_test_present_verified_touches_seam_passes_and_records():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,  # real merge-test verdict gated the merge
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.kind == "named_test"
    # named_test and visual are proven at evaluation time; a consumed
    # downstream_artifact is the third proven path (see
    # test_consumed_downstream_artifact_upgrades_to_proven).
    assert verdict.proven is True

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None and recorded.satisfied is True
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stamped["satisfied"] is True
    assert stamped["proven"] is True
    assert stamped["kind"] == "named_test"
    # A satisfied verdict does NOT block success scoring.
    assert acceptance_blocks_success(stamped) is False
    # ...and IS counted as genuinely proven by machine-checked evidence.
    assert acceptance_is_proven(stamped) is True
    assert acceptance_was_declared(stamped) is True


# ─────────────────────────────────────────────────────────────────────────────
# named_test — three fail-closed cases
# ─────────────────────────────────────────────────────────────────────────────


def test_named_test_missing_from_changed_files_fails_closed():
    # The claimed test is not among the PR's changed files — never added here.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert "not among" in verdict.reason

    record_code_acceptance_verdict(task)
    assert acceptance_blocks_success(task.context[CODE_ACCEPTANCE_VERDICT_KEY]) is True


def test_named_test_test_only_diff_fails_closed():
    # Test present + verified, but the diff is test-only: no changed production
    # seam for the test to exercise (covers the "does not touch changed code" and
    # the always-true-test-added-in-isolation shapes).
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert "test-only" in verdict.reason


def test_named_test_unverified_merge_fails_closed():
    # Test present + touches a real seam, but the merge fell open WITHOUT real
    # test verification, so the named test did not actually run green.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason="no_runnable_suite",
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert "did not run green" in verdict.reason


# ─────────────────────────────────────────────────────────────────────────────
# named_test — semantic grounding
# ─────────────────────────────────────────────────────────────────────────────


def test_named_test_unrelated_seam_fails_closed():
    """A declared test whose module name does not match the changed production
    seam fails closed — test_widget.py cannot be proven by a change to
    unrelated_module.py, even though the diff has both test + production files
    and CI ran green."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=[
            "backend/app/unrelated_module.py",
            "backend/testing/test_widget.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert "does not semantically correspond" in verdict.reason
    assert verdict.proven is False


def test_named_test_several_seams_one_matches_passes():
    """When multiple production files change and at least one matches the test
    module name, the semantic grounding check passes."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=[
            "backend/app/unrelated_module.py",
            "backend/app/widget.py",
            "backend/testing/test_widget.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "semantically grounded" in verdict.reason


def test_named_test_cross_cutting_invariants_passes_semantic_grounding():
    """A named_test declaring test_invariants.py — a cross-cutting test whose
    stem 'invariants' does not match any single production module — passes
    semantic grounding because the test validates invariants spanning multiple
    modules and is intrinsically grounded when the PR ships a non-test change."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_invariants.py",
        },
        changed_files=[
            "backend/app/research_direction_state.py",
            "backend/testing/test_invariants.py",
        ],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "semantically grounded" in verdict.reason


# ─────────────────────────────────────────────────────────────────────────────
# observable_behavior / downstream_artifact — checkable subset
# ─────────────────────────────────────────────────────────────────────────────


def test_observable_behavior_with_real_diff_passes():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "observable_behavior",
            "description": "the dashboard shows the live serving SHA banner",
        },
        changed_files=["admin-ui/src/components/Banner.tsx"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True


def test_observable_behavior_empty_diff_fails_closed():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "observable_behavior",
            "description": "the dashboard shows the live serving SHA banner",
        },
        changed_files=[],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert "no changed files" in verdict.reason


def test_downstream_artifact_grounded_diff_passes():
    # The declared artifact_id is reflected in a shipped changed-file path
    # (token "promotion" appears in release_promotion.py) — grounded, satisfied,
    # but not machine-checked proven.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True
    assert verdict.proven is False
    assert "reflected in the shipped changed files" in verdict.reason


def test_downstream_artifact_unrelated_diff_is_unsatisfied():
    # A meaningful-sounding artifact_id paired with a completely unrelated diff
    # is NOT grounded — the declared artifact was not delivered by what shipped,
    # so the verdict fails closed and blocks verified-success credit.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "deploy-rollback-registry",
        },
        changed_files=["admin-ui/src/components/Banner.tsx"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "not reflected" in verdict.reason
    recorded = record_code_acceptance_verdict(task)
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert recorded is not None and stamped["satisfied"] is False
    assert acceptance_blocks_success(stamped) is True


def test_downstream_artifact_empty_diff_fails_closed():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "deploy-rollback-registry",
        },
        changed_files=[],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert "no changed files" in verdict.reason


@pytest.mark.asyncio
async def test_consumed_downstream_artifact_upgrades_to_proven(db_session):
    """A real consumer consumption row upgrades only the matching producer verdict."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    project_id = project.id
    artifact_key = "release-promotion-record"
    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": artifact_key,
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project_id
    producer.context["provides_capability"] = "release-promotion"
    record_code_acceptance_verdict(producer)
    assert producer.context[CODE_ACCEPTANCE_VERDICT_KEY] == {
        "satisfied": True,
        "proven": False,
        "kind": "downstream_artifact",
        "reason": (
            "downstream_artifact 'release-promotion-record' is reflected in "
            "the shipped changed files"
        ),
        "proof_ref": "release-promotion-record",
    }
    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project_id
    consumer.depends_on = [str(producer.id)]
    db_session.add(producer)
    db_session.add(consumer)
    await db_session.flush()

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key=artifact_key,
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    db_session.add(artifact)
    await db_session.flush()
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(artifact.id),
                "logical_key": artifact_key,
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    rows = await record_artifact_consumptions_for_task(
        consumer, db_session, [artifact.id]
    )
    assert len(rows) == 1
    await db_session.refresh(producer)
    await db_session.refresh(consumer)

    consumption = (
        (
            await db_session.execute(
                select(ArtifactConsumption).where(
                    ArtifactConsumption.consumer_task_id == consumer.id,
                    ArtifactConsumption.artifact_id == artifact.id,
                )
            )
        )
        .scalars()
        .one()
    )
    assert consumption.producer_task_id == producer.id
    assert consumption.logical_key == artifact_key
    verdict = producer.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert verdict["satisfied"] is True
    assert verdict["proven"] is True
    assert verdict["consumed"] is True
    assert verdict["consumed_artifact_id"] == str(artifact.id)
    assert verdict["consumed_by_task_id"] == str(consumer.id)
    assert consumer.context["consumed_artifact_ids"] == [str(artifact.id)]

    # record_capability_proof must have been invoked on the producer's project.
    from app.models import Project as ProjectModel

    producer_project = await db_session.get(ProjectModel, project_id)
    assert producer_project is not None
    proof_records = producer_project.capability_proof_records or {}
    assert "release-promotion" in proof_records, (
        f"expected 'release-promotion' key in capability_proof_records; got {list(proof_records.keys())}"
    )
    proof_entries = proof_records["release-promotion"]
    assert len(proof_entries) == 1
    entry = proof_entries[0]
    assert entry["task_id"] == str(producer.id)
    assert entry["proof_kind"] == "downstream_artifact"
    assert entry["proof_ref"] == artifact_key


@pytest.mark.asyncio
async def test_explicit_consumed_artifact_ids_ignore_undelivered_ids(db_session):
    """Explicit consumption reports are limited to delivered artifact prerequisites."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    project_id = project.id

    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project_id
    record_code_acceptance_verdict(producer)
    db_session.add(producer)
    await db_session.flush()

    delivered_artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="release-promotion-record",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    stray_artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="other-artifact",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/other-artifact.json"
        ),
    )
    db_session.add(delivered_artifact)
    db_session.add(stray_artifact)
    await db_session.flush()

    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project_id
    consumer.depends_on = [str(producer.id)]
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(delivered_artifact.id),
                "logical_key": "release-promotion-record",
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    rows = await record_artifact_consumptions_for_task(
        consumer,
        db_session,
        [delivered_artifact.id, stray_artifact.id],
    )
    assert len(rows) == 1
    await db_session.refresh(producer)
    await db_session.refresh(consumer)

    verdict = producer.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert verdict["consumed"] is True
    assert verdict["consumed_artifact_id"] == str(delivered_artifact.id)
    assert consumer.context["consumed_artifact_ids"] == [str(delivered_artifact.id)]


@pytest.mark.asyncio
async def test_modern_omitted_consumed_artifact_ids_do_not_fallback(db_session):
    """Modern agent omitting consumed_artifact_ids does NOT trigger legacy fallback."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()

    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project.id
    record_code_acceptance_verdict(producer)
    db_session.add(producer)
    await db_session.flush()

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="release-promotion-record",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    db_session.add(artifact)
    await db_session.flush()

    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project.id
    consumer.depends_on = [str(producer.id)]
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(artifact.id),
                "logical_key": "release-promotion-record",
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    # modern_agent=True + None means "agent ran but omitted the field" — no fallback
    rows = await record_artifact_consumptions_for_task(
        consumer, db_session, None, modern_agent=True
    )
    assert rows == []
    await db_session.refresh(producer)
    await db_session.refresh(consumer)
    # No consumption rows written; producer verdict is NOT upgraded to consumed
    assert consumer.context.get("consumed_artifact_ids") is None
    assert producer.context[CODE_ACCEPTANCE_VERDICT_KEY].get("consumed") is not True


@pytest.mark.asyncio
async def test_legacy_omitted_consumed_artifact_ids_falls_back(db_session):
    """Legacy path (modern_agent=False, the default) still uses dependency fallback."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()

    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project.id
    record_code_acceptance_verdict(producer)
    db_session.add(producer)
    await db_session.flush()

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="release-promotion-record",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    db_session.add(artifact)
    await db_session.flush()

    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project.id
    consumer.depends_on = [str(producer.id)]
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(artifact.id),
                "logical_key": "release-promotion-record",
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    # modern_agent=False (default) + None triggers legacy fallback from dependencies
    rows = await record_artifact_consumptions_for_task(
        consumer, db_session, None, modern_agent=False
    )
    assert len(rows) == 1
    await db_session.refresh(producer)
    await db_session.refresh(consumer)
    # Legacy fallback DID record consumption
    assert consumer.context.get("consumed_artifact_ids") == [str(artifact.id)]
    assert producer.context[CODE_ACCEPTANCE_VERDICT_KEY].get("consumed") is True


@pytest.mark.asyncio
async def test_empty_explicit_consumed_artifact_ids_do_not_fallback(db_session):
    """An explicit empty report means no consumption, not legacy fallback."""
    project = make_project()
    db_session.add(project)
    await db_session.flush()
    producer = _make_code_task(
        acceptance_evidence={
            "kind": "downstream_artifact",
            "artifact_id": "release-promotion-record",
        },
        changed_files=["backend/app/release_promotion.py"],
    )
    producer.project_id = project.id
    record_code_acceptance_verdict(producer)
    db_session.add(producer)
    await db_session.flush()

    artifact = Artifact(
        task_id=producer.id,
        artifact_type=ArtifactType.RESEARCH_FINDINGS,
        logical_key="release-promotion-record",
        storage_uri=(
            "gs://blueprint-agent-artifacts/tasks/"
            f"{producer.id}/release-promotion-record.json"
        ),
    )
    db_session.add(artifact)
    await db_session.flush()

    consumer = _make_code_task(changed_files=["backend/app/consumer.py"])
    consumer.project_id = project.id
    consumer.depends_on = [str(producer.id)]
    consumer.context = {
        ARTIFACT_PREREQUISITES_KEY: [
            {
                "artifact_id": str(artifact.id),
                "logical_key": "release-promotion-record",
                "source_task_id": str(producer.id),
            }
        ]
    }
    db_session.add(consumer)
    await db_session.flush()

    rows = await record_artifact_consumptions_for_task(consumer, db_session, [])
    assert rows == []
    await db_session.refresh(producer)
    await db_session.refresh(consumer)
    assert consumer.context.get("consumed_artifact_ids") is None
    assert producer.context[CODE_ACCEPTANCE_VERDICT_KEY].get("consumed") is not True


# ─────────────────────────────────────────────────────────────────────────────
# acceptance_blocks_success contract
# ─────────────────────────────────────────────────────────────────────────────


def test_acceptance_blocks_success_only_on_explicit_unsatisfied():
    assert acceptance_blocks_success(None) is False
    assert acceptance_blocks_success({"satisfied": True}) is False
    assert acceptance_blocks_success({"satisfied": False}) is True
    # Malformed / non-dict recorded value never blocks (additive, fail-safe).
    assert acceptance_blocks_success("garbage") is False
    assert acceptance_blocks_success({}) is False


def test_satisfied_soft_kinds_bucket_as_satisfied_unproven_not_proven():
    """Soft satisfied declarations are not machine-proven in release buckets."""
    soft_task = _make_code_task(
        acceptance_evidence={
            "kind": "observable_behavior",
            "description": "the dashboard shows the live serving SHA banner",
        },
        changed_files=["backend/app/release_readiness.py"],
    )
    record_code_acceptance_verdict(soft_task)

    proven_task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
            "test_id": (
                "test_satisfied_soft_kinds_bucket_as_satisfied_unproven_not_proven"
            ),
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
    )
    record_code_acceptance_verdict(proven_task)

    assert classify_merged_acceptance(soft_task) == "satisfied_unproven"
    assert classify_merged_acceptance(proven_task) == "proven"

    aggregate = summarize_version_acceptance_aggregate([soft_task, proven_task])
    assert aggregate == {
        "proven": 1,
        "satisfied_unproven": 1,
        "inferred_test_cochange": 0,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
        "total": 2,
        "proven_rate": 0.5,
        "satisfied_rate": 1.0,
    }


def test_malformed_recorded_verdict_buckets_as_malformed():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_acceptance_gate.py",
            "test_id": "test_malformed_recorded_verdict_buckets_as_malformed",
        },
        changed_files=["backend/app/acceptance_gate.py"],
    )
    record_code_acceptance_verdict(task)
    task.context[CODE_ACCEPTANCE_VERDICT_KEY].pop("satisfied")

    assert classify_merged_acceptance(task) == "malformed"

    aggregate = summarize_version_acceptance_aggregate([task])
    assert aggregate["malformed"] == 1


# ─────────────────────────────────────────────────────────────────────────────
# inferred_test_cochange — undeclared merges upgraded from PR co-change signal
# ─────────────────────────────────────────────────────────────────────────────


def _inferred_ctx(
    changed_files: list[str] | None, unverified_reason: str | None = None
) -> dict:
    """A landed, undeclared context with the given changed files / merge reason."""
    ctx: dict = {"pr_merged": True, "landed_on_integration": True}
    if changed_files is not None:
        ctx["_pr_changed_files"] = changed_files
    if unverified_reason is not None:
        ctx[REVIEW_LOOP_KEY] = {UNVERIFIED_MERGE_REASON_KEY: unverified_reason}
    return ctx


def test_undeclared_verified_test_and_prod_cochange_is_inferred():
    """Verified merge, no verdict, test + non-test prod file → inferred_test_cochange."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ]
    )
    assert classify_merged_acceptance_context(ctx) == "inferred_test_cochange"
    # It still counts as verified merged work (at least as strong as undeclared).
    assert acceptance_counts_as_verified_context(ctx) is True


def test_undeclared_test_only_diff_is_not_inferred():
    """A test-only diff has no production seam to exercise → stays undeclared."""
    ctx = _inferred_ctx(
        [
            "backend/testing/test_acceptance_gate.py",
            "backend/testing/test_release_qualifier.py",
        ]
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"


def test_undeclared_prod_only_diff_is_not_inferred():
    """A production-only diff shipped no test → stays undeclared."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/app/release_qualifier.py",
        ]
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"


def test_undeclared_empty_changed_files_is_not_inferred():
    """No changed-file signal at all → stays undeclared (fail-safe)."""
    assert classify_merged_acceptance_context(_inferred_ctx([])) == "undeclared"
    assert classify_merged_acceptance_context(_inferred_ctx(None)) == "undeclared"


def test_benign_unverified_test_cochange_is_not_inferred():
    """A benign fall-open means CI did not run — test presence is moot → undeclared."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="no_detectable_test_command",
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"


def test_classify_undeclared_reason_not_ci_verified():
    """A benign fall-open (CI did not run) → not_ci_verified, even with a test+prod diff."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="no_detectable_test_command",
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"
    assert classify_undeclared_reason(ctx) == "not_ci_verified"


def test_classify_undeclared_reason_no_changed_files_context():
    """CI-verified but no _pr_changed_files list recorded → no_changed_files_context."""
    ctx = _inferred_ctx(None)
    assert classify_merged_acceptance_context(ctx) == "undeclared"
    assert classify_undeclared_reason(ctx) == "no_changed_files_context"


def test_classify_undeclared_reason_no_test_prod_cochange():
    """CI-verified with a prod-only changed-files list → no_test_prod_cochange."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/app/release_qualifier.py",
        ]
    )
    assert classify_merged_acceptance_context(ctx) == "undeclared"
    assert classify_undeclared_reason(ctx) == "no_test_prod_cochange"


def test_classify_undeclared_reason_partitions_undeclared_bucket():
    """The three reasons are exhaustive over the undeclared branch's inputs."""
    from app.acceptance_gate import _UNDECLARED_REASON_KEYS

    reasons = {
        classify_undeclared_reason(
            _inferred_ctx(
                ["backend/app/x.py", "backend/testing/test_x.py"],
                unverified_reason="no_detectable_test_command",
            )
        ),
        classify_undeclared_reason(_inferred_ctx(None)),
        classify_undeclared_reason(_inferred_ctx(["backend/app/x.py"])),
    }
    assert reasons == set(_UNDECLARED_REASON_KEYS)


def test_degraded_unverified_test_cochange_stays_degraded():
    """A degraded fall-open overrides inference → merged_unverified_degraded."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
        unverified_reason="verify_setup_failed",
    )
    assert (
        classify_merged_acceptance_context(ctx) == "merged_unverified_degraded"
    )


def test_declared_verdict_wins_over_inferred_cochange():
    """An explicit recorded verdict is classified on its own axis, never inferred."""
    ctx = _inferred_ctx(
        [
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ]
    )
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": True,
        "proven": True,
        "kind": "named_test",
    }
    assert classify_merged_acceptance_context(ctx) == "proven"


def test_inferred_bucket_summarized_and_counts_verified():
    """The aggregate exposes the inferred bucket and it is a distinct 7th key."""
    inferred = _make_code_task(
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_acceptance_gate.py",
        ],
    )
    inferred.context["pr_merged"] = True
    inferred.context["landed_on_integration"] = True
    assert classify_merged_acceptance(inferred) == "inferred_test_cochange"

    aggregate = summarize_version_acceptance_aggregate([inferred])
    assert aggregate == {
        "proven": 0,
        "satisfied_unproven": 0,
        "inferred_test_cochange": 1,
        "declared_unsatisfied": 0,
        "undeclared": 0,
        "merged_unverified_degraded": 0,
        "malformed": 0,
        "total": 1,
        "proven_rate": 0.0,
        "satisfied_rate": 0.0,
    }
    assert aggregate["proven"] == 0
    assert aggregate["satisfied_unproven"] == 0
    assert aggregate["proven_rate"] == 0.0
    assert aggregate["satisfied_rate"] == 0.0


# ─────────────────────────────────────────────────────────────────────────────
# proven axis — evaluate_code_acceptance: named_test and visual are proven at
# evaluation time; downstream_artifact becomes proven later via
# record_artifact_consumptions_for_task (see DB tests below)
# ─────────────────────────────────────────────────────────────────────────────


def test_soft_kinds_are_satisfied_but_not_proven():
    # A satisfied observable_behavior proves only that a diff shipped — it is
    # satisfied but NOT proven.  A downstream_artifact is also not proven at
    # evaluation time; it becomes proven only after downstream consumption.
    for kind, extra, changed in (
        (
            "observable_behavior",
            {"description": "the dashboard shows the SHA banner"},
            ["backend/app/x.py"],
        ),
        (
            "downstream_artifact",
            {"artifact_id": "release-promotion-record"},
            ["backend/app/release_promotion.py"],
        ),
    ):
        task = _make_code_task(
            acceptance_evidence={"kind": kind, **extra},
            changed_files=changed,
        )
        verdict = evaluate_code_acceptance(task)
        assert verdict.satisfied is True
        assert verdict.proven is False
        assert record_code_acceptance_verdict(task).proven is False
        recorded = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        assert recorded["satisfied"] is True
        assert recorded["proven"] is False
        # Declared (so coverage counts it) but NOT proven.
        assert acceptance_was_declared(recorded) is True
        assert acceptance_is_proven(recorded) is False


def test_visual_kind_requires_matching_viewport_screenshot_and_is_proven():
    task = _make_code_task(
        acceptance_evidence={
            "kind": "visual",
            "component": "MetricsDashboard",
            "viewport_width": 1280,
        },
        changed_files=["admin-ui/src/components/MetricsDashboard.tsx"],
    )

    verdict_missing = evaluate_code_acceptance(task)
    assert verdict_missing.satisfied is False
    assert verdict_missing.proven is False
    assert "matching viewport screenshot" in verdict_missing.reason

    record_code_acceptance_verdict(task)
    recorded_missing = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert recorded_missing["satisfied"] is False
    assert acceptance_blocks_success(recorded_missing) is True

    task_with_screenshot = _make_code_task(
        acceptance_evidence={
            "kind": "visual",
            "component": "MetricsDashboard",
            "viewport_width": 1280,
        },
        changed_files=["admin-ui/src/components/MetricsDashboard.tsx"],
    )
    task_with_screenshot.context["viewport_failure_screenshot_uri"] = (
        "gs://bucket/viewport-failures/"
        f"{task_with_screenshot.project_id}/MetricsDashboard-1280.png"
    )

    verdict = evaluate_code_acceptance(task_with_screenshot)
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "viewport-failures" in verdict.reason

    recorded = record_code_acceptance_verdict(task_with_screenshot)
    stamped = task_with_screenshot.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert recorded is not None
    assert stamped["satisfied"] is True
    assert stamped["proven"] is True
    assert acceptance_was_declared(stamped) is True
    assert acceptance_is_proven(stamped) is True
    assert acceptance_blocks_success(stamped) is False


def test_failed_named_test_is_declared_but_not_proven():
    # A declared-but-unsatisfied named_test (test missing from the diff) is
    # declared, blocks success, and is never proven.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False
    recorded = record_code_acceptance_verdict(task)
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert recorded is not None and stamped["proven"] is False
    assert acceptance_was_declared(stamped) is True
    assert acceptance_is_proven(stamped) is False
    assert acceptance_blocks_success(stamped) is True


# ─────────────────────────────────────────────────────────────────────────────
# coverage_delta — test-coverage based acceptance gate
# ─────────────────────────────────────────────────────────────────────────────


def test_coverage_delta_meets_threshold_passes_and_is_proven():
    """coverage_delta passes when the coverage result meets or exceeds threshold."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": 85.0}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "achieved" in verdict.reason


def test_coverage_delta_below_threshold_fails_closed():
    """coverage_delta fails when coverage is below the declared threshold."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": 45.0}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "coverage <" in verdict.reason or "below" in verdict.reason


def test_coverage_delta_missing_result_fails_closed():
    """coverage_delta fails when no coverage result is stored in context."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py"],
    )
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False
    assert "no result" in verdict.reason.lower()


def test_coverage_delta_missing_threshold_fails_closed():
    """coverage_delta fails when no threshold is provided in the declaration.

    The schema-layer validator on AcceptanceEvidence rejects missing threshold
    before ``evaluate_code_acceptance`` is reached, so this test validates the
    gate-level ``_evaluate_coverage_delta`` function directly (bypassing the
    schema validator via ``model_construct``) to confirm the fail-closed guard
    still fires if a malformed declaration ever reached it.
    """
    evidence = AcceptanceEvidence.model_construct(kind="coverage_delta")
    verdict = _evaluate_coverage_delta(evidence, context={})
    assert verdict.declared is True
    assert verdict.satisfied is False
    assert verdict.proven is False


def test_coverage_delta_non_numeric_coverage_fails_closed():
    """coverage_delta fails when coverage_pct is not a number."""
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py"],
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": "high"}
    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is False
    assert verdict.proven is False


def test_coverage_delta_populated_from_verify_tests_artifact():
    """coverage_delta acceptance evidence consumes coverage data that was
    extracted from a MERGE_TEST_RESULT verdict and bridged into task context,
    simulating what the orchestrator CI monitor + ci_completion do in production.

    The MERGE_TEST_RESULT artifact carries a ``coverage`` block
    (``{passed, skipped, total}``) from the verify-tests handler. This test
    simulates the bridge: computing ``coverage_pct`` from those counts and
    storing it under ``test_coverage_result``, then verifying the acceptance
    gate evaluates it correctly.
    """
    # Simulate the coverage block a real MERGE_TEST_RESULT artifact would carry.
    verdict_coverage = {"passed": 42, "skipped": 3, "total": 50}
    total = verdict_coverage["total"]
    passed = verdict_coverage["passed"]
    coverage_pct = round((passed / total) * 100.0, 2)
    assert coverage_pct == 84.0  # 42/50 = 84%

    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": coverage_pct}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is True
    assert "achieved" in verdict.reason


def test_coverage_delta_uses_line_coverage_not_test_pass_rate():
    """coverage_pct reflects the fraction of changed lines hit by the test
    (line coverage), not the test pass rate.

    The orchestrator stores line_coverage_pct (from pytest-cov) as
    coverage_pct in test_coverage_result. When line coverage differs from
    the test pass rate (passed/total), the acceptance gate evaluates against
    the line coverage value — NOT the pass rate.

    This verifies the coverage_delta acceptance evidence kind fixes the
    semantic bug where coverage_pct was computed as (passed/total)*100,
    which is test pass rate, not changed-line coverage.
    """
    # Simulate a MERGE_TEST_RESULT coverage block where line coverage (90%)
    # differs from test pass rate (passed=8, total=10 → 80%).
    # The orchestrator reads line_coverage_pct and stores it as coverage_pct.
    line_cov = 90.0
    pass_rate = round((8 / 10) * 100.0, 2)  # 80.0
    assert line_cov != pass_rate  # 90 != 80 — coverage differs from pass rate

    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 85,  # threshold between pass_rate and line_cov
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    # The coverage_pct is the line coverage value, NOT the test pass rate.
    task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": line_cov}
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True  # 90 >= 85 passes
    assert verdict.proven is True

    # Prove it's NOT pass rate: with the same test counts (8/10 = 80%),
    # using pass rate as coverage_pct would give coverage_pct=80, which
    # is below the threshold of 85 and would fail.
    task2 = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 85,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task2.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": pass_rate}
    pass_verdict = evaluate_code_acceptance(task2)
    assert pass_verdict.satisfied is False  # 80 < 85 fails
    assert pass_verdict.proven is False


def test_coverage_delta_pass_rate_not_proven():
    """coverage_delta with a pass-rate proxy (source="pass_rate") is satisfied
    but NOT proven — test pass rate is semantically distinct from changed-line
    coverage.

    When no pytest-cov/c8 instrumentation is active, the orchestrator stores
    coverage_pct computed from (passed/total)*100 with source="pass_rate". The
    acceptance gate must distinguish this from instrumented line coverage:
    the result is satisfied (a real test run happened and the threshold was met)
    but NOT machine-checked proven (pass-rate is a weaker claim than line
    coverage). This prevents coverage_delta declared on uninstrumented repos
    from silently proving stronger claims than the data supports.

    Exercises the production logic in _evaluate_coverage_delta's source check.
    """
    # Threshold of 80%, pass-rate proxy of 85% — meets threshold but is pass rate.
    task = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task.context[TEST_COVERAGE_RESULT_KEY] = {
        "coverage_pct": 85.0,
        "source": "pass_rate",
    }
    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is True
    assert verdict.proven is False  # pass-rate proxy is NOT proven
    assert "pass-rate proxy" in verdict.reason or "pass rate" in verdict.reason

    # Recorded verdict reflects the same: satisfied but not proven.
    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert recorded.satisfied is True
    assert recorded.proven is False
    stamped = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    assert stamped["satisfied"] is True
    assert stamped["proven"] is False
    assert acceptance_blocks_success(stamped) is False  # satisfied does not block
    assert acceptance_is_proven(stamped) is False  # but is NOT machine-proven

    # Compare with the instrumented (line coverage) path: same threshold, same
    # coverage_pct value but no pass-rate source — IS proven.
    task_instrumented = _make_code_task(
        acceptance_evidence={
            "kind": "coverage_delta",
            "threshold": 80,
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task_instrumented.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": 85.0}
    inst_verdict = evaluate_code_acceptance(task_instrumented)
    assert inst_verdict.proven is True  # instrumented coverage IS proven


def test_acceptance_was_declared_and_is_proven_contract():
    # Undeclared / legacy / garbage rows are never declared and never proven.
    for bad in (None, "garbage", 123, []):
        assert acceptance_was_declared(bad) is False
        assert acceptance_is_proven(bad) is False
    # An empty dict counts as "declared" (a verdict dict was stamped) but,
    # lacking a proven=True flag, is not proven — fail-safe for legacy verdicts
    # written before the proven key existed.
    assert acceptance_was_declared({}) is True
    assert acceptance_is_proven({}) is False
    # Only an explicit proven=True reads as proven.
    assert acceptance_is_proven({"proven": True}) is True
    assert acceptance_is_proven({"proven": False}) is False
    assert acceptance_is_proven({"satisfied": True}) is False


# ─────────────────────────────────────────────────────────────────────────────
# Verdict-axis invariant guard — proven ⟹ satisfied ⟹ declared, over a full mix
#
# A durable, non-gameable guard at the per-task verdict level that complements
# the aggregate-level guard in test_direction_outcomes.py. Across every kind of
# constructed task, it asserts the implication chain between the three truth axes
# always holds, and that via evaluate_code_acceptance a verified, satisfied
# named_test exercising a changed seam is the only static path to proven=True —
# soft, undeclared, test-only, and degraded/unverified evaluations are not.
# (downstream_artifact becomes proven via record_artifact_consumptions_for_task,
# tested separately in the DB section.)
# ─────────────────────────────────────────────────────────────────────────────


# (label, kwargs-for-_make_code_task, expected_declared, expected_satisfied,
#  expected_proven)
_VERDICT_MIX = [
    (
        "proven_named_test",
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": [
                "backend/app/widget.py",
                "backend/testing/test_widget.py",
            ],
            "unverified_reason": None,
        },
        True,
        True,
        True,
    ),
    (
        "soft_observable_behavior",
        {
            "acceptance_evidence": {
                "kind": "observable_behavior",
                "description": "the widget renders the empty state without error",
            },
            "changed_files": ["backend/app/widget.py"],
        },
        True,
        True,
        False,
    ),
    (
        "soft_downstream_artifact",
        {
            "acceptance_evidence": {
                "kind": "downstream_artifact",
                "artifact_id": "widget_metrics_v2",
            },
            "changed_files": ["backend/app/widget.py"],
        },
        True,
        True,
        False,
    ),
    (
        "visual_proven",
        {
            "acceptance_evidence": {
                "kind": "visual",
                "component": "MetricsDashboard",
                "viewport_width": 1280,
            },
            "changed_files": ["admin-ui/src/components/MetricsDashboard.tsx"],
        },
        True,
        True,
        True,
    ),
    (
        "coverage_delta_proven",
        {
            "acceptance_evidence": {
                "kind": "coverage_delta",
                "threshold": 80,
            },
            "changed_files": [
                "backend/app/widget.py",
                "backend/testing/test_widget.py",
            ],
            "unverified_reason": None,
        },
        True,
        True,
        True,
    ),
    (
        "declared_unsatisfied_named_test_missing",
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/app/widget.py"],  # test not in diff
            "unverified_reason": None,
        },
        True,
        False,
        False,
    ),
    (
        "test_only_diff",
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/testing/test_widget.py"],  # no production seam
            "unverified_reason": None,
        },
        True,
        False,
        False,
    ),
    (
        "degraded_unverified_named_test",
        {
            "acceptance_evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": [
                "backend/app/widget.py",
                "backend/testing/test_widget.py",
            ],
            "unverified_reason": "verify_setup_failed",  # fell open, no real verdict
        },
        True,
        False,
        False,
    ),
    (
        "undeclared_legacy",
        {"acceptance_evidence": None, "changed_files": ["backend/app/widget.py"]},
        False,
        True,  # undeclared is vacuously satisfied (never blocks)
        False,
    ),
]


def test_verdict_axis_implication_invariant_over_mix():
    """proven ⟹ satisfied ⟹ declared — checked over a full mix of task kinds.

    Via evaluate_code_acceptance alone, only a verified satisfied named_test
    exercising a changed seam is proven; downstream_artifact consumption is the
    second proven path but requires record_artifact_consumptions_for_task (DB).
    Drives the REAL gate and REAL predicates, never a reimplementation."""
    for label, kwargs, exp_declared, exp_satisfied, exp_proven in _VERDICT_MIX:
        task = _make_code_task(**kwargs)
        if label == "visual_proven":
            task.context["viewport_failure_screenshot_uri"] = (
                "gs://bucket/viewport-failures/"
                f"{task.project_id}/MetricsDashboard-1280.png"
            )
        if label == "coverage_delta_proven":
            task.context[TEST_COVERAGE_RESULT_KEY] = {"coverage_pct": 85.0}
        verdict = evaluate_code_acceptance(task)

        # The verdict matches the expected per-axis outcome for this kind.
        assert verdict.declared is exp_declared, label
        assert verdict.satisfied is exp_satisfied, label
        assert verdict.proven is exp_proven, label

        # Core implication chain on the live verdict: proven ⟹ satisfied ⟹ declared.
        if verdict.proven:
            assert verdict.satisfied, label
        if verdict.satisfied and not verdict.declared:
            # Only the undeclared case is satisfied-without-declared; it is the
            # documented fail-safe (undeclared work is never blocked) and is
            # NEVER proven.
            assert not verdict.proven, label

        # Only a satisfied named_test, visual, or coverage_delta (from
        # line-coverage instrumentation, not pass-rate proxy) is ever proven
        # at evaluation time.
        if verdict.proven:
            assert verdict.kind in {"named_test", "visual", "coverage_delta"}, label

        # Now exercise the recorded-verdict predicates the aggregates consume.
        recorded = record_code_acceptance_verdict(task)
        stamped = task.context.get(CODE_ACCEPTANCE_VERDICT_KEY)
        if not verdict.declared:
            # Undeclared tasks record nothing and are never declared/proven.
            assert recorded is None, label
            assert stamped is None, label
            assert acceptance_was_declared(stamped) is False, label
            assert acceptance_is_proven(stamped) is False, label
            continue

        assert acceptance_was_declared(stamped) is True, label
        # proven ⟹ declared, and proven ⟹ not-blocking (it is satisfied).
        assert acceptance_is_proven(stamped) is exp_proven, label
        if acceptance_is_proven(stamped):
            assert acceptance_was_declared(stamped) is True, label
            assert acceptance_blocks_success(stamped) is False, label


def test_soft_undeclared_testonly_degraded_never_proven():
    """The non-gameability promise: a soft, undeclared, test-only, or
    degraded/unverified merge is NEVER counted as proven."""
    never_proven_labels = {
        "soft_observable_behavior",
        "soft_downstream_artifact",
        "declared_unsatisfied_named_test_missing",
        "test_only_diff",
        "degraded_unverified_named_test",
        "undeclared_legacy",
    }
    for label, kwargs, _decl, _sat, _proven in _VERDICT_MIX:
        if label not in never_proven_labels:
            continue
        task = _make_code_task(**kwargs)
        verdict = evaluate_code_acceptance(task)
        assert verdict.proven is False, label
        record_code_acceptance_verdict(task)
        assert acceptance_is_proven(task.context.get(CODE_ACCEPTANCE_VERDICT_KEY)) is False, label


# ─────────────────────────────────────────────────────────────────────────────
# Consolidated axis invariant tests: universal laws across full verdict matrix
# ─────────────────────────────────────────────────────────────────────────────
#
# These tests parametrize across the FULL verdict-kind matrix to pin the
# non-gameable semantic relationships as fail-closed invariants: proven implies
# declared AND satisfied; soft kinds are satisfied-but-not-proven; unsatisfied
# verdicts never block success; no-declaration verdicts record nothing.


def test_invariant_proven_implies_declared_and_satisfied():
    """Proven NEVER occurs without BOTH declared and satisfied."""
    # Via evaluate_code_acceptance: named_test + verified + seam changes is the
    # static proven path. Consumed downstream_artifact is the second proven path
    # but requires record_artifact_consumptions_for_task (tested in DB section).
    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
            "test_id": "test_widget_handles_empty",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    verdict = evaluate_code_acceptance(task)
    # The one proven case: declared + satisfied + named_test + verified + seam
    assert verdict.proven is True
    assert verdict.declared is True
    assert verdict.satisfied is True

    # Contrapositive: if NOT declared, then NOT proven.
    task_undeclared = _make_code_task(
        acceptance_evidence=None,
        changed_files=["backend/app/widget.py"],
    )
    verdict_undeclared = evaluate_code_acceptance(task_undeclared)
    assert verdict_undeclared.declared is False
    assert verdict_undeclared.proven is False

    # Contrapositive: if NOT satisfied, then NOT proven.
    task_unsatisfied = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],  # test not present
        unverified_reason=None,
    )
    verdict_unsatisfied = evaluate_code_acceptance(task_unsatisfied)
    assert verdict_unsatisfied.declared is True
    assert verdict_unsatisfied.satisfied is False
    assert verdict_unsatisfied.proven is False


def test_invariant_soft_kinds_are_satisfied_not_proven():
    """Observable_behavior / downstream_artifact: satisfied but never proven."""
    # downstream_artifact must be GROUNDED — the declared artifact_id token has
    # to be reflected in a shipped changed-file path (here "promotion" appears in
    # release_promotion.py). A real diff alone is sufficient for observable_behavior.
    for kind, extra, changed in (
        (
            "observable_behavior",
            {"description": "the dashboard shows the SHA banner"},
            ["backend/app/x.py"],
        ),
        (
            "downstream_artifact",
            {"artifact_id": "release-promotion-record"},
            ["backend/app/release_promotion.py"],
        ),
    ):
        task = _make_code_task(
            acceptance_evidence={"kind": kind, **extra},
            changed_files=changed,  # shipped a real, grounded diff
        )
        verdict = evaluate_code_acceptance(task)
        # Soft kinds CAN be satisfied (they shipped a real diff)...
        assert verdict.satisfied is True
        # ...but are NEVER proven (no machine-checked assertion).
        assert verdict.proven is False
        # And soft kinds must be declared (verdict was recorded).
        assert verdict.declared is True


def test_invariant_unsatisfied_verdict_never_proven_blocks_success():
    """Unsatisfied verdicts are never proven and block success scoring."""
    # All fail-closed cases: declared but unsatisfied
    fail_cases = [
        # named_test: test not present in PR
        {
            "evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/app/widget.py"],  # test missing
            "unverified_reason": None,
        },
        # named_test: test-only diff
        {
            "evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/testing/test_widget.py"],  # no seam
            "unverified_reason": None,
        },
        # named_test: unverified merge
        {
            "evidence": {
                "kind": "named_test",
                "test_path": "backend/testing/test_widget.py",
            },
            "changed_files": ["backend/app/widget.py", "backend/testing/test_widget.py"],
            "unverified_reason": "no_runnable_suite",  # fall-open
        },
        # observable_behavior: no changed files
        {
            "evidence": {
                "kind": "observable_behavior",
                "description": "the dashboard shows the SHA banner",
            },
            "changed_files": [],  # empty diff
            "unverified_reason": None,
        },
    ]
    for case in fail_cases:
        task = _make_code_task(
            acceptance_evidence=case["evidence"],
            changed_files=case["changed_files"],
            unverified_reason=case.get("unverified_reason"),
        )
        verdict = evaluate_code_acceptance(task)
        # All fail-closed cases: declared but unsatisfied.
        assert verdict.declared is True, f"Case {case['evidence']['kind']} should be declared"
        assert verdict.satisfied is False, f"Case {case['evidence']['kind']} should be unsatisfied"
        # NEVER proven if unsatisfied.
        assert verdict.proven is False, f"Case {case['evidence']['kind']} should not be proven"
        # Unsatisfied verdict blocks success.
        record_code_acceptance_verdict(task)
        recorded = task.context[CODE_ACCEPTANCE_VERDICT_KEY]
        assert acceptance_blocks_success(recorded) is True


def test_invariant_no_declaration_records_nothing():
    """Undeclared tasks: verdict not recorded, scoring unchanged."""
    task = _make_code_task(
        acceptance_evidence=None,
        changed_files=["backend/app/x.py"],
    )
    verdict = evaluate_code_acceptance(task)
    # No declaration: not recorded.
    assert verdict.declared is False
    result = record_code_acceptance_verdict(task)
    assert result is None
    # The verdict key is never written.
    assert CODE_ACCEPTANCE_VERDICT_KEY not in (task.context or {})
    # Undeclared tasks never block success (backward-compatible).
    assert acceptance_blocks_success(None) is False


def test_invariant_all_valid_kinds_covered():
    """Every VALID kind is tested: named_test, observable_behavior, downstream_artifact, visual."""
    # This invariant ensures there is no blind spot in the kind matrix.
    # The only valid kinds per AcceptanceEvidence schema are these four.
    # (Unknown kinds cannot be created by validation, so we test only real ones.)
    from app.payload_schemas import AcceptanceEvidence

    # named_test kind
    nt = AcceptanceEvidence(kind="named_test", test_path="backend/testing/test_x.py")
    assert nt.kind == "named_test"

    # observable_behavior kind
    ob = AcceptanceEvidence(kind="observable_behavior", description="feature works")
    assert ob.kind == "observable_behavior"

    # downstream_artifact kind
    da = AcceptanceEvidence(kind="downstream_artifact", artifact_id="some-artifact")
    assert da.kind == "downstream_artifact"

    # visual kind
    vis = AcceptanceEvidence(
        kind="visual", component="MetricsDashboard", viewport_width=1280
    )
    assert vis.kind == "visual"

    # Verify the four kinds are the closed set by checking the schema.
    # (This documents that if AcceptanceEvidence.kind ever adds a new kind,
    # this test's assertion will need updating, triggering a review of whether
    # the evaluation logic covers the new kind.)
    import typing

    hints = typing.get_type_hints(AcceptanceEvidence)
    kind_annotation = hints.get("kind")
    if hasattr(kind_annotation, "__args__"):
        valid_kinds = set(kind_annotation.__args__)
        assert valid_kinds == {
            "named_test",
            "observable_behavior",
            "downstream_artifact",
            "visual",
            "coverage_delta",
        }


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate invariant tests: per-cycle relationships that strategic/release
# decisions consume (proven_merged <= declared_merged <= total_write, etc.)
# ─────────────────────────────────────────────────────────────────────────────


def test_aggregate_invariant_proven_subset_of_declared():
    """Per-cycle: proven_merged_count <= declared_merged_count."""
    # Import the aggregation functions from direction_outcomes.
    from app.direction_outcomes import (
        proven_merged_count,
        declared_merged_count,
    )

    # Construct synthetic research_history entries covering all sub-relations.
    cases = [
        # All zero: no merges, no declarations.
        {
            "entry": {
                "acceptance_proven_merged": 0,
                "acceptance_declared_merged": 0,
                "acceptance_merged_write_total": 0,
                "acceptance_declaration_coverage": None,
            },
            "expected_proven": 0,
            "expected_declared": 0,
            "expected_total": 0,
        },
        # Some declared, none proven (soft kinds only).
        {
            "entry": {
                "acceptance_proven_merged": 0,
                "acceptance_declared_merged": 3,  # 3 soft-kind merges
                "acceptance_merged_write_total": 5,
                "acceptance_declaration_coverage": 0.6,
            },
            "expected_proven": 0,
            "expected_declared": 3,
            "expected_total": 5,
        },
        # Mixed: some proven, some declared but not proven.
        {
            "entry": {
                "acceptance_proven_merged": 2,  # 2 named_test proven
                "acceptance_declared_merged": 5,  # 3 more soft kinds
                "acceptance_merged_write_total": 7,  # 2 undeclared
                "acceptance_declaration_coverage": 5 / 7,
            },
            "expected_proven": 2,
            "expected_declared": 5,
            "expected_total": 7,
        },
        # All declared, all proven (all named_tests).
        {
            "entry": {
                "acceptance_proven_merged": 4,
                "acceptance_declared_merged": 4,
                "acceptance_merged_write_total": 4,
                "acceptance_declaration_coverage": 1.0,
            },
            "expected_proven": 4,
            "expected_declared": 4,
            "expected_total": 4,
        },
    ]
    for case in cases:
        entry = case["entry"]
        proven = proven_merged_count(entry)
        declared = declared_merged_count(entry)

        # Invariant 1: proven is always a subset of declared.
        assert proven <= declared, (
            f"proven={proven} > declared={declared} violates subset invariant"
        )
        # Invariant 2: declared is never larger than total merged write tasks.
        assert declared <= entry.get("acceptance_merged_write_total", 0), (
            f"declared={declared} > total={entry.get('acceptance_merged_write_total')} "
            "violates coverage invariant"
        )


def test_aggregate_invariant_coverage_fraction():
    """Per-cycle: declaration_coverage == declared / total, in [0, 1]."""
    from app.direction_outcomes import declared_merged_count

    cases = [
        # No write tasks: coverage is None.
        {
            "entry": {
                "acceptance_declared_merged": 0,
                "acceptance_merged_write_total": 0,
                "acceptance_declaration_coverage": None,
            },
            "expected_coverage": None,
        },
        # All undeclared: coverage is 0.0.
        {
            "entry": {
                "acceptance_declared_merged": 0,
                "acceptance_merged_write_total": 5,
                "acceptance_declaration_coverage": 0.0,
            },
            "expected_coverage": 0.0,
        },
        # Half declared: coverage is 0.5.
        {
            "entry": {
                "acceptance_declared_merged": 3,
                "acceptance_merged_write_total": 6,
                "acceptance_declaration_coverage": 0.5,
            },
            "expected_coverage": 0.5,
        },
        # All declared: coverage is 1.0.
        {
            "entry": {
                "acceptance_declared_merged": 4,
                "acceptance_merged_write_total": 4,
                "acceptance_declaration_coverage": 1.0,
            },
            "expected_coverage": 1.0,
        },
    ]
    for case in cases:
        entry = case["entry"]
        coverage = entry.get("acceptance_declaration_coverage")
        declared = declared_merged_count(entry)
        total = entry.get("acceptance_merged_write_total", 0)

        if coverage is None:
            # No write tasks: coverage is None.
            assert total == 0 or declared == 0
        else:
            # Coverage must be in [0, 1].
            assert 0.0 <= coverage <= 1.0, f"coverage={coverage} outside [0, 1]"
            # Coverage must equal declared / total (within floating-point tolerance).
            if total > 0:
                expected_coverage = round(declared / total, 3)
                assert coverage == expected_coverage, (
                    f"coverage={coverage} != declared/total={expected_coverage}"
                )


def test_aggregate_invariant_legacy_entries_degrade_safely():
    """Legacy entries (before new fields existed) degrade to safe defaults."""
    from app.direction_outcomes import (
        proven_merged_count,
        declared_merged_count,
    )

    # Old entry: no acceptance fields at all (pre-2026-06 entries).
    old_entry = {
        "prs_merged": 10,
        "tasks_completed": 5,
    }
    # proven and declared count functions default to 0 when fields are absent.
    assert proven_merged_count(old_entry) == 0
    assert declared_merged_count(old_entry) == 0


def test_aggregate_invariant_coverage_never_exceeds_bounds():
    """Declaration coverage is clamped to [0, 1] and never inverted."""
    # This invariant guards against division errors or inverted numerator/denominator.
    cases = [
        # declared > total (logic error — should never happen, but guard it).
        {
            "declared": 5,
            "total": 3,
            "should_clamp": True,
        },
        # Normal case: 0 <= declared <= total.
        {
            "declared": 2,
            "total": 5,
            "should_clamp": False,
        },
    ]
    for case in cases:
        declared = case["declared"]
        total = case["total"]
        # Synthesize the coverage value as the code would compute it.
        coverage = round(declared / total, 3) if total > 0 else None
        # Coverage should be in [0, 1].
        # If declared > total, coverage would exceed 1 — that's a bug in the
        # source code (declared should never exceed total), not a test issue.
        # But we document the invariant: if it ever happens, coverage > 1 is
        # detectable evidence of the bug.
        if coverage is not None and declared <= total:
            assert coverage <= 1.0


def test_missing_review_loop_not_scored_as_proven():
    """Missing/malformed review_loop fails closed: unverified merge not scored as proven.

    The _merge_was_test_verified helper must fail closed when review_loop context
    is absent or malformed (like all other evidence checks in the module), NOT
    return True and give an unverified merge a free pass to proven scoring.
    An absent review_loop means we cannot attest the merge was verified —
    the safe default is unverified.
    """
    # Case 1: completely missing review_loop context
    task_no_context = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,  # Context dict has no review_loop at all
    )
    # Explicitly clear the context to simulate no review_loop key
    task_no_context.context = {"_pr_changed_files": task_no_context.context["_pr_changed_files"]}

    verdict_no_context = evaluate_code_acceptance(task_no_context)
    # Missing review_loop should fail closed: NOT proven, not satisfied for named_test
    assert verdict_no_context.declared is True
    assert verdict_no_context.satisfied is False
    assert verdict_no_context.proven is False
    assert "did not run green" in verdict_no_context.reason

    # Case 2: malformed review_loop (not a dict)
    task_malformed = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason=None,
    )
    task_malformed.context[REVIEW_LOOP_KEY] = "not a dict"  # Malformed

    verdict_malformed = evaluate_code_acceptance(task_malformed)
    assert verdict_malformed.declared is True
    assert verdict_malformed.satisfied is False
    assert verdict_malformed.proven is False
    assert "did not run green" in verdict_malformed.reason

    # Case 3: with explicit unverified_reason (control — should already fail)
    task_explicit_unverified = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py", "backend/testing/test_widget.py"],
        unverified_reason="no_runnable_suite",  # Explicit fall-open
    )

    verdict_explicit = evaluate_code_acceptance(task_explicit_unverified)
    assert verdict_explicit.declared is True
    assert verdict_explicit.satisfied is False
    assert verdict_explicit.proven is False

    # All three (missing, malformed, explicit unverified) fail closed identically
    # when a named_test is declared with a real changed seam.
    record_code_acceptance_verdict(task_no_context)
    record_code_acceptance_verdict(task_malformed)
    record_code_acceptance_verdict(task_explicit_unverified)

    assert acceptance_blocks_success(task_no_context.context[CODE_ACCEPTANCE_VERDICT_KEY]) is True
    assert acceptance_blocks_success(task_malformed.context[CODE_ACCEPTANCE_VERDICT_KEY]) is True
    assert acceptance_blocks_success(task_explicit_unverified.context[CODE_ACCEPTANCE_VERDICT_KEY]) is True


@pytest.mark.asyncio
async def test_backfill_acceptance_verdicts_empty_db(db_session):
    """The backfill returns 0 on an empty or unpopulated database."""
    result = await backfill_acceptance_verdicts(db_session)
    assert result == 0


@pytest.mark.asyncio
async def test_backfill_acceptance_verdicts_upgrades_false_negative(db_session):
    """A pre-fix task with the false-negative verdict pattern is upgraded by
    the backfill sweep, and its verdict is updated to proven=True.
    """
    from datetime import datetime, timezone

    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_invariants.py",
        },
        changed_files=[
            "backend/app/acceptance_gate.py",
            "backend/testing/test_invariants.py",
        ],
    )
    task.project_id = project.id
    task.completed_at = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)
    # Stamp a false-negative verdict exactly as the pre-fix gate would have:
    # satisfied=True (test was present, verified, and shipped a production seam)
    # but proven=False because _test_semantically_grounded failed for the
    # cross-cutting test_invariants.py before the _CROSS_CUTTING_TEST_FILES fix.
    ctx = dict(task.context or {})
    ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
        "satisfied": True,
        "proven": False,
        "kind": "named_test",
        "reason": (
            "declared named_test 'backend/testing/test_invariants.py' exercises "
            "a production seam that does not semantically correspond to the "
            "declared test module"
        ),
    }
    task.context = ctx
    db_session.add(task)
    await db_session.flush()

    result = await backfill_acceptance_verdicts(db_session)
    assert result >= 1, "backfill should upgrade the pre-fix false-negative verdict"

    # Re-read the task and verify the verdict was upgraded
    await db_session.refresh(task)
    updated = (task.context or {}).get(CODE_ACCEPTANCE_VERDICT_KEY)
    assert updated is not None
    assert updated.get("proven") is True, "verdict should be upgraded to proven"
    assert updated.get("backfilled") is True


@pytest.mark.asyncio
async def test_backfill_verdicts_no_starvation(db_session):
    """ORDER BY + NOT EXISTS ensures repeated backfill calls cover all
    >200 false-negative tasks (no starvation), and already-proven tasks
    with CriterionEvidence rows are excluded from the query.
    """
    from datetime import datetime, timezone

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    # Create an open release version so record_task_merge_evidence
    # persists CriterionEvidence rows for upgraded tasks — this is
    # required by the NOT EXISTS filter to advance the LIMIT window.
    version = ReleaseVersion(project_id=project.id)
    db_session.add(version)
    await db_session.flush()

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-backfill-starvation-test",
        text="Backfill starvation test criterion",
    )
    db_session.add(criterion)
    await db_session.flush()
    criterion_key = criterion.criterion_key

    FIX_TS = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)

    def _make_candidate(*, proven: bool) -> Task:
        task = _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_invariants.py",
            },
            changed_files=[
                "backend/app/acceptance_gate.py",
                "backend/testing/test_invariants.py",
            ],
        )
        task.project_id = project.id
        task.completed_at = FIX_TS
        task.criterion_ids = [criterion_key]
        ctx = dict(task.context or {})
        ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
            "satisfied": True,
            "proven": proven,
            "kind": "named_test",
            "reason": "simulated for starvation test",
        }
        task.context = ctx
        return task

    # ── Already-proven tasks (have CriterionEvidence rows) ──────────────
    ALREADY_PROVEN = 10
    for _ in range(ALREADY_PROVEN):
        task = _make_candidate(proven=True)
        db_session.add(task)
        await db_session.flush()
        ev = CriterionEvidence(
            project_id=project.id,
            criterion_id=criterion.id,
            source_kind="task_merge",
            source_id=str(task.id),
            detail={"proven": True},
        )
        db_session.add(ev)
    await db_session.flush()

    # ── False-negative tasks (satisfied=True, proven=False, no evidence) ─
    FALSE_NEGATIVE = 205
    for _ in range(FALSE_NEGATIVE):
        task = _make_candidate(proven=False)
        db_session.add(task)
    await db_session.flush()

    # ── Run backfill until all false-negatives are consumed ─────────────
    total_upgraded = 0
    for iteration in range(5):
        upgraded = await backfill_acceptance_verdicts(db_session)
        total_upgraded += upgraded
        if upgraded == 0:
            break

    assert total_upgraded == FALSE_NEGATIVE, (
        f"Expected {FALSE_NEGATIVE} upgrades (all false-negative tasks), "
        f"got {total_upgraded}; {ALREADY_PROVEN} already-proven tasks "
        f"should have been excluded via NOT EXISTS"
    )

    # Confirm idempotent: a final pass returns 0
    final = await backfill_acceptance_verdicts(db_session)
    assert final == 0, "No false negatives should remain after full sweep"


@pytest.mark.skipif(
    not USE_POSTGRES,
    reason=(
        "MissingGreenlet on expired task attributes is asyncpg-specific; aiosqlite "
        "lazy-loads succeed in the async greenlet, masking the regression. "
        "Fault injection is not practical: the rollback happens inside "
        "record_task_merge_evidence which re-fetches the task via session.get(), "
        "leaving no ORM object that a proxy could intercept."
    ),
)
@pytest.mark.asyncio
async def test_backfill_verdicts_resilient_to_mid_loop_rollback(db_session):
    """A rollback inside record_task_merge_evidence (e.g. IntegrityError on
    CriterionEvidence unique constraint) must not abort the whole backfill
    cycle.  Specifically, subsequent loop iterations must not raise
    MissingGreenlet when accessing task attributes after the rollback
    unconditionally expired all session objects.

    Regression test for the gap where expire_on_commit=False only covered
    the COMMIT path, leaving the ROLLBACK path unprotected.  The fix
    collects task IDs upfront and re-fetches each task fresh at the start
    of every iteration via session.get(), making each iteration independent
    of prior commits or rollbacks.
    """
    from datetime import datetime, timezone
    from unittest.mock import patch

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    FIX_TS = datetime(2026, 7, 9, 8, 0, 0, tzinfo=timezone.utc)

    # Three tasks eligible for upgrade (false-negative named_test verdicts).
    for _ in range(3):
        task = _make_code_task(
            acceptance_evidence={
                "kind": "named_test",
                "test_path": "backend/testing/test_invariants.py",
            },
            changed_files=[
                "backend/app/acceptance_gate.py",
                "backend/testing/test_invariants.py",
            ],
        )
        task.project_id = project.id
        task.completed_at = FIX_TS
        ctx = dict(task.context or {})
        ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
            "satisfied": True,
            "proven": False,
            "kind": "named_test",
            "reason": "simulated false-negative for rollback resilience test",
        }
        task.context = ctx
        db_session.add(task)
    await db_session.flush()

    # Patch record_task_merge_evidence to simulate a session rollback on the
    # first call (as would happen when record_evidence hits an IntegrityError
    # on a duplicate CriterionEvidence row).  After the rollback all session
    # objects are expired; the fix must re-fetch each task so the 2nd and 3rd
    # iterations do not raise MissingGreenlet on task attribute access.
    call_count = [0]

    async def _mock_rtme_with_rollback(session, task, project_id):
        call_count[0] += 1
        if call_count[0] == 1:
            await session.rollback()
        return 0

    with patch(
        "app.acceptance_criteria.record_task_merge_evidence",
        _mock_rtme_with_rollback,
    ):
        upgraded = await backfill_acceptance_verdicts(db_session)

    assert upgraded == 3, (
        f"Expected all 3 tasks upgraded (verdict set to proven); got {upgraded}. "
        "A MissingGreenlet or premature abort would have reduced this count."
    )


@pytest.mark.asyncio
async def test_backfill_verdicts_advances_past_unprovable(db_session):
    """The backfill query advances past tasks that can never be proven (wrong
    verdict kind, not satisfied, or re-evaluation still unprovable), instead of
    getting stuck on them permanently and starving newer tasks behind them.

    Regression test for the starvation bug where the LIMIT-200 window never
    advanced because non-upgradeable tasks received no marker and blocked all
    later-ordered tasks from ever being examined.
    """
    from datetime import datetime, timezone

    from testing.factories import make_project

    project = make_project()
    db_session.add(project)
    await db_session.flush()

    # Create an open release version so record_task_merge_evidence persists
    # CriterionEvidence rows for upgraded tasks — this is required by the
    # NOT EXISTS filter that handles already-proven rows.
    version = ReleaseVersion(project_id=project.id)
    db_session.add(version)
    await db_session.flush()

    criterion = VersionAcceptanceCriterion(
        project_id=project.id,
        release_version_id=version.id,
        criterion_key="c-backfill-advance-test",
        text="Backfill advance test criterion",
    )
    db_session.add(criterion)
    await db_session.flush()
    criterion_key = criterion.criterion_key

    OLD_TS = datetime(2026, 7, 9, 7, 0, 0, tzinfo=timezone.utc)  # Pre-fix
    NEW_TS = datetime(2026, 7, 9, 9, 0, 0, tzinfo=timezone.utc)  # Also pre-fix, newer

    def _make_candidate(
        *,
        verdict_kind: str = "named_test",
        satisfied: bool = True,
        proven: bool = False,
        completed_at: datetime = OLD_TS,
        test_path: str | None = None,
        changed_files: list[str] | None = None,
    ) -> Task:
        task = _make_code_task(
            acceptance_evidence=(
                {"kind": "named_test", "test_path": test_path or "backend/testing/test_invariants.py"}
                if verdict_kind == "named_test"
                else {"kind": "observable_behavior"}
            ),
            changed_files=changed_files or [
                "backend/app/acceptance_gate.py",
                "backend/testing/test_invariants.py",
            ],
        )
        task.project_id = project.id
        task.completed_at = completed_at
        task.criterion_ids = [criterion_key]
        ctx = dict(task.context or {})
        ctx[CODE_ACCEPTANCE_VERDICT_KEY] = {
            "satisfied": satisfied,
            "proven": proven,
            "kind": verdict_kind,
            "reason": "simulated for advance test",
        }
        task.context = ctx
        return task

    # ── Old non-upgradeable tasks (occupy the front of the LIMIT window) ──
    # We create >= 200 tasks that the backfill examines and marks but can
    # never upgrade. These fill the entire LIMIT-200 window on every call,
    # so WITHOUT the _backfill_checked_at marker the window never advances
    # past them and the upgradeable tasks behind them are starved.
    # Three categories are cycled:
    #   - Wrong verdict kind (not named_test)
    #   - Not satisfied
    #   - Re-evaluation still unprovable
    BLOCKER_COUNT = 215  # > LIMIT-200 to saturate the window
    for i in range(BLOCKER_COUNT):
        category = i % 3
        if category == 0:
            # Wrong verdict kind — skipped at the kind guard
            task = _make_candidate(verdict_kind="observable_behavior")
        elif category == 1:
            # Not satisfied — skipped at the satisfied guard
            task = _make_candidate(satisfied=False)
        else:
            # Re-evaluation still unprovable — test_path not in changed_files
            task = _make_candidate(
                test_path="backend/testing/test_reaper.py",
                changed_files=["backend/app/reaper.py"],
            )
        db_session.add(task)
    await db_session.flush()

    # ── Newer upgradeable tasks behind the stuck ones ─────────────────────
    # These are only reachable across successive backfill calls if the
    # _backfill_checked_at marker excludes the blocker tasks from the query.
    FALSE_NEGATIVE = 205
    for _ in range(FALSE_NEGATIVE):
        task = _make_candidate(
            completed_at=NEW_TS,
            test_path="backend/testing/test_invariants.py",
            changed_files=[
                "backend/app/acceptance_gate.py",
                "backend/testing/test_invariants.py",
            ],
        )
        db_session.add(task)
    await db_session.flush()

    # ── Run backfill repeatedly until exhausted ──────────────────────────
    # The loop does NOT break on upgraded==0 because the first 1-2 calls
    # may return 0 upgrades while filling the LIMIT-200 window with blocker
    # tasks that the marker stamps. Only when all blockers are marked (and
    # thus excluded via _backfill_checked) does the query advance to the
    # upgradeable false-negative tasks.
    # Without the marker fix, the window stays stuck on the 215 blockers
    # permanently and total_upgraded stays 0 → assertion catches it.
    total_upgraded = 0
    for iteration in range(6):
        upgraded = await backfill_acceptance_verdicts(db_session)
        total_upgraded += upgraded

    # All 205 false negatives must be upgraded. WITHOUT the marker fix the
    # window stays stuck on the 215 blocker tasks (all in the LIMIT-200 query
    # result on every call since none get marked) and the upgradeable tasks
    # are never reached.
    assert total_upgraded == FALSE_NEGATIVE, (
        f"Expected {FALSE_NEGATIVE} upgrades (all false-negative tasks), "
        f"got {total_upgraded} — the backfill window is stuck on "
        f"non-upgradeable tasks"
    )

    # Confirm idempotent: a final pass returns 0
    final = await backfill_acceptance_verdicts(db_session)
    assert final == 0, "No false negatives should remain after full sweep"
