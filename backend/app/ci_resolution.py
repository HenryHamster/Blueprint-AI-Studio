"""Strict CI resolution for the orchestrator's PR-merge hot path.

The shared ``github.get_check_runs`` helper defaults to
``treat_missing_as_success=True`` — when a commit has zero check-runs it
returns ``status="completed", conclusion="success"``.  That fail-open exists so
the orchestrator's CI-wait does not hang forever on repos with no CI; it is
wrong for PR-driven CI configs where the ``pull_request`` event runs on
GitHub's synthetic *merge-commit* SHA rather than on the branch HEAD.  In that
shape the branch HEAD legitimately carries zero check-runs while the PR-event
merge SHA carries the real verdict — the lenient default makes the orchestrator
mark CI green on the bare HEAD, attempt to merge, get refused with
"checks in progress", reset its CI-wait clock, and spin until the 60-min
backstop fires.

``resolve_pr_ci_status`` is the orchestrator-facing fix.  It scopes the
qualifier's strict resolution path (originally lifted from
``release_qualifier._resolve_candidate_ci``) to the PR-merge hot path:

  1. Inspect check-runs on the branch HEAD in **strict** mode (empty list →
     ``no_checks``, not a pass).  A completed verdict here is authoritative.
  2. When the HEAD carries no check-runs, resolve through
     ``commits/{sha}/pulls`` to find the PR head commit where the
     ``pull_request``-triggered build actually ran, and check that SHA.
  3. When neither path yields a completed build, return ``ci_passed=False``
     with ``source="unresolved"`` — the orchestrator must keep polling, never
     promote on the absence of a signal.

The final ``state`` distinguishes a *definitive* absence of CI
(``state="no_checks"``: the branch HEAD and every reachable PR-event SHA were
queried successfully and carry zero check-runs) from a *transient* failure to
resolve (``state="pending"``: an I/O error, or checks still in progress).
Both keep ``ci_passed=False``, but the orchestrator treats them differently —
a definitive ``no_checks`` on an internal staging branch (research/plan
rollups, which CI path-filters out by design) means CI will never run, so the
merge gate may proceed; a transient ``pending`` must keep polling.

Public API: ``resolve_pr_ci_status``.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from app import github

logger = logging.getLogger(__name__)


# CI resolution states for the branch-HEAD strict check.
_CI_GREEN = "green"  # a completed, all-passing build exists
_CI_RED = "red"  # a completed build with at least one failure
_CI_PENDING = "pending"  # checks in progress
_CI_NO_CHECKS = "no_checks"  # the commit carries no check-runs


def _classify_check_runs(result: dict[str, Any] | None) -> str:
    """Map a strict ``get_check_runs`` result to a resolution state."""
    if not isinstance(result, dict):
        return _CI_PENDING
    status = result.get("status")
    if status == "no_checks":
        return _CI_NO_CHECKS
    if status == "completed":
        conclusion = result.get("conclusion")
        if conclusion == "success":
            return _CI_GREEN
        if conclusion == "failure":
            return _CI_RED
        return _CI_PENDING
    return _CI_PENDING


def requires_ci_checks(project) -> bool:
    """Whether a project's PRs must wait for CI status checks before merging.

    Defaults True (CI-gated, like Blueprint's own repo). A project with no CI at
    all — e.g. a greenfield repo with no GitHub Actions — can declare
    ``build_config.requires_ci_checks = False`` so a *definitive* absence of
    checks is treated as 'CI not applicable' instead of polled until the merge
    backstop FAILs the task.
    """
    return bool(
        (getattr(project, "build_config", None) or {}).get("requires_ci_checks", True)
    )


def proceed_on_no_checks(project, branch: str | None) -> bool:
    """Whether a *definitive* ``no_checks`` (every reachable SHA queried cleanly,
    zero check-runs) should be treated as passed so the orchestrator proceeds to
    the mergeability-gated merge, rather than polled until the backstop FAILs it.

    True when the PR targets an internal staging branch (research/plan rollups,
    where CI is path-filtered out by design), OR when the project declares it has
    no CI (``requires_ci_checks`` is False). CI-gated repos keep polling — for
    them a missing check means 'not registered yet', never 'will never run'.
    """
    if project is None:
        return False
    if project.is_staging_branch(branch):
        return True
    return not requires_ci_checks(project)


def has_detectable_tests(project) -> bool:
    """Whether the project's stored ``build_config`` carries a runnable test
    command — i.e. the no-CI merge gate has a suite the ``verify_tests`` agent
    can run on a PR head.

    Onboarding (and the auto re-detect path) populate
    ``build_config.directories[].test_cmd`` for each detected manifest
    directory.  This only answers 'is ANY non-empty ``test_cmd`` present'; the
    agent's ``detectTestRunners`` does the authoritative resolution at run time
    and reports ``detected=false`` if nothing is actually runnable, at which
    point the gate falls back to proceed-without-checks.  A near-empty /
    greenfield repo with no manifests has no directories (or empty
    ``test_cmd``) and returns False here, so it keeps today's immediate-pass
    behavior.
    """
    if project is None:
        return False
    build_config = getattr(project, "build_config", None) or {}
    directories = build_config.get("directories") or []
    for d in directories:
        if isinstance(d, dict) and str(d.get("test_cmd") or "").strip():
            return True
    return False


def compute_merge_verification_verdict(project) -> dict[str, Any]:
    """Read-only advisory describing what the no-CI merge gate will do for a
    project's normal (non-staging-base) PRs — display data for the operator,
    NOT a live decision input.

    The verdict is computed purely from the project's persisted
    ``build_config`` by REUSING this module's existing gate predicates
    (:func:`requires_ci_checks` and :func:`has_detectable_tests`); it never
    re-implements test detection.  The three mutually-exclusive states follow
    the same precedence the gate itself uses:

      - ``"ci_checks"`` — :func:`requires_ci_checks` is True (the default):
        merges wait for the repository's GitHub required CI checks.
      - ``"tests_gated"`` — ``requires_ci_checks`` is False AND
        :func:`has_detectable_tests` is True: the repo's own detected test
        command(s) run on the PR head as the merge verdict.  ``test_commands``
        lists each non-empty ``build_config.directories[].test_cmd`` paired
        with its directory ``path`` (in stored order).
      - ``"unverified"`` — ``requires_ci_checks`` is False AND no detectable
        tests: PRs merge with no test verification at all.

    Returns
    -------
    A stable, JSON-serializable dict::

        {"state": "ci_checks" | "tests_gated" | "unverified",
         "test_commands": [{"path": str, "test_cmd": str}, ...]}

    ``test_commands`` is ``[]`` for every state except ``"tests_gated"``.

    Handles ``project is None`` / missing ``build_config`` defensively
    (mirroring :func:`has_detectable_tests`): a ``None`` project yields the
    CI-gated default shape, matching ``requires_ci_checks``'s default-True
    semantics, and never raises.
    """
    if requires_ci_checks(project):
        return {"state": "ci_checks", "test_commands": []}

    if has_detectable_tests(project):
        build_config = getattr(project, "build_config", None) or {}
        directories = build_config.get("directories") or []
        test_commands: list[dict[str, str]] = []
        for d in directories:
            if not isinstance(d, dict):
                continue
            test_cmd = str(d.get("test_cmd") or "").strip()
            if not test_cmd:
                continue
            test_commands.append(
                {"path": str(d.get("path") or ""), "test_cmd": test_cmd}
            )
        return {"state": "tests_gated", "test_commands": test_commands}

    return {"state": "unverified", "test_commands": []}


def should_run_no_ci_test_gate(project, branch: str | None, *, gate_enabled: bool) -> bool:
    """Whether a definitive ``no_checks`` PR should be gated on a ``verify_tests``
    verdict (run the repo's detected suite on the PR head) instead of taking the
    legacy immediate-pass.

    True only when ALL hold:
      - the kill-switch ``gate_enabled`` is on;
      - the base is NOT an internal staging branch (research/plan rollups keep
        the immediate-pass — CI is path-filtered out there by design, and we do
        not run repo tests on internal rollups);
      - ``proceed_on_no_checks`` is True, i.e. the merge would otherwise
        immediate-pass — combined with the not-staging check above this means
        the project is an EXTERNAL no-CI repo (``requires_ci_checks`` False).
        A CI-gated repo keeps polling and never reaches this gate;
      - the project has a detectable test command.

    Otherwise the legacy immediate-pass applies (CI-gated repos are untouched).
    """
    if project is None or not gate_enabled:
        return False
    if project.is_staging_branch(branch):
        return False
    if not proceed_on_no_checks(project, branch):
        return False
    return has_detectable_tests(project)


# Outcome of mapping a MERGE_TEST_RESULT verdict onto the merge gate.
MergeTestOutcome = Literal["pass", "fail", "undetected"]


def classify_merge_test_verdict(
    verdict: dict[str, Any] | None,
) -> tuple[MergeTestOutcome, str]:
    """Map a ``verify_tests`` MERGE_TEST_RESULT artifact to a merge-gate outcome.

    The agent posts ``{detected, passed, tests[], output}``.  Returns
    ``(outcome, detail)``:

      - ``"pass"``       — a suite was detected, ran, and passed; the merge may
                           proceed (subject to the unchanged review gate).
      - ``"fail"``       — a suite was detected and ran but failed; block the
                           merge.  ``detail`` carries the failing-test output.
      - ``"undetected"`` — the agent found no runnable command
                           (``detected=false``) OR the verify task produced no
                           usable verdict (crashed / no artifact).  Fall back to
                           the legacy proceed-without-checks behavior: an
                           absent-tests repo must still progress, merging
                           UNVERIFIED.

    ``detail`` is the failing-test output for ``"fail"`` and ``""`` otherwise.
    """
    if not isinstance(verdict, dict):
        return ("undetected", "")
    if verdict.get("detected") is False:
        return ("undetected", "")
    if verdict.get("passed"):
        return ("pass", "")
    output = str(verdict.get("output") or "").strip()
    return ("fail", output or "Detected test suite failed (no output captured).")


def build_no_ci_gate_ci_result(
    outcome: MergeTestOutcome, detail: str = ""
) -> dict[str, Any]:
    """Map a no-CI merge-gate ``outcome`` to the ``ci_result`` dict the
    orchestrator hands to ``handle_ci_completion``.

    This is the load-bearing transition the orchestrator's inline no-CI gate
    performs once it has a ``verify_tests`` verdict (a sibling to
    ``classify_merge_test_verdict``, which produces that ``outcome``):

      - ``"fail"``                  → ``conclusion="failure"`` so the existing
                                      ``handle_ci_completion`` block-and-fix path
                                      fires carrying ``detail`` as ``failed_logs``.
                                      A blank ``detail`` falls back to a generic
                                      message so the fix task is never spawned
                                      with empty output.
      - ``"pass"`` / ``"undetected"`` → ``conclusion="success"`` so the merge may
                                      proceed (``"undetected"`` merges UNVERIFIED:
                                      an absent-tests repo must still progress).

    Extracted so a regression in this mapping — e.g. flipping ``fail`` back to a
    merge-allowing ``success`` — fails a test rather than silently shipping
    merge-without-checks.
    """
    if outcome == "fail":
        return {
            "status": "completed",
            "conclusion": "failure",
            "details": [],
            "failed_logs": detail or "Detected test suite failed.",
        }
    return {
        "status": "completed",
        "conclusion": "success",
        "details": [],
        "failed_logs": "",
    }


ResolutionSource = Literal["branch_head", "merge_sha", "unresolved"]


async def resolve_pr_ci_status(
    pr_number: int | None,
    branch_sha: str,
    *,
    token: str,
    owner: str,
    repo: str,
    project_id: str | None = None,
) -> dict[str, Any]:
    """Resolve the real CI verdict for a PR's branch HEAD.

    Returns
    -------
    ``{"ci_passed": bool, "conclusion": str | None, "head_sha_resolved": str,
       "source": "branch_head" | "merge_sha" | "unresolved",
       "state": str, "details": list[dict]}``

    ``ci_passed`` is True only when a completed, all-passing build was found on
    either the branch HEAD or — when the HEAD has no check-runs — on the PR
    head commit reachable via ``commits/{sha}/pulls``.  Pending / unresolved
    states return ``ci_passed=False``; the orchestrator should keep polling
    rather than treat the absence of a signal as a pass.

    Never raises: any GitHub I/O failure degrades to ``source="unresolved"`` so
    the orchestrator stays in its CI-wait loop instead of crashing the cycle.
    """
    if not branch_sha:
        return {
            "ci_passed": False,
            "conclusion": None,
            "head_sha_resolved": "",
            "source": "unresolved",
            "state": _CI_PENDING,
            "details": [],
        }

    # --- Step 1: strict check-runs on the branch HEAD itself --------------
    try:
        direct = await github.get_check_runs_cached(
            token,
            owner,
            repo,
            branch_sha,
            project_id=project_id,
            treat_missing_as_success=False,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CI_RESOLUTION] branch-head check-runs fetch failed for %s/%s@%s: %s",
            owner,
            repo,
            branch_sha[:8],
            exc,
        )
        return {
            "ci_passed": False,
            "conclusion": None,
            "head_sha_resolved": branch_sha,
            "source": "unresolved",
            "state": _CI_PENDING,
            "details": [],
        }

    direct_state = _classify_check_runs(direct)
    if direct_state in (_CI_GREEN, _CI_RED):
        return {
            "ci_passed": direct_state == _CI_GREEN,
            "conclusion": direct.get("conclusion"),
            "head_sha_resolved": branch_sha,
            "source": "branch_head",
            "state": direct_state,
            "details": direct.get("details", []),
        }
    if direct_state == _CI_PENDING:
        return {
            "ci_passed": False,
            "conclusion": None,
            "head_sha_resolved": branch_sha,
            "source": "branch_head",
            "state": _CI_PENDING,
            "details": direct.get("details", []),
        }

    # --- Step 2: resolve through commits/{sha}/pulls ----------------------
    # direct_state == _CI_NO_CHECKS — fall through to the PR-event merge SHA
    # where pull_request-triggered builds actually live.
    try:
        prs = await github.list_pull_requests_for_commit(
            token, owner, repo, branch_sha, project_id=project_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[CI_RESOLUTION] pr-for-commit lookup failed for %s/%s@%s: %s",
            owner,
            repo,
            branch_sha[:8],
            exc,
        )
        return {
            "ci_passed": False,
            "conclusion": None,
            "head_sha_resolved": branch_sha,
            "source": "unresolved",
            "state": _CI_PENDING,
            "details": [],
        }

    # Prefer the explicitly-requested PR when supplied; otherwise prefer merged
    # PRs and fall back to every associated PR when none carry merged_at.
    explicit_pr: list[dict] = []
    if pr_number is not None:
        explicit_pr = [
            pr for pr in prs if isinstance(pr, dict) and pr.get("number") == pr_number
        ]
    if explicit_pr:
        use_prs = explicit_pr
    else:
        merged_prs = [pr for pr in prs if isinstance(pr, dict) and pr.get("merged_at")]
        use_prs = merged_prs or [pr for pr in prs if isinstance(pr, dict)]

    head_shas: list[str] = []
    for pr in use_prs:
        head = pr.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        if head_sha and head_sha != branch_sha and head_sha not in head_shas:
            head_shas.append(head_sha)

    if not head_shas:
        # HEAD carries no check-runs and there is no distinct PR-event SHA to
        # fall back to — CI is *definitively* absent here (we queried and found
        # nothing), not merely slow. Report ``no_checks`` so the orchestrator
        # can distinguish "CI will never run" (e.g. an internal staging-branch
        # PR that CI path-filters out) from a transient resolution failure.
        return {
            "ci_passed": False,
            "conclusion": None,
            "head_sha_resolved": branch_sha,
            "source": "unresolved",
            "state": _CI_NO_CHECKS,
            "details": [],
        }

    # Exhaustive RED-preferring sweep: a red on ANY PR head wins over a green
    # on another head regardless of the order GitHub returns them.
    first_red_sha: str | None = None
    first_red_result: dict | None = None
    first_green_sha: str | None = None
    first_green_result: dict | None = None
    # Track whether we saw a real in-progress build or a fetch error: either
    # means the absence of a green/red is *transient* (keep polling), as
    # opposed to every reachable head definitively carrying zero check-runs.
    saw_pending = False
    saw_error = False
    for head_sha in head_shas:
        try:
            pr_checks = await github.get_check_runs_cached(
                token,
                owner,
                repo,
                head_sha,
                project_id=project_id,
                treat_missing_as_success=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[CI_RESOLUTION] check-runs fetch failed for PR head %s/%s@%s: %s",
                owner,
                repo,
                head_sha[:8],
                exc,
            )
            saw_error = True
            continue
        pr_state = _classify_check_runs(pr_checks)
        if pr_state == _CI_RED and first_red_sha is None:
            first_red_sha = head_sha
            first_red_result = pr_checks
        elif pr_state == _CI_GREEN and first_green_sha is None:
            first_green_sha = head_sha
            first_green_result = pr_checks
        elif pr_state == _CI_PENDING:
            saw_pending = True

    if first_red_sha is not None:
        return {
            "ci_passed": False,
            "conclusion": (first_red_result or {}).get("conclusion"),
            "head_sha_resolved": first_red_sha,
            "source": "merge_sha",
            "state": _CI_RED,
            "details": (first_red_result or {}).get("details", []),
        }
    if first_green_sha is not None:
        return {
            "ci_passed": True,
            "conclusion": (first_green_result or {}).get("conclusion"),
            "head_sha_resolved": first_green_sha,
            "source": "merge_sha",
            "state": _CI_GREEN,
            "details": (first_green_result or {}).get("details", []),
        }
    # No green and no red across the reachable heads. If any head had checks
    # in progress (or a fetch errored), the verdict is merely not-final-yet →
    # ``pending`` (keep polling). Only when every head was queried cleanly and
    # carried zero check-runs is CI *definitively* absent → ``no_checks``.
    return {
        "ci_passed": False,
        "conclusion": None,
        "head_sha_resolved": branch_sha,
        "source": "unresolved",
        "state": _CI_PENDING if (saw_pending or saw_error) else _CI_NO_CHECKS,
        "details": [],
    }
