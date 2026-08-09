"""Walkthrough probe finalization and evidence-contract tests."""
from _release_qualifier_testhelpers import *  # noqa: F403

from app.models import LocalRunner
from app.time_utils import _utcnow

@pytest.mark.asyncio
async def test_finalize_infra_shaped_probe_failure_is_infra_failure_not_quality(
    db_session, mocker
):
    """An infra-shaped walkthrough probe failure (the child task itself FAILED,
    e.g. an agent-VM or clone failure) is classified as a non-conclusive
    infra_failure — NOT a conclusive not_promoted quality verdict against the
    candidate.

    Regression for audit finding 4b795cc96f7e5633: moving the probe leg after
    dimension composition reclassified every infra-shaped probe failure from a
    re-driven infra_failure into a conclusive not_promoted QUALITY verdict.  The
    restored contract routes infra-shaped failures to _handle_infra_failure so
    the infra re-driver re-runs the attempt, while the eager-written audit fact
    survives."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Compatible rubric so a request-time gate would keep the probe (this test
    # exercises the FINALIZE classification, which runs regardless).
    await _seed_walkthrough_compatible_rubric(db_session, project, version)

    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
            # The fixed audits.ts emits the acceptance-category contract whenever
            # an active rubric exists — independent of a version goal string — so
            # a goal-less version's report still carries per-category verdicts.
            "category_verdicts": [
                {
                    "category_key": "c-dfdc9d82",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:10"}],
                },
                {
                    "category_key": "c-f52fd975",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:20"}],
                },
                {
                    "category_key": "c-5214996f",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:30"}],
                },
            ],
            "residual_uncaptured_gap": None,
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    candidate_sha = version.last_qualification["candidate_sha"]
    # An infra-terminal walkthrough child: the clone/stack-boot failure case the
    # probe lane must never book as a quality judgment against the candidate.
    walk_task = Task(
        project_id=project.id,
        title="walkthrough",
        payloads=[{"type": "walkthrough_e2e", "base_branch": "automain"}],
        vm_instance_id="vm-walkthrough",
        status=TaskStatus.FAILED,
        context={
            "candidate_sha": candidate_sha,
            "agent_failure_category_raw": "clone_failed",
        },
    )
    db_session.add(walk_task)
    await db_session.commit()
    await db_session.refresh(walk_task)

    qual = {
        **(version.last_qualification or {}),
        "requested_probes": ["walkthrough_e2e"],
        "walkthrough_e2e_task_id": str(walk_task.id),
    }
    version.last_qualification = _with_current_qualification_contract(
        project, version, qual
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Infra-shaped probe failure → non-conclusive infra_failure, NOT a quality
    # not_promoted verdict.
    assert result["status"] == "infra_failure"
    assert result["error"].startswith("walkthrough_e2e_task_failed")

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "infra_failure"
    assert lq["error"].startswith("walkthrough_e2e_task_failed")
    # The audit verdict survives via the eager-written per-dimension ledger.
    assert lq["resolved_dimensions"]["audit"]["status"] == "passed"
    assert lq["resolved_dimensions"]["audit"]["sub_score"] == pytest.approx(1.0)


async def test_finalize_failed_walkthrough_probe_blocks_promotion_and_preserves_verdict(
    db_session, mocker
):
    """A requested walkthrough_e2e probe that FAILED blocks promotion while
    preserving the already-computed audit verdict.

    Regression for the probe-leg blast radius: the probe evaluation used to run
    BEFORE the release_audit report was read, so any probe exit discarded the
    whole verdict (three consecutive attempts reported audit='unavailable').  The
    probe leg now runs AFTER dimension composition and a probe that did not pass
    fails ONLY its own dimension — the audit sub-score survives alongside the
    walkthrough_e2e=0.0 blocker."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # Compatible rubric so the report keys resolve to ACTIVE criteria (the probe
    # evaluation then reaches the genuine criterion-failure path).
    await _seed_walkthrough_compatible_rubric(db_session, project, version)

    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
            # The fixed audits.ts emits the acceptance-category contract whenever
            # an active rubric exists — independent of a version goal string — so
            # a goal-less version's report still carries per-category verdicts.
            "category_verdicts": [
                {
                    "category_key": "c-dfdc9d82",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:10"}],
                },
                {
                    "category_key": "c-f52fd975",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:20"}],
                },
                {
                    "category_key": "c-5214996f",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:30"}],
                },
            ],
            "residual_uncaptured_gap": None,
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    candidate_sha = version.last_qualification["candidate_sha"]
    walk_task = Task(
        project_id=project.id,
        title="walkthrough",
        payloads=[{"type": "walkthrough_e2e", "base_branch": "automain"}],
        vm_instance_id="vm-walkthrough",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": candidate_sha},
    )
    db_session.add(walk_task)
    await db_session.flush()
    db_session.add(
        Artifact(
            task_id=walk_task.id,
            artifact_type=ArtifactType.WALKTHROUGH_E2E_REPORT,
            logs=json.dumps(
                {
                    "git_sha": candidate_sha,
                    "exercised_engine_sha": candidate_sha,
                    "results": [
                        {"criterion_key": "c-dfdc9d82", "passed": True},
                        {"criterion_key": "c-f52fd975", "passed": False},
                        {
                            "criterion_key": "c-workday-session-lifecycle",
                            "passed": True,
                        },
                        {"criterion_key": "c-5214996f", "passed": False},
                    ],
                }
            ),
        )
    )
    await db_session.commit()
    await db_session.refresh(walk_task)

    # Stamp the probe request + task id and re-compute the frozen contract so
    # finalize does not treat the mutation as mid-flight rubric drift.
    qual = {
        **(version.last_qualification or {}),
        "requested_probes": ["walkthrough_e2e"],
        "walkthrough_e2e_task_id": str(walk_task.id),
    }
    version.last_qualification = _with_current_qualification_contract(
        project, version, qual
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    assert result["status"] == "not_promoted"
    assert result["blocker"] == "walkthrough_e2e_failed"

    await db_session.refresh(version)
    lq = version.last_qualification
    # The audit verdict is preserved alongside the walkthrough failure — the
    # core regression: a probe failure no longer discards the computed audit.
    assert lq["sub_scores"]["audit"] == pytest.approx(1.0)
    assert lq["sub_scores"]["walkthrough_e2e"] == 0.0
    assert lq["walkthrough_e2e_result"]["status"] == "failed"
    assert lq["walkthrough_e2e_result"]["failed_criterion_keys"] == [
        "c-f52fd975",
        "c-5214996f",
    ]


async def test_finalize_unknown_criteria_probe_is_contract_blocker_preserves_verdict(
    db_session, mocker
):
    """A report whose keys are not active in the version rubric (unknown criteria)
    is a REPORT-CONTRACT defect resolved by the DEPLOYED qualifier — booked as a
    durable not_promoted verdict with blocker=walkthrough_e2e_contract_failed /
    execution_site=deployed — NOT a transient infra hold.  The eager-written
    audit fact still survives via the per-dimension ledger: the probe leg runs
    AFTER dimension composition and the eager writes, and the contract-blocker
    record composes the resolved facts back in, so the already-computed audit
    verdict stays visible."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # A v27-style rubric — the walkthrough report's retired-v26 keys resolve to
    # NO active criterion (unknown_criteria at finalize; refused at request time
    # by the Change-B gate, but a legacy/redriven qual still reaches finalize).
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

    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
            # The fixed audits.ts emits the acceptance-category contract whenever
            # an active rubric exists — independent of a version goal string — so
            # a goal-less version's report still carries per-category verdicts.
            "category_verdicts": [
                {
                    "category_key": "c-28e29fc5",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:10"}],
                },
                {
                    "category_key": "c-09e29209",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:20"}],
                },
                {
                    "category_key": "c-13867029",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:30"}],
                },
                {
                    "category_key": "c-635ee093",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:40"}],
                },
                {
                    "category_key": "c-280480cf",
                    "status": "met",
                    "citations": [{"kind": "diff", "ref": "ui.tsx:50"}],
                },
            ],
            "residual_uncaptured_gap": None,
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    candidate_sha = version.last_qualification["candidate_sha"]
    walk_task = Task(
        project_id=project.id,
        title="walkthrough",
        payloads=[{"type": "walkthrough_e2e", "base_branch": "automain"}],
        vm_instance_id="vm-walkthrough",
        status=TaskStatus.COMPLETED,
        context={"candidate_sha": candidate_sha},
    )
    db_session.add(walk_task)
    await db_session.flush()
    db_session.add(
        Artifact(
            task_id=walk_task.id,
            artifact_type=ArtifactType.WALKTHROUGH_E2E_REPORT,
            logs=json.dumps(
                {
                    "git_sha": candidate_sha,
                    "exercised_engine_sha": candidate_sha,
                    "results": [
                        {"criterion_key": "c-dfdc9d82", "passed": True},
                        {"criterion_key": "c-f52fd975", "passed": True},
                        {
                            "criterion_key": "c-workday-session-lifecycle",
                            "passed": True,
                        },
                        {"criterion_key": "c-5214996f", "passed": True},
                    ],
                }
            ),
        )
    )
    await db_session.commit()
    await db_session.refresh(walk_task)

    qual = {
        **(version.last_qualification or {}),
        "requested_probes": ["walkthrough_e2e"],
        "walkthrough_e2e_task_id": str(walk_task.id),
    }
    version.last_qualification = _with_current_qualification_contract(
        project, version, qual
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()

    # Report-contract defect → DEPLOYED-side hard blocker, NOT a transient
    # infra hold and NOT a candidate-side quality verdict.
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "walkthrough_e2e_contract_failed"
    assert result["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert result["error"] == "walkthrough_e2e_report_unknown_criteria"

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "walkthrough_e2e_contract_failed"
    assert lq["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert lq["error"] == "walkthrough_e2e_report_unknown_criteria"
    # The audit verdict survives via the eager-written per-dimension ledger —
    # the core regression.  The contract-blocker record composes the resolved
    # facts back in, exactly like the infra record did.
    assert lq["resolved_dimensions"]["audit"]["status"] == "passed"
    assert lq["resolved_dimensions"]["audit"]["sub_score"] == pytest.approx(1.0)
    # The failing probe's child is carried on the verdict for traceability.
    assert lq["walkthrough_e2e_task_id"] == str(walk_task.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "walkthrough_child_shape",
    [
        pytest.param("absent_id", id="absent_task_id"),
        pytest.param("deleted_row", id="deleted_task_row"),
    ],
)
async def test_pending_with_missing_walkthrough_task_finalizes(
    db_session, walkthrough_child_shape
):
    """A pending qualification that requested the walkthrough_e2e probe but has
    NO walkthrough child (walkthrough_e2e_task_id absent / the row was deleted)
    is not in-flight work: the reaper sweep's finalize path classifies it as a
    non-conclusive infra_failure (walkthrough_e2e_task_missing) instead of
    stranding the version in pending until the 6h qualification_timeout
    backstop.  A missing probe child is an infra-shaped probe failure, not a
    quality judgment against the candidate — the infra re-driver re-runs it.

    Regression for the stranded-pending hazard: both guards used to treat the
    missing child as non-terminal — _qualification_in_flight reported a live
    child (blocking re-entry) and required_children_terminal reported not-ready
    (blocking the sweep finalize path).  The version then sat pending with zero
    in-flight work until the wall-clock timeout mislabeled it an infra failure.
    """
    import uuid

    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    await _seed_qualification_children(
        db_session,
        project,
        version,
        audit_logs={
            "audit_score": 1.0,
            "test_erosion": {
                "deleted_test_files": [],
                "assertion_delta": 5,
                "test_count_delta": 2,
            },
        },
        smoke_logs={
            "passed": True,
            "tests": [{"name": "a", "status": "pass"}],
            "suites": {"startup": {"passed": True}},
        },
    )

    # The stranded shape: walkthrough_e2e requested but the child never
    # materialized — either the task id is absent from the record (never
    # spawned) or the id points at a task row that no longer exists.  Restamp
    # the frozen contract so finalize does not treat the fixture mutation as
    # mid-flight rubric drift, and freshen started_at so the 6h timeout
    # backstop does NOT fire first.
    wt_task_id = (
        None
        if walkthrough_child_shape == "absent_id"
        else str(uuid.uuid4())  # present id, but no matching Task row
    )
    version.last_qualification = _with_current_qualification_contract(
        project,
        version,
        {
            **(version.last_qualification or {}),
            "started_at": _utcnow().isoformat(),
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_task_id": wt_task_id,
        },
    )
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    # Both guards must agree there is no in-flight work left to wait for.
    assert not await rq._qualification_in_flight(db_session, version)
    assert await rq._required_children_terminal(db_session, version)

    # The sweep's finalize path (audit-H5 per-version dedicated session) must
    # finalize the stranded pending record rather than skip it.
    count = await rq.sweep_stuck_qualifications(
        db_session, session_factory=_shared_session_factory(db_session)
    )
    await db_session.commit()

    assert count == 1
    await db_session.refresh(version)
    lq = version.last_qualification
    # Infra-shaped probe failure (missing child) → non-conclusive infra_failure,
    # re-driven by the release-qualifier infra re-driver rather than booked as a
    # quality verdict.
    assert lq["status"] == "infra_failure"
    assert lq["error"] == "walkthrough_e2e_task_missing"
    assert lq["infra_failure_signature"].startswith("walkthrough_e2e")


def test_walkthrough_probe_contract_is_strict_and_cache_safe():
    with pytest.raises(ValueError, match="unsupported"):
        rq._normalize_qualification_probes(["made_up_probe"])
    with pytest.raises(ValueError, match="duplicate"):
        rq._normalize_qualification_probes(
            ["walkthrough_e2e", "walkthrough_e2e"]
        )
    with pytest.raises(ValueError, match="collection"):
        rq._normalize_qualification_probes("walkthrough_e2e")

    candidate = "a" * 40
    assert not rq._qualification_probe_contract_is_reusable(
        {"candidate_sha": candidate, "requested_probes": []},
        frozenset({"walkthrough_e2e"}),
    )
    assert not rq._qualification_probe_contract_is_reusable(
        {
            "candidate_sha": candidate,
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_result": {
                "status": "passed",
                "candidate_sha": candidate,
            },
        },
        frozenset({"walkthrough_e2e"}),
    )
    assert not rq._qualification_probe_contract_is_reusable(
        {
            "candidate_sha": candidate,
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_result": {
                "status": "passed",
                "candidate_sha": candidate,
                "exercised_engine_sha": "b" * 40,
            },
        },
        frozenset({"walkthrough_e2e"}),
    )
    assert rq._qualification_probe_contract_is_reusable(
        {
            "candidate_sha": candidate,
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_result": {
                "status": "passed",
                "candidate_sha": candidate,
                "exercised_engine_sha": candidate,
            },
        },
        frozenset({"walkthrough_e2e"}),
    )


def test_walkthrough_report_requires_exact_exercised_sha_and_classifies_failure():
    candidate = "b" * 40
    full_results = [
        {"criterion_key": "c-dfdc9d82", "passed": True},
        {"criterion_key": "c-f52fd975", "passed": True},
        {"criterion_key": "c-workday-session-lifecycle", "passed": True},
        {"criterion_key": "c-5214996f", "passed": True},
    ]
    missing_engine = rq._evaluate_walkthrough_e2e_report(
        {
            "git_sha": candidate,
            "results": full_results,
        },
        candidate,
    )
    assert missing_engine == {
        "status": "invalid",
        "reason": "exercised_engine_sha_mismatch",
        "candidate_sha": candidate,
        "exercised_engine_sha": None,
    }

    failed = rq._evaluate_walkthrough_e2e_report(
        {
            "git_sha": candidate,
            "exercised_engine_sha": candidate,
            "results": [
                {"criterion_key": "c-dfdc9d82", "passed": True},
                {"criterion_key": "c-f52fd975", "passed": False},
                {
                    "criterion_key": "c-workday-session-lifecycle",
                    "passed": True,
                },
                {"criterion_key": "c-5214996f", "passed": False},
            ],
        },
        candidate,
    )
    assert failed["status"] == "failed"
    assert failed["failed_criterion_keys"] == ["c-f52fd975", "c-5214996f"]
    assert rq._walkthrough_e2e_canonical_evidence_keys(failed["results"]) == {
        "c-dfdc9d82",
        "c-f52fd975",
        "c-5214996f",
    }
    decision = rq._apply_walkthrough_decision(
        {"promote": True, "blocker": None, "sub_scores": {"audit": 1.0}},
        failed,
    )
    assert decision["promote"] is False
    assert decision["blocker"] == "walkthrough_e2e_failed"
    assert decision["sub_scores"]["walkthrough_e2e"] == 0.0


def test_walkthrough_report_rejects_subset_duplicate_and_malformed_rows():
    candidate = "c" * 40
    full_results = [
        {"criterion_key": "c-dfdc9d82", "passed": True},
        {"criterion_key": "c-f52fd975", "passed": True},
        {"criterion_key": "c-workday-session-lifecycle", "passed": True},
        {"criterion_key": "c-5214996f", "passed": True},
    ]

    def evaluate(results):
        return rq._evaluate_walkthrough_e2e_report(
            {
                "git_sha": candidate,
                "exercised_engine_sha": candidate,
                "results": results,
            },
            candidate,
        )

    subset = evaluate(full_results[:-1])
    assert subset["status"] == "invalid"
    assert subset["reason"] == "result_schema_mismatch"
    assert subset["missing_criterion_keys"] == ["c-5214996f"]

    duplicate = evaluate(
        [*full_results[:-1], {"criterion_key": "c-dfdc9d82", "passed": True}]
    )
    assert duplicate["status"] == "invalid"
    assert duplicate["reason"] == "result_key_duplicate"

    malformed = evaluate(
        [
            full_results[0],
            {"criterion_key": "c-f52fd975", "passed": 1},
            *full_results[2:],
        ]
    )
    assert malformed["status"] == "invalid"
    assert malformed["reason"] == "result_entry_invalid"


def test_walkthrough_report_accepts_fresh_substantive_keys_with_active_meta():
    """A report carrying fresh open-rubric keys alongside the canonical meta row
    is an open-rubric report, not a partial legacy report.

    The canonical meta criterion c-5214996f participates in BOTH the retired v26
    report and the open rubric, so it is deliberately NOT a legacy signal in the
    partial-legacy rule (_WALKTHROUGH_E2E_SUBSTANTIVE_LEGACY_RAW_KEYS excludes
    it).  A report that carries the meta row alongside re-minted fresh
    substantive keys — the exact shape the harness now emits for a fresh+meta
    rubric (see test_meta_criterion_gate_resolves_active_rubric) — must pass the
    report validator; its keys are validated downstream against the ACTIVE
    rubric, not against the retired v26 set.
    """
    candidate = "d" * 40
    fresh_with_meta = {
        "git_sha": candidate,
        "exercised_engine_sha": candidate,
        "results": [
            {"criterion_key": "c-fresh-runner-enroll", "passed": True},
            {"criterion_key": "c-fresh-iframe-token", "passed": True},
            {"criterion_key": "c-5214996f", "passed": True},
        ],
    }
    out = rq._evaluate_walkthrough_e2e_report(fresh_with_meta, candidate)
    assert out["status"] == "passed", f"fresh+meta report must pass; got {out}"
    # No canonical collision: the fresh keys and the meta key are distinct.
    assert rq._walkthrough_e2e_canonical_evidence_keys(out["results"]) == {
        "c-fresh-runner-enroll",
        "c-fresh-iframe-token",
        "c-5214996f",
    }

    # The meta row ALONE (no substantive rows) is also an open-rubric report —
    # the meta gate downstream fails it closed, but the report is well-formed.
    meta_only = {
        "git_sha": candidate,
        "exercised_engine_sha": candidate,
        "results": [
            {"criterion_key": "c-5214996f", "passed": True},
        ],
    }
    meta_out = rq._evaluate_walkthrough_e2e_report(meta_only, candidate)
    assert meta_out["status"] == "passed", f"meta-only report must pass; got {meta_out}"


@pytest.mark.asyncio
async def test_requested_walkthrough_is_a_required_terminal_child():
    import uuid
    from types import SimpleNamespace

    attempt_id = uuid.uuid4()
    audit_id = uuid.uuid4()
    walkthrough_id = uuid.uuid4()
    version = SimpleNamespace(
        version_number=1,
        last_qualification={
            "status": "pending",
            "qualification_attempt_id": str(attempt_id),
            "audit_task_id": str(audit_id),
            "requested_probes": ["walkthrough_e2e"],
            "walkthrough_e2e_task_id": str(walkthrough_id),
        },
    )
    tasks = {
        audit_id: SimpleNamespace(
            id=audit_id,
            status=TaskStatus.COMPLETED,
            context={"qualification_attempt_id": str(attempt_id)},
        ),
        walkthrough_id: SimpleNamespace(
            id=walkthrough_id,
            status=TaskStatus.QUEUED,
            context={"qualification_attempt_id": str(attempt_id)},
        ),
    }
    session = MagicMock()
    session.get = AsyncMock(side_effect=lambda _model, task_id: tasks.get(task_id))

    assert not await rq._required_children_terminal(session, version)
    tasks[walkthrough_id].status = TaskStatus.COMPLETED
    assert await rq._required_children_terminal(session, version)


@pytest.mark.asyncio
async def test_walkthrough_e2e_lane_records_registered_criterion_evidence(
    db_session, mocker
):
    """End-to-end: the walkthrough_e2e lane spawns a PROVISIONING child pinned to
    the candidate SHA, ingests PASS results as covered-tier CriterionEvidence rows
    for all three registered keys (c-dfdc9d82, c-f52fd975, c-5214996f), and
    surfaces a loud Alert for any phantom/drifted key while writing no evidence
    for it.

    Properties verified:
      1. ACTIVE VersionAcceptanceCriterion rows for the three registered keys.
      2. run_qualification spawns exactly one PROVISIONING walkthrough_e2e child
         pinned to the verified runner + candidate SHA + qualification attempt id.
      3-4. record_walkthrough_e2e_evidence writes covered-tier CriterionEvidence
           rows (source_kind=walkthrough_e2e) for each registered key; no unmatched
           alert when all keys resolve.
      5. A phantom/drifted key in the result batch surfaces in unmatched_keys AND
         triggers a project Alert (event_type=release.walkthrough_key_unmatched)
         while writing no evidence row.
    """
    from app import shared_state
    from app.acceptance_criteria import (
        WALKTHROUGH_E2E_EVIDENCE_KIND,
        compute_criterion_coverage,
        get_evidence_for_criterion,
        record_walkthrough_e2e_evidence,
    )
    from sqlmodel import select as _sel

    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    shared_state.runtime_config["promotion_enabled"] = True

    # PROPERTY 1: project + open version; register the three emitted keys as ACTIVE criteria.
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

    REGISTERED_KEYS = ["c-dfdc9d82", "c-f52fd975", "c-5214996f"]
    for key in REGISTERED_KEYS:
        await _add_version_criterion(
            db_session,
            project,
            version,
            key=key,
            text=f"Walkthrough criterion {key}",
            sync_pending_rubric=False,
        )
    await db_session.refresh(version)

    # Fetch created criteria so we can query evidence by id later.
    all_criteria = (
        (
            await db_session.execute(
                _sel(VersionAcceptanceCriterion).where(
                    VersionAcceptanceCriterion.project_id == project.id,
                    VersionAcceptanceCriterion.release_version_id == version.id,
                )
            )
        )
        .scalars()
        .all()
    )
    criteria_by_key = {c.criterion_key: c for c in all_criteria}
    for key in REGISTERED_KEYS:
        assert key in criteria_by_key, f"Criterion {key} not registered"

    candidate = "c0de1234" * 5  # deterministic 40-char SHA

    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value={"conclusion": "success"}))

    # PROPERTY 2: run_qualification spawns exactly one PROVISIONING walkthrough_e2e child.
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
    assert version.last_qualification["candidate_sha"] == candidate

    project_tasks = (
        (
            await db_session.execute(
                _sel(Task).where(Task.project_id == project.id)
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
    assert walk.context["runner_target_id"] == str(capable_runner.id)
    assert walk.first_payload["candidate_sha"] == candidate
    assert walk.first_payload["qualification_attempt_id"] == attempt_id

    # PROPERTIES 3-4: Feed PASS results for all three registered keys plus the
    # FLOW 5 placeholder key (c-workday-session-lifecycle), required by the meta
    # gate and routed to c-5214996f via the alias table.
    # exercised_engine_sha must equal candidate_sha to satisfy the SHA-match guard.
    results_batch = [
        {"criterion_key": key, "passed": True, "notes": f"green-{key}"}
        for key in REGISTERED_KEYS
    ]
    results_batch.append({"criterion_key": "c-workday-session-lifecycle", "passed": True, "notes": "green-flow5"})
    summary = await record_walkthrough_e2e_evidence(
        db_session,
        version=version,
        candidate_sha=candidate,
        results=results_batch,
        qualification_attempt_id=attempt_id,
        exercised_engine_sha=candidate,
    )
    await db_session.commit()

    assert summary["status"] == "recorded"
    # All three registered keys must be written (meta gate passes because
    # c-dfdc9d82, c-f52fd975, and c-workday-session-lifecycle are all present
    # and passed in the same batch; the alias routes c-workday-session-lifecycle
    # to c-5214996f, so recorded=3 not 4).
    assert summary["recorded"] == 3
    assert summary["unmatched_keys"] == []

    # Each registered criterion has exactly one covered-tier walkthrough_e2e row.
    for key in REGISTERED_KEYS:
        criterion = criteria_by_key[key]
        ev_list = await get_evidence_for_criterion(db_session, criterion.id)
        walk_ev = [e for e in ev_list if e.source_kind == WALKTHROUGH_E2E_EVIDENCE_KIND]
        assert walk_ev, f"No walkthrough_e2e evidence row for registered key {key!r}"
        assert walk_ev[0].detail["candidate_sha"] == candidate
        assert (
            compute_criterion_coverage(ev_list) == "covered"
        ), f"Expected covered tier for {key!r}, got {compute_criterion_coverage(ev_list)!r}"

    # No unmatched-key alert when all keys resolved.
    no_alerts = (
        (
            await db_session.execute(
                _sel(Alert).where(
                    Alert.project_id == project.id,
                    Alert.event_type == "release.walkthrough_key_unmatched",
                )
            )
        )
        .scalars()
        .all()
    )
    assert no_alerts == [], "No unmatched-key alert expected when all keys resolve"

    # PROPERTY 5: A phantom/drifted key must appear in unmatched_keys, trigger a
    # loud Alert, and write NO evidence row.
    # NOTE: this must be a key that is neither registered NOR a recognized legacy
    # alias. 'c-workday-session-lifecycle' is intentionally NOT used here — it is a
    # live legacy alias (WALKTHROUGH_LEGACY_CRITERION_ALIASES) that resolves to the
    # active meta criterion c-5214996f, so it is not a phantom. A truly-drifted key
    # exercises the unmatched-key alert path.
    phantom_key = "c-workday-phantom-drift"
    summary2 = await record_walkthrough_e2e_evidence(
        db_session,
        version=version,
        candidate_sha=candidate,
        results=[
            {"criterion_key": REGISTERED_KEYS[0], "passed": True},
            {"criterion_key": phantom_key, "passed": True, "notes": "phantom"},
        ],
        qualification_attempt_id=f"{attempt_id}-b",
        exercised_engine_sha=candidate,
    )
    await db_session.commit()

    assert phantom_key in summary2["unmatched_keys"]
    # The real key still wrote evidence (best-effort per-row recording continues).
    assert summary2["recorded"] >= 1

    unmatched_alerts = (
        (
            await db_session.execute(
                _sel(Alert).where(
                    Alert.project_id == project.id,
                    Alert.event_type == "release.walkthrough_key_unmatched",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(unmatched_alerts) >= 1, "Expected at least one release.walkthrough_key_unmatched Alert"
    assert any(phantom_key in a.message for a in unmatched_alerts), (
        f"Alert message must mention the phantom key; got: {[a.message for a in unmatched_alerts]}"
    )

    # The phantom key must have written zero evidence rows.
    phantom_ev = (
        (
            await db_session.execute(
                _sel(CriterionEvidence).where(
                    CriterionEvidence.project_id == project.id,
                )
            )
        )
        .scalars()
        .all()
    )
    phantom_source_ids = [
        e.source_id for e in phantom_ev if phantom_key in (e.source_id or "")
    ]
    assert phantom_source_ids == [], (
        f"Phantom key must not write any CriterionEvidence rows; found: {phantom_source_ids}"
    )

