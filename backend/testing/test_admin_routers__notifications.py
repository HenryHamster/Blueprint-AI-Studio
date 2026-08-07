# AUTO-SPLIT from test_admin_routers.py — see refactor: decompose test_admin_routers.py
# monolith into focused domain slices. Behavior-preserving: conftest.py fixtures
# (client, db_session, session) are inherited automatically; shared helpers are
# duplicated here to keep this file self-contained.
"""Domain slice: admin_notifications.py — alerts, playbook, notification-config
settings, external dispatch, and startup restore.

Split from test_admin_routers.py; no behavior change."""

import uuid
from datetime import timedelta
from unittest.mock import (
    AsyncMock,
    patch,
)

import pytest
from httpx import AsyncClient

from app.models import (
    Project,
    ProjectMember,
    Alert,
    _utcnow,
)
from app.workbench_events import (
    SURFACE_MESSAGES,
    SURFACE_RUNTIME,
    SURFACE_STRATEGIC_SESSION,
)


# ── Constants ────────────────────────────────────────────────

TEST_USER_ID = "test-user-123"


@pytest.fixture(autouse=True)
def _default_no_migration_pr():
    """Default the pre-merge Alembic dual-head guard to a no-op for merge tests.

    The operator-approval merge paths (``_handle_rollup_pr_approval``,
    ``_merge_critical_pr``) and the operator-triggered rollup auto-merge now run
    ``enforce_premerge_migration_guard`` before merging, which calls
    ``github.get_pr_files``. Most merge tests only mock ``merge_pull_request``;
    returning a benign non-migration file list short-circuits the guard to
    ``ok`` so those merges proceed unchanged. A non-empty list is used so other
    ``get_pr_files`` consumers (e.g. empty-PR detection) keep their pre-change
    behavior. Tests exercising the guard itself re-patch ``app.github.get_pr_files``.
    """
    with patch(
        "app.github.get_pr_files",
        new_callable=AsyncMock,
        return_value=[{"filename": "README.md"}],
    ):
        yield


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


class TestAlerts:
    @pytest.mark.asyncio
    async def test_get_alerts_empty(self, client: AsyncClient, db_session):
        """No projects → empty alert list."""
        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_get_alerts_returns_own_project_alerts(
        self, client: AsyncClient, db_session
    ):
        """Alerts from the user's project appear in the list."""
        project = await _make_project(db_session)
        db_session.add(
            Alert(
                project_id=project.id,
                severity="ERROR",
                title="Test Alert",
                message="Something failed",
                event_type="failure_alert",
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["title"] == "Test Alert"

    @pytest.mark.asyncio
    async def test_get_alerts_excludes_other_projects(
        self, client: AsyncClient, db_session
    ):
        """Alerts from projects the user is not a member of are hidden."""
        other = Project(name="Other", user_id="other-77")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            Alert(
                project_id=other.id,
                severity="ERROR",
                title="Private Alert",
                message="Private",
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        titles = [a["title"] for a in resp.json()]
        assert "Private Alert" not in titles

    @pytest.mark.asyncio
    async def test_dismiss_alert(self, client: AsyncClient, db_session):
        """Alert is dismissed by ID."""
        project = await _make_project(db_session)
        alert = Alert(
            project_id=project.id,
            severity="WARNING",
            title="Dismiss Me",
            message="warn",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        with patch("app.workbench_events.broadcast_to_project", new=AsyncMock()) as bcast:
            resp = await client.post(f"/api/admin/alerts/{alert.id}/dismiss")
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"
        bcast.assert_awaited_once()
        assert bcast.await_args.args[:2] == (str(project.id), "workbench_refetch")
        event_payload = bcast.await_args.args[2]
        assert event_payload["resource"] == "alert"
        assert event_payload["action"] == "dismissed"
        assert event_payload["refetch"] == {
            "surfaces": [SURFACE_RUNTIME, SURFACE_STRATEGIC_SESSION, SURFACE_MESSAGES],
        }
        assert event_payload["alert_id"] == str(alert.id)
        assert event_payload["alert"]["alert_id"] == str(alert.id)

    @pytest.mark.asyncio
    async def test_dismiss_alert_not_found_returns_404(self, client: AsyncClient):
        """Dismissing a missing alert returns 404."""
        resp = await client.post(f"/api/admin/alerts/{uuid.uuid4()}/dismiss")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_dismiss_alert_viewer_forbidden(
        self, client: AsyncClient, db_session
    ):
        """A viewer may not suppress operator escalation by dismissing an alert."""
        project = await _make_project(db_session, name="Viewer Alerts", role="viewer")
        alert = Alert(
            project_id=project.id,
            severity="ERROR",
            title="Do not silence me",
            message="boom",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        resp = await client.post(f"/api/admin/alerts/{alert.id}/dismiss")
        assert resp.status_code == 403, resp.text

        await db_session.refresh(alert)
        assert alert.dismissed_at is None

    @pytest.mark.asyncio
    async def test_acknowledge_alert_viewer_forbidden(
        self, client: AsyncClient, db_session
    ):
        """A viewer may not acknowledge an alert (read-only role)."""
        project = await _make_project(db_session, name="Viewer Ack", role="viewer")
        alert = Alert(
            project_id=project.id,
            severity="ERROR",
            title="Unacknowledged",
            message="boom",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        resp = await client.post(f"/api/admin/alerts/{alert.id}/acknowledge")
        assert resp.status_code == 403, resp.text

        await db_session.refresh(alert)
        assert alert.acknowledged_at is None

    @pytest.mark.asyncio
    async def test_acknowledge_alert_member_allowed(
        self, client: AsyncClient, db_session
    ):
        """The member floor must not over-reject: a member (not owner) can ack."""
        project = await _make_project(db_session, name="Member Ack", role="member")
        alert = Alert(
            project_id=project.id,
            severity="ERROR",
            title="Acknowledge me",
            message="boom",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        resp = await client.post(f"/api/admin/alerts/{alert.id}/acknowledge")
        assert resp.status_code == 200, resp.text

        await db_session.refresh(alert)
        assert alert.acknowledged_at is not None

    @pytest.mark.asyncio
    async def test_dismiss_all_skips_viewer_projects(
        self, client: AsyncClient, db_session
    ):
        """dismiss-all must leave alerts on viewer-only projects untouched.

        The UNFILTERED bulk shape ("dismiss everything I can") scopes silently
        rather than 403-ing the whole call — a caller who is member on 3 projects
        and viewer on a 4th should dismiss the 3. But it must report what it left
        behind via ``skipped_count`` instead of hiding it.
        """
        project = await _make_project(db_session, name="Viewer Bulk", role="viewer")
        alert = Alert(
            project_id=project.id,
            severity="ERROR",
            title="Still open",
            message="boom",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        resp = await client.post("/api/admin/alerts/dismiss-all")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["dismissed_count"] == 0
        # The no-op is surfaced, not silent.
        assert body["skipped_count"] == 1

        await db_session.refresh(alert)
        assert alert.dismissed_at is None

    @pytest.mark.asyncio
    async def test_dismiss_all_explicit_viewer_project_id_forbidden(
        self, client: AsyncClient, db_session
    ):
        """An EXPLICITLY-targeted dismiss-all on a viewer project → 403, not a silent no-op.

        Silent scoping is right for the unfiltered bulk case, but when the caller
        names a specific project they cannot write to, returning 200 /
        ``dismissed_count: 0`` tells them nothing about why nothing happened.
        Gates must emit WHY.
        """
        project = await _make_project(
            db_session, name="Viewer Targeted", role="viewer"
        )
        alert = Alert(
            project_id=project.id,
            severity="ERROR",
            title="Still open",
            message="boom",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        resp = await client.post(
            f"/api/admin/alerts/dismiss-all?project_id={project.id}"
        )
        assert resp.status_code == 403, resp.text
        assert "member" in resp.json()["error"]["message"].lower()

        await db_session.refresh(alert)
        assert alert.dismissed_at is None

    @pytest.mark.asyncio
    async def test_dismiss_all_explicit_member_project_id_allowed(
        self, client: AsyncClient, db_session
    ):
        """The explicit-target 403 must not over-reject a member."""
        project = await _make_project(
            db_session, name="Member Targeted", role="member"
        )
        alert = Alert(
            project_id=project.id,
            severity="ERROR",
            title="Dismiss me",
            message="boom",
        )
        db_session.add(alert)
        await db_session.commit()
        await db_session.refresh(alert)

        resp = await client.post(
            f"/api/admin/alerts/dismiss-all?project_id={project.id}"
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["dismissed_count"] == 1
        assert resp.json()["skipped_count"] == 0

        await db_session.refresh(alert)
        assert alert.dismissed_at is not None

    @pytest.mark.asyncio
    async def test_dismiss_all_alerts(self, client: AsyncClient, db_session):
        """All undismissed alerts across user's projects are dismissed."""
        project = await _make_project(db_session)
        for i in range(3):
            db_session.add(
                Alert(
                    project_id=project.id,
                    severity="INFO",
                    title=f"Alert {i}",
                    message="msg",
                )
            )
        await db_session.commit()

        resp = await client.post("/api/admin/alerts/dismiss-all")
        assert resp.status_code == 200
        assert resp.json()["dismissed_count"] == 3

    @pytest.mark.asyncio
    async def test_get_alerts_includes_dismissed_when_requested(
        self, client: AsyncClient, db_session
    ):
        """include_dismissed=true returns already-dismissed alerts."""
        from app.models import _utcnow

        project = await _make_project(db_session)
        db_session.add(
            Alert(
                project_id=project.id,
                severity="INFO",
                title="Old",
                message="old",
                event_type="failure_alert",
                dismissed_at=_utcnow(),
            )
        )
        await db_session.commit()

        # By default dismissed alerts are excluded
        resp = await client.get("/api/admin/alerts")
        assert resp.status_code == 200
        assert len(resp.json()) == 0

        # With the flag they appear
        resp = await client.get("/api/admin/alerts?include_dismissed=true")
        assert resp.status_code == 200
        assert len(resp.json()) == 1


class TestNotificationDispatchStats:
    @pytest.mark.asyncio
    async def test_stats_returns_zeros_for_new_project(
        self, client: AsyncClient, db_session
    ):
        """Newly created project returns zeroed delivery stats."""
        project = await _make_project(db_session)
        resp = await client.get(
            f"/api/admin/notification-dispatch/stats?project_id={project.id}"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_dispatched"] == 0
        assert body["total_delivered"] == 0
        assert body["total_failed"] == 0
        assert body["retry_queue_depth"] == 0

    @pytest.mark.asyncio
    async def test_stats_requires_project_access(self, client: AsyncClient, db_session):
        """Missing project membership → 404."""
        other_project = Project(name="Other", user_id="other-user")
        db_session.add(other_project)
        await db_session.commit()
        await db_session.refresh(other_project)
        resp = await client.get(
            f"/api/admin/notification-dispatch/stats?project_id={other_project.id}"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_stats_invalid_project_id(self, client: AsyncClient, db_session):
        """Invalid UUID → 400."""
        resp = await client.get(
            "/api/admin/notification-dispatch/stats?project_id=not-a-uuid"
        )
        assert resp.status_code == 400


class TestPlaybook:
    @pytest.mark.asyncio
    async def test_playbook_list_empty(self, client: AsyncClient, db_session):
        """New project has an empty playbook."""
        project = await _make_project(db_session)

        resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert resp.status_code == 200
        data = resp.json()
        assert data["entries"] == {}
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_playbook_set_creates_entry(self, client: AsyncClient, db_session):
        """PUT creates a new playbook entry."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "my-tip", "value": "Always run tests before merging."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["key"] == "my-tip"

    @pytest.mark.asyncio
    async def test_playbook_set_then_list(self, client: AsyncClient, db_session):
        """Entry appears in the listing after creation."""
        project = await _make_project(db_session)
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "tip1", "value": "First tip"},
        )

        resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert resp.status_code == 200
        entries = resp.json()["entries"]
        assert "tip1" in entries
        assert entries["tip1"]["content"] == "First tip"

    @pytest.mark.asyncio
    async def test_playbook_set_invalid_key_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Key with spaces/special chars returns 400."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "invalid key with spaces!", "value": "Some value"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_playbook_set_value_too_large_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Value exceeding 2048 bytes returns 400."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "big-entry", "value": "x" * 2049},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_playbook_set_with_applicable_types(
        self, client: AsyncClient, db_session
    ):
        """Entry can specify which task types it applies to, and they persist."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={
                "key": "typed-tip",
                "value": "Only for create tasks",
                "applicable_types": ["create", "fix"],
            },
        )
        assert resp.status_code == 200

        # Read the entry back: applicable_types must round-trip through storage.
        read = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert read.status_code == 200
        entries = read.json()["entries"]
        assert "typed-tip" in entries
        assert entries["typed-tip"]["applicable_types"] == ["create", "fix"]

    @pytest.mark.asyncio
    async def test_playbook_set_invalid_applicable_type_returns_400(
        self, client: AsyncClient, db_session
    ):
        """Unknown task type in applicable_types returns 400."""
        project = await _make_project(db_session)

        resp = await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={
                "key": "bad-types",
                "value": "Test",
                "applicable_types": ["invalid-task-type"],
            },
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_playbook_update_existing_entry(
        self, client: AsyncClient, db_session
    ):
        """PUT on an existing key updates the content."""
        project = await _make_project(db_session)
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "updatable", "value": "Original"},
        )
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "updatable", "value": "Updated"},
        )

        resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert resp.json()["entries"]["updatable"]["content"] == "Updated"

    @pytest.mark.asyncio
    async def test_playbook_delete_entry(self, client: AsyncClient, db_session):
        """Can delete an existing playbook entry."""
        project = await _make_project(db_session)
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "to-delete", "value": "Temporary"},
        )

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/playbook/to-delete"
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # Verify gone from list
        list_resp = await client.get(f"/api/admin/projects/{project.id}/playbook")
        assert "to-delete" not in list_resp.json()["entries"]

    @pytest.mark.asyncio
    async def test_playbook_delete_not_found_returns_404(
        self, client: AsyncClient, db_session
    ):
        """Deleting a non-existent key returns 404."""
        project = await _make_project(db_session)

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/playbook/no-such-key"
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_admin_playbook_delete_writes_tombstone(
        self, client: AsyncClient, db_session
    ):
        """Deleting a playbook entry records a tombstone in playbook_archive and playbook_deleted_keys."""
        project = await _make_project(db_session)
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "tombstone-test", "value": "To be deleted"},
        )

        resp = await client.delete(
            f"/api/admin/projects/{project.id}/playbook/tombstone-test"
        )
        assert resp.status_code == 200
        assert resp.json()["deleted"] is True

        # The archive (with the tombstone) is persisted to the project_telemetry
        # KV store — "playbook_archive" is a migrated key that
        # strip_migrated_doc_keys() removes from design_doc before the row is
        # written, so design_doc no longer retains it.
        from app.project_telemetry import get_project_telemetry

        archive = await get_project_telemetry(
            db_session, project.id, "playbook_archive"
        )
        tombstones = [
            e
            for e in (archive or [])
            if e.get("_archived_key") == "playbook/tombstone-test"
        ]
        assert len(tombstones) == 1
        assert tombstones[0]["_retirement_reason"] == "operator_deleted"
        assert "_archived_at" in tombstones[0]

        # The deleted-key tombstone list is NOT a migrated key, so it remains in
        # design_doc.
        await db_session.refresh(project)
        deleted_keys = (project.design_doc or {}).get("playbook_deleted_keys", [])
        assert "playbook/tombstone-test" in deleted_keys

    @pytest.mark.asyncio
    async def test_playbook_seed_creates_default_entries(
        self, client: AsyncClient, db_session
    ):
        """Seed populates the playbook with operational defaults."""
        project = await _make_project(db_session)

        resp = await client.post(f"/api/admin/projects/{project.id}/playbook/seed")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert len(data["added"]) > 0
        assert "build-backend" in data["added"]
        assert len(data["skipped"]) == 0

    @pytest.mark.asyncio
    async def test_playbook_seed_skips_existing_entries(
        self, client: AsyncClient, db_session
    ):
        """Seed does not overwrite entries that already exist."""
        project = await _make_project(db_session)
        # Pre-create one of the seed keys
        await client.put(
            f"/api/admin/projects/{project.id}/playbook",
            json={"key": "build-backend", "value": "Custom version"},
        )

        resp = await client.post(f"/api/admin/projects/{project.id}/playbook/seed")
        assert resp.status_code == 200
        data = resp.json()
        assert "build-backend" in data["skipped"]
        assert "build-backend" not in data["added"]

    @pytest.mark.asyncio
    async def test_playbook_list_requires_member_role(
        self, client: AsyncClient, db_session
    ):
        """Viewer-role can list the playbook (member threshold for list is 'member'
        — actually admin_playbook_list uses 'member', so viewer is rejected)."""
        other = Project(name="Viewer Only", user_id="other-owner-pb")
        db_session.add(other)
        await db_session.commit()
        await db_session.refresh(other)
        db_session.add(
            ProjectMember(project_id=other.id, user_id=TEST_USER_ID, role="viewer")
        )
        await db_session.commit()

        resp = await client.get(f"/api/admin/projects/{other.id}/playbook")
        assert resp.status_code == 403


class TestCapabilityProofGateCoverageSurface:
    """GET /api/admin/settings surfaces the live fleet-wide capability-proof
    coverage ratio the auto-graduation observe/enforce thresholds are compared
    against, so the operator no longer sets those thresholds blind."""

    @pytest.mark.asyncio
    async def test_capability_proof_coverage_endpoint_matches_sweep(
        self, client: AsyncClient, db_session
    ):
        """The settings payload's ``capability_proof_gate_coverage`` is exactly
        what the auto-graduation sweep computes from the same ground truth — the
        same helper backs both, so the operator sees the number the gate acts on.
        """
        from app.metrics_integrity_sweep import compute_fleet_proof_coverage

        proven_ref = {
            "kind": "named_test",
            "ref": "testing/test_x.py::test_x",
            "proven": True,
        }
        # One project, one proven + one unproven non-exempt working cap → 1/2.
        # Cap A's evidence_refs claim must be corroborated by a platform ledger
        # entry — compute_fleet_proof_coverage counts only ledger-cleared proofs,
        # so a self-reported evidence_refs alone grades "unverified", not "proven".
        db_session.add(
            Project(
                name="cov-proj",
                user_id="u-cov",
                problem_model={
                    "capabilities": [
                        {
                            "name": "Cap A",
                            "status": "working",
                            "evidence_refs": [proven_ref],
                        },
                        {
                            "name": "Cap B",
                            "status": "working",
                            "evidence_refs": [],
                        },
                    ]
                },
                capability_proof_records={
                    "cap-a": [
                        {
                            "proof_kind": "named_test",
                            "ref": "testing/test_x.py::test_x",
                            "proved_at": (_utcnow() - timedelta(days=1)).isoformat(),
                        }
                    ]
                },
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        payload = resp.json()["capability_proof_gate_coverage"]

        # Endpoint value equals the sweep's own computation (the shared seam),
        # minus the internal contributing_project_ids list (UUIDs) which the
        # settings surface intentionally does not expose.
        full = await compute_fleet_proof_coverage(db_session, 25)
        expected = {k: v for k, v in full.items() if k != "contributing_project_ids"}
        assert payload == expected
        assert "contributing_project_ids" not in payload

        # And it reflects the fleet we built: 1 proven of 2 non-exempt → 0.5.
        assert payload["total_proven"] == 1
        assert payload["total_non_exempt_working"] == 2
        assert payload["coverage_ratio"] == 0.5
        assert payload["projects_contributing"] == 1

    @pytest.mark.asyncio
    async def test_capability_proof_coverage_null_when_no_non_exempt(
        self, client: AsyncClient, db_session
    ):
        """With no non-exempt working capabilities the ratio is undefined and the
        endpoint reports ``coverage_ratio: null`` (not a divide-by-zero / 0.0)."""
        db_session.add(
            Project(
                name="planned-only",
                user_id="u-planned",
                problem_model={
                    "capabilities": [
                        {"name": "Planned", "status": "planned", "evidence_refs": []},
                    ]
                },
            )
        )
        await db_session.commit()

        resp = await client.get("/api/admin/settings")
        assert resp.status_code == 200
        payload = resp.json()["capability_proof_gate_coverage"]
        assert payload["coverage_ratio"] is None
        assert payload["total_non_exempt_working"] == 0


class TestDirectionGapRespawnSettings:
    """direction_gap_respawn_block_mode and direction_gap_respawn_cooldown_days
    are registered in the settings allowlist, exposed by GET /settings with
    correct defaults, validated on PATCH, and persisted + restored via
    sync_runtime_config_from_state."""

    @pytest.mark.asyncio
    async def test_gap_respawn_settings_appear_in_get_with_defaults(
        self, client: AsyncClient
    ):
        """GET /settings includes both keys with the expected defaults."""
        body = (await client.get("/api/admin/settings")).json()
        assert "direction_gap_respawn_block_mode" in body
        assert "direction_gap_respawn_cooldown_days" in body
        assert body["direction_gap_respawn_block_mode"] == "observe"
        assert body["direction_gap_respawn_cooldown_days"] == 14

    @pytest.mark.asyncio
    async def test_gap_respawn_block_mode_roundtrip(self, client: AsyncClient):
        """Valid block-mode values are accepted and reflected in GET."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"direction_gap_respawn_block_mode": "enforce"},
        )
        assert resp.status_code == 200, resp.text
        body = (await client.get("/api/admin/settings")).json()
        assert body["direction_gap_respawn_block_mode"] == "enforce"

        # Restore to observe
        resp2 = await client.patch(
            "/api/admin/settings",
            json={"direction_gap_respawn_block_mode": "observe"},
        )
        assert resp2.status_code == 200
        body2 = (await client.get("/api/admin/settings")).json()
        assert body2["direction_gap_respawn_block_mode"] == "observe"

    @pytest.mark.asyncio
    async def test_gap_respawn_block_mode_invalid_rejected(self, client: AsyncClient):
        """Invalid block-mode strings are rejected with 400."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"direction_gap_respawn_block_mode": "advisory"},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_gap_respawn_cooldown_days_roundtrip(self, client: AsyncClient):
        """Valid cooldown values (float, in 1..365) are accepted."""
        resp = await client.patch(
            "/api/admin/settings",
            json={"direction_gap_respawn_cooldown_days": 30},
        )
        assert resp.status_code == 200, resp.text
        body = (await client.get("/api/admin/settings")).json()
        assert body["direction_gap_respawn_cooldown_days"] == 30.0

    @pytest.mark.asyncio
    async def test_gap_respawn_cooldown_days_out_of_range_rejected(
        self, client: AsyncClient
    ):
        """Out-of-range cooldown values are rejected with 400."""
        resp_low = await client.patch(
            "/api/admin/settings",
            json={"direction_gap_respawn_cooldown_days": 0},
        )
        assert resp_low.status_code == 400

        resp_high = await client.patch(
            "/api/admin/settings",
            json={"direction_gap_respawn_cooldown_days": 400},
        )
        assert resp_high.status_code == 400

    @pytest.mark.asyncio
    async def test_gap_respawn_settings_persist_and_restore(
        self, client: AsyncClient, db_session
    ):
        """Both keys persist to runtime_config_json and are restored by
        sync_runtime_config_from_state so the values survive a restart."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        db_session.add(DeploymentState())
        await db_session.commit()

        resp = await client.patch(
            "/api/admin/settings",
            json={
                "direction_gap_respawn_block_mode": "enforce",
                "direction_gap_respawn_cooldown_days": 21,
            },
        )
        assert resp.status_code == 200, resp.text

        # Values appear in GET /settings
        body = (await client.get("/api/admin/settings")).json()
        assert body["direction_gap_respawn_block_mode"] == "enforce"
        assert body["direction_gap_respawn_cooldown_days"] == 21.0

        # Values are persisted to runtime_config_json
        row = await db_session.get(DeploymentState, 1)
        assert row is not None
        assert row.runtime_config_json is not None
        assert row.runtime_config_json["direction_gap_respawn_block_mode"] == "enforce"
        assert row.runtime_config_json["direction_gap_respawn_cooldown_days"] == 21.0

        # Simulate a restart by resetting in-memory values to defaults
        _shared_state.runtime_config["direction_gap_respawn_block_mode"] = "observe"
        _shared_state.runtime_config["direction_gap_respawn_cooldown_days"] = 14

        await sync_runtime_config_from_state(row)

        # Persisted values are restored
        assert _shared_state.runtime_config["direction_gap_respawn_block_mode"] == "enforce"
        assert _shared_state.runtime_config["direction_gap_respawn_cooldown_days"] == 21.0

    @pytest.mark.asyncio
    async def test_gap_respawn_block_mode_junk_ignored_in_sync(
        self, client: AsyncClient, db_session
    ):
        """Junk mode value in runtime_config_json is ignored by
        sync_runtime_config_from_state — the existing value survives."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        state = DeploymentState()
        state.runtime_config_json = {"direction_gap_respawn_block_mode": "invalid_junk"}
        db_session.add(state)
        await db_session.commit()

        _shared_state.runtime_config["direction_gap_respawn_block_mode"] = "observe"
        await sync_runtime_config_from_state(state)
        # Junk is ignored; original default survives
        assert _shared_state.runtime_config["direction_gap_respawn_block_mode"] == "observe"


class TestDirectionGuardrailSettings:
    """direction_churn_*, direction_lifetime_*, and direction_review_recommendation_threshold
    are registered in the settings allowlist, exposed by GET /settings with
    correct defaults, validated on PATCH, and persisted + restored via
    sync_runtime_config_from_state."""

    @pytest.mark.asyncio
    async def test_direction_guardrail_settings_appear_in_get_with_defaults(
        self, client: AsyncClient
    ):
        """GET /settings includes all 6 guardrail keys with expected defaults."""
        body = (await client.get("/api/admin/settings")).json()
        assert body.get("direction_churn_window_cycles") == 5
        assert body.get("direction_churn_non_merge_threshold") == 15
        assert abs(body.get("direction_churn_min_merge_rate", -1) - 0.10) < 1e-9
        assert body.get("direction_lifetime_non_merge_threshold") == 150
        assert abs(body.get("direction_lifetime_min_merge_rate", -1) - 0.10) < 1e-9
        assert body.get("direction_review_recommendation_threshold") == 2

    @pytest.mark.asyncio
    async def test_direction_guardrail_settings_round_trip_and_persist(
        self, client: AsyncClient, db_session
    ):
        """Valid guardrail values round-trip through PATCH→GET and persist to
        runtime_config_json so they survive a restart (sync_runtime_config_from_state)."""
        from app.models import DeploymentState
        from app.runtime_config_state import sync_runtime_config_from_state
        import app.shared_state as _shared_state

        db_session.add(DeploymentState())
        await db_session.commit()

        resp = await client.patch(
            "/api/admin/settings",
            json={
                "direction_churn_window_cycles": 8,
                "direction_churn_non_merge_threshold": 20,
                "direction_churn_min_merge_rate": 0.15,
                "direction_lifetime_non_merge_threshold": 200,
                "direction_lifetime_min_merge_rate": 0.20,
                "direction_review_recommendation_threshold": 3,
            },
        )
        assert resp.status_code == 200, resp.text

        body = (await client.get("/api/admin/settings")).json()
        assert body["direction_churn_window_cycles"] == 8
        assert body["direction_churn_non_merge_threshold"] == 20
        assert abs(body["direction_churn_min_merge_rate"] - 0.15) < 1e-9
        assert body["direction_lifetime_non_merge_threshold"] == 200
        assert abs(body["direction_lifetime_min_merge_rate"] - 0.20) < 1e-9
        assert body["direction_review_recommendation_threshold"] == 3

        # Values are persisted to runtime_config_json
        row = await db_session.get(DeploymentState, 1)
        assert row is not None and row.runtime_config_json is not None
        rcj = row.runtime_config_json
        assert rcj["direction_churn_window_cycles"] == 8
        assert rcj["direction_churn_non_merge_threshold"] == 20
        assert abs(rcj["direction_churn_min_merge_rate"] - 0.15) < 1e-9
        assert rcj["direction_lifetime_non_merge_threshold"] == 200
        assert abs(rcj["direction_lifetime_min_merge_rate"] - 0.20) < 1e-9
        assert rcj["direction_review_recommendation_threshold"] == 3

        # Simulate restart: reset in-memory values to defaults
        _shared_state.runtime_config["direction_churn_window_cycles"] = 5
        _shared_state.runtime_config["direction_churn_non_merge_threshold"] = 15
        _shared_state.runtime_config["direction_churn_min_merge_rate"] = 0.10
        _shared_state.runtime_config["direction_lifetime_non_merge_threshold"] = 150
        _shared_state.runtime_config["direction_lifetime_min_merge_rate"] = 0.10
        _shared_state.runtime_config["direction_review_recommendation_threshold"] = 2

        await sync_runtime_config_from_state(row)

        # Persisted values are restored after sync
        assert _shared_state.runtime_config["direction_churn_window_cycles"] == 8
        assert _shared_state.runtime_config["direction_churn_non_merge_threshold"] == 20
        assert abs(_shared_state.runtime_config["direction_churn_min_merge_rate"] - 0.15) < 1e-9
        assert _shared_state.runtime_config["direction_lifetime_non_merge_threshold"] == 200
        assert abs(_shared_state.runtime_config["direction_lifetime_min_merge_rate"] - 0.20) < 1e-9
        assert _shared_state.runtime_config["direction_review_recommendation_threshold"] == 3

    @pytest.mark.asyncio
    async def test_direction_guardrail_out_of_range_rejected(self, client: AsyncClient):
        """Out-of-range values are rejected with HTTP 400."""
        cases = [
            {"direction_churn_window_cycles": 0},
            {"direction_churn_window_cycles": 101},
            {"direction_churn_non_merge_threshold": 0},
            {"direction_churn_non_merge_threshold": 10001},
            {"direction_churn_min_merge_rate": -0.1},
            {"direction_churn_min_merge_rate": 1.1},
            {"direction_lifetime_non_merge_threshold": 0},
            {"direction_lifetime_non_merge_threshold": 100001},
            {"direction_lifetime_min_merge_rate": -0.1},
            {"direction_lifetime_min_merge_rate": 1.1},
            {"direction_review_recommendation_threshold": 0},
            {"direction_review_recommendation_threshold": 101},
        ]
        for payload in cases:
            resp = await client.patch("/api/admin/settings", json=payload)
            assert resp.status_code == 400, f"Expected 400 for {payload}, got {resp.status_code}"

    @pytest.mark.asyncio
    async def test_direction_guardrail_wrong_type_rejected(self, client: AsyncClient):
        """Non-numeric values are rejected with HTTP 400."""
        cases = [
            {"direction_churn_window_cycles": "five"},
            {"direction_churn_min_merge_rate": "high"},
            {"direction_review_recommendation_threshold": None},
        ]
        for payload in cases:
            resp = await client.patch("/api/admin/settings", json=payload)
            assert resp.status_code == 400, f"Expected 400 for {payload}, got {resp.status_code}"


class TestDispatchExternalNotification:
    """Tests for dispatch_external_notification in notification_dispatch.py."""

    @pytest.fixture(autouse=True)
    def _mock_safe_dns_dispatch(self):
        """Stub the SSRF resolver so dispatch tests can use hooks.example.com
        without hitting real DNS, and so the mocked httpx client receives the
        original webhook URL (not an IP-pinned variant)."""
        with patch(
            "app.notification_dispatch._resolve_safe_external_url",
            return_value=(True, "", "93.184.216.34"),
        ):
            yield

    @pytest.mark.asyncio
    async def test_dispatches_when_event_enabled(self, mocker):
        """POSTs to webhook_url when event_type is in enabled_events."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        # Clear throttle state
        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/test",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 0,
        }

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient", return_value=mock_client
        )

        project_id = uuid.uuid4()
        await dispatch_external_notification(
            project_id, "TASK_FAILED", {"task_id": "abc", "task_title": "Test Task"}
        )

        mock_client.post.assert_awaited_once()
        call_args = mock_client.post.call_args
        body = (
            call_args.kwargs.get("json") or call_args.args[1]
            if len(call_args.args) > 1
            else call_args.kwargs["json"]
        )
        assert body["event_type"] == "TASK_FAILED"
        assert body["project_id"] == str(project_id)
        assert "timestamp" in body
        assert body["payload"]["task_id"] == "abc"

    @pytest.mark.asyncio
    async def test_skips_when_event_not_enabled(self, mocker):
        """Does NOT POST when event_type is not in enabled_events."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/test",
            "enabled_events": ["APPROVAL_NEEDED"],
            "throttle_seconds": 0,
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient", return_value=mock_client
        )

        await dispatch_external_notification(uuid.uuid4(), "TASK_FAILED", {})

        mock_client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_skips_when_no_webhook_url(self, mocker):
        """Does NOT POST when webhook_url is empty."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 0,
        }

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock()
        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient", return_value=mock_client
        )

        await dispatch_external_notification(uuid.uuid4(), "TASK_FAILED", {})

        mock_client.post.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_respects_throttle_seconds(self, mocker):
        """Second call within throttle window is silently dropped."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/test",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 60,
        }

        mock_response = mocker.MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)
        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient", return_value=mock_client
        )

        project_id = uuid.uuid4()
        await dispatch_external_notification(project_id, "TASK_FAILED", {"n": 1})
        # Second call within throttle window
        await dispatch_external_notification(project_id, "TASK_FAILED", {"n": 2})

        assert mock_client.post.await_count == 1, "throttle should drop the second call"

    @pytest.mark.asyncio
    async def test_never_raises_on_http_error(self, mocker):
        """dispatch_external_notification swallows all exceptions without propagating."""
        from app import shared_state
        from app.notification_dispatch import (
            dispatch_external_notification,
            _last_dispatch_time,
        )

        _last_dispatch_time.clear()

        shared_state.runtime_config["notification_config"] = {
            "webhook_url": "https://hooks.example.com/test",
            "enabled_events": ["TASK_FAILED"],
            "throttle_seconds": 0,
        }

        import httpx as _httpx

        mocker.patch(
            "app.notification_dispatch.httpx.AsyncClient",
            side_effect=_httpx.RequestError("network down"),
        )

        # Must not raise
        await dispatch_external_notification(uuid.uuid4(), "TASK_FAILED", {})


class TestNotificationConfigStartupRestore:
    """Verify that notification_config in runtime_config_json is restored on startup."""

    @pytest.mark.asyncio
    async def test_notification_config_restored_from_runtime_config_json(self, mocker):
        """notification_config written to DeploymentState.runtime_config_json
        must be loaded back into shared_state.runtime_config on _init_deployment_state.
        """
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from app import shared_state
        from app.models import DeploymentState
        from app.main import _init_deployment_state

        # Create isolated in-memory SQLite DB (same pattern as test_services.py)
        _engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        _Session = sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        mocker.patch("app.main.AsyncSessionLocal", _Session)

        notification_cfg = {
            "webhook_url": "https://hooks.example.com/token123",
            "enabled_events": ["TASK_FAILED", "APPROVAL_NEEDED"],
            "throttle_seconds": 120,
        }

        # Seed DeploymentState with notification_config in runtime_config_json
        async with _Session() as session:
            state = DeploymentState(
                id=1,
                current_revision="r1",
                current_sha="abc123",
                runtime_config_json={"notification_config": notification_cfg},
            )
            session.add(state)
            await session.commit()

        # Clear the in-memory value so we can verify it is populated by the restore
        saved = shared_state.runtime_config.pop("notification_config", None)
        try:
            await _init_deployment_state()
            restored = shared_state.runtime_config.get("notification_config")
            assert restored is not None, (
                "notification_config must be restored from runtime_config_json"
            )
            assert restored["webhook_url"] == "https://hooks.example.com/token123"
            assert restored["enabled_events"] == ["TASK_FAILED", "APPROVAL_NEEDED"]
            assert restored["throttle_seconds"] == 120
        finally:
            # Restore previous state to avoid leaking into other tests
            if saved is not None:
                shared_state.runtime_config["notification_config"] = saved
            else:
                shared_state.runtime_config.pop("notification_config", None)
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.drop_all)
            await _engine.dispose()

    @pytest.mark.asyncio
    async def test_notification_config_webhook_url_encrypted_in_db_and_decrypted_on_restore(
        self, mocker
    ):
        """When GITHUB_PAT_ENCRYPTION_KEY is set, the webhook_url stored in
        runtime_config_json is encrypted (ciphertext at rest) and transparently
        decrypted when loaded back into shared_state.runtime_config on restart."""
        from cryptography.fernet import Fernet
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from app import shared_state
        from app.models import DeploymentState
        from app.main import _init_deployment_state
        from app.field_encryption import decrypt_field
        from app.routers.admin_settings import _encrypt_notification_config

        key = Fernet.generate_key().decode()
        mocker.patch.dict("os.environ", {"GITHUB_PAT_ENCRYPTION_KEY": key})

        _engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

        _Session = sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        mocker.patch("app.main.AsyncSessionLocal", _Session)

        plaintext_url = "https://hooks.example.com/secret-bearer-token-abc123"
        encrypted_nc = _encrypt_notification_config(
            {
                "webhook_url": plaintext_url,
                "enabled_events": ["TASK_FAILED"],
                "throttle_seconds": 60,
            }
        )

        # Encrypted value must differ from plaintext and must be Fernet ciphertext
        assert encrypted_nc["webhook_url"] != plaintext_url
        assert decrypt_field(encrypted_nc["webhook_url"]) == plaintext_url

        # Seed DeploymentState with the encrypted notification_config
        async with _Session() as session:
            state = DeploymentState(
                id=1,
                current_revision="r1",
                current_sha="abc123",
                runtime_config_json={"notification_config": encrypted_nc},
            )
            session.add(state)
            await session.commit()

        # Simulate restart: clear in-memory state, restore from DB
        saved = shared_state.runtime_config.pop("notification_config", None)
        try:
            await _init_deployment_state()
            restored = shared_state.runtime_config.get("notification_config")
            assert restored is not None, (
                "notification_config must be restored from runtime_config_json"
            )
            # After decryption, the webhook_url must be the original plaintext
            assert restored["webhook_url"] == plaintext_url, (
                f"Expected decrypted URL {plaintext_url!r}, got {restored['webhook_url']!r}"
            )
            assert restored["enabled_events"] == ["TASK_FAILED"]
            assert restored["throttle_seconds"] == 60
        finally:
            if saved is not None:
                shared_state.runtime_config["notification_config"] = saved
            else:
                shared_state.runtime_config.pop("notification_config", None)
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.drop_all)
            await _engine.dispose()

    @pytest.mark.asyncio
    async def test_init_deployment_state_degrades_on_commit_lock_timeout(self, mocker):
        """A contended DeploymentState write during deploy-detection must NOT crash
        startup. Boot used to die ("Application startup failed. Exiting.") when this
        commit hit a lock timeout, failing the deploy and pinning prod on the old
        revision (incident 2026-06-04). It must now degrade and still boot."""
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlalchemy.exc import OperationalError
        from sqlmodel import SQLModel
        from app.models import DeploymentState, DeploymentStatus
        from app.main import _init_deployment_state
        import app.main as _main

        _engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        _Session = sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        mocker.patch("app.main.AsyncSessionLocal", _Session)

        # Seed an existing deploy-state on a DIFFERENT revision → the new-deploy
        # path, which reaches the (previously fatal) detection commit.
        async with _Session() as session:
            session.add(
                DeploymentState(
                    id=1,
                    current_revision="old-rev",
                    current_sha="old-sha",
                    last_known_good_revision="old-rev",
                    last_known_good_sha="old-sha",
                    status=DeploymentStatus.STABLE,
                )
            )
            await session.commit()

        mocker.patch.object(_main, "_current_revision", "new-rev")
        mocker.patch.object(_main, "_current_sha", "new-sha")

        # Simulate the contended row: the detection commit raises a lock timeout.
        # (Seeded above before this patch; close()/rollback on context-exit are
        # unaffected since only commit is patched.)
        async def _boom(_self):
            raise OperationalError(
                "UPDATE deploymentstate",
                {},
                Exception("canceling statement due to lock timeout"),
            )

        mocker.patch.object(AsyncSession, "commit", _boom)

        try:
            # Must NOT raise — it degrades (logs + rollback) and the app boots.
            await _init_deployment_state()
        finally:
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.drop_all)
            await _engine.dispose()

    @pytest.mark.asyncio
    async def test_init_deployment_state_adopts_resolved_sha(self, mocker):
        """On Cloud Run the resolver returns the real serving SHA; _init_deployment_state
        must adopt it into _current_sha (and therefore the singleton row /
        /api/version) before initializing deployment state."""
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from unittest.mock import AsyncMock
        from app.models import DeploymentState
        from app.main import _init_deployment_state
        import app.main as _main

        _engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        _Session = sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        mocker.patch("app.main.AsyncSessionLocal", _Session)
        mocker.patch.object(_main, "_current_sha", "unknown")
        mocker.patch.object(
            _main,
            "resolve_deployed_sha",
            new=AsyncMock(return_value="cafebabe123"),
        )

        try:
            # First boot — no existing row, so it creates the singleton.
            await _init_deployment_state()
            assert _main._current_sha == "cafebabe123"
            async with _Session() as session:
                row = await session.get(DeploymentState, 1)
                assert row is not None
                assert row.current_sha == "cafebabe123"
        finally:
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.drop_all)
            await _engine.dispose()

    @pytest.mark.asyncio
    async def test_periodic_deploy_state_reconciles_stale_current_sha(self, mocker):
        """A degraded boot write leaves DeploymentState.current_sha stale; the
        periodic health-tick reconciliation must re-detect the deployed SHA and
        reconcile the singleton so the serving-confirmation providers
        (backend_revision / agent_image) read the truth instead of a stale row."""
        from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        from sqlmodel import SQLModel
        from unittest.mock import AsyncMock
        from app.models import DeploymentState, DeploymentStatus
        from app.main import _reconcile_deploy_state_health_tick
        import app.main as _main

        _engine = create_async_engine(
            "sqlite+aiosqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        async with _engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)
        _Session = sessionmaker(
            bind=_engine,
            class_=AsyncSession,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )
        mocker.patch("app.main.AsyncSessionLocal", _Session)

        # Seed a stale singleton: a previous boot's best-effort write failed, so
        # the row still records the OLD revision/sha while this process is
        # actually serving a NEW revision/sha.
        async with _Session() as session:
            session.add(
                DeploymentState(
                    id=1,
                    current_revision="old-rev",
                    current_sha="old-sha",
                    last_known_good_revision="old-rev",
                    last_known_good_sha="old-sha",
                    status=DeploymentStatus.STABLE,
                )
            )
            await session.commit()

        mocker.patch.object(_main, "_current_revision", "new-rev")
        mocker.patch.object(_main, "_current_sha", "unknown")
        mocker.patch.object(
            _main,
            "resolve_deployed_sha",
            new=AsyncMock(return_value="cafebabe123"),
        )

        try:
            # The health tick re-detects the deployed SHA and reconciles the
            # drifted singleton row (and the in-process _current_sha global).
            reconciled = await _reconcile_deploy_state_health_tick()
            assert reconciled is True
            assert _main._current_sha == "cafebabe123"
            async with _Session() as session:
                row = await session.get(DeploymentState, 1)
                assert row is not None
                assert row.current_sha == "cafebabe123"
                assert row.current_revision == "new-rev"
                # deployed_at must reflect this process's actual boot time,
                # not the moment the health tick happened to fire — the tick
                # can run long after boot, and stamping "now" would fabricate
                # a deploy time that postdates an in-flight promotion.
                assert row.deployed_at == _main._process_boot_time

            # No drift — the row already matches the observed serving state, so
            # the tick must be a no-op (conservative: never churn an up-to-date row).
            async with _Session() as session:
                row = await session.get(DeploymentState, 1)
                row.current_sha = "cafebabe123"
                row.current_revision = "new-rev"
                await session.commit()
            assert await _reconcile_deploy_state_health_tick() is False
        finally:
            async with _engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.drop_all)
            await _engine.dispose()
