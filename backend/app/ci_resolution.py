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


def record_no_ci_gate_runtime(
    db_session,
    project,
    *,
    executed_real_tests: bool,
    unverified_reason: str | None,
    when,
) -> None:
    """Best-effort persist of the no-CI merge gate's LAST runtime outcome onto
    ``project.merge_gate_runtime``.

    Where :func:`compute_merge_verification_verdict` is a read-only advisory
    derived purely from stored ``build_config``, this captures what the gate
    ACTUALLY did the last time it ran a PR through to a terminal outcome — so an
    operator can distinguish a config that *claims* tests are gated from one
    whose live no-CI path fell open to an UNVERIFIED merge.

    Always records ``last_run_at`` (ISO) and ``executed_real_tests``
    (True ONLY when verify-tests produced a real pass/fail verdict — i.e. a
    suite actually ran to a verdict). When the gate fell open to UNVERIFIED
    (``unverified_reason`` is set), additionally stamps ``last_unverified_at``
    and ``last_unverified_reason``. A verified run does NOT clear those
    ``last_unverified_*`` fields — they preserve the most recent fall-open,
    which remains historically true.

    Best-effort by contract: it mutates the in-memory ``project`` and stages it
    on ``db_session`` (the caller's commit persists it) but NEVER raises — a
    failure here must not block or break the merge hot path.
    """
    try:
        if project is None:
            return
        when_iso = when.isoformat() if hasattr(when, "isoformat") else str(when)
        runtime: dict[str, Any] = dict(
            getattr(project, "merge_gate_runtime", None) or {}
        )
        runtime["last_run_at"] = when_iso
        runtime["executed_real_tests"] = bool(executed_real_tests)
        if unverified_reason:
            runtime["last_unverified_at"] = when_iso
            runtime["last_unverified_reason"] = str(unverified_reason)
        project.merge_gate_runtime = runtime
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(project, "merge_gate_runtime")
        except Exception:  # noqa: BLE001
            # flag_modified is a no-op safety net for the JSONB nested-dict
            # write; a detached/plain object (e.g. a unit-test stub) simply
            # skips it. Never let this break the merge path.
            pass
        if db_session is not None:
            db_session.add(project)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[CI_RESOLUTION] failed to record no-CI gate runtime readiness",
            exc_info=True,
        )


def merge_gate_runtime_block(project) -> dict[str, Any] | None:
    """Normalize ``project.merge_gate_runtime`` into the stable API block, or
    ``None`` when the gate has never run for this project.

    Returns a JSON-serializable dict with the four operator-facing keys
    (``last_run_at``, ``executed_real_tests``, ``last_unverified_at``,
    ``last_unverified_reason``).  ``last_unverified_*`` are omitted when the gate
    has never fallen open to an UNVERIFIED merge.  Defensive against a missing /
    malformed column so it can be attached to every project API response without
    raising.
    """
    raw = getattr(project, "merge_gate_runtime", None)
    if not isinstance(raw, dict) or not raw:
        return None
    block: dict[str, Any] = {
        "last_run_at": raw.get("last_run_at"),
        "executed_real_tests": bool(raw.get("executed_real_tests", False)),
    }
    if raw.get("last_unverified_at") is not None:
        block["last_unverified_at"] = raw.get("last_unverified_at")
    if raw.get("last_unverified_reason") is not None:
        block["last_unverified_reason"] = raw.get("last_unverified_reason")
    return block


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

    Acceptance probe (``detected=false`` only): when no unit suite ran, the agent
    may still attach a cheap goal-level ``probe`` ("does the changed code still
    compile/build/boot"). A CLEAR probe failure — it ran, did not pass, and was
    NOT a could-not-run degrade — maps to ``"fail"`` so the existing block-and-fix
    path fires carrying the probe output. Every other probe state (pass /
    could-not-run / absent / old-shape verdict) keeps today's ``"undetected"``
    fall-open, so missing-deps / unprovisioned setup never hard-blocks a merge.
    """
    if not isinstance(verdict, dict):
        return ("undetected", "")
    if verdict.get("detected") is False:
        if _probe_is_clear_failure(verdict.get("probe")):
            probe = verdict["probe"]
            output = str(probe.get("output") or "").strip()
            if probe.get("declared"):
                # The operator's OWN declared build command failed — surface that
                # explicitly so the spawned fix task knows the authoritative build
                # (not a generic compile probe) is what broke.
                label = str(probe.get("label") or "").strip()
                prefix = (
                    f"Declared build command `{label}` failed: "
                    if label
                    else "Declared build command failed: "
                )
                return (
                    "fail",
                    (prefix + output)
                    if output
                    else (prefix + "the declared build did not succeed."),
                )
            return (
                "fail",
                output
                or "Acceptance probe failed: the changed code does not compile/build.",
            )
        return ("undetected", "")
    if verdict.get("passed"):
        return ("pass", "")
    output = str(verdict.get("output") or "").strip()
    return ("fail", output or "Detected test suite failed (no output captured).")


def _probe_is_clear_failure(probe: Any) -> bool:
    """Whether an acceptance ``probe`` block represents a CLEAR, blocking failure.

    True only when the probe ran, was not a could-not-run degrade, and did not
    pass — i.e. the changed code genuinely failed to compile/build. Defensive
    about a missing / non-dict / old-shape probe (returns False), so a verdict
    from an older agent that emits no probe keeps the undetected fall-open.
    """
    if not isinstance(probe, dict):
        return False
    if not probe.get("ran"):
        return False
    if probe.get("could_not_run"):
        return False
    return probe.get("passed") is False


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


# Values an agent's verify-tests handler may emit in the verdict's
# ``noTestsReason`` field to mark a suite that was PRESENT and ran but skipped
# (or deselected) every test — distinct from a genuine no-suite/placeholder
# repo. Matched case-insensitively; tolerant of the field name not yet existing
# on an older agent (then the generic reason is kept).
_ALL_SKIPPED_NO_TESTS_REASONS = frozenset(
    {"all_skipped", "all_tests_skipped", "every_test_skipped", "skipped"}
)


def classify_unverified_fall_open_reason(
    verdict: dict[str, Any] | None,
) -> tuple[str, str]:
    """Map a detected=false MERGE_TEST_RESULT verdict to the operator-visible
    ``(phrase, stable_key)`` for a no-CI gate fall-open to an UNVERIFIED merge.

    A genuine no-suite/placeholder repo keeps today's generic
    ``("no test command detectable", "undetected_runtime")``.  When the agent's
    verify-tests handler marks — via the verdict's ``noTestsReason`` field — a
    suite that was PRESENT but skipped EVERY test (e.g. an integration suite
    that all-skips on the cold verify VM because no services are provisioned),
    return a DISTINCT ``("suite present but every test skipped",
    "suite_all_skipped")`` so an operator can tell the two apart on
    ``merge_gate_runtime.last_unverified_reason``.

    Acceptance probe: when the gate falls open with a probe attached (and the
    probe was NOT a clear failure — that case blocks instead, via
    :func:`classify_merge_test_verdict`), surface the probe's honest signal:
    ``("acceptance probe passed (...)", "probe_passed")`` when the cheap
    compile/build check passed, or ``("acceptance probe could not run (...)",
    "probe_could_not_run")`` when missing deps / unprovisioned setup degraded it.
    The probe signal takes precedence over the all-skipped phrasing because it is
    the stronger execution evidence we have for this fall-open.

    Purely a refinement of the operator-visible reason: it never changes the
    pass/fail/undetected merge decision.  Defensive about a missing / old-shape
    verdict (an older agent that does not emit ``noTestsReason`` keeps the
    generic reason).
    """
    if isinstance(verdict, dict):
        probe = verdict.get("probe")
        if isinstance(probe, dict) and probe.get("ran"):
            # A declared probe ran the project's OWN ``build_cmd`` — surface
            # declared-build wording (with the command label) instead of the
            # generic compile-probe phrasing. The stable KEY is unchanged
            # (``probe_passed`` / ``probe_could_not_run``) so the operator
            # label maps and the no-CI fall-open invariant stay intact; only the
            # human phrase distinguishes declared from generic. An older agent
            # that emits no ``declared`` marker keeps the generic phrase.
            declared = bool(probe.get("declared"))
            label = str(probe.get("label") or "").strip()
            if probe.get("could_not_run"):
                if declared:
                    phrase = (
                        f"declared build command could not run ({label})"
                        if label
                        else "declared build command could not run (missing deps/unprovisioned setup)"
                    )
                    return (phrase, "probe_could_not_run")
                return (
                    "acceptance probe could not run (missing deps/unprovisioned setup)",
                    "probe_could_not_run",
                )
            if probe.get("passed") is True:
                if declared:
                    phrase = (
                        f"declared build command passed ({label})"
                        if label
                        else "declared build command passed (deliverable builds)"
                    )
                    return (phrase, "probe_passed")
                return (
                    "acceptance probe passed (changed code compiles/builds)",
                    "probe_passed",
                )
        no_tests_reason = str(verdict.get("noTestsReason") or "").strip().lower()
        if no_tests_reason in _ALL_SKIPPED_NO_TESTS_REASONS:
            return ("suite present but every test skipped", "suite_all_skipped")
    return ("no test command detectable", "undetected_runtime")


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
