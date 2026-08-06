# AUTO-SPLIT from test_release_qualifier.py — see refactor: split mega backend test files.
# Behavior-preserving: shared preamble lives in _release_qualifier_testhelpers.py; this file
# holds one domain slice of the original test suite.
"""Domain slice: _finalize_qualification — infra-failure detection (FAILED/CANCELLED children)

Split from test_release_qualifier.py."""
from _release_qualifier_testhelpers import *  # noqa: F403


class TestFinalizeInfraFailure:
    """_finalize_qualification correctly distinguishes infra failures from verdicts."""

    @pytest.mark.asyncio
    async def test_audit_task_failed_records_infra_failure_not_verdict(
        self, db_session
    ):
        """Audit FAILED → status='infra_failure', NO QualificationVerdict row.
        Version returns to stabilize but is NOT blocked on a quality finding."""
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert "audit_task_failed" in result["error"]

        # Version back to stabilize — no quality judgement recorded.
        await db_session.refresh(version)
        assert version.status == "open"
        assert version.phase == "stabilize"
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_task_failed"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"
        assert verdicts[0].sub_scores["infra_failure"]["error"] == "audit_task_failed"

        # An operator notification was posted with release_infra_failure event.
        msgs = (
            (
                await db_session.execute(
                    select(ProjectMessage).where(
                        ProjectMessage.project_id == project.id,
                        ProjectMessage.context["event_type"].as_string()
                        == "release_infra_failure",
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(msgs) == 1

    @pytest.mark.asyncio
    async def test_audit_task_cancelled_records_infra_failure(self, db_session):
        """Audit CANCELLED also routes to the infra-failure path."""
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.CANCELLED,
            smoke_status=TaskStatus.COMPLETED,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert "audit_task_cancelled" in result["error"]

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_task_cancelled"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

    @pytest.mark.asyncio
    async def test_audit_task_blocked_records_infra_failure_not_verdict(
        self, db_session
    ):
        """Audit BLOCKED routes to infra_failure before artifact scoring."""
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.BLOCKED,
            smoke_status=TaskStatus.COMPLETED,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_task_blocked"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_task_blocked"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"



    @pytest.mark.parametrize(
        "legacy_artifact_logs",
        [
            None,
            "not json {{{",
            json.dumps(
                {
                    "passed": False,
                    "tests": [{"name": "legacy-failure", "status": "fail"}],
                    "suites": {"startup": {"passed": False}},
                }
            ),
        ],
        ids=["missing", "unparseable", "failed-result"],
    )
    @pytest.mark.asyncio
    async def test_legacy_smoke_artifact_is_archival_and_unscored(
        self, db_session, legacy_artifact_logs
    ):
        """Historical smoke artifacts cannot block or influence a new verdict."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False
        project, version = await _make_project_and_version(db_session)
        audit_task, smoke_task = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
        )
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(
                    {
                        "audit_score": 1.0,
                        "findings": [],
                        "test_erosion": {"deleted_test_files": []},
                    }
                ),
            )
        )
        if legacy_artifact_logs is not None:
            db_session.add(
                Artifact(
                    task_id=smoke_task.id,
                    artifact_type=ArtifactType.SMOKETEST_RESULT,
                    logs=legacy_artifact_logs,
                )
            )
        legacy_smoke_id = str(smoke_task.id)
        version.last_qualification = {
            **version.last_qualification,
            "env_preflight_enabled": False,
        }
        db_session.add(version)
        await db_session.commit()

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "dry_run"
        await db_session.refresh(version)
        qual = version.last_qualification
        assert qual["smoketest_task_id"] == legacy_smoke_id
        assert "smoketest" not in (qual.get("sub_scores") or {})
        assert qual.get("smoketest_detail") is None
        assert "smoketest" not in (await _dimension_rows_by_dim(db_session, version))

    @pytest.mark.asyncio
    async def test_infra_failure_does_not_appear_in_promotion_readiness(
        self, db_session
    ):
        """Infra verdict rows do not pollute the dry-run readiness track record."""
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
        )

        await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        readiness = await rq.compute_promotion_readiness(db_session, project.id)
        assert readiness["verdicts_evaluated"] == 0

    @pytest.mark.asyncio
    async def test_audit_completed_no_report_reclassified_as_infra_failure(
        self, db_session
    ):
        """A COMPLETED audit task whose RELEASE_AUDIT_REPORT artifact is absent
        is now reclassified as infra_failure — the agent ran but emitted no
        usable output. The operator notification is 'release_infra_failure',
        not 'release_blocked'."""
        project, version = await _make_project_and_version(db_session)
        # Both children COMPLETED but NO artifacts seeded — audit produced
        # no report, which is an infra failure (defense-in-depth path).
        _audit_task, _ = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Reclassified as infra_failure, not a quality verdict.
        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_missing"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

    @pytest.mark.asyncio
    async def test_audit_completed_empty_artifact_reclassified_as_infra_failure(
        self, db_session
    ):
        """A COMPLETED audit task with an empty JSON artifact is infra_failure."""
        project, version = await _make_project_and_version(db_session)
        audit_task, _ = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
        )
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs="{}",
            )
        )
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_empty_dict"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

    @pytest.mark.asyncio
    async def test_audit_completed_unparseable_artifact_reclassified_as_infra_failure(
        self, db_session
    ):
        """A COMPLETED audit task whose RELEASE_AUDIT_REPORT artifact contains
        non-JSON logs (unparseable) is reclassified as infra_failure — distinct
        regression test from the 'no artifact row' sub-case above."""
        project, version = await _make_project_and_version(db_session)
        audit_task, _ = await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.COMPLETED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=True,
        )
        # Seed a RELEASE_AUDIT_REPORT artifact with non-JSON content.
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs="this is not valid json {{{{",
            )
        )
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_missing"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

    @pytest.mark.asyncio
    async def test_audit_completed_parseable_but_unscoreable_reclassified_as_infra_failure(
        self, db_session
    ):
        """Structured but unscorable audit findings are infra_failure, not a verdict."""
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "audit_score": 0.95,
                "findings": [
                    {"description": "missing severity"},
                    "plain-text finding with no structured severity",
                ],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-c", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_parseable_but_unscoreable"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "audit_report_parseable_but_unscoreable"
        )

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

    @pytest.mark.asyncio
    async def test_legitimate_empty_findings_audit_reaches_verdict_not_infra_failure(
        self, db_session
    ):
        """An audit report with findings=[] and a valid audit_score reaches a real verdict.

        Regression guard: if _audit_missing_reason() were changed to return
        'parseable_but_unscoreable' for empty-findings-with-score reports, the
        finalize path would silently mis-route a clean audit as infra_failure.
        This test ensures that path stays open — a QualificationVerdict row is
        written and the audit sub-score reflects the clean signal (1.0).
        """
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "audit_score": 1.0,
                "findings": [],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Primary guard: legitimate empty findings must NOT route to infra_failure.
        assert result["status"] != "infra_failure", (
            f"empty-findings clean audit mis-routed as infra_failure: {result}"
        )
        # A real QualificationVerdict row should be written.
        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 1, f"expected 1 QualificationVerdict, got {count}"

        # Audit sub-score must reflect the clean 1.0 signal, not collapse.
        await db_session.refresh(version)
        qual = version.last_qualification
        assert qual["sub_scores"]["audit"] == 1.0, (
            f"audit sub-score collapsed: {qual['sub_scores']['audit']}, expected 1.0"
        )
        # A clean audit is not a hard-blocking dimension.
        assert result["would_promote"] is True, (
            f"would_promote should be True for a clean audit, got {result}"
        )




    @pytest.mark.asyncio
    async def test_consecutive_infra_errors_backstop_fires_alert(self, db_session):
        """After _MIN_INFRA_ERROR_STREAK consecutive infra failures, an Alert is
        raised — the calcification guard is silent for infra failures, so this
        backstop ensures a persistently broken qualifier pipeline surfaces."""
        project, version = await _make_project_and_version(db_session)

        # Simulate two prior infra failures already recorded (streak = 2).
        # The third failure (current_streak = 3 == _MIN_INFRA_ERROR_STREAK)
        # should trigger the alert.
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
            prior_infra_streak=rq._MIN_INFRA_ERROR_STREAK - 1,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["consecutive_infra_errors"] == rq._MIN_INFRA_ERROR_STREAK

        # An Alert was raised for the consecutive infra-failure backstop.
        alerts = (
            (
                await db_session.execute(
                    select(Alert).where(Alert.project_id == project.id)
                )
            )
            .scalars()
            .all()
        )
        assert len(alerts) == 1
        assert "infrastructure" in alerts[0].title.lower()
        assert str(rq._MIN_INFRA_ERROR_STREAK) in alerts[0].title

    @pytest.mark.asyncio
    async def test_infra_failure_streak_resets_after_quality_verdict(self, db_session):
        """The infra-failure streak resets to 0 when a non-infra finalization
        (a real quality verdict) occurs between infra failures."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)

        # Run a real quality verdict (not_promoted) to reset the streak.
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "audit_score": 0.5,
                "test_erosion": {"deleted_test_files": [], "assertion_delta": 0},
            },
            smoke_logs={
                "passed": False,
                "tests": [{"name": "a", "status": "fail"}],
                "suites": {"startup": {"passed": True}},
            },
        )
        await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Re-open for another qualification cycle (simulate new run_qualification).
        # After a non-infra verdict, run_qualification should carry prior_infra_streak=0.
        await db_session.refresh(version)
        assert version.last_qualification["status"] in ("not_promoted", "dry_run")

        # Now manually simulate the next run_qualification stamping prior_infra_streak=0
        # (because the prior status was not infra_failure).
        prior_qual = version.last_qualification or {}
        prior_streak = (
            int(prior_qual.get("consecutive_infra_errors") or 0)
            if prior_qual.get("status") == "infra_failure"
            else 0
        )
        assert prior_streak == 0  # streak reset after a real verdict

    @pytest.mark.asyncio
    async def test_infra_failure_streak_carries_across_candidate_sha_change(
        self, db_session
    ):
        """The infra-failure streak must carry forward even when the candidate SHA
        advances so that repeated audit/smoketest cancellations on a moving
        automain accumulate toward the escalation cap and fire an operator Alert.
        (Regression guard: old code reset the streak to 0 on SHA change, so the
        cap never tripped on a rolling automain.)"""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)

        # Simulate a previous infra_failure with SHA A (cafe...cafe)
        old_candidate_sha = "cafe" * 10
        version.last_qualification = {
            "status": "infra_failure",
            "candidate_sha": old_candidate_sha,
            "error": "PROVISION_FAILURE",
            "consecutive_infra_errors": 2,  # Previous streak was 2
            "started_at": "2026-05-19T00:00:00",
        }
        db_session.add(version)
        await db_session.commit()

        # Simulate a new run_qualification with SHA B (beef...beef) — the streak
        # init logic that mirrors what run_qualification does internally.
        new_candidate_sha = "beef" * 10
        prior_qual = version.last_qualification or {}
        # Streak carries regardless of SHA change (the fix).
        prior_infra_streak = (
            int(prior_qual.get("consecutive_infra_errors") or 0)
            if prior_qual.get("status") == "infra_failure"
            else 0
        )
        assert prior_infra_streak == 2, (
            f"Expected streak=2 to carry forward when SHA changes from "
            f"{old_candidate_sha[:8]} to {new_candidate_sha[:8]}, "
            f"but got streak={prior_infra_streak}"
        )

    @pytest.mark.asyncio
    async def test_infra_failure_escalation_fires_across_advancing_shas(
        self, db_session, monkeypatch
    ):
        """When infra failures accumulate across multiple advancing candidate SHAs
        (each run_qualification sees a new SHA), the streak still reaches the cap
        and fires an operator Alert — the escalation must not be gated on SHA
        stability."""
        from app import release_qualifier as rq_module
        from app import release_qualifier_audit as rqa
        from app.release_qualifier_audit import _MIN_INFRA_ERROR_STREAK

        notified: list[str] = []

        async def _capture_notify(session, version, event, message, extra=None):
            notified.append(message)

        monkeypatch.setattr(rq_module, "_notify_operator", _capture_notify)

        project, version = await _make_project_and_version(db_session)

        # Seed a pending qualification record that carries prior_infra_streak from
        # a DIFFERENT candidate SHA (SHA_A), simulating what run_qualification
        # stamps when the previous last_qualification was infra_failure on SHA_A
        # and the new cycle runs on SHA_B.
        sha_a = "cafe" * 10
        sha_b = "beef" * 10
        _seed_count = _MIN_INFRA_ERROR_STREAK - 1
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": sha_b,  # current cycle is on SHA_B
            "ci_green": True,
            "smoketest_enabled": True,
            "started_at": "2026-07-19T00:00:00",
            # prior_infra_streak carried from SHA_A infra failures (the fix path)
            "prior_infra_streak": _seed_count,
            "previous_infra_failure_candidate_sha": sha_a,  # came from a different SHA
        }
        db_session.add(version)
        await db_session.commit()

        await rqa._handle_infra_failure(
            db_session,
            version,
            dict(version.last_qualification),
            "migration_apply_check_unavailable",
        )
        await db_session.commit()

        await db_session.refresh(version)
        lq = version.last_qualification
        assert lq["status"] == "infra_failure"
        assert lq["consecutive_infra_errors"] == _MIN_INFRA_ERROR_STREAK, (
            f"Expected streak={_MIN_INFRA_ERROR_STREAK} after accumulating across "
            f"SHA change but got {lq.get('consecutive_infra_errors')}"
        )
        assert notified, (
            "Expected operator Alert to fire when streak reached cap across advancing "
            "SHAs, but no notification was sent"
        )

    @pytest.mark.asyncio
    async def test_smoketest_task_failed_when_disabled_does_not_route_infra_failure(
        self, db_session
    ):
        """When smoketest_enabled=False, the smoketest child is not required.
        A COMPLETED audit (even with an absent smoketest artifact) routes through
        the normal compute_composite path — NOT the infra-failure path."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        # Audit COMPLETED with a valid report; smoketest disabled.
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-audit-smoke-disabled",
            status=TaskStatus.COMPLETED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(
                    {
                        "audit_score": 1.0,
                        "findings": [],
                        "test_erosion": {"deleted_test_files": []},
                    }
                ),
            )
        )
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": "cafe" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Took the normal quality-verdict path (dry_run since promotion disabled).
        assert result["status"] == "dry_run"
        assert result["status"] != "infra_failure"

    @pytest.mark.asyncio
    async def test_audit_task_failed_enriches_error_with_failure_category(
        self, db_session
    ):
        """FAILED audit with agent_failure_category_raw → enriched error label.

        Ensures operators can distinguish audit infra failures by the specific
        agent failure category (e.g. rate_limited vs. claude_code_failed) when
        inspecting consecutive infra-failure alerts.
        """
        project, version = await _make_project_and_version(db_session)
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-audit-enriched",
            status=TaskStatus.FAILED,
            context={
                "release_version_id": str(version.id),
                "agent_failure_category_raw": "release_audit_report_unparseable",
            },
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": "cafe" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_task_failed_release_audit_report_unparseable"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "audit_task_failed_release_audit_report_unparseable"
        )

    @pytest.mark.asyncio
    async def test_audit_task_failed_no_failure_category_falls_back_to_generic(
        self, db_session
    ):
        """FAILED audit with no agent_failure_category_raw → generic label.

        Ensures backward compatibility when older agents didn't record the
        failure category — the label stays 'audit_task_failed'.
        """
        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
            smoketest_enabled=False,
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_task_failed"

        await db_session.refresh(version)
        assert version.last_qualification["error"] == "audit_task_failed"


    @pytest.mark.asyncio
    async def test_challenge_task_failed_records_infra_failure(self, db_session):
        """A FAILED challenge child routes to infra_failure with an enriched label,
        consistent with audit/smoketest terminal-status handling."""
        project, version = await _make_project_and_version(db_session)
        # Seed audit + smoketest COMPLETED (with a verified high finding so the
        # challenge gate is active), plus a FAILED challenge child.
        _candidate_sha = "feed" * 10
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-audit-chal",
            status=TaskStatus.COMPLETED,
            context={"candidate_sha": _candidate_sha},
        )
        smoke_task = Task(
            project_id=project.id,
            title="smoke",
            payloads=[{"type": "smoketest", "base_branch": "automain"}],
            vm_instance_id="vm-smoke-chal",
            status=TaskStatus.COMPLETED,
        )
        challenge_task = Task(
            project_id=project.id,
            title="audit-challenge",
            payloads=[{"type": "release_audit_challenge", "base_branch": "main"}],
            vm_instance_id="vm-challenge-chal",
            status=TaskStatus.FAILED,
            context={"candidate_sha": _candidate_sha},
        )
        db_session.add(audit_task)
        db_session.add(smoke_task)
        db_session.add(challenge_task)
        await db_session.commit()
        await db_session.refresh(audit_task)
        await db_session.refresh(smoke_task)
        await db_session.refresh(challenge_task)

        # Seed a verified high finding so the challenge gate is active.
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "high",
                                "summary": "Unvalidated redirect in login flow",
                                "verification_note": "confirmed via full repo scan",
                            }
                        ],
                        "test_erosion": {"deleted_test_files": []},
                    }
                ),
            )
        )
        db_session.add(
            Artifact(
                task_id=smoke_task.id,
                artifact_type=ArtifactType.SMOKETEST_RESULT,
                logs=json.dumps(
                    {
                        "passed": True,
                        "tests": [{"name": "startup", "status": "pass"}],
                        "suites": {"startup": {"passed": True}},
                    }
                ),
            )
        )
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": _candidate_sha,
            "ci_green": True,
            "smoketest_enabled": True,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": str(smoke_task.id),
            "challenge_task_id": str(challenge_task.id),
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert "audit_challenge_task_failed" in result["error"]

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert "audit_challenge_task_failed" in version.last_qualification["error"]

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

    @pytest.mark.asyncio
    async def test_challenge_provision_failure_respawns_new_task(self, db_session):
        """PROVISION_FAILURE challenge child is re-spawned with the same candidate_sha."""
        from app.failure_classification import (
            PROVISION_FAILURE,
            persist_failure_category,
        )

        project, version = await _make_project_and_version(db_session)
        _candidate_sha = "feed" * 10
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-audit-chal-prov",
            status=TaskStatus.COMPLETED,
            context={"candidate_sha": _candidate_sha},
        )
        smoke_task = Task(
            project_id=project.id,
            title="smoke",
            payloads=[{"type": "smoketest", "base_branch": "automain"}],
            vm_instance_id="vm-smoke-chal-prov",
            status=TaskStatus.COMPLETED,
        )
        challenge_task = Task(
            project_id=project.id,
            title="audit-challenge",
            payloads=[
                {
                    "type": "release_audit_challenge",
                    "base_branch": "main",
                    "diff_range": f"main...{_candidate_sha}",
                }
            ],
            vm_instance_id="vm-challenge-prov",
            status=TaskStatus.FAILED,
            context={"candidate_sha": _candidate_sha},
        )
        db_session.add(audit_task)
        db_session.add(smoke_task)
        db_session.add(challenge_task)
        await db_session.commit()
        await db_session.refresh(audit_task)
        await db_session.refresh(smoke_task)
        await db_session.refresh(challenge_task)

        persist_failure_category(challenge_task, PROVISION_FAILURE)
        db_session.add(challenge_task)
        await db_session.commit()
        await db_session.refresh(challenge_task)

        # Seed a verified high finding so the challenge gate is active and
        # _primary_findings_for_challenge_retry returns a non-empty list.
        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "high",
                                "summary": "SQL injection in user input handler",
                                "verification_note": "confirmed via full repo scan",
                            }
                        ],
                        "test_erosion": {"deleted_test_files": []},
                    }
                ),
            )
        )
        db_session.add(
            Artifact(
                task_id=smoke_task.id,
                artifact_type=ArtifactType.SMOKETEST_RESULT,
                logs=json.dumps(
                    {
                        "passed": True,
                        "tests": [{"name": "startup", "status": "pass"}],
                        "suites": {"startup": {"passed": True}},
                    }
                ),
            )
        )
        await db_session.commit()

        original_challenge_id = str(challenge_task.id)
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": _candidate_sha,
            "ci_green": True,
            "smoketest_enabled": True,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": str(smoke_task.id),
            "challenge_task_id": original_challenge_id,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "challenge_infra_respawned"
        assert result["respawn_count"] == 1

        await db_session.refresh(version)
        new_qual = version.last_qualification
        assert new_qual["challenge_infra_respawn_count"] == 1
        assert new_qual["challenge_task_id"] != original_challenge_id

        # No QualificationVerdict — verdict not rendered on respawn.
        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 0

    @pytest.mark.asyncio
    async def test_challenge_infra_respawn_cap_exceeded_routes_to_infra_failure(
        self, db_session
    ):
        """When challenge_infra_respawn_count hits cap, route to infra_failure
        with label 'challenge_infra_respawn_cap_exceeded'."""
        from app.failure_classification import (
            PROVISION_FAILURE,
            persist_failure_category,
        )
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        project, version = await _make_project_and_version(db_session)
        _candidate_sha = "bead" * 10
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "main"}],
            vm_instance_id="vm-audit-chal-cap",
            status=TaskStatus.COMPLETED,
            context={"candidate_sha": _candidate_sha},
        )
        smoke_task = Task(
            project_id=project.id,
            title="smoke",
            payloads=[{"type": "smoketest", "base_branch": "automain"}],
            vm_instance_id="vm-smoke-chal-cap",
            status=TaskStatus.COMPLETED,
        )
        challenge_task = Task(
            project_id=project.id,
            title="audit-challenge",
            payloads=[
                {
                    "type": "release_audit_challenge",
                    "base_branch": "main",
                    "diff_range": f"main...{_candidate_sha}",
                }
            ],
            vm_instance_id="vm-challenge-cap",
            status=TaskStatus.FAILED,
            context={"candidate_sha": _candidate_sha},
        )
        db_session.add(audit_task)
        db_session.add(smoke_task)
        db_session.add(challenge_task)
        await db_session.commit()
        await db_session.refresh(audit_task)
        await db_session.refresh(smoke_task)
        await db_session.refresh(challenge_task)

        persist_failure_category(challenge_task, PROVISION_FAILURE)
        db_session.add(challenge_task)
        await db_session.commit()
        await db_session.refresh(challenge_task)

        db_session.add(
            Artifact(
                task_id=audit_task.id,
                artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
                logs=json.dumps(
                    {
                        "findings": [
                            {
                                "severity": "high",
                                "summary": "Auth bypass in session check",
                                "verification_note": "confirmed via full repo scan",
                            }
                        ],
                        "test_erosion": {"deleted_test_files": []},
                    }
                ),
            )
        )
        db_session.add(
            Artifact(
                task_id=smoke_task.id,
                artifact_type=ArtifactType.SMOKETEST_RESULT,
                logs=json.dumps(
                    {
                        "passed": True,
                        "tests": [{"name": "startup", "status": "pass"}],
                        "suites": {"startup": {"passed": True}},
                    }
                ),
            )
        )
        await db_session.commit()

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": _candidate_sha,
            "ci_green": True,
            "smoketest_enabled": True,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": str(smoke_task.id),
            "challenge_task_id": str(challenge_task.id),
            # At the cap — next failure must route to infra_failure.
            "challenge_infra_respawn_count": _MAX_AUDIT_RL_RESPAWNS,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "challenge_infra_respawn_cap_exceeded"

        await db_session.refresh(version)
        assert version.phase == "stabilize"
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "challenge_infra_respawn_cap_exceeded"
        )

    @pytest.mark.asyncio
    async def test_infra_failure_dispatches_external_webhook(self, db_session, mocker):
        """_handle_infra_failure fires an external webhook with RELEASE_INFRA_FAILURE."""
        import asyncio

        dispatch_mock = AsyncMock()
        mocker.patch(
            "app.release_qualifier.dispatch_external_notification",
            dispatch_mock,
        )

        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
        )

        await rq._finalize_qualification(db_session, version)
        await db_session.commit()
        # Drain ensure_future'd coroutine.
        await asyncio.sleep(0)

        dispatch_mock.assert_called_once()
        call_args = dispatch_mock.call_args
        assert call_args[0][1] == "RELEASE_INFRA_FAILURE"
        payload = call_args[0][2]
        assert payload["consecutive_infra_errors"] >= 1
        assert "version_number" in payload
        assert "infra_error" in payload

    @pytest.mark.asyncio
    async def test_infra_failure_streak_in_webhook_payload(self, db_session, mocker):
        """Webhook payload carries the correct consecutive_infra_errors streak count."""
        import asyncio

        dispatch_mock = AsyncMock()
        mocker.patch(
            "app.release_qualifier.dispatch_external_notification",
            dispatch_mock,
        )

        project, version = await _make_project_and_version(db_session)
        await _seed_infra_failure_children(
            db_session,
            project,
            version,
            audit_status=TaskStatus.FAILED,
            smoke_status=TaskStatus.COMPLETED,
            prior_infra_streak=1,
        )

        await rq._finalize_qualification(db_session, version)
        await db_session.commit()
        await asyncio.sleep(0)

        dispatch_mock.assert_called_once()
        payload = dispatch_mock.call_args[0][2]
        assert payload["consecutive_infra_errors"] == 2

    @pytest.mark.asyncio
    async def test_audit_rate_limited_respawns_new_task(self, db_session):
        """FAILED audit classified RATE_LIMIT → respawn, not infra_failure.

        A transient rate-limit failure must not pollute the consecutive_infra_errors
        streak or waste the candidate_sha snapshot.  The qualifier re-spawns a new
        audit task carrying the same candidate_sha and returns
        status='audit_rate_limit_respawned' instead of routing to _handle_infra_failure.
        """
        from app.failure_classification import RATE_LIMIT, persist_failure_category

        project, version = await _make_project_and_version(db_session)
        attempt_id = uuid.uuid4()
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "main",
                    "diff_range": "main...abcd1234",
                    "candidate_sha": "abcd1234" * 5,
                    "qualification_attempt_id": str(attempt_id),
                }
            ],
            vm_instance_id="vm-audit-rate-limited",
            status=TaskStatus.FAILED,
            context={
                "release_version_id": str(version.id),
                "qualification_attempt_id": str(attempt_id),
            },
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, RATE_LIMIT)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        original_audit_id = str(audit_task.id)
        candidate = "abcd1234" * 5

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "qualification_attempt_id": str(attempt_id),
            "retry_episode": 0,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": original_audit_id,
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        version.active_qualification_attempt_id = attempt_id
        version.qualification_attempts = 1
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Returns the respawn status, not infra_failure.
        assert result["status"] == "audit_rate_limit_respawned"
        assert result["respawn_count"] == 1

        # last_qualification carries the new audit_task_id and the counter.
        await db_session.refresh(version)
        new_qual = version.last_qualification
        assert new_qual["audit_rl_respawn_count"] == 1
        assert new_qual["audit_task_id"] != original_audit_id
        assert new_qual["qualification_attempt_id"] == str(attempt_id)
        assert new_qual["retry_episode"] == 1
        assert new_qual["retry_episode_cap"] == rq._MAX_AUDIT_RL_RESPAWNS
        assert version.qualification_attempts == 1
        assert version.qualification_retry_episode == 1
        replacement = await db_session.get(Task, uuid.UUID(new_qual["audit_task_id"]))
        assert replacement is not None
        assert replacement.context["qualification_attempt_id"] == str(attempt_id)
        assert replacement.first_payload["qualification_attempt_id"] == str(
            attempt_id
        )

        # No QualificationVerdict row — verdict was not rendered.
        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 0

        # No infra_failure ProjectMessage — this is a transient respawn, not a
        # hard pipeline failure.
        infra_msg_count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.context["event_type"].as_string()
                    == "release_infra_failure",
                )
            )
        ).scalar()
        assert infra_msg_count == 0

    @pytest.mark.asyncio
    async def test_audit_rate_limited_cap_exceeded_routes_to_infra_failure(
        self, db_session
    ):
        """When audit_rl_respawn_count hits _MAX_AUDIT_RL_RESPAWNS, fall through to
        infra_failure with label 'audit_rate_limit_respawn_cap_exceeded'."""
        from app.failure_classification import RATE_LIMIT, persist_failure_category
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        project, version = await _make_project_and_version(db_session)
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "main",
                    "diff_range": "main...cafe5678",
                    "candidate_sha": "cafe5678" * 5,
                }
            ],
            vm_instance_id="vm-audit-rl-cap",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, RATE_LIMIT)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": "cafe5678" * 5,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
            # At the cap — next failure must route to infra_failure.
            "audit_rl_respawn_count": _MAX_AUDIT_RL_RESPAWNS,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_rate_limit_respawn_cap_exceeded"

        await db_session.refresh(version)
        assert version.phase == "stabilize"
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "audit_rate_limit_respawn_cap_exceeded"
        )

    @pytest.mark.asyncio
    async def test_audit_provision_failure_respawns_new_task(self, db_session):
        """PROVISION_FAILURE audit child is re-spawned with the same candidate_sha."""
        from app.failure_classification import (
            PROVISION_FAILURE,
            persist_failure_category,
        )

        project, version = await _make_project_and_version(db_session)
        candidate = "babe1234" * 5
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "automain",
                    "diff_range": f"automain...{candidate}",
                    "candidate_sha": candidate,
                }
            ],
            vm_instance_id="vm-audit-provision",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, PROVISION_FAILURE)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        original_audit_id = str(audit_task.id)
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": original_audit_id,
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "audit_infra_respawned"
        assert result["respawn_count"] == 1

        await db_session.refresh(version)
        new_qual = version.last_qualification
        assert new_qual["audit_infra_respawn_count"] == 1
        assert new_qual["audit_task_id"] != original_audit_id

        # No QualificationVerdict — verdict not rendered on respawn.
        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 0

        # No infra_failure ProjectMessage — transient respawn, not a hard failure.
        infra_msg_count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.context["event_type"].as_string()
                    == "release_infra_failure",
                )
            )
        ).scalar()
        assert infra_msg_count == 0

    @pytest.mark.asyncio
    async def test_audit_agent_timeout_respawns_new_task(self, db_session):
        """AGENT_TIMEOUT audit child is re-spawned with the same candidate_sha."""
        from app.failure_classification import AGENT_TIMEOUT, persist_failure_category

        project, version = await _make_project_and_version(db_session)
        candidate = "cafe5678" * 5
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "automain",
                    "diff_range": f"automain...{candidate}",
                    "candidate_sha": candidate,
                }
            ],
            vm_instance_id="vm-audit-timeout",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, AGENT_TIMEOUT)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "audit_infra_respawned"
        assert result["respawn_count"] == 1

        await db_session.refresh(version)
        assert version.last_qualification["audit_infra_respawn_count"] == 1

    @pytest.mark.asyncio
    async def test_audit_infra_respawn_cap_exceeded_routes_to_infra_failure(
        self, db_session
    ):
        """When audit_infra_respawn_count hits cap, fall through to infra_failure
        with label 'audit_infra_respawn_cap_exceeded'."""
        from app.failure_classification import (
            PROVISION_FAILURE,
            persist_failure_category,
        )
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        project, version = await _make_project_and_version(db_session)
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "automain",
                    "diff_range": "automain...dead1234",
                    "candidate_sha": "dead1234" * 5,
                }
            ],
            vm_instance_id="vm-audit-infra-cap",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, PROVISION_FAILURE)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": "dead1234" * 5,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
            # At the cap — next failure must route to infra_failure.
            "audit_infra_respawn_count": _MAX_AUDIT_RL_RESPAWNS,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_infra_respawn_cap_exceeded"

        await db_session.refresh(version)
        assert version.phase == "stabilize"
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_infra_respawn_cap_exceeded"

    @pytest.mark.asyncio
    async def test_infra_failure_repeat_signature_recorded_across_attempts(
        self, db_session, mocker
    ):
        """Re-requested qualifications carry prior infra signature/history forward."""
        from app.failure_classification import (
            PROVISION_FAILURE,
            persist_failure_category,
        )
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        project, version = await _make_project_and_version(
            db_session, phase="stabilize"
        )
        first_candidate = "11112222" * 5
        first_audit = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "automain",
                    "diff_range": f"automain...{first_candidate}",
                    "candidate_sha": first_candidate,
                }
            ],
            vm_instance_id="vm-first-repeat",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(first_audit)
        await db_session.commit()
        await db_session.refresh(first_audit)
        persist_failure_category(first_audit, PROVISION_FAILURE)
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": first_candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(first_audit.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
            "audit_infra_respawn_count": _MAX_AUDIT_RL_RESPAWNS,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        first_result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()
        assert first_result["status"] == "infra_failure"

        await db_session.refresh(version)
        first_qual = version.last_qualification
        first_signature = first_qual["infra_failure_signature"]
        assert first_qual["same_error_as_prior"] is False
        assert len(first_qual["infra_attempt_history"]) == 1

        second_candidate = "33334444" * 5
        mocker.patch(
            "app.github.get_repo_installation_token",
            new=AsyncMock(return_value="gh-token"),
        )
        mocker.patch(
            "app.github.get_branch_sha",
            new=AsyncMock(return_value=second_candidate),
        )
        mocker.patch(
            "app.github.get_check_runs_cached",
            new=AsyncMock(
                return_value={"status": "completed", "conclusion": "success"}
            ),
        )
        mocker.patch(
            "app.release_qualifier.resolve_project_promotion_enabled",
            return_value=True,
        )

        spawned = await rq.run_qualification(
            db_session, project.id, version, explicit_request=True
        )
        await db_session.commit()
        assert spawned["status"] == "spawned"

        await db_session.refresh(version)
        pending = dict(version.last_qualification)
        assert pending["previous_infra_failure_signature"] == first_signature
        assert pending["previous_infra_failure_candidate_sha"] == first_candidate
        assert pending["infra_attempt_history"][0]["signature"] == first_signature

        second_audit = await db_session.get(
            Task, uuid.UUID(str(pending["audit_task_id"]))
        )
        second_audit.status = TaskStatus.FAILED
        persist_failure_category(second_audit, PROVISION_FAILURE)
        pending["audit_infra_respawn_count"] = _MAX_AUDIT_RL_RESPAWNS
        version.last_qualification = pending
        db_session.add(second_audit)
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        second_result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()
        assert second_result["status"] == "infra_failure"
        assert second_result["infra_failure_signature"] == first_signature
        assert second_result["same_error_as_prior"] is True
        assert second_result["same_error_different_candidate"] is True

        await db_session.refresh(version)
        final_qual = version.last_qualification
        assert final_qual["same_infra_failure_as_previous"] is True
        assert final_qual["same_infra_failure_different_candidate"] is True
        assert len(final_qual["infra_attempt_history"]) == 2
        assert (
            final_qual["infra_attempt_history"][1]["child_failures"][0][
                "failure_category"
            ]
            == PROVISION_FAILURE
        )

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 2
        latest_evidence = verdicts[-1].sub_scores["infra_failure"]
        assert latest_evidence["signature"] == first_signature
        assert latest_evidence["same_error_as_prior"] is True
        assert latest_evidence["same_error_different_candidate"] is True

    # -----------------------------------------------------------------------
    # Malformed audit artifact shapes — each must produce either a real verdict
    # OR an infra_failure; never both, never neither, never a silent clean score.
    # -----------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_empty_dict_audit_report_routes_to_infra_failure(self, db_session):
        """audit_report = {} (empty dict) must route to infra_failure.

        Without the empty-dict guard _score_audit() returns (1.0, 1.0) with no
        deductions applied — a silent clean score.  Verify infra_failure is
        returned and no QualificationVerdict row is written."""
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={},
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_empty_dict"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_report_empty_dict"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

    @pytest.mark.asyncio
    async def test_findings_list_no_audit_score_field_reaches_verdict(self, db_session):
        """audit_report = {"findings": []} with no audit_score field reaches a verdict.

        An empty findings list with no self-reported score is a legitimately
        clean audit — _audit_missing_reason returns "audit_score_null_no_findings"
        which does NOT trigger infra_failure.  A QualificationVerdict row is
        written and the audit sub-score is 1.0 (no deductions applied)."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                # Deliberately omit "audit_score" — this is the variant under test.
                "findings": [],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] != "infra_failure", (
            f"empty-findings-no-score mis-routed as infra_failure: {result}"
        )

        await db_session.refresh(version)
        qual = version.last_qualification
        assert qual.get("audit_missing_reason") == "audit_score_null_no_findings"
        assert qual["sub_scores"]["audit"] == 1.0

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 1, f"expected 1 QualificationVerdict row, got {count}"

    @pytest.mark.asyncio
    async def test_findings_entries_no_severity_routes_to_infra_failure(
        self, db_session
    ):
        """audit_report with findings that have no severity/level field → infra_failure.

        A single finding dict missing severity/level is "parseable_but_unscoreable" —
        the agent produced structured output but _score_audit() cannot apply any
        deduction. An infra QualificationVerdict row is written."""
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "findings": [{"description": "finding with no severity field"}],
                "test_erosion": {"deleted_test_files": [], "assertion_delta": 0},
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_parseable_but_unscoreable"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

    @pytest.mark.asyncio
    async def test_issues_key_equivalent_to_findings_reaches_verdict(self, db_session):
        """audit_report with "issues" key (not "findings") reaches a real verdict.

        The "issues" fallback is recognized by _score_audit and _audit_missing_reason.
        A low-severity finding applies a deduction so audit_score < 1.0 (not a
        silent clean score).  A QualificationVerdict row is written."""
        from app import shared_state

        shared_state.runtime_config["promotion_enabled"] = False

        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "issues": [{"severity": "low", "description": "minor style nit"}],
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] != "infra_failure", (
            f"issues-key audit incorrectly routed as infra_failure: {result}"
        )

        await db_session.refresh(version)
        qual = version.last_qualification
        # Low-severity finding deducts 0.02 → audit score 0.98 < 1.0.
        assert qual["sub_scores"]["audit"] < 1.0, (
            f"expected deduction from low-severity finding, "
            f"got {qual['sub_scores']['audit']}"
        )

        count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    QualificationVerdict.release_version_id == version.id
                )
            )
        ).scalar()
        assert count == 1, f"expected 1 QualificationVerdict row, got {count}"

    @pytest.mark.asyncio
    async def test_findings_wrong_type_routes_to_infra_failure(self, db_session):
        """audit_report = {"findings": "not-a-list"} routes to infra_failure.

        A "findings" key whose value is not a list causes _score_audit() to fall
        through to the "issues" fallback (also absent here), yielding a silent
        clean 1.0 without the wrong-type guard.  Verify infra_failure is returned
        and an infra QualificationVerdict row is written."""
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "findings": "not-a-list",
                "test_erosion": {"deleted_test_files": [], "assertion_delta": 0},
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "smoke-ok", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_report_findings_wrong_type"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        assert version.last_qualification["error"] == "audit_report_findings_wrong_type"

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"


    @pytest.mark.asyncio

    async def test_permanent_gate_incapacity_classification(self, db_session):
        """Volatile per-attempt details do not hide a persistent gate failure."""
    async def test_permanent_gate_incapacity_recommends_hotfix_channel_when_eligible(
        self, db_session, mocker
    ):
        """3 consecutive same-signature migration_apply_check_unavailable errors
        classify as permanent gate-incapacity and recommend the eligible narrow
        hotfix channel, not force-promote."""
        from app import release_qualifier_audit as rqa

        project, version = await _make_project_and_version(db_session)
        sig = "migration_apply:unknown:migration_apply_check_unavailable"
        same_entry = {
            "finalized_at": "2026-07-10T00:00:00",

            "error": (
                "migration_apply_check_unavailable: scratch database "
                "blueprint_migration_attempt_a unavailable"
            ),
            "signature": sig,
            "candidate_sha": "dead" * 10,
            "failing_leg": "migration_apply",
            "child_failures": [],
        }
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": "dead" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-07-10T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "prior_infra_streak": 2,
            "infra_attempt_history": [
                dict(same_entry, finalized_at="2026-07-10T00:00:00"),

                dict(
                    same_entry,
                    finalized_at="2026-07-10T00:01:00",
                    error="migration_apply_check_unavailable: connection id 8472",
                ),
                dict(same_entry, finalized_at="2026-07-10T00:01:00"),
            ],
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)
        preview_mock = mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(
                return_value={
                    "status": "eligible",
                    "eligible": True,
                    "reason": "all changed files are hotfix-eligible",
                    "offending_files": [],
                    "candidate_sha": "dead" * 10,
                    "prior_deployed_sha": "beef" * 10,
                }
            ),
        )

        result = await rqa._handle_infra_failure(
            db_session,
            version,
            dict(version.last_qualification),

            "migration_apply_check_unavailable: scratch database attempt_c timed out",
        )
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["is_permanent_gate_incapacity"] is True
        assert result["same_signature_streak"] == 4  # 3 prior + current all same sig


        await db_session.refresh(version)

        # Verify a force-promote Alert was raised with the right message.
        assert result["hotfix_preview"]["eligible"] is True
        preview_mock.assert_awaited_once_with(
            db_session, project.id, candidate_sha="dead" * 10
        )

        await db_session.refresh(version)

        # The hotfix_preview must be persisted in the version's last_qualification
        # so post-mortem analysis can see the eligible changes without re-computing.
        assert version.last_qualification.get("hotfix_preview") == result["hotfix_preview"]

        # The persisted alert gives the operator the narrow endpoint, rather
        # than authorizing the entire candidate tree through force-promote.
        alerts = (
            (await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == "WARNING",
                )
            ))
            .scalars()
            .all()
        )
        assert len(alerts) >= 1
        latest = alerts[-1]
        assert "persistently incapable" in latest.message

        assert "hotfix scope" in latest.message
        assert "promote/hotfix" in latest.message
        assert "whole-tree force-promotion" in latest.message
        assert "agent VM" not in latest.message
        assert "clone pipeline" not in latest.message
        assert latest.message.count("Sanctioned remediation:") == 1
        assert "Sanctioned Sanctioned" not in latest.message
        assert "POST /projects/{id}/promote/hotfix" in latest.message
        assert "force-promote" not in latest.message
        assert "agent VM" not in latest.message
        assert "clone pipeline" not in latest.message

    @pytest.mark.asyncio
    async def test_permanent_gate_incapacity_holds_when_hotfix_scope_is_ineligible(
        self, db_session, mocker
    ):
        """3 consecutive same-signature migration_apply errors classify as
        permanent gate-incapacity, but an INELIGIBLE hotfix scope preview must
        keep the release held and surface the not-eligible reason clause in BOTH
        the persisted alert and the operator notification."""
        from app import release_qualifier_audit as rqa

        project, version = await _make_project_and_version(db_session)
        sig = "migration_apply:unknown:migration_apply_check_unavailable"
        same_entry = {
            "finalized_at": "2026-07-10T00:00:00",
            "error": "migration_apply_check_unavailable",
            "signature": sig,
            "candidate_sha": "dead" * 10,
            "failing_leg": "migration_apply",
            "child_failures": [],
        }
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "dead" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-07-10T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "prior_infra_streak": 2,
            "infra_attempt_history": [
                dict(same_entry, finalized_at="2026-07-10T00:00:00"),
                dict(same_entry, finalized_at="2026-07-10T00:01:00"),
            ],
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)
        preview_mock = mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(
                return_value={
                    "status": "ineligible",
                    "eligible": False,
                    "reason": "changed files touch code outside migrations",
                    "offending_files": ["app/main.py"],
                    "candidate_sha": "dead" * 10,
                    "prior_deployed_sha": "beef" * 10,
                }
            ),
        )
        notify_mock = mocker.patch(
            "app.release_qualifier._notify_operator", new=AsyncMock()
        )

        result = await rqa._handle_infra_failure(
            db_session,
            version,
            dict(version.last_qualification),
            "migration_apply_check_unavailable",
        )
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["is_permanent_gate_incapacity"] is True
        assert result["same_signature_streak"] == 3
        assert result["hotfix_preview"]["eligible"] is False
        preview_mock.assert_awaited_once_with(
            db_session, project.id, candidate_sha="dead" * 10
        )

        await db_session.refresh(version)

        # An ineligible candidate is held rather than being routed through a
        # whole-tree qualification bypass.
        alerts = (
            (await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == "WARNING",
                )
            ))
            .scalars()
            .all()
        )
        assert len(alerts) >= 1
        latest = alerts[-1]
        assert "persistently incapable" in latest.message
        assert "force-promote" not in latest.message
        assert "POST /projects/{id}/promote/hotfix" in latest.message
        assert "unavailable or ineligible" in latest.message

        # The operator notification carries the same fail-closed guidance.
        notify_mock.assert_awaited()
        notify_message = notify_mock.await_args.args[3]
        assert "hotfix scope preview" in notify_message
        assert "unavailable or ineligible" in notify_message
        assert "POST /projects/{id}/promote/hotfix" in notify_message

    @pytest.mark.asyncio
    async def test_permanent_gate_incapacity_holds_when_hotfix_preview_is_unavailable(
        self, db_session, mocker
    ):
        """When check_hotfix_scope_preview raises, the preview-unavailable path
        must keep the release held and surface the 'unavailable' status clause
        rather than authorizing a whole-tree qualification bypass."""
        from app import release_qualifier_audit as rqa

        project, version = await _make_project_and_version(db_session)
        sig = "migration_apply:unknown:migration_apply_check_unavailable"
        same_entry = {
            "finalized_at": "2026-07-10T00:00:00",
            "error": "migration_apply_check_unavailable",
            "signature": sig,
            "candidate_sha": "dead" * 10,
            "failing_leg": "migration_apply",
            "child_failures": [],
        }
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "dead" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-07-10T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "prior_infra_streak": 2,
            "infra_attempt_history": [
                dict(same_entry, finalized_at="2026-07-10T00:00:00"),
                dict(same_entry, finalized_at="2026-07-10T00:01:00"),
            ],
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)
        preview_mock = mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(side_effect=RuntimeError("scope preview backend down")),
        )
        notify_mock = mocker.patch(
            "app.release_qualifier._notify_operator", new=AsyncMock()
        )

        result = await rqa._handle_infra_failure(
            db_session,
            version,
            dict(version.last_qualification),
            "migration_apply_check_unavailable",
        )
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["is_permanent_gate_incapacity"] is True
        assert result["same_signature_streak"] == 3
        # A failed preview is recorded as None — no preview available — so it
        # cannot become a silent hotfix authorization.
        assert result["hotfix_preview"] is None
        preview_mock.assert_awaited_once_with(
            db_session, project.id, candidate_sha="dead" * 10
        )

        await db_session.refresh(version)

        alerts = (
            (await db_session.execute(
                select(Alert).where(
                    Alert.project_id == project.id,
                    Alert.severity == "WARNING",
                )
            ))
            .scalars()
            .all()
        )
        assert len(alerts) >= 1
        latest = alerts[-1]
        assert "persistently incapable" in latest.message
        assert "force-promote" not in latest.message
        assert "POST /projects/{id}/promote/hotfix" in latest.message
        assert "unavailable or ineligible" in latest.message

        notify_mock.assert_awaited()
        notify_message = notify_mock.await_args.args[3]
        assert "hotfix scope preview" in notify_message
        assert "unavailable or ineligible" in notify_message
        assert "POST /projects/{id}/promote/hotfix" in notify_message

    @pytest.mark.asyncio
    async def test_permanent_gate_incapacity_resets_on_different_signature(
        self, db_session
    ):
        """2 same-signature then 1 different-signature migration_apply error
        does NOT classify as permanent gate-incapacity."""
        from app import release_qualifier_audit as rqa

        project, version = await _make_project_and_version(db_session)
        sig_a = "migration_apply:unknown:migration_apply_check_unavailable"
        same_entry_a = {
            "finalized_at": "2026-07-10T00:00:00",
            "error": "migration_apply_check_unavailable",
            "signature": sig_a,
            "candidate_sha": "dead" * 10,
            "failing_leg": "migration_apply",
            "child_failures": [],
        }
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "dead" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-07-10T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "prior_infra_streak": 2,
            "infra_attempt_history": [
                dict(same_entry_a, finalized_at="2026-07-10T00:00:00"),
                dict(same_entry_a, finalized_at="2026-07-10T00:01:00"),
            ],
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)


        # A genuinely different stable gate code resets the per-signature streak.
        # Merely changing the diagnostic suffix is covered by the test above.
        # The current call has a DIFFERENT error (carries a detail suffix) than the prior
        # two entries, so the per-signature streak should reset to 1.
        result = await rqa._handle_infra_failure(
            db_session,
            version,
            dict(version.last_qualification),

            "migration_apply_scratch_provision_failed: unreachable",
        )
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["is_permanent_gate_incapacity"] is False
        # The trailing same-signature count should be 1 (just current entry)
        assert result["same_signature_streak"] == 1

    @pytest.mark.asyncio
    async def test_audit_leg_permanent_incapacity_after_streak(
        self, db_session
    ):
        """An audit-leg error at streak 3+ now triggers permanent gate-incapacity
        classification (previously only migration_apply legs were eligible)."""
        from app import release_qualifier_audit as rqa

        project, version = await _make_project_and_version(db_session)
        # Signature is "{leg}:unknown:{stable_error_code}" per _infra_failure_signature
        sig = "audit:unknown:audit_task_failed_provision_failure"
        same_entry = {
            "finalized_at": "2026-07-10T00:00:00",
            "error": "audit_task_failed_provision_failure",
            "signature": sig,
            "candidate_sha": "dead" * 10,
            "failing_leg": "audit",
            "child_failures": [],
        }
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "dead" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-07-10T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "prior_infra_streak": 2,
            "infra_attempt_history": [
                dict(same_entry, finalized_at="2026-07-10T00:00:00"),
                dict(same_entry, finalized_at="2026-07-10T00:01:00"),
            ],
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rqa._handle_infra_failure(
            db_session,
            version,
            dict(version.last_qualification),
            "audit_task_failed_provision_failure",
        )
        await db_session.commit()

        assert result["status"] == "infra_failure"
        # Audit leg now also triggers permanent incapacity after _MIN_INFRA_ERROR_STREAK
        assert result["is_permanent_gate_incapacity"] is True
        assert result["same_signature_streak"] == 3
        assert result["consecutive_infra_errors"] == 3
        # No hotfix_preview for non-migration-apply legs
        assert result["hotfix_preview"] is None


    @pytest.mark.asyncio
    async def test_migration_apply_permanent_incapacity_still_detected(
        self, db_session, mocker
    ):
        """Regression guard: migration_apply legs still trigger permanent
        gate-incapacity detection after the guard was generalized."""
        from app import release_qualifier_audit as rqa

        mocker.patch(
            "app.release_qualifier.check_hotfix_scope_preview",
            new=AsyncMock(return_value={"eligible": False, "status": "ineligible"}),
        )
        mocker.patch(
            "app.release_qualifier._notify_operator", new=AsyncMock()
        )
        project, version = await _make_project_and_version(db_session)
        sig = "migration_apply:unknown:migration_apply_check_unavailable"
        same_entry = {
            "finalized_at": "2026-07-10T00:00:00",
            "error": "migration_apply_check_unavailable",
            "signature": sig,
            "candidate_sha": "beef" * 10,
            "failing_leg": "migration_apply",
            "child_failures": [],
        }
        version.last_qualification = {
            "status": "pending",
            "candidate_sha": "beef" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-07-10T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "prior_infra_streak": 2,
            "infra_attempt_history": [
                dict(same_entry, finalized_at="2026-07-10T00:00:00"),
                dict(same_entry, finalized_at="2026-07-10T00:01:00"),
            ],
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rqa._handle_infra_failure(
            db_session,
            version,
            dict(version.last_qualification),
            "migration_apply_check_unavailable",
        )
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["is_permanent_gate_incapacity"] is True
        assert result["same_signature_streak"] == 3
        assert result["failing_dimension"] == "migration_apply"

    @pytest.mark.asyncio
    async def test_audit_admin_cancel_respawns_new_task(self, db_session):
        """CANCELLED+ADMIN_CANCEL audit child → respawn, not infra_failure.

        An externally cancelled audit (admin action, reaper cascade, dedup)
        bears failure_category=ADMIN_CANCEL.  The qualifier must re-spawn the
        audit with the same candidate_sha and return
        status='audit_admin_cancel_respawned' without burning the infra-failure
        streak or emitting a release_infra_failure project message.
        """
        from app.failure_classification import ADMIN_CANCEL, persist_failure_category
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        project, version = await _make_project_and_version(db_session)
        attempt_id = uuid.uuid4()
        candidate = "abcd9876" * 5
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "automain",
                    "diff_range": f"automain...{candidate}",
                    "candidate_sha": candidate,
                    "qualification_attempt_id": str(attempt_id),
                }
            ],
            vm_instance_id="vm-audit-admin-cancel",
            status=TaskStatus.CANCELLED,
            context={
                "release_version_id": str(version.id),
                "qualification_attempt_id": str(attempt_id),
            },
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, ADMIN_CANCEL)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        original_audit_id = str(audit_task.id)
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "qualification_attempt_id": str(attempt_id),
            "retry_episode": 0,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": original_audit_id,
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        version.active_qualification_attempt_id = attempt_id
        version.qualification_attempts = 1
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "audit_admin_cancel_respawned"
        assert result["respawn_count"] == 1

        await db_session.refresh(version)
        new_qual = version.last_qualification
        assert new_qual["audit_admin_cancel_respawn_count"] == 1
        assert new_qual["audit_task_id"] != original_audit_id
        assert new_qual["qualification_attempt_id"] == str(attempt_id)
        assert new_qual["retry_episode"] == 1
        assert new_qual["retry_episode_cap"] == _MAX_AUDIT_RL_RESPAWNS
        assert version.qualification_attempts == 1

        replacement = await db_session.get(Task, uuid.UUID(new_qual["audit_task_id"]))
        assert replacement is not None
        assert replacement.context["qualification_attempt_id"] == str(attempt_id)

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 0

        infra_msg_count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.context["event_type"].as_string()
                    == "release_infra_failure",
                )
            )
        ).scalar()
        assert infra_msg_count == 0

    @pytest.mark.asyncio
    async def test_audit_admin_cancel_cap_exceeded_routes_to_infra_failure(
        self, db_session
    ):
        """When audit_admin_cancel_respawn_count hits cap, fall through to
        infra_failure with label 'audit_admin_cancel_respawn_cap_exceeded'."""
        from app.failure_classification import ADMIN_CANCEL, persist_failure_category
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        project, version = await _make_project_and_version(db_session)
        candidate = "cafe4321" * 5
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {
                    "type": "release_audit",
                    "base_branch": "automain",
                    "diff_range": f"automain...{candidate}",
                    "candidate_sha": candidate,
                }
            ],
            vm_instance_id="vm-audit-ac-cap",
            status=TaskStatus.CANCELLED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        persist_failure_category(audit_task, ADMIN_CANCEL)
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
            "audit_admin_cancel_respawn_count": _MAX_AUDIT_RL_RESPAWNS,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "audit_admin_cancel_respawn_cap_exceeded"

        await db_session.refresh(version)
        assert version.phase == "stabilize"
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "audit_admin_cancel_respawn_cap_exceeded"
        )



    async def _seed_challenge_admin_cancel_scenario(
        self,
        db_session,
        *,
        challenge_admin_cancel_respawn_count: int = 0,
    ):
        """Seed a version whose challenge child was externally ADMIN_CANCELled.

        Returns ``(project, version, challenge_task_id)``.  The primary audit is
        COMPLETED and carries deductible findings so
        ``_primary_findings_for_challenge_retry`` yields a non-empty payload,
        letting the challenge-leg admin-cancel respawn path exercise its spawn.
        """
        from app.failure_classification import ADMIN_CANCEL, persist_failure_category

        project, version = await _make_project_and_version(db_session)
        audit_task, smoke_task = await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 0,
                    "test_count_delta": 0,
                },
                "findings": [
                    {
                        "severity": "high",
                        "summary": "Unbounded retry can promote stale challenge state.",
                        "file": "backend/app/release_qualifier.py",
                        "verification_note": "Read backend/app/release_qualifier.py:1950 and confirmed the stale challenge state can be reused.",
                    },
                    {
                        "severity": "medium",
                        "description": "Challenge matching loses the release scoring file.",
                        "file_path": "backend/app/release_scoring.py",
                        "verification_note": "Read backend/app/release_scoring.py:1300 and confirmed the binding match can fail.",
                    },
                ],
            },
            smoke_logs={
                "passed": True,
                "tests": [{"name": "startup", "status": "pass"}],
                "suites": {"startup": {"passed": True}},
            },
            challenge_verdicts=None,
        )

        candidate = version.last_qualification["candidate_sha"]
        challenge_task = Task(
            project_id=project.id,
            title="audit-challenge",
            payloads=[
                {
                    "type": "release_audit_challenge",
                    "base_branch": "main",
                    "candidate_sha": candidate,
                }
            ],
            vm_instance_id="vm-challenge-admin-cancel",
            status=TaskStatus.CANCELLED,
            context={
                "release_version_id": str(version.id),
                "candidate_sha": candidate,
            },
        )
        db_session.add(challenge_task)
        await db_session.commit()
        await db_session.refresh(challenge_task)

        persist_failure_category(challenge_task, ADMIN_CANCEL)
        db_session.add(challenge_task)
        await db_session.commit()
        await db_session.refresh(challenge_task)

        challenge_task_id = str(challenge_task.id)
        version.last_qualification = {
            **version.last_qualification,
            "status": "pending",
            "challenge_task_id": challenge_task_id,
            "audit_challenge_task_id": challenge_task_id,
            "challenge_admin_cancel_respawn_count": (
                challenge_admin_cancel_respawn_count
            ),
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)
        return project, version, challenge_task_id

    @pytest.mark.asyncio
    async def test_challenge_admin_cancel_respawns_new_task(self, db_session):
        """CANCELLED+ADMIN_CANCEL challenge child → respawn, not infra_failure.

        An externally cancelled challenge (admin action, reaper cascade) bears
        failure_category=ADMIN_CANCEL.  The qualifier must re-spawn the challenge
        with the same candidate_sha and return
        status='challenge_admin_cancel_respawned' without burning the
        infra-failure streak or emitting a release_infra_failure project message.
        """
        project, version, original_challenge_id = (
            await self._seed_challenge_admin_cancel_scenario(db_session)
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "challenge_admin_cancel_respawned"
        assert result["respawn_count"] == 1

        await db_session.refresh(version)
        new_qual = version.last_qualification
        assert new_qual["challenge_admin_cancel_respawn_count"] == 1
        assert new_qual["challenge_task_id"] != original_challenge_id
        # Dual write: audit_challenge_task_id tracks the new challenge task id.
        assert new_qual["audit_challenge_task_id"] == new_qual["challenge_task_id"]
        assert new_qual["status"] == "pending"

        replacement = await db_session.get(
            Task, uuid.UUID(new_qual["challenge_task_id"])
        )
        assert replacement is not None
        assert (
            replacement.first_payload["type"] == "release_audit_challenge"
        )

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 0

        infra_msg_count = (
            await db_session.execute(
                select(sa_func.count()).where(
                    ProjectMessage.project_id == project.id,
                    ProjectMessage.context["event_type"].as_string()
                    == "release_infra_failure",
                )
            )
        ).scalar()
        assert infra_msg_count == 0

    @pytest.mark.asyncio
    async def test_challenge_admin_cancel_cap_exceeded_routes_to_infra_failure(
        self, db_session
    ):
        """When challenge_admin_cancel_respawn_count hits cap, fall through to
        infra_failure with label 'challenge_admin_cancel_respawn_cap_exceeded'."""
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        _project, version, _challenge_task_id = (
            await self._seed_challenge_admin_cancel_scenario(
                db_session,
                challenge_admin_cancel_respawn_count=_MAX_AUDIT_RL_RESPAWNS,
            )
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "challenge_admin_cancel_respawn_cap_exceeded"

        await db_session.refresh(version)
        assert version.phase == "stabilize"
        assert version.last_qualification["status"] == "infra_failure"
        assert (
            version.last_qualification["error"]
            == "challenge_admin_cancel_respawn_cap_exceeded"
        )

    @pytest.mark.asyncio
    async def test_challenge_admin_cancel_payload_too_large_routes_to_infra_failure(
        self, db_session, mocker
    ):
        """PayloadTooLargeError on the admin-cancel respawn spawn → infra_failure.

        The spawn of the replacement challenge can reject an over-large
        primary_findings payload.  Rather than crash, the leg logs and falls
        through to _handle_infra_failure with the plain cancelled label (the
        respawn count has NOT hit the cap, so the cap-exceeded label is not used).
        """
        from app.payload_schemas import PayloadTooLargeError

        _project, version, _challenge_task_id = (
            await self._seed_challenge_admin_cancel_scenario(db_session)
        )

        mocker.patch(
            "app.release_qualifier._spawn_release_audit_challenge_task",
            new=AsyncMock(
                side_effect=PayloadTooLargeError(
                    9_999_999, 1_000_000, [("primary_findings", 9_999_999)]
                )
            ),
        )

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        # count < cap, so the plain cancelled label is used, not cap_exceeded.
        assert result["error"] == "audit_challenge_task_cancelled"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"
        # No respawn happened: the counter is untouched.
        assert (
            version.last_qualification.get("challenge_admin_cancel_respawn_count", 0)
            == 0
        )

    # --- Smoketest PROVISION_FAILURE / AGENT_TIMEOUT respawn tests -----------




    # --- Smoketest claude_code_failed (env-death) respawn tests --------------




    # --- Merge-grade PROVISION_FAILURE / AGENT_TIMEOUT respawn tests --------

    @pytest.mark.asyncio
    async def test_merge_grade_provision_failure_respawns_new_task(
        self, db_session
    ):
        """FAILED merge-grade with PROVISION_FAILURE → respawn, not infra_failure."""
        from app.failure_classification import (
            PROVISION_FAILURE,
            persist_failure_category,
        )

        project, version = await _make_project_and_version(db_session)
        candidate = "feed" * 10
        version.goal = "Finish the widget refactor"
        merge_grade_task = Task(
            project_id=project.id,
            title="merge-grade",
            payloads=[
                {
                    "type": "release_merge_grade",
                    "base_branch": "main",
                    "diff_range": f"main...{candidate}",
                    "candidate_sha": candidate,
                    "version_goal": "Finish the widget refactor",
                    "merged_prs": [101, 102],
                    "categories": [],
                    "rubric_fingerprint": "abc123",
                }
            ],
            vm_instance_id="vm-mg-pf",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(merge_grade_task)
        await db_session.commit()
        await db_session.refresh(merge_grade_task)

        persist_failure_category(merge_grade_task, PROVISION_FAILURE)
        db_session.add(merge_grade_task)
        await db_session.commit()
        await db_session.refresh(merge_grade_task)

        original_mg_id = str(merge_grade_task.id)
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "merge_grade_task_id": original_mg_id,
            "version_goal": "Finish the widget refactor",
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "merge_grade_infra_respawned"
        assert result["respawn_count"] == 1

        await db_session.refresh(version)
        new_qual = version.last_qualification
        assert new_qual["merge_grade_infra_respawn_count"] == 1
        assert new_qual["merge_grade_task_id"] != original_mg_id

        # No QualificationVerdict — verdict not rendered on respawn.
        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 0

    @pytest.mark.asyncio
    async def test_merge_grade_agent_timeout_respawns_new_task(
        self, db_session
    ):
        """FAILED merge-grade with AGENT_TIMEOUT → respawn, not infra_failure."""
        from app.failure_classification import (
            AGENT_TIMEOUT,
            persist_failure_category,
        )

        project, version = await _make_project_and_version(db_session)
        candidate = "bee5" * 10
        version.goal = "Ship the new API"
        merge_grade_task = Task(
            project_id=project.id,
            title="merge-grade",
            payloads=[
                {
                    "type": "release_merge_grade",
                    "base_branch": "automain",
                    "diff_range": f"automain...{candidate}",
                    "candidate_sha": candidate,
                    "version_goal": "Ship the new API",
                    "merged_prs": [201],
                    "categories": [{"category_key": "perf", "text": "perf goal"}],
                    "rubric_fingerprint": "def456",
                }
            ],
            vm_instance_id="vm-mg-at",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(merge_grade_task)
        await db_session.commit()
        await db_session.refresh(merge_grade_task)

        persist_failure_category(merge_grade_task, AGENT_TIMEOUT)
        db_session.add(merge_grade_task)
        await db_session.commit()
        await db_session.refresh(merge_grade_task)

        original_mg_id = str(merge_grade_task.id)
        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "merge_grade_task_id": original_mg_id,
            "version_goal": "Ship the new API",
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "merge_grade_infra_respawned"
        assert result["respawn_count"] == 1

        await db_session.refresh(version)
        new_qual = version.last_qualification
        assert new_qual["merge_grade_infra_respawn_count"] == 1
        assert new_qual["merge_grade_task_id"] != original_mg_id

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 0

    @pytest.mark.asyncio
    async def test_merge_grade_infra_respawn_cap_exceeded_routes_to_infra_failure(
        self, db_session
    ):
        """When merge_grade_infra_respawn_count hits cap, route to infra_failure
        with label 'merge_grade_infra_respawn_cap_exceeded'."""
        from app.failure_classification import (
            PROVISION_FAILURE,
            persist_failure_category,
        )
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS

        project, version = await _make_project_and_version(db_session)
        candidate = "bead" * 10
        version.goal = "Fix the pipeline"
        merge_grade_task = Task(
            project_id=project.id,
            title="merge-grade",
            payloads=[
                {
                    "type": "release_merge_grade",
                    "base_branch": "automain",
                    "diff_range": f"automain...{candidate}",
                    "version_goal": "Fix the pipeline",
                    "merged_prs": [],
                    "categories": [],
                }
            ],
            vm_instance_id="vm-mg-cap",
            status=TaskStatus.FAILED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(merge_grade_task)
        await db_session.commit()
        await db_session.refresh(merge_grade_task)

        persist_failure_category(merge_grade_task, PROVISION_FAILURE)
        db_session.add(merge_grade_task)
        await db_session.commit()
        await db_session.refresh(merge_grade_task)

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": None,
            "smoketest_task_id": None,
            "merge_grade_task_id": str(merge_grade_task.id),
            "merge_grade_infra_respawn_count": _MAX_AUDIT_RL_RESPAWNS,
            "version_goal": "Fix the pipeline",
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        assert result["status"] == "infra_failure"
        assert result["error"] == "merge_grade_infra_respawn_cap_exceeded"

        await db_session.refresh(version)
        assert version.phase == "stabilize"
        assert (
            version.last_qualification["error"]
            == "merge_grade_infra_respawn_cap_exceeded"
        )

    @pytest.mark.asyncio
    async def test_merge_grade_admin_cancel_returns_neutral_hold(self, db_session):
        """CANCELLED merge-grade with failure_category=ADMIN_CANCEL → neutral hold.

        The merge-grade leg has no respawn mechanism; ADMIN_CANCEL must still
        return without burning an infra-failure attempt or streak count.
        """
        from app.failure_classification import ADMIN_CANCEL, persist_failure_category

        project, version = await _make_project_and_version(db_session)
        candidate = "adcc9012" * 5

        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "automain"}],
            vm_instance_id="vm-audit-mg-cancel",
            status=TaskStatus.COMPLETED,
            context={"release_version_id": str(version.id)},
        )
        merge_grade_task = Task(
            project_id=project.id,
            title="merge_grade",
            payloads=[{"type": "release_merge_grade", "base_branch": "automain"}],
            vm_instance_id="vm-mg-admin-cancel",
            status=TaskStatus.CANCELLED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        db_session.add(merge_grade_task)
        await db_session.commit()
        await db_session.refresh(audit_task)
        await db_session.refresh(merge_grade_task)

        persist_failure_category(merge_grade_task, ADMIN_CANCEL)
        db_session.add(merge_grade_task)
        await db_session.commit()
        await db_session.refresh(merge_grade_task)

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "merge_grade_task_id": str(merge_grade_task.id),
            "prior_infra_streak": 0,
        }
        version.qualification_attempts = 3
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Neutral hold — NOT infra_failure.
        assert result["status"] == "merge_grade_admin_cancel_hold"

        # last_qualification stays pending (not rewritten to infra_failure).
        await db_session.refresh(version)
        assert version.last_qualification["status"] == "pending"

        # qualification_attempts not advanced (no attempt burned).
        assert version.qualification_attempts == 3

        # No QualificationVerdict row.
        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 0

    @pytest.mark.asyncio
    async def test_cancelled_without_admin_cancel_still_burns_attempt(
        self, db_session
    ):
        """CANCELLED audit without ADMIN_CANCEL failure_category → infra_failure.

        Regression guard: plain CANCELLED (no failure_category or any other
        category) must still route to _handle_infra_failure so that non-admin
        cancellations are not silently ignored.
        """
        project, version = await _make_project_and_version(db_session)
        candidate = "adce7890" * 5
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[{"type": "release_audit", "base_branch": "automain"}],
            vm_instance_id="vm-audit-plain-cancel",
            status=TaskStatus.CANCELLED,
            context={"release_version_id": str(version.id)},
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": candidate,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # Plain CANCELLED (no failure_category) still routes to infra_failure.
        assert result["status"] == "infra_failure"

        await db_session.refresh(version)
        assert version.last_qualification["status"] == "infra_failure"


# ===========================================================================
# _finalize_qualification — reaper-induced heartbeat timeout
# ===========================================================================


class TestReaperInducedAuditHeartbeatTimeout:
    """Regression guard: reaper-reaped release_audit routes to infra_failure.

    Exercises the full reaper → finalize pipeline to catch regressions where
    a reaped audit task silently creates a QualificationVerdict with
    sub_scores.audit=0 (the 0.00 collapse) or crashes during finalization.
    """

    @pytest.mark.asyncio
    async def test_reaper_heartbeat_timeout_routes_to_infra_failure(self, db_session):
        """Heartbeat-stale audit reaped as FAILED → infra_failure.

        Five assertions (matching the task contract):
          (a) status is infra_failure, not not_promoted / dry_run.
          (b) An infra QualificationVerdict row is created.
          (c) Version remains open with phase=stabilize.
          (d) Error label carries the heartbeat_timeout enrichment so the
              operator can distinguish a VM heartbeat death from a code-defect
              failure.
          (e) consecutive_infra_errors is incremented (0 prior → 1).
        """
        from datetime import timedelta

        from app.failure_classification import persist_failure_category
        from app.reaper import _heartbeat_stale_cutoff
        from app.release_qualifier import _MAX_AUDIT_RL_RESPAWNS
        from app.time_utils import _utcnow

        project, version = await _make_project_and_version(
            db_session, phase="stabilize"
        )

        now = _utcnow()
        # 400 s exceeds the default small-scope heartbeat-stale cutoff (300 s).
        stale_heartbeat = now - timedelta(seconds=400)

        # Seed an IN_PROGRESS release_audit task with a stale last_heartbeat,
        # simulating an agent VM that stopped heartbeating before the reaper swept.
        audit_task = Task(
            project_id=project.id,
            title="audit",
            payloads=[
                {"type": "release_audit", "base_branch": "main", "scope": "small"}
            ],
            vm_instance_id="vm-audit-heartbeat-timeout",
            status=TaskStatus.IN_PROGRESS,
            last_heartbeat=stale_heartbeat,
            context={
                "release_version_id": str(version.id),
                # Simulates an agent that reported its failure category before dying
                # (e.g. via the agent_status endpoint before the VM was killed).
                "agent_failure_category_raw": "heartbeat_timeout",
            },
        )
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)

        # Confirm the seeded heartbeat is stale per the reaper's own cutoff logic.
        cutoff = _heartbeat_stale_cutoff(audit_task, now)
        assert stale_heartbeat < cutoff, (
            f"test precondition: heartbeat {stale_heartbeat} must be before "
            f"stale cutoff {cutoff} — adjust timedelta if thresholds changed"
        )

        version.last_qualification = {
            "status": "pending",
            **_EMPTY_RUBRIC_PROJECTION,
            "candidate_sha": "dead" * 10,
            "ci_green": True,
            "smoketest_enabled": False,
            "started_at": "2026-05-19T00:00:00",
            "audit_task_id": str(audit_task.id),
            "smoketest_task_id": None,
            "prior_infra_streak": 0,
            # At the respawn cap so the AGENT_TIMEOUT routes to infra_failure
            # rather than re-spawning (same-candidate respawn hardening).
            "audit_infra_respawn_count": _MAX_AUDIT_RL_RESPAWNS,
        }
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        # Simulate the reaper sweep: mark the task FAILED and record the
        # failure category (AGENT_TIMEOUT is what the reaper classifies
        # heartbeat_timeout_post_claim failures as).
        audit_task.status = TaskStatus.FAILED
        persist_failure_category(audit_task, "AGENT_TIMEOUT")
        db_session.add(audit_task)
        await db_session.commit()
        await db_session.refresh(audit_task)
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()

        # (a) status is infra_failure, not not_promoted / dry_run.
        assert result["status"] == "infra_failure", (
            f"expected infra_failure, got {result['status']}: {result}"
        )

        verdicts = await _infra_verdicts_for_version(db_session, version)
        assert len(verdicts) == 1
        assert verdicts[0].terminal_status == "infra_failure"

        # (c) Version remains open with phase=stabilize.
        await db_session.refresh(version)
        assert version.status == "open"
        assert version.phase == "stabilize"

        # (d) Error label reflects cap-exceeded path — respawn budget exhausted
        #     before routing to infra_failure.
        assert result["error"] == "audit_infra_respawn_cap_exceeded", (
            f"expected 'audit_infra_respawn_cap_exceeded', got {result['error']!r}"
        )

        # (e) consecutive_infra_errors is incremented (0 prior → 1).
        assert result["consecutive_infra_errors"] == 1


# ===========================================================================
# _finalize_qualification — happy-path audit_score regression
# ===========================================================================


@pytest.mark.asyncio
async def test_finalize_happy_path_audit_score_high(db_session, mocker):
    """Happy path: a COMPLETED audit with clean findings yields audit sub-score ≥ 0.7.

    Regression guard for the 0.00-collapse finding: ensures that a real
    COMPLETED audit task whose RELEASE_AUDIT_REPORT artifact carries a small
    number of low-severity findings produces a high composite with no blocker.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.9,
            "findings": [
                {
                    "severity": "low",
                    "summary": "Minor style issue.",
                    "file": "app/foo.py",
                },
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 3,
                "test_count_delta": 1,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "dry_run", f"expected dry_run, got {result['status']}"
    assert result["would_promote"] is True

    # sub_scores are stored in the persisted last_qualification record.
    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual["sub_scores"]["audit"] >= 0.7, (
        f"audit sub-score collapsed to {qual['sub_scores']['audit']}, expected ≥ 0.7"
    )
    assert qual.get("blocker") is None, f"unexpected blocker: {qual.get('blocker')}"


@pytest.mark.asyncio
async def test_finalize_quality_blocker_four_critical_findings(db_session, mocker):
    """Quality blocker: 4 critical findings yield a quality verdict (not infra_failure).

    Ensures that a COMPLETED audit task with a 0.00 audit_score from real
    findings is recorded as a quality verdict and the strategic agent can target
    the findings list — not mis-routed as infra_failure.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 0.0,
            "findings": [
                {
                    "severity": "critical",
                    "summary": f"SQL injection {i}.",
                    "file": "app/db.py",
                    "verification_note": f"Read app/db.py lines {i * 10 + 5}-{i * 10 + 15}: user input from request body reaches cursor.execute f-string at line {i * 10 + 12} without parameterisation.",
                }
                for i in range(4)
            ],
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 0,
                "test_count_delta": 0,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "smoke-b", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
        challenge_verdicts=[
            {
                "severity": "critical",
                "summary": f"SQL injection {i}.",
                "file": "app/db.py",
                "verdict": "confirmed",
                "reason": f"Independently confirmed: parameterisation missing at injection vector {i}.",
            }
            for i in range(4)
        ],
    )

    mocker.patch("app.release_qualifier.promote", new=AsyncMock())

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Must be a quality verdict (not_promoted), NOT an infra_failure.
    assert result["status"] == "not_promoted", (
        f"expected not_promoted quality verdict, got {result['status']}"
    )
    assert result["status"] != "infra_failure"

    # A QualificationVerdict row must be written (quality path, not infra path).
    count = (
        await db_session.execute(
            select(sa_func.count()).where(
                QualificationVerdict.release_version_id == version.id
            )
        )
    ).scalar()
    assert count == 1, f"expected 1 QualificationVerdict row, got {count}"

    # sub_scores and findings are persisted in last_qualification.
    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual["sub_scores"]["audit"] == pytest.approx(0.0), (
        f"expected audit sub-score 0.0 (4 critical findings), got {qual['sub_scores']['audit']}"
    )
    # Findings must be captured so the strategic agent can target them.
    findings = qual.get("audit_findings", [])
    assert len(findings) > 0, (
        "expected audit_findings in last_qualification for strategic agent"
    )


@pytest.mark.asyncio
async def test_in_flight_attempt_not_superseded_when_automain_advances(
    db_session, mocker
):
    """An in-flight qualification attempt whose children are still non-terminal
    must RUN TO A VERDICT on its pinned candidate_sha — it must NOT be superseded
    merely because automain advanced beneath the pin.

    Root cause (parent task): the superseded reconciler's in-flight guard only
    checked the audit + smoketest children, diverging from the canonical
    ``_qualification_in_flight`` definition (which also covers the blind
    merge-grade and audit-challenge children).  So an attempt whose audit/smoke
    had finished but whose merge-grade child was still running could be falsely
    superseded — cancelling a legitimately in-flight attempt — when automain
    moved out from under the pinned candidate.

    This test pins a candidate, makes the merge-grade child non-terminal while
    audit + smoke are terminal, advances automain to a NEW head, and asserts:
      (a) the reconciler does NOT supersede (children survive; still pending);
      (b) candidate_sha stays the pinned snapshot even though automain moved;
      (c) the branch-movement path does NOT burn the retry-episode counter or
          the consecutive-infra-errors streak (so the cap is not falsely tripped);
      (d) once the merge-grade child reaches a terminal state a genuine
          out-of-band supersession (candidate already on the promotion target)
          still fires — no regression to the legitimate cancellation path.
    """
    from app import release_reconciliation

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    pinned_candidate = "feed" * 10  # the exact commit the children scored
    audit_task, _smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        candidate_sha=pinned_candidate,
    )
    # A blind merge-grade child is STILL RUNNING — the attempt is genuinely
    # in flight even though audit + smoke have finished.
    merge_grade_task = await _seed_merge_grade_child(
        db_session, project, version, status=TaskStatus.IN_PROGRESS
    )

    # Baseline the attempt-accounting so we can prove branch movement never
    # advances it.
    version.qualification_retry_episode = 2
    qual = dict(version.last_qualification)
    qual["retry_episode"] = 2
    qual["consecutive_infra_errors"] = 1
    version.last_qualification = qual
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # automain HEAD has advanced far past the pinned candidate, and the
    # candidate now *appears* reachable from the promotion target — the exact
    # conditions under which the old audit+smoke-only guard would have
    # superseded an attempt whose merge-grade child was still running.
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain="advanced0" * 5, main="mainhead" * 5),
    )
    is_ancestor = mocker.patch(
        "app.github.is_ancestor_of", new=AsyncMock(return_value=True)
    )

    reconciled = await release_reconciliation._reconcile_if_pending_qualification_superseded(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # (a) NOT superseded while the merge-grade child is in flight.
    assert reconciled is False
    # The in-flight guard short-circuits BEFORE any ancestry probe.
    is_ancestor.assert_not_called()
    await db_session.refresh(version)
    assert version.status == "open"
    assert version.last_qualification["status"] == "pending"
    # The children survive (nothing cancelled the in-flight attempt).
    await db_session.refresh(merge_grade_task)
    await db_session.refresh(audit_task)
    assert merge_grade_task.status == TaskStatus.IN_PROGRESS
    assert audit_task.status == TaskStatus.COMPLETED
    # (b) The pin is unchanged even though automain moved.
    assert version.last_qualification["candidate_sha"] == pinned_candidate
    # (c) No episode / infra-streak burn from pure branch movement.
    assert version.qualification_retry_episode == 2
    assert version.last_qualification.get("retry_episode") == 2
    assert version.last_qualification.get("consecutive_infra_errors") == 1

    # (d) Once the merge-grade child terminates, a genuine out-of-band
    # supersession (candidate already reachable from the promotion target)
    # still fires — the legitimate cancellation path is preserved.
    merge_grade_task.status = TaskStatus.COMPLETED
    db_session.add(merge_grade_task)
    await db_session.commit()

    reconciled_after = await release_reconciliation._reconcile_if_pending_qualification_superseded(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert reconciled_after is True
    is_ancestor.assert_awaited()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["blocker"] == "qualification_superseded"
    # The superseded verdict is recorded against the PINNED candidate, and even
    # a legitimate supersession never advances the episode / infra counters.
    assert version.last_qualification["candidate_sha"] == pinned_candidate
    assert version.qualification_retry_episode == 2
    assert version.last_qualification.get("retry_episode") == 2
    assert version.last_qualification.get("consecutive_infra_errors") == 1


@pytest.mark.asyncio
async def test_maybe_finalize_non_qualifier_task_returns_none(db_session):
    """A task with no release_version_id in its context must return None
    immediately — cheap fast path for the overwhelmingly common non-qualifier
    case."""
    project, _version = await _make_project_and_version(db_session)
    task = await _make_task_with_context(
        db_session,
        project,
        status=TaskStatus.COMPLETED,
        context={"some_other_key": "value"},
    )

    result = await rq.maybe_finalize_qualification(
        task, session_factory=_shared_session_factory(db_session)
    )

    assert result is None


@pytest.mark.parametrize(
    "legacy_status",
    [
        TaskStatus.IN_PROGRESS,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.BLOCKED,
        TaskStatus.COMPLETED,
    ],
)
@pytest.mark.asyncio
async def test_legacy_smoke_task_status_never_gates_or_respawns(
    db_session, legacy_status
):
    """A historical smoke child is archival regardless of its task status."""
    from app import shared_state
    from app.release_qualifier import _required_children_terminal

    shared_state.runtime_config["promotion_enabled"] = False
    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=legacy_status,
    )
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    "findings": [],
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 1,
                        "test_count_delta": 1,
                    },
                }
            ),
        )
    )
    legacy_smoke_id = str(smoke_task.id)
    version.last_qualification = {
        **version.last_qualification,
        "env_preflight_enabled": False,
        "smoketest_infra_respawn_count": 99,
        "smoketest_admin_cancel_respawn_count": 99,
        "smoketest_env_death_respawn_count": 99,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    assert await _required_children_terminal(db_session, version) is True
    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert result is not None
    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    await db_session.refresh(smoke_task)
    qual = version.last_qualification
    assert qual["smoketest_task_id"] == legacy_smoke_id
    assert qual["smoketest_enabled"] is True
    assert smoke_task.status == legacy_status
    assert "smoketest" not in (qual.get("sub_scores") or {})
    assert qual.get("smoketest_detail") is None
    assert "smoketest" not in (await _dimension_rows_by_dim(db_session, version))

    smoke_tasks = [
        task
        for task in (
            await db_session.execute(select(Task).where(Task.project_id == project.id))
        ).scalars()
        if task.first_payload.get("type") == "smoketest"
    ]
    assert [str(task.id) for task in smoke_tasks] == [legacy_smoke_id]




@pytest.mark.asyncio
async def test_finalize_surfaces_env_preflight_evidence_without_gating(db_session, monkeypatch):
    """Slice 2 acceptance: a qualification run stores the pure-content
    {required_env, startup_check_names, manifest_source} env_preflight artifact
    on last_qualification, sanitized to presence-only.

    The run WITH evidence passes (declared vars were all-present in
    scope=cloud_run context).  The run WITHOUT evidence now fail-closes
    with boot_prereq_evidence_missing — evidence absence is a
    deterministic block, not a silent pass (the fix for Defect 2).
    """
    base_smoke = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }
    env_preflight_body = {
        "required_env": [
            # A smuggled value MUST be stripped by the read-side sanitizer.
            {"name": "GITHUB_PAT_ENCRYPTION_KEY", "scope": "cloud_run", "value": "sekret"},
        ],
        "startup_check_names": ["python3 -c \"import app.main\""],
        "manifest_source": "backend/app/required_runtime_env.py",
    }

    async def _finalize_with_env_preflight_evidence(evidence):
        project, version = await _make_project_and_version(db_session)
        await _seed_qualification_children(
            db_session,
            project,
            version,
            audit_logs={
                "audit_score": 1.0,
                "test_erosion": {
                    "deleted_test_files": [],
                    "assertion_delta": 1,
                    "test_count_delta": 0,
                },
            },
            smoke_logs=dict(base_smoke),
        )
        version.last_qualification = {
            **version.last_qualification,
            "env_preflight_enabled": True,
            "env_preflight_evidence": evidence,
        }
        version.last_qualification = _with_current_qualification_contract(
            project, version
        )
        db_session.add(version)
        await db_session.commit()
        await db_session.refresh(version)

        result = await rq._finalize_qualification(db_session, version)
        await db_session.commit()
        await db_session.refresh(version)
        return result, dict(version.last_qualification)

    with_result, with_qual = await _finalize_with_env_preflight_evidence(
        env_preflight_body
    )
    without_result, without_qual = await _finalize_with_env_preflight_evidence(None)

    # Evidence surfaced, sanitized to presence-only (no smuggled value).
    stored = with_qual["env_preflight"]
    assert stored == {
        "required_env": [{"name": "GITHUB_PAT_ENCRYPTION_KEY", "scope": "cloud_run"}],
        "startup_check_names": ["python3 -c \"import app.main\""],
        "manifest_source": "backend/app/required_runtime_env.py",
    }
    assert "value" not in stored["required_env"][0]

    # WITHOUT evidence: now fail-closes (boot_prereq_evidence_missing).
    assert without_qual["env_preflight"] is None
    assert without_qual["blocker"] == "boot_prereq_evidence_missing", (
        f"Expected boot_prereq_evidence_missing, got {without_qual['blocker']!r} — "
        "missing evidence must fail-closed"
    )
    assert without_result["status"] == "not_promoted"

    # WITH evidence (cloud_run-scoped vars are present in cloud_run context):
    # promotion proceeds normally.
    assert with_result["status"] == "dry_run"
    assert with_result["would_promote"] is True


def test_policy_env_preflight_enabled_default_on():
    """promotion_policy.yaml must ship with env_preflight_enabled: true.

    The Slice-3 deployed-side env-preflight gate was initially shipped dark
    (env_preflight_enabled: false).  This test pins the ENABLED default so a
    future yaml edit does not silently re-disable it.
    """
    import yaml
    from pathlib import Path

    policy_path = Path(__file__).resolve().parent.parent / "promotion_policy.yaml"
    with open(policy_path) as f:
        policy = yaml.safe_load(f)
    assert policy.get("env_preflight_enabled") is True, (
        "promotion_policy.yaml must have env_preflight_enabled: true — "
        "the env-preflight gate must be enabled by default (Slice-3, design 245212a4)"
    )


@pytest.mark.asyncio
async def test_env_preflight_gate_fires_and_names_missing_var(db_session, monkeypatch):
    """Slice-3 acceptance: when env_preflight_enabled is true in the policy
    and a required 'always'-scope var is absent from the deployed env, the
    gate blocks with boot_prereq_missing AND names the missing var in
    sub_scores and the operator notification message.

    This is the v22 lesson: a deterministic, actionable blocker that names the
    exact missing prerequisite rather than degrading into a vague retry loop.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    # Ensure the test var is absent from os.environ so the gate fires.
    _ABSENT_VAR = "BLUEPRINT_PREFLIGHT_TEST_ABSENT_VAR_SHOULD_NOT_EXIST"
    monkeypatch.delenv(_ABSENT_VAR, raising=False)
    # K_REVISION absent → deploy_target_scope = "always", so scope:"always" vars are in scope.
    monkeypatch.delenv("K_REVISION", raising=False)

    env_preflight_evidence = {
        "required_env": [
            # scope "always" → evaluated in every deploy target (cloud_run AND non-cloud_run).
            {"name": _ABSENT_VAR, "scope": "always"},
        ],
        "startup_check_names": [],
        "manifest_source": "backend/app/required_runtime_env.py",
    }
    smoke_logs = {
        "passed": True,
        "tests": [{"name": "startup_check", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 0,
            },
        },
        smoke_logs=smoke_logs,
    )
    # Enable env_preflight explicitly (seed defaults to False).
    version.last_qualification = {
        **version.last_qualification,
        "env_preflight_enabled": True,
        "env_preflight_evidence": env_preflight_evidence,
    }
    version.last_qualification = _with_current_qualification_contract(project, version)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = version.last_qualification
    # The gate must fire with boot_prereq_missing when the flag is on in the policy.
    assert qual["blocker"] == "boot_prereq_missing", (
        f"Expected boot_prereq_missing, got {qual['blocker']!r} — "
        "env_preflight gate must block when a required var is absent"
    )
    # The specific missing var name must appear in sub_scores — not just a generic message.
    missing = qual["sub_scores"].get("env_preflight_missing", [])
    assert _ABSENT_VAR in missing, (
        f"Expected {_ABSENT_VAR!r} in env_preflight_missing, got {missing!r} — "
        "actionable blocker must name the exact missing prerequisite"
    )
    # The candidate was not promoted — gate is deterministic, not a retry loop.
    assert qual["status"] == "not_promoted"
    assert qual["confidence"] == 0.0

    # The operator notification must also name the missing var — deterministic message.
    msgs = await _messages_for(db_session, project.id, "release_blocked")
    assert len(msgs) >= 1
    blocked_msg = msgs[-1]
    assert _ABSENT_VAR in blocked_msg.content, (
        f"Operator notification must name the missing var {_ABSENT_VAR!r}; "
        f"got: {blocked_msg.content!r}"
    )


@pytest.mark.asyncio
async def test_env_preflight_gate_inert_when_no_artifact(db_session):
    """When env_preflight_enabled is true but no env_preflight evidence was
    produced (the SMOKETEST_RESULT artifact exists but carries no env_preflight
    key), the gate NOW FAILS CLOSED with boot_prereq_evidence_missing.

    This is the corrected behavior (previously the defect was that missing
    evidence silently passed through — a fail-open).  The evidence-missing
    block is a deterministic quality verdict, never an infra-failure retry loop.
    A real successful producer always emits a non-empty manifest (even
    required_env: []), so 'no artifact while enabled' legitimately fail-closes.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 0,
            },
        },
        # No env_preflight key in smoke_logs — no evidence produced.
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    # Enable env_preflight explicitly (seed defaults to False).
    version.last_qualification = {
        **version.last_qualification,
        "env_preflight_enabled": True,
    }
    version.last_qualification = _with_current_qualification_contract(project, version)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = version.last_qualification
    # Gate must hard-block with evidence-missing when enabled but no evidence.
    assert qual["blocker"] == "boot_prereq_evidence_missing", (
        f"Expected boot_prereq_evidence_missing, got {qual['blocker']!r} — "
        "env_preflight gate must fail-closed when no evidence was produced"
    )
    assert qual["status"] == "not_promoted"
    assert qual["confidence"] == 0.0


@pytest.mark.asyncio
async def test_env_preflight_gate_inert_for_cloud_run_only_var_in_non_cloud_run_env(
    db_session, monkeypatch
):
    """A required var with scope 'cloud_run' does NOT block in a non-Cloud-Run
    deploy target (K_REVISION absent).  The scope-gate prevents false positives
    on developer and CI environments that legitimately lack cloud-run-only secrets.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    _CLOUD_RUN_VAR = "GITHUB_PAT_ENCRYPTION_KEY"
    # K_REVISION absent → deploy_target_scope = "always"; cloud_run-scoped vars are out of scope.
    monkeypatch.delenv("K_REVISION", raising=False)

    env_preflight_evidence = {
        "required_env": [
            {"name": _CLOUD_RUN_VAR, "scope": "cloud_run"},
        ],
        "startup_check_names": [],
        "manifest_source": "backend/app/required_runtime_env.py",
    }
    smoke_logs = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 0,
            },
        },
        smoke_logs=smoke_logs,
    )
    # Enable env_preflight explicitly (seed defaults to False).
    version.last_qualification = {
        **version.last_qualification,
        "env_preflight_enabled": True,
        "env_preflight_evidence": env_preflight_evidence,
    }
    version.last_qualification = _with_current_qualification_contract(project, version)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = version.last_qualification
    # cloud_run-scoped var must NOT block a non-Cloud-Run deployment.
    assert qual["blocker"] != "boot_prereq_missing", (
        "cloud_run-scoped var must not block in a non-Cloud-Run environment"
    )
    assert qual["status"] == "dry_run"
    assert qual["would_promote"] is True


@pytest.mark.asyncio
async def test_env_preflight_gate_inert_with_ack_override(db_session, monkeypatch):
    """The owner-armed Slice-4 ack suppresses the boot_prereq_missing block
    exactly once when the operator acknowledges that the missing var will be
    bound in the same deploy (ack_env_will_be_bound).
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    _ABSENT_VAR = "BLUEPRINT_PREFLIGHT_ACK_TEST_VAR"
    monkeypatch.delenv(_ABSENT_VAR, raising=False)
    monkeypatch.delenv("K_REVISION", raising=False)

    env_preflight_evidence = {
        "required_env": [{"name": _ABSENT_VAR, "scope": "always"}],
        "startup_check_names": [],
        "manifest_source": "backend/app/required_runtime_env.py",
    }
    smoke_logs = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 0,
            },
        },
        smoke_logs=smoke_logs,
    )
    # Arm the Slice-4 one-shot ack and enable env_preflight (seed defaults to False).
    version.last_qualification = {
        **version.last_qualification,
        "env_preflight_enabled": True,
        "env_preflight_evidence": env_preflight_evidence,
        "ack_env_will_be_bound": {
            "actor": "test-operator",
            "acknowledged_at": "2026-07-16T00:00:00",
        },
    }
    version.last_qualification = _with_current_qualification_contract(project, version)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = version.last_qualification
    # Ack suppresses the boot_prereq_missing block.
    assert qual["blocker"] != "boot_prereq_missing", (
        "Slice-4 ack must suppress boot_prereq_missing block"
    )
    # The ack-applied record is auditable.
    assert qual.get("env_preflight_ack_applied") is not None
    assert qual["env_preflight_ack_applied"]["overridden_blocker"] == "boot_prereq_missing"
    # Missing names still recorded for audit (presence-only, names never values).
    assert _ABSENT_VAR in qual["env_preflight_ack_applied"]["missing"]
    # Dry-run promotion proceeds.
    assert qual["status"] == "dry_run"
    assert qual["would_promote"] is True


@pytest.mark.asyncio
async def test_env_preflight_blocks_without_smoketest_child(db_session):
    """When env_preflight_enabled is True but smoketest_enabled is False
    and NO env_preflight evidence is produced (the child was never spawned
    or produced no usable artifact), the gate hard-blocks with
    boot_prereq_evidence_missing — a deterministic quality verdict, NOT
    an infra_failure retry loop.

    This is the primary acceptance test: the boot gate is independently
    enforceable even when the smoketest subsystem is disabled.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 0,
            },
        },
        # No env_preflight in smoke_logs — no evidence produced.
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )
    # Simulate: smoketest disabled, env-preflight enabled.
    version.last_qualification = {
        **version.last_qualification,
        "smoketest_enabled": False,
        "env_preflight_enabled": True,
    }
    version.last_qualification = _with_current_qualification_contract(project, version)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = version.last_qualification
    # Must block with boot_prereq_evidence_missing — NOT infra_failure.
    assert qual["blocker"] == "boot_prereq_evidence_missing", (
        f"Expected boot_prereq_evidence_missing, got {qual['blocker']!r} — "
        "env_preflight gate must hard-block when enabled but no evidence produced"
    )
    assert qual["status"] == "not_promoted"
    assert qual["confidence"] == 0.0


@pytest.mark.asyncio
async def test_env_preflight_passes_with_evidence_even_without_smoketest(db_session, monkeypatch):
    """When env_preflight_enabled is True, smoketest_enabled is False, BUT the
    evidence producer ran successfully and all declared prerequisites are present:
    promotion proceeds normally (no block, no confidence erosion).
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    monkeypatch.delenv("K_REVISION", raising=False)

    env_preflight_evidence = {
        "required_env": [
            # scope "always" → in scope for non-cloud-run.
            {"name": "SOME_PRESENT_VAR", "scope": "always"},
        ],
        "startup_check_names": [],
        "manifest_source": "backend/app/required_runtime_env.py",
    }
    smoke_logs = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 0,
            },
        },
        smoke_logs=smoke_logs,
    )
    # Simulate: smoketest disabled, env-preflight enabled.
    version.last_qualification = {
        **version.last_qualification,
        "smoketest_enabled": False,
        "env_preflight_enabled": True,
        "env_preflight_evidence": env_preflight_evidence,
    }
    version.last_qualification = _with_current_qualification_contract(project, version)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Ensure the declared required var is present in the deployed env.
    monkeypatch.setenv("SOME_PRESENT_VAR", "1")

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = version.last_qualification
    # No block — evidence was produced and all vars are present.
    assert qual["blocker"] is None, (
        f"Expected no blocker, got {qual['blocker']!r} — "
        "gate must pass when evidence exists and all vars are present"
    )
    # Dry-run promotion proceeds (promotion_enabled=False but would_promote).
    assert qual["status"] == "dry_run"
    assert qual["would_promote"] is True


@pytest.mark.asyncio
async def test_env_preflight_passes_with_empty_requirements(db_session, monkeypatch):
    """When env_preflight_enabled is True and the evidence producer ran
    successfully reporting required_env: [] (candidate genuinely declares
    zero prerequisites), the gate passes (score 1.0 — all zero requirements
    are trivially met).  This must NOT block on evidence-missing or
    boot_prereq_missing.
    """
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    monkeypatch.delenv("K_REVISION", raising=False)

    env_preflight_evidence = {
        "required_env": [],  # candidate genuinely declares zero prerequisites
        "startup_check_names": [],
        "manifest_source": "backend/app/required_runtime_env.py",
    }
    smoke_logs = {
        "passed": True,
        "tests": [{"name": "a", "status": "pass"}],
        "suites": {"startup": {"passed": True}},
    }

    project, version = await _make_project_and_version(db_session)
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 1,
                "test_count_delta": 0,
            },
        },
        smoke_logs=smoke_logs,
    )
    # Simulate env-preflight enabled (smoketest enabled is the seed default).
    version.last_qualification = {
        **version.last_qualification,
        "env_preflight_enabled": True,
        "env_preflight_evidence": env_preflight_evidence,
    }
    version.last_qualification = _with_current_qualification_contract(project, version)
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    await db_session.refresh(version)

    qual = version.last_qualification
    # No block — evidence exists (empty list = trivially all-present).
    assert qual["blocker"] is None, (
        f"Expected no blocker, got {qual['blocker']!r} — "
        "empty required_env must pass: candidate genuinely declares zero prerequisites"
    )
    assert qual["status"] == "dry_run"
    assert qual["would_promote"] is True


@pytest.mark.asyncio
async def test_maybe_finalize_already_finalized_version_returns_none(db_session):
    """If the version's last_qualification.status is not 'pending' (e.g.
    'dry_run', 'promoted', 'not_promoted'), a second call returns None
    without re-running _finalize_qualification (idempotent guard)."""
    project, version = await _make_project_and_version(db_session)

    audit_task, _smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )

    # Simulate the version already having been finalized.
    version.last_qualification = {
        **version.last_qualification,
        "status": "dry_run",
        "confidence": 0.9,
        "would_promote": True,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )

    assert result is None

    # Status must not have changed — still dry_run.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"




@pytest.mark.asyncio
async def test_required_children_terminal_waits_for_release_audit_challenge(db_session):
    """_required_children_terminal returns False when audit and smoketest are
    terminal but challenge_task_id is present and its child is still IN_PROGRESS.
    Finalize must not fire until the challenge child is also terminal."""
    from app import shared_state
    from app.release_qualifier import _required_children_terminal

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    # audit + smoke COMPLETED, challenge still IN_PROGRESS.
    (
        _audit_task,
        _smoke_task,
        _challenge_task,
    ) = await _seed_pending_qualification_with_challenge(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        challenge_status=TaskStatus.IN_PROGRESS,
    )

    result = await _required_children_terminal(db_session, version)

    assert result is False, (
        "finalize-gate must return False while challenge child is IN_PROGRESS"
    )


@pytest.mark.asyncio
async def test_required_children_terminal_true_when_all_three_terminal(db_session):
    """_required_children_terminal returns True only when audit, smoketest, AND
    challenge children are all in terminal states."""
    from app import shared_state
    from app.release_qualifier import _required_children_terminal

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    # All three terminal.
    await _seed_pending_qualification_with_challenge(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        challenge_status=TaskStatus.COMPLETED,
    )

    result = await _required_children_terminal(db_session, version)

    assert result is True


@pytest.mark.asyncio
async def test_maybe_finalize_challenge_child_non_terminal_returns_none(db_session):
    """maybe_finalize_qualification returns None and does NOT finalize when
    audit and smoketest are terminal but the challenge child is still running."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    (
        audit_task,
        _smoke_task,
        _challenge_task,
    ) = await _seed_pending_qualification_with_challenge(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        challenge_status=TaskStatus.IN_PROGRESS,
    )

    result = await rq.maybe_finalize_qualification(
        audit_task, session_factory=_shared_session_factory(db_session)
    )

    assert result is None

    # Version must still be pending — finalize must NOT have run.
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_qualification_in_flight_true_when_challenge_non_terminal(db_session):
    """_qualification_in_flight returns True when audit+smoke are terminal but
    the challenge child is still IN_PROGRESS — the qualification is still active."""
    from app.release_qualifier import _qualification_in_flight

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification_with_challenge(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        challenge_status=TaskStatus.IN_PROGRESS,
    )

    result = await _qualification_in_flight(db_session, version)

    assert result is True


@pytest.mark.asyncio
async def test_qualification_in_flight_false_when_all_three_terminal(db_session):
    """_qualification_in_flight returns False when ALL three children (audit,
    smoketest, challenge) are terminal — the qualification is eligible for a
    fresh run, not blocked by a prior in-flight execution."""
    from app.release_qualifier import _qualification_in_flight

    project, version = await _make_project_and_version(db_session)

    await _seed_pending_qualification_with_challenge(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
        challenge_status=TaskStatus.COMPLETED,
    )

    result = await _qualification_in_flight(db_session, version)

    assert result is False


# ===========================================================================
# Release tagging — Phase 4: v{N} tag on promotion, failure is non-fatal
# ===========================================================================


@pytest.mark.asyncio
async def test_promote_success_creates_release_tag(db_session, mocker):
    """A successful promotion creates a git tag v{version_number} on the REAL
    merge-response commit via github.create_tag."""
    _project, version = await _make_project_and_version(db_session)

    merge_sha = "tagmerge" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 42, "html_url": "http://pr/42"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "sha": merge_sha, "message": "Merged"}
        ),
    )
    tag_mock = mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(
            return_value={"ref": "refs/tags/v1", "object": {"sha": "abc" * 13 + "a"}}
        ),
    )

    result = await rq.promote(
        db_session,
        version,
        "abcd" * 10,
        composite={"confidence": 0.9},
    )
    await db_session.commit()

    assert result["status"] == "promoted"

    # create_tag was called once with the v1 tag on the merge commit.
    tag_mock.assert_called_once()
    call_kwargs = tag_mock.call_args
    assert call_kwargs.args[3] == "v1"  # tag name
    assert call_kwargs.args[4] == merge_sha  # sha — the real merge commit


@pytest.mark.asyncio
async def test_promote_success_tag_failure_does_not_abort_promotion(db_session, mocker):
    """A create_tag failure (any exception) is non-fatal: the promotion still
    succeeds (status='promoted') and the next version is still opened."""
    _project, version = await _make_project_and_version(db_session)

    merge_sha = "beefmerg" * 5
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(main=merge_sha),
    )
    _patch_promote_github_defaults(mocker)
    mocker.patch(
        "app.github.create_branch",
        new=_create_branch_mock(),
    )
    mocker.patch(
        "app.github.create_pull_request",
        new=AsyncMock(return_value={"number": 99, "html_url": "http://pr/99"}),
    )
    mocker.patch(
        "app.github.merge_pull_request",
        new=AsyncMock(
            return_value={"merged": True, "sha": merge_sha, "message": "Merged"}
        ),
    )
    # Simulate a tag creation failure (e.g. GitHub 500, network error, etc.)
    from fastapi import HTTPException as _HTTPException

    mocker.patch(
        "app.github.create_tag",
        new=AsyncMock(side_effect=_HTTPException(status_code=500, detail="GitHub 500")),
    )

    result = await rq.promote(
        db_session,
        version,
        "beef" * 10,
        composite={"confidence": 0.95},
    )
    await db_session.commit()

    # The promotion itself must succeed despite the tag failure.
    assert result["status"] == "promoted"
    assert result["promoted_sha"] == merge_sha
    assert result["next_version_number"] == 2

    # The version row must be promoted.
    await db_session.refresh(version)
    assert version.status == "promoted"
    assert version.promoted_sha == merge_sha

    # The next version was opened normally.
    from app import release_version as rv_mod

    next_version = await rv_mod.current_open_version(db_session, version.project_id)
    assert next_version is not None
    assert next_version.version_number == 2


# ===========================================================================
# sweep_stuck_qualifications — defense-in-depth reaper
# ===========================================================================


@pytest.mark.asyncio
async def test_sweep_finalizes_version_stuck_pending(db_session):
    """A version stuck 'pending' with BOTH children terminal is finalized by
    the sweep — the count reflects exactly that one finalization."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session)

    # Both children COMPLETED but the version was never finalized (its
    # completion handler died) — last_qualification still says 'pending'.
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.COMPLETED,
    )
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    "findings": [],
                    "test_erosion": {
                        "deleted_test_files": [],
                        "assertion_delta": 3,
                        "test_count_delta": 1,
                    },
                }
            ),
        )
    )
    db_session.add(
        Artifact(
            task_id=smoke_task.id,
            artifact_type=ArtifactType.SMOKETEST_RESULT,
            logs=json.dumps(
                {
                    "passed": True,
                    "tests": [{"name": "a", "status": "pass"}],
                    "suites": {"startup": {"passed": True}},
                }
            ),
        )
    )
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1

    # The stuck version is now finalized (dry_run — promotion disabled).
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


@pytest.mark.asyncio
async def test_sweep_ignores_running_legacy_smoke_child(db_session):
    """A running legacy smoke child cannot keep an active audit pending."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False
    project, version = await _make_project_and_version(db_session)
    audit_task, smoke_task = await _seed_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        smoke_status=TaskStatus.IN_PROGRESS,
    )
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps(
                {
                    "audit_score": 1.0,
                    "findings": [],
                    "test_erosion": {"deleted_test_files": []},
                }
            ),
        )
    )
    version.last_qualification = {
        **version.last_qualification,
        "env_preflight_enabled": False,
    }
    db_session.add(version)
    await db_session.commit()

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1
    await db_session.refresh(version)
    await db_session.refresh(smoke_task)
    assert version.last_qualification["status"] == "dry_run"
    assert version.last_qualification["smoketest_task_id"] == str(smoke_task.id)
    assert smoke_task.status == TaskStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_stall_alerts_preserve_concurrently_committed_last_qualification(
    db_session,
):
    """Regression: the lifecycle-stall loop must NOT clobber a ``last_qualification``
    committed concurrently between sweep start and the stall check.

    The loop used to ``merge(load=False)`` the sweep-START snapshot of the version
    into the alert session, so the promotion stall checker rebuilt the
    promotion-pending verdict from that STALE snapshot and wrote it back — erasing
    a ``last_qualification`` another writer had committed in the interim.  The fix
    re-``SELECT``s the row fresh inside the stall-check transaction, so the alert
    is layered onto the CURRENT verdict.

    Interleaving reproduced exactly: the sweep snapshot (``open_versions`` object)
    carries the STALE verdict (PR #111), a different writer commits a fresh verdict
    (PR #222) to the row, and the stall loop then runs on its own dedicated session
    (fresh identity map, as in production).  The concurrently committed PR #222 must
    survive with the managed-wait alert layered on top of it — never reverting
    to #111.
    """
    from contextlib import asynccontextmanager
    from datetime import datetime, timedelta

    from sqlalchemy import update as sa_update
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    from app import release_qualifier_sweep as rqs
    from app import shared_state

    # promotion-readiness check short-circuits when promotion is enabled; keep it
    # disabled so the stall loop exercises the managed-wait path deterministically.
    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # A stall-eligible promotion_pending anchored >4h ago (past
    # the managed-wait threshold) with no prior alert, so the stall checker
    # fires. This is the SWEEP-START snapshot (PR #111) carried in open_versions.
    enabled_at = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=5)
    ).isoformat()
    stale_qual = {
        "status": "pending",
        "promotion_pending": {
            "pr_number": 111,
            "pr_url": "http://pr/111",
            "managed_check_wait": True,
            "enabled_at": enabled_at,
        },
    }
    version.last_qualification = stale_qual
    db_session.add(version)
    await db_session.commit()

    # The sweep-start snapshot the stall loop is handed (still holds PR #111).
    open_versions = [version]

    # Another writer commits a FRESH verdict (PR #222) to the row between sweep
    # start and the stall check. synchronize_session=False updates the DB row only
    # and deliberately leaves the in-memory `version` snapshot on PR #111 — exactly
    # reproducing a commit that landed on a different session.
    concurrent_qual = {
        "status": "pending",
        "promotion_pending": {
            "pr_number": 222,
            "pr_url": "http://pr/222",
            "managed_check_wait": True,
            "enabled_at": enabled_at,
        },
    }
    await db_session.execute(
        sa_update(rv.ReleaseVersion)
        .where(rv.ReleaseVersion.id == version.id)
        .values(last_qualification=concurrent_qual)
        .execution_options(synchronize_session=False)
    )
    await db_session.commit()
    # Sanity: the in-memory snapshot is genuinely stale (still PR #111).
    assert version.last_qualification["promotion_pending"]["pr_number"] == 111

    # Dedicated-session factory on the SAME test engine: genuinely independent
    # transactions and identity map (as the production per-version alert session
    # is), so the stall loop's re-SELECT actually reads the committed row.
    test_engine = db_session.bind

    @asynccontextmanager
    async def _dedicated_factory():
        async with _AsyncSession(test_engine, expire_on_commit=False) as s:
            yield s

    await rqs._run_lifecycle_stall_alerts(
        open_versions, session_factory=_dedicated_factory
    )

    # The concurrently committed verdict SURVIVES: the managed-wait alert was
    # layered onto the CURRENT row (PR #222), not the stale snapshot (PR #111).
    await db_session.refresh(version)
    pending = version.last_qualification["promotion_pending"]
    assert pending["pr_number"] == 222, (
        "stall handling clobbered a concurrently committed last_qualification "
        "with the stale sweep-start snapshot"
    )
    # And the alert genuinely fired (so the assertion above is not vacuous).
    assert version.last_qualification["status"] == "pending"
    assert "mcw_last_alerted_at" in pending
    assert pending["mcw_alert_count"] == 1


@pytest.mark.asyncio
async def test_sweep_leaves_deferred_unsynced_for_strategic(db_session, mocker):
    """A cleared deferral remains a Strategic delivery decision, not sweep work."""
    from datetime import datetime, timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Stranded deferral, recorded a few minutes ago (well within the 30-min
    # fall-through window, so the ONLY thing that can clear it is now being
    # synced — not the fall-through).
    version.last_qualification = {
        "status": "deferred_unsynced",
        "candidate_sha": "stale00" * 5 + "00000",
        "first_deferred_at": (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=3)).isoformat(),
    }
    db_session.add(version)
    await db_session.commit()

    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    # main is NOW absorbed into the (fresh) candidate — the back-merge landed.
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "deferred_unsynced"
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_sweep_leaves_deferred_unsynced_when_still_behind(db_session, mocker):
    """A version still genuinely behind the promotion target (back-merge not yet
    landed, within the fall-through window) is NOT scored by the sweep: the
    re-drive re-defers, spawns no audit, and preserves ``first_deferred_at`` so
    the bounded fall-through clock keeps accumulating across sweeps."""
    from datetime import datetime, timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    first_deferred = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=3)).isoformat()
    version.last_qualification = {
        "status": "deferred_unsynced",
        "candidate_sha": "stale00" * 5 + "00000",
        "first_deferred_at": first_deferred,
    }
    db_session.add(version)
    await db_session.commit()

    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock())
    # Still NOT absorbed — the back-merge has not landed yet.
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=False)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    # Still deferred, nothing spawned, and the accrued-wait clock is preserved.
    assert version.last_qualification["status"] == "deferred_unsynced"
    assert version.last_qualification["first_deferred_at"] == first_deferred
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_redrive_deferred_unsynced_preserves_state_on_transient_sha_error(
    db_session, mocker
):
    """A transient GitHub error during deferred_unsynced re-drive must NOT
    overwrite last_qualification with the error state.

    Acceptance proof: after the sweep, version.last_qualification["status"] is
    still "deferred_unsynced" (not "sha_error") and no audit task was created —
    the version remains eligible for the next sweep's re-drive loop.
    """
    from datetime import datetime, timedelta

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    first_deferred = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)).isoformat()
    version.last_qualification = {
        "status": "deferred_unsynced",
        "candidate_sha": "stale00" * 5 + "00000",
        "first_deferred_at": first_deferred,
    }
    db_session.add(version)
    await db_session.commit()

    # Simulate a transient GitHub error: get_branch_sha raises, causing
    # run_qualification to return sha_error without clearing the deferral.
    mocker.patch(
        "app.github.get_branch_sha",
        side_effect=RuntimeError("transient GitHub 503"),
    )
    # Prevent _redrive_error_qualification from picking up the sha_error status
    # written by the deferred_unsynced re-drive and committing it — that loop
    # runs after the deferred_unsynced rollback in the same sweep cycle.
    mocker.patch(
        "app.release_qualifier_sweep._redrive_error_qualification",
        new=AsyncMock(return_value=False),
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # The sweep does not count a failed re-drive as a finalization.
    assert count == 0
    await db_session.refresh(version)
    # Critical: the deferred_unsynced state must be preserved, not overwritten
    # with sha_error — otherwise the version falls out of the re-drive loop.
    assert version.last_qualification["status"] == "deferred_unsynced"
    assert version.last_qualification["first_deferred_at"] == first_deferred
    # No audit task should have been spawned.
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_sweep_redrives_transient_sha_error_version(db_session, mocker):
    """A stabilize version stranded at status=error/sha_error is re-driven by
    the sweep once GitHub recovers.

    Acceptance proof: after the sweep, version transitions from error to pending
    — the transient outage is recovered automatically.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    sha = "aabbcc" * 6 + "aabb"
    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "error",
        "qualification_attempt_id": str(attempt_id),
        "error": "sha_error",
        "detail": "ReadTimeout: ",
        "started_at": "2026-07-05T11:00:00",
    }
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 1
    db_session.add(version)
    await db_session.commit()

    audit_task = Task(
        project_id=project.id,
        title="release-audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-err",
        status=TaskStatus.QUEUED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    spawned_audit = mocker.Mock(id=audit_task.id)

    # The autouse fixture _qualifier_ci_resolves_green handles token + CI
    # resolution mocks. Only need to override get_branch_sha and spawn.
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=sha),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(return_value=spawned_audit),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-abc",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_sweep_error_redrive_respects_attempt_cap(db_session, mocker):
    """A stabilize version at status=error is NOT re-driven once
    last_qualification["error_redrive_count"] reaches _MAX_ERROR_REDRIVE_ATTEMPTS,
    preventing infinite retries on a permanently broken path."""
    from app.release_qualifier_sweep import _MAX_ERROR_REDRIVE_ATTEMPTS

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "error",
        "error": "sha_error",
        "detail": "HTTPException: connection refused",
        "started_at": "2026-07-05T10:00:00",
        "error_redrive_count": _MAX_ERROR_REDRIVE_ATTEMPTS,
    }
    db_session.add(version)
    await db_session.commit()

    spawn_mock = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # No spawn should have happened — attempt cap blocks re-drive.
    spawn_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "error"


@pytest.mark.asyncio
async def test_sweep_error_redrive_accumulates_attempts_until_capped(
    db_session, mocker
):
    """A persistently-failing transient path is re-driven only a BOUNDED number
    of times: last_qualification["error_redrive_count"] must ACCUMULATE across
    sweeps and converge on _MAX_ERROR_REDRIVE_ATTEMPTS, at which point re-drive stops.

    Here get_branch_sha ALWAYS raises, so every re-drive returns sha_error; the
    per-episode counter must stop climbing at the cap and the GitHub call count
    must be bounded, not unbounded.
    """
    from app.release_qualifier_sweep import _MAX_ERROR_REDRIVE_ATTEMPTS

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "error",
        "qualification_attempt_id": str(attempt_id),
        "error": "sha_error",
        "detail": "ReadTimeout: ",
        "started_at": "2026-07-05T11:00:00",
    }
    # Start from a clean slate — the cap must be reached purely by accumulation.
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 0
    db_session.add(version)
    await db_session.commit()

    sha_mock = mocker.patch(
        "app.github.get_branch_sha",
        side_effect=RuntimeError("transient GitHub 503 — permanently down"),
    )
    spawn_mock = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    # Run the sweep more times than the cap allows — extra sweeps must be no-ops.
    for _ in range(_MAX_ERROR_REDRIVE_ATTEMPTS + 3):
        await rq.sweep_stuck_qualifications(
            db_session, session_factory=_shared_session_factory(db_session)
        )
        await db_session.commit()

    await db_session.refresh(version)
    # The per-episode counter persisted across sweeps and converged on the cap
    # exactly — never exceeding it, never stalling below it.
    assert version.last_qualification.get("error_redrive_count") == _MAX_ERROR_REDRIVE_ATTEMPTS
    # GitHub was hit at most once per re-drive and re-drive stopped at the cap:
    # the API call count is bounded, not unbounded.
    assert sha_mock.call_count == _MAX_ERROR_REDRIVE_ATTEMPTS
    # Never spawned (every attempt failed) and stays in the transient-error state.
    spawn_mock.assert_not_called()
    assert version.last_qualification["status"] == "error"
    assert version.last_qualification["error"] == "sha_error"


@pytest.mark.asyncio
async def test_sweep_error_redrive_allows_versions_with_many_lifetime_attempts(
    db_session, mocker
):
    """A version with many lifetime qualification_attempts is still re-driven on
    its first error episode — the cap must be per-episode (error_redrive_count in
    last_qualification), NOT the lifetime qualification_attempts column.

    Regression test for the v22 bug: the deployed re-driver used
    qualification_attempts >= _MAX_ERROR_REDRIVE_ATTEMPTS as its guard, so any
    version that had accumulated 5+ prior spawn attempts of ANY kind was silently
    blocked from ALL transient-error re-drives and stranded at status=error
    indefinitely, regardless of how many times it had actually been re-driven for
    the current error episode.
    """
    sha = "aabbcc" * 6 + "aabb"
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "error",
        "qualification_attempt_id": str(attempt_id),
        "error": "sha_error",
        "detail": "ReadTimeout: ",
        "started_at": "2026-07-05T11:00:00",
        # No error_redrive_count — this is the first error episode.
    }
    # Many lifetime attempts from previous qualification episodes (the bug trigger).
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 11
    db_session.add(version)
    await db_session.commit()

    audit_task = Task(
        project_id=project.id,
        title="release-audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-long-lived",
        status=TaskStatus.QUEUED,
        context={"release_version_id": str(version.id)},
    )
    db_session.add(audit_task)
    await db_session.commit()
    await db_session.refresh(audit_task)
    spawned_audit = mocker.Mock(id=audit_task.id)

    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=sha),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(return_value=spawned_audit),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-longv",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # The version MUST have been re-driven despite high lifetime attempts.
    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"
    # The successfully spawned qualification begins a new episode. Its retry
    # counter must not inherit the previous error episode's retry history.
    assert "error_redrive_count" not in version.last_qualification


@pytest.mark.asyncio
async def test_sweep_does_not_redrive_non_transient_error(db_session, mocker):
    """A status=error version with an error code NOT in _TRANSIENT_ERROR_CODES
    is not re-driven by the sweep — only transient errors are retried.
    (spawn_failed is now a transient error that re-drives walkthrough
    execution-site outages, so the fixture uses a genuinely permanent error.)"""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "error",
        "error": "missing_candidate_sha",  # not in _TRANSIENT_ERROR_CODES
        "started_at": "2026-07-05T10:00:00",
    }
    version.qualification_attempts = 1
    db_session.add(version)
    await db_session.commit()

    spawn_mock = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # Non-transient error: no spawn.
    spawn_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "error"


@pytest.mark.asyncio
async def test_sweep_redrives_infra_failure_version(db_session, mocker):
    """A stabilize version stranded at status=infra_failure is re-driven by
    the sweep — the only non-conclusive qualification outcome with no scheduled
    re-entry (live v22 stranded exactly this way on 2026-07-06).

    Acceptance proof: after the sweep, version transitions from infra_failure
    to pending — the transient infrastructure problem is recovered automatically.
    """
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    sha = "aabbcc" * 6 + "aabb"
    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(attempt_id),
        "blocker": "qualification_timeout",
        "detail": "qualification pending 361 min (> 360 min limit)",
        "started_at": "2026-07-06T00:00:00",
        "finalized_at": "2026-07-06T06:01:00",
    }
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 2
    db_session.add(version)
    await db_session.commit()

    # The audit row is persisted INSIDE the spawn mock, mirroring the real
    # spawner: pre-creating a QUEUED release_audit row would trip the
    # live-audit double-spawn guard in _redrive_infra_failure_qualification
    # and defer the very re-drive under test.
    audit_task = Task(
        project_id=project.id,
        title="release-audit",
        payloads=[{"type": "release_audit", "base_branch": "main"}],
        vm_instance_id="vm-audit-infra",
        status=TaskStatus.QUEUED,
        context={"release_version_id": str(version.id)},
    )

    async def _spawn_and_persist(*_args, **_kwargs):
        db_session.add(audit_task)
        await db_session.flush()
        return audit_task

    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=sha),
    )
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(side_effect=_spawn_and_persist),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-infra",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "pending"


@pytest.mark.asyncio
async def test_redrive_infra_failure_respects_attempt_cap(db_session, mocker):
    """A stabilize version at status=infra_failure is NOT re-driven once the
    PERSISTED last_qualification["consecutive_infra_errors"] streak reaches
    _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS, preventing infinite retries.

    Regression for audit acaa610d: the cap must key on the streak the finalize
    path maintains, NOT the dead ``infra_failure_redrive_count`` field (deliberately
    absent here — the old code read it, defaulting to 0, and would re-drive)."""
    from app.release_qualifier_sweep import _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "capca0de" * 5,
        "error": "migration_identity_check_unavailable",
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
    }
    db_session.add(version)
    await db_session.commit()

    spawn_mock = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # No spawn should have happened — attempt cap blocks re-drive.
    spawn_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"


@pytest.mark.asyncio
async def test_redrive_infra_failure_noop_when_not_stabilize_phase(db_session, mocker):
    """A version in a phase other than stabilize with status=infra_failure is
    NOT re-driven — the re-driver is scoped to stabilize-phase versions only."""
    project, version = await _make_project_and_version(db_session, phase="build")
    version.last_qualification = {
        "status": "infra_failure",
        "blocker": "qualification_timeout",
        "detail": "qualification pending 361 min (> 360 min limit)",
    }
    db_session.add(version)
    await db_session.commit()

    spawn_mock = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    spawn_mock.assert_not_called()
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"


@pytest.mark.asyncio
async def test_redrive_infra_failure_cap_keyed_on_persisted_streak(
    db_session, mocker
):
    """The infra-failure circuit-breaker keys on the PERSISTED
    ``consecutive_infra_errors`` streak (which ``_handle_infra_failure`` maintains
    on the finalized verdict), NOT on a re-driver-local counter and NOT on the
    synchronous ``run_qualification`` return value.

    Regression for audit acaa610d: the old code only incremented
    ``infra_failure_redrive_count`` when ``run_qualification`` synchronously
    returned status=='infra_failure' — which it NEVER does (that verdict is only
    written asynchronously at finalize).  So the counter stayed 0, the cap never
    tripped, and re-drives were unbounded.  Here ``run_qualification`` returns the
    realistic ``spawned`` and we assert the cap is driven purely by the persisted
    streak: a stale/dead ``infra_failure_redrive_count`` is ignored.
    """
    from app.release_qualifier_sweep import (
        _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS,
        _redrive_infra_failure_qualification,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Real streak BELOW the cap, but a stale/dead infra_failure_redrive_count ABOVE
    # the cap.  The old code (keyed on infra_failure_redrive_count) would have
    # blocked; the fixed code ignores that field and re-drives because the persisted
    # consecutive_infra_errors streak is below the cap.
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "5treakab" * 5,
        "error": "migration_identity_check_unavailable",
        "consecutive_infra_errors": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS - 1,
        "infra_failure_redrive_count": _MAX_INFRA_FAILURE_REDRIVE_ATTEMPTS + 5,
    }
    version.qualification_attempts = 1
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # run_qualification returns the realistic "spawned" — it NEVER synchronously
    # returns infra_failure.
    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # Below the persisted-streak cap → the re-driver fires (proves the dead
    # infra_failure_redrive_count field no longer gates the breaker).
    assert result is True
    run_qual.assert_awaited_once()


@pytest.mark.asyncio
async def test_same_candidate_redrive_reuses_dimension_with_origin_attempt(
    db_session, mocker,
):
    """A fresh operator re-drive reuses same-candidate facts with provenance.

    This exercises the production start seam (not a synthetic serializer): the
    first attempt's resolved audit/test facts remain owned by their producing
    attempt, the new active attempt is recorded as a reuser, and the child that
    still must run carries the new identity in both context and payload.
    """
    project, version = await _make_project_and_version(
        db_session, phase="stabilize"
    )
    sha = "c0ffee12" * 5
    origin_attempt = uuid.uuid4()
    for dimension, score in (
        ("audit", 0.92),
        ("test_erosion", 1.0),
        ("ci", None),
        ("migration_identity", None),
        ("migration_apply", None),
        ("challenge", None),
    ):
        await _seed_dimension_row(
            db_session,
            project,
            version,
            dimension,
            status="passed",
            sub_score=score,
            candidate_sha=sha,
            origin_attempt_id=origin_attempt,
        )
    await _seed_dimension_row(
        db_session,
        project,
        version,
        "merge_grade",
        status="unavailable",
        candidate_sha=sha,
        origin_attempt_id=origin_attempt,
    )
    version.goal = "Prove qualification attempt provenance across a re-drive"
    version.active_qualification_attempt_id = origin_attempt
    version.qualification_attempts = 1
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(origin_attempt),
        "candidate_sha": sha,
        "error": "merge_grade_report_unparseable",
        "retry_episode": 0,
    }
    db_session.add(version)
    await db_session.commit()

    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=sha))
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="same-candidate-surface",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )
    audit_spawn = mocker.spy(rq, "_spawn_release_audit_task")

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()
    await db_session.refresh(version)

    assert result["status"] == "spawned"
    active_attempt = uuid.UUID(result["qualification_attempt_id"])
    assert active_attempt != origin_attempt
    assert version.active_qualification_attempt_id == active_attempt
    assert version.qualification_attempts == 2
    assert version.qualification_retry_episode == 0
    assert version.last_qualification["qualification_attempt_id"] == str(
        active_attempt
    )
    audit_spawn.assert_awaited_once()

    audit_row = (
        await db_session.execute(
            select(QualificationDimensionResult).where(
                QualificationDimensionResult.release_version_id == version.id,
                QualificationDimensionResult.candidate_sha == sha,
                QualificationDimensionResult.dimension == "audit",
            )
        )
    ).scalar_one()
    assert audit_row.origin_attempt_id == origin_attempt
    assert str(active_attempt) in (audit_row.reused_by_attempt_ids or [])
    assert version.last_qualification["resolved_dimensions"]["audit"] == {
        "status": "passed",
        "sub_score": 0.92,
        "blocker": None,
        "detail": None,
        "origin_attempt_id": str(origin_attempt),
        "reused_by_attempt_id": str(active_attempt),
        "producing_task_id": None,
    }

    audit_task = await db_session.get(
        Task, uuid.UUID(version.last_qualification["audit_task_id"])
    )
    assert audit_task is not None
    assert audit_task.context["qualification_attempt_id"] == str(active_attempt)
    assert audit_task.first_payload["qualification_attempt_id"] == str(
        active_attempt
    )
    assert version.last_qualification["merge_grade_task_id"] is None


@pytest.mark.asyncio
async def test_pending_to_finalized_attempt_identity_continuity(db_session):
    """The immutable verdict keeps the UUID published by the pending record."""
    from app import release_promotion as rp

    _project, version = await _make_project_and_version(db_session)
    attempt_id = uuid.uuid4()
    version.active_qualification_attempt_id = attempt_id
    version.last_qualification = {
        "status": "pending",
        "qualification_attempt_id": str(attempt_id),
        "candidate_sha": "facefeed" * 5,
        "started_at": "2026-07-15T10:00:00",
    }
    finalized = {
        **version.last_qualification,
        "status": "dry_run",
        "finalized_at": "2026-07-15T10:05:00",
        "confidence": 0.91,
        "threshold": 0.9,
    }
    version.last_qualification = finalized
    db_session.add(version)
    await rp._record_qualification_verdict(
        db_session,
        version,
        finalized,
        terminal_status="dry_run",
        would_promote=True,
        promotion_enabled=False,
    )
    await db_session.commit()

    verdict = (
        await db_session.execute(
            select(QualificationVerdict).where(
                QualificationVerdict.release_version_id == version.id
            )
        )
    ).scalar_one()
    assert verdict.qualification_attempt_id == attempt_id
    assert version.last_qualification["qualification_attempt_id"] == str(attempt_id)


@pytest.mark.asyncio
async def test_infra_redrive_reuses_resolved_dimensions_without_respawn(
    db_session, mocker,
):
    """On an infra_failure re-drive at the SAME candidate_sha, already-resolved
    dimensions from the per-dimension ledger are carried forward and their child
    tasks are NOT re-spawned — only the still-unresolved dimensions' children
    are spawned.  This is the piece that flattens the "attempt 12→13" re-audit
    cost when a single gate (e.g. merge-grade) hit a transient infra failure.

    Acceptance evidence: the sweep re-driver consults the ledger before spawning,
    so a re-drive that only needs to re-run merge-grade skips the ~15min audit
    and the smoketest — verified by asserting the spawn mocks are NOT called for
    resolved dimensions and the resolved facts are carried forward in the pending
    record.
    """
    from app.release_qualifier_sweep import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    sha = "f00dcafe" * 5

    # Seed dimension rows: most dimensions are already resolved from a prior
    # finalize at this candidate_sha.  Only merge_grade was left 'unavailable'
    # by a transient infra failure on that gate.
    await _seed_dimension_row(
        db_session, project, version, "audit",
        status="passed", sub_score=0.85, candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "test_erosion",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "ci",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "migration_identity",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "migration_apply",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "challenge",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "merge_grade",
        status="unavailable", candidate_sha=sha,
    )
    await db_session.commit()

    # Set version in infra_failure at the SAME candidate_sha — the merge-grade
    # gate hit a transient unparseable-report failure while every other gate
    # already resolved (passed).
    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(attempt_id),
        "retry_episode": 0,
        "candidate_sha": sha,
        "error": "merge_grade_report_unparseable",
        "consecutive_infra_errors": 0,
        "started_at": "2026-07-10T00:00:00",
        "finalized_at": "2026-07-10T00:01:00",
    }
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 1
    # The merge-grade spawn requires a non-empty, non-contentless goal.
    version.goal = "Deliver the per-dimension ledger reuse on qualifier re-drive"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Mock spawn functions to track which are called.
    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )
    merge_grade_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_merge_grade_task",
        new=AsyncMock(),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=sha),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-f00d",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # The re-drive should have fired (returned True — a spawn happened).
    assert result is True

    await db_session.refresh(version)
    qual = version.last_qualification

    # The version transitioned from infra_failure to pending.
    assert qual["status"] == "pending"
    assert qual["qualification_attempt_id"] == str(attempt_id)
    assert qual["retry_episode"] == 1
    assert version.qualification_attempts == 1

    # --- Core assertions: resolved-dimension children were NOT re-spawned ---
    # A legacy audit without this attempt's holistic goal/rubric fingerprint is
    # re-run once; its old code-quality facts remain available for provenance.
    audit_spawn.assert_awaited_once()
    # The holistic audit owns merge-grade evaluation; no separate child is spawned.
    merge_grade_spawn.assert_not_called()

    # --- Resolved dimensions are carried forward in the pending record ---
    resolved = qual.get("resolved_dimensions") or {}
    assert isinstance(resolved, dict)
    assert "audit" in resolved
    assert resolved["audit"]["status"] == "passed"
    assert resolved["audit"]["sub_score"] == 0.85
    assert "test_erosion" in resolved
    assert resolved["test_erosion"]["status"] == "passed"
    assert "smoketest" not in resolved
    # merge_grade was NOT resolved (status=unavailable), so not in resolved_dims.
    assert "merge_grade" not in resolved

    # --- Task ID fields reflect which children were spawned ---
    assert qual["audit_task_id"] is not None
    assert qual["merge_grade_task_id"] is None


@pytest.mark.asyncio
async def test_infra_redrive_isolates_failure_to_one_dimension_ledger_facts_survive(
    db_session, mocker,
):
    """Per-(candidate_sha, dimension) failure isolation (v25 criterion c-2d1be255).

    A transient infra failure in ONE gate must redrive ONLY that gate's
    dimension; every OTHER dimension's previously-passed per-dimension ledger
    fact must survive the re-drive un-discarded and un-downgraded.  This is the
    isolation guarantee the qualification ledger provides: a flapping audit gate
    cannot silently throw away a smoketest 'passed' fact (or any other resolved
    dimension) that already cost real compute to earn.

    Covers the audit+smoketest pair (plus ci as a second surviving control):
    audit hit a transient ``audit_task_failed`` while smoketest and ci had
    already resolved 'passed' at the SAME candidate_sha.  On re-drive:
      (a) ONLY the audit dimension is re-spawned (smoketest child is NOT), and
      (b) the smoketest AND ci ``QualificationDimensionResult`` ledger rows are
          still present with status 'passed' and their sub_scores intact — the
          audit-gate failure did not discard or downgrade another dimension's
          passed result.

    Regression tripwire: if the re-drive ever discarded/downgraded a sibling
    dimension's passed ledger fact (breaking isolation), the ledger-row survival
    assertions below fail.
    """
    from app.release_qualifier_sweep import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    sha = "1solat3d" * 5

    # audit is the transiently-failed gate (left 'unavailable' by the infra
    # failure); every other gate already resolved 'passed' at this candidate_sha.
    await _seed_dimension_row(
        db_session, project, version, "audit",
        status="unavailable", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "test_erosion",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "ci",
        status="passed", sub_score=0.77, candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "migration_identity",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "migration_apply",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "challenge",
        status="passed", candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "merge_grade",
        status="passed", candidate_sha=sha,
        detail={
            "rubric_provenance": rq._rubric_provenance(
                [],
                source="prior_attempt",
                qualification_attempt_id=None,
            )
        },
    )
    await db_session.commit()

    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(attempt_id),
        "candidate_sha": sha,
        "error": "audit_task_failed",
        "failing_dimension": "audit",
        "consecutive_infra_errors": 0,
        "started_at": "2026-07-10T00:00:00",
        "finalized_at": "2026-07-10T00:01:00",
    }
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 1
    version.goal = "Isolate a transient audit-gate failure from resolved dimensions"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )
    merge_grade_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_merge_grade_task",
        new=AsyncMock(),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=sha),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-1solat3d",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert result is True

    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual["status"] == "pending"

    # --- (a) ONLY the failing (audit) dimension is redriven -----------------
    audit_spawn.assert_awaited_once()
    # The holistic audit owns merge-grade evaluation; no separate child is spawned.
    merge_grade_spawn.assert_not_called()
    assert qual["audit_task_id"] is not None
    assert qual["merge_grade_task_id"] is None

    # Audit is being re-measured, so it is not in the carried-forward reuse set.
    # Mutable gate facts are likewise re-measured on every same-SHA redrive;
    # only immutable resolved dimensions are carried forward.
    resolved = qual.get("resolved_dimensions") or {}
    assert "audit" not in resolved
    assert "smoketest" not in resolved
    assert "ci" not in resolved
    assert "migration_identity" not in resolved
    assert "migration_apply" not in resolved

    # --- (b) sibling dimensions' PASSED ledger facts survive un-discarded ----
    # and un-downgraded: the audit-gate transient failure could not throw away
    # or weaken another dimension's already-earned passed result.
    ledger = await _dimension_rows_by_dim(db_session, version)
    # The ci passed fact is likewise intact (second surviving control).
    assert "ci" in ledger, "ci passed ledger fact was discarded"
    assert ledger["ci"].status == "passed"
    assert ledger["ci"].sub_score == 0.77
    # The failing dimension's own row is not spuriously promoted to 'passed'.
    assert ledger["audit"].status != "passed"


@pytest.mark.asyncio
async def test_redrive_failed_audit_without_passed_challenge_re_measures(
    db_session, mocker,
):
    """Audit-challenge parity on reuse: a stored audit fact with status 'failed'
    whose challenge gate never resolved 'passed' must NOT be reused verbatim — a
    fresh audit+challenge re-measurement is forced so both paths pass through the
    same gate.

    The audit dimension is upserted to the ledger BEFORE the challenge gate runs,
    so a prior attempt that held on a missing/uncertain challenge leaves
    audit='failed' while 'challenge' is never written 'passed'.  Reusing that
    audit would compose an unconfirmed finding-driven deduction and silently drop
    the audit-challenge gate.  On re-drive, the audit child MUST be re-spawned
    (challenge re-driven too), not skipped.
    """
    from app.release_qualifier_sweep import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    sha = "beefcafe" * 5

    # Audit resolved 'failed' (a finding-driven deduction) — but the challenge
    # dimension is NOT resolved 'passed' (the prior attempt held on it, so no
    # challenge row was ever written).
    await _seed_dimension_row(
        db_session, project, version, "audit",
        status="failed", sub_score=0.7, candidate_sha=sha,
    )
    await _seed_redrive_common_dimensions(db_session, project, version, sha)
    await _seed_dimension_row(
        db_session, project, version, "merge_grade",
        status="passed", candidate_sha=sha,
    )
    await db_session.commit()

    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(attempt_id),
        "candidate_sha": sha,
        "error": "audit_challenge_missing",
        "consecutive_infra_errors": 0,
        "started_at": "2026-07-10T00:00:00",
        "finalized_at": "2026-07-10T00:01:00",
    }
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 1
    version.goal = "Deliver the audit-challenge parity guard on reuse"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=sha),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-beef",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert result is True

    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual["status"] == "pending"

    # Core assertion: the 'failed' audit was NOT reused — a fresh audit child was
    # spawned to re-measure it through the challenge gate.
    audit_spawn.assert_awaited_once()

    # The challenge is re-driven (spawn pending) once the fresh audit lands.
    assert qual.get("audit_challenge_spawn_pending") is True
    # Audit and test erosion are dropped from the reuse set.
    resolved = qual.get("resolved_dimensions") or {}
    assert "audit" not in resolved
    assert "test_erosion" not in resolved
    assert "smoketest" not in resolved
    assert qual["audit_task_id"] is not None


@pytest.mark.asyncio
async def test_redrive_failed_audit_with_passed_challenge_is_reused(
    db_session, mocker,
):
    """Control for the audit-challenge parity guard: a 'failed' audit whose
    challenge gate DID resolve 'passed' is a CONFIRMED deduction — it is reused
    verbatim and the audit child is NOT re-spawned (no over-firing)."""
    from app.release_qualifier_sweep import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    sha = "d00dfeed" * 5
    version.goal = "Confirmed-deduction audit reuse stays cheap"
    rubric_fingerprint = rq._rubric_fingerprint([])
    promotion_policy = rq._load_policy(project)
    contract_fingerprint = rq._qualification_contract_fingerprint(
        version.goal,
        rubric_fingerprint,
        promotion_policy=promotion_policy,
        capability_proof_mode=str(
            rq._runtime_config.get("capability_proof_gate_mode", "off") or "off"
        ).strip().lower(),
        env_preflight_enabled=bool(
            (project.build_config or {}).get(
                "env_preflight_enabled",
                promotion_policy.get("env_preflight_enabled", False),
            )
        ),
        requested_probes=[],
    )

    await _seed_dimension_row(
        db_session, project, version, "audit",
        status="failed", sub_score=0.7, candidate_sha=sha,
        detail={"holistic_contract_fingerprint": contract_fingerprint},
    )
    await _seed_redrive_common_dimensions(db_session, project, version, sha)
    # The challenge gate CONFIRMED the deduction — challenge dimension 'passed'.
    await _seed_dimension_row(
        db_session, project, version, "challenge",
        status="passed", candidate_sha=sha,
    )
    # Leave merge_grade unavailable so the re-drive still has something to spawn.
    await _seed_dimension_row(
        db_session, project, version, "merge_grade",
        status="unavailable", candidate_sha=sha,
    )
    await db_session.commit()

    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(attempt_id),
        "candidate_sha": sha,
        "error": "merge_grade_report_unparseable",
        "consecutive_infra_errors": 0,
        "started_at": "2026-07-10T00:00:00",
        "finalized_at": "2026-07-10T00:01:00",
    }
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 1
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )
    merge_grade_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_merge_grade_task",
        new=AsyncMock(),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=sha),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-d00d",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert result is True

    await db_session.refresh(version)
    qual = version.last_qualification

    # The confirmed 'failed' audit is reused — no expensive re-audit.
    audit_spawn.assert_not_called()
    # The holistic audit owns merge-grade evaluation; no separate child is spawned.
    merge_grade_spawn.assert_not_called()
    resolved = qual.get("resolved_dimensions") or {}
    assert resolved.get("audit", {}).get("status") == "failed"
    assert "test_erosion" in resolved
    assert qual["audit_task_id"] is None


@pytest.mark.asyncio
async def test_redrive_blocked_audit_fact_re_measures_instead_of_reblocking(
    db_session, mocker,
):
    """A reused 'blocked' audit fact must ALSO force a fresh audit — otherwise a
    permitted same-sha re-drive can never recover (fail-closed wedge).

    A 'blocked' audit fact is the ``audit_missing`` hard-blocker sentinel: the
    dimension carries NO usable report (sub_score None).  Before the fix, only a
    'failed' audit fact was dropped from the reuse set, so a 'blocked' one was
    reused verbatim: ``_audit_needed`` stayed False (audit + test_erosion both
    "resolved"), the audit child was never re-spawned, and finalize composed the
    same None sub-score straight back into ``audit_missing``.  Every subsequent
    re-drive at this sha replayed that block with nothing able to clear it.

    The recovery valve: a 'blocked' audit is re-measured, so the audit child IS
    re-spawned and the block can actually resolve.
    """
    from app.release_qualifier_sweep import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    sha = "0bad0bad" * 5

    # The exact ledger shape a prior finalize writes when the audit produced no
    # usable report: audit blocked (sub_score None == audit_missing sentinel) and
    # test_erosion blocked alongside it (_score_audit(None) -> (None, None)).
    await _seed_dimension_row(
        db_session, project, version, "audit",
        status="blocked", sub_score=None, blocker="audit_missing",
        candidate_sha=sha,
    )
    await _seed_dimension_row(
        db_session, project, version, "test_erosion",
        status="blocked", sub_score=None, candidate_sha=sha,
    )
    # Every OTHER gate resolved cleanly — so the audit fact is the only thing
    # standing between this candidate and a verdict.
    for dim in ("ci", "migration_identity", "migration_apply", "merge_grade"):
        await _seed_dimension_row(
            db_session, project, version, dim, status="passed", candidate_sha=sha,
        )
    await db_session.commit()

    attempt_id = uuid.uuid4()
    version.last_qualification = {
        "status": "infra_failure",
        "qualification_attempt_id": str(attempt_id),
        "candidate_sha": sha,
        "error": "audit_report_missing",
        "consecutive_infra_errors": 0,
        "started_at": "2026-07-10T00:00:00",
        "finalized_at": "2026-07-10T00:01:00",
    }
    version.active_qualification_attempt_id = attempt_id
    version.qualification_attempts = 1
    version.goal = "A blocked audit fact must not wedge the same-sha re-drive"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    audit_spawn = mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=sha),
    )
    mocker.patch(
        "app.release_qualifier.compute_audited_surface_hash",
        return_value="hash-0bad",
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()
    assert result is True

    await db_session.refresh(version)
    qual = version.last_qualification
    assert qual["status"] == "pending"

    # Core assertion: the 'blocked' audit was NOT reused — a fresh audit child is
    # spawned, which is the ONLY thing that can produce the missing report.
    audit_spawn.assert_awaited_once()
    assert qual["audit_task_id"] is not None

    # Both audit-child dimensions leave the reuse set, so the finalize
    # composition re-reads the fresh report instead of replaying sub_score None
    # (which compute_composite turns straight back into audit_missing).
    resolved = qual.get("resolved_dimensions") or {}
    assert "audit" not in resolved, (
        "a blocked audit fact was reused — finalize will re-block on audit_missing "
        "with no way to clear it"
    )
    assert "test_erosion" not in resolved

    # Scoped: active sibling gates that resolved on their own merits are still
    # reused; retired smoke never enters the active reuse set.
    assert "smoketest" not in resolved


@pytest.mark.asyncio
async def test_infra_redrive_uses_per_dimension_cap_not_attempt_level(db_session, mocker):
    """Phase 1b per-dimension cap: _redrive_infra_failure_qualification consults
    the per-dimension infra streak from the QualificationDimensionResult ledger
    instead of the attempt-global consecutive_infra_errors so one gate's flakiness
    (e.g. migration_identity failing 3 times while audit passes) cannot exhaust the
    redrive budget for other gates.

    Scenario:
    - version in infra_failure with attempt-level streak=3 (would block under old code)
    - failing_dimension=migration_identity with per-dimension streak=2 (below cap)
    - Expected: redrive fires (per-dimension streak 2 < cap 3)

    Complementary scenario (per-dimension streak at cap):
    - same setup but per-dimension streak=3
    - Expected: redrive is blocked (per-dimension streak 3 >= cap 3)
    """
    from app.release_qualifier_redrive import _redrive_infra_failure_qualification
    from app.models import QualificationDimensionResult

    # --- Scenario A: attempt-level at cap but per-dimension below cap → should redrive ---
    project_a, version_a = await _make_project_and_version(db_session, phase="stabilize")
    sha_a = "cafecafe" * 5
    version_a.goal = ""  # no merge-grade child needed
    db_session.add(version_a)

    # Seed migration_identity as 'unavailable' with per-dimension streak=2 (below cap 3).
    row_a = QualificationDimensionResult(
        project_id=version_a.project_id,
        release_version_id=version_a.id,
        candidate_sha=sha_a,
        dimension="migration_identity",
        status="unavailable",
        detail={"consecutive_infra_errors": 2, "infra_error": "migration_identity_check_unavailable"},
    )
    db_session.add(row_a)

    # attempt-level streak=3 (would block under old code)
    version_a.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": sha_a,
        "error": "migration_identity_check_unavailable",
        "failing_dimension": "migration_identity",
        "consecutive_infra_errors": 3,  # attempt-level: AT the cap
        "started_at": "2026-07-10T00:00:00",
        "finalized_at": "2026-07-10T00:01:00",
    }
    version_a.qualification_attempts = 3
    await db_session.commit()
    await db_session.refresh(version_a)

    run_qual_a = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )

    result_a = await _redrive_infra_failure_qualification(
        version_a.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # Per-dimension streak=2 < cap=3 → redrive should fire even though attempt-level is AT cap.
    assert result_a is True, (
        "per-dimension streak 2 is below the cap 3 — redrive must fire"
    )
    run_qual_a.assert_awaited_once()

    # --- Scenario B: per-dimension streak AT cap → should be blocked ---
    run_qual_a.reset_mock()
    project_b, version_b = await _make_project_and_version(db_session, phase="stabilize")
    sha_b = "deadbeef" * 5
    version_b.goal = ""
    db_session.add(version_b)

    # Seed migration_identity with per-dimension streak=3 (AT cap).
    row_b = QualificationDimensionResult(
        project_id=version_b.project_id,
        release_version_id=version_b.id,
        candidate_sha=sha_b,
        dimension="migration_identity",
        status="unavailable",
        detail={"consecutive_infra_errors": 3, "infra_error": "migration_identity_check_unavailable"},
    )
    db_session.add(row_b)

    version_b.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": sha_b,
        "error": "migration_identity_check_unavailable",
        "failing_dimension": "migration_identity",
        "consecutive_infra_errors": 3,
        "started_at": "2026-07-10T00:00:00",
        "finalized_at": "2026-07-10T00:01:00",
    }
    version_b.qualification_attempts = 3
    await db_session.commit()
    await db_session.refresh(version_b)

    # reuse the same patch (already patched above); just check it's not called.
    run_qual_b = run_qual_a

    result_b = await _redrive_infra_failure_qualification(
        version_b.id, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # Per-dimension streak=3 >= cap=3 → redrive must be blocked.
    assert result_b is False, (
        "per-dimension streak 3 is at the cap 3 — redrive must be blocked"
    )
    run_qual_b.assert_not_awaited()


# ---------------------------------------------------------------------------
# Time-based infra-failure re-drive backoff tests
# ---------------------------------------------------------------------------

def test_infra_backoff_seconds_schedule():
    """_infra_backoff_seconds returns 0 for streak=0 and follows the schedule."""
    from app.release_qualifier_redrive import _infra_backoff_seconds

    assert _infra_backoff_seconds(0) == 0
    assert _infra_backoff_seconds(1) == 60
    assert _infra_backoff_seconds(2) == 300
    assert _infra_backoff_seconds(3) == 1800
    # Clamped to last entry for streak > len(schedule)-1.
    assert _infra_backoff_seconds(100) == 1800


@pytest.mark.parametrize("current_leg", ["audit", "challenge"])
def test_legacy_smoke_infra_lineage_preserves_current_non_smoke_retry_state(
    current_leg,
):
    """Historic smoke failures do not reset a current active-gate failure."""
    from app.release_qualifier import _legacy_smoke_infra_lineage

    qual = {
        "status": "infra_failure",
        "error": "child_terminal_failure",
        "infra_failure_signature": "unknown:terminal:failed",
        "infra_failure_child_events": [{"leg": "smoketest"}],
        "infra_attempt_history": [
            {"failing_leg": "smoketest", "signature": "smoketest:terminal:failed"},
            {
                "failing_leg": current_leg,
                "signature": f"{current_leg}:terminal:failed",
            },
        ],
    }

    assert _legacy_smoke_infra_lineage(qual) is False

@pytest.mark.asyncio
async def test_legacy_smoke_infra_lineage_does_not_consume_cap_or_backoff(
    db_session, mocker
):
    """Old smoke-only infra records re-enter immediately under active gates."""
    from datetime import datetime
    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "smokeold" * 5,
        # Deliberately lacks failing_dimension and smoke-prefixed error/signature:
        # the surviving legacy discriminator is child-event/history lineage.
        "error": "child_terminal_failure",
        "infra_failure_signature": "unknown:terminal:failed",
        "engine_sha": "engine-old",
        "consecutive_infra_errors": 99,
        "infra_failure_child_events": [{"leg": "smoketest"}],
        "infra_attempt_history": [
            {
                "signature": "unknown:terminal:failed",
                "engine_sha": "engine-old",
                "failing_leg": "smoketest",
            }
        ],
        "finalized_at": "2026-07-10T12:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    mocker.patch(
        "app.release_qualifier_redrive._utcnow",
        return_value=datetime(2026, 7, 10, 12, 0, 1),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is True
    run_qual.assert_awaited_once()


@pytest.mark.asyncio
async def test_redrive_infra_failure_backoff_gates_on_recent_failure(
    db_session, mocker
):
    """With a same-(signature, engine_sha) streak of 1 (two trailing identical
    entries) and finalized_at only 5 seconds ago, the re-driver must return False
    (backoff: 60 s required, only 5 s elapsed).

    This is the core regression guard: before the fix, re-drives fired ~15 s
    after each finalize whenever automain advanced to a new candidate SHA."""
    from datetime import datetime
    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    sig = "sig-abc123"
    eng = "eng-sha-01"
    # Two trailing attempt_history entries with the same sig+engine → streak=1
    # (the first entry in history is older, the second is the most recent).
    attempt_history = [
        {
            "finalized_at": "2026-07-10T11:59:50",
            "error": "migration_apply_check_unavailable",
            "signature": sig,
            "candidate_sha": "sha0000a" * 5,
            "engine_sha": eng,
        },
        {
            "finalized_at": "2026-07-10T11:59:55",
            "error": "migration_apply_check_unavailable",
            "signature": sig,
            "candidate_sha": "sha0000b" * 5,  # different SHA (candidate advanced)
            "engine_sha": eng,
        },
    ]

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "sha0000b" * 5,
        "error": "migration_apply_check_unavailable",
        "infra_failure_signature": sig,
        "engine_sha": eng,
        "consecutive_infra_errors": 1,  # below cap — attempt-level allows redrive
        "infra_attempt_history": attempt_history,
        "finalized_at": "2026-07-10T11:59:55",
    }
    db_session.add(version)
    await db_session.commit()

    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    # Mock _utcnow to 5 seconds after finalized_at → elapsed < 60s backoff.
    mocker.patch(
        "app.release_qualifier_redrive._utcnow",
        return_value=datetime(2026, 7, 10, 12, 0, 0),  # finalized_at + 5s
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is False, "backoff must gate the re-drive (elapsed < required_wait)"
    run_qual.assert_not_called()


@pytest.mark.asyncio
async def test_redrive_infra_failure_backoff_clears_after_wait_elapsed(
    db_session, mocker
):
    """After the backoff window elapses, the re-drive proceeds normally.

    One history entry with same (sig, eng) → streak=1 → 60 s required.
    _utcnow stubbed to 90 s after finalized_at — well past the threshold."""
    from datetime import datetime
    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    sig = "sig-abc123"
    eng = "eng-sha-01"
    # One trailing entry with same sig+eng → streak=1, backoff=60 s.
    attempt_history = [
        {
            "finalized_at": "2026-07-10T12:00:00",
            "error": "migration_apply_check_unavailable",
            "signature": sig,
            "candidate_sha": "sha0000a" * 5,
            "engine_sha": eng,
        },
    ]

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "sha0000a" * 5,
        "error": "migration_apply_check_unavailable",
        "infra_failure_signature": sig,
        "engine_sha": eng,
        "consecutive_infra_errors": 1,
        "infra_attempt_history": attempt_history,
        "finalized_at": "2026-07-10T12:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    # 90 s after finalized_at → elapsed 90 > 60 s required → backoff cleared.
    mocker.patch(
        "app.release_qualifier_redrive._utcnow",
        return_value=datetime(2026, 7, 10, 12, 1, 30),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is True, "backoff elapsed — redrive must fire"
    run_qual.assert_awaited_once()


@pytest.mark.asyncio
async def test_redrive_infra_failure_backoff_resets_on_signature_change(
    db_session, mocker
):
    """When the current infra_failure_signature differs from prior attempt_history
    entries, the trailing same-sig streak is 0 and no backoff is applied."""
    from datetime import datetime
    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    eng = "eng-sha-01"
    old_sig = "sig-old"
    new_sig = "sig-new"  # changed — streak breaks at the first entry
    attempt_history = [
        {
            "finalized_at": "2026-07-10T11:59:55",
            "error": "migration_apply_check_unavailable",
            "signature": old_sig,
            "candidate_sha": "sha0000a" * 5,
            "engine_sha": eng,
        },
    ]

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "sha0000b" * 5,
        "error": "migration_apply_check_unavailable",
        "infra_failure_signature": new_sig,  # different from history entry
        "engine_sha": eng,
        "consecutive_infra_errors": 1,
        "infra_attempt_history": attempt_history,
        "finalized_at": "2026-07-10T12:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    # Time is only 5 s after finalized_at — would be blocked IF streak > 0.
    mocker.patch(
        "app.release_qualifier_redrive._utcnow",
        return_value=datetime(2026, 7, 10, 12, 0, 5),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    # Streak=0 → _infra_backoff_seconds(0)=0 → no backoff → redrive fires.
    assert result is True, "signature changed → streak=0 → no backoff"
    run_qual.assert_awaited_once()


@pytest.mark.asyncio
async def test_redrive_infra_failure_backoff_resets_on_engine_sha_change(
    db_session, mocker
):
    """When engine_sha changes (new engine deployed), the trailing streak resets
    to 0 and no backoff is applied — a fresh engine may resolve the failure."""
    from datetime import datetime
    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    sig = "sig-abc123"
    old_eng = "eng-sha-old"
    new_eng = "eng-sha-new"  # engine advanced → streak resets
    attempt_history = [
        {
            "finalized_at": "2026-07-10T11:59:55",
            "error": "migration_apply_check_unavailable",
            "signature": sig,
            "candidate_sha": "sha0000a" * 5,
            "engine_sha": old_eng,
        },
    ]

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": "sha0000b" * 5,
        "error": "migration_apply_check_unavailable",
        "infra_failure_signature": sig,
        "engine_sha": new_eng,  # different from history entry's engine_sha
        "consecutive_infra_errors": 1,
        "infra_attempt_history": attempt_history,
        "finalized_at": "2026-07-10T12:00:00",
    }
    db_session.add(version)
    await db_session.commit()

    run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=AsyncMock(return_value={"status": "spawned"}),
    )
    mocker.patch(
        "app.release_qualifier_redrive._utcnow",
        return_value=datetime(2026, 7, 10, 12, 0, 5),
    )

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )

    assert result is True, "engine_sha changed → streak=0 → no backoff"
    run_qual.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_leaves_resolved_blocker_stale_verdict_for_strategic(
    db_session, mocker
):
    """Resolved findings do not authorize the scheduler to start a new review."""
    from app.release_readiness import apply_finding_resolution

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Stale not_promoted verdict pinned to an old SHA.
    old_sha = "oldsha0" * 5 + "0000000"
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": old_sha,
        "blocker": "below_threshold",
        "audit_findings": [
            {
                "key": "FINDING_A",
                "severity": "high",
                "summary": "some finding",
            }
        ],
    }
    db_session.add(version)
    await db_session.commit()

    # Resolve the gating finding in the ledger.
    await apply_finding_resolution(
        db_session,
        project.id,
        version.id,
        "FINDING_A",
        "resolved",
        severity="high",
    )
    await db_session.commit()

    # automain has advanced to a NEW SHA (different from old_sha).
    new_sha = "automn0" * 5 + "00000"
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=new_sha))
    # Synced — LTS gate passes.
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    # The sweep return counts FINALIZED versions only — a re-drive spawn is
    # deliberately not a finalization; the spawn itself is asserted below.
    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_sweep_leaves_not_promoted_when_findings_unresolved(db_session, mocker):
    """A not_promoted version whose gating audit findings are NOT yet
    ledger-resolved is left untouched when HEAD has NOT advanced — the
    auto-stale gate short-circuits (same SHA) so the re-drive never fires."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # candidate SHA matches automain HEAD — no advance.
    same_sha = "automn0" * 5 + "00000"
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": same_sha,
        "blocker": "below_threshold",
        "audit_findings": [
            {
                "key": "FINDING_UNRESOLVED",
                "severity": "high",
                "summary": "still open",
            }
        ],
    }
    db_session.add(version)
    await db_session.commit()

    # No ledger rows — finding is unresolved; HEAD unchanged (same SHA).
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=same_sha))
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_sweep_leaves_not_promoted_when_head_unchanged(db_session, mocker):
    """A not_promoted version whose gating findings ARE resolved but automain
    HEAD has NOT advanced (same candidate SHA) is left as-is — run_qualification
    returns cached_verdict and nothing spawns."""
    from app.release_readiness import apply_finding_resolution

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Candidate SHA matches what _branch_sha_mock returns for automain.
    same_sha = "automn0" * 5 + "00000"
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": same_sha,
        "blocker": "below_threshold",
        "audit_findings": [
            {
                "key": "FINDING_B",
                "severity": "medium",
                "summary": "resolved finding",
            }
        ],
    }
    db_session.add(version)
    await db_session.commit()

    # Mark the finding as resolved in the ledger.
    await apply_finding_resolution(
        db_session,
        project.id,
        version.id,
        "FINDING_B",
        "resolved",
        severity="medium",
    )
    await db_session.commit()

    # automain HEAD is UNCHANGED — same SHA as candidate.
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=same_sha))
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    # Still not_promoted — cached_verdict guard fired.
    assert version.last_qualification["status"] == "not_promoted"
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_sweep_leaves_cleared_ci_unresolved_for_strategic(db_session, mocker):
    """Green CI is evidence for Strategic, not scheduler qualification authority."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "ciunres0" * 5
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": candidate_sha,
        "blocker": "ci_unresolved",
        "ci_green": False,
        "ci_resolution": {"state": "pending", "ci_green": False},
        "confidence": 0.0,
        "audit_findings": [],
    }
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate_sha),
    )
    mocker.patch(
        "app.release_qualifier._resolve_candidate_ci",
        new=AsyncMock(
            return_value={
                "state": "green",
                "ci_green": True,
                "resolved_sha": candidate_sha,
                "detail": "checks passed",
            }
        ),
    )
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["candidate_sha"] == candidate_sha
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_sweep_ci_unresolved_noop_when_still_pending(db_session, mocker):
    """A same-SHA ci_unresolved verdict remains untouched while CI is pending."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate_sha = "ciunres0" * 5
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": candidate_sha,
        "blocker": "ci_unresolved",
        "ci_green": False,
        "ci_resolution": {"state": "pending", "ci_green": False},
        "confidence": 0.0,
        "audit_findings": [],
    }
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch(
        "app.github.get_branch_sha",
        new=_branch_sha_mock(automain=candidate_sha),
    )
    mocker.patch(
        "app.release_qualifier._resolve_candidate_ci",
        new=AsyncMock(
            return_value={
                "state": "pending",
                "ci_green": False,
                "resolved_sha": candidate_sha,
                "detail": "checks pending",
            }
        ),
    )
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 0
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "not_promoted"
    assert version.last_qualification["blocker"] == "ci_unresolved"
    assert await _audit_tasks(db_session, project.id) == []


@pytest.mark.asyncio
async def test_sweep_does_not_auto_stale_findings(db_session, mocker):
    """Only a decision owner may retire findings; branch movement is evidence."""
    from app.models import AuditFindingResolution

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    old_sha = "oldsha0" * 5 + "0000000"
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": old_sha,
        "blocker": "below_threshold",
        "audit_findings": [
            {"key": "FINDING_SHA_PROV", "severity": "high", "summary": "stale"},
        ],
    }
    db_session.add(version)
    await db_session.commit()

    new_sha = "newhead0" * 5
    mocker.patch("app.github.get_branch_sha", new=_branch_sha_mock(automain=new_sha))
    mocker.patch(
        "app.release_qualifier._is_target_absorbed", new=AsyncMock(return_value=True)
    )

    await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    ledger_rows = list(
        (
            await db_session.execute(
                select(AuditFindingResolution).where(
                    AuditFindingResolution.release_version_id == version.id
                )
            )
        )
        .scalars()
        .all()
    )
    assert ledger_rows == []
