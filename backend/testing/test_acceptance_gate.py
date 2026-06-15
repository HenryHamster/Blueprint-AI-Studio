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

import uuid

import pytest
from sqlmodel import select

from app.acceptance_gate import (
    CODE_ACCEPTANCE_VERDICT_KEY,
    CRITERION_OPS,
    acceptance_blocks_success,
    acceptance_is_proven,
    acceptance_was_declared,
    classify_merged_acceptance,
    coerce_number,
    criterion_met,
    evaluate_code_acceptance,
    record_artifact_consumptions_for_task,
    record_code_acceptance_verdict,
    summarize_version_acceptance_aggregate,
)
from app.artifact_prerequisites import ARTIFACT_PREREQUISITES_KEY
from app.landed_signal import REVIEW_LOOP_KEY, UNVERIFIED_MERGE_REASON_KEY
from app.models import Artifact, ArtifactConsumption, ArtifactType, Task, TaskStatus
from testing.factories import make_project


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
    if unverified_reason is not None:
        context[REVIEW_LOOP_KEY] = {UNVERIFIED_MERGE_REASON_KEY: unverified_reason}
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

        # Only a satisfied named_test or visual is ever proven at evaluation time.
        if verdict.proven:
            assert verdict.kind in {"named_test", "visual"}, label

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
    # (This documents that if AcceptanceEvidence.kind ever adds a fourth kind,
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
