# AUTO-SPLIT from test_admin_routers.py — see refactor: split mega backend test files.
# Behavior-preserving: conftest.py fixtures (db_session, client, override_get_session) are
# inherited automatically; shared helpers (_make_project) are duplicated here to keep
# this file self-contained. This file holds the autonomous-mode domain slice: pause-all,
# toggle, pause/resume, autonomous-status, direction-task-counts, directions-lite, and
# autonomy-debug endpoints.
"""Domain slice: admin_projects.py — Autonomous Mode

Split from test_admin_routers.py. Covers pause-all autonomous mode, per-project
toggle, pause/resume, autonomous-status, direction task counts, directions lite
mode, and autonomy-debug endpoints."""

from datetime import timedelta

import pytest
from httpx import AsyncClient

from app.models import (
    DiscoveryCycle,
    Project,
    ProjectMember,
    Task,
    TaskStatus,
    _utcnow,
)


# ── Constants ──────────────────────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"
TEST_USER_EMAIL = "test@example.com"


# ── Helpers ────────────────────────────────────────────────────────────────────


async def _make_project(session, owner_id=TEST_USER_ID, name="Test Project", role="owner"):
    """Create a project with the given user as a member, plus the membership row.

    ``role`` defaults to ``owner``; pass ``viewer``/``member`` to exercise the
    role floor on mutating endpoints (viewer is documented read-only).
    """
    project = Project(name=name, user_id=owner_id)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    member = ProjectMember(project_id=project.id, user_id=owner_id, role=role)
    session.add(member)
    await session.commit()
    return project


# ═══════════════════════════════════════════════════════════════════════════════
# admin_projects.py — Autonomous Mode
# ═══════════════════════════════════════════════════════════════════════════════


class TestAutonomousMode:
    @pytest.mark.asyncio
    async def test_pause_all_autonomous(self, client: AsyncClient, db_session):
        """pause-all disables autonomous mode on all user projects."""
        project = await _make_project(db_session)
        project.autonomous_enabled = True
        db_session.add(project)
        await db_session.commit()

        resp = await client.post("/api/admin/autonomous/pause-all")
        assert resp.status_code == 200
        assert resp.json()["paused"] == 1

    @pytest.mark.asyncio
    async def test_pause_all_skips_viewer_projects(
        self, client: AsyncClient, db_session
    ):
        """pause-all must not touch projects where the caller is only a viewer.

        Viewers are read-only: disabling autonomous mode is a mutation they may
        not perform, so the project is left running and excluded from the count.

        This route takes no ``project_id``, so it is inherently the "pause
        everything I can" bulk shape — scoping viewer-only projects out silently
        is correct, but it must report what it left RUNNING via ``skipped_count``
        rather than hiding it.
        """
        viewed = await _make_project(db_session, name="Viewed", role="viewer")
        viewed.autonomous_enabled = True
        db_session.add(viewed)
        await db_session.commit()

        resp = await client.post("/api/admin/autonomous/pause-all")
        assert resp.status_code == 200
        assert resp.json()["paused"] == 0
        # The still-running viewer project is surfaced, not silently dropped.
        assert resp.json()["skipped_count"] == 1

        await db_session.refresh(viewed)
        assert viewed.autonomous_enabled is True

    @pytest.mark.asyncio
    async def test_pause_all_pauses_member_projects(
        self, client: AsyncClient, db_session
    ):
        """The member floor must not over-reject: a member (not owner) can pause."""
        owned = await _make_project(db_session, name="Member Project", role="member")
        owned.autonomous_enabled = True
        db_session.add(owned)
        await db_session.commit()

        resp = await client.post("/api/admin/autonomous/pause-all")
        assert resp.status_code == 200
        assert resp.json()["paused"] == 1

        await db_session.refresh(owned)
        assert owned.autonomous_enabled is False

    @pytest.mark.asyncio
    async def test_toggle_autonomous_enable(self, client: AsyncClient, db_session):
        """Owner can enable autonomous mode on a project."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/autonomous",
            json={"enabled": True},
        )
        assert resp.status_code == 200
        assert resp.json()["autonomous_enabled"] is True

    @pytest.mark.asyncio
    async def test_toggle_autonomous_disable(self, client: AsyncClient, db_session):
        """Owner can disable autonomous mode on a project."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/autonomous",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["autonomous_enabled"] is False

    @pytest.mark.asyncio
    async def test_toggle_autonomous_missing_field_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Missing 'enabled' key returns 400."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/autonomous",
            json={"wrong_field": True},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_pause_project(self, client: AsyncClient, db_session):
        """Owner can pause a project; pause is independent of autonomous_enabled."""
        project = await _make_project(db_session)
        project.autonomous_enabled = True
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/pause",
            json={"paused": True},
        )
        assert resp.status_code == 200
        assert resp.json()["paused"] is True

        await db_session.refresh(project)
        assert project.paused is True
        # Pause must not touch the autonomous toggle.
        assert project.autonomous_enabled is True

    @pytest.mark.asyncio
    async def test_resume_project(self, client: AsyncClient, db_session):
        """Owner can resume (unpause) a paused project."""
        project = await _make_project(db_session)
        project.paused = True
        db_session.add(project)
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/pause",
            json={"paused": False},
        )
        assert resp.status_code == 200
        assert resp.json()["paused"] is False

        await db_session.refresh(project)
        assert project.paused is False

    @pytest.mark.asyncio
    async def test_pause_project_missing_field_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Missing 'paused' key returns 400."""
        project = await _make_project(db_session)

        resp = await client.patch(
            f"/api/admin/projects/{project.id}/pause",
            json={"wrong_field": True},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_pause_project_requires_owner(self, client: AsyncClient, db_session):
        """A non-owner member cannot pause a project (owner-only mutation)."""
        owner_project = Project(name="Owner Pause Project", user_id="other-owner")
        db_session.add(owner_project)
        await db_session.commit()
        await db_session.refresh(owner_project)
        db_session.add(
            ProjectMember(
                project_id=owner_project.id, user_id="other-owner", role="owner"
            )
        )
        db_session.add(
            ProjectMember(
                project_id=owner_project.id, user_id=TEST_USER_ID, role="member"
            )
        )
        await db_session.commit()

        resp = await client.patch(
            f"/api/admin/projects/{owner_project.id}/pause",
            json={"paused": True},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_paused_field_in_list_and_detail(
        self, client: AsyncClient, db_session
    ):
        """The additive `paused` field appears in both list and detail responses."""
        project = await _make_project(db_session, name="Paused Field Project")
        project.paused = True
        db_session.add(project)
        await db_session.commit()

        list_resp = await client.get("/api/admin/projects")
        assert list_resp.status_code == 200
        target = next(
            p for p in list_resp.json()["items"] if p["id"] == str(project.id)
        )
        assert target["paused"] is True

        detail_resp = await client.get(f"/api/admin/projects/{project.id}")
        assert detail_resp.status_code == 200
        assert detail_resp.json()["paused"] is True

    @pytest.mark.asyncio
    async def test_get_autonomous_status(self, client: AsyncClient, db_session):
        """Returns autonomous status summary across all user projects."""
        from app.shared_state import runtime_config
        from datetime import timedelta
        from app.models import _utcnow

        project = await _make_project(db_session, name="Auto Project")

        # Seed a downstream servicing orchestrator task to confirm the
        # health view separates churn from initiative spawns.
        non_research_task = Task(
            project_id=project.id,
            payloads=[{"type": "review", "pr_number": 1}],
            status=TaskStatus.COMPLETED,
            created_by="ORCHESTRATOR",
            created_at=_utcnow() - timedelta(minutes=30),
        )
        db_session.add(non_research_task)
        await db_session.commit()

        resp = await client.get("/api/admin/autonomous-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "projects" in data
        by_id = {p["project_id"]: p for p in data["projects"]}
        assert str(project.id) in by_id
        entry = by_id[str(project.id)]
        # Freshly created project: autonomous_enabled defaults True (model default),
        # no failures, rate_limit mirrors global config, source is fleet_default.
        assert entry["autonomous_enabled"] is True
        assert entry["consecutive_failures"] == 0
        # Total churn is still visible, but it is separated from initiative.
        assert entry["tasks_spawned_last_hour"] == 1
        assert entry["initiative_tasks_spawned_last_hour"] == 0
        assert entry["servicing_tasks_spawned_last_hour"] == 1
        assert entry["research_create_tasks_spawned_last_hour"] == 0
        assert entry["last_research_at"] is None
        assert entry["rate_limit"] == runtime_config["max_autonomous_tasks_per_hour"]
        assert (
            entry["fleet_default_rate_limit"]
            == runtime_config["max_autonomous_tasks_per_hour"]
        )
        assert entry["rate_limit_source"] == "fleet_default"
        assert "global_rate_limit" in data
        assert data["global_rate_limit"]["global_headroom"] >= 0

    @pytest.mark.asyncio
    async def test_get_autonomous_status_uses_runtime_categories(
        self, client: AsyncClient, db_session
    ):
        """Custom autonomous spawn categories stay aligned with the status split."""
        from app.shared_state import runtime_config
        from datetime import timedelta
        from app.models import _utcnow

        project = await _make_project(db_session, name="Categorized Auto Project")
        original_categories = runtime_config.get("autonomous_spawn_budget_categories")
        runtime_config["autonomous_spawn_budget_categories"] = {
            "initiative": ["review"],
            "servicing": ["qa", "deps", "security_audit"],
        }
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "review"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=_utcnow() - timedelta(minutes=30),
            )
        )
        await db_session.commit()
        try:
            resp = await client.get("/api/admin/autonomous-status")
            assert resp.status_code == 200
            data = resp.json()
            entry = next(p for p in data["projects"] if p["project_id"] == str(project.id))
            assert entry["tasks_spawned_last_hour"] == 1
            assert entry["initiative_tasks_spawned_last_hour"] == 1
            assert entry["servicing_tasks_spawned_last_hour"] == 0
            assert entry["other_tasks_spawned_last_hour"] == 0
        finally:
            runtime_config["autonomous_spawn_budget_categories"] = original_categories

    @pytest.mark.asyncio
    async def test_get_autonomous_status_project_override(
        self, client: AsyncClient, db_session
    ):
        """Project with build_config override reports the override cap and correct source."""
        from app.shared_state import runtime_config
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session, name="Override Project")
        project.build_config = {"max_autonomous_tasks_per_hour": 5}
        flag_modified(project, "build_config")
        await db_session.commit()
        await db_session.refresh(project)

        resp = await client.get("/api/admin/autonomous-status")
        assert resp.status_code == 200
        data = resp.json()
        by_id = {p["project_id"]: p for p in data["projects"]}
        assert str(project.id) in by_id
        entry = by_id[str(project.id)]
        assert entry["rate_limit"] == 5
        assert (
            entry["fleet_default_rate_limit"]
            == runtime_config["max_autonomous_tasks_per_hour"]
        )
        assert entry["rate_limit_source"] == "project_override"

    @pytest.mark.asyncio
    async def test_get_direction_task_counts(self, client: AsyncClient, db_session):
        """Returns per-direction task counts for a project."""
        project = await _make_project(db_session)
        direction_id = "d-abc123"
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research", "current_direction_id": direction_id}],
                status=TaskStatus.IN_PROGRESS,
            )
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/directions/task-counts"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) >= {"counts", "counts_by_type", "active_tasks"}
        assert body["counts"].get(direction_id) == 1
        assert body["counts_by_type"][direction_id]["research"] == 1

    @pytest.mark.asyncio
    async def test_admin_tasks_direction_filter_matches_canonical_shapes(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        other_project = await _make_project(db_session)
        direction_id = "d-canonical"
        expected_tasks = [
            Task(
                project_id=project.id,
                title="context direction",
                context={"direction_id": direction_id},
                payloads=[{"type": "create"}],
                status=TaskStatus.QUEUED,
            ),
            Task(
                project_id=project.id,
                title="payload direction",
                payloads=[{"type": "research", "direction_id": direction_id}],
                status=TaskStatus.IN_PROGRESS,
            ),
            Task(
                project_id=project.id,
                title="current direction",
                payloads=[{"type": "research", "current_direction_id": direction_id}],
                status=TaskStatus.IN_PROGRESS,
            ),
            Task(
                project_id=project.id,
                title="directions array",
                payloads=[{"type": "plan", "directions": [{"id": direction_id}]}],
                status=TaskStatus.QUEUED,
            ),
            Task(
                project_id=project.id,
                title="plan output source",
                payloads=[{"type": "plan"}],
                plan_output={"source_direction_id": direction_id},
                status=TaskStatus.QUEUED,
            ),
            Task(
                project_id=project.id,
                title="nested finding",
                payloads=[{"type": "research"}],
                plan_output={"findings": [{"direction_id": direction_id}]},
                status=TaskStatus.QUEUED,
            ),
        ]
        excluded_tasks = [
            Task(
                project_id=project.id,
                title="other direction",
                context={"direction_id": "d-other"},
                payloads=[{"type": "create"}],
                status=TaskStatus.QUEUED,
            ),
            Task(
                project_id=other_project.id,
                title="cross project direction",
                context={"direction_id": direction_id},
                payloads=[{"type": "create"}],
                status=TaskStatus.QUEUED,
            ),
        ]
        db_session.add_all([*expected_tasks, *excluded_tasks])
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/tasks?project_id={project.id}&direction_id={direction_id}&limit=50"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        returned_ids = {row["id"] for row in body["tasks"]}
        assert returned_ids == {str(task.id) for task in expected_tasks}
        assert body["total"] == len(expected_tasks)

    @pytest.mark.asyncio
    async def test_get_direction_task_counts_matches_canonical_shapes(
        self, client: AsyncClient, db_session
    ):
        project = await _make_project(db_session)
        other_project = await _make_project(db_session)
        direction_id = "d-counts"
        db_session.add_all(
            [
                Task(
                    project_id=project.id,
                    context={"direction_id": direction_id},
                    payloads=[{"type": "create"}],
                    status=TaskStatus.QUEUED,
                ),
                Task(
                    project_id=project.id,
                    payloads=[{"type": "research", "direction_id": direction_id}],
                    status=TaskStatus.IN_PROGRESS,
                ),
                Task(
                    project_id=project.id,
                    payloads=[
                        {
                            "type": "research",
                            "current_direction_id": direction_id,
                            "directions": [{"id": "d-not-current"}],
                        }
                    ],
                    status=TaskStatus.IN_PROGRESS,
                ),
                Task(
                    project_id=project.id,
                    payloads=[{"type": "plan", "directions": [{"id": direction_id}]}],
                    status=TaskStatus.QUEUED,
                ),
                Task(
                    project_id=project.id,
                    payloads=[{"type": "plan"}],
                    plan_output={"research_findings": [{"source_direction_id": direction_id}]},
                    status=TaskStatus.QUEUED,
                ),
                Task(
                    project_id=project.id,
                    context={"direction_id": direction_id},
                    payloads=[{"type": "failed"}],
                    status=TaskStatus.FAILED,
                ),
                Task(
                    project_id=other_project.id,
                    context={"direction_id": direction_id},
                    payloads=[{"type": "create"}],
                    status=TaskStatus.QUEUED,
                ),
            ]
        )
        await db_session.commit()

        resp = await client.get(
            f"/api/admin/projects/{project.id}/directions/task-counts"
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # The task with direction_id only in plan_output is not counted: the endpoint
        # skips plan_output for performance (task_primary_direction_ids_shallow).
        assert body["counts"][direction_id] == 4
        assert "d-not-current" not in body["counts"]
        assert body["counts_by_type"][direction_id]["create"] == 1
        assert body["counts_by_type"][direction_id]["research"] == 2
        assert body["counts_by_type"][direction_id]["plan"] == 1

    @pytest.mark.asyncio
    async def test_directions_lite_mode_omits_heavy_fields(
        self, client: AsyncClient, db_session
    ):
        """?lite=1 omits design_doc, problem_model, self_reflection_log, and
        running_update_reconciliation while preserving directions and lightweight fields."""
        from sqlalchemy.orm.attributes import flag_modified

        project = await _make_project(db_session)
        project.directions = [{"id": "d-lite1", "text": "Lite test", "priority": "medium"}]
        project.design_doc = {"key": "large doc"}
        project.problem_model = {"intent": {"purpose": "Build X."}}
        project.self_reflection_log = [{"entry": "some reflection"}]
        flag_modified(project, "directions")
        flag_modified(project, "design_doc")
        flag_modified(project, "problem_model")
        flag_modified(project, "self_reflection_log")
        db_session.add(project)
        await db_session.commit()

        # Full response includes heavy fields
        full = await client.get(f"/api/admin/projects/{project.id}/directions")
        assert full.status_code == 200
        full_data = full.json()
        assert "design_doc" in full_data
        assert "problem_model" in full_data
        assert "self_reflection_log" in full_data
        assert len(full_data["directions"]) == 1

        # Lite response omits heavy fields but keeps directions and lightweight fields
        lite = await client.get(
            f"/api/admin/projects/{project.id}/directions?lite=1"
        )
        assert lite.status_code == 200
        lite_data = lite.json()
        assert "design_doc" not in lite_data
        assert "problem_model" not in lite_data
        assert "self_reflection_log" not in lite_data
        assert "running_update_reconciliation" not in lite_data
        assert len(lite_data["directions"]) == 1
        assert "autonomous_enabled" in lite_data
        assert "running_update" in lite_data


class TestAutonomyDebug:
    """Tests for GET /api/admin/projects/{project_id}/autonomy-debug."""

    @pytest.mark.asyncio
    async def test_strategic_lane_flag_is_informational_for_tactical_autonomy(
        self, client: AsyncClient, db_session
    ):
        """Turning off automatic Strategic cycles must not add a tactical
        ``spawn_blocked`` guard to the autonomy projection."""
        project = await _make_project(db_session, name="Tactical debug isolation")

        enabled_resp = await client.get(
            f"/api/admin/projects/{project.id}/autonomy-debug"
        )
        assert enabled_resp.status_code == 200, enabled_resp.text
        enabled = enabled_resp.json()
        assert enabled["automatic_strategic_cycles_enabled"] is True

        project.build_config = {"automatic_strategic_cycles_enabled": False}
        db_session.add(project)
        await db_session.commit()

        disabled_resp = await client.get(
            f"/api/admin/projects/{project.id}/autonomy-debug"
        )
        assert disabled_resp.status_code == 200, disabled_resp.text
        disabled = disabled_resp.json()
        assert disabled["automatic_strategic_cycles_enabled"] is False
        assert disabled["spawn_blocked"] == enabled["spawn_blocked"]
        assert {guard["guard"] for guard in disabled["blocking_guards"]} == {
            guard["guard"] for guard in enabled["blocking_guards"]
        }
        assert "automatic_strategic_cycles_disabled" not in {
            guard["guard"] for guard in disabled["blocking_guards"]
        }

    @pytest.mark.asyncio
    async def test_rate_limit_counts_only_research_tasks(
        self, client: AsyncClient, db_session
    ):
        """tasks_this_hour in rate_limit block counts only research-type ORCHESTRATOR
        tasks, not non-research ORCHESTRATOR tasks created in the last hour.

        This exercises the dialect-specific SQL COUNT that replaced the Python-side
        sum() loop — verifying it filters on payloads[0].type == 'research'.
        """
        from datetime import timedelta
        from app.models import _utcnow

        project = await _make_project(db_session, name="AutonomyDebug Rate Limit")
        now = _utcnow()

        # Research task within the last hour — should count.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(minutes=20),
            )
        )
        # Another research task within the last hour — should also count.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(minutes=10),
            )
        )
        # Non-research ORCHESTRATOR task within the last hour — must NOT count.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "create"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(minutes=15),
            )
        )
        # Research task older than one hour — must NOT count.
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research"}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=now - timedelta(hours=2),
            )
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{project.id}/autonomy-debug")
        assert resp.status_code == 200, resp.text
        data = resp.json()

        assert "rate_limit" in data
        tasks_this_hour = data["rate_limit"]["tasks_this_hour"]
        assert tasks_this_hour == 2, (
            f"Expected 2 research tasks this hour, got {tasks_this_hour}. "
            "Non-research and out-of-window tasks must be excluded."
        )

    @pytest.mark.asyncio
    async def test_nine_debug_eligible_but_zero_scheduler_runnable_regression(
        self,
        client: AsyncClient,
        db_session,
    ):
        """The debug view and scheduler share discovery-root admission truth.

        This recreates the live 9/10 false-green shape: nine directions look
        active and out of cooldown but are actually in review_pending; the
        tenth is refractory.  Both surfaces must report zero runnable roots.
        """

        from app.autonomous_research import _cycle_blocked_direction_keys

        project = await _make_project(db_session, name="AutonomyDebug Cycle Parity")
        directions = [
            {
                "id": f"d-parity-{index}",
                "text": f"Direction {index}",
                "status": "active",
                "priority": "medium",
                "completion_criteria": f"Direction {index} is verified.",
            }
            for index in range(10)
        ]
        directions[-1]["refractory_until"] = (
            _utcnow() + timedelta(hours=1)
        ).isoformat()
        project.directions = directions
        db_session.add(project)
        await db_session.flush()
        db_session.add_all(
            [
                DiscoveryCycle(
                    project_id=project.id,
                    direction_key=direction["id"],
                    generation=1,
                    outcome_kind="mixed",
                    state="review_pending",
                    bet_snapshot={"direction": dict(direction)},
                )
                for direction in directions[:9]
            ]
        )
        await db_session.commit()

        scheduler_blocked = await _cycle_blocked_direction_keys(
            db_session,
            project.id,
            directions,
        )
        assert scheduler_blocked == {
            direction["id"] for direction in directions[:9]
        }

        resp = await client.get(f"/api/admin/projects/{project.id}/autonomy-debug")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        by_id = {direction["id"]: direction for direction in data["directions"]}

        assert sum(direction["is_eligible"] for direction in by_id.values()) == 0
        assert {
            by_id[direction["id"]]["block_reason_code"]
            for direction in directions[:9]
        } == {"discovery_review_pending"}
        assert all(
            by_id[direction["id"]]["next_action"]
            for direction in directions[:9]
        )
        guard = next(
            item
            for item in data["blocking_guards"]
            if item["guard"] == "no_eligible_directions"
        )
        assert guard["discovery_blocked_direction_count"] == 9
        assert guard["total_active_directions"] == 10

    @pytest.mark.asyncio
    async def test_autonomy_debug_reports_global_backstop_and_project_pause(
        self, client: AsyncClient, db_session
    ):
        """The debug endpoint mirrors the global backstop and Project.paused guard."""
        import app.shared_state as _shared_state

        project = await _make_project(db_session, name="AutonomyDebug Pause")
        project.paused = True
        await db_session.commit()
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "review", "pr_number": 77}],
                status=TaskStatus.COMPLETED,
                created_by="ORCHESTRATOR",
                created_at=_utcnow() - timedelta(minutes=10),
            )
        )
        await db_session.commit()

        original_global = _shared_state.runtime_config[
            "max_global_autonomous_tasks_per_hour"
        ]
        _shared_state.runtime_config["max_global_autonomous_tasks_per_hour"] = 1
        try:
            resp = await client.get(f"/api/admin/projects/{project.id}/autonomy-debug")
        finally:
            _shared_state.runtime_config[
                "max_global_autonomous_tasks_per_hour"
            ] = original_global

        assert resp.status_code == 200, resp.text
        data = resp.json()
        guards = {guard["guard"]: guard for guard in data["blocking_guards"]}
        assert "project_paused" in guards
        assert "global_rate_limit_exceeded" in guards
        assert guards["global_rate_limit_exceeded"]["current"] == 1
        assert guards["global_rate_limit_exceeded"]["limit"] == 1
        assert data["global_rate_limit"]["global_cap"] == 1
        assert data["global_rate_limit"]["global_headroom"] == 0

    @pytest.mark.asyncio
    async def test_autonomy_debug_reports_total_research_cap_guard(
        self, client: AsyncClient, db_session
    ):
        """The debug endpoint reports the total-research cap
        (`_spawn_research_task_inner`'s second cap) as its own blocking guard —
        previously this cap was enforced but entirely absent from the debug view,
        so an operator staring at "why isn't research spawning" could not see it.

        The debug view must count exactly the statuses enforcement counts. With
        the headroom relaxation on (the default) AND the fleet above the
        min-free-slots floor, EVALUATING parents are excluded from the total;
        with the operator escape hatch set to false they are included. Both are
        asserted here so the debug view cannot drift from the spawn path.
        """
        from app.shared_state import runtime_config

        project = await _make_project(db_session, name="AutonomyDebug TotalCap")
        # `max_total_research` (not `..._per_project`) is the build_config
        # override key `project_parallelism_cap` reads — `..._per_project` is
        # only the global runtime_config fallback key.
        project.build_config = {"max_total_research": 2}
        await db_session.commit()

        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research", "current_direction_id": "d1"}],
                status=TaskStatus.QUEUED,
                created_by="ORCHESTRATOR",
                created_at=_utcnow() - timedelta(minutes=5),
            )
        )
        db_session.add(
            Task(
                project_id=project.id,
                payloads=[{"type": "research", "current_direction_id": "d2"}],
                status=TaskStatus.EVALUATING,
                created_by="ORCHESTRATOR",
                created_at=_utcnow() - timedelta(minutes=3),
            )
        )
        await db_session.commit()

        async def _guards():
            resp = await client.get(
                f"/api/admin/projects/{project.id}/autonomy-debug"
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            return {g["guard"]: g for g in data["blocking_guards"]}

        original = runtime_config.get("research_headroom_ignores_evaluating", True)

        # Escape hatch OFF (relaxation disabled) → EVALUATING counts, so
        # QUEUED + EVALUATING = 2 reaches the cap and the guard fires. This is
        # the branch enforcement takes when the fleet is below the floor too.
        runtime_config["research_headroom_ignores_evaluating"] = False
        try:
            guards = await _guards()
            assert "total_research_active" in guards, (
                "Expected total_research_active guard when QUEUED+EVALUATING "
                "research (2) reaches max_total_research (2) with the "
                f"relaxation disabled; got guards: {list(guards)}"
            )
            assert guards["total_research_active"]["total_research_count"] == 2
            assert guards["total_research_active"]["max_total_research"] == 2
            assert "including EVALUATING" in guards["total_research_active"]["reason"]
            # The parallel-research cap (default 6) must NOT also fire — only one
            # of the two tasks (QUEUED) counts against it, well under the cap —
            # so the new guard exercises a genuinely distinct enforcement path.
            assert "research_task_active" not in guards

            # Relaxation ON (the default) with the fleet above the floor →
            # EVALUATING is excluded, so only the QUEUED task counts (1 < 2) and
            # the guard must NOT fire. Mirrors `_spawn_research_task_inner`,
            # which would admit here.
            runtime_config["research_headroom_ignores_evaluating"] = True
            guards = await _guards()
            assert "vm_slots_full" not in guards, (
                "precondition: this assertion only holds while the fleet is "
                f"above the min-free-slots floor; got guards: {list(guards)}"
            )
            assert "total_research_active" not in guards, (
                "With the relaxation on and headroom above the floor the "
                "EVALUATING parent must not count, leaving 1 < 2; got: "
                f"{guards.get('total_research_active')}"
            )

            # ...and the guard is still reported under the default once the
            # statuses enforcement DOES count reach the cap.
            db_session.add(
                Task(
                    project_id=project.id,
                    payloads=[{"type": "research", "current_direction_id": "d3"}],
                    status=TaskStatus.QUEUED,
                    created_by="ORCHESTRATOR",
                    created_at=_utcnow() - timedelta(minutes=1),
                )
            )
            await db_session.commit()
            guards = await _guards()
            assert "total_research_active" in guards, (
                "Expected total_research_active guard when two QUEUED research "
                f"tasks reach max_total_research (2); got guards: {list(guards)}"
            )
            assert guards["total_research_active"]["total_research_count"] == 2
            assert "excluding EVALUATING" in guards["total_research_active"]["reason"]
        finally:
            runtime_config["research_headroom_ignores_evaluating"] = original

    @pytest.mark.asyncio
    async def test_autonomy_debug_max_per_hour_default_matches_enforcement(
        self, client: AsyncClient, db_session
    ):
        """The debug endpoint's default `max_per_hour` (no override, no runtime
        override) must match the spawn path's default of 12, not a stale 5 —
        otherwise the debug view can report a rate-limit block enforcement
        would not actually apply (or vice versa)."""
        from app.shared_state import runtime_config

        project = await _make_project(db_session, name="AutonomyDebug PerHourDefault")
        await db_session.commit()

        original = runtime_config.get("max_autonomous_tasks_per_hour")
        runtime_config.pop("max_autonomous_tasks_per_hour", None)
        try:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/autonomy-debug"
            )
        finally:
            if original is not None:
                runtime_config["max_autonomous_tasks_per_hour"] = original

        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rate_limit"]["max_per_hour"] == 12

    @pytest.mark.asyncio
    async def test_autonomy_debug_reports_total_research_guard(
        self, client: AsyncClient, db_session
    ):
        """total_research_active mirrors _spawn_research_task_inner's second cap:
        it fires once total non-terminal research reaches max_total_research, but
        excludes EVALUATING parents that still have active (non-terminal)
        children — those are productive in-flight work, not stuck backlog.

        The EVALUATING-counting path only applies when the headroom relaxation
        is not in effect (that relaxation excludes EVALUATING entirely — see
        test_autonomy_debug_reports_total_research_cap_guard), so the escape
        hatch is pinned off here to exercise the productive-children exclusion.
        """
        from app.shared_state import runtime_config

        project = await _make_project(db_session, name="AutonomyDebug Total Research")
        project.build_config = {"max_total_research": 1}
        await db_session.commit()

        # A genuinely-stuck EVALUATING research parent with no active children —
        # counts against the cap and alone should trip it (cap == 1).
        stuck_parent = Task(
            project_id=project.id,
            payloads=[{"type": "research", "current_direction_id": "d-stuck"}],
            status=TaskStatus.EVALUATING,
            created_by="ORCHESTRATOR",
        )
        db_session.add(stuck_parent)
        await db_session.commit()

        original = runtime_config.get("research_headroom_ignores_evaluating", True)
        runtime_config["research_headroom_ignores_evaluating"] = False
        try:
            resp = await client.get(
                f"/api/admin/projects/{project.id}/autonomy-debug"
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            guards = {guard["guard"]: guard for guard in data["blocking_guards"]}
            assert "total_research_active" in guards, (
                "Expected total_research_active guard with a stuck EVALUATING parent "
                f"and max_total_research=1. Got guards: {list(guards)}"
            )
            assert guards["total_research_active"]["total_research_count"] == 1
            assert guards["total_research_active"]["max_total_research"] == 1

            # Now give the EVALUATING parent an active (non-terminal) child — it
            # becomes productive in-flight work and must be excluded from the
            # count, dropping total_research_count back to 0 and clearing the
            # guard.
            db_session.add(
                Task(
                    project_id=project.id,
                    parent_task_id=stuck_parent.id,
                    payloads=[{"type": "create"}],
                    status=TaskStatus.IN_PROGRESS,
                    created_by="ORCHESTRATOR",
                )
            )
            await db_session.commit()

            resp = await client.get(
                f"/api/admin/projects/{project.id}/autonomy-debug"
            )
            assert resp.status_code == 200, resp.text
            data = resp.json()
            guards = {guard["guard"]: guard for guard in data["blocking_guards"]}
            assert "total_research_active" not in guards, (
                "EVALUATING parent with an active child is productive in-flight "
                f"work and must not count against max_total_research. Got guards: {list(guards)}"
            )
        finally:
            runtime_config["research_headroom_ignores_evaluating"] = original

        # And the endpoint's max_per_hour default must match enforcement's 12,
        # not the stale 5 the debug endpoint previously reported.
        assert data["rate_limit"]["max_per_hour"] == 12
