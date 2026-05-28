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
