"""Walkthrough execution-site and runner-routing tests."""
from _release_qualifier_testhelpers import *  # noqa: F403

from app.models import LocalRunner
from app.time_utils import _utcnow

@pytest.mark.asyncio
async def test_walkthrough_e2e_site_gate_rejects_unproven_runners(db_session):
    """Fail-closed site gate + deterministic low-id pin for the walkthrough child."""
    from app.release_qualifier_audit import (
        QualifierWalkthroughSiteUnavailableError,
        _spawn_qualifier_walkthrough_task,
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    def add_runner(runner_id, caps):
        db_session.add(LocalRunner(
            id=runner_id, project_id=project.id, token_hash="x" * 64,
            status="active", last_seen_at=_utcnow(), capabilities=caps))

    base = {"task_types": "all", "max_concurrent": 2}
    add_runner(uuid.uuid4(), {**base, "isolation_backends": {"bwrap_usable": True}})
    add_runner(uuid.uuid4(), {**base, "isolation_backends": {"docker_usable": True}})
    # A docker box whose chromium claim is only operator-stamped at the TOP level
    # (never emitted by the runner's own inventory) is NOT a proven site: the
    # runner-emitted capability must live in isolation_backends, so this fails
    # closed just like a thin-docker box.
    add_runner(uuid.uuid4(), {**base, "isolation_backends": {"docker_usable": True},
                              "playwright_chromium_usable": True})
    await db_session.commit()

    with pytest.raises(QualifierWalkthroughSiteUnavailableError,
                       match="candidate-stack site"):
        await _spawn_qualifier_walkthrough_task(
            db_session, project.id, version_id=version.id,
            candidate_sha="f011c10se" * 5,
            qualification_attempt_id=uuid.uuid4(),
        )

    low_id = uuid.UUID("00000000-0000-0000-0000-0000000000aa")
    site_caps = {**base,
                 "isolation_backends": {"docker_usable": True,
                                        "playwright_chromium_usable": True}}
    add_runner(low_id, site_caps)
    add_runner(uuid.UUID("00000000-0000-0000-0000-0000000000bb"), site_caps)
    await db_session.commit()

    pinned = await _spawn_qualifier_walkthrough_task(
        db_session, project.id, version_id=version.id,
        candidate_sha="f011c10se" * 5,
        qualification_attempt_id=uuid.uuid4(),
    )
    assert pinned.status == TaskStatus.PROVISIONING
    assert pinned.vm_instance_id == f"runner:{low_id}"
    assert pinned.context["runner_target_id"] == str(low_id)
    assert pinned.vm_provisioned_at is not None
    assert pinned.started_at is None
    assert pinned.context["qualifier_walkthrough_e2e"] is True


def test_walkthrough_site_gate_reads_runner_emitted_chromium_only():
    """The walkthrough site gate is satisfiable ONLY through the capability the
    runner's own inventory emits (``isolation_backends.playwright_chromium_usable``),
    never through an operator top-level claim, and never defaulted to True.

    DEFECT-B regression: the gate must not silently admit a thin-docker box
    because a missing capability defaults to True (default-off-is-guilty).
    """
    from types import SimpleNamespace

    from app.release_qualifier_audit import _walkthrough_e2e_site_eligible

    def snap(caps):
        return SimpleNamespace(capabilities=caps)

    base = {"task_types": "all", "max_concurrent": 2}
    # Runner-emitted proof (nested isolation_backends, the heartbeat inventory
    # location) → eligible.
    assert _walkthrough_e2e_site_eligible(
        snap({**base, "isolation_backends": {"docker_usable": True,
                                             "playwright_chromium_usable": True}})
    )
    # Operator top-level claim only (never emitted by the runner inventory) →
    # NOT eligible.
    assert not _walkthrough_e2e_site_eligible(
        snap({**base, "isolation_backends": {"docker_usable": True},
              "playwright_chromium_usable": True})
    )
    # Thin-docker (no chromium at all) → NOT eligible.
    assert not _walkthrough_e2e_site_eligible(
        snap({**base, "isolation_backends": {"docker_usable": True}})
    )
    # bwrap-only → NOT eligible.
    assert not _walkthrough_e2e_site_eligible(
        snap({**base, "isolation_backends": {"bwrap_usable": True}})
    )
    # Missing capabilities → NOT eligible.
    assert not _walkthrough_e2e_site_eligible(snap(None))
    assert not _walkthrough_e2e_site_eligible(snap({}))


@pytest.mark.asyncio
async def test_walkthrough_spawn_records_live_runner_site_matching_registry(
    db_session,
):
    """The walkthrough spawn's execution site is the LIVE LOCAL RUNNER, recorded
    with the SAME typed site the registry declares.

    The registry and the qualifier's pin share one ExecutionSite value
    (LOCAL_RUNNER_EXECUTION_SITE); the spawn records that exact site label on the
    context. This regression keeps declaration, enforcement, and consumption from
    diverging into a registry/qualifier vocabulary split again.
    """
    from app.release_qualifier_audit import (
        _WALKTHROUGH_E2E_EXECUTION_SITE,
        _spawn_qualifier_walkthrough_task,
    )
    from app.task_capabilities import (
        LOCAL_RUNNER_EXECUTION_SITE,
        TaskType,
        execution_site_for_task_type,
    )

    # The registry declares the same typed site the qualifier pins.
    assert (
        execution_site_for_task_type(TaskType.walkthrough_e2e)
        == _WALKTHROUGH_E2E_EXECUTION_SITE
        == LOCAL_RUNNER_EXECUTION_SITE
    )

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    capable_runner = LocalRunner(
        project_id=project.id, token_hash="x" * 64, status="active",
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "max_concurrent": 2,
                      "isolation_backends": {"docker_usable": True,
                                             "playwright_chromium_usable": True}},
    )
    db_session.add(capable_runner)
    await db_session.commit()

    pinned = await _spawn_qualifier_walkthrough_task(
        db_session, project.id, version_id=version.id,
        candidate_sha="f011c10se" * 5,
        qualification_attempt_id=uuid.uuid4(),
    )
    assert pinned.status == TaskStatus.PROVISIONING
    assert pinned.vm_instance_id == f"runner:{capable_runner.id}"
    assert pinned.context["runner_target_id"] == str(capable_runner.id)
    # The recorded site is the typed live-local-runner site the registry declares.
    assert pinned.context["execution_site"] == _WALKTHROUGH_E2E_EXECUTION_SITE
    assert _WALKTHROUGH_E2E_EXECUTION_SITE == LOCAL_RUNNER_EXECUTION_SITE


@pytest.mark.asyncio
async def test_walkthrough_site_failure_leaves_no_queued_release_audit_child(
    db_session, mocker
):
    """A fail-closed walkthrough execution-site check must not orphan a QUEUED
    release_audit child.

    run_qualification spawns the walkthrough_e2e child BEFORE the release_audit
    child. When no live runner proves the candidate-stack site, the walkthrough
    spawn raises QualifierWalkthroughSiteUnavailableError before any child row
    is allocated, so the spawn_failed record is written with zero surviving
    release_audit children."""
    from app import shared_state
    from sqlmodel import select as _sqlmodel_select

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Compatible rubric so the probe request passes the request-time gate and
    # reaches the spawn (which then fails closed on the missing runner site).
    await _seed_walkthrough_compatible_rubric(db_session, project, version)

    # No live runner proves the candidate-stack site (docker info + Compose +
    # Chromium), so the walkthrough spawn fails closed before the release_audit
    # child is allocated.
    candidate = "deadbeef" * 5  # 40-char placeholder SHA
    stamped_engine = "f" * 40
    mocker.patch("app.deployed_sha.get_serving_sha", return_value=stamped_engine)
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        requested_probes=["walkthrough_e2e"],
        explicit_request=True,
    )
    await db_session.commit()

    # Fail-closed: the site failure still blocks the qualification.
    assert result["status"] == "spawn_failed"
    await db_session.refresh(version)
    assert version.last_qualification["error"] == "spawn_failed"
    assert version.last_qualification.get("audit_task_id") is None
    assert version.last_qualification.get("walkthrough_e2e_task_id") is None
    # The spawn_failed record stamps the serving engine sha so the error-redrive
    # site-change re-arm has a baseline to compare against a later deploy.
    assert version.last_qualification.get("engine_sha") == stamped_engine
    assert isinstance(
        version.last_qualification.get("promotion_policy_snapshot"), dict
    )
    assert version.last_qualification.get("promotion_policy_source")
    assert len(version.last_qualification.get("promotion_policy_hash") or "") == 64
    attempt_policy = rq.qualification_attempt_policy_projection(
        version.last_qualification
    )
    assert attempt_policy is not None
    assert attempt_policy["threshold"] == version.last_qualification[
        "promotion_policy_snapshot"
    ]["threshold"]

    # No QUEUED release_audit child may survive the failed dispatch.
    project_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert [
        task
        for task in project_tasks
        if (task.first_payload or {}).get("type") == "release_audit"
    ] == []


@pytest.mark.asyncio
async def test_audit_child_not_orphaned_when_probe_spawn_fails(db_session, mocker):
    """A spawn failure after the walkthrough (probe) child is created must not
    orphan a qualifier child.

    run_qualification spawns the walkthrough_e2e (probe) child FIRST, then the
    release_audit child.  When a later spawn fails, the already-created probe
    child row must be cancelled with the attempt — the qualification is going
    terminal as spawn_failed, so no child may linger in a non-terminal state
    (QUEUED release_audit or PROVISIONING walkthrough_e2e).
    """
    from app import shared_state
    from sqlmodel import select as _sqlmodel_select

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # Compatible rubric so the probe request passes the request-time gate and
    # reaches the spawn (the audit spawn below then fails and must not orphan
    # the already-allocated walkthrough child).
    await _seed_walkthrough_compatible_rubric(db_session, project, version)

    # A live runner proving the candidate-stack site so the walkthrough (probe)
    # child spawn SUCCEEDS and its row is allocated before the audit spawn fails.
    capable_runner = LocalRunner(
        project_id=project.id, token_hash="x" * 64, status="active",
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "max_concurrent": 2,
                      "isolation_backends": {"docker_usable": True,
                                             "playwright_chromium_usable": True}},
    )
    db_session.add(capable_runner)
    await db_session.commit()

    candidate = "c0ffee01" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )
    # The probe child spawns first and succeeds; force the FOLLOWING audit spawn
    # to fail so the already-allocated probe child would otherwise be orphaned.
    mocker.patch(
        "app.release_qualifier._spawn_release_audit_task",
        new=AsyncMock(side_effect=RuntimeError("audit spawn boom")),
    )

    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        requested_probes=["walkthrough_e2e"],
        explicit_request=True,
    )
    await db_session.commit()

    # The spawn failure still blocks the qualification.
    assert result["status"] == "spawn_failed"
    await db_session.refresh(version)
    assert version.last_qualification["error"] == "spawn_failed"
    assert version.last_qualification.get("audit_task_id") is None
    assert version.last_qualification.get("walkthrough_e2e_task_id") is None

    project_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )

    # No release_audit child may survive in QUEUED after the failed spawn.
    assert [
        task
        for task in project_tasks
        if (task.first_payload or {}).get("type") == "release_audit"
        and task.status == TaskStatus.QUEUED
    ] == []

    # The walkthrough (probe) child was created before the audit spawn failed
    # and must not be left orphaned in PROVISIONING — it is cancelled with the
    # failing attempt.
    walk_children = [
        task
        for task in project_tasks
        if (task.context or {}).get("qualifier_walkthrough_e2e") is True
    ]
    assert len(walk_children) == 1
    assert walk_children[0].status == TaskStatus.CANCELLED


@pytest.mark.asyncio
async def test_spawn_failed_redriven_on_site_change(db_session, mocker):
    """A CAPPED walkthrough_e2e spawn_failed re-arms EXACTLY once when the
    execution-site capability changed — the engine revision that recorded the
    failure is no longer serving — so the newly-available dispatch site gets one
    genuine attempt before the cap re-applies.

    This is the acceptance evidence for the fix: a fleet that temporarily has no
    site-proven runner parks a walkthrough qualification at spawn_failed, and the
    error-redrive sweep previously skipped it (``spawn_failed`` was absent from
    ``_TRANSIENT_ERROR_CODES``).  Adding it to the transient codes makes the
    version re-drivable; the engine-sha re-arm mirrors the infra-failure path so
    a CAPPED spawn_failed can still resume once after a deploy (every deploy
    stamps a new ``DEPLOY_SHA``, so a record whose ``engine_sha`` differs from
    the current serving sha means the site situation may have changed).  The
    re-arm is one-shot: once a fresh capped failure is recorded under the
    CURRENT engine, the cap re-applies and halts again.
    """
    from app.release_qualifier_redrive import (
        _MAX_ERROR_REDRIVE_ATTEMPTS,
        _redrive_error_qualification,
    )

    old_engine = "a" * 40
    new_engine = "b" * 40
    mocker.patch("app.deployed_sha.get_serving_sha", return_value=new_engine)

    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "error",
        "error": "spawn_failed",
        "detail": (
            "cannot dispatch qualifier walkthrough_e2e for candidate "
            "ca11face...: no live project-allowed runner is registered"
        ),
        "candidate_sha": "ca11face" * 5,
        "engine_sha": old_engine,
        "requested_probes": ["walkthrough_e2e"],
        "error_redrive_count": _MAX_ERROR_REDRIVE_ATTEMPTS,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )

    result = await _redrive_error_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True, (
        "capped walkthrough spawn_failed must re-arm once after the "
        "execution-site capability change (record engine no longer serving)"
    )
    mock_run_qual.assert_called_once()

    # ONE-SHOT: after a fresh capped failure recorded under the CURRENT serving
    # engine, the re-arm must NOT fire again — the cap re-applies and halts.
    await db_session.refresh(version)
    version.last_qualification = {
        "status": "error",
        "error": "spawn_failed",
        "detail": (
            "cannot dispatch qualifier walkthrough_e2e for candidate "
            "ca11face...: no live project-allowed runner is registered"
        ),
        "candidate_sha": "ca11face" * 5,
        "engine_sha": new_engine,
        "requested_probes": ["walkthrough_e2e"],
        "error_redrive_count": _MAX_ERROR_REDRIVE_ATTEMPTS,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result_again = await _redrive_error_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result_again is False, (
        "capped walkthrough spawn_failed must NOT re-arm a second time once "
        "the record's engine matches the current serving engine"
    )
    assert mock_run_qual.call_count == 1


