"""Audited-surface, grading-range, and walkthrough redrive tests."""
from _release_qualifier_testhelpers import *  # noqa: F403
from app import release_qualifier_audit as rqa

def test_audited_surface_seam_is_reachable_via_rq_and_reuses_verdicts():
    """The audited-surface cluster extracted into release_qualifier_audit_surface
    stays reachable through the canonical ``rq.*`` surface (re-export seam), and
    its verdict-reuse gating behaves correctly.

    Regression direction (the orchestrator_ci_monitor trap): a behaviour-preserving
    extraction that moves code out but does not keep the moved module reachable by
    the tests that cover it silently orphans that coverage by blast radius. This
    test anchors ``app.release_qualifier_audit_surface`` DIRECTLY (so the
    impacted-tests resolver selects it at hop 0) AND asserts the re-export
    identity, so the existing ``rq.*`` / mock-patch callers keep covering the
    moved implementations.
    """
    from app import release_qualifier_audit_surface as rqas

    # Re-export seam: the canonical rq.* surface must resolve to the moved
    # implementation — existing rq.* callers and
    # ``mocker.patch("app.release_qualifier.compute_audited_surface_hash", ...)``
    # keep covering this code after the extraction.
    assert rq.compute_audited_surface_hash is rqas.compute_audited_surface_hash
    assert rq._incremental_audit_base_sha is rqas._incremental_audit_base_sha
    assert rq._promotion_failed_reuse_eligible is rqas._promotion_failed_reuse_eligible
    assert rq._merge_scoped_test_erosion_with_prior_fact is (
        rqas._merge_scoped_test_erosion_with_prior_fact
    )

    # Deterministic content hash over the audited surface, excluding the
    # mechanically-regenerated artifacts an audit does not judge.
    tree = [
        {"path": "app/foo.py", "sha": "a" * 40},
        {"path": ".github/workflows/ci.yml", "sha": "b" * 40},
        {"path": "backend/testing/module_size_limits.json", "sha": "c" * 40},
        {"path": "docs/README.md", "sha": "d" * 40},
    ]
    h1 = rq.compute_audited_surface_hash(tree)
    assert h1 is not None
    # Re-ordering the tree entries must not change the hash (normalized map).
    assert rq.compute_audited_surface_hash(list(reversed(tree))) == h1
    # A real app-code change must change the surface hash.
    changed = [
        {"path": "app/foo.py", "sha": "a" * 40},
        {"path": "app/bar.py", "sha": "e" * 40},
    ]
    assert rq.compute_audited_surface_hash(changed) != h1
    # An unavailable tree fails open to None (no reuse signal).
    assert rq.compute_audited_surface_hash(None) is None

    # promotion_failed verdicts are reusable only when the live candidate diff
    # hash matches the prior verdict's (a fix landing entirely in an
    # audit-excluded path) — anything else must fall through to a re-audit.
    assert (
        rq._promotion_failed_reuse_eligible(
            {"status": "promotion_failed", "candidate_diff_hash": "x"}, "x"
        )
        is True
    )
    assert (
        rq._promotion_failed_reuse_eligible(
            {"status": "not_promoted", "candidate_diff_hash": "x"}, "x"
        )
        is False
    )
    assert (
        rq._promotion_failed_reuse_eligible(
            {"status": "promotion_failed", "candidate_diff_hash": "x"}, "y"
        )
        is False
    )
    assert rq._promotion_failed_reuse_eligible(None, "x") is False


def test_incremental_audit_base_refuses_dirty_ledger_hint():
    """A ledger-provided prior-audit SHA hint is only adopted when the prior
    audit left no verified or unverified findings.

    Regression: the hint branch of ``_incremental_audit_base_sha`` returned the
    ledger SHA before reaching ANY cleanliness check, so a prior attempt that
    DID leave findings could be adopted as an incremental base and silently drop
    those findings from the new audit's view (they sit in the skipped range).
    The hint must now honor the same precondition as the qualification-sourced
    base; when it fails, the safe (wider) base is used instead.
    """
    OLD = "a" * 40
    NEW = "b" * 40
    HINT = "c" * 40

    def clean(**overrides):
        record = {
            "status": "not_promoted",
            "candidate_sha": OLD,
            "sub_scores": {"audit": 1.0},
            "audit_findings": [],
            "excluded_audit_findings": [],
            "audit_findings_total": 0,
            "excluded_audit_findings_total": 0,
        }
        record.update(overrides)
        return record

    # Clean prior audit: the hint is safe and is adopted (distinct from the
    # qualification-sourced base, proving the hint itself is returned).
    assert (
        rq._incremental_audit_base_sha(clean(), NEW, prior_audit_candidate_sha=HINT)
        == HINT
    )

    # Dirty prior audit: the hint must be refused and the safe wider base used
    # instead — identical to the no-hint (full-audit) result, never the hint.
    for dirty in (
        clean(audit_findings=[{"summary": "x"}]),
        clean(excluded_audit_findings=[{"summary": "x"}]),
        clean(audit_findings_total=3),
    ):
        assert (
            rq._incremental_audit_base_sha(
                dirty, NEW, prior_audit_candidate_sha=HINT
            )
            is None
        )
        assert rq._incremental_audit_base_sha(
            dirty, NEW, prior_audit_candidate_sha=HINT
        ) == rq._incremental_audit_base_sha(dirty, NEW)


def test_finding_clearance_proof_is_pinned_to_the_landed_candidate():
    """Audit remediation credit requires complete, exact current-tree proof."""
    from app.acceptance_gate import (
        FINDING_CLEARANCE_VERIFICATION_KEY,
        finding_clearance_blocks_success,
    )
    from app.landed_signal import LANDED_MERGE_SHA_KEY

    merge_sha = "a" * 40
    claimed = {"resolving_finding_keys": ["finding-a", "finding-b"]}
    assert finding_clearance_blocks_success(claimed) is True

    verified = {
        **claimed,
        LANDED_MERGE_SHA_KEY: merge_sha,
        FINDING_CLEARANCE_VERIFICATION_KEY: {
            "state": "verified",
            "finding_keys": ["finding-a", "finding-b"],
            "verified_against_sha": merge_sha,
        },
    }
    assert finding_clearance_blocks_success(verified) is False
    verified[FINDING_CLEARANCE_VERIFICATION_KEY]["verified_against_sha"] = "b" * 40
    assert finding_clearance_blocks_success(verified) is True


async def _finding_clearance_setup(db_session, *, claimed: list[str]):
    """A merged task claiming findings, plus the version that carries them."""
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": "c" * 40,
        "audit_findings": [
            {
                "key": "finding-a",
                "severity": "high",
                "file": "backend/app/foo.py",
                "blob_sha": "old-blob-a",
                "summary": "a",
            },
            {
                "key": "finding-b",
                "severity": "high",
                "file": "backend/app/bar.py",
                "blob_sha": "old-blob-b",
                "summary": "b",
            },
        ],
    }
    db_session.add(version)
    task = Task(
        project_id=project.id,
        title="clear findings",
        status=TaskStatus.IN_PROGRESS,
        created_by="TEST",
        payloads=[{"type": "fix", "prompt": "clear"}],
        context={"resolving_finding_keys": claimed},
    )
    db_session.add(task)
    await db_session.commit()
    return project, version, task


@pytest.mark.asyncio
async def test_finding_clearance_marker_is_produced_from_the_landed_tree(
    db_session, mocker
):
    """The credit path has a real producer: the landed tree, not the claim.

    Both claimed findings' cited blobs moved at the landed commit (one changed,
    one deleted), so the marker is ``verified`` and pinned to that exact sha —
    which is what ``finding_clearance_blocks_success`` requires for credit.
    """
    from app.acceptance_gate import (
        FINDING_CLEARANCE_VERIFICATION_KEY,
        finding_clearance_blocks_success,
        verify_finding_clearance_at_landing,
    )
    from app.landed_signal import LANDED_MERGE_SHA_KEY

    merge_sha = "d" * 40
    _project, _version, task = await _finding_clearance_setup(
        db_session, claimed=["finding-a", "finding-b"]
    )
    mocker.patch(
        "app.github.get_file_blob_sha_at_ref",
        new=AsyncMock(
            side_effect=[(True, "new-blob-a"), (False, None)]
        ),
    )

    marker = await verify_finding_clearance_at_landing(
        task,
        db_session,
        merge_commit_sha=merge_sha,
        github_credentials=("token", "acme", "widget"),
    )

    assert marker["state"] == "verified"
    assert sorted(marker["finding_keys"]) == ["finding-a", "finding-b"]
    assert marker["verified_against_sha"] == merge_sha
    assert task.context[FINDING_CLEARANCE_VERIFICATION_KEY] == marker
    task.context[LANDED_MERGE_SHA_KEY] = merge_sha
    assert finding_clearance_blocks_success(task.context) is False


@pytest.mark.asyncio
async def test_finding_clearance_marker_fails_closed_on_unchanged_surface(
    db_session, mocker
):
    """An unchanged cited blob, and an unreadable one, never earn credit."""
    from app.acceptance_gate import (
        finding_clearance_blocks_success,
        verify_finding_clearance_at_landing,
    )
    from app.landed_signal import LANDED_MERGE_SHA_KEY

    merge_sha = "e" * 40
    _project, _version, task = await _finding_clearance_setup(
        db_session, claimed=["finding-a", "finding-b"]
    )
    mocker.patch(
        "app.github.get_file_blob_sha_at_ref",
        new=AsyncMock(side_effect=[(True, "old-blob-a"), (None, None)]),
    )

    marker = await verify_finding_clearance_at_landing(
        task,
        db_session,
        merge_commit_sha=merge_sha,
        github_credentials=("token", "acme", "widget"),
    )

    assert marker["state"] == "unverified"
    assert marker["finding_keys"] == []
    task.context[LANDED_MERGE_SHA_KEY] = merge_sha
    assert finding_clearance_blocks_success(task.context) is True


@pytest.mark.asyncio
async def test_finding_clearance_verification_skips_unclaimed_tasks(db_session):
    """A task that claims no findings is untouched — no marker, no GitHub call."""
    from app.acceptance_gate import (
        FINDING_CLEARANCE_VERIFICATION_KEY,
        verify_finding_clearance_at_landing,
    )

    _project, _version, task = await _finding_clearance_setup(db_session, claimed=[])

    assert (
        await verify_finding_clearance_at_landing(
            task, db_session, merge_commit_sha="f" * 40
        )
        is None
    )
    assert FINDING_CLEARANCE_VERIFICATION_KEY not in task.context


def test_release_qualifier_module_size_cap_is_governing():
    """release_qualifier.py's size cap is a governing value, not dead headroom.

    The cap was loosened 10300 -> 11900 when the module briefly touched 10309
    lines (rollup #16848), then d68d3e1 extracted the audited-surface cluster and
    the module dropped to ~9890 lines while the loosened cap stayed. A cap with
    ~2k lines of dead headroom cannot fire the guardrail until the file grows to
    an unreasonable size, so this test pins the module's own declared governing
    cap and cross-checks the JSON entry against it. The JSON cap and the module
    constant must stay in sync (change both together), and the headroom must stay
    a small explicit margin — an over-loosened cap regresses red instead of
    silently widening the gap.
    """
    import json
    import os

    testing_dir = os.path.dirname(__file__)
    with open(
        os.path.join(testing_dir, "module_size_limits.json"), encoding="utf-8"
    ) as fh:
        limits = json.load(fh)
    cap = limits["backend"]["release_qualifier.py"]

    assert cap == rq._MODULE_SIZE_GOVERNANCE_CAP, (
        f"release_qualifier.py cap {cap} drifted from the module's declared "
        f"governing cap {rq._MODULE_SIZE_GOVERNANCE_CAP} — keep "
        "backend/testing/module_size_limits.json and the module constant in sync"
    )

    with open(
        os.path.join(testing_dir, "..", "app", "release_qualifier.py"),
        encoding="utf-8",
    ) as fh:
        count = len(fh.readlines())

    assert count <= cap, (
        f"release_qualifier.py has {count} lines (cap: {cap}) — the module "
        "exceeds its governing cap"
    )

    # A governing cap sits close to the actual size. The 11900 value left ~2000
    # lines of dead headroom (~20% of the module), so the guardrail could not
    # fire until the file grew to an unreasonable size; require the headroom
    # under the governing cap to stay a small explicit margin (<10% of the cap).
    headroom = cap - count
    assert headroom < cap * 0.10, (
        f"release_qualifier.py cap {cap} is {headroom} lines above the module's "
        f"{count} lines — the cap has drifted into dead headroom and cannot govern"
    )


@pytest.mark.asyncio
async def test_goal_category_grading_uses_full_version_delta_while_audit_stays_incremental(
    db_session, mocker
):
    """The audit child grades goal alignment / acceptance categories over the FULL
    version delta even when its code-quality review is scoped to an incremental
    prior-audit base.

    Regression for the requalification ratchet: ``_incremental_audit_base_sha``
    narrows the release_audit child's reviewed range to the prior clean audit's
    candidate.  That narrowing is correct for code-quality findings (unchanged
    paths carry forward from the prior audit fact) but WRONG for goal alignment
    and acceptance categories, which have NO carry-forward and must grade the
    whole version the promotion PR would carry (``main...candidate``).  The
    spawned payload must therefore carry BOTH the incremental ``audit_base_sha``
    (code-quality stays incremental) AND a ``goal_category_diff_range`` pinned
    to the full version delta (goal/category grading spans it)."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.goal = "STABILIZE — reduce pipeline friction"

    # A prior clean-audited not_promoted verdict at an OLDER candidate.  The
    # dimension ledger is empty for the new candidate, so
    # ``_incremental_audit_base_sha`` derives the delta base from
    # ``last_qualification`` (conclusive status + numeric audit sub-score + zero
    # findings ⇒ the prior candidate is an eligible incremental baseline).
    prior_candidate = "1111aaaa" * 5
    version.last_qualification = {
        "status": "not_promoted",
        "candidate_sha": prior_candidate,
        "sub_scores": {"audit": 0.9},
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    candidate = "bbbb2222" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    audit_tasks = [
        t for t in tasks if (t.first_payload or {}).get("type") == "release_audit"
    ]
    assert len(audit_tasks) == 1, (
        f"expected 1 release_audit task, got {len(audit_tasks)}"
    )
    payload = audit_tasks[0].first_payload
    # The code-quality review stays incremental — scoped to the prior clean
    # audit's candidate.  The backend's scoped-findings carry-forward merge keys
    # off this base, so it must survive alongside the full-range goal grading.
    assert payload.get("audit_base_sha") == prior_candidate
    # The goal/category grading range spans the FULL version delta — the same
    # range the promotion PR would carry — independent of the incremental base.
    full_range = f"main...{candidate}"
    assert payload.get("diff_range") == full_range
    assert payload.get("goal_category_diff_range") == full_range


@pytest.mark.asyncio
async def test_release_audit_payload_carries_candidate_range_authorship_evidence_and_contract(
    db_session, mocker
):
    """The spawned release_audit payload's release_evidence must reflect the
    candidate's REAL measured base-to-head authorship receipt and the active
    authorship criterion's evidence_contract — not the hardcoded
    ``missing_or_invalid``/empty-contracts degrade the spawn call used to emit
    unconditionally.

    Regression: ``_spawn_release_audit_task`` was called with a literal
    ``None`` receipt and a ``categories`` projection stripped of
    ``evidence_contract``, so the audit's ``release_evidence`` always reported
    state ``missing_or_invalid`` with an empty ``contracts`` list, even though
    the candidate's authorship budget was measurable and declared. The goal
    grader can only machine-attest a declared candidate-range authorship
    budget when the payload actually carries the measured receipt and the
    contract."""
    from app import shared_state

    shared_state.runtime_config["smoketest_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    version.goal = "STABILIZE — declared authorship budget"
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    candidate = "cccc3333" * 5  # 40-char placeholder SHA
    mocker.patch(
        "app.github.get_repo_installation_token",
        new=AsyncMock(return_value="gh-token"),
    )
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch(
        "app.github.get_check_runs_cached",
        new=AsyncMock(return_value={"status": "completed", "conclusion": "success"}),
    )
    mocker.patch(
        "app.release_qualifier.resolve_project_promotion_enabled",
        return_value=True,
    )

    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    await db_session.commit()

    assert result["status"] == "spawned"

    tasks = (
        (await db_session.execute(select(Task).where(Task.project_id == project.id)))
        .scalars()
        .all()
    )
    audit_tasks = [
        t for t in tasks if (t.first_payload or {}).get("type") == "release_audit"
    ]
    assert len(audit_tasks) == 1, (
        f"expected 1 release_audit task, got {len(audit_tasks)}"
    )
    payload = audit_tasks[0].first_payload
    release_evidence = payload.get("release_evidence") or {}
    authorship_evidence = release_evidence.get("operator_authored_commits") or {}

    # The autouse _qualifier_ci_resolves_green fixture measures the candidate
    # range as a clean, resolved receipt (count=0) -- the payload must carry
    # THAT, not the always-missing degrade branch the bug produced.
    assert authorship_evidence.get("state") == "resolved"
    assert authorship_evidence.get("count") == 0

    # The default operator-authorship criterion (opened on every new version
    # by ensure_operator_authorship_criterion) declares max_count=0 -- its
    # evidence_contract must survive the categories projection into the
    # audit payload instead of being stripped to {category_key, text} only.
    contracts = release_evidence.get("contracts") or []
    assert {
        "type": "operator_authored_commits",
        "predicate": {"max_count": 0},
    } in contracts

    categories = payload.get("categories") or []
    authorship_category = next(
        (c for c in categories if c.get("category_key") == "c-operator-authorship"),
        None,
    )
    assert authorship_category is not None
    assert authorship_category.get("evidence_contract") == {
        "type": "operator_authored_commits",
        "predicate": {"max_count": 0},
    }


@pytest.mark.asyncio
async def test_finalize_holds_when_audit_graded_a_narrowed_goal_category_range(
    db_session,
):
    """Finalize HOLDS when the audit report's echoed goal_category_diff_range
    disagrees with the full version delta the promotion would carry.

    ``goal_category_diff_range`` is pinned at spawn to ``base_branch...candidate``
    so goal/acceptance-category grading spans the full version delta (goal
    verdicts have no scoped carry-forward).  The agent echoes the range it
    actually graded into ``audit_scope.goal_category_diff_range``, but the
    backend previously never read that echo — a grader could report the full
    range while grading a narrowed incremental window and every gate would
    pass.  The finalize-time read site closes that gap by holding the
    qualification via _handle_infra_failure."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate = "abcd" * 10
    await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {"deleted_test_files": []},
            "audit_scope": {
                "mode": "full",
                # A narrowed echo — the grader claims a SMALLER window than the
                # full version delta the promotion PR would carry.
                "goal_category_diff_range": "main...0000111122223333",
            },
        },
    )
    _pre_gates = {
        "ci_green": True,
        "ci_resolution": {"state": "success", "resolved_sha": candidate},
        "migration_verdict": "ok",
        "migration_detail": None,
        "apply_verdict": "ok",
        "apply_detail": None,
    }

    result = await rq._finalize_qualification(
        db_session, version, _pre_gates=_pre_gates
    )
    await db_session.commit()

    assert result["status"] == "infra_failure"
    assert result["error"] == "audit_goal_category_range_narrowed"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "infra_failure"
    assert version.last_qualification["error"] == "audit_goal_category_range_narrowed"


@pytest.mark.asyncio
async def test_finalize_accepts_audit_that_graded_the_full_goal_category_range(
    db_session,
):
    """A report whose echoed goal_category_diff_range IS the full version delta
    finalizes normally — proving the read-site check is not an always-true
    hold."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = False

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    candidate = "abcd" * 10
    await _seed_audit_only_pending_qualification(
        db_session,
        project,
        version,
        audit_status=TaskStatus.COMPLETED,
        audit_logs={
            "audit_score": 1.0,
            "findings": [],
            "test_erosion": {"deleted_test_files": []},
            "audit_scope": {
                "mode": "full",
                "goal_category_diff_range": f"main...{candidate}",
            },
        },
    )
    _pre_gates = {
        "ci_green": True,
        "ci_resolution": {"state": "success", "resolved_sha": candidate},
        "migration_verdict": "ok",
        "migration_detail": None,
        "apply_verdict": "ok",
        "apply_detail": None,
    }

    result = await rq._finalize_qualification(
        db_session, version, _pre_gates=_pre_gates
    )
    await db_session.commit()

    assert result["status"] == "dry_run"
    await db_session.refresh(version)
    assert version.last_qualification["status"] == "dry_run"


# ---------------------------------------------------------------------------
# Walkthrough evidence-contract re-driver
# (_redrive_walkthrough_contract_qualification)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_walkthrough_contract_failed_is_redriven_within_cap(
    db_session, mocker, monkeypatch
):
    """Acceptance evidence for the walkthrough-contract re-drive path: a version
    blocked on ``walkthrough_e2e_contract_failed`` — a REAL ``not_promoted``
    verdict produced by ``_finalize_qualification`` from a walkthrough report
    whose keys do not resolve to the active rubric (a deployed-side contract
    defect) — is automatically re-driven by the sweep re-driver within a bounded
    cap.

    Below the cap the re-driver re-enters ``run_qualification`` through the
    sweep's sanctioned path (``auto_trigger=True``, never ``explicit_request``)
    and a fresh qualification spawns.  The re-drive is bounded: once
    ``walkthrough_contract_redrive_count`` reaches the cap the re-driver halts
    (no spawn) and raises exactly one operator Alert, deduped on
    ``walkthrough_contract_redrive_loop_escalated`` across subsequent sweeps —
    a deployed site that keeps rejecting reports surfaces to a human instead of
    spinning fresh qualification attempts forever.
    """
    from app import shared_state
    from app.release_qualifier_redrive import (
        _MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS,
        _redrive_walkthrough_contract_qualification,
    )

    shared_state.runtime_config["promotion_enabled"] = True
    # The serving engine sha the DEPLOYED validator stamps on the contract
    # failure record (monkeypatched like the other engine-sha tests).
    monkeypatch.setenv("DEPLOY_SHA", "abc1234def5678")

    project, version = await _make_project_and_version(db_session, phase="stabilize")
    # A rubric whose ACTIVE keys do NOT include the walkthrough report's keys —
    # the report is a REPORT-CONTRACT defect (unknown criteria) resolved by the
    # deployed qualifier, recorded as a durable not_promoted verdict.
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
            # Without this section the merge-grade gate HOLDs on
            # merge_grade_grader_lacks_category_verdicts before the walkthrough
            # contract check can run.
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

    # Produce the REAL not_promoted / walkthrough_e2e_contract_failed verdict.
    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    assert result["status"] == "not_promoted"
    assert result["blocker"] == "walkthrough_e2e_contract_failed"

    await db_session.refresh(version)
    lq = version.last_qualification
    assert lq["status"] == "not_promoted"
    assert lq["blocker"] == "walkthrough_e2e_contract_failed"
    assert lq["execution_site"] == rs.EXECUTION_SITE_DEPLOYED
    assert lq["walkthrough_contract_redrive_count"] is None
    # Observability: the record stamps the serving engine sha that produced it.
    assert lq["engine_sha"] == "abc1234def5678"

    # Below the cap: the re-driver re-enters the qualifier and a fresh
    # qualification spawns.
    mock_run_qual = mocker.patch(
        "app.release_qualifier.run_qualification",
        new=mocker.AsyncMock(return_value={"status": "spawned"}),
    )
    redriven = await _redrive_walkthrough_contract_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert redriven is True, (
        "a walkthrough contract-failed version below the cap must be re-driven"
    )
    mock_run_qual.assert_called_once()
    call_kwargs = mock_run_qual.await_args.kwargs
    assert call_kwargs.get("auto_trigger") is True, (
        "the walkthrough-contract re-drive is the sweep's sanctioned "
        "attempt-scoped recovery path, never a claim of explicit_request"
    )
    assert call_kwargs.get("explicit_request") is not True

    await db_session.refresh(version)
    assert version.last_qualification["walkthrough_contract_redrive_count"] == 1, (
        "a successful re-drive must advance the per-episode counter so the cap "
        "holds under repeated contract failures"
    )

    # At the cap: the re-driver halts (no spawn) and raises exactly one
    # operator Alert, deduped across subsequent sweep cycles.
    version.last_qualification = {
        **version.last_qualification,
        "walkthrough_contract_redrive_count": _MAX_WALKTHROUGH_CONTRACT_REDRIVE_ATTEMPTS,
    }
    db_session.add(version)
    await db_session.commit()
    await db_session.refresh(version)

    halted = await _redrive_walkthrough_contract_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    assert halted is False, "a contract-failed version AT the cap must halt"
    mock_run_qual.assert_called_once()  # no additional spawn

    alerts = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts) == 1, "reaching the re-drive cap raises exactly one Alert"
    assert "walkthrough" in alerts[0].title.lower()
    await db_session.refresh(version)
    assert (
        version.last_qualification.get("walkthrough_contract_redrive_loop_escalated")
        is True
    )

    # Deduped: a second sweep cycle at the cap does not spam another Alert.
    await _redrive_walkthrough_contract_qualification(
        version.id, session_factory=_shared_session_factory(db_session)
    )
    alerts_again = (
        (
            await db_session.execute(
                select(Alert).where(Alert.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(alerts_again) == 1


def _approved_task(project_id, title, pr_number, *, merged=False, target=True):
    """An approved review loop, optionally already landed / without a target."""
    base = {"base_sha": "base" * 10, "head_sha": "head" * 10}
    review_loop = {"review_approved": True}
    if target is True:
        review_loop["review_target"] = {**base, "pr_number": pr_number}
    elif target is not None:
        review_loop["review_target"] = target
    return Task(
        project_id=project_id,
        title=title,
        status=TaskStatus.COMPLETED,
        context={
            "review_loop": review_loop,
            **({"pr_merged": True} if merged else {}),
        },
    )


@pytest.mark.asyncio
async def test_qualification_reports_stranded_approved_prs(db_session, mocker):
    """Qualification records the bounded approved-but-unlanded PR snapshot."""
    from app import shared_state

    shared_state.runtime_config["promotion_enabled"] = True
    shared_state.runtime_config["smoketest_enabled"] = False
    shared_state.runtime_config["env_preflight_enabled"] = False
    project, version = await _make_project_and_version(db_session, phase="stabilize")
    db_session.add_all([
        _approved_task(project.id, "approved review", 701),
        _approved_task(project.id, "duplicate review", 701),
        _approved_task(project.id, "already landed review", 702, merged=True),
    ])
    reconciled_rollup_child = _approved_task(project.id, "reconciled rollup child", 703)
    reconciled_rollup_child.context.update(
        {
            "pr_merged": True,
            "landed_on_integration": False,
            "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
            "approved_pr_stranding": {
                "state": "landed",
                "rollup_subsumed": True,
            },
        }
    )
    db_session.add(reconciled_rollup_child)
    await db_session.commit()

    snapshot = await rq.snapshot_stranded_approved_prs(db_session, project.id)
    assert snapshot["status"] == "available"
    assert snapshot["count"] == 1
    assert snapshot["pr_numbers"] == [701]

    candidate = "qualify0" * 5
    mocker.patch("app.github.get_repo_installation_token", new=AsyncMock(return_value="gh-token"))
    mocker.patch("app.github.get_branch_sha", new=AsyncMock(return_value=candidate))
    mocker.patch("app.github.get_check_runs_cached", new=AsyncMock(return_value={"conclusion": "success"}))
    result = await rq.run_qualification(
        db_session, project.id, version, explicit_request=True
    )
    assert result["status"] == "spawned"
    await db_session.commit()
    await db_session.refresh(version)
    assert version.last_qualification["stranded_approved_prs"]["count"] == 1

    audit_task = await db_session.get(
        Task, uuid.UUID(version.last_qualification["audit_task_id"])
    )
    audit_task.status = TaskStatus.COMPLETED
    db_session.add(audit_task)
    db_session.add(
        Artifact(
            task_id=audit_task.id,
            artifact_type=ArtifactType.RELEASE_AUDIT_REPORT,
            logs=json.dumps({"findings": []}),
        )
    )
    shared_state.runtime_config["promotion_enabled"] = False
    await db_session.commit()

    result = await rq._finalize_qualification(db_session, version)
    await db_session.commit()
    assert result["status"] in {"dry_run", "promoted"}

    await db_session.refresh(version)
    assert version.last_qualification["stranded_approved_prs"]["count"] == 1
    from app.release_readiness import format_qualification_for_api

    assert format_qualification_for_api(version.last_qualification)[
        "stranded_approved_prs"
    ]["count"] == 1


@pytest.mark.asyncio
async def test_stranded_snapshot_filters_landed_rows_before_the_scan_cap(db_session):
    """A mature project's merged approvals must not exhaust the bounded scan.

    ``review_approved`` persists after a merge and archival is manual, so if the
    landed filter ran in Python AFTER the cap the readout would be permanently
    ``unavailable`` on any real project — the feature would ship dark.
    """
    project, _version = await _make_project_and_version(db_session)
    direct_landed = [
        _approved_task(project.id, f"landed {n}", 1000 + n, merged=True)
        for n in range(10)
    ]
    reconciled_rollup_children = []
    for n in range(rqa._STRANDED_APPROVED_PR_SCAN_LIMIT + 50):
        child = _approved_task(project.id, f"reconciled child {n}", 2000 + n)
        child.context.update(
            {
                "pr_merged": True,
                "landed_on_integration": False,
                "_non_merge_unblock_reason": "merged_pr_non_integration_branch",
                "approved_pr_stranding": {
                    "state": "landed",
                    "rollup_subsumed": True,
                },
            }
        )
        reconciled_rollup_children.append(child)
    db_session.add_all(
        direct_landed + reconciled_rollup_children + [_approved_task(project.id, "stranded", 999)]
    )
    await db_session.commit()

    snapshot = await rq.snapshot_stranded_approved_prs(db_session, project.id)
    assert snapshot["status"] == "available"
    assert snapshot["pr_numbers"] == [999]


@pytest.mark.asyncio
async def test_stranded_snapshot_distinguishes_reland_and_superseded_records(db_session):
    """The qualification readout stays truthful while bounded recovery runs."""
    project, _version = await _make_project_and_version(db_session)
    relanding = _approved_task(project.id, "relanding", 800)
    relanding.status = TaskStatus.EVALUATING
    relanding.context["approved_pr_reland"] = {
        "state": "in_flight",
        "identity": "review-target-800",
        "attempt_count": 1,
    }
    relanding.context["review_loop"]["ci_waiting"] = True
    relanding.context["review_loop"]["pr_number"] = 800
    relanding.context = dict(relanding.context)
    superseded = _approved_task(project.id, "superseded", 801)
    superseded.context["approved_pr_reland"] = {
        "state": "superseded",
        "identity": "review-target-801",
        "attempt_count": 1,
        "reason": "immutable review target was replaced",
    }
    exhausted = _approved_task(project.id, "closed unmerged re-drive", 802)
    exhausted.status = TaskStatus.FAILED
    exhausted.context["approved_pr_reland"] = {
        "state": "in_flight",
        "identity": "review-target-802",
        "attempt_count": 1,
    }
    exhausted.context = dict(exhausted.context)
    db_session.add_all([relanding, superseded, exhausted])
    await db_session.commit()

    snapshot = await rq.snapshot_stranded_approved_prs(db_session, project.id)

    assert snapshot["status"] == "available"
    assert snapshot["count"] == 1
    assert snapshot["pr_numbers"] == [802]
    assert snapshot["unlanded_approved_pr_count"] == 2
    assert snapshot["reland_inflight_count"] == 1
    assert snapshot["superseded_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("gate", "value"),
    [
        ("requires_approval", True),
        ("conflict_fix_spawned", "still-running-conflict-fix"),
        ("ci_fix_spawned", "still-running-ci-fix"),
        ("merge_rule_blocked", True),
        ("missing_pr_number", None),
    ],
)
async def test_stranded_snapshot_surfaces_monitor_skipped_relands_as_actionable(
    db_session, gate, value, monkeypatch
):
    """An in-flight receipt is live only while the canonical monitor can poll it."""
    monkeypatch.setitem(rq._runtime_config, "require_approval_gates", True)
    project, _version = await _make_project_and_version(db_session)
    relanding = _approved_task(project.id, f"monitor-skipped {gate}", 803)
    relanding.status = TaskStatus.EVALUATING
    relanding.context["approved_pr_reland"] = {
        "state": "in_flight",
        "identity": "review-target-803",
        "attempt_count": 1,
    }
    relanding.context["review_loop"]["ci_waiting"] = True
    relanding.context["review_loop"]["pr_number"] = 803
    if gate == "requires_approval":
        relanding.requires_approval = value
    elif gate != "missing_pr_number":
        relanding.context["review_loop"][gate] = value
    else:
        relanding.context["review_loop"].pop("pr_number", None)
    relanding.context = dict(relanding.context)
    db_session.add(relanding)
    await db_session.commit()

    snapshot = await rq.snapshot_stranded_approved_prs(db_session, project.id)

    assert snapshot["count"] == 1
    assert snapshot["pr_numbers"] == [803]
    assert snapshot["details"][0]["reland_state"] == "exhausted"
    assert snapshot["reland_inflight_count"] == 0


@pytest.mark.asyncio
async def test_stranded_snapshot_degrades_per_row_not_project_wide(db_session):
    """One approval without a usable review_target must not void the readout."""
    project, _version = await _make_project_and_version(db_session)
    db_session.add_all(
        [
            _approved_task(project.id, "legacy no target", 0, target=None),
            _approved_task(project.id, "malformed target", 0, target="not-a-dict"),
            _approved_task(project.id, "no pr number", 0, target={"head_sha": "abc"}),
            _approved_task(project.id, "real stranding", 4242),
        ]
    )
    await db_session.commit()

    snapshot = await rq.snapshot_stranded_approved_prs(db_session, project.id)
    assert snapshot["status"] == "available"
    assert snapshot["pr_numbers"] == [4242]
    assert snapshot["unparseable_approvals"] == 3


@pytest.mark.asyncio
async def test_stranded_snapshot_failure_does_not_poison_caller_transaction(
    db_session, mocker
):
    """A failed scan is absorbed by a SAVEPOINT, leaving the caller usable.

    ``run_qualification`` calls this while holding the version row's FOR UPDATE
    lock inside an open transaction; swallowing the exception without a nested
    rollback would leave that transaction aborted on PostgreSQL.
    """
    project, _version = await _make_project_and_version(db_session)
    db_session.add(_approved_task(project.id, "stranded", 777))
    await db_session.commit()

    # Force the SELECT itself to fail so the SAVEPOINT path is exercised, not
    # just a pre-query raise.
    from sqlalchemy import func as _sa_func

    mocker.patch.object(
        rqa,
        "_stranded_approved_pr_columns",
        return_value=(
            _sa_func.blueprint_no_such_function(rqa.Task.context),
            rqa.Task.context,
        ),
    )
    snapshot = await rq.snapshot_stranded_approved_prs(db_session, project.id)
    assert snapshot["status"] == "unavailable"
    assert snapshot["count"] is None

    # The caller's session is still usable for subsequent statements.
    mocker.stopall()
    still_works = await rq.snapshot_stranded_approved_prs(db_session, project.id)
    assert still_works["status"] == "available"
