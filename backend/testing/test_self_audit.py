"""Tests for the operator-initiated self_audit completion path:

1. ``_handle_self_audit_completion`` ingest path — SELF_AUDIT_REPORT logs
   produce SystemMaturity rows, idempotent re-ingest, malformed entries are
   dropped with a warning, invalid recommended_mode is rejected.
2. ``_normalize_self_audit_finding`` — pure-function validator behavior.
3. ``POST /api/admin/projects/{project_id}/self-audit`` — enqueues a Task with
   the right payload type and forwards focus_systems / max_systems.

The autonomous self-audit *dispatch* feeder (``self_audit_dispatch``) was
removed with the self-repair apparatus; its tests are gone. The operator
trigger (admin endpoint) and the result-ingest / auto-mode-recover handler
(``app.task_completion``) remain.
"""

import json
import uuid

import pytest
from httpx import AsyncClient
from sqlmodel import select

from app.models import (
    ProjectMember,
    SystemMaturity,
    Task,
    TaskStatus,
)
from app.task_completion import (
    _handle_self_audit_completion,
    _normalize_self_audit_finding,
    _parse_self_audit_findings,
    SELF_AUDIT_PROPAGATION_RECORD_KEY,
)
from app.task_completion_self_audit import _PROPAGATION_RECORD_MAX_ENTRIES
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
    async def test_operator_pinned_mode_not_overwritten_by_propagation(
        self, db_session, mock_broadcast
    ):
        """Operator-pinned direction mode (no meta / unknown source) survives self-audit propagation."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "ship",
                # No recommended_mode_meta → operator-pinned
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
                            "score": 40,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["recommended_mode"] == "ship", (
            f"Operator-pinned mode was overwritten; got {d['recommended_mode']!r}"
        )

    @pytest.mark.asyncio
    async def test_operator_pinned_mode_not_overwritten_by_propagation_pause(
        self, db_session, mock_broadcast
    ):
        """Operator-pinned mode survives even when audit proposes the strictest mode (pause)."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "ship",
                # No recommended_mode_meta → operator-pinned
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
                            "score": 10,
                            "recommended_mode": "pause",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["recommended_mode"] == "ship", (
            f"Operator-pinned mode was overwritten by pause; got {d['recommended_mode']!r}"
        )

    @pytest.mark.asyncio
    async def test_skipped_propagation_note_is_recorded(
        self, db_session, mock_broadcast
    ):
        """When propagation is skipped for an operator-pinned direction, a note is appended."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "ship",
                "notes": [],
                # No recommended_mode_meta → operator-pinned
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
                            "score": 40,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        d = project.directions[0]
        notes = d.get("notes") or []
        skip_notes = [n for n in notes if n.get("source") == "self_audit_skip"]
        assert len(skip_notes) == 1, (
            f"Expected 1 self_audit_skip note, got {len(skip_notes)}: {notes}"
        )
        assert skip_notes[0].get("proposed_mode") == "fundamentals"

    @pytest.mark.asyncio
    async def test_self_audit_sourced_mode_can_still_be_overwritten(
        self, db_session, mock_broadcast
    ):
        """Direction with source=self_audit meta is NOT protected — self-audit can update it."""
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
                    "set_at": "2026-01-01T00:00:00+00:00",
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
                            "score": 55,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(project)
        d = project.directions[0]
        assert d["recommended_mode"] == "fundamentals", (
            f"self_audit-sourced mode should be overwritten; got {d['recommended_mode']!r}"
        )

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

    @pytest.mark.asyncio
    async def test_self_audit_records_mode_propagation_challenge_and_preserves_operator_pin(
        self, db_session, mock_broadcast
    ):
        """Propagation record captures applied and operator-pin-skipped entries."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d-applied",
                "status": "active",
                "system_id": "sys-auth",
                # No mode set — propagation should apply
            },
            {
                "id": "d-pinned",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "ship",
                # No recommended_mode_meta → operator-pinned
            },
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
                            "score": 45,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )
        await db_session.refresh(task)
        record = (task.context or {}).get(SELF_AUDIT_PROPAGATION_RECORD_KEY)
        assert record is not None, "Propagation record should be stored in task.context"
        assert isinstance(record, list)
        applied = [e for e in record if e.get("outcome") == "applied"]
        skipped = [e for e in record if e.get("outcome") == "skipped"]
        assert len(applied) == 1, f"Expected 1 applied entry, got {applied}"
        assert applied[0]["direction_id"] == "d-applied"
        assert applied[0]["proposed_mode"] == "fundamentals"
        assert applied[0]["system_id"] == "sys-auth"
        assert len(skipped) == 1, f"Expected 1 skipped entry, got {skipped}"
        assert skipped[0]["direction_id"] == "d-pinned"
        assert "operator" in skipped[0].get("reason", "").lower()

        # Confirm operator-pinned direction was NOT changed
        await db_session.refresh(project)
        pinned = next(d for d in project.directions if d["id"] == "d-pinned")
        assert pinned["recommended_mode"] == "ship"

    @pytest.mark.asyncio
    async def test_propagation_record_applied_stored_in_context(
        self, db_session, mock_broadcast
    ):
        """An applied mode propagation produces an outcome='applied' record in task.context."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {"id": "d1", "status": "active", "system_id": "sys-compute"}
        ]
        db_session.add(project)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await _handle_self_audit_completion(
            task,
            json.dumps(
                {"findings": [{"system_id": "sys-compute", "score": 40, "recommended_mode": "audit"}]}
            ),
            db_session,
        )
        await db_session.refresh(task)
        record = (task.context or {}).get(SELF_AUDIT_PROPAGATION_RECORD_KEY)
        assert record is not None
        assert len(record) == 1
        assert record[0]["outcome"] == "applied"
        assert record[0]["system_id"] == "sys-compute"
        assert record[0]["proposed_mode"] == "audit"

    @pytest.mark.asyncio
    async def test_propagation_record_auto_clear_captured(
        self, db_session, mock_broadcast
    ):
        """Auto-clear (2 consecutive ship assessments) produces outcome='cleared' entries."""
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
                    "set_at": "2026-01-01T00:00:00+00:00",
                },
                "notes": [],
            }
        ]
        db_session.add(project)
        ship_logs = json.dumps(
            {"findings": [{"system_id": "sys-auth", "score": 90, "recommended_mode": "ship"}]}
        )
        task1 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task1)
        await db_session.commit()
        await _handle_self_audit_completion(task1, ship_logs, db_session)

        task2 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task2)
        await db_session.commit()
        await _handle_self_audit_completion(task2, ship_logs, db_session)

        await db_session.refresh(task2)
        record = (task2.context or {}).get(SELF_AUDIT_PROPAGATION_RECORD_KEY)
        assert record is not None
        cleared = [e for e in record if e.get("outcome") == "cleared"]
        assert len(cleared) >= 1, f"Expected cleared entry after 2 ship assessments, got {record}"
        assert cleared[0]["system_id"] == "sys-auth"

    @pytest.mark.asyncio
    async def test_propagation_record_bounded(self, db_session, mock_broadcast):
        """Propagation record is capped at _PROPAGATION_RECORD_MAX_ENTRIES."""
        project = await _seed_project_with_member(db_session)
        # Create more directions than the cap to verify truncation
        many_directions = [
            {"id": f"d{i}", "status": "active", "system_id": "sys-load"}
            for i in range(_PROPAGATION_RECORD_MAX_ENTRIES + 10)
        ]
        project.directions = many_directions
        db_session.add(project)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()
        await _handle_self_audit_completion(
            task,
            json.dumps(
                {"findings": [{"system_id": "sys-load", "score": 40, "recommended_mode": "audit"}]}
            ),
            db_session,
        )
        await db_session.refresh(task)
        record = (task.context or {}).get(SELF_AUDIT_PROPAGATION_RECORD_KEY)
        assert record is not None
        assert len(record) <= _PROPAGATION_RECORD_MAX_ENTRIES

    @pytest.mark.asyncio
    async def test_skipped_propagation_note_deduped_on_same_mode(
        self, db_session, mock_broadcast
    ):
        """Identical skip notes for the same operator-pinned mode are deduplicated."""
        project = await _seed_project_with_member(db_session)
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "ship",
                "notes": [],
                # No recommended_mode_meta → operator-pinned
            }
        ]
        db_session.add(project)
        await db_session.commit()

        # First audit: skip note created
        task1 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task1)
        await db_session.commit()
        await _handle_self_audit_completion(
            task1,
            json.dumps(
                {
                    "findings": [
                        {
                            "system_id": "sys-auth",
                            "score": 40,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )

        await db_session.refresh(project)
        d = project.directions[0]
        notes = d.get("notes") or []
        skip_notes_1 = [n for n in notes if n.get("source") == "self_audit_skip"]
        assert len(skip_notes_1) == 1

        # Second audit with same findings: skip note should NOT be duplicated
        task2 = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task2)
        await db_session.commit()
        await _handle_self_audit_completion(
            task2,
            json.dumps(
                {
                    "findings": [
                        {
                            "system_id": "sys-auth",
                            "score": 40,
                            "recommended_mode": "fundamentals",
                        }
                    ]
                }
            ),
            db_session,
        )

        await db_session.refresh(project)
        d = project.directions[0]
        notes = d.get("notes") or []
        skip_notes_2 = [n for n in notes if n.get("source") == "self_audit_skip"]
        assert len(skip_notes_2) == 1, (
            f"Skip note should be deduplicated; expected 1, got {len(skip_notes_2)}: {notes}"
        )
        assert skip_notes_2[0].get("proposed_mode") == "fundamentals"

    @pytest.mark.asyncio
    async def test_direction_notes_capped_at_history_max(
        self, db_session, mock_broadcast
    ):
        """Direction notes are capped at STRATEGIC_NOTES_HISTORY_MAX when skip notes are added."""
        from app.strategic_constants import STRATEGIC_NOTES_HISTORY_MAX

        project = await _seed_project_with_member(db_session)
        # Pre-populate direction with notes at the cap
        initial_notes = [
            {"text": f"Note {i}", "source": "test"} for i in range(STRATEGIC_NOTES_HISTORY_MAX)
        ]
        project.directions = [
            {
                "id": "d1",
                "status": "active",
                "system_id": "sys-auth",
                "recommended_mode": "ship",
                "notes": initial_notes,
                # No recommended_mode_meta → operator-pinned
            }
        ]
        db_session.add(project)
        await db_session.commit()

        # Trigger an audit that tries to propagate but is skipped due to operator pin
        # This should add a skip note and cap the list
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
        d = project.directions[0]
        notes = d.get("notes") or []
        assert len(notes) <= STRATEGIC_NOTES_HISTORY_MAX, (
            f"Notes list should be capped to {STRATEGIC_NOTES_HISTORY_MAX}, "
            f"got {len(notes)}"
        )
        # Verify that a skip note was added
        skip_notes = [n for n in notes if n.get("source") == "self_audit_skip"]
        assert len(skip_notes) == 1, f"Expected 1 skip note, got {len(skip_notes)}"


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

    @pytest.mark.asyncio
    async def test_duplicate_system_id_in_batch_does_not_raise(
        self, db_session, mock_broadcast
    ):
        """Two findings with the same system_id in one batch upsert to a single row."""
        project = await _seed_project_with_member(db_session)
        task = make_task(project_id=project.id, status=TaskStatus.COMPLETED)
        db_session.add(task)
        await db_session.commit()

        # Two findings share the same system_id — the second should update,
        # not insert, avoiding UniqueConstraint(project_id, system_id) violation.
        logs = json.dumps(
            {
                "findings": [
                    {
                        "system_id": "sys-dup",
                        "score": 40,
                        "recommended_mode": "audit",
                        "top_issues": ["first"],
                        "evidence": [],
                    },
                    {
                        "system_id": "sys-dup",
                        "score": 80,
                        "recommended_mode": "ship",
                        "top_issues": ["second"],
                        "evidence": [],
                    },
                ]
            }
        )

        # Must not raise IntegrityError
        await _handle_self_audit_completion(task, logs, db_session)

        rows = (
            (
                await db_session.execute(
                    select(SystemMaturity).where(
                        SystemMaturity.project_id == project.id,
                        SystemMaturity.system_id == "sys-dup",
                    )
                )
            )
            .scalars()
            .all()
        )
        # Only one row for the system_id — the second finding wins
        assert len(rows) == 1
        assert rows[0].score == 80
        assert rows[0].recommended_mode == "ship"
