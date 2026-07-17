"""Tests for app.experiment_authoring_context.

Covers the three experiment-authoring context surfaces and their shared budget:
  * command normalization / fingerprinting (pure)
  * runner device descriptors (pure — bounds, VRAM skip, container flags)
  * the experiment ledger — a bounded QUERY over existing Task/ComputeJob rows
    (NOT a new table), rendered as compact lines
  * near-duplicate advisories for pending experiments (string matching only)
  * apply_experiment_authoring_budget — ONE shared char budget with priority
    devices > ledger > advisories
"""

import uuid
from datetime import timedelta

import pytest

from app.experiment_authoring_context import (
    DEVICES_MAX_GPU_MODELS,
    DEVICES_MAX_RUNNERS,
    EXPERIMENT_LEDGER_MAX_ROWS,
    apply_experiment_authoring_budget,
    build_experiment_ledger,
    build_near_duplicate_advisories,
    build_runner_device,
    build_runner_devices,
    command_fingerprint,
    fetch_pending_experiment_specs,
    normalize_command,
    render_experiment_ledger_lines,
    render_experiment_ledger_prompt_block,
)
from app.models import (
    ComputeJob,
    ComputeJobOutcome,
    ComputeJobStatus,
    LocalRunner,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    _utcnow,
)

TEST_USER_ID = "test-user-123"


async def _make_project(session, name="Experiment Ledger Project"):
    project = Project(name=name, user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner"))
    await session.commit()
    return project


async def _make_compute_task(
    session,
    project_id,
    *,
    title,
    command,
    hypothesis=None,
    status=TaskStatus.COMPLETED,
    compute_result=None,
    created_at=None,
    completed_at=None,
):
    payload = {"type": "compute_experiment", "command": command}
    if hypothesis is not None:
        payload["hypothesis"] = hypothesis
    task = Task(
        project_id=project_id,
        title=title,
        status=status,
        payloads=[payload],
        created_at=created_at or _utcnow(),
        completed_at=completed_at,
        plan_output={"compute_result": compute_result} if compute_result else None,
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    return task


# ── normalization / fingerprinting ───────────────────────────────────────────


def test_normalize_command_collapses_whitespace():
    assert normalize_command("  python   train.py \n --epochs 3  ") == (
        "python train.py --epochs 3"
    )
    assert normalize_command(None) == ""
    assert normalize_command(42) == ""


def test_command_fingerprint_stable_across_whitespace():
    a = command_fingerprint("python train.py --epochs 3")
    b = command_fingerprint("  python \t train.py \n --epochs 3 ")
    assert a is not None
    assert a == b
    assert command_fingerprint("python eval.py") != a
    assert command_fingerprint("") is None
    assert command_fingerprint(None) is None


# ── runner device descriptors (pure) ─────────────────────────────────────────


def _runner(caps, hostname="host-1"):
    return LocalRunner(
        project_id=uuid.uuid4(),
        token_hash="0" * 64,
        hostname=hostname,
        capabilities=caps,
        last_seen_at=_utcnow(),
    )


def test_build_runner_device_full_caps():
    device = build_runner_device(
        _runner(
            {
                "gpu_count": 8,
                "gpu_names": ["Tesla V100-SXM2-16GB"] * 8,
                "gpu_memory_total_mib": [16384] * 8,
                "isolation_backends": {"docker_usable": True, "docker_cdi_gpu": True},
                "container_gpu_passthrough": {
                    "docker": {"verified": True, "mode": "cdi", "reason": "verified"}
                },
                "active_task_ids": ["a", "b"],
                "active_compute_job_ids": ["c"],
            },
            hostname="dgx-1",
        )
    )
    assert device == {
        "runner_name": "dgx-1",
        "gpu_count": 8,
        "gpu_models": ["Tesla V100-SXM2-16GB"],
        "container_ok": True,
        "gpu_in_container_ok": True,
        "active_tasks": 2,
        "active_compute_jobs": 1,
        "gpu_vram_mib": [16384] * 8,
    }


def test_build_runner_device_skips_absent_vram_and_bounds_models():
    many_models = [f"GPU-Model-{i}" for i in range(DEVICES_MAX_GPU_MODELS + 3)]
    device = build_runner_device(
        _runner({"gpu_count": len(many_models), "gpu_names": many_models})
    )
    assert "gpu_vram_mib" not in device  # older runner: key omitted, never 0
    assert len(device["gpu_models"]) == DEVICES_MAX_GPU_MODELS
    assert device["container_ok"] is False
    assert device["gpu_in_container_ok"] is False


def test_build_runner_device_uses_selected_runtime_and_mode_proof():
    """A lower-priority runtime's proof cannot authorize selected Docker."""
    device = build_runner_device(
        _runner(
            {
                "gpu_count": 1,
                "isolation_backends": {
                    "docker_usable": True,
                    "docker_cdi_gpu": True,
                    "podman_usable": True,
                },
                "container_gpu_passthrough": {
                    "docker": {
                        "verified": True,
                        "mode": "hook",
                        "reason": "verified",
                    },
                    "podman": {
                        "verified": True,
                        "mode": "hook",
                        "reason": "verified",
                    },
                },
            }
        )
    )
    assert device["container_ok"] is True
    assert device["gpu_in_container_ok"] is False


def test_build_runner_devices_total_bound_and_gpu_first():
    runners = [
        _runner({"gpu_count": i % 3}, hostname=f"node-{i}")
        for i in range(DEVICES_MAX_RUNNERS + 4)
    ]
    devices = build_runner_devices(runners)
    assert len(devices) == DEVICES_MAX_RUNNERS
    counts = [d["gpu_count"] for d in devices]
    assert counts == sorted(counts, reverse=True)


# ── experiment ledger (bounded query over existing rows) ─────────────────────


@pytest.mark.asyncio
async def test_build_experiment_ledger_entries_and_lines(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    # Older successful experiment with a full compute_result verdict.
    met = await _make_compute_task(
        db_session,
        project.id,
        title="MJX throughput probe",
        command="python bench.py --envs 4096",
        hypothesis="MJX sustains >=90k env-steps/s on L40S",
        status=TaskStatus.COMPLETED,
        compute_result={
            "outcome": "SUCCESS",
            "final_cost_usd": 1.42,
            "criterion": {
                "metric": "env_steps_per_s",
                "operator": ">=",
                "threshold": 90000,
                "value": 95000,
                "met": True,
                "measurable": True,
            },
        },
        created_at=now - timedelta(hours=2),
        completed_at=now - timedelta(hours=1),
    )
    # Newer failed experiment with NO compute_result — cost/outcome/reason must
    # fall back to its ComputeJob row.
    failed = await _make_compute_task(
        db_session,
        project.id,
        title="PPO long train",
        command="python train.py --steps 5e7",
        status=TaskStatus.FAILED,
        created_at=now - timedelta(minutes=30),
    )
    db_session.add(
        ComputeJob(
            project_id=project.id,
            task_id=failed.id,
            backend="runner_subprocess",
            status=ComputeJobStatus.FAILED,
            outcome=ComputeJobOutcome.TIMED_OUT,
            final_cost_usd=3.5,
            terminal_reason="wall clock deadline exceeded",
        )
    )
    # Pending experiment and a non-compute task must both be excluded.
    await _make_compute_task(
        db_session,
        project.id,
        title="queued probe",
        command="python bench.py --envs 8192",
        status=TaskStatus.QUEUED,
        created_at=now,
    )
    db_session.add(
        Task(
            project_id=project.id,
            title="ordinary research",
            status=TaskStatus.COMPLETED,
            payloads=[{"type": "research", "prompt": "look around"}],
        )
    )
    await db_session.commit()

    entries = await build_experiment_ledger(db_session, project.id)
    assert [e["title"] for e in entries] == ["PPO long train", "MJX throughput probe"]

    failed_entry, met_entry = entries
    assert failed_entry["outcome"] == "TIMED_OUT"
    assert failed_entry["cost_usd"] == 3.5
    assert failed_entry["terminal_reason"] == "wall clock deadline exceeded"
    assert failed_entry["command_fingerprint"] == command_fingerprint(
        "python train.py --steps 5e7"
    )

    assert met_entry["task_id"] == str(met.id)
    assert met_entry["outcome"] == "SUCCESS"
    assert met_entry["cost_usd"] == 1.42
    assert met_entry["criterion"] == "env_steps_per_s>=90000 MET (95000)"
    assert met_entry["hypothesis"] == "MJX sustains >=90k env-steps/s on L40S"

    lines = render_experiment_ledger_lines(entries)
    assert len(lines) == 2
    assert "outcome=TIMED_OUT" in lines[0]
    assert "reason: wall clock deadline exceeded" in lines[0]
    assert "crit: env_steps_per_s>=90000 MET (95000)" in lines[1]
    assert "cost=$1.42" in lines[1]
    assert all(len(line) <= 240 for line in lines)


@pytest.mark.asyncio
async def test_build_experiment_ledger_row_cap(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    for i in range(EXPERIMENT_LEDGER_MAX_ROWS + 3):
        await _make_compute_task(
            db_session,
            project.id,
            title=f"exp-{i}",
            command=f"python run.py --seed {i}",
            status=TaskStatus.COMPLETED,
            compute_result={"outcome": "SUCCESS", "final_cost_usd": 0.1},
            created_at=now - timedelta(minutes=i),
        )
    entries = await build_experiment_ledger(db_session, project.id)
    assert len(entries) == EXPERIMENT_LEDGER_MAX_ROWS
    # Newest first: exp-0 was created most recently.
    assert entries[0]["title"] == "exp-0"


# ── near-duplicate advisories ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_near_duplicate_advisory_for_matching_pending_command(db_session):
    project = await _make_project(db_session)
    now = _utcnow()
    prior = await _make_compute_task(
        db_session,
        project.id,
        title="bench baseline",
        command="python bench.py --envs 4096 --backend mjx",
        status=TaskStatus.COMPLETED,
        compute_result={"outcome": "SUCCESS", "final_cost_usd": 0.9},
        created_at=now - timedelta(hours=3),
    )
    # Same command modulo whitespace → fingerprint match.
    await _make_compute_task(
        db_session,
        project.id,
        title="bench re-run",
        command="python  bench.py   --envs 4096 --backend mjx",
        status=TaskStatus.QUEUED,
        created_at=now,
    )
    # Unrelated pending experiment → no advisory.
    await _make_compute_task(
        db_session,
        project.id,
        title="unrelated",
        command="python other_thing.py --mode full",
        status=TaskStatus.QUEUED,
        created_at=now,
    )

    entries = await build_experiment_ledger(db_session, project.id)
    pending = await fetch_pending_experiment_specs(db_session, project.id)
    advisories = build_near_duplicate_advisories(pending, entries)

    assert len(advisories) == 1
    assert "NEAR-DUPLICATE" in advisories[0]
    assert str(prior.id)[:8] in advisories[0]
    assert "outcome=SUCCESS" in advisories[0]


def test_near_duplicate_ignores_short_containment():
    entries = [
        {
            "task_id": "a" * 32,
            "outcome": "SUCCESS",
            "command_fingerprint": command_fingerprint("ls"),
            "normalized_command": "ls",
            "hypothesis": "",
        }
    ]
    pending = [{"task_id": "b" * 32, "title": "t", "command": "ls -la", "hypothesis": ""}]
    # "ls" ⊂ "ls -la" but both are far below the containment floor → no advisory
    # (fingerprints differ, so only containment could match).
    assert build_near_duplicate_advisories(pending, entries) == []


# ── shared budget: devices > ledger > advisories ─────────────────────────────


def _sample_devices(n=3):
    return [
        {
            "runner_name": f"node-{i}",
            "gpu_count": 1,
            "gpu_models": ["RTX 3060"],
            "container_ok": True,
            "gpu_in_container_ok": False,
            "active_tasks": 0,
            "active_compute_jobs": 0,
        }
        for i in range(n)
    ]


def test_budget_generous_keeps_everything():
    devices = _sample_devices()
    ledger = [f"- line {i} " + "x" * 40 for i in range(5)]
    advisories = ["- NEAR-DUPLICATE: something"]
    kept_devices, kept_ledger, kept_advisories = apply_experiment_authoring_budget(
        devices, ledger, advisories, budget_chars=10_000
    )
    assert kept_devices == devices
    assert kept_ledger == ledger
    assert kept_advisories == advisories


def test_budget_drops_advisories_then_oldest_ledger_lines_before_devices():
    devices = _sample_devices(2)
    ledger = [f"- line {i} " + "x" * 90 for i in range(10)]
    advisories = ["- NEAR-DUPLICATE: " + "y" * 120]
    # Budget sized so devices fit and only a few ledger lines do — advisories
    # (lowest priority) must vanish and ledger must drop from the OLD end.
    import json

    devices_chars = len(json.dumps(devices, separators=(",", ":")))
    budget = devices_chars + 3 * (len(ledger[0]) + 1) + 10
    kept_devices, kept_ledger, kept_advisories = apply_experiment_authoring_budget(
        devices, ledger, advisories, budget_chars=budget
    )
    assert kept_devices == devices  # highest priority: untouched
    assert kept_advisories == []  # lowest priority: dropped first
    assert 0 < len(kept_ledger) < len(ledger)
    assert kept_ledger == ledger[: len(kept_ledger)]  # newest-first head kept


def test_budget_trims_devices_from_tail_under_extreme_pressure():
    devices = _sample_devices(5)
    # Long lower-priority lines so nothing but (trimmed) devices can fit.
    kept_devices, kept_ledger, kept_advisories = apply_experiment_authoring_budget(
        devices, ["- ledger " + "x" * 250], ["- advisory " + "y" * 250], budget_chars=300
    )
    assert 0 < len(kept_devices) < len(devices)
    assert kept_devices == devices[: len(kept_devices)]
    assert kept_ledger == []
    assert kept_advisories == []


# ── prompt block rendering ───────────────────────────────────────────────────


def test_render_prompt_block_empty_when_nothing_to_show():
    assert render_experiment_ledger_prompt_block([], []) == ""


def test_render_prompt_block_sections():
    block = render_experiment_ledger_prompt_block(
        ['- [2026-07-10] 1a2b3c4d "t" | outcome=SUCCESS'],
        ["- NEAR-DUPLICATE: pending matches prior"],
    )
    assert block.startswith("## Experiment ledger")
    assert "### Near-duplicate advisories" in block
    assert "do not re-run a settled hypothesis" in block
