"""Standalone tests for _compute_research_anomalies.

These tests do not require a database — all DB calls are mocked.
Run with: bash scripts/test-backend.sh testing/test_anomaly_detection.py --standalone
"""

import uuid
from datetime import timedelta

import pytest
from unittest.mock import AsyncMock, MagicMock

from app.anomaly_detection import _compute_research_anomalies
from app.failure_classification import classify_failure
from app.models import TaskStatus, Project
from testing.factories import make_task, make_project, _utcnow


def _make_session(tasks: list, project: Project, all_tasks_72h: list | None = None):
    """Build a mock AsyncSession.

    Execute call order in _compute_research_anomalies:
      1. terminal tasks → .scalars().all()
      2. all tasks in 72h momentum window → .scalars().all()
      3. project → .scalar_one_or_none()
    """
    session = AsyncMock()
    # session.add is synchronous in SQLAlchemy — use MagicMock to avoid
    # RuntimeWarning about unawaited coroutines
    session.add = MagicMock()

    tasks_scalars = MagicMock()
    tasks_scalars.all.return_value = tasks
    tasks_result = MagicMock()
    tasks_result.scalars.return_value = tasks_scalars

    all72_scalars = MagicMock()
    all72_scalars.all.return_value = all_tasks_72h or []
    all72_result = MagicMock()
    all72_result.scalars.return_value = all72_scalars

    project_result = MagicMock()
    project_result.scalar_one_or_none.return_value = project

    session.execute = AsyncMock(
        side_effect=[tasks_result, all72_result, project_result]
    )
    return session


# ─────────────────────────────────────────────────────────────────────────────
# Empty / no-data cases
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_task_history_returns_empty_list():
    """No terminal tasks and no directions → no anomalies."""
    project_id = uuid.uuid4()
    project = make_project(directions=[])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert result == []


@pytest.mark.asyncio
async def test_few_failures_below_threshold_no_anomaly():
    """3 or fewer failures of a class in 48h (at threshold boundary) → no anomaly."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="non-zero exit code",
            created_at=now - timedelta(hours=1),
        )
        for _ in range(3)  # exactly 3 — threshold is >3
    ]
    session = _make_session(tasks=tasks, project=project)

    result = await _compute_research_anomalies(project_id, session)

    # No CODE_ERROR anomaly because count is 3, not >3
    assert all("CODE_ERROR" not in a["signal"] for a in result)


# ─────────────────────────────────────────────────────────────────────────────
# Rising failure counts
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rising_failure_count_produces_high_severity_anomaly():
    """failure_class count doubled from prior 48h to recent 48h → high severity."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # 5 CODE_ERROR failures in last 48h (no keyword match → CODE_ERROR default)
    recent = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="AttributeError: 'NoneType' object has no attribute 'get'",
            created_at=now - timedelta(hours=12 + i),
        )
        for i in range(5)
    ]
    # 2 CODE_ERROR failures in prior 48h (48–96h ago)  # noqa: RUF003
    prior = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="AttributeError: 'NoneType' object has no attribute 'get'",
            created_at=now - timedelta(hours=60 + i),
        )
        for i in range(2)
    ]
    session = _make_session(tasks=recent + prior, project=project)

    result = await _compute_research_anomalies(project_id, session)

    high = [a for a in result if a["severity"] == "high"]
    assert len(high) >= 1
    assert any("CODE_ERROR" in a["signal"] for a in high)


@pytest.mark.asyncio
async def test_high_failure_count_no_prior_is_medium_severity():
    """More than 3 failures in recent 48h with zero in prior window → medium severity."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # 4 recent CODE_ERROR failures, none in prior window (no keyword → CODE_ERROR default)
    recent = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="TypeError: unsupported operand type for +",
            created_at=now - timedelta(hours=10 + i),
        )
        for i in range(4)
    ]
    session = _make_session(tasks=recent, project=project)

    result = await _compute_research_anomalies(project_id, session)

    # Should have at least one anomaly, all medium (no prior → no doubling)
    assert len(result) >= 1
    code_error = [a for a in result if "CODE_ERROR" in a["signal"]]
    assert len(code_error) >= 1
    assert all(a["severity"] == "medium" for a in code_error)


@pytest.mark.asyncio
async def test_provision_failures_detected_separately_from_code_errors():
    """PROVISION_FAILURE and CODE_ERROR are tracked as separate failure classes."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # 4 provision failures in recent window
    provision = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="failed to provision vm",
            created_at=now - timedelta(hours=5 + i),
        )
        for i in range(4)
    ]
    # 4 code failures in recent window (no keyword match → CODE_ERROR default)
    code = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="ImportError: cannot import name 'foo'",
            created_at=now - timedelta(hours=5 + i),
        )
        for i in range(4)
    ]
    session = _make_session(tasks=provision + code, project=project)

    result = await _compute_research_anomalies(project_id, session)

    signals = [a["signal"] for a in result]
    assert any("PROVISION_FAILURE" in s for s in signals)
    assert any("CODE_ERROR" in s for s in signals)


# ─────────────────────────────────────────────────────────────────────────────
# classify_failure — agent crash report classification
# ─────────────────────────────────────────────────────────────────────────────


def test_classify_failure_uncaught_exception_is_agent_timeout():
    """[FATAL:uncaughtException] crash reports must classify as AGENT_TIMEOUT, not CODE_ERROR."""
    task = make_task(
        latest_status_message="[512MB cg:2048MB] [FATAL:uncaughtException] TypeError: Cannot read properties of undefined",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_unhandled_rejection_is_agent_timeout():
    """[FATAL:unhandledRejection] crash reports must classify as AGENT_TIMEOUT."""
    task = make_task(
        latest_status_message="[256MB cg:2048MB] [FATAL:unhandledRejection] Promise rejected: connection lost",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_sigterm_still_works():
    """[FATAL:SIGTERM] must continue to classify as AGENT_TIMEOUT."""
    task = make_task(
        latest_status_message="[256MB cg:2048MB] [FATAL:SIGTERM] Agent received shutdown signal",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_genuine_code_error_no_fatal_prefix():
    """Errors without [FATAL: prefix and no other timeout keywords stay as CODE_ERROR."""
    task = make_task(
        latest_status_message="AttributeError: 'NoneType' object has no attribute 'foo'",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "CODE_ERROR"


def test_classify_failure_merge_attempt_is_external_api():
    """'Merge attempt N failed' messages from ci_completion.py → EXTERNAL_API."""
    task = make_task(
        latest_status_message="Merge attempt 3 failed on PR #42: Repository rule violations",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "EXTERNAL_API"


def test_classify_failure_merge_blocked_is_external_api():
    """'merge blocked' messages from rollup_management.py → EXTERNAL_API."""
    task = make_task(
        latest_status_message="merge blocked: branch protection rules require passing CI",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "EXTERNAL_API"


def test_classify_failure_repository_rule_violations_is_external_api():
    """'repository rule' violations → EXTERNAL_API."""
    task = make_task(
        latest_status_message="GitHub merge rejected: repository rule violations",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "EXTERNAL_API"


def test_classify_failure_not_mergeable_is_external_api():
    """'not mergeable' state → EXTERNAL_API."""
    task = make_task(
        latest_status_message="PR is not mergeable: required status checks pending",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "EXTERNAL_API"


def test_classify_failure_merge_conflicts_is_external_api():
    """'merge conflicts' from orchestrator.py → EXTERNAL_API."""
    task = make_task(
        latest_status_message="merge conflicts — fix iterations exhausted on PR #88",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "EXTERNAL_API"


def test_classify_failure_merge_check_failed_is_external_api():
    """'merge check failed' from ci_completion.py → EXTERNAL_API."""
    task = make_task(
        latest_status_message="Merge check failed on PR #17: required checks pending",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "EXTERNAL_API"


def test_classify_failure_ci_wait_exceeded_is_agent_timeout():
    """'ci wait exceeded' messages from orchestrator.py → AGENT_TIMEOUT."""
    task = make_task(
        latest_status_message="CI wait exceeded for PR #55 after 2 hours",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_ci_wait_timed_out_is_agent_timeout():
    """'ci wait timed out' → AGENT_TIMEOUT."""
    task = make_task(
        latest_status_message="ci wait timed out waiting for checks to complete",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_ci_in_progress_is_agent_timeout():
    """'CI in progress' on timed-out tasks from orchestrator.py → AGENT_TIMEOUT."""
    task = make_task(
        latest_status_message="CI in progress on PR #42 (task timed out waiting)",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


def test_classify_failure_stuck_in_ci_wait_is_agent_timeout():
    """'stuck in ci-wait' → AGENT_TIMEOUT."""
    task = make_task(
        latest_status_message="task stuck in ci-wait state for PR #99",
        status=TaskStatus.FAILED,
    )
    assert classify_failure(task) == "AGENT_TIMEOUT"


# ─────────────────────────────────────────────────────────────────────────────
# Declining merge rates
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_declining_merge_rate_produces_medium_anomaly():
    """Direction with consistently declining merge rate → medium anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-test0001",
        "text": "Improve test coverage for API layer",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 4, "tasks_failed": 0},  # rate 0.80
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 0},  # rate 0.40
            {"tasks_created": 5, "prs_merged": 0, "tasks_failed": 0},  # rate 0.00
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    medium = [
        a for a in result if a["severity"] == "medium" and "d-test0001" in a["signal"]
    ]
    assert len(medium) >= 1
    assert "merge rate declining" in medium[0]["signal"]


@pytest.mark.asyncio
async def test_stable_merge_rate_no_declining_anomaly():
    """Direction with flat merge rate → no declining merge rate anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-stable01",
        "text": "Add observability layer",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 4, "prs_merged": 2, "tasks_failed": 0},  # 0.50
            {"tasks_created": 4, "prs_merged": 2, "tasks_failed": 0},  # 0.50
            {"tasks_created": 4, "prs_merged": 2, "tasks_failed": 0},  # 0.50
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any("merge rate declining" in a["signal"] for a in result)


@pytest.mark.asyncio
async def test_fewer_than_3_history_cycles_no_merge_rate_anomaly():
    """Direction with only 2 history entries → insufficient data, no merge rate anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-short01",
        "text": "Short history direction",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 4, "tasks_failed": 0},
            {"tasks_created": 5, "prs_merged": 1, "tasks_failed": 0},
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any("merge rate declining" in a["signal"] for a in result)


# ─────────────────────────────────────────────────────────────────────────────
# High task failure rate per direction
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_high_direction_failure_rate_produces_high_anomaly():
    """Direction with >50% failure rate in last 2 cycles → high severity anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-faildir1",
        "text": "Migrate authentication to OAuth2",
        "status": "active",
        "priority": "high",
        "paths": [],
        "research_history": [
            {"tasks_created": 4, "prs_merged": 0, "tasks_failed": 3},  # 75% fail
            {"tasks_created": 6, "prs_merged": 0, "tasks_failed": 4},  # 67% fail
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    high = [
        a for a in result if a["severity"] == "high" and "d-faildir1" in a["signal"]
    ]
    assert len(high) >= 1
    assert "failure rate" in high[0]["signal"]


@pytest.mark.asyncio
async def test_low_direction_failure_rate_no_anomaly():
    """Direction with 40% failure rate in last 2 cycles → below threshold, no anomaly."""
    project_id = uuid.uuid4()
    direction = {
        "id": "d-lowfail1",
        "text": "Improve caching strategy",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 2},  # 40% fail
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 2},  # 40% fail
        ],
    }
    project = make_project(directions=[direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any(
        "d-lowfail1" in a["signal"] and "failure rate" in a["signal"] for a in result
    )


@pytest.mark.asyncio
async def test_paused_directions_excluded_from_analysis():
    """Paused directions are skipped — only active directions are analyzed."""
    project_id = uuid.uuid4()
    paused_direction = {
        "id": "d-paused01",
        "text": "Paused work direction",
        "status": "paused",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 4, "prs_merged": 0, "tasks_failed": 4},
            {"tasks_created": 4, "prs_merged": 0, "tasks_failed": 4},
        ],
    }
    project = make_project(directions=[paused_direction])
    session = _make_session(tasks=[], project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any("d-paused01" in a["signal"] for a in result)


# ─────────────────────────────────────────────────────────────────────────────
# Combined scenarios
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_multiple_anomalies_returned_when_multiple_signals_present():
    """Multiple independent signals all appear in the returned anomaly list."""
    now = _utcnow()
    project_id = uuid.uuid4()

    direction = {
        "id": "d-multi01",
        "text": "Multi-signal direction for testing",
        "status": "active",
        "priority": "medium",
        "paths": [],
        "research_history": [
            {"tasks_created": 5, "prs_merged": 4, "tasks_failed": 0},  # 0.80
            {"tasks_created": 5, "prs_merged": 2, "tasks_failed": 0},  # 0.40
            {"tasks_created": 5, "prs_merged": 0, "tasks_failed": 4},  # 0.00, 80% fail
        ],
    }
    project = make_project(directions=[direction])

    # 5 provision failures in recent 48h, 2 in prior 48h → high severity
    recent_provisions = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="failed to provision vm",
            created_at=now - timedelta(hours=10 + i),
        )
        for i in range(5)
    ]
    prior_provisions = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message="failed to provision vm",
            created_at=now - timedelta(hours=60 + i),
        )
        for i in range(2)
    ]
    session = _make_session(tasks=recent_provisions + prior_provisions, project=project)

    result = await _compute_research_anomalies(project_id, session)

    # Should have at least: provision failure anomaly + merge rate or failure rate anomaly
    assert len(result) >= 2
    signals = [a["signal"] for a in result]
    assert any("PROVISION_FAILURE" in s for s in signals)
    assert any("d-multi01" in s for s in signals)


# ─────────────────────────────────────────────────────────────────────────────
# Cross-direction systemic fingerprint pattern
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_direction_fingerprint_emits_systemic_anomaly():
    """Same normalized error across 2+ directions with 3+ total failures → systemic anomaly."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # Same message — all failures share one fingerprint — across three directions
    shared_msg = "ImportError: cannot import name 'Foo' from 'app.bar'"
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=shared_msg,
            context={"direction_id": "d-aaa"},
            created_at=now - timedelta(hours=2),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=shared_msg,
            context={"direction_id": "d-bbb"},
            created_at=now - timedelta(hours=3),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=shared_msg,
            context={"direction_id": "d-ccc"},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_session(tasks=tasks, project=project)

    result = await _compute_research_anomalies(project_id, session)

    systemic = [
        a for a in result if a["signal"].startswith("CrossDirectionFingerprint_")
    ]
    assert len(systemic) == 1
    anomaly = systemic[0]
    assert anomaly["severity"] == "high"
    assert sorted(anomaly["affected_directions"]) == ["d-aaa", "d-bbb", "d-ccc"]
    assert anomaly["fingerprint"]  # non-empty fingerprint stored


@pytest.mark.asyncio
async def test_single_direction_fingerprint_no_systemic_anomaly():
    """Fingerprint confined to one direction → no cross-direction anomaly (even if count is high)."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    shared_msg = "AttributeError: 'NoneType' has no attribute 'x'"
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=shared_msg,
            context={"direction_id": "d-only"},
            created_at=now - timedelta(hours=i + 1),
        )
        for i in range(5)
    ]
    session = _make_session(tasks=tasks, project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any(a["signal"].startswith("CrossDirectionFingerprint_") for a in result)


@pytest.mark.asyncio
async def test_cross_direction_below_count_threshold_no_anomaly():
    """2 directions but only 2 total failures → below count threshold → no systemic anomaly."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    shared_msg = "KeyError: 'missing_key'"
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=shared_msg,
            context={"direction_id": "d-a"},
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=shared_msg,
            context={"direction_id": "d-b"},
            created_at=now - timedelta(hours=2),
        ),
    ]
    session = _make_session(tasks=tasks, project=project)

    result = await _compute_research_anomalies(project_id, session)

    assert not any(a["signal"].startswith("CrossDirectionFingerprint_") for a in result)


# ─────────────────────────────────────────────────────────────────────────────
# Proactive fingerprint investigation spawning
# ─────────────────────────────────────────────────────────────────────────────

import app.shared_state as _shared_state
from app.anomaly_detection import _spawn_proactive_fingerprint_investigations
from app.models import Task as _TaskModel


@pytest.fixture(autouse=True)
def _stub_recent_changes_for_proactive(monkeypatch):
    """Stub _get_recent_changes_context to {} for all proactive-spawn tests.

    Production reads DeploymentState.deployed_at.isoformat() for the payload's
    recent_changes block. The fake AsyncMock session in this module returns a
    MagicMock for that chain, which then leaks into the payload as a non-string
    and trips fail-closed validate_internal_payload (introduced in 6fe0b544).
    """

    async def _empty_recent_changes(*args, **kwargs):
        return {}

    monkeypatch.setattr(
        "app.autonomous_research_diagnosis._get_recent_changes_context",
        _empty_recent_changes,
    )


def _make_proactive_session(active_proactive_count: int = 0):
    """Build an AsyncSession mock for proactive-spawn tests.

    First execute() call: _count_active_proactive_investigations → N rows.
    Subsequent execute() calls: consumed by _recent_investigation_exists
    (mocked separately via monkeypatch, so returns empty).
    """
    session = AsyncMock()
    adds: list = []

    def _add(obj):
        adds.append(obj)

    session.add = MagicMock(side_effect=_add)
    session.commit = AsyncMock()
    session.flush = AsyncMock()

    count_result = MagicMock()
    count_result.all.return_value = [("row",)] * active_proactive_count

    empty_result = MagicMock()
    empty_result.all.return_value = []
    # _get_recent_changes_context calls session.execute(...).scalars().first()
    # to fetch DeploymentState; with no row present that must yield None so
    # the InvestigatePayload validator (fail-closed since 3eeb693) sees real
    # str | None values for deployed_at / current_sha rather than MagicMocks.
    empty_result.scalars.return_value.first.return_value = None

    session.execute = AsyncMock(
        side_effect=[count_result, empty_result, empty_result, empty_result]
    )
    return session, adds


@pytest.mark.asyncio
async def test_proactive_fingerprint_spawns_investigation(monkeypatch):
    """Persistent cross-direction fingerprint entry → proactive investigate task spawned."""
    project_id = uuid.uuid4()
    fp = "abc123def456"
    _shared_state.persistent_anomalies.clear()
    _shared_state.persistent_anomalies[str(project_id)] = {
        f"CrossDirectionFingerprint_{fp}": {
            "first_seen": _utcnow().isoformat(),
            "last_seen": _utcnow().isoformat(),
            "occurrence_count": 1,
            "severity": "high",
            "latest_details": "systemic",
            "fingerprint": fp,
            "affected_directions": ["d-aaa", "d-bbb"],
            "failure_count": 5,
            "example_message": "ImportError: cannot import name Foo",
        }
    }

    async def _no_recent(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "app.autonomous_research._recent_investigation_exists",
        _no_recent,
    )

    session, adds = _make_proactive_session(active_proactive_count=0)
    try:
        spawned = await _spawn_proactive_fingerprint_investigations(session)
    finally:
        _shared_state.persistent_anomalies.clear()

    assert spawned == 1
    tasks = [o for o in adds if isinstance(o, _TaskModel)]
    assert len(tasks) == 1
    new_task = tasks[0]
    ctx = new_task.context
    assert ctx["diagnosis_trigger"] == "proactive_pattern_detection"
    assert ctx["proactive_pattern_fix"] is True
    assert ctx["error_fingerprint"] == fp
    assert ctx["affected_directions"] == ["d-aaa", "d-bbb"]
    assert ctx["failure_count"] == 5
    assert new_task.payloads[0]["type"] == "investigate"


@pytest.mark.asyncio
async def test_proactive_fingerprint_dedup_flag(monkeypatch):
    """Entries already flagged as spawned are skipped."""
    project_id = uuid.uuid4()
    fp = "aaa111bbb222"
    _shared_state.persistent_anomalies.clear()
    _shared_state.persistent_anomalies[str(project_id)] = {
        f"CrossDirectionFingerprint_{fp}": {
            "severity": "high",
            "occurrence_count": 1,
            "fingerprint": fp,
            "affected_directions": ["d-x", "d-y"],
            "failure_count": 4,
            "example_message": "err",
            "proactive_investigation_spawned": True,
        }
    }

    async def _no_recent(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "app.autonomous_research._recent_investigation_exists",
        _no_recent,
    )

    session, adds = _make_proactive_session(active_proactive_count=0)
    try:
        spawned = await _spawn_proactive_fingerprint_investigations(session)
    finally:
        _shared_state.persistent_anomalies.clear()

    assert spawned == 0
    assert not [o for o in adds if isinstance(o, _TaskModel)]


@pytest.mark.asyncio
async def test_proactive_fingerprint_respects_recent_investigation_exists(monkeypatch):
    """Pre-existing investigation for the fingerprint blocks spawn and marks flag."""
    project_id = uuid.uuid4()
    fp = "ccc333ddd444"
    _shared_state.persistent_anomalies.clear()
    entry = {
        "severity": "high",
        "occurrence_count": 1,
        "fingerprint": fp,
        "affected_directions": ["d-1", "d-2"],
        "failure_count": 5,
        "example_message": "err",
    }
    _shared_state.persistent_anomalies[str(project_id)] = {
        f"CrossDirectionFingerprint_{fp}": entry,
    }

    async def _yes_recent(*args, **kwargs):
        return True

    monkeypatch.setattr(
        "app.autonomous_research._recent_investigation_exists",
        _yes_recent,
    )

    session, adds = _make_proactive_session(active_proactive_count=0)
    try:
        spawned = await _spawn_proactive_fingerprint_investigations(session)
    finally:
        _shared_state.persistent_anomalies.clear()

    assert spawned == 0
    assert not [o for o in adds if isinstance(o, _TaskModel)]
    assert entry.get("proactive_investigation_spawned") is True


@pytest.mark.asyncio
async def test_proactive_fingerprint_per_project_cap(monkeypatch):
    """Two eligible fingerprints in one project → at most 1 spawn."""
    project_id = uuid.uuid4()
    _shared_state.persistent_anomalies.clear()
    _shared_state.persistent_anomalies[str(project_id)] = {
        "CrossDirectionFingerprint_fp1aaa": {
            "severity": "high",
            "occurrence_count": 1,
            "fingerprint": "fp1aaa",
            "affected_directions": ["d-a", "d-b"],
            "failure_count": 4,
            "example_message": "e1",
        },
        "CrossDirectionFingerprint_fp2bbb": {
            "severity": "high",
            "occurrence_count": 1,
            "fingerprint": "fp2bbb",
            "affected_directions": ["d-c", "d-d"],
            "failure_count": 4,
            "example_message": "e2",
        },
    }

    async def _no_recent(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "app.autonomous_research._recent_investigation_exists",
        _no_recent,
    )

    session, adds = _make_proactive_session(active_proactive_count=0)
    try:
        spawned = await _spawn_proactive_fingerprint_investigations(session)
    finally:
        _shared_state.persistent_anomalies.clear()

    assert spawned == 1
    assert len([o for o in adds if isinstance(o, _TaskModel)]) == 1


@pytest.mark.asyncio
async def test_proactive_fingerprint_system_concurrent_cap(monkeypatch):
    """When system cap is reached, no new spawns occur."""
    project_id = uuid.uuid4()
    fp = "eee555fff666"
    _shared_state.persistent_anomalies.clear()
    _shared_state.persistent_anomalies[str(project_id)] = {
        f"CrossDirectionFingerprint_{fp}": {
            "severity": "high",
            "occurrence_count": 1,
            "fingerprint": fp,
            "affected_directions": ["d-m", "d-n"],
            "failure_count": 5,
            "example_message": "err",
        }
    }

    async def _no_recent(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "app.autonomous_research._recent_investigation_exists",
        _no_recent,
    )

    session, adds = _make_proactive_session(active_proactive_count=2)
    try:
        spawned = await _spawn_proactive_fingerprint_investigations(session)
    finally:
        _shared_state.persistent_anomalies.clear()

    assert spawned == 0
    assert not [o for o in adds if isinstance(o, _TaskModel)]


# ─────────────────────────────────────────────────────────────────────────────
# Intra-direction fingerprint detection + proactive spawning
# ─────────────────────────────────────────────────────────────────────────────

from app.anomaly_detection import (
    _spawn_proactive_intra_direction_investigations,
)


@pytest.mark.asyncio
async def test_intra_direction_fingerprint_emits_anomaly():
    """Same fingerprint 3x within one direction → IntraDirectionFingerprint anomaly."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])
    msg = "TypeError: unhashable type: 'list' at app/foo.py"
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
            context={"direction_id": "d-only"},
            created_at=now - timedelta(hours=i + 1),
        )
        for i in range(3)
    ]
    session = _make_session(tasks=tasks, project=project)
    result = await _compute_research_anomalies(project_id, session)
    intra = [a for a in result if a["signal"].startswith("IntraDirectionFingerprint_")]
    assert len(intra) == 1
    a = intra[0]
    assert a["severity"] == "high"
    assert a["direction_id"] == "d-only"
    assert a["failure_count"] == 3
    assert a["fingerprint"]


@pytest.mark.asyncio
async def test_intra_direction_suppressed_when_cross_direction_fires():
    """Fingerprint spanning 2+ directions → only cross-direction anomaly, no intra-direction."""
    now = _utcnow()
    project_id = uuid.uuid4()
    project = make_project(directions=[])
    msg = "ImportError: cannot import name 'Bar'"
    tasks = [
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
            context={"direction_id": "d-a"},
            created_at=now - timedelta(hours=1),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
            context={"direction_id": "d-a"},
            created_at=now - timedelta(hours=2),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
            context={"direction_id": "d-a"},
            created_at=now - timedelta(hours=3),
        ),
        make_task(
            project_id=project_id,
            status=TaskStatus.FAILED,
            latest_status_message=msg,
            context={"direction_id": "d-b"},
            created_at=now - timedelta(hours=4),
        ),
    ]
    session = _make_session(tasks=tasks, project=project)
    result = await _compute_research_anomalies(project_id, session)
    assert any(a["signal"].startswith("CrossDirectionFingerprint_") for a in result)
    assert not any(a["signal"].startswith("IntraDirectionFingerprint_") for a in result)


@pytest.mark.asyncio
async def test_proactive_intra_direction_spawns_investigation(monkeypatch):
    """Persistent IntraDirectionFingerprint entry → proactive investigate task spawned."""
    project_id = uuid.uuid4()
    fp = "1a2b3c4d5e6f"
    did = "d-target"
    _shared_state.persistent_anomalies.clear()
    _shared_state.persistent_anomalies[str(project_id)] = {
        f"IntraDirectionFingerprint_{fp}_{did}": {
            "severity": "high",
            "occurrence_count": 1,
            "fingerprint": fp,
            "direction_id": did,
            "failure_count": 4,
            "example_message": "TypeError: bad thing",
        }
    }

    async def _no_recent(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "app.autonomous_research._recent_investigation_exists",
        _no_recent,
    )

    session, adds = _make_proactive_session(active_proactive_count=0)
    try:
        spawned = await _spawn_proactive_intra_direction_investigations(session)
    finally:
        _shared_state.persistent_anomalies.clear()

    assert spawned == 1
    tasks = [o for o in adds if isinstance(o, _TaskModel)]
    assert len(tasks) == 1
    ctx = tasks[0].context
    assert ctx["diagnosis_trigger"] == "proactive_intra_direction"
    assert ctx["proactive_intra_direction_fix"] is True
    assert ctx["error_fingerprint"] == fp
    assert ctx["direction_id"] == did
    assert ctx["failure_count"] == 4
    assert tasks[0].payloads[0]["type"] == "investigate"


@pytest.mark.asyncio
async def test_proactive_intra_direction_system_cap(monkeypatch):
    """System cap blocks intra-direction spawns just like cross-direction."""
    project_id = uuid.uuid4()
    fp = "deadbeefcafe"
    did = "d-capped"
    _shared_state.persistent_anomalies.clear()
    _shared_state.persistent_anomalies[str(project_id)] = {
        f"IntraDirectionFingerprint_{fp}_{did}": {
            "severity": "high",
            "occurrence_count": 1,
            "fingerprint": fp,
            "direction_id": did,
            "failure_count": 3,
            "example_message": "e",
        }
    }

    async def _no_recent(*args, **kwargs):
        return False

    monkeypatch.setattr(
        "app.autonomous_research._recent_investigation_exists",
        _no_recent,
    )

    session, adds = _make_proactive_session(active_proactive_count=2)
    try:
        spawned = await _spawn_proactive_intra_direction_investigations(session)
    finally:
        _shared_state.persistent_anomalies.clear()

    assert spawned == 0
    assert not [o for o in adds if isinstance(o, _TaskModel)]


# ─────────────────────────────────────────────────────────────────────────────
# Failure rate momentum
# ─────────────────────────────────────────────────────────────────────────────


def _make_status_task(project_id, status, hours_ago):
    return make_task(
        project_id=project_id,
        status=status,
        latest_status_message="x" if status == TaskStatus.FAILED else "",
        created_at=_utcnow() - timedelta(hours=hours_ago),
    )


@pytest.mark.asyncio
async def test_failure_rate_momentum_detected_across_rising_windows():
    """Strictly rising failure rate across 3x24h windows with last>=20% → medium momentum anomaly."""
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    # Failures: 1 in 72-48h, 3 in 48-24h, 6 in 24-0h
    failed = (
        [_make_status_task(project_id, TaskStatus.FAILED, 60) for _ in range(1)]
        + [_make_status_task(project_id, TaskStatus.FAILED, 36) for _ in range(3)]
        + [_make_status_task(project_id, TaskStatus.FAILED, 12) for _ in range(6)]
    )
    # Per-window totals: 10, 10, 10 → rates 10%, 30%, 60%
    totals = (
        [_make_status_task(project_id, TaskStatus.COMPLETED, 60) for _ in range(9)]
        + [_make_status_task(project_id, TaskStatus.COMPLETED, 36) for _ in range(7)]
        + [_make_status_task(project_id, TaskStatus.COMPLETED, 12) for _ in range(4)]
        + failed
    )
    session = _make_session(tasks=failed, project=project, all_tasks_72h=totals)

    result = await _compute_research_anomalies(project_id, session)

    momentum = [a for a in result if "FailureRateMomentum" in a["signal"]]
    assert len(momentum) == 1
    assert momentum[0]["severity"] == "medium"


@pytest.mark.asyncio
async def test_failure_rate_momentum_no_anomaly_when_flat():
    """Flat failure rate across windows → no momentum anomaly."""
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    failed = (
        [_make_status_task(project_id, TaskStatus.FAILED, 60) for _ in range(3)]
        + [_make_status_task(project_id, TaskStatus.FAILED, 36) for _ in range(3)]
        + [_make_status_task(project_id, TaskStatus.FAILED, 12) for _ in range(3)]
    )
    totals = (
        [_make_status_task(project_id, TaskStatus.COMPLETED, 60) for _ in range(7)]
        + [_make_status_task(project_id, TaskStatus.COMPLETED, 36) for _ in range(7)]
        + [_make_status_task(project_id, TaskStatus.COMPLETED, 12) for _ in range(7)]
        + failed
    )
    session = _make_session(tasks=failed, project=project, all_tasks_72h=totals)

    result = await _compute_research_anomalies(project_id, session)
    assert not any("FailureRateMomentum" in a["signal"] for a in result)


@pytest.mark.asyncio
async def test_failure_rate_momentum_no_anomaly_when_below_threshold():
    """Rising rates but most recent < 20% → no momentum anomaly."""
    project_id = uuid.uuid4()
    project = make_project(directions=[])

    failed = (
        [_make_status_task(project_id, TaskStatus.FAILED, 60) for _ in range(1)]
        + [_make_status_task(project_id, TaskStatus.FAILED, 36) for _ in range(2)]
        + [_make_status_task(project_id, TaskStatus.FAILED, 12) for _ in range(3)]
    )
    # Per-window totals: 50, 50, 50 → rates 2%, 4%, 6%
    totals = (
        [_make_status_task(project_id, TaskStatus.COMPLETED, 60) for _ in range(49)]
        + [_make_status_task(project_id, TaskStatus.COMPLETED, 36) for _ in range(48)]
        + [_make_status_task(project_id, TaskStatus.COMPLETED, 12) for _ in range(47)]
        + failed
    )
    session = _make_session(tasks=failed, project=project, all_tasks_72h=totals)

    result = await _compute_research_anomalies(project_id, session)
    assert not any("FailureRateMomentum" in a["signal"] for a in result)


# ─────────────────────────────────────────────────────────────────────────────
# seed_task_results_from_db
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_seed_task_results_from_db_populates_deque():
    """Seeding rebuilds task_results from completed/failed tasks within window."""
    import app.shared_state as _ss
    from app.anomaly_detection import seed_task_results_from_db

    _ss.deploy_monitoring["task_results"].clear()

    project_id = uuid.uuid4()
    now = _utcnow()
    in_window_ok = make_task(
        project_id=project_id,
        status=TaskStatus.COMPLETED,
        vm_stopped_at=now - timedelta(minutes=30),
        payloads=[{"type": "create"}],
    )
    in_window_fail = make_task(
        project_id=project_id,
        status=TaskStatus.FAILED,
        vm_stopped_at=now - timedelta(minutes=45),
        payloads=[{"type": "fix"}],
        latest_status_message="Build failed",
    )
    rows = [in_window_ok, in_window_fail]

    scalars = MagicMock()
    scalars.all.return_value = rows
    result = MagicMock()
    result.scalars.return_value = scalars
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    seeded = await seed_task_results_from_db(session, hours=2)

    assert seeded == 2
    entries = list(_ss.deploy_monitoring["task_results"])
    assert len(entries) == 2
    assert entries[0][1] == "success"
    assert entries[0][3] == "create"
    assert entries[1][1] == "failure"
    assert entries[1][3] == "fix"
    assert entries[1][2] != ""  # failure_class populated


# ─────────────────────────────────────────────────────────────────────────────
# Persistence boundary: _compute_research_anomalies calls persist_anomaly_snapshot
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_compute_research_anomalies_persists_snapshot():
    """_compute_research_anomalies calls persist_anomaly_snapshot after detection."""
    from unittest.mock import patch

    project_id = uuid.uuid4()
    project = make_project(directions=[])
    session = _make_session(tasks=[], project=project)

    captured: dict = {}

    async def fake_persist(proj_key, proj_anomalies, sess):
        captured["proj_key"] = proj_key
        captured["sess"] = sess

    with patch("app.shared_state.persist_anomaly_snapshot", new=fake_persist):
        await _compute_research_anomalies(project_id, session)

    assert captured.get("proj_key") == str(project_id)
    assert captured.get("sess") is session


@pytest.mark.asyncio
async def test_compute_research_anomalies_records_last_run_timestamp():
    """A successful detection cycle stamps shared_state.last_anomaly_detection_run_at."""
    from unittest.mock import patch
    import app.shared_state as _shared_state

    project_id = uuid.uuid4()
    project = make_project(directions=[])
    session = _make_session(tasks=[], project=project)

    saved = _shared_state.last_anomaly_detection_run_at
    _shared_state.last_anomaly_detection_run_at = None
    try:
        before = _utcnow()
        with patch("app.shared_state.persist_anomaly_snapshot", new=AsyncMock()):
            await _compute_research_anomalies(project_id, session)
        after = _utcnow()
        recorded = _shared_state.last_anomaly_detection_run_at
        assert recorded is not None
        # The function uses _utcnow() at the top; bound it within the test window.
        assert before - timedelta(seconds=1) <= recorded <= after + timedelta(seconds=1)
    finally:
        _shared_state.last_anomaly_detection_run_at = saved
