"""Tests for the self_audit task end-to-end:

1. ``_handle_self_audit_completion`` ingest path — SELF_AUDIT_REPORT logs
   produce SystemMaturity rows, idempotent re-ingest, malformed entries are
   dropped with a warning, invalid recommended_mode is rejected.
2. ``_normalize_self_audit_finding`` — pure-function validator behavior.
3. ``POST /api/admin/projects/{project_id}/self-audit`` — enqueues a Task with
   the right payload type and forwards focus_systems / max_systems.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.models import (
    ProjectMember,
    SystemMaturity,
    Task,
    TaskStatus,
)
from app.self_audit_dispatch import _should_dispatch_self_audit
from app.task_completion import (
    _handle_self_audit_completion,
    _normalize_self_audit_finding,
    _parse_self_audit_findings,
)
from testing.factories import make_project, make_task


TEST_USER_ID = "test-user-123"


async def _seed_project_with_member(session):
    project = make_project(name="Self Audit Test", user_id=TEST_USER_ID)
    session.add(project)
    await session.commit()
    await session.refresh(project)
    session.add(
        ProjectMember(project_id=project.id, user_id=TEST_USER_ID, role="owner")
    )
    await session.commit()
    return project


# ── Pure-function validators ────────────────────────────────────────────────


class TestNormalizeFinding:
    def test_full_valid_finding_round_trips(self):
        raw = {
            "system_id": "sys-auth",
            "score": 72,
            "components": {
                "test_coverage": 80,
                "friction_density": 65,
                "claim_consistency": 75,
                "failure_mix": 70,
            },
            "top_issues": ["Missing integration tests", "Flaky CI"],
            "recommended_mode": "fundamentals",
            "evidence": [{"file": "backend/app/auth.py", "summary": "no tests"}],
        }
        norm = _normalize_self_audit_finding(raw)
        assert norm is not None
        assert norm["system_id"] == "sys-auth"
        assert norm["score"] == 72
        assert norm["recommended_mode"] == "fundamentals"
        assert norm["components"]["test_coverage"] == 80
        # string issues get wrapped as {issue: str}
        assert norm["top_issues"][0] == {"issue": "Missing integration tests"}
        assert norm["evidence"][0]["file"] == "backend/app/auth.py"

    def test_invalid_recommended_mode_rejected(self):
        raw = {"system_id": "sys-x", "score": 50, "recommended_mode": "panic"}
        assert _normalize_self_audit_finding(raw) is None

    def test_missing_system_id_rejected(self):
        assert (
            _normalize_self_audit_finding({"score": 50, "recommended_mode": "ship"})
            is None
        )
        assert _normalize_self_audit_finding({"system_id": "", "score": 50}) is None
        assert _normalize_self_audit_finding({"system_id": "   ", "score": 50}) is None

    def test_score_clamped_to_0_100(self):
        assert (
            _normalize_self_audit_finding({"system_id": "s", "score": -10})["score"]
            == 0
        )
        assert (
            _normalize_self_audit_finding({"system_id": "s", "score": 999})["score"]
            == 100
        )

    def test_non_integer_score_rejected(self):
        assert _normalize_self_audit_finding({"system_id": "s", "score": "n/a"}) is None

    def test_recommended_mode_optional(self):
        norm = _normalize_self_audit_finding({"system_id": "s", "score": 50})
        assert norm is not None
        assert norm["recommended_mode"] is None

    def test_non_dict_returns_none(self):
        assert _normalize_self_audit_finding("not a dict") is None  # type: ignore[arg-type]
        assert _normalize_self_audit_finding(None) is None  # type: ignore[arg-type]


class TestParseFindings:
    def test_parses_object_with_findings_key(self):
        logs = json.dumps({"findings": [{"system_id": "a", "score": 50}]})
        out = _parse_self_audit_findings(logs)
        assert len(out) == 1
        assert out[0]["system_id"] == "a"

    def test_parses_bare_array(self):
        logs = json.dumps([{"system_id": "a", "score": 50}])
        out = _parse_self_audit_findings(logs)
        assert len(out) == 1

    def test_parses_array_with_surrounding_text(self):
        logs = 'preamble\n[{"system_id":"a","score":50}]\ntrailing'
        out = _parse_self_audit_findings(logs)
        assert len(out) == 1
        assert out[0]["system_id"] == "a"

    def test_empty_logs_returns_empty(self):
        assert _parse_self_audit_findings("") == []
        assert _parse_self_audit_findings("no json here") == []


# ── Completion ingest ───────────────────────────────────────────────────────


class TestSelfAuditCompletion:
    @pytest.mark.asyncio
    async def test_ingest_creates_system_maturity_rows(
        self, db_session, mock_broadcast
    ):
        project = await _seed_project_with_member(db_session)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()

        logs = json.dumps(
            {
                "findings": [
                    {
                        "system_id": "sys-auth",
                        "score": 72,
                        "components": {
                            "test_coverage": 80,
                            "friction_density": 65,
                            "claim_consistency": 75,
                            "failure_mix": 70,
                        },
                        "top_issues": ["Missing integration tests"],
                        "recommended_mode": "fundamentals",
                        "evidence": [
                            {"file": "backend/app/auth.py", "summary": "no tests"}
                        ],
                    },
                    {
                        "system_id": "sys-billing",
                        "score": 30,
                        "components": {
                            "test_coverage": 20,
                            "friction_density": 30,
                            "claim_consistency": 30,
                            "failure_mix": 30,
                        },
                        "top_issues": ["No tests at all"],
                        "recommended_mode": "audit",
                        "evidence": [],
                    },
                ]
            }
        )

        await _handle_self_audit_completion(task, logs, db_session)

        rows = (
            (
                await db_session.execute(
                    select(SystemMaturity).where(
                        SystemMaturity.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 2
        by_system = {r.system_id: r for r in rows}
        assert by_system["sys-auth"].score == 72
        assert by_system["sys-auth"].recommended_mode == "fundamentals"
        assert by_system["sys-auth"].source_task_id == task.id
        assert by_system["sys-billing"].score == 30
        assert by_system["sys-billing"].recommended_mode == "audit"

    @pytest.mark.asyncio
    async def test_ingest_is_idempotent(self, db_session, mock_broadcast):
        """Re-running an audit overwrites the existing row in place."""
        project = await _seed_project_with_member(db_session)
        first_task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(first_task)
        await db_session.commit()

        first_logs = json.dumps(
            {
                "findings": [
                    {
                        "system_id": "sys-auth",
                        "score": 50,
                        "recommended_mode": "fundamentals",
                        "components": {
                            "test_coverage": 50,
                            "friction_density": 50,
                            "claim_consistency": 50,
                            "failure_mix": 50,
                        },
                        "top_issues": [],
                        "evidence": [],
                    }
                ]
            }
        )
        await _handle_self_audit_completion(first_task, first_logs, db_session)

        # Re-audit with a different score / mode under the same project_id+system_id
        second_task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(second_task)
        await db_session.commit()

        second_logs = json.dumps(
            {
                "findings": [
                    {
                        "system_id": "sys-auth",
                        "score": 90,
                        "recommended_mode": "ship",
                        "components": {
                            "test_coverage": 90,
                            "friction_density": 90,
                            "claim_consistency": 90,
                            "failure_mix": 90,
                        },
                        "top_issues": ["nothing"],
                        "evidence": [{"file": "x.py", "summary": "ok"}],
                    }
                ]
            }
        )
        await _handle_self_audit_completion(second_task, second_logs, db_session)

        rows = (
            (
                await db_session.execute(
                    select(SystemMaturity).where(
                        SystemMaturity.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == 1
        row = rows[0]
        assert row.score == 90
        assert row.recommended_mode == "ship"
        assert row.source_task_id == second_task.id
        assert row.evidence[0]["file"] == "x.py"

    @pytest.mark.asyncio
    async def test_ingest_drops_invalid_recommended_mode(
        self, db_session, mock_broadcast
    ):
        project = await _seed_project_with_member(db_session)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()

        logs = json.dumps(
            {
                "findings": [
                    {"system_id": "sys-good", "score": 50, "recommended_mode": "ship"},
                    {
                        "system_id": "sys-bad",
                        "score": 50,
                        "recommended_mode": "explode",
                    },
                ]
            }
        )
        await _handle_self_audit_completion(task, logs, db_session)

        rows = (
            (
                await db_session.execute(
                    select(SystemMaturity).where(
                        SystemMaturity.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        # The valid finding lands; the invalid one is dropped.
        assert {r.system_id for r in rows} == {"sys-good"}

    @pytest.mark.asyncio
    async def test_ingest_no_findings_marks_status_message(
        self, db_session, mock_broadcast
    ):
        project = await _seed_project_with_member(db_session)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()

        await _handle_self_audit_completion(
            task, "completely unparseable garbage", db_session
        )
        await db_session.refresh(task)
        assert (
            task.latest_status_message
            and "no parseable findings" in task.latest_status_message
        )
        rows = (
            (
                await db_session.execute(
                    select(SystemMaturity).where(
                        SystemMaturity.project_id == project.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert rows == []

    @pytest.mark.asyncio
    async def test_propagates_fundamentals_to_matching_active_directions(
        self, db_session, mock_broadcast
    ):
        project = await _seed_project_with_member(db_session)
        project.directions = [{"id": "d1", "status": "active", "system_id": "sys-auth"}]
        db_session.add(project)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await _handle_self_audit_completion(
            task,
            json.dumps(
                {
                    "findings": [
                        {
                            "system_id": "sys-auth",
                            "score": 50,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "fundamentals"
        assert project.directions[0]["recommended_mode_meta"]["source"] == "self_audit"

    @pytest.mark.asyncio
    async def test_ship_does_not_downgrade_existing_audit_mode(
        self, db_session, mock_broadcast
    ):
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "audit",
            }
        ]
        db_session.add(project)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await _handle_self_audit_completion(
            task,
            json.dumps(
                {
                    "findings": [
                        {
                            "system_id": "sys-auth",
                            "score": 50,
                            "recommended_mode": "ship",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "audit"

    @pytest.mark.asyncio
    async def test_null_mode_does_not_touch_directions(
        self, db_session, mock_broadcast
    ):
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "audit",
            }
        ]
        db_session.add(project)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await _handle_self_audit_completion(
            task,
            json.dumps(
                {
                    "findings": [
                        {"system_id": "sys-auth", "score": 50, "recommended_mode": None}
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "audit"

    @pytest.mark.asyncio
    async def test_directions_without_system_id_are_unaffected(
        self, db_session, mock_broadcast
    ):
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {"id": "d1", "status": "active", "recommended_mode": "ship"}
        ]
        db_session.add(project)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await _handle_self_audit_completion(
            task,
            json.dumps(
                {
                    "findings": [
                        {
                            "system_id": "sys-auth",
                            "score": 50,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "ship"

    @pytest.mark.asyncio
    async def test_inactive_directions_are_skipped(self, db_session, mock_broadcast):
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "expired",
                "system_id": "sys-auth",
                "recommended_mode": "ship",
            }
        ]
        db_session.add(project)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await _handle_self_audit_completion(
            task,
            json.dumps(
                {
                    "findings": [
                        {
                            "system_id": "sys-auth",
                            "score": 50,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "ship"

    @pytest.mark.asyncio
    async def test_naive_strategic_set_at_does_not_crash_ingest(
        self, db_session, mock_broadcast
    ):
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "audit",
                "recommended_mode_meta": {
                    "source": "strategic",
                    "set_at": "2026-04-28T00:00:00",
                },
            }
        ]
        db_session.add(project)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await _handle_self_audit_completion(
            task,
            json.dumps(
                {
                    "findings": [
                        {
                            "system_id": "sys-auth",
                            "score": 50,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "audit"


# ── Admin endpoint ──────────────────────────────────────────────────────────


class TestSelfAuditAdminEndpoint:
    @pytest.mark.asyncio
    async def test_endpoint_creates_self_audit_task(
        self, client: AsyncClient, db_session
    ):
        project = await _seed_project_with_member(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/self-audit", json={}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == TaskStatus.QUEUED
        task_id = uuid.UUID(body["task_id"])

        task = await db_session.get(Task, task_id)
        assert task is not None
        assert task.payloads[0]["type"] == "self_audit"
        assert task.created_by == "USER"

    @pytest.mark.asyncio
    async def test_endpoint_forwards_focus_systems_and_max(
        self, client: AsyncClient, db_session
    ):
        project = await _seed_project_with_member(db_session)

        resp = await client.post(
            f"/api/admin/projects/{project.id}/self-audit",
            json={"focus_systems": ["sys-auth", "sys-billing"], "max_systems": 5},
        )
        assert resp.status_code == 200, resp.text

        task = await db_session.get(Task, uuid.UUID(resp.json()["task_id"]))
        payload = task.payloads[0]
        assert payload["focus_systems"] == ["sys-auth", "sys-billing"]
        assert payload["max_systems"] == 5

    @pytest.mark.asyncio
    async def test_endpoint_rejects_unauthorized_project(
        self, client: AsyncClient, db_session
    ):
        # Create a project owned by someone else (test user is NOT a member)
        other_project = make_project(name="Other", user_id="other-user-999")
        db_session.add(other_project)
        await db_session.commit()
        await db_session.refresh(other_project)

        resp = await client.post(
            f"/api/admin/projects/{other_project.id}/self-audit", json={}
        )
        assert resp.status_code == 404


# ── Dispatch decision — direction.recommended_mode ─────────────────────────


class TestShouldDispatchDirectionMode:
    """_should_dispatch_self_audit honours direction.recommended_mode.

    Each test seeds a project with a single direction that carries a specific
    recommended_mode, then asserts that the dispatch function produces the
    expected result (candidate vs. skip).
    """

    @pytest.mark.asyncio
    async def test_pause_direction_is_excluded_from_dispatch(self, db_session):
        """A direction with mode='pause' must not produce audit candidates."""
        project = await _seed_project_with_member(db_session)
        project.directions = [{"system_id": "sys-paused", "recommended_mode": "pause"}]
        flag_modified(project, "directions")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        result = await _should_dispatch_self_audit(project, db_session)
        # All directions are paused → direction_system_ids is empty → None
        assert result is None

    @pytest.mark.asyncio
    async def test_audit_direction_forces_fresh_system_into_candidates(
        self, db_session
    ):
        """mode='audit' must trigger dispatch even when SystemMaturity row is fresh."""
        project = await _seed_project_with_member(db_session)
        project.directions = [{"system_id": "sys-audit", "recommended_mode": "audit"}]
        flag_modified(project, "directions")
        # Fresh row that would normally pass the staleness check — should be overridden.
        fresh = SystemMaturity(
            project_id=project.id,
            system_id="sys-audit",
            score=85,
            recommended_mode="ship",
        )
        db_session.add(project)
        db_session.add(fresh)
        await db_session.commit()
        await db_session.refresh(project)

        result = await _should_dispatch_self_audit(project, db_session)
        assert result is not None
        assert "sys-audit" in (result.get("focus_systems") or [])

    @pytest.mark.asyncio
    async def test_fundamentals_direction_forces_fresh_system_into_candidates(
        self, db_session
    ):
        """mode='fundamentals' must trigger dispatch even when SystemMaturity row is fresh."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {"system_id": "sys-fund", "recommended_mode": "fundamentals"}
        ]
        flag_modified(project, "directions")
        fresh = SystemMaturity(
            project_id=project.id,
            system_id="sys-fund",
            score=70,
            recommended_mode="ship",
        )
        db_session.add(project)
        db_session.add(fresh)
        await db_session.commit()
        await db_session.refresh(project)

        result = await _should_dispatch_self_audit(project, db_session)
        assert result is not None
        assert "sys-fund" in (result.get("focus_systems") or [])

    @pytest.mark.asyncio
    async def test_ship_direction_with_fresh_row_is_not_a_candidate(self, db_session):
        """mode='ship' must not change existing behaviour: fresh row → no dispatch."""
        project = await _seed_project_with_member(db_session)
        project.directions = [{"system_id": "sys-ship", "recommended_mode": "ship"}]
        flag_modified(project, "directions")
        fresh = SystemMaturity(
            project_id=project.id,
            system_id="sys-ship",
            score=90,
            recommended_mode="ship",
        )
        db_session.add(project)
        db_session.add(fresh)
        await db_session.commit()
        await db_session.refresh(project)

        result = await _should_dispatch_self_audit(project, db_session)
        assert result is None

    @pytest.mark.asyncio
    async def test_unset_mode_with_fresh_row_is_not_a_candidate(self, db_session):
        """No recommended_mode on direction must not change existing behaviour."""
        project = await _seed_project_with_member(db_session)
        project.directions = [{"system_id": "sys-unset"}]
        flag_modified(project, "directions")
        fresh = SystemMaturity(
            project_id=project.id,
            system_id="sys-unset",
            score=88,
            recommended_mode="ship",
        )
        db_session.add(project)
        db_session.add(fresh)
        await db_session.commit()
        await db_session.refresh(project)

        result = await _should_dispatch_self_audit(project, db_session)
        assert result is None


# ── Auto-clear mode on recovery ────────────────────────────────────────────


class TestAutoModeRecover:
    """Auto-clear: audit/fundamentals mode resets when system health recovers.

    The threshold is 2 consecutive ship assessments on the linked system.
    Only directions whose mode was set by self_audit (source == 'self_audit')
    are eligible; operator-pinned modes (no meta or different source) are exempt.
    """

    @pytest.mark.asyncio
    async def test_audit_mode_clears_after_two_consecutive_ship_assessments(
        self, db_session, mock_broadcast
    ):
        """Direction auto-clears from audit → default after 2 ship assessments."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "audit",
                "recommended_mode_meta": {
                    "source": "self_audit",
                    "mode": "audit",
                    "set_at": "2026-04-01T00:00:00",
                },
                "notes": [],
            }
        ]
        db_session.add(project)

        ship_logs = json.dumps(
            {
                "findings": [
                    {"system_id": "sys-auth", "score": 90, "recommended_mode": "ship"}
                ]
            }
        )

        task1 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task1)
        await db_session.commit()
        await _handle_self_audit_completion(task1, ship_logs, db_session)

        task2 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task2)
        await db_session.commit()
        await _handle_self_audit_completion(task2, ship_logs, db_session)

        await db_session.refresh(project)
        d = project.directions[0]
        assert d.get("recommended_mode") in (None, "ship"), (
            f"Expected mode cleared, got {d.get('recommended_mode')!r}"
        )
        notes = d.get("notes") or []
        assert any(
            any(
                kw in (n.get("text") or "").lower()
                for kw in ("auto", "clear", "recover", "healthy")
            )
            for n in notes
        ), "Expected an auto-clear explanatory note on the direction"

    @pytest.mark.asyncio
    async def test_fundamentals_mode_clears_after_two_consecutive_ship_assessments(
        self, db_session, mock_broadcast
    ):
        """Direction auto-clears from fundamentals → default after 2 ship assessments."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-fund",
                "recommended_mode": "fundamentals",
                "recommended_mode_meta": {
                    "source": "self_audit",
                    "mode": "fundamentals",
                    "set_at": "2026-04-01T00:00:00",
                },
                "notes": [],
            }
        ]
        db_session.add(project)

        ship_logs = json.dumps(
            {
                "findings": [
                    {"system_id": "sys-fund", "score": 85, "recommended_mode": "ship"}
                ]
            }
        )

        task1 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task1)
        await db_session.commit()
        await _handle_self_audit_completion(task1, ship_logs, db_session)

        task2 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task2)
        await db_session.commit()
        await _handle_self_audit_completion(task2, ship_logs, db_session)

        await db_session.refresh(project)
        d = project.directions[0]
        assert d.get("recommended_mode") in (None, "ship"), (
            f"Expected fundamentals cleared, got {d.get('recommended_mode')!r}"
        )

    @pytest.mark.asyncio
    async def test_single_ship_assessment_does_not_auto_clear_mode(
        self, db_session, mock_broadcast
    ):
        """One ship assessment is below the 2-assessment threshold — mode stays."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "audit",
                "recommended_mode_meta": {
                    "source": "self_audit",
                    "mode": "audit",
                    "set_at": "2026-04-01T00:00:00",
                },
            }
        ]
        db_session.add(project)

        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()

        ship_logs = json.dumps(
            {
                "findings": [
                    {"system_id": "sys-auth", "score": 90, "recommended_mode": "ship"}
                ]
            }
        )
        await _handle_self_audit_completion(task, ship_logs, db_session)

        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "audit"

    @pytest.mark.asyncio
    async def test_operator_pinned_mode_not_cleared_by_auto_recover(
        self, db_session, mock_broadcast
    ):
        """Operator-set audit mode (no self_audit meta) survives consecutive ship assessments."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "audit",
                # No recommended_mode_meta → operator-pinned, not auto-set by self_audit
            }
        ]
        db_session.add(project)

        ship_logs = json.dumps(
            {
                "findings": [
                    {"system_id": "sys-auth", "score": 90, "recommended_mode": "ship"}
                ]
            }
        )

        task1 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task1)
        await db_session.commit()
        await _handle_self_audit_completion(task1, ship_logs, db_session)

        task2 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task2)
        await db_session.commit()
        await _handle_self_audit_completion(task2, ship_logs, db_session)

        await db_session.refresh(project)
        assert project.directions[0]["recommended_mode"] == "audit"


# ── Audit-deferral escalation ──────────────────────────────────────────────


class TestRecordResidualAuditDeferralEscalation:
    """_record_residual_audit_deferral fires the escalation message exactly
    when audit_defer_count first reaches the threshold (== not >=)."""

    @pytest.mark.asyncio
    async def test_escalation_fires_at_threshold(self, db_session):
        from unittest.mock import AsyncMock, patch
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from app.self_audit_dispatch import _record_residual_audit_deferral
        from app.notification_dispatch import _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD

        project = await _seed_project_with_member(db_session)
        fp = "fp-esc-threshold"
        case = ResidualRemediationCase(
            project_id=project.id,
            fingerprint=fp,
            status=ResidualCaseStatus.ACTIVE,
            audit_defer_count=_RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD - 1,
        )
        db_session.add(case)
        await db_session.commit()

        with patch(
            "app.notification_dispatch.post_residual_fix_audit_deferred_escalation_message",
            new_callable=AsyncMock,
        ) as mock_esc:
            await _record_residual_audit_deferral(db_session, project.id, fp, "sys-x")

        mock_esc.assert_awaited_once()
        call_kwargs = mock_esc.call_args.kwargs
        assert call_kwargs["defer_count"] == _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD
        assert call_kwargs["fingerprint"] == fp

    @pytest.mark.asyncio
    async def test_escalation_not_fired_below_threshold(self, db_session):
        from unittest.mock import AsyncMock, patch
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from app.self_audit_dispatch import _record_residual_audit_deferral

        project = await _seed_project_with_member(db_session)
        fp = "fp-esc-below"
        case = ResidualRemediationCase(
            project_id=project.id,
            fingerprint=fp,
            status=ResidualCaseStatus.ACTIVE,
            audit_defer_count=1,
        )
        db_session.add(case)
        await db_session.commit()

        with patch(
            "app.notification_dispatch.post_residual_fix_audit_deferred_escalation_message",
            new_callable=AsyncMock,
        ) as mock_esc:
            await _record_residual_audit_deferral(db_session, project.id, fp, "sys-y")

        mock_esc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_escalation_not_fired_above_threshold(self, db_session):
        """One-shot guard: count already at threshold → next increment skips alert."""
        from unittest.mock import AsyncMock, patch
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from app.self_audit_dispatch import _record_residual_audit_deferral
        from app.notification_dispatch import _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD

        project = await _seed_project_with_member(db_session)
        fp = "fp-esc-above"
        case = ResidualRemediationCase(
            project_id=project.id,
            fingerprint=fp,
            status=ResidualCaseStatus.ACTIVE,
            audit_defer_count=_RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD,
        )
        db_session.add(case)
        await db_session.commit()

        with patch(
            "app.notification_dispatch.post_residual_fix_audit_deferred_escalation_message",
            new_callable=AsyncMock,
        ) as mock_esc:
            await _record_residual_audit_deferral(db_session, project.id, fp, "sys-z")

        mock_esc.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_escalation_re_fires_after_streak_reset(self, db_session):
        """Streak reset (simulating a successful fix spawn) enables re-escalation
        on the next deferral cycle — the one-shot guard resets with the counter."""
        from unittest.mock import AsyncMock, patch
        from app.models import ResidualRemediationCase, ResidualCaseStatus
        from app.self_audit_dispatch import _record_residual_audit_deferral
        from app.notification_dispatch import _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD

        project = await _seed_project_with_member(db_session)
        fp = "fp-esc-refire"
        case = ResidualRemediationCase(
            project_id=project.id,
            fingerprint=fp,
            status=ResidualCaseStatus.ACTIVE,
            audit_defer_count=_RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD - 1,
        )
        db_session.add(case)
        await db_session.commit()

        with patch(
            "app.notification_dispatch.post_residual_fix_audit_deferred_escalation_message",
            new_callable=AsyncMock,
        ) as mock_esc:
            # First streak reaches threshold → escalation fires once.
            await _record_residual_audit_deferral(
                db_session, project.id, fp, "sys-refire-a"
            )
            assert mock_esc.await_count == 1

            # Simulate the streak reset that post_residual_fix_spawned_message
            # performs when a fix is successfully spawned.
            await db_session.refresh(case)
            case.audit_defer_count = _RESIDUAL_AUDIT_DEFER_ESCALATION_THRESHOLD - 1
            db_session.add(case)
            await db_session.commit()

            # Second streak reaches threshold → escalation fires again.
            await _record_residual_audit_deferral(
                db_session, project.id, fp, "sys-refire-b"
            )
            assert mock_esc.await_count == 2


# ── Drift-triggered dispatch context ───────────────────────────────────────


class TestDriftTriggeredDispatch:
    """Drift signals are embedded as drift_context in the dispatched task.

    When _should_dispatch_self_audit finds over-claimed or stalled systems,
    the returned dict must carry drift_context keyed by system_id.
    When _spawn_self_audit_task consumes that payload, the created task's
    context must carry the same drift_context so the agent can reconcile
    claims vs shipped work explicitly.
    """

    @pytest.mark.asyncio
    async def test_dispatch_result_includes_drift_context_for_over_claim_system(
        self, db_session
    ):
        """_should_dispatch_self_audit returns drift_context when system has over_claim_flag."""
        from unittest.mock import patch, AsyncMock

        project = await _seed_project_with_member(db_session)
        project.directions = [{"id": "d-drift", "system_id": "sys-drifted"}]
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "directions")
        db_session.add(project)
        await db_session.commit()
        await db_session.refresh(project)

        drift_sig = {
            "claimed_changes_count": 5,
            "actually_merged_prs": 0,
            "claim_shipped_consistency": 0.0,
            "over_claim_flag": True,
            "stalled_despite_churn_flag": True,
        }
        fake_drift = {
            "by_direction": {},
            "by_system": {"sys-drifted": drift_sig},
        }

        with patch(
            "app.drift_detection.compute_direction_drift",
            new=AsyncMock(return_value=fake_drift),
        ):
            result = await _should_dispatch_self_audit(project, db_session)

        assert result is not None
        assert "sys-drifted" in (result.get("focus_systems") or [])
        drift_ctx = result.get("drift_context") or {}
        assert "sys-drifted" in drift_ctx
        sig = drift_ctx["sys-drifted"]
        assert sig["over_claim_flag"] is True
        assert sig["claimed_changes_count"] == 5
        assert sig["actually_merged_prs"] == 0

    @pytest.mark.asyncio
    async def test_spawned_task_context_carries_drift_context(self, db_session):
        """_spawn_self_audit_task embeds drift_context from audit_payload into task.context."""
        from app.self_audit_dispatch import _spawn_self_audit_task
        from app.models import Task
        from sqlmodel import select

        project = await _seed_project_with_member(db_session)

        drift_ctx = {
            "sys-drifted": {
                "claimed_changes_count": 4,
                "actually_merged_prs": 0,
                "claim_shipped_consistency": 0.0,
                "over_claim_flag": True,
                "stalled_despite_churn_flag": False,
            }
        }
        audit_payload = {
            "focus_systems": ["sys-drifted"],
            "max_systems": 3,
            "drift_context": drift_ctx,
        }

        spawned = await _spawn_self_audit_task(project, db_session, audit_payload)
        assert spawned is True

        tasks = (
            (
                await db_session.execute(
                    select(Task).where(Task.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(tasks) == 1
        task_context = tasks[0].context or {}
        assert "drift_context" in task_context
        assert "sys-drifted" in task_context["drift_context"]
        assert task_context["drift_context"]["sys-drifted"]["over_claim_flag"] is True
