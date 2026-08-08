"""Tests for the canonical direction lineage-spend meter.

``app.direction_lineage_spend`` is the ONE summer behind a direction's
``cost_budget_usd`` envelope. These tests pin the properties every consumer
depends on: the per-``provider|model`` breakdown sums exactly to the returned
total, LLM token spend is a real term (not silently zero), banked retry spend is
never dropped, terminal ComputeJob spend is folded in, and the lifetime query
covers tasks the rolling digest window would have excluded.
"""

from datetime import timedelta

import pytest
import sqlalchemy.exc
from sqlmodel import select

from app import direction_lineage_spend
from app.direction_lineage_spend import (
    BASIS_LIFETIME,
    BASIS_WINDOW,
    COMPUTE_SUBSTRATE_BUCKET,
    UNATTRIBUTED_BUCKET,
    load_direction_lineage_spend,
    summarize_task_spend,
)
from app.models import ComputeJob, ComputeJobStatus, Task, TaskStatus
from app.vm_cost_rates import get_vm_rate_per_second
from testing.factories import make_project, make_task, _utcnow

# 1M input @ $5/MTok + 100k output @ $25/MTok on claude-opus-4-7.
_ANTHROPIC_PRICED_USAGE = {
    "total_input_tokens": 1_000_000,
    "total_output_tokens": 100_000,
    "llm_call_count": 7,
}
_EXPECTED_ANTHROPIC_LLM_USD = 1_000_000 * 5e-6 + 100_000 * 25e-6  # $7.50


def _on_demand_vm_usd(seconds: float) -> float:
    return get_vm_rate_per_second(machine_type=None, spot=False) * seconds


def test_summarize_empty_returns_zeroed_lifetime_basis():
    """No tasks is a genuine zero, not a missing measurement."""
    spend = summarize_task_spend([])
    assert spend.total_usd == 0.0
    assert spend.vm_usd == 0.0
    assert spend.llm_usd == 0.0
    assert spend.compute_usd == 0.0
    assert spend.task_count == 0
    assert spend.llm_unpriced is False
    assert spend.by_provider_model == {}
    assert spend.basis == BASIS_LIFETIME


def test_summarize_counts_llm_tokens_as_a_real_term():
    """A task with token usage contributes priced LLM dollars on top of VM cost."""
    now = _utcnow()
    task = make_task(
        title="t",
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-opus-4-7",
        api_call_summary=dict(_ANTHROPIC_PRICED_USAGE),
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now,
        vm_spot=False,
    )

    spend = summarize_task_spend([task])

    expected_vm = _on_demand_vm_usd(3600)
    assert spend.vm_usd == pytest.approx(expected_vm)
    assert spend.llm_usd == pytest.approx(_EXPECTED_ANTHROPIC_LLM_USD)
    assert spend.total_usd == pytest.approx(expected_vm + _EXPECTED_ANTHROPIC_LLM_USD)
    assert spend.task_count == 1
    assert spend.llm_unpriced is False
    # LLM dollars land in the SAME provider|model bucket as the VM dollars.
    assert set(spend.by_provider_model) == {"anthropic|claude-opus-4-7"}
    assert spend.by_provider_model["anthropic|claude-opus-4-7"] == pytest.approx(
        expected_vm + _EXPECTED_ANTHROPIC_LLM_USD
    )


def test_summarize_breakdown_sums_exactly_to_total():
    """The invariant every downstream cost axis relies on, across mixed tasks."""
    now = _utcnow()
    tasks = [
        make_task(
            title="anthropic",
            status=TaskStatus.COMPLETED,
            provider="anthropic",
            model="claude-opus-4-7",
            api_call_summary=dict(_ANTHROPIC_PRICED_USAGE),
            vm_provisioned_at=now - timedelta(hours=2),
            vm_stopped_at=now - timedelta(hours=1),
            vm_spot=False,
        ),
        make_task(
            title="openai",
            status=TaskStatus.FAILED,
            provider="openai",
            model="gpt-5.4",
            api_call_summary={
                "total_input_tokens": 400_000,
                "total_output_tokens": 20_000,
            },
            vm_provisioned_at=now - timedelta(minutes=30),
            vm_stopped_at=now,
            vm_spot=True,
        ),
        make_task(
            title="legacy-no-pair",
            status=TaskStatus.COMPLETED,
            vm_provisioned_at=now - timedelta(minutes=10),
            vm_stopped_at=now,
            vm_spot=True,
        ),
    ]

    spend = summarize_task_spend(tasks)

    assert spend.task_count == 3
    assert set(spend.by_provider_model) == {
        "anthropic|claude-opus-4-7",
        "openai|gpt-5.4",
        UNATTRIBUTED_BUCKET,
    }
    assert sum(spend.by_provider_model.values()) == pytest.approx(spend.total_usd)
    assert spend.total_usd == pytest.approx(spend.vm_usd + spend.llm_usd)


def test_summarize_flags_unpriced_llm_without_inventing_dollars():
    """Tokens on a model with no pricing row set the flag and add $0."""
    now = _utcnow()
    task = make_task(
        title="t",
        status=TaskStatus.COMPLETED,
        provider="acme-labs",
        model="totally-unlisted-model",
        api_call_summary={
            "total_input_tokens": 900_000,
            "total_output_tokens": 90_000,
        },
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now,
        vm_spot=False,
    )

    spend = summarize_task_spend([task])

    assert spend.llm_unpriced is True
    assert spend.llm_usd == 0.0
    assert spend.total_usd == pytest.approx(_on_demand_vm_usd(3600))
    assert sum(spend.by_provider_model.values()) == pytest.approx(spend.total_usd)


def test_summarize_lifetime_folds_banked_column_residual():
    """Legacy rows that banked VM cost on the column keep their dollars.

    ``attempt_history`` accounts for $0.50 while the banked column says $0.80.
    The $0.30 the per-pair helper cannot attribute is folded into the
    unattributed bucket rather than silently dropped from the envelope.
    """
    now = _utcnow()
    task = make_task(
        title="t",
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-opus-4-7",
        # attempt_history / vm_runtime_cost_banked are context-backed
        # properties; the SQLModel constructor takes the persisted *_db columns.
        vm_runtime_cost_banked_db=0.80,
        attempt_history_db=[
            {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "vm_runtime_cost_usd": 0.50,
                "vm_stopped_at": (now - timedelta(days=30)).isoformat(),
            }
        ],
    )

    spend = summarize_task_spend([task])

    assert spend.vm_usd == pytest.approx(0.80)
    assert spend.by_provider_model["anthropic|claude-opus-4-7"] == pytest.approx(0.50)
    assert spend.by_provider_model[UNATTRIBUTED_BUCKET] == pytest.approx(0.30)
    assert sum(spend.by_provider_model.values()) == pytest.approx(spend.total_usd)


def test_summarize_window_start_excludes_older_banked_attempts():
    """A window sum drops banked attempts that stopped before window_start.

    The same tasks summed lifetime-wide keep them — that difference is exactly
    why the badge and the envelope needed separate, explicitly-labelled meters.
    """
    now = _utcnow()
    window_start = now - timedelta(days=1)
    task = make_task(
        title="t",
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-opus-4-7",
        attempt_history_db=[
            {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "vm_runtime_cost_usd": 4.00,
                "vm_stopped_at": (now - timedelta(days=10)).isoformat(),
            },
            {
                "provider": "anthropic",
                "model": "claude-opus-4-7",
                "vm_runtime_cost_usd": 1.00,
                "vm_stopped_at": (now - timedelta(hours=2)).isoformat(),
            },
        ],
    )

    windowed = summarize_task_spend([task], window_start=window_start)
    lifetime = summarize_task_spend([task])

    assert windowed.basis == BASIS_WINDOW
    assert windowed.vm_usd == pytest.approx(1.00)
    assert lifetime.basis == BASIS_LIFETIME
    assert lifetime.vm_usd == pytest.approx(5.00)


def test_as_dict_is_rounded_and_json_safe():
    """The payload view rounds every float and preserves the provenance string."""
    now = _utcnow()
    task = make_task(
        title="t",
        status=TaskStatus.COMPLETED,
        provider="anthropic",
        model="claude-opus-4-7",
        api_call_summary=dict(_ANTHROPIC_PRICED_USAGE),
        vm_provisioned_at=now - timedelta(hours=1),
        vm_stopped_at=now,
    )

    payload = summarize_task_spend([task]).as_dict(digits=2)

    assert payload["llm_usd"] == round(_EXPECTED_ANTHROPIC_LLM_USD, 2)
    assert payload["task_count"] == 1
    assert payload["llm_unpriced"] is False
    assert payload["basis"] == BASIS_LIFETIME
    assert payload["by_provider_model"]["anthropic|claude-opus-4-7"] > 0


@pytest.mark.asyncio
async def test_load_direction_lineage_spend_is_lifetime_and_per_direction(db_session):
    """Lifetime query attributes every lineage task to its own direction.

    The old task predates any rolling digest window by 60 days and still counts.
    A direction with no tasks reports a genuine zero rather than a missing key.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            title="old-but-billed",
            status=TaskStatus.COMPLETED,
            context={"direction_id": "dir-a"},
            provider="anthropic",
            model="claude-opus-4-7",
            api_call_summary=dict(_ANTHROPIC_PRICED_USAGE),
            created_at=now - timedelta(days=60),
            vm_provisioned_at=now - timedelta(days=60, hours=1),
            vm_stopped_at=now - timedelta(days=60),
            vm_spot=False,
        )
    )
    db_session.add(
        make_task(
            project_id=project.id,
            title="other-direction",
            status=TaskStatus.COMPLETED,
            context={"direction_id": "dir-b"},
            provider="openai",
            model="gpt-5.4",
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now,
            vm_spot=False,
        )
    )
    await db_session.commit()

    spend = await load_direction_lineage_spend(
        db_session,
        project_id=project.id,
        direction_ids=["dir-a", "dir-b", "dir-empty"],
    )

    assert set(spend) == {"dir-a", "dir-b", "dir-empty"}

    dir_a = spend["dir-a"]
    assert dir_a.task_count == 1
    assert dir_a.basis == BASIS_LIFETIME
    assert dir_a.llm_usd == pytest.approx(_EXPECTED_ANTHROPIC_LLM_USD)
    assert dir_a.vm_usd == pytest.approx(_on_demand_vm_usd(3600))
    assert dir_a.total_usd == pytest.approx(dir_a.vm_usd + dir_a.llm_usd)
    assert sum(dir_a.by_provider_model.values()) == pytest.approx(dir_a.total_usd)

    dir_b = spend["dir-b"]
    assert dir_b.task_count == 1
    assert dir_b.llm_usd == 0.0
    assert dir_b.vm_usd > 0

    empty = spend["dir-empty"]
    assert empty.task_count == 0
    assert empty.total_usd == 0.0


@pytest.mark.asyncio
async def test_load_direction_lineage_spend_is_scoped_to_its_own_project(db_session):
    """A same-named direction in another project contributes exactly $0.

    Direction ids are project-scoped strings, not globally unique, so without the
    ``Task.project_id`` predicate one project's budget envelope would be paused by
    another project's spend.
    """
    project = make_project()
    other = make_project()
    db_session.add(project)
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(project)
    await db_session.refresh(other)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=other.id,
            title="another project, same direction id",
            status=TaskStatus.COMPLETED,
            context={"direction_id": "dir-shared"},
            provider="anthropic",
            model="claude-opus-4-7",
            api_call_summary=dict(_ANTHROPIC_PRICED_USAGE),
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now,
            vm_spot=False,
        )
    )
    await db_session.commit()

    spend = await load_direction_lineage_spend(
        db_session,
        project_id=project.id,
        direction_ids=["dir-shared"],
    )
    assert spend["dir-shared"].task_count == 0
    assert spend["dir-shared"].total_usd == 0.0
    assert spend["dir-shared"].llm_usd == 0.0

    # Sanity: the stranger task is real and DOES price under its own project.
    stranger = await load_direction_lineage_spend(
        db_session,
        project_id=other.id,
        direction_ids=["dir-shared"],
    )
    assert stranger["dir-shared"].task_count == 1
    assert stranger["dir-shared"].llm_usd == pytest.approx(
        _EXPECTED_ANTHROPIC_LLM_USD
    )


@pytest.mark.asyncio
async def test_load_direction_lineage_spend_folds_terminal_compute_jobs(db_session):
    """Terminal ComputeJob dollars count; non-terminal ones do not yet."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    task = make_task(
        project_id=project.id,
        title="compute-experiment",
        status=TaskStatus.COMPLETED,
        context={"direction_id": "dir-c"},
        payloads=[{"type": "compute_experiment"}],
    )
    db_session.add(task)
    await db_session.commit()

    db_session.add(
        ComputeJob(
            project_id=project.id,
            task_id=task.id,
            backend="test-backend",
            status=ComputeJobStatus.SUCCEEDED,
            final_cost_usd=12.50,
        )
    )
    db_session.add(
        ComputeJob(
            project_id=project.id,
            task_id=task.id,
            backend="test-backend",
            status=ComputeJobStatus.RUNNING,
            final_cost_usd=99.0,
        )
    )
    await db_session.commit()

    spend = await load_direction_lineage_spend(
        db_session, project_id=project.id, direction_ids=["dir-c"]
    )

    dir_c = spend["dir-c"]
    # The monitoring task itself ran no VM, so compute is the whole bill.
    assert dir_c.compute_usd == pytest.approx(12.50)
    assert dir_c.total_usd == pytest.approx(12.50)
    assert dir_c.by_provider_model[COMPUTE_SUBSTRATE_BUCKET] == pytest.approx(12.50)
    assert sum(dir_c.by_provider_model.values()) == pytest.approx(dir_c.total_usd)


@pytest.mark.asyncio
async def test_load_direction_lineage_spend_empty_direction_ids(db_session):
    """No direction ids issues no query and returns an empty mapping."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    assert (
        await load_direction_lineage_spend(
            db_session, project_id=project.id, direction_ids=[]
        )
        == {}
    )


@pytest.mark.asyncio
async def test_load_direction_lineage_spend_projects_columns(db_session):
    """The lifetime scan hydrates ONLY the columns the accumulator reads.

    This runs on every digest request over the whole lineage, so selecting full
    Task rows would (a) drag payloads/status_log/result blobs into memory and
    (b) populate ``vm_machine_type`` — which the digest defers on purpose and
    ``get_task_current_attempt_vm_cost_usd`` probes via ``__dict__.get`` — thereby
    silently repricing window VM cost for directed tasks only.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    db_session.add(
        make_task(
            project_id=project.id,
            title="projected",
            status=TaskStatus.COMPLETED,
            context={"direction_id": "dir-p"},
            payloads=[{"type": "create", "description": "x" * 1000}],
            provider="anthropic",
            model="claude-opus-4-7",
            vm_machine_type="n2-standard-8",
            vm_provisioned_at=now - timedelta(hours=1),
            vm_stopped_at=now,
            vm_spot=False,
        )
    )
    await db_session.commit()
    # Drop the write-side identity map so the query below loads fresh rows.
    db_session.expunge_all()

    spend = await load_direction_lineage_spend(
        db_session, project_id=project.id, direction_ids=["dir-p"]
    )
    assert spend["dir-p"].task_count == 1
    # Default on-demand rate, NOT the n2-standard-8 rate: vm_machine_type stays
    # unloaded, exactly as in the digest's own window queries.
    assert spend["dir-p"].vm_usd == pytest.approx(_on_demand_vm_usd(3600))
    assert spend["dir-p"].truncated is False

    # And directly: the shared projection defers everything the accumulator does
    # not read, with raiseload so a future reader fails loudly instead of
    # emitting a per-row lazy SELECT.
    db_session.expunge_all()
    task = (
        (
            await db_session.execute(
                select(Task)
                .where(Task.project_id == project.id)
                .options(direction_lineage_spend._lineage_load_options())
            )
        )
        .scalars()
        .one()
    )
    assert "vm_machine_type" not in task.__dict__
    assert "payloads" not in task.__dict__
    for deferred in ("payloads", "status_log", "description", "vm_machine_type"):
        with pytest.raises(sqlalchemy.exc.InvalidRequestError):
            getattr(task, deferred)
    # The columns the accumulator DOES read stay reachable.
    assert task.provider == "anthropic"
    assert task.context["direction_id"] == "dir-p"
    assert task.attempt_history in (None, [])


@pytest.mark.asyncio
async def test_load_direction_lineage_spend_declares_truncation(
    db_session, monkeypatch
):
    """Hitting the declared row ceiling reports a flagged LOWER BOUND, not a lie."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    for i in range(3):
        db_session.add(
            make_task(
                project_id=project.id,
                title=f"lineage-{i}",
                status=TaskStatus.COMPLETED,
                context={"direction_id": "dir-t"},
                provider="anthropic",
                model="claude-opus-4-7",
                vm_provisioned_at=now - timedelta(hours=1),
                vm_stopped_at=now,
                vm_spot=False,
            )
        )
    await db_session.commit()

    monkeypatch.setattr(direction_lineage_spend, "_LINEAGE_TASK_ROW_CAP", 2)
    spend = await load_direction_lineage_spend(
        db_session, project_id=project.id, direction_ids=["dir-t"]
    )

    dir_t = spend["dir-t"]
    assert dir_t.truncated is True
    assert dir_t.task_count == 2  # ceilinged, not all 3
    assert dir_t.basis == f"{BASIS_LIFETIME}+truncated@2"
    assert dir_t.as_dict()["truncated"] is True

    # An un-ceilinged read of the same lineage is complete and unflagged.
    monkeypatch.setattr(direction_lineage_spend, "_LINEAGE_TASK_ROW_CAP", 3)
    full = await load_direction_lineage_spend(
        db_session, project_id=project.id, direction_ids=["dir-t"]
    )
    assert full["dir-t"].truncated is False
    assert full["dir-t"].task_count == 3
    assert full["dir-t"].basis == BASIS_LIFETIME
