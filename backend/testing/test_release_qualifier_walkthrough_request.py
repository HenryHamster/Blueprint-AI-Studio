"""Walkthrough probe request compatibility and child-spawn tests."""
from _release_qualifier_testhelpers import *  # noqa: F403

from app.models import LocalRunner
from app.time_utils import _utcnow

@pytest.mark.asyncio
async def test_run_qualification_spawns_walkthrough_e2e_when_enabled(
    db_session, mocker
):
    """An explicit walkthrough probe request spawns exactly ONE PROVISIONING
    walkthrough_e2e child pinned AT BIRTH to the verified container-capable
    project runner (candidate SHA + attempt id); a same-attempt re-drive does
    NOT spawn a duplicate."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    # The walkthrough_e2e probe's canonical evidence keys must intersect the
    # open version's ACTIVE rubric (request-time compatibility gate) or the
    # probe is refused before any child spawn.
    await _seed_walkthrough_compatible_rubric(db_session, project, version)

    # A live runner proving the candidate-stack site (docker info + Compose +
    # Chromium) so run_qualification pins the walkthrough child to it.
    capable_runner = LocalRunner(
        project_id=project.id, token_hash="x" * 64, status="active",
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "max_concurrent": 2,
                      "isolation_backends": {"docker_usable": True,
                                             "playwright_chromium_usable": True}},
    )
    db_session.add(capable_runner)
    await db_session.commit()

    candidate = "wa1c0e2e" * 5  # 40-char placeholder SHA
    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value={"conclusion": "success"}))

    result = await rq.run_qualification(
        db_session,
        project.id,
        version,
        requested_probes=["walkthrough_e2e"],
        explicit_request=True,
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["requested_probes"] == ["walkthrough_e2e"]
    walk_task_id = result["walkthrough_e2e_task_id"]
    assert walk_task_id is not None

    await db_session.refresh(version)
    attempt_id = version.last_qualification["qualification_attempt_id"]
    assert version.last_qualification["requested_probes"] == ["walkthrough_e2e"]
    assert version.last_qualification["walkthrough_e2e_task_id"] == walk_task_id

    from sqlmodel import select as _sqlmodel_select

    project_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    walk_tasks = [
        task
        for task in project_tasks
        if (task.context or {}).get("qualifier_walkthrough_e2e") is True
    ]
    assert len(walk_tasks) == 1
    walk = walk_tasks[0]
    assert str(walk.id) == walk_task_id
    assert walk.status == TaskStatus.PROVISIONING
    assert walk.vm_instance_id == f"runner:{capable_runner.id}"
    assert walk.vm_provisioned_at is not None
    assert walk.started_at is None
    assert walk.created_by == "ORCHESTRATOR"
    assert walk.first_payload["type"] == "walkthrough_e2e"
    assert walk.first_payload["candidate_sha"] == candidate
    assert walk.first_payload["qualification_attempt_id"] == attempt_id
    assert walk.first_payload["base_branch"] == "automain"
    # Qualifier marker prevents ordinary task-fix cascades.
    assert walk.context["qualifier_walkthrough_e2e"] is True
    assert walk.context["runner_target_id"] == str(capable_runner.id)
    assert walk.context["candidate_sha"] == candidate
    assert walk.context["qualification_attempt_id"] == attempt_id
    assert walk.context["release_version_id"] == str(version.id)
    assert walk.context["execution_site"] == "local_runner"


@pytest.mark.asyncio
async def test_run_qualification_skips_walkthrough_e2e_when_disabled(
    db_session, mocker
):
    """Without an explicit probe request, no walkthrough_e2e child is spawned
    and last_qualification records a None walkthrough_e2e_task_id."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")

    candidate = "de1e7ed0" * 5  # 40-char placeholder SHA
    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value={"conclusion": "success"}))

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["requested_probes"] == []
    assert result["walkthrough_e2e_task_id"] is None

    await db_session.refresh(version)
    assert version.last_qualification["requested_probes"] == []
    assert version.last_qualification["walkthrough_e2e_task_id"] is None

    from sqlmodel import select as _sqlmodel_select

    project_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    walk_tasks = [
        task
        for task in project_tasks
        if (task.context or {}).get("qualifier_walkthrough_e2e") is True
    ]
    assert walk_tasks == []


async def test_run_qualification_drops_incompatible_walkthrough_probe_and_spawns_probe_free(
    db_session, mocker
):
    """A walkthrough_e2e probe request whose canonical evidence keys share NO
    ACTIVE criterion with the open version's rubric is DROPPED (not refused):
    the qualification still spawns, probe-free, and the drop is recorded
    durably under ``qualify_request_blocked`` / ``qualify_request_blocked_at``.

    The walkthrough evidence contract is pinned to the retired v26 criteria; a
    rubric with zero overlap would only fail (contained, but expensive) after a
    full qualification was spent.  The request-time gate therefore DROPS the
    incompatible probe and continues — an incompatible probe must never prevent
    the qualification from spawning at all."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # A v27-style rubric with NO overlap with the walkthrough canonical keys.
    for _rubric_key in (
        "c-28e29fc5",
        "c-09e29209",
        "c-13867029",
        "c-635ee093",
        "c-280480cf",
    ):
        await _add_version_criterion(
            db_session,
            project,
            version,
            key=_rubric_key,
            text=f"criterion {_rubric_key}",
        )

    candidate = "de1e7ed0" * 5  # 40-char placeholder SHA
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

    # The qualification is NOT refused — it spawns probe-free.
    assert result["status"] == "spawned"
    assert result["requested_probes"] == []
    assert result["walkthrough_e2e_task_id"] is None

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "pending"
    assert lq["requested_probes"] == []
    assert lq["walkthrough_e2e_task_id"] is None
    # The drop is recorded durably with the blocker + probe + BOTH key sets.
    blocked = lq["qualify_request_blocked"]
    assert blocked["blocker"] == "incompatible_probe"
    assert blocked["probe"] == "walkthrough_e2e"
    assert blocked["canonical_evidence_keys"] == sorted(
        rq._WALKTHROUGH_E2E_EXPECTED_CANONICAL_EVIDENCE_KEYS
    )
    assert blocked["active_rubric_keys"] == sorted(
        ["c-28e29fc5", "c-09e29209", "c-13867029", "c-635ee093", "c-280480cf"]
    )
    assert lq["qualify_request_blocked_at"]

    # No walkthrough child was spawned for the dropped probe.
    from sqlmodel import select as _sqlmodel_select

    project_tasks = (
        (
            await db_session.execute(
                _sqlmodel_select(Task).where(Task.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    walk_tasks = [
        task
        for task in project_tasks
        if (task.context or {}).get("qualifier_walkthrough_e2e") is True
    ]
    assert walk_tasks == []


async def test_redrive_infra_failure_drops_latched_incompatible_probe_and_spawns_probe_free(
    db_session, mocker
):
    """A latched incompatible probe arriving on an infra-failure redrive is
    dropped probe-free, and the redrive returns True (a real re-spawn) instead
    of stalling with ``return False``.

    The re-driver re-passes ``last_qualification.requested_probes`` verbatim;
    when that latched walkthrough_e2e probe is structurally impossible for the
    open version's rubric, run_qualification must drop it and spawn probe-free
    so the redrive reaches a real verdict instead of freezing the streak."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # v27-style rubric with NO overlap → the latched probe is incompatible.
    for _rubric_key in (
        "c-28e29fc5",
        "c-09e29209",
        "c-13867029",
        "c-635ee093",
        "c-280480cf",
    ):
        await _add_version_criterion(
            db_session,
            project,
            version,
            key=_rubric_key,
            text=f"criterion {_rubric_key}",
        )

    candidate = "de1e7ed0" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    # A stranded infra_failure record that latched the incompatible probe.  It
    # carries the original qualification_attempt_id (a real infra_failure record
    # always does — _handle_infra_failure copies it from the pending record), so
    # the redrive re-enters run_qualification as a continued attempt.
    version.last_qualification = {
        "status": "infra_failure",
        "candidate_sha": candidate,
        "qualification_attempt_id": str(uuid.uuid4()),
        "error": "walkthrough_e2e_candidate_stack_boot_failure",
        "infra_failure_signature": "walkthrough_e2e:boot_failure",
        "consecutive_infra_errors": 1,
        "requested_probes": ["walkthrough_e2e"],
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    from app.release_qualifier_redrive import _redrive_infra_failure_qualification

    result = await _redrive_infra_failure_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert result is True, (
        "redrive must re-spawn probe-free instead of stalling on the latched "
        "incompatible probe"
    )

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "pending"
    assert lq["requested_probes"] == []
    assert lq["walkthrough_e2e_task_id"] is None
    blocked = lq["qualify_request_blocked"]
    assert blocked["blocker"] == "incompatible_probe"
    assert blocked["probe"] == "walkthrough_e2e"
    assert blocked["canonical_evidence_keys"] == sorted(
        rq._WALKTHROUGH_E2E_EXPECTED_CANONICAL_EVIDENCE_KEYS
    )
    assert lq["qualify_request_blocked_at"]


async def test_compatible_walkthrough_probe_passes_through_unchanged(
    db_session, mocker
):
    """A walkthrough_e2e probe whose canonical keys ARE active in the open
    version's rubric passes through unchanged: the probe is NOT dropped, no
    qualify_request_blocked record is stamped, and the walkthrough child is
    spawned.  Regression guard for the #16704 compatible-probe contract."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # The walkthrough_e2e probe's canonical evidence keys must intersect the
    # open version's ACTIVE rubric (request-time compatibility gate) or the
    # probe is dropped before any child spawn.
    await _seed_walkthrough_compatible_rubric(db_session, project, version)

    # A live runner proving the candidate-stack site (docker info + Compose +
    # Chromium) so run_qualification pins the walkthrough child to it.
    capable_runner = LocalRunner(
        project_id=project.id, token_hash="x" * 64, status="active",
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "max_concurrent": 2,
                      "isolation_backends": {"docker_usable": True,
                                             "playwright_chromium_usable": True}},
    )
    db_session.add(capable_runner)
    await db_session.commit()

    candidate = "wa1c0e2e" * 5  # 40-char placeholder SHA
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

    assert result["status"] == "spawned"
    assert result["requested_probes"] == ["walkthrough_e2e"]
    assert result["walkthrough_e2e_task_id"] is not None

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["requested_probes"] == ["walkthrough_e2e"]
    assert lq["walkthrough_e2e_task_id"] == result["walkthrough_e2e_task_id"]
    # Compatible probe → no drop record at all.
    assert lq.get("qualify_request_blocked") is None
    assert lq.get("qualify_request_blocked_at") is None


@pytest.mark.asyncio
async def test_walkthrough_probe_resolves_against_open_rubric_and_is_not_refused(
    db_session, mocker
):
    """A walkthrough_e2e probe request against the OPEN version's rubric is NOT
    refused when the rubric carries criteria the live harness can produce
    evidence for — even though none of the harness's legacy v26 keys is present.

    The evidence contract resolves scenario→criterion against the OPEN version's
    ACTIVE criteria at request time (not a module-level constant naming one
    frozen rubric), so a v27-style rubric whose criterion texts name the
    walkthrough flows keeps the probe requestable.  Regression for audit finding
    e597be25dd8ac5d9: the strategist guidance tells the strategist to request
    walkthrough_e2e to clear a ``criterion_coverage_incomplete`` blocker, but the
    request-time gate used to refuse it for any rubric whose keys do not include
    the retired v26 canonical keys."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # v27-style rubric with FRESH keys whose texts describe the walkthrough flows
    # the live harness exercises.  None of the harness's legacy v26 keys appears.
    live_walkthrough_keys = []
    for _text in (
        "Prove runner enrollment on a live local runner",
        "Project tools iframe subresource token flow works",
        "Conversational Workday session lifecycle end to end",
    ):
        key = f"c-{uuid.uuid4().hex[:8]}"
        live_walkthrough_keys.append(key)
        await _add_version_criterion(
            db_session,
            project,
            version,
            key=key,
            text=_text,
        )

    # The resolution maps the probe's flows to the OPEN rubric's ACTIVE criteria.
    rows = await rq.get_criteria_for_version(db_session, version.id)
    resolved = rq._resolve_walkthrough_e2e_evidence_contract(rows)
    assert resolved == frozenset(live_walkthrough_keys)

    # A live runner proving the candidate-stack site so run_qualification pins
    # the walkthrough child to it.
    capable_runner = LocalRunner(
        project_id=project.id, token_hash="x" * 64, status="active",
        last_seen_at=_utcnow(),
        capabilities={"task_types": "all", "max_concurrent": 2,
                      "isolation_backends": {"docker_usable": True,
                                             "playwright_chromium_usable": True}},
    )
    db_session.add(capable_runner)
    await db_session.commit()

    candidate = "wa1c0e2e" * 5  # 40-char placeholder SHA
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

    # The probe is NOT refused: it passes through and the child spawns.
    assert result["status"] == "spawned"
    assert result["requested_probes"] == ["walkthrough_e2e"]
    assert result["walkthrough_e2e_task_id"] is not None

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["requested_probes"] == ["walkthrough_e2e"]
    assert lq["walkthrough_e2e_task_id"] == result["walkthrough_e2e_task_id"]
    # Compatible probe → no drop record at all.
    assert lq.get("qualify_request_blocked") is None
    assert lq.get("qualify_request_blocked_at") is None

