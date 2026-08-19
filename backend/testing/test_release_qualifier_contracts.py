"""Qualification contract and promotion-state tests."""
from _release_qualifier_testhelpers import *  # noqa: F403

def test_walkthrough_execution_site_is_registry_declared():
    """The qualifier's walkthrough_e2e site is a legal ExecutionSite value AND
    matches the registry declaration for the task type.

    Declaration (task_capabilities registry), enforcement (dispatch), and
    consumption (release_qualifier_audit) must share ONE typed ExecutionSite;
    this is the assertion that prevents the registry/qualifier split from
    recurring.
    """
    from typing import get_args

    from app.release_qualifier_audit import _WALKTHROUGH_E2E_EXECUTION_SITE
    from app.task_capabilities import (
        ExecutionSite,
        LOCAL_RUNNER_EXECUTION_SITE,
        TaskType,
        execution_site_for_task_type,
    )

    # The qualifier-pinned site is a legal value of the type that governs it.
    assert _WALKTHROUGH_E2E_EXECUTION_SITE in get_args(ExecutionSite)
    assert LOCAL_RUNNER_EXECUTION_SITE in get_args(ExecutionSite)
    # The qualifier pins the same site the registry declares for the task type.
    assert (
        execution_site_for_task_type(TaskType.walkthrough_e2e)
        == _WALKTHROUGH_E2E_EXECUTION_SITE
        == LOCAL_RUNNER_EXECUTION_SITE
    )


def test_walkthrough_execution_site_is_not_generically_dispatchable():
    """Generic dispatch fails closed for the walkthrough site.

    A non-default execution site is a hard routing contract: generic dispatch
    (runner pin / warm pool / Cloud Run / GCE) must refuse to route the task
    rather than silently treating the site as ``default``.  Unknown task types
    and default-site types stay on the default matrix.
    """
    from app.task_capabilities import (
        TaskType,
        execution_site_is_generic_dispatchable,
    )

    assert execution_site_is_generic_dispatchable(TaskType.walkthrough_e2e) is False
    assert execution_site_is_generic_dispatchable(TaskType.create) is True
    assert execution_site_is_generic_dispatchable("totally_unknown_type") is True


def test_qualification_contract_fingerprint_tracks_goal_and_categories():
    empty_rubric = rq._rubric_fingerprint([])
    category_rubric = rq._rubric_fingerprint(
        [{"criterion_key": "ship", "text": "Ship the feature"}]
    )
    baseline = rq._qualification_contract_fingerprint("Ship safely", empty_rubric)

    assert baseline == rq._qualification_contract_fingerprint(
        "  Ship   safely  ", empty_rubric
    )
    assert baseline != rq._qualification_contract_fingerprint(
        "Ship quickly", empty_rubric
    )
    assert baseline != rq._qualification_contract_fingerprint(
        "Ship safely", category_rubric
    )
    assert baseline != rq._qualification_contract_fingerprint(
        "Ship safely",
        empty_rubric,
        promotion_policy={"threshold": 0.91, "weights": {"audit": 1.0}},
    )
    assert baseline != rq._qualification_contract_fingerprint(
        "Ship safely", empty_rubric, capability_proof_mode="enforce"
    )
    assert rq._qualification_contract_fingerprint(
        "Ship safely", empty_rubric, requested_probes=["walkthrough", "smoke"]
    ) == rq._qualification_contract_fingerprint(
        "Ship safely", empty_rubric, requested_probes=["smoke", "walkthrough"]
    )


def test_declared_promotion_threshold_matches_enforced(mocker):
    """The packaged default and the real composite gate share one threshold.

    Runtime and project-specific overrides remain supported; this guard checks
    the fleet default that seeds those overrides.  The three verdicts below
    exercise the inclusive ``confidence >= threshold`` enforcement boundary.
    """
    mocker.patch.dict(
        rq._runtime_config,
        {"promotion_threshold": None, "promotion_weights": None},
        clear=False,
    )
    policy = rq._load_policy()
    declared_threshold = policy["threshold"]

    assert declared_threshold == pytest.approx(rq.PROMOTION_CONFIDENCE_THRESHOLD)

    for candidate_confidence, expected_promotion in (
        (declared_threshold - 0.0001, False),
        (declared_threshold, True),
        (declared_threshold + 0.0001, True),
    ):
        verdict = rq.compute_composite(
            ci_green=True,
            audit_score=candidate_confidence,
            test_erosion=candidate_confidence,
            smoketest_score=None,
            smoketest_enabled=False,
            policy=policy,
        )
        assert verdict["confidence"] == pytest.approx(candidate_confidence, abs=0.0001)
        assert verdict["promote"] is expected_promotion


@pytest.mark.asyncio
async def test_promotion_disabled_does_not_overwrite_pending_qualification(
    db_session, mocker
):
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    pending = {
        "status": "pending",
        "qualification_attempt_id": str(uuid.uuid4()),
        "candidate_sha": "pending0" * 5,
    }
    version.last_qualification = pending
    db_session.add(version)
    await db_session.commit()

    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=False,
    )
    mocker.patch(
        "app.release_qualifier._qualification_in_flight",
        new=AsyncMock(return_value=True),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )

    assert result == {"status": "already_pending"}
    await db_session.refresh(version)
    assert version.last_qualification == pending


@pytest.mark.asyncio
async def test_pending_goal_change_supersedes_attempt_before_promotion(
    db_session, mocker
):
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    _project, version = await _make_project_and_version(db_session, phase="stabilize")
    launched_goal = "Ship the original goal"
    # Only the GOAL may differ from the launch contract here: open_version seeds
    # a default authorship criterion, so a hardcoded empty launched rubric would
    # trip the earlier rubric-drift gate and never reach the contract check.
    launched_rubric, launched_lifecycle = await rq._snapshot_qualification_rubric(
        db_session, version
    )
    rubric_fingerprint = rq._rubric_fingerprint(launched_rubric)
    policy_snapshot = {"threshold": 0.8, "weights": {}}
    version.goal = "Ship the revised goal"
    version.last_qualification = {
        "status": "pending",
        "qualification_attempt_id": str(uuid.uuid4()),
        "candidate_sha": "contract" * 5,
        "version_goal": launched_goal,
        "rubric_snapshot": launched_rubric,
        "rubric_fingerprint": rubric_fingerprint,
        "rubric_lifecycle_snapshot": launched_lifecycle,
        "rubric_provenance": rq._rubric_provenance(
            launched_rubric,
            source="qualification_launch",
            qualification_attempt_id=None,
        ),
        "contract_fingerprint": rq._qualification_contract_fingerprint(
            launched_goal,
            rubric_fingerprint,
            promotion_policy=policy_snapshot,
        ),
        "promotion_policy_snapshot": policy_snapshot,
        "capability_proof_mode": "off",
        "env_preflight_enabled": False,
        "requested_probes": [],
        "started_at": "2026-08-03T00:00:00+00:00",
    }
    db_session.add(version)
    await db_session.commit()

    promote_spy = mocker.patch("app.release_qualifier.promote", new=AsyncMock())
    result = await rq._finalize_qualification(
        db_session,
        version,
        _pre_gates={
            "ci_green": True,
            "ci_resolution": {"state": "green", "resolved_sha": "contract" * 5},
        },
    )

    assert result["status"] == "rubric_changed"
    assert result["blocker"] == "qualification_contract_superseded"
    promote_spy.assert_not_called()


@pytest.mark.parametrize(
    "contract_change", ["goal", "category", "policy_threshold", "capability_mode"]
)
@pytest.mark.asyncio
async def test_same_sha_contract_change_spawns_fresh_holistic_review(
    db_session, mocker, contract_change
):
    from app import shared_state

    mocker.patch.dict(
        shared_state.runtime_config,
        {
            "promotion_enabled": True,
            "capability_proof_gate_mode": "off",
            "env_preflight_enabled": False,
        },
        clear=False,
    )
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate = ("newcontract" * 4)[:40]
    launched_goal = "Ship the original outcome"
    launched_rubric = []
    launched_rubric_fingerprint = rq._rubric_fingerprint(launched_rubric)
    launched_policy = rq._load_policy(project)
    launched_contract = rq._qualification_contract_fingerprint(
        launched_goal,
        launched_rubric_fingerprint,
        promotion_policy=launched_policy,
        capability_proof_mode="off",
        env_preflight_enabled=False,
        requested_probes=[],
    )
    version.goal = launched_goal
    version.last_qualification = {
        "status": "dry_run",
        "candidate_sha": candidate,
        "requested_probes": [],
        "version_goal": launched_goal,
        "rubric_snapshot": launched_rubric,
        "rubric_fingerprint": launched_rubric_fingerprint,
        "contract_fingerprint": launched_contract,
    }
    db_session.add(version)
    await db_session.commit()

    if contract_change == "goal":
        version.goal = "Ship the revised outcome"
        db_session.add(version)
        await db_session.commit()
    elif contract_change == "category":
        await _add_version_criterion(
            db_session,
            project,
            version,
            key="new-category",
            text="Prove the newly required outcome",
        )
    elif contract_change == "policy_threshold":
        old_threshold = float(launched_policy["threshold"])
        changed_threshold = 0.95 if old_threshold != 0.95 else 0.9
        project.build_config = {
            **(project.build_config or {}),
            "promotion_threshold": changed_threshold,
        }
        db_session.add(project)
        await db_session.commit()
    else:
        shared_state.runtime_config["capability_proof_gate_mode"] = "enforce"

    mocker.patch(
        "app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token")
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"conclusion": "success"}),
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"
    assert result["audit_task_id"] is not None
    await db_session.refresh(version)
    assert version.last_qualification["contract_fingerprint"] != launched_contract


