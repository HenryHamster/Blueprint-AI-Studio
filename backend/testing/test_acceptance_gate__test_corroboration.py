"""Backend-side corroboration of declared test existence at the merge commit.

Split out of ``test_acceptance_gate.py`` (PR #16956 regression suite): the
parent module is a chronic size-cap offender in
``testing/module_size_limits.json``, so this self-contained section lives on its
own rather than pushing the parent past its cap. Behavior is unchanged — the
shared factory ``_make_code_task`` is imported from the parent so there is
exactly one definition of it.

The gate previously trusted the agent's ``present`` word; the backend must
independently verify the declared path/test-id in the merged tree and fail
closed on a corroborated absence. Standalone in-memory tests; run under
``bash scripts/test-backend.sh --standalone``.
"""

import pytest

from app.acceptance_gate import (
    acceptance_blocks_success,
    classify_unsatisfied_reason,
    evaluate_code_acceptance,
    record_code_acceptance_verdict,
)
from app.models import Task
from app.payload_schemas import AcceptanceEvidence
from testing.test_acceptance_gate import _make_code_task


# ─────────────────────────────────────────────────────────────────────────────


def test_corroborated_absence_overrides_agent_present_word():
    """A backend-refuted declared test is a non-existence verdict even when the
    agent reported the test present AND green. The gate must fail closed rather
    than silently credit a test the backend can prove exists nowhere."""
    from app.acceptance_gate import (
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        _acceptance_test_exists_at_merge,
        _acceptance_test_ran_green_at_merge,
        _declared_named_test_path,
    )

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "testing/test_nowhere.py",
            "test_id": "test_x",
        },
        changed_files=["app/seam.py"],
        targeted_green=True,  # agent claims it ran the declared test
        merge_test_block={
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    )
    # Backend corroboration ran against the merged tree and refuted the path.
    task.context = {
        **(task.context or {}),
        ACCEPTANCE_TEST_CORROBORATED_KEY: False,
    }
    declared = _declared_named_test_path(task.first_payload)
    assert _acceptance_test_exists_at_merge(task.context, declared) is False
    assert _acceptance_test_ran_green_at_merge(task.context, declared) is False


def test_corroborated_absence_is_a_non_existence_verdict_end_to_end():
    """End-to-end through the real writer: a backend-refuted declared test must
    not be credited — ``evaluate_code_acceptance`` must mint a non-existence
    (unproven) verdict even when the agent reported a targeted green run, exactly
    the PR #16956 fail-open the fix closes."""
    from app.acceptance_gate import (
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        evaluate_code_acceptance,
    )

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "testing/test_nowhere.py",
            "test_id": "test_x",
        },
        changed_files=["app/seam.py"],  # production change, test NOT in diff
        targeted_green=True,  # agent claims it ran the declared test green
        merge_test_block={
            # The block NAMES the declared test, so the block-corroboration
            # guard has nothing to withdraw — the only thing standing between
            # this task and a credit is the backend's refuted corroboration.
            "selector": "testing/test_nowhere.py::test_x",
            "descriptor": "testing/test_nowhere.py",
            "present": True,
            "ran": True,
            "status": "ran",
            "passed": True,
        },
    )
    # Backend corroboration refuted the declared path in the merged tree.
    task.context = {
        **(task.context or {}),
        ACCEPTANCE_TEST_CORROBORATED_KEY: False,
    }
    verdict = evaluate_code_acceptance(task)
    assert verdict.proven is False, (
        f"corroborated-absent test must not be credited: {verdict.reason}"
    )
    assert "not among the PR's changed files" in verdict.reason


def test_truncation_credit_refuted_by_hard_corroborated_false():
    """A backend-refuted declared test refutes the TRUNCATION credit.

    On a >50-file PR the stored changed-file list is capped at 50 paths, so a
    declared test that lies beyond the stored prefix reads as absent and is
    credited soft (satisfied/unproven) — the "absence is inconclusive" branch.
    But that soft credit rests on the premise that the test "may ship beyond the
    stored prefix". A corroborated absence (``ACCEPTANCE_TEST_CORROBORATED_KEY``
    is ``False``) defeats the premise: the backend checked the EXACT merge-commit
    tree and confirmed the declared test exists NOWHERE, so there is no stored
    prefix it could lie beyond. The declared_unsatisfied hard fail applies and
    maps to ``named_test_missing_from_diff``. This is the large-rollup name-drop
    shape: a >50-file PR whose declared test never shipped must not earn the
    truncation credit.
    """
    from app.acceptance_gate import (
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        CODE_ACCEPTANCE_VERDICT_KEY,
    )

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_nowhere.py",
        },
        changed_files=["backend/app/acceptance_gate.py"],  # test absent from diff
        unverified_reason=None,
    )
    ctx = dict(task.context or {})
    ctx["_pr_changed_files_truncated"] = True
    ctx[ACCEPTANCE_TEST_CORROBORATED_KEY] = False
    task.context = ctx

    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False, (
        "corroborated-absent declared test must hard-fail even on a truncated "
        "changed-file list — absence is proven, not inconclusive"
    )
    assert verdict.proven is False
    assert "is not among the PR's changed files" in verdict.reason

    recorded = record_code_acceptance_verdict(task)
    assert recorded is not None
    assert classify_unsatisfied_reason(
        task.context[CODE_ACCEPTANCE_VERDICT_KEY]
    ) == "named_test_missing_from_diff"
    assert (
        acceptance_blocks_success(task.context[CODE_ACCEPTANCE_VERDICT_KEY]) is True
    )


def test_beyond_50_prefix_corroborated_absent_fails_closed_end_to_end():
    """End-to-end through the real ``stamp_changed_files_signal``: a >50-file PR
    whose declared test falls beyond the stored 50-path prefix AND is
    backend-refuted (corroborated absent) hard-fails — the truncation credit
    cannot survive a proven non-existence at the merge commit."""
    from app.acceptance_gate import (
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        stamp_changed_files_signal,
    )

    declared_test = "backend/testing/test_nowhere.py"
    changed = ["backend/app/acceptance_gate.py"]
    changed += [f"backend/app/module_{i:03d}.py" for i in range(53)]
    changed.append(declared_test)
    assert len(changed) > 50

    signal = stamp_changed_files_signal(changed)
    assert signal["_pr_changed_files_truncated"] is True
    assert declared_test not in signal["_pr_changed_files"]

    task = _make_code_task(
        acceptance_evidence={"kind": "named_test", "test_path": declared_test},
        unverified_reason=None,
    )
    ctx = dict(task.context or {})
    ctx.update(signal)
    ctx[ACCEPTANCE_TEST_CORROBORATED_KEY] = False
    task.context = ctx

    verdict = evaluate_code_acceptance(task)
    assert verdict.declared is True
    assert verdict.satisfied is False, (
        f"real-stamp truncation + corroborated absence must fail closed: "
        f"{verdict.reason}"
    )
    assert verdict.proven is False
    assert "is not among the PR's changed files" in verdict.reason


def test_truncated_absent_corroborated_true_keeps_soft_credit():
    """A corroborated-PRESENT declared test keeps the truncation soft credit.

    ``corroborated is True`` means the backend confirmed the test EXISTS in the
    merged tree, but that is not absence — the truncation premise (the test may
    ship beyond the stored prefix) still holds, and existence alone without a
    green run is satisfied/unproven, never a hard fail.
    """
    from app.acceptance_gate import ACCEPTANCE_TEST_CORROBORATED_KEY

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "backend/testing/test_widget.py",
        },
        changed_files=["backend/app/widget.py"],  # test absent from stored prefix
        unverified_reason=None,
    )
    ctx = dict(task.context or {})
    ctx["_pr_changed_files_truncated"] = True
    ctx[ACCEPTANCE_TEST_CORROBORATED_KEY] = True
    task.context = ctx

    verdict = evaluate_code_acceptance(task)
    assert verdict.satisfied is True, (
        "corroborated presence is not absence — the truncation soft credit holds"
    )
    assert verdict.proven is False
    assert "truncated" in verdict.reason


def test_corroborated_presence_preserves_existing_verdict():
    """A genuinely-corroborated declared test (backend confirmed the path and
    node id in the merged tree) must still mint the same existing verdict — the
    marker only overrides on a refuted absence, never on a confirmed presence."""
    from app.acceptance_gate import (
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        _acceptance_test_exists_at_merge,
        _acceptance_test_ran_green_at_merge,
        _declared_named_test_path,
    )

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "testing/test_seam.py",
            "test_id": "test_x",
        },
        changed_files=["app/seam.py"],
        targeted_green=True,
    )
    task.context = {
        **(task.context or {}),
        ACCEPTANCE_TEST_CORROBORATED_KEY: True,
    }
    declared = _declared_named_test_path(task.first_payload)
    assert _acceptance_test_exists_at_merge(task.context, declared) is True
    assert _acceptance_test_ran_green_at_merge(task.context, declared) is True


def test_missing_corroboration_marker_falls_back_to_agent_evidence():
    """When the backend has not yet computed corroboration (marker absent — e.g.
    the task landed before this fix, or the tree snapshot was unavailable) the
    predicate keeps today's behavior: a genuinely-existing test is not suppressed
    on missing data."""
    from app.acceptance_gate import (
        ACCEPTANCE_TEST_CORROBORATED_KEY,
        _acceptance_test_exists_at_merge,
        _acceptance_test_ran_green_at_merge,
        _declared_named_test_path,
    )

    task = _make_code_task(
        acceptance_evidence={
            "kind": "named_test",
            "test_path": "testing/test_seam.py",
            "test_id": "test_x",
        },
        changed_files=["app/seam.py"],
        targeted_green=True,
    )
    assert ACCEPTANCE_TEST_CORROBORATED_KEY not in (task.context or {})
    declared = _declared_named_test_path(task.first_payload)
    assert _acceptance_test_exists_at_merge(task.context, declared) is True
    assert _acceptance_test_ran_green_at_merge(task.context, declared) is True


def _corroboration_task(test_path: str, test_id: str | None = None) -> Task:
    """Task whose first payload declares a ``named_test`` at *test_path*."""
    evidence: dict = {"kind": "named_test", "test_path": test_path}
    if test_id is not None:
        evidence["test_id"] = test_id
    return _make_code_task(
        acceptance_evidence=evidence,
        changed_files=["app/seam.py"],
    )


@pytest.mark.asyncio
async def test_corroboration_confirms_declared_path_and_node():
    """A declared test whose path and node id are present in the merged tree is
    corroborated as True — the genuinely-corroborated case keeps its verdict."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import corroborate_acceptance_test_at_merge

    task = _corroboration_task(
        "backend/testing/test_seam.py", test_id="test_x"
    )
    ctx = {
        "token": "tok",
        "owner": "owner",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }
    with (
        patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value=dict(ctx)),
        ),
        patch(
            "app.github_repo_snapshot.file_text_on_branch",
            new=AsyncMock(return_value=(True, "def test_x():\n    assert 1")),
        ),
    ):
        assert (
            await corroborate_acceptance_test_at_merge(task, None) is True
        )


@pytest.mark.asyncio
async def test_corroboration_refutes_declared_but_absent_path():
    """A declared test path that is ABSENT from the merged tree is a backend
    non-existence verdict (False) — never fail-open."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import corroborate_acceptance_test_at_merge

    task = _corroboration_task(
        "backend/testing/test_nowhere.py", test_id="test_x"
    )
    ctx = {
        "token": "tok",
        "owner": "owner",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }
    with (
        patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value=dict(ctx)),
        ),
        patch(
            "app.github_repo_snapshot.file_text_on_branch",
            new=AsyncMock(return_value=(False, None)),
        ),
    ):
        assert (
            await corroborate_acceptance_test_at_merge(task, None) is False
        )


@pytest.mark.asyncio
async def test_corroboration_refutes_absent_test_id_in_present_file():
    """A declared test path that EXISTS but does not contain the declared test id
    is a backend non-existence verdict (False) — a fabricated id inside a real
    file must not pass."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import corroborate_acceptance_test_at_merge

    task = _corroboration_task(
        "backend/testing/test_seam.py", test_id="test_fabricated"
    )
    ctx = {
        "token": "tok",
        "owner": "owner",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }
    with (
        patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value=dict(ctx)),
        ),
        patch(
            "app.github_repo_snapshot.file_text_on_branch",
            new=AsyncMock(
                return_value=(True, "def test_real():\n    assert 1")
            ),
        ),
    ):
        assert (
            await corroborate_acceptance_test_at_merge(task, None) is False
        )


@pytest.mark.asyncio
async def test_corroboration_tries_backend_prefixed_variant():
    """A declaration that drops the monorepo ``backend/`` prefix still
    corroborates against the real tree via the prefixed candidate path."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import corroborate_acceptance_test_at_merge

    task = _corroboration_task(
        "testing/test_seam.py", test_id="test_x"
    )
    ctx = {
        "token": "tok",
        "owner": "owner",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }
    # First candidate (unprefixed) is absent; the backend-prefixed variant
    # exists and contains the declared id.
    with (
        patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value=dict(ctx)),
        ),
        patch(
            "app.github_repo_snapshot.file_text_on_branch",
            new=AsyncMock(
                side_effect=[(False, None), (True, "def test_x():\n    assert 1")]
            ),
        ),
    ):
        assert (
            await corroborate_acceptance_test_at_merge(task, None) is True
        )


@pytest.mark.asyncio
async def test_corroboration_indeterminate_when_snapshot_unavailable():
    """An unavailable tree snapshot (tri-state None) is indeterminate — not an
    absence — so a genuinely-existing test is not suppressed on missing data."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import corroborate_acceptance_test_at_merge

    task = _corroboration_task(
        "backend/testing/test_seam.py", test_id="test_x"
    )
    ctx = {
        "token": "tok",
        "owner": "owner",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }
    with (
        patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value=dict(ctx)),
        ),
        patch(
            "app.github_repo_snapshot.file_text_on_branch",
            new=AsyncMock(return_value=(None, None)),
        ),
    ):
        assert (
            await corroborate_acceptance_test_at_merge(task, None) is None
        )


@pytest.mark.asyncio
async def test_corroboration_no_named_test_is_none():
    """A task that declares no ``named_test`` cannot be corroborated one way or
    the other — indeterminate, not absent."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import corroborate_acceptance_test_at_merge

    task = _make_code_task(
        acceptance_evidence={"kind": "coverage_delta", "threshold": 5},
        changed_files=["app/seam.py"],
    )
    with patch(
        "app.source_ref_validation.resolve_branch_verification_context",
        new=AsyncMock(),
    ) as _resolve:
        assert await corroborate_acceptance_test_at_merge(task, None) is None
    _resolve.assert_not_awaited()


def test_declared_test_id_present_uses_word_boundaries():
    """The declared node-id check is a standalone-identifier match: a full
    pytest node reduces to its leaf, and a substring/prefix collision does not
    false-positive."""
    from app.acceptance_gate import _declared_test_id_present

    assert _declared_test_id_present("test_x", "def test_x():\n    pass") is True
    # Full pytest node id reduces to its leaf segment.
    assert (
        _declared_test_id_present(
            "testing/test_seam.py::TestSeam::test_x",
            "def test_x():\n    pass",
        )
        is True
    )
    # Prefix / substring-only matches must not be credited.
    assert _declared_test_id_present("test_x", "def test_xy():\n    pass") is False
    assert _declared_test_id_present("test_x", "# a comment about test_xz") is False
    assert _declared_test_id_present("", "def test_x():\n    pass") is False


def test_candidate_paths_cover_all_monorepo_roots_and_dedupe():
    """A declaration that drops its monorepo prefix yields a candidate under
    EVERY root (backend, admin-ui, agent) — a prefix-dropped admin-ui/agent test
    must never corroborate as a definite absence because only the backend
    variant was probed. Already-prefixed declarations are used as-is, and the
    result is deduplicated with order preserved."""
    from app.acceptance_gate import _candidate_test_paths

    assert _candidate_test_paths("src/components/X.viewport.test.tsx") == [
        "src/components/X.viewport.test.tsx",
        "backend/src/components/X.viewport.test.tsx",
        "admin-ui/src/components/X.viewport.test.tsx",
        "agent/src/components/X.viewport.test.tsx",
    ]
    # Already-prefixed declarations probe exactly one path.
    assert _candidate_test_paths("backend/testing/test_seam.py") == [
        "backend/testing/test_seam.py"
    ]
    assert _candidate_test_paths("admin-ui/src/X.test.tsx") == [
        "admin-ui/src/X.test.tsx"
    ]
    assert _candidate_test_paths("agent/src/handlers/x.test.ts") == [
        "agent/src/handlers/x.test.ts"
    ]
    # Leading ./ is stripped and never duplicates.
    assert _candidate_test_paths("./backend/testing/test_seam.py") == [
        "backend/testing/test_seam.py"
    ]


@pytest.mark.asyncio
async def test_corroboration_finds_prefix_dropped_agent_test():
    """A prefix-dropped agent-side declaration corroborates True via the
    ``agent/`` candidate instead of reading as a definite absence."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import corroborate_acceptance_test_at_merge

    task = _corroboration_task("src/handlers/seam.test.ts", test_id="handles seam")
    ctx = {
        "token": "tok",
        "owner": "owner",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }
    with (
        patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value=dict(ctx)),
        ),
        patch(
            "app.github_repo_snapshot.file_text_on_branch",
            new=AsyncMock(
                side_effect=[
                    (False, None),  # src/handlers/seam.test.ts
                    (False, None),  # backend/src/handlers/seam.test.ts
                    (False, None),  # admin-ui/src/handlers/seam.test.ts
                    (True, "it('handles seam', () => {})"),  # agent/…
                ]
            ),
        ),
    ):
        assert await corroborate_acceptance_test_at_merge(task, None) is True


@pytest.mark.asyncio
async def test_corroboration_pins_to_merge_commit_when_sha_provided():
    """When the caller knows the merge commit, the tree read is pinned to that
    exact immutable SHA — never routed through the branch-tip cache, whose TTL
    can still serve the PRE-merge tip moments after a merge and stamp a durable
    false "test absent" for a test the PR itself just added."""
    from unittest.mock import AsyncMock, patch

    from app.acceptance_gate import corroborate_acceptance_test_at_merge

    task = _corroboration_task("backend/testing/test_seam.py", test_id="test_x")
    ctx = {
        "token": "tok",
        "owner": "owner",
        "repo": "repo",
        "branch": "integration",
        "project_id": "proj-1",
    }
    commit_reader = AsyncMock(return_value=(True, "def test_x():\n    assert 1"))
    branch_reader = AsyncMock(return_value=(False, None))
    with (
        patch(
            "app.source_ref_validation.resolve_branch_verification_context",
            new=AsyncMock(return_value=dict(ctx)),
        ),
        patch("app.github_repo_snapshot.file_text_at_commit", new=commit_reader),
        patch("app.github_repo_snapshot.file_text_on_branch", new=branch_reader),
    ):
        assert (
            await corroborate_acceptance_test_at_merge(
                task, None, commit_sha="a" * 40
            )
            is True
        )
    branch_reader.assert_not_awaited()
    assert commit_reader.await_args.kwargs["commit_sha"] == "a" * 40


def test_not_in_diff_proven_requires_positive_corroboration():
    """For a declared test ABSENT from the PR's diff, every input behind the
    existence/green-run credit is agent-reported — so machine ``proven``
    additionally requires the backend's OWN tree check to have confirmed
    existence. Indeterminate corroboration credits soft (satisfied, unproven);
    positive corroboration keeps the proven mint; an in-diff test is unaffected
    because presence in the GitHub-sourced diff is already backend-grounded."""
    from app.acceptance_gate import _evaluate_named_test

    evidence = AcceptanceEvidence(
        kind="named_test", test_path="testing/test_seam.py", test_id="test_x"
    )
    # Indeterminate (None / never computed): soft credit, never proven.
    soft = _evaluate_named_test(
        evidence,
        ["app/seam.py"],
        verified=True,
        test_exists=True,
        ran_green=True,
    )
    assert soft.satisfied is True
    assert soft.proven is False
    assert "corroboration" in (soft.reason or "")
    # Positive backend corroboration: the proven mint is preserved.
    proven = _evaluate_named_test(
        evidence,
        ["app/seam.py"],
        verified=True,
        test_exists=True,
        ran_green=True,
        corroborated=True,
    )
    assert proven.satisfied is True
    assert proven.proven is True
    # In-diff: the declared test is among the PR's own changed files — proven
    # without any corroboration marker, exactly as before.
    in_diff = _evaluate_named_test(
        evidence,
        ["app/seam.py", "testing/test_seam.py"],
        verified=True,
    )
    assert in_diff.satisfied is True
    assert in_diff.proven is True
