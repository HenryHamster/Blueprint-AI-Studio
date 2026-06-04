"""Tests for constants and helper methods defined in app.models."""

import uuid
from datetime import datetime, timedelta

import pytest

from app.models import (
    CYCLE_META_MEMOS_AVAILABLE,
    CYCLE_META_MEMOS_CITED,
    ComputeJob,
    ComputeJobStatus,
    MaxPlanAccount,
    Project,
    StrategicCycle,
    Task,
    get_task_current_attempt_vm_cost_usd,
    get_task_total_vm_cost_usd_or_none,
)

_PROJECT_ID = uuid.uuid4()


def _make_cycle(cycle_metadata=None):
    return StrategicCycle(
        id=str(uuid.uuid4()),
        project_id=_PROJECT_ID,
        cycle_metadata=cycle_metadata if cycle_metadata is not None else {},
    )


def test_cycle_meta_constants_have_expected_values():
    assert CYCLE_META_MEMOS_AVAILABLE == "memos_available"
    assert CYCLE_META_MEMOS_CITED == "memos_cited"


def test_compute_job_table_metadata_tracks_budgeted_compute_lifecycle():
    table = ComputeJob.__table__

    assert table.name == "computejob"
    assert set(table.columns) >= {
        table.c.id,
        table.c.project_id,
        table.c.task_id,
        table.c.backend,
        table.c.external_compute_id,
        table.c.machine_type,
        table.c.status,
        table.c.terminal_reason,
        table.c.outcome,
        table.c.spend_cap_usd,
        table.c.wall_clock_deadline,
        table.c.accrued_cost_usd,
        table.c.final_cost_usd,
        table.c.result_reference,
        table.c.artifact_id,
        table.c.polling_metadata,
    }
    assert table.c.project_id.foreign_keys
    assert table.c.task_id.foreign_keys
    assert table.c.artifact_id.foreign_keys

    # ComputeJob is durable history decoupled from Task: the task and artifact
    # links must DETACH (SET NULL) on parent delete, never block or cascade, so
    # bulk task cleanup (reset_project_admin) cannot FK-violate and job history
    # survives. Mirrors Alert.task_id / SystemMaturity.source_task_id.
    task_fk = next(iter(table.c.task_id.foreign_keys))
    artifact_fk = next(iter(table.c.artifact_id.foreign_keys))
    assert task_fk.ondelete == "SET NULL"
    assert artifact_fk.ondelete == "SET NULL"

    assert table.c.status.default is None
    assert str(table.c.status.server_default.arg) == ComputeJobStatus.QUEUED.value

    index_names = {idx.name for idx in table.indexes}
    assert {
        "ix_computejob_project_id",
        "ix_computejob_task_id",
        "ix_computejob_artifact_id",
        "ix_computejob_project_status",
        "ix_computejob_task_status",
        "ix_computejob_backend_external",
        "ix_computejob_status_deadline",
        "ix_computejob_next_poll",
    } <= index_names


def test_memo_stats_returns_zeros_when_metadata_empty():
    cycle = _make_cycle({})
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 0
    assert stats[CYCLE_META_MEMOS_CITED] == 0


def test_memo_stats_returns_zeros_when_metadata_none():
    cycle = _make_cycle(None)
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 0
    assert stats[CYCLE_META_MEMOS_CITED] == 0


def test_memo_stats_reads_values_from_metadata():
    cycle = _make_cycle(
        {
            CYCLE_META_MEMOS_AVAILABLE: 7,
            CYCLE_META_MEMOS_CITED: 3,
        }
    )
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 7
    assert stats[CYCLE_META_MEMOS_CITED] == 3


def test_memo_stats_coerces_to_int():
    cycle = _make_cycle(
        {
            CYCLE_META_MEMOS_AVAILABLE: "4",
            CYCLE_META_MEMOS_CITED: "1",
        }
    )
    stats = cycle.memo_stats()
    assert stats[CYCLE_META_MEMOS_AVAILABLE] == 4
    assert stats[CYCLE_META_MEMOS_CITED] == 1


_T0 = datetime(2026, 1, 1, 12, 0, 0)


# ---------------------------------------------------------------------------
# MaxPlanAccount.is_in_rate_limit_cooldown — persisted-state check
# ---------------------------------------------------------------------------


def _make_account(rate_limit_state=None):
    return MaxPlanAccount(
        user_id="test-user",
        name="test-account",
        rate_limit_state=rate_limit_state,
    )


def test_is_in_rate_limit_cooldown_no_state():
    acct = _make_account(rate_limit_state=None)
    assert acct.is_in_rate_limit_cooldown() is False


def test_is_in_rate_limit_cooldown_empty_disabled_models():
    acct = _make_account(rate_limit_state={"disabled_models": {}})
    assert acct.is_in_rate_limit_cooldown() is False


def test_is_in_rate_limit_cooldown_active_entry():
    """An entry disabled 5 minutes ago (within 4-hour window) → True."""
    at = (datetime.utcnow() - timedelta(minutes=5)).isoformat()
    acct = _make_account(
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {"at": at, "hits": 1, "kind": "rate_limit"}
            }
        }
    )
    assert acct.is_in_rate_limit_cooldown() is True


def test_is_in_rate_limit_cooldown_expired_entry():
    """An entry disabled 5 hours ago (beyond 4-hour window) → False."""
    at = (datetime.utcnow() - timedelta(hours=5)).isoformat()
    acct = _make_account(
        rate_limit_state={
            "disabled_models": {
                "claude-opus-4-7": {"at": at, "hits": 2, "kind": "rate_limit"}
            }
        }
    )
    assert acct.is_in_rate_limit_cooldown() is False


def test_is_in_rate_limit_cooldown_malformed_at_field():
    """Unparseable at string → False (no exception raised)."""
    acct = _make_account(
        rate_limit_state={
            "disabled_models": {"claude-sonnet-4-6": {"at": "not-a-date"}}
        }
    )
    assert acct.is_in_rate_limit_cooldown() is False


def test_is_in_rate_limit_cooldown_at_time_parameter():
    """at_time shifts the reference point: same entry, different at_time → different result."""
    disabled_at = datetime(2026, 1, 1, 12, 0, 0)  # noon
    at_str = disabled_at.isoformat()
    acct = _make_account(
        rate_limit_state={
            "disabled_models": {
                "claude-sonnet-4-6": {"at": at_str, "hits": 1, "kind": "rate_limit"}
            }
        }
    )
    # hits=1 → exponential = 30 minutes cooldown
    # reference 10 minutes after disable → still in cooldown
    inside_window = disabled_at + timedelta(minutes=10)
    assert acct.is_in_rate_limit_cooldown(at_time=inside_window) is True
    # reference 31 minutes after disable → cooldown expired
    after_window = disabled_at + timedelta(minutes=31)
    assert acct.is_in_rate_limit_cooldown(at_time=after_window) is False


def test_is_in_rate_limit_cooldown_quota_exhausted_uses_24h():
    """quota_exhausted kind uses 24-hour flat window, not exponential."""
    disabled_at = datetime(2026, 1, 1, 12, 0, 0)
    at_str = disabled_at.isoformat()
    acct = _make_account(
        rate_limit_state={
            "disabled_models": {
                "claude-opus-4-7": {
                    "at": at_str,
                    "hits": 1,
                    "kind": "quota_exhausted",
                }
            }
        }
    )
    # 5 hours after — still in 24-hour window
    assert (
        acct.is_in_rate_limit_cooldown(at_time=disabled_at + timedelta(hours=5)) is True
    )
    # 25 hours after — window expired
    assert (
        acct.is_in_rate_limit_cooldown(at_time=disabled_at + timedelta(hours=25))
        is False
    )


# task_branch_prefix (project-level branch namespace)


def _make_project(build_config=None):
    return Project(name="Test", user_id="user1", build_config=build_config)


def test_task_branch_prefix_defaults_to_blueprint():
    assert _make_project().task_branch_prefix == "blueprint/"


def test_task_branch_prefix_reads_from_build_config():
    project = _make_project(build_config={"task_branch_prefix": "acme/"})
    assert project.task_branch_prefix == "acme/"


def test_task_branch_prefix_falls_back_when_build_config_is_none():
    assert _make_project(build_config=None).task_branch_prefix == "blueprint/"


def test_task_branch_prefix_falls_back_when_key_absent_in_build_config():
    project = _make_project(build_config={"other_key": "value"})
    assert project.task_branch_prefix == "blueprint/"


def test_task_branch_prefix_derives_from_task_branch_namespace():
    project = _make_project(build_config={"task_branch_namespace": "autoproj"})
    assert project.task_branch_prefix == "autoproj/"


def test_task_branch_prefix_explicit_override_wins_over_namespace_derivation():
    project = _make_project(
        build_config={
            "task_branch_namespace": "autoproj",
            "task_branch_prefix": "legacy/",
        }
    )
    assert project.task_branch_prefix == "legacy/"


# is_staging_branch (per-project intermediate staging branch predicate)


def test_is_staging_branch_default_prefixes_research():
    assert _make_project().is_staging_branch("research/cycle-1") is True


def test_is_staging_branch_default_prefixes_plan():
    assert _make_project().is_staging_branch("plan/cycle-1") is True


def test_is_staging_branch_default_prefixes_non_staging():
    assert _make_project().is_staging_branch("blueprint/task-abc123") is False
    assert _make_project().is_staging_branch("main") is False


def test_is_staging_branch_custom_prefixes_recognized():
    project = _make_project(
        build_config={"staging_branch_prefixes": ["feature/", "wip/"]}
    )
    assert project.is_staging_branch("feature/foo") is True
    assert project.is_staging_branch("wip/bar") is True


def test_is_staging_branch_custom_prefixes_reject_default_research():
    project = _make_project(
        build_config={"staging_branch_prefixes": ["feature/", "wip/"]}
    )
    # With custom prefixes configured, the Blueprint default 'research/' is no
    # longer a staging branch for this project.
    assert project.is_staging_branch("research/cycle-1") is False


def test_is_staging_branch_empty_or_none_branch():
    project = _make_project()
    assert project.is_staging_branch("") is False
    assert project.is_staging_branch(None) is False


# ---------------------------------------------------------------------------
# get_task_total_vm_cost_usd_or_none — canonical admin-facing cost helper
# ---------------------------------------------------------------------------

_PROJ_ID = uuid.uuid4()
_T_START = datetime(2026, 1, 1, 10, 0, 0)
_T_STOP = datetime(2026, 1, 1, 10, 0, 30)  # 30s later


def _make_task(**kwargs) -> Task:
    return Task(
        project_id=_PROJ_ID,
        title="t",
        **kwargs,
    )


def test_get_task_total_vm_cost_or_none_returns_none_for_never_ran_task():
    """Task with no provisioning and no banked cost → None."""
    task = _make_task()
    assert get_task_total_vm_cost_usd_or_none(task) is None


def test_get_task_total_vm_cost_or_none_returns_current_attempt_cost():
    """Task with only a current provisioning window → computes cost from it."""
    task = _make_task(vm_provisioned_at=_T_START, vm_stopped_at=_T_STOP, vm_spot=False)
    result = get_task_total_vm_cost_usd_or_none(task)
    assert result is not None
    assert result > 0.0


def test_get_task_current_attempt_vm_cost_uses_task_machine_type():
    """Non-default machine types should price from their own rate entry."""
    task = _make_task(
        vm_provisioned_at=_T_START,
        vm_stopped_at=_T_STOP,
        vm_spot=False,
        vm_machine_type="e2-standard-4",
    )
    result = get_task_current_attempt_vm_cost_usd(task)
    assert result == pytest.approx(0.134 * 30 / 3600)


def test_get_task_total_vm_cost_or_none_returns_banked_only_for_retried_task():
    """Retried task: vm_provisioned_at cleared after banking; banked cost returned."""
    task = _make_task(vm_provisioned_at=None, vm_stopped_at=None)
    task.vm_runtime_cost_banked_db = 2.5
    result = get_task_total_vm_cost_usd_or_none(task)
    assert result == 2.5


def test_get_task_total_vm_cost_or_none_sums_banked_and_current():
    """Task with both banked retries and an active/final attempt → sums both."""
    task = _make_task(vm_provisioned_at=_T_START, vm_stopped_at=_T_STOP, vm_spot=False)
    task.vm_runtime_cost_banked_db = 1.0
    result = get_task_total_vm_cost_usd_or_none(task)
    assert result is not None
    assert result > 1.0  # banked + current-attempt cost


def test_get_task_total_vm_cost_or_none_banked_zero_with_no_provisioning_is_none():
    """Banked value of zero with no provisioning window: treat as never-ran → None."""
    task = _make_task()
    task.vm_runtime_cost_banked_db = 0.0
    assert get_task_total_vm_cost_usd_or_none(task) is None
