"""Shared scaffolding for the ``test_strategic_spawning_*`` module family.

``test_strategic_spawning.py`` was split into eight domain modules; this file
holds the fixtures and builders every one of them was otherwise duplicating.
Import only what a module actually uses::

    from _strategic_spawning_test_helpers import _make_project, _patch_spawning_deps

``_ensure_test_account_pool`` is autouse: importing the name into a test
module registers it for that module's tests.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import or_, select

from app.models import LocalRunner, Project, Task
from app.max_plan_pool import _max_plan_pools, _account_by_id
from app.task_capabilities import BACKEND_MANAGED_TASK_TYPES


def _select_agent_tasks(project_id):
    """Select project Tasks while excluding hidden backend control work."""

    task_type = Task.payloads[0]["type"].as_string()
    return select(Task).where(
        Task.project_id == project_id,
        or_(
            task_type.is_(None),
            task_type.not_in(sorted(BACKEND_MANAGED_TASK_TYPES)),
        ),
    )


USE_POSTGRES = os.getenv("USE_TEST_POSTGRES", "").lower() == "true" and os.getenv(
    "DATABASE_URL", ""
).startswith("postgresql")


def _assert_generated_at_iso_utc(payload: dict) -> None:
    generated_at = payload.get("generated_at")
    assert isinstance(generated_at, str) and generated_at
    parsed = datetime.fromisoformat(generated_at)
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Account pool fixture (required by _spawn_research_task) ───────────────────


@pytest.fixture(autouse=True)
def _ensure_test_account_pool():
    """Ensure _spawn_research_task finds an available account for the test user."""
    uid = "test-user-123"
    entry = {
        "id": "test-acct-ss",
        "user_id": uid,
        "name": "test",
        "enabled": True,
        "active_tasks": 0,
        "oauth_token": "sk-ant-test",
        "total_tasks": 0,
        "token_created_at": None,
        "claude_config_json": None,
    }
    _max_plan_pools.setdefault(uid, []).append(entry)
    _account_by_id["test-acct-ss"] = entry
    yield
    _max_plan_pools.get(uid, [])[:] = [
        a for a in _max_plan_pools.get(uid, []) if a["id"] != "test-acct-ss"
    ]
    _account_by_id.pop("test-acct-ss", None)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _make_project(**kwargs) -> Project:
    """Build a Project with sensible test defaults."""
    defaults = {
        "name": "Test Project",
        "user_id": "test-user-123",
        "autonomous_enabled": True,
        "directions": [],
        "vision": {},
        "integration_branch": "automain",
        "promotion_target": "main",
    }
    defaults.update(kwargs)
    return Project(**defaults)


def _local_continuation_marker(**overrides) -> dict:
    marker = {
        "version": 1,
        "active": True,
        "operator_user_id": "test-user-123",
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    }
    marker.update(overrides)
    return marker


def _make_local_runner(project_id: uuid.UUID, **overrides) -> LocalRunner:
    defaults = {
        "project_id": project_id,
        "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,
        "status": "active",
        "capabilities": {
            "task_types": "all",
            "has_gpu": False,
            "max_concurrent": 2,
        },
        "last_seen_at": datetime.now(timezone.utc).replace(tzinfo=None),
    }
    defaults.update(overrides)
    return LocalRunner(**defaults)


async def _seed_legacy_cycles(session, project: Project) -> None:
    """Audit W2: seed StrategicCycle rows from legacy ``vision._strategic_cycle_history``.

    Tests historically built cycle history as a JSONB list inline in vision.
    The W2 migration moved cycles to a real table; this helper backfills the
    table from the legacy shape so existing test fixtures still produce the
    same agent-context payload without rewriting every test.
    """
    from app.models import StrategicCycle as _SC
    from app.time_utils import _utcnow as _ut
    from datetime import datetime as _dt

    raw = (project.vision or {}).get("_strategic_cycle_history") or []
    if not raw:
        return

    def _parse_ts(v):
        if not v:
            return None
        try:
            d = _dt.fromisoformat(str(v).replace("Z", "+00:00"))
            return d.replace(tzinfo=None) if d.tzinfo else d
        except Exception:
            return None

    for entry in raw:
        if not isinstance(entry, dict) or not entry.get("cycle_id"):
            continue
        # Skip if a row already exists for this cycle id.
        existing = await session.get(_SC, entry["cycle_id"])
        if existing is not None:
            continue
        meta = {
            k: v
            for k, v in entry.items()
            if k
            not in {
                "cycle_id",
                "outcome",
                "phase1_task_id",
                "phase1_completed_at",
                "phase2_task_id",
                "phase2_completed_at",
                "phase1_started_at",
                "directions_created",
                "directions_retired",
                "tasks_spawned",
                "vision_updated",
                "cycle_reasoning",
                "summary_text",
                "started_at",
            }
        }
        session.add(
            _SC(
                id=entry["cycle_id"],
                project_id=project.id,
                outcome=entry.get("outcome") or "in_progress",
                phase1_task_id=None,  # legacy entries store str not UUID; skip FK
                phase1_started_at=_parse_ts(entry.get("started_at"))
                or _parse_ts(entry.get("phase1_started_at"))
                or _ut(),
                phase1_completed_at=_parse_ts(entry.get("phase1_completed_at")),
                phase2_started_at=_parse_ts(entry.get("phase2_started_at")),
                phase2_completed_at=_parse_ts(entry.get("phase2_completed_at")),
                directions_created=int(entry.get("directions_created") or 0),
                directions_retired=int(entry.get("directions_retired") or 0),
                tasks_spawned=int(entry.get("tasks_spawned") or 0),
                vision_updated=bool(entry.get("vision_updated", False)),
                cycle_reasoning=entry.get("cycle_reasoning"),
                summary_text=entry.get("summary_text"),
                cycle_metadata=meta,
            )
        )
    await session.commit()


def _patch_spawning_deps(mocker):
    """Patch external dependencies required by the spawning functions."""
    mocker.patch(
        "app.strategic_spawning.validate_internal_payload",
        side_effect=lambda p, _src: p,
    )
    mocker.patch(
        "app.strategic_analysis._build_outcome_evaluation",
        return_value={},
    )


@pytest.fixture
def _lts_split_active(monkeypatch):
    """Activate the LTS split for version-lifecycle tests.

    The strategic version-phase block is gated on the integration branch being
    a real trunk distinct from the promotion target. Override the per-project
    resolvers (the global runtime_config bridge was removed) so tests using
    `_make_project()` without an explicit `integration_branch` column see a
    distinct trunk/target pair.
    """
    monkeypatch.setattr(
        "app.strategic_spawning.get_project_integration_branch",
        lambda _p: "automain",
    )
    monkeypatch.setattr(
        "app.strategic_spawning.get_project_promotion_target",
        lambda _p: "main",
    )
    yield
