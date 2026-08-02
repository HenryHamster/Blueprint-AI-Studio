"""Regression tests: task-outcome walkers must never lazy-load deferred columns.

Prod 2026-06-07 (296+ occurrences): four admin endpoints 500ed with
``sqlalchemy.exc.MissingGreenlet`` because the v6 outcome walkers
(``task_outcomes.evaluate_task_outcome`` / ``is_successful_outcome``) were fed
Task rows from ``load_only()`` queries that omitted columns the walkers read
directly:

* ``Task.payloads`` — read by ``task_outcomes._task_type`` via
  ``Task.first_payload`` (the always-failing frame),
* ``Task.latest_status_message`` — read by
  ``direction_outcomes._is_cascade_cancelled`` for CANCELLED rows
  (the ``direction_outcomes.py:53`` UNHANDLED family),
* ``Task.plan_output`` — degraded silently to ``neutral`` for the read-only
  types' honest success signals rather than crashing.

These tests fetch through the REAL endpoint paths. The endpoint request gets
its own session (``override_get_session`` opens a fresh one per request), so
the seeded rows are loaded through the endpoints' actual ``load_only``
projections with an empty identity map — exactly the shape that lazy-loaded
and raised in production. Pre-fix, each endpoint test here dies with
``MissingGreenlet``; post-fix they return 200 with honest outcome counts.
"""

from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.models import StrategicCycle, TaskStatus
from testing.conftest import USE_POSTGRES
from testing.factories import make_membership, make_project, make_task, _utcnow

# The deferred-column MissingGreenlet fault these endpoint walks reproduce only
# fires on asyncpg: the root conftest globally no-ops InstanceState._expire, so on
# the default aiosqlite backend the lazy-load never happens and every test here
# would pass tautologically regardless of the projection fix. Each test is marked
# Postgres-only with an inline @pytest.mark.skipif(not USE_POSTGRES, ...) so it
# exercises the real failure mode instead of masking it (the fidelity guard in
# test_audit_finding_class_guards.py only recognises the inline skipif form).
_POSTGRES_ONLY_REASON = (
    "MissingGreenlet deferred-column fault only reproduces on asyncpg; SQLite "
    "no-ops InstanceState._expire so the walk passes tautologically."
)


def _seed_non_code_portfolio(project_id, now):
    """Terminal, non-review-chain tasks covering every walker branch.

    Returns (research, memo, audit, failed_fix): three read-only successes —
    research via the modern ``research_surviving_findings`` context signal,
    memo/audit via their ``plan_output`` artifacts (exercising the
    plan_output hydration) — plus a genuine write-type failure.
    """
    research = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=1),
        payloads=[{"type": "research"}],
        context={"research_surviving_findings": 2},
    )
    memo = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=1),
        payloads=[{"type": "memo"}],
        plan_output={"memo": {"summary": "s", "reasoning": "r"}},
    )
    audit = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=1),
        payloads=[{"type": "security_audit"}],
        plan_output={"findings": [{"title": "finding", "severity": "high"}]},
    )
    failed_fix = make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        created_at=now - timedelta(hours=1),
        payloads=[{"type": "fix"}],
    )
    return research, memo, audit, failed_fix


@pytest.mark.asyncio
@pytest.mark.skipif(not USE_POSTGRES, reason=_POSTGRES_ONLY_REASON)
async def test_outer_loop_convergence_walks_non_code_outcomes_without_lazy_load(
    client: AsyncClient, db_session
):
    """/outer-loop-convergence walks terminal non-review-chain tasks through
    evaluate_task_outcome. Pre-fix the load_only projection omitted payloads
    (MissingGreenlet at task_outcomes._task_type) and latest_status_message
    (MissingGreenlet at direction_outcomes._is_cascade_cancelled for the
    CANCELLED hydrated chain-original)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    portfolio = _seed_non_code_portfolio(project.id, now)
    # CANCELLED chain-original: outside the COMPLETED/FAILED window query, so
    # it is hydrated by _load_outer_loop_original_tasks and walked — its
    # cascade-cancel classification reads latest_status_message.
    cancelled_original = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        created_at=now - timedelta(hours=1),
        payloads=[{"type": "create"}],
        latest_status_message="Cancelled: parent requeued with a replacement",
    )
    review_child = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(minutes=30),
        context={
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(cancelled_original.id),
                "pr_number": 7,
            }
        },
    )
    db_session.add_all([*portfolio, cancelled_original, review_child])
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/outer-loop-convergence?hours=24&project_id={project.id}"
    )
    assert resp.status_code == 200
    nco = resp.json()["non_code_outcomes"]
    # research + memo + audit successes (memo/audit prove the plan_output
    # hydration — without it they'd silently score neutral), the FAILED fix is
    # a failure, and the cascade-CANCELLED original scores neutral (non-genuine).
    assert nco["total"] == 5
    assert nco["successes"] == 3
    assert nco["failures"] == 1
    assert nco["neutrals"] == 1
    assert nco["by_kind"]["research_quality"]["success"] == 1
    assert nco["by_kind"]["memo_artifact"]["success"] == 1
    assert nco["by_kind"]["audit_findings"]["success"] == 1
    assert nco["by_kind"]["code_merge"] == {
        "success": 0,
        "neutral": 1,
        "failure": 1,
    }


@pytest.mark.asyncio
async def test_outer_loop_convergence_counts_straddling_task_via_widened_scan(
    client: AsyncClient, db_session
):
    """A task created BEFORE the hours window but completing INSIDE it must be
    counted in non_code_outcomes. Guards the widened DB scan in
    get_outer_loop_convergence (lookback_cutoff = cutoff - hours): with the old
    ``created_at >= cutoff`` filter the straddling row never left the DB, so
    the completion-time walk in _compute_non_code_outcomes could not see it —
    a silent under-count no pure-function test can catch."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Created 30h ago (outside hours=24, inside the widened 48h scan),
    # completed 1h ago (inside the window). Memo type so the honest success
    # signal also proves plan_output hydration covers straddling rows.
    straddling = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=30),
        completed_at=now - timedelta(hours=1),
        payloads=[{"type": "memo"}],
        plan_output={"memo": {"summary": "s", "reasoning": "r"}},
    )
    db_session.add(straddling)
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/outer-loop-convergence?hours=24&project_id={project.id}"
    )
    assert resp.status_code == 200
    nco = resp.json()["non_code_outcomes"]
    assert nco["total"] == 1
    assert nco["successes"] == 1
    assert nco["by_kind"]["memo_artifact"]["success"] == 1


@pytest.mark.asyncio
async def test_fetch_outer_loop_tasks_lookback_cutoff_widens_created_at_scan(
    db_session,
):
    """DB-layer contract of the ``lookback_cutoff`` parameter itself: passing a
    cutoff wider than ``cutoff`` admits rows created between the two, without
    admitting rows older than the widened bound; omitting it preserves the
    original ``created_at >= cutoff`` behavior for existing callers."""
    from conftest import TestingSessionLocal

    from app.routers.admin_pipeline_observability import _fetch_outer_loop_tasks

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    in_window = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=1),
        payloads=[{"type": "memo"}],
    )
    straddling = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=30),
        completed_at=now - timedelta(hours=1),
        payloads=[{"type": "memo"}],
    )
    ancient = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=60),
        payloads=[{"type": "memo"}],
    )
    db_session.add_all([in_window, straddling, ancient])
    await db_session.commit()

    cutoff = now - timedelta(hours=24)
    async with TestingSessionLocal() as fresh_session:
        default_ids = {
            t.id
            for t in await _fetch_outer_loop_tasks(
                fresh_session, cutoff=cutoff, project_id=project.id, limit=100
            )
        }
    async with TestingSessionLocal() as fresh_session:
        widened_ids = {
            t.id
            for t in await _fetch_outer_loop_tasks(
                fresh_session,
                cutoff=cutoff,
                project_id=project.id,
                limit=100,
                lookback_cutoff=cutoff - timedelta(hours=24),
            )
        }

    assert default_ids == {in_window.id}
    assert widened_ids == {in_window.id, straddling.id}


@pytest.mark.asyncio
@pytest.mark.skipif(not USE_POSTGRES, reason=_POSTGRES_ONLY_REASON)
async def test_lane_diag_walks_hydrated_original_without_lazy_load(
    client: AsyncClient, db_session
):
    """/lane-diag's waste reducer walks the hydrated chain-original too.

    Prod 2026-06-13 (147x): ``/api/admin/lane-diag`` 500ed with MissingGreenlet
    because ``_load_outer_loop_original_tasks`` hydrated chain-originals with a
    projection that omitted columns the NEW lane-waste reducer reads — first
    ``fix_loop_history`` (``classify_lane_waste_category`` ->
    ``has_terminal_divergence``), then ``started_at`` / ``status_log`` /
    ``updated_at`` / ``archived``. ``getattr(t, "fix_loop_history", None)`` does
    NOT suppress the deferred load, so walking a hydrated original lazy-loaded
    inside the async session and crashed the whole endpoint.

    Repro shape: a chain-original that itself carries ``review_loop`` context
    (so the reducer groups + walks it) but is NON-terminal (EVALUATING) — so it
    is excluded from the wide terminal pool and reaches the reducer ONLY through
    the narrow ``_load_outer_loop_original_tasks`` hydration. Pre-fix this dies
    with MissingGreenlet; post-fix it returns 200.
    """
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Chain-original O: in-window, NON-terminal (excluded from the wide terminal
    # query), carries review_loop so the waste reducer walks it. Reached only via
    # the narrow hydration projection — the exact row that lazy-loaded in prod.
    original = make_task(
        project_id=project.id,
        status=TaskStatus.EVALUATING,
        created_at=now - timedelta(hours=2),
        payloads=[{"type": "create"}],
        context={
            "review_loop": {
                "iteration": 0,
                "pr_number": 7311,
            }
        },
    )
    # Terminal review child C references O as its chain-original, so O is
    # hydrated by _load_outer_loop_original_tasks.
    review_child = make_task(
        project_id=project.id,
        status=TaskStatus.FAILED,
        created_at=now - timedelta(minutes=20),
        payloads=[{"type": "create"}],
        context={
            "review_loop": {
                "iteration": 1,
                "original_task_id": str(original.id),
                "pr_number": 7311,
            }
        },
    )
    db_session.add_all([original, review_child])
    await db_session.commit()

    resp = await client.get(f"/api/admin/lane-diag?project_id={project.id}")
    assert resp.status_code == 200
    lane_waste = resp.json()["lane_waste"]
    # Both the terminal child and the hydrated original are walked as waste rows.
    walked_ids = {
        row["task_id"] for chain in lane_waste["chains"] for row in chain["tasks"]
    }
    assert str(original.id) in walked_ids
    assert str(review_child.id) in walked_ids


@pytest.mark.asyncio
@pytest.mark.skipif(not USE_POSTGRES, reason=_POSTGRES_ONLY_REASON)
async def test_outer_loop_convergence_trend_walks_non_code_outcomes(
    client: AsyncClient, db_session
):
    """/outer-loop-convergence-trend runs the same per-day walk; pre-fix its
    identical load_only projection raised MissingGreenlet on first_payload."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    db_session.add_all(_seed_non_code_portfolio(project.id, _utcnow()))
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/outer-loop-convergence-trend?days=2&project_id={project.id}"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["days"] == 2
    assert len(data["buckets"]) == 2
    assert data["churn_direction"] in {"falling", "rising", "flat"}


@pytest.mark.asyncio
@pytest.mark.skipif(not USE_POSTGRES, reason=_POSTGRES_ONLY_REASON)
async def test_digest_survives_trend_window_exceeding_digest_lookback(
    client: AsyncClient, db_session
):
    """/digest computes the 7-day convergence trend inline. With a small
    ``hours`` the digest's own scan window (hours*7) is narrower than the
    trend's 7-day window, so the trend query loads rows the digest scan never
    stashed a payload projection for — pre-fix those fresh instances
    lazy-loaded ``payloads`` inside the async session (MissingGreenlet, the
    prod /api/admin/digest 500)."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    # Outside the hours=1 digest lookback (7h), inside the trend's 7d window.
    memo = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=30),
        payloads=[{"type": "memo"}],
        plan_output={"memo": {"summary": "s"}},
    )
    in_window = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(minutes=30),
        payloads=[{"type": "create"}],
        context={"pr_merged": True},
    )
    db_session.add_all([memo, in_window])
    await db_session.commit()

    resp = await client.get(f"/api/admin/digest?hours=1&project_id={project.id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["window_hours"] == 1
    assert data["completed_count"] == 1


@pytest.mark.asyncio
async def test_research_actionability_walks_cancelled_and_non_code_followons(
    client: AsyncClient, db_session
):
    """/research-actionability walks follow-ons through is_successful_outcome.

    Pre-fix the follow-on load_only omitted latest_status_message, so a
    CANCELLED follow-on crashed in direction_outcomes._is_cascade_cancelled
    (the prod ``direction_outcomes.py:53`` frame); it also omitted plan_output,
    silently scoring non-code follow-ons neutral. The memo follow-on here must
    count as succeeded for its finding's grade."""
    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    db_session.add(make_membership(project.id))

    now = _utcnow()
    db_session.add(
        StrategicCycle(
            id="cycle-mg-regression",
            project_id=project.id,
            created_at=now - timedelta(hours=3),
        )
    )
    research = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=2),
        payloads=[{"type": "research"}],
        context={"research_findings_finalized": True},
        plan_output=[
            {
                "title": "Finding HIGH",
                "actionability_grade": "HIGH",
                "direction_id": "dir-mg",
            },
        ],
    )
    # Cascade-CANCELLED follow-on: previously lazy-loaded latest_status_message.
    cancelled_followon = make_task(
        project_id=project.id,
        status=TaskStatus.CANCELLED,
        created_at=now - timedelta(hours=1),
        payloads=[{"type": "fix"}],
        context={"direction_id": "dir-mg"},
        latest_status_message="Cancelled: parent requeued with a replacement",
    )
    # Non-code follow-on whose honest success signal lives on plan_output.
    memo_followon = make_task(
        project_id=project.id,
        status=TaskStatus.COMPLETED,
        created_at=now - timedelta(hours=1),
        payloads=[{"type": "memo"}],
        context={"direction_id": "dir-mg"},
        plan_output={"memo": {"summary": "s", "reasoning": "r"}},
    )
    db_session.add_all([research, cancelled_followon, memo_followon])
    await db_session.commit()

    resp = await client.get(
        f"/api/admin/projects/{project.id}/research-actionability"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["grade_distribution"]["HIGH"] == 1
    assert data["grade_tasked"]["HIGH"] == 1
    # No PR merged, but the memo follow-on succeeded — the generalized
    # (non-code) success must be counted, not silently degraded to neutral.
    assert data["grade_merged"]["HIGH"] == 0
    assert data["grade_succeeded"]["HIGH"] == 1
    assert data["grade_success_rate"]["HIGH"] == 1.0


@pytest.mark.asyncio
@pytest.mark.skipif(not USE_POSTGRES, reason=_POSTGRES_ONLY_REASON)
async def test_evaluate_task_outcome_over_fixed_projection_fetch(db_session):
    """Focused unit walk: evaluate_task_outcome over Tasks fetched with the
    fixed outer-loop projection in a FRESH async session (empty identity map —
    the exact shape that raised MissingGreenlet in production)."""
    from conftest import TestingSessionLocal

    from app.routers.admin_pipeline_observability import (
        _fetch_outer_loop_tasks,
        _hydrate_outcome_plan_outputs,
    )
    from app.task_outcomes import (
        KIND_AUDIT_FINDINGS,
        KIND_CODE_MERGE,
        KIND_MEMO_ARTIFACT,
        KIND_RESEARCH_QUALITY,
        TaskOutcome,
        evaluate_task_outcome,
    )

    project = make_project()
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)

    now = _utcnow()
    research, memo, audit, failed_fix = _seed_non_code_portfolio(project.id, now)
    db_session.add_all([research, memo, audit, failed_fix])
    await db_session.commit()
    expected = {
        research.id: TaskOutcome("success", KIND_RESEARCH_QUALITY),
        memo.id: TaskOutcome("success", KIND_MEMO_ARTIFACT),
        audit.id: TaskOutcome("success", KIND_AUDIT_FINDINGS),
        failed_fix.id: TaskOutcome("failure", KIND_CODE_MERGE),
    }

    async with TestingSessionLocal() as fresh_session:
        tasks = await _fetch_outer_loop_tasks(
            fresh_session,
            cutoff=now - timedelta(hours=24),
            project_id=project.id,
            limit=100,
        )
        await _hydrate_outcome_plan_outputs(fresh_session, tasks)
        assert {t.id for t in tasks} == set(expected)
        outcomes = {t.id: evaluate_task_outcome(t) for t in tasks}

    assert outcomes == expected
