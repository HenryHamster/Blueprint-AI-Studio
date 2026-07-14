"""Tests for per-project resource envelope: schema validation, resolvers, and spend helpers."""

from datetime import timedelta

import pytest

from pydantic import ValidationError

from app.schemas import BuildConfig, ResourceEnvelope
from app.resource_envelope import (
    resolve_project_hardware_policy,
    resolve_project_vm_cap,
    resolve_project_spend_ceiling,
    resolve_project_spend_window_days,
    get_project_recent_spend_usd,
)
from testing.factories import make_project, make_task, _utcnow


# ---------------------------------------------------------------------------
# ResourceEnvelope schema validation
# ---------------------------------------------------------------------------


def test_resource_envelope_valid_full():
    env = ResourceEnvelope(
        max_concurrent_vms=5, spend_ceiling_usd=100.0, spend_window_days=7
    )
    assert env.max_concurrent_vms == 5
    assert env.spend_ceiling_usd == 100.0
    assert env.spend_window_days == 7


def test_resource_envelope_defaults():
    env = ResourceEnvelope()
    assert env.max_concurrent_vms is None
    assert env.spend_ceiling_usd is None
    assert env.spend_window_days == 30


def test_resource_envelope_null_fields():
    env = ResourceEnvelope(max_concurrent_vms=None, spend_ceiling_usd=None)
    assert env.max_concurrent_vms is None
    assert env.spend_ceiling_usd is None


def test_resource_envelope_rejects_zero_max_concurrent_vms():
    with pytest.raises(ValidationError):
        ResourceEnvelope(max_concurrent_vms=0)


def test_resource_envelope_rejects_negative_max_concurrent_vms():
    with pytest.raises(ValidationError):
        ResourceEnvelope(max_concurrent_vms=-1)


def test_resource_envelope_rejects_zero_spend_ceiling():
    with pytest.raises(ValidationError):
        ResourceEnvelope(spend_ceiling_usd=0.0)


def test_resource_envelope_rejects_negative_spend_ceiling():
    with pytest.raises(ValidationError):
        ResourceEnvelope(spend_ceiling_usd=-5.0)


def test_resource_envelope_rejects_zero_spend_window_days():
    with pytest.raises(ValidationError):
        ResourceEnvelope(spend_window_days=0)


def test_resource_envelope_rejects_negative_spend_window_days():
    with pytest.raises(ValidationError):
        ResourceEnvelope(spend_window_days=-1)


def test_resource_envelope_rejects_non_numeric_max_concurrent_vms():
    with pytest.raises(ValidationError):
        ResourceEnvelope(max_concurrent_vms="five")


def test_resource_envelope_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ResourceEnvelope(unknown_field=42)


# ---------------------------------------------------------------------------
# BuildConfig round-trips with resource_envelope
# ---------------------------------------------------------------------------


def test_build_config_without_resource_envelope_unchanged():
    bc = BuildConfig(language="python")
    assert bc.resource_envelope is None
    dumped = bc.model_dump(exclude_none=True)
    assert "resource_envelope" not in dumped


def test_build_config_with_resource_envelope_round_trips():
    bc = BuildConfig(
        language="python",
        resource_envelope=ResourceEnvelope(
            max_concurrent_vms=3, spend_ceiling_usd=50.0, spend_window_days=14
        ),
    )
    assert bc.resource_envelope is not None
    assert bc.resource_envelope.max_concurrent_vms == 3
    dumped = bc.model_dump(exclude_none=True)
    assert "resource_envelope" in dumped
    assert dumped["resource_envelope"]["max_concurrent_vms"] == 3
    assert dumped["resource_envelope"]["spend_ceiling_usd"] == 50.0


def test_build_config_resource_envelope_all_none_excluded():
    """A ResourceEnvelope with all-None fields should be excluded by exclude_none=True on BuildConfig."""
    bc = BuildConfig(resource_envelope=ResourceEnvelope())
    dumped = bc.model_dump(exclude_none=True)
    # The nested envelope object itself is non-None, so it appears, but its
    # None sub-fields are stripped.
    if "resource_envelope" in dumped:
        assert "max_concurrent_vms" not in dumped["resource_envelope"]
        assert "spend_ceiling_usd" not in dumped["resource_envelope"]


def test_build_config_resource_envelope_from_dict():
    """Validate that BuildConfig accepts resource_envelope as a nested dict (JSON deserialization path)."""
    bc = BuildConfig.model_validate(
        {
            "language": "python",
            "resource_envelope": {"max_concurrent_vms": 2, "spend_window_days": 30},
        }
    )
    assert bc.resource_envelope is not None
    assert bc.resource_envelope.max_concurrent_vms == 2


def test_build_config_resource_envelope_rejects_invalid_nested():
    with pytest.raises(ValidationError):
        BuildConfig(resource_envelope={"max_concurrent_vms": 0})


# ---------------------------------------------------------------------------
# apply_operator_build_config_overrides carry-forward
# ---------------------------------------------------------------------------


def test_apply_operator_overrides_carries_resource_envelope():
    """resource_envelope is preserved across re-detect."""
    from app.build_config_detection import apply_operator_build_config_overrides

    existing = {
        "resource_envelope": {"max_concurrent_vms": 5, "spend_ceiling_usd": 200.0}
    }
    detected = {"language": "python"}
    result = apply_operator_build_config_overrides(detected, existing)
    assert result["resource_envelope"] == {
        "max_concurrent_vms": 5,
        "spend_ceiling_usd": 200.0,
    }


def test_apply_operator_overrides_no_envelope_when_absent():
    """resource_envelope is not injected when absent from existing config."""
    from app.build_config_detection import apply_operator_build_config_overrides

    existing = {"max_parallel_research": 3}
    detected = {"language": "python"}
    result = apply_operator_build_config_overrides(detected, existing)
    assert "resource_envelope" not in result


# ---------------------------------------------------------------------------
# resolve_project_vm_cap
# ---------------------------------------------------------------------------


def test_resolve_project_vm_cap_returns_configured_value():
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": 4}}
    )
    assert resolve_project_vm_cap(project) == 4


def test_resolve_project_vm_cap_returns_none_when_absent():
    project = make_project(build_config={})
    assert resolve_project_vm_cap(project) is None


def test_resolve_project_vm_cap_returns_none_when_null():
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": None}}
    )
    assert resolve_project_vm_cap(project) is None


def test_resolve_project_vm_cap_returns_none_no_build_config():
    project = make_project(build_config=None)
    assert resolve_project_vm_cap(project) is None


def test_resolve_project_vm_cap_tolerates_junk_string():
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": "ten"}}
    )
    assert resolve_project_vm_cap(project) is None


def test_resolve_project_vm_cap_tolerates_junk_zero():
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": 0}}
    )
    assert resolve_project_vm_cap(project) is None


def test_resolve_project_vm_cap_tolerates_junk_negative():
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": -3}}
    )
    assert resolve_project_vm_cap(project) is None


def test_resolve_project_vm_cap_tolerates_bool():
    project = make_project(
        build_config={"resource_envelope": {"max_concurrent_vms": True}}
    )
    assert resolve_project_vm_cap(project) is None


def test_resolve_project_vm_cap_tolerates_non_dict_envelope():
    project = make_project(build_config={"resource_envelope": "bad-value"})
    assert resolve_project_vm_cap(project) is None


# ---------------------------------------------------------------------------
# resolve_project_spend_ceiling
# ---------------------------------------------------------------------------


def test_resolve_project_spend_ceiling_returns_configured_value():
    project = make_project(
        build_config={"resource_envelope": {"spend_ceiling_usd": 100.0}}
    )
    assert resolve_project_spend_ceiling(project) == 100.0


def test_resolve_project_spend_ceiling_returns_none_when_absent():
    project = make_project(build_config={})
    assert resolve_project_spend_ceiling(project) is None


def test_resolve_project_spend_ceiling_returns_none_when_null():
    project = make_project(
        build_config={"resource_envelope": {"spend_ceiling_usd": None}}
    )
    assert resolve_project_spend_ceiling(project) is None


def test_resolve_project_spend_ceiling_returns_none_no_build_config():
    project = make_project(build_config=None)
    assert resolve_project_spend_ceiling(project) is None


def test_resolve_project_spend_ceiling_tolerates_zero():
    project = make_project(
        build_config={"resource_envelope": {"spend_ceiling_usd": 0.0}}
    )
    assert resolve_project_spend_ceiling(project) is None


def test_resolve_project_spend_ceiling_tolerates_negative():
    project = make_project(
        build_config={"resource_envelope": {"spend_ceiling_usd": -10.0}}
    )
    assert resolve_project_spend_ceiling(project) is None


def test_resolve_project_spend_ceiling_tolerates_string():
    project = make_project(
        build_config={"resource_envelope": {"spend_ceiling_usd": "fifty"}}
    )
    assert resolve_project_spend_ceiling(project) is None


def test_resolve_project_spend_ceiling_accepts_int():
    """Integer spend ceiling is coerced to float."""
    project = make_project(
        build_config={"resource_envelope": {"spend_ceiling_usd": 75}}
    )
    result = resolve_project_spend_ceiling(project)
    assert result == 75.0
    assert isinstance(result, float)


# ---------------------------------------------------------------------------
# resolve_project_spend_window_days
# ---------------------------------------------------------------------------


def test_resolve_project_spend_window_days_returns_configured_value():
    project = make_project(
        build_config={"resource_envelope": {"spend_window_days": 14}}
    )
    assert resolve_project_spend_window_days(project) == 14


def test_resolve_project_spend_window_days_defaults_when_absent():
    project = make_project(build_config={})
    assert resolve_project_spend_window_days(project) == 30


def test_resolve_project_spend_window_days_tolerates_junk():
    project = make_project(
        build_config={"resource_envelope": {"spend_window_days": "bad"}}
    )
    assert resolve_project_spend_window_days(project) == 30


# ---------------------------------------------------------------------------
# get_project_recent_spend_usd — DB-backed tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_empty(db_session):
    """Returns 0.0 when there are no jobs or tasks."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )
    assert result["total"] == 0.0
    assert result["compute"] == 0.0
    assert result["agent_vm"] == 0.0
    assert result["llm"] == 0.0
    assert result["idle"] == 0.0


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_sums_task_vm_cost(db_session):
    """Includes banked VM cost from tasks updated within the window."""
    from app.models import TaskStatus

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_runtime_cost_banked_db=1.5,
        updated_at=now - timedelta(days=1),
    )
    db_session.add(task)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )
    assert result["total"] == pytest.approx(1.5, abs=1e-6)
    assert result["agent_vm"] == pytest.approx(1.5, abs=1e-6)
    assert result["llm"] == pytest.approx(0.0, abs=1e-6)
    assert result["compute"] == pytest.approx(0.0, abs=1e-6)
    assert result["idle"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_excludes_old_tasks(db_session):
    """Tasks updated outside the window do not contribute to spend."""
    from app.models import TaskStatus

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    old_task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_runtime_cost_banked_db=9.99,
        updated_at=now - timedelta(days=60),
        created_at=now - timedelta(days=60),
    )
    db_session.add(old_task)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )
    assert result["total"] == 0.0
    assert result["compute"] == 0.0
    assert result["agent_vm"] == 0.0
    assert result["llm"] == 0.0
    assert result["idle"] == 0.0


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_sums_compute_job_cost(db_session):
    """Includes final_cost_usd from terminal compute jobs within the window."""
    from app.models import ComputeJob, ComputeJobStatus, ComputeJobOutcome, TaskStatus

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        spend_cap_usd=10.0,
        final_cost_usd=2.5,
        completed_at=now - timedelta(days=2),
    )
    db_session.add(job)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )
    assert result["total"] == pytest.approx(2.5, abs=1e-6)
    assert result["compute"] == pytest.approx(2.5, abs=1e-6)
    assert result["agent_vm"] == pytest.approx(0.0, abs=1e-6)
    assert result["llm"] == pytest.approx(0.0, abs=1e-6)
    assert result["idle"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_combines_both_sources(db_session):
    """Sums compute-job spend and task VM cost together."""
    from app.models import ComputeJob, ComputeJobStatus, ComputeJobOutcome, TaskStatus

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_runtime_cost_banked_db=1.25,
        updated_at=now - timedelta(days=1),
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        spend_cap_usd=10.0,
        final_cost_usd=3.75,
        completed_at=now - timedelta(days=2),
    )
    db_session.add(job)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )
    assert result["total"] == pytest.approx(5.0, abs=1e-6)
    assert result["compute"] == pytest.approx(3.75, abs=1e-6)
    assert result["agent_vm"] == pytest.approx(1.25, abs=1e-6)
    assert result["llm"] == pytest.approx(0.0, abs=1e-6)
    assert result["idle"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_scoped_to_project(db_session):
    """Spend from a different project does not leak into the result."""
    from app.models import ComputeJob, ComputeJobStatus, ComputeJobOutcome, TaskStatus

    project_a = make_project(name="Project A")
    project_b = make_project(name="Project B")
    db_session.add(project_a)
    db_session.add(project_b)
    await db_session.commit()
    await db_session.refresh(project_a)
    await db_session.refresh(project_b)

    now = _utcnow()
    task_b = make_task(
        project_id=project_b.id,
        status=TaskStatus.COMPLETED,
        vm_runtime_cost_banked_db=50.0,
        updated_at=now - timedelta(days=1),
    )
    db_session.add(task_b)
    await db_session.commit()
    await db_session.refresh(task_b)

    job_b = ComputeJob(
        project_id=project_b.id,
        task_id=task_b.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        spend_cap_usd=100.0,
        final_cost_usd=20.0,
        completed_at=now - timedelta(days=1),
    )
    db_session.add(job_b)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project_a.id, window_days=30
    )
    assert result["total"] == 0.0
    assert result["compute"] == 0.0
    assert result["agent_vm"] == 0.0
    assert result["llm"] == 0.0
    assert result["idle"] == 0.0


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_includes_pool_idle_cost(db_session):
    """Warm-pool idle cost (PoolIdleSpan within window) is included in the total."""
    from app.models import PoolIdleSpan

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    span = PoolIdleSpan(
        project_id=project.id,
        vm_instance_id="test-vm-idle-001",
        started_at=now - timedelta(days=2, hours=1),
        settled_at=now - timedelta(days=2),
        cost_usd=3.0,
    )
    db_session.add(span)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )
    assert result["total"] == pytest.approx(3.0, abs=1e-6)
    assert result["idle"] == pytest.approx(3.0, abs=1e-6)
    assert result["llm"] == pytest.approx(0.0, abs=1e-6)
    assert result["compute"] == pytest.approx(0.0, abs=1e-6)
    assert result["agent_vm"] == pytest.approx(0.0, abs=1e-6)


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_excludes_old_pool_idle_cost(db_session):
    """PoolIdleSpan rows with settled_at before the window are excluded."""
    from app.models import PoolIdleSpan

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    old_span = PoolIdleSpan(
        project_id=project.id,
        vm_instance_id="test-vm-idle-old",
        started_at=now - timedelta(days=45),
        settled_at=now - timedelta(days=40),
        cost_usd=99.0,
    )
    db_session.add(old_span)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )
    assert result["total"] == 0.0
    assert result["compute"] == 0.0
    assert result["agent_vm"] == 0.0
    assert result["llm"] == 0.0
    assert result["idle"] == 0.0


@pytest.mark.asyncio
async def test_get_project_recent_spend_usd_combines_all_three_sources(db_session):
    """Sums compute-job spend, task VM cost, and pool idle cost together."""
    from app.models import (
        ComputeJob,
        ComputeJobStatus,
        ComputeJobOutcome,
        PoolIdleSpan,
        TaskStatus,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        vm_runtime_cost_banked_db=1.0,
        updated_at=now - timedelta(days=1),
    )
    db_session.add(task)
    await db_session.commit()
    await db_session.refresh(task)

    job = ComputeJob(
        project_id=project.id,
        task_id=task.id,
        backend="local-subprocess",
        status=ComputeJobStatus.SUCCEEDED,
        outcome=ComputeJobOutcome.SUCCESS,
        spend_cap_usd=10.0,
        final_cost_usd=2.0,
        completed_at=now - timedelta(days=2),
    )
    db_session.add(job)

    span = PoolIdleSpan(
        project_id=project.id,
        vm_instance_id="test-vm-idle-combined",
        started_at=now - timedelta(days=3, hours=1),
        settled_at=now - timedelta(days=3),
        cost_usd=4.0,
    )
    db_session.add(span)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )
    # task VM (1.0) + compute job (2.0) + pool idle (4.0) = 7.0
    assert result["total"] == pytest.approx(7.0, abs=1e-6)
    assert result["compute"] == pytest.approx(2.0, abs=1e-6)
    assert result["agent_vm"] == pytest.approx(1.0, abs=1e-6)
    assert result["llm"] == pytest.approx(0.0, abs=1e-6)
    assert result["idle"] == pytest.approx(4.0, abs=1e-6)


# ---------------------------------------------------------------------------
# ResourceEnvelope hardware policy field validation
# ---------------------------------------------------------------------------


def test_resource_envelope_allowed_machine_types_valid():
    env = ResourceEnvelope(allowed_machine_types=["e2-standard-2", "e2-standard-4"])
    assert env.allowed_machine_types == ["e2-standard-2", "e2-standard-4"]


def test_resource_envelope_allowed_machine_types_rejects_unknown():
    with pytest.raises(ValidationError) as exc_info:
        ResourceEnvelope(allowed_machine_types=["e2-standard-2", "n1-standard-4"])
    assert "n1-standard-4" in str(exc_info.value)


def test_resource_envelope_allowed_machine_types_empty_list_accepted():
    env = ResourceEnvelope(allowed_machine_types=[])
    assert env.allowed_machine_types == []


def test_resource_envelope_allowed_machine_types_none_accepted():
    env = ResourceEnvelope(allowed_machine_types=None)
    assert env.allowed_machine_types is None


def test_resource_envelope_allowed_accelerator_types_valid():
    env = ResourceEnvelope(allowed_accelerator_types=["T4", "L4"])
    assert env.allowed_accelerator_types == ["T4", "L4"]


def test_resource_envelope_allowed_accelerator_types_rejects_unknown():
    with pytest.raises(ValidationError) as exc_info:
        ResourceEnvelope(allowed_accelerator_types=["T4", "A100"])
    assert "A100" in str(exc_info.value)


def test_resource_envelope_allowed_accelerator_types_empty_list_accepted():
    env = ResourceEnvelope(allowed_accelerator_types=[])
    assert env.allowed_accelerator_types == []


def test_resource_envelope_allowed_accelerator_types_none_accepted():
    env = ResourceEnvelope(allowed_accelerator_types=None)
    assert env.allowed_accelerator_types is None


# ---------------------------------------------------------------------------
# resolve_project_hardware_policy
# ---------------------------------------------------------------------------


def test_resolve_project_hardware_policy_returns_none_when_absent():
    project = make_project(build_config={})
    machines, accels = resolve_project_hardware_policy(project)
    assert machines is None
    assert accels is None


def test_resolve_project_hardware_policy_returns_lists_when_set():
    project = make_project(
        build_config={
            "resource_envelope": {
                "allowed_machine_types": ["e2-standard-2"],
                "allowed_accelerator_types": ["T4", "L4"],
            }
        }
    )
    machines, accels = resolve_project_hardware_policy(project)
    assert machines == ["e2-standard-2"]
    assert accels == ["T4", "L4"]


def test_resolve_project_hardware_policy_returns_none_no_build_config():
    project = make_project(build_config=None)
    machines, accels = resolve_project_hardware_policy(project)
    assert machines is None
    assert accels is None


def test_resolve_project_hardware_policy_tolerates_junk_values():
    project = make_project(
        build_config={
            "resource_envelope": {
                "allowed_machine_types": "bad",
                "allowed_accelerator_types": 42,
            }
        }
    )
    machines, accels = resolve_project_hardware_policy(project)
    assert machines is None
    assert accels is None


# ---------------------------------------------------------------------------
# ceiling_gate_total — non-Anthropic LLM conservatism buffer
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ceiling_gate_total_applies_conservatism_to_non_anthropic_llm(db_session):
    """ceiling_gate_total inflates non-Anthropic LLM cost by 25%; total is unchanged."""
    from app.models import TaskStatus
    from app.llm_cost_attribution import LLM_TOKEN_PRICING, NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Use deepseek-v4-pro: input_rate=0.435e-6, output_rate=0.87e-6
    input_tokens = 1_000_000
    output_tokens = 500_000
    input_rate, output_rate = LLM_TOKEN_PRICING[("deepseek", "deepseek-v4-pro")]
    expected_llm = input_tokens * input_rate + output_tokens * output_rate

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        provider="deepseek",
        model="deepseek-v4-pro",
        api_call_summary={
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "llm_call_count": 5,
        },
        updated_at=now - timedelta(days=1),
    )
    db_session.add(task)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )

    expected_buffer = (NON_ANTHROPIC_LLM_CONSERVATISM_MULTIPLIER - 1.0) * expected_llm
    assert result["llm"] == pytest.approx(expected_llm, abs=1e-9)
    assert result["total"] == pytest.approx(expected_llm, abs=1e-9)
    assert result["ceiling_gate_total"] == pytest.approx(
        expected_llm + expected_buffer, abs=1e-9
    )
    # ceiling_gate_total must be strictly greater than total when non-Anthropic LLM spend exists
    assert result["ceiling_gate_total"] > result["total"]


@pytest.mark.asyncio
async def test_ceiling_gate_total_unchanged_for_anthropic_llm(db_session):
    """ceiling_gate_total equals total when all LLM cost is Anthropic-attributed."""
    from app.models import TaskStatus
    from app.llm_cost_attribution import LLM_TOKEN_PRICING

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    input_tokens = 1_000_000
    output_tokens = 500_000
    input_rate, output_rate = LLM_TOKEN_PRICING[("anthropic", "claude-sonnet-4-6")]
    expected_llm = input_tokens * input_rate + output_tokens * output_rate

    task = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-sonnet-4-6",
        api_call_summary={
            "total_input_tokens": input_tokens,
            "total_output_tokens": output_tokens,
            "llm_call_count": 5,
        },
        updated_at=now - timedelta(days=1),
    )
    db_session.add(task)
    await db_session.commit()

    result = await get_project_recent_spend_usd(
        db_session, project_id=project.id, window_days=30
    )

    assert result["llm"] == pytest.approx(expected_llm, abs=1e-9)
    assert result["total"] == pytest.approx(expected_llm, abs=1e-9)
    # No buffer applied for Anthropic-attributed LLM cost
    assert result["ceiling_gate_total"] == pytest.approx(result["total"], abs=1e-9)
