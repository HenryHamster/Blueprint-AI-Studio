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
from app.claim_task_helpers import is_commands_stale

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
    where CI is path-filtered out by design), when detection confirmed the repo
    carries zero CI workflows (``has_ci=False`` — a physical impossibility of
    checks ever appearing), or when the project explicitly declares no CI
    (``requires_ci_checks=False``). CI-gated repos keep polling — for them a
    missing check means 'not registered yet', never 'will never run'.

    ``ci_required_branches`` override (``build_config``, default ``["main"]`` —
    the promotion target): a repo that CONFIRMED it has CI (``has_ci=True``) but
    relaxed ``requires_ci_checks`` — to skip per-PR CI on its integration branch
    (e.g. ``automain``, whose PRs then legitimately carry zero checks) — STILL
    keeps its required branches strictly CI-gated. This keeps the
    integration->``main`` promotion gate airtight: a no_checks on the promotion
    PR (the empty-checks window between PR-open and GitHub registering check-runs)
    must never merge on absence of signal. The override is deliberately scoped to
    ``has_ci is True`` so it ONLY constrains a CI-capable repo that opted out —
    an external no-CI repo (``has_ci`` False or None/undetermined) is untouched
    and proceeds on its real base as before, never wedged into polling for checks
    that will never come.

    ``has_ci=False`` is authoritative over a stale ``requires_ci_checks=True``:
    a repo with zero workflows cannot produce check-runs on any PR, so polling
    is guaranteed to strand the parent in EVALUATING forever.
    """
    if project is None:
        return False
    if project.is_staging_branch(branch):
        return True
    build_config = getattr(project, "build_config", None) or {}
    if build_config.get("has_ci") is False:
        return True
    # CI-capable repo that relaxed requires_ci_checks: its required branches stay
    # strict so the promotion gate cannot merge on absence of signal. Scoped to
    # has_ci is True so no-CI / undetermined repos keep their existing behavior.
    if build_config.get("has_ci") is True and branch is not None:
        required_branches = build_config.get("ci_required_branches", ["main"])
        if branch in required_branches:
            return False
    return not requires_ci_checks(project)


def _effective_test_cmd(d: dict) -> str:
    """Return the effective test command for a single directory entry.

    A FRESH (non-stale) agent-confirmed command takes precedence over the
    detected ``test_cmd``.  A stale or absent confirmation falls back to
    ``test_cmd``.  Returns an empty string when neither is set.
    """
    if not is_commands_stale(d.get("commands_confirmed_at")):
        confirmed = str(d.get("confirmed_test_cmd") or "").strip()
        if confirmed:
            return confirmed
    return str(d.get("test_cmd") or "").strip()


def has_detectable_tests(project) -> bool:
    """Whether the project's stored ``build_config`` carries a runnable test
    command — i.e. the no-CI merge gate has a suite the ``verify_tests`` agent
    can run on a PR head.

    Onboarding (and the auto re-detect path) populate
    ``build_config.directories[].test_cmd`` for each detected manifest
    directory.  A FRESH (non-stale) agent-confirmed ``confirmed_test_cmd``
    also counts — an agent that discovers a working command after detection
    found none upgrades the project from 'unverified' to 'tests_gated'.
    This only answers 'is ANY non-empty effective command present'; the
    agent's ``detectTestRunners`` does the authoritative resolution at run
    time and reports ``detected=false`` if nothing is actually runnable.  A
    near-empty / greenfield repo with no manifests (or empty commands) returns
    False here, so it keeps today's immediate-pass behavior.
    """
    if project is None:
        return False
    build_config = getattr(project, "build_config", None) or {}
    directories = build_config.get("directories") or []
    for d in directories:
        if isinstance(d, dict) and _effective_test_cmd(d):
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
            test_cmd = _effective_test_cmd(d)
            if not test_cmd:
                continue
            test_commands.append(
                {"path": str(d.get("path") or ""), "test_cmd": test_cmd}
            )
        return {"state": "tests_gated", "test_commands": test_commands}

    return {"state": "unverified", "test_commands": []}


# Bound on the rolling ``recent_outcomes`` history kept inside
# ``project.merge_gate_runtime``. Small by design — operators want an
# at-a-glance recent trend, not a full audit log — and it caps the JSONB
# column growth (oldest entries are dropped first).
_RECENT_OUTCOMES_CAP = 20


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

    Additionally maintains, additively, a bounded rolling history and a
    per-reason tally so an operator can see a TREND rather than only the single
    latest value:
      - ``recent_outcomes``: list (capped at :data:`_RECENT_OUTCOMES_CAP`,
        oldest dropped) of ``{"at": iso, "verified": bool, "reason": key|None}``
        — one entry per terminal call (verified AND fell-open);
      - ``unverified_reason_counts``: ``{reason_key: count}`` incremented only
        on fall-opens.
    Legacy four-key-only records upgrade transparently (the list starts empty,
    the tally starts absent).

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

        # Rolling history of recent terminal outcomes (additive; legacy
        # four-key records simply start an empty list). Append one entry per
        # terminal call and cap at the most recent ``_RECENT_OUTCOMES_CAP`` so
        # the JSONB column cannot grow unbounded.
        prior_outcomes = runtime.get("recent_outcomes")
        outcomes: list[dict[str, Any]] = (
            list(prior_outcomes) if isinstance(prior_outcomes, list) else []
        )
        outcomes.append(
            {
                "at": when_iso,
                "verified": bool(executed_real_tests),
                "reason": str(unverified_reason) if unverified_reason else None,
            }
        )
        runtime["recent_outcomes"] = outcomes[-_RECENT_OUTCOMES_CAP:]

        # Per-reason fall-open tally — incremented ONLY on fall-opens (a
        # verified run has no reason key to count).
        if unverified_reason:
            prior_counts = runtime.get("unverified_reason_counts")
            counts: dict[str, int] = (
                dict(prior_counts) if isinstance(prior_counts, dict) else {}
            )
            key = str(unverified_reason)
            counts[key] = int(counts.get(key, 0) or 0) + 1
            runtime["unverified_reason_counts"] = counts

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
            # Completion-debt ledger: a fall-open to an UNVERIFIED merge is a
            # standing obligation (the no-CI gate owes an enforce decision).
            # Best-effort and deduped per project so repeated fall-opens keep a
            # SINGLE open row. Sync callers/tests use the plain recorder; the
            # live async merge path uses the async recorder below.
            if unverified_reason:
                try:
                    from app.completion_debt import (
                        record_completion_debt,
                        KIND_NO_CI_GATE,
                    )

                    _pid = getattr(project, "id", None)
                    record_completion_debt(
                        db_session,
                        dedupe_key=f"no_ci_gate:{_pid}",
                        kind=KIND_NO_CI_GATE,
                        title="No-CI merge gate fell open to an UNVERIFIED merge",
                        project_id=_pid,
                        source="ci_resolution",
                        detail={"last_unverified_reason": str(unverified_reason)},
                    )
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        logger.warning(
            "[CI_RESOLUTION] failed to record no-CI gate runtime readiness",
            exc_info=True,
        )


async def arecord_no_ci_gate_runtime(
    db_session,
    project,
    *,
    executed_real_tests: bool,
    unverified_reason: str | None,
    when,
) -> None:
    """Async analogue of :func:`record_no_ci_gate_runtime`.

    The orchestrator's live merge path already runs on ``AsyncSession``; this
    variant keeps the runtime ledger write effective there instead of relying on
    the sync recorder's async-session no-op.
    """
    try:
        when_iso = when.isoformat() if hasattr(when, "isoformat") else str(when)
        runtime: dict[str, Any] = dict(
            getattr(project, "merge_gate_runtime", None) or {}
        )
        runtime["last_run_at"] = when_iso
        runtime["executed_real_tests"] = bool(executed_real_tests)
        if unverified_reason:
            runtime["last_unverified_at"] = when_iso
            runtime["last_unverified_reason"] = str(unverified_reason)
        outcomes = list(runtime.get("recent_outcomes") or [])
        outcomes.append(
            {
                "at": when_iso,
                "verified": bool(executed_real_tests),
                "reason": str(unverified_reason) if unverified_reason else None,
            }
        )
        runtime["recent_outcomes"] = outcomes[-_RECENT_OUTCOMES_CAP:]

        if unverified_reason:
            prior_counts = runtime.get("unverified_reason_counts")
            counts: dict[str, int] = (
                dict(prior_counts) if isinstance(prior_counts, dict) else {}
            )
            key = str(unverified_reason)
            counts[key] = int(counts.get(key, 0) or 0) + 1
            runtime["unverified_reason_counts"] = counts

        project.merge_gate_runtime = runtime
        try:
            from sqlalchemy.orm.attributes import flag_modified

            flag_modified(project, "merge_gate_runtime")
        except Exception:  # noqa: BLE001
            pass
        if db_session is not None:
            db_session.add(project)
            if unverified_reason:
                try:
                    from app.completion_debt import (
                        arecord_completion_debt,
                        KIND_NO_CI_GATE,
                    )

                    _pid = getattr(project, "id", None)
                    await arecord_completion_debt(
                        db_session,
                        dedupe_key=f"no_ci_gate:{_pid}",
                        kind=KIND_NO_CI_GATE,
                        title="No-CI merge gate fell open to an UNVERIFIED merge",
                        project_id=_pid,
                        source="ci_resolution",
                        detail={"last_unverified_reason": str(unverified_reason)},
                    )
                except Exception:  # noqa: BLE001
                    pass
    except Exception:  # noqa: BLE001
        logger.warning(
            "[CI_RESOLUTION] failed to record no-CI gate runtime readiness",
            exc_info=True,
        )


def record_no_ci_gate_degraded_block(
    db_session,
    project,
    *,
    unverified_reason: str | None,
    when,
) -> None:
    """Best-effort persist that the no-CI gate HONESTLY BLOCKED a degraded
    unverified merge — the opt-in ``block_degraded_unverified_merge`` policy is
    ON and the gate fell open with a DEGRADED reason (the suite could not run).

    Records the same terminal-outcome trend as any other fall-open via
    :func:`record_no_ci_gate_runtime` (the suite did NOT run, so
    ``executed_real_tests`` is False and the reason is tallied) AND additionally
    stamps block-specific fields so an operator can distinguish a PR that was
    deliberately HELD from one that merged UNVERIFIED:
      - ``last_degraded_block_at`` / ``last_degraded_block_reason``;
      - ``degraded_block_counts``: ``{reason_key: count}`` tally.

    Best-effort by contract: mutates the in-memory ``project`` and stages it on
    ``db_session`` (the caller's commit persists it) but NEVER raises — a
    failure here must not block or break the gate hot path.
    """
    try:
        if project is None:
            return
        # Reuse the canonical recorder so the rolling history / per-reason tally
        # stay consistent with every other terminal outcome. The suite could NOT
        # run, so this is an unverified fall-open with executed_real_tests=False.
        record_no_ci_gate_runtime(
            db_session,
            project,
            executed_real_tests=False,
            unverified_reason=unverified_reason,
            when=when,
        )
        when_iso = when.isoformat() if hasattr(when, "isoformat") else str(when)
        runtime: dict[str, Any] = dict(
            getattr(project, "merge_gate_runtime", None) or {}
        )
        runtime["last_degraded_block_at"] = when_iso
        if unverified_reason:
            runtime["last_degraded_block_reason"] = str(unverified_reason)
            prior_counts = runtime.get("degraded_block_counts")
            counts: dict[str, int] = (
                dict(prior_counts) if isinstance(prior_counts, dict) else {}
            )
            key = str(unverified_reason)
            counts[key] = int(counts.get(key, 0) or 0) + 1
            runtime["degraded_block_counts"] = counts
        project.merge_gate_runtime = runtime
        # flag_modified is a no-op safety net for the JSONB nested-dict write; a
        # detached/plain object (e.g. a unit-test stub) makes it raise, which the
        # outer guard below catches — never let it break the gate path.
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "merge_gate_runtime")
        if db_session is not None:
            db_session.add(project)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[CI_RESOLUTION] failed to record no-CI gate degraded block",
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

    When the runtime record carries the additive history fields it also surfaces:
      - ``recent_outcomes``: the bounded rolling list (most recent last);
      - ``unverified_reason_counts``: the per-reason fall-open tally;
      - ``summary``: a derived at-a-glance aggregate —
        ``window`` (entries retained), ``verified_runs`` / ``fell_open_runs``
        (counts WITHIN that window), ``total_fall_opens`` (the all-time tally
        sum, which can exceed the window once older entries are evicted), and
        ``most_common_reason`` (the most-tallied fall-open reason key, or None).
    A legacy four-key-only record or a never-ran project omits all four — the
    response shape stays additive and backward-compatible.
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

    # Additive honest-block fields — present only when the opt-in
    # ``block_degraded_unverified_merge`` policy actually HELD a PR. A project
    # that never blocked (or never opted in) omits all three, so the shape stays
    # backward-compatible.
    if raw.get("last_degraded_block_at") is not None:
        block["last_degraded_block_at"] = raw.get("last_degraded_block_at")
    if raw.get("last_degraded_block_reason") is not None:
        block["last_degraded_block_reason"] = raw.get("last_degraded_block_reason")
    raw_block_counts = raw.get("degraded_block_counts")
    block_counts: dict[str, int] = {}
    if isinstance(raw_block_counts, dict):
        for reason_key, count in raw_block_counts.items():
            # The recorder only ever writes int counts; skip any malformed
            # (str/None/bool) value defensively without a per-iteration guard.
            if isinstance(count, bool) or not isinstance(count, (int, float)):
                continue
            block_counts[str(reason_key)] = int(count)
    if block_counts:
        block["degraded_block_counts"] = block_counts

    # Additive no-test honest-block fields — present only when the opt-in
    # ``block_no_test_unverified_merge`` policy actually HELD a PR. A project
    # that never blocked (or never opted in) omits all three, so the shape stays
    # backward-compatible.
    if raw.get("last_no_test_block_at") is not None:
        block["last_no_test_block_at"] = raw.get("last_no_test_block_at")
    if raw.get("last_no_test_block_reason") is not None:
        block["last_no_test_block_reason"] = raw.get("last_no_test_block_reason")
    raw_no_test_counts = raw.get("no_test_block_counts")
    no_test_counts: dict[str, int] = {}
    if isinstance(raw_no_test_counts, dict):
        for reason_key, count in raw_no_test_counts.items():
            if isinstance(count, bool) or not isinstance(count, (int, float)):
                continue
            no_test_counts[str(reason_key)] = int(count)
    if no_test_counts:
        block["no_test_block_counts"] = no_test_counts

    # Additive history fields. A legacy four-key-only record (no list, no tally)
    # simply omits these — older consumers keep working unchanged.
    raw_outcomes = raw.get("recent_outcomes")
    outcomes = (
        [o for o in raw_outcomes if isinstance(o, dict)]
        if isinstance(raw_outcomes, list)
        else []
    )
    if outcomes:
        block["recent_outcomes"] = outcomes

    raw_counts = raw.get("unverified_reason_counts")
    counts: dict[str, int] = {}
    if isinstance(raw_counts, dict):
        for reason_key, count in raw_counts.items():
            try:
                counts[str(reason_key)] = int(count)
            except (TypeError, ValueError):
                continue
    if counts:
        block["unverified_reason_counts"] = counts

    # Derived at-a-glance summary over the retained window. Only emitted when
    # there is real history to summarize (never for a legacy/never-ran record).
    if outcomes:
        verified_runs = sum(1 for o in outcomes if o.get("verified"))
        fell_open_runs = len(outcomes) - verified_runs
        most_common_reason = None
        if counts:
            # Deterministic tie-break: highest count, then reason key.
            most_common_reason = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[
                0
            ][0]
        block["summary"] = {
            "window": len(outcomes),
            "verified_runs": verified_runs,
            "fell_open_runs": fell_open_runs,
            "total_fall_opens": sum(counts.values()) if counts else fell_open_runs,
            "most_common_reason": most_common_reason,
        }
    return block


def should_run_no_ci_test_gate(
    project, branch: str | None, *, gate_enabled: bool
) -> bool:
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


# Action a stranded no-checks escalation should take, decided by
# :func:`resolve_no_checks_escalation`.
NoChecksEscalationAction = Literal["keep_polling", "verify_tests", "merge_unverified"]


def resolve_no_checks_escalation(
    project, branch: str | None, *, gate_enabled: bool
) -> NoChecksEscalationAction:
    """Decide how the orchestrator's no-checks escalation backstop should resolve
    a PR that has waited ~600s with no CI checks ever appearing.

    Historically the escalation block raised ONE failure alert and then kept
    polling forever, so a no-CI repo's PR (and every rollup that targets a real
    base) had to be hand-merged. This backstop decides whether the strand can be
    auto-resolved instead — strictly gated on :func:`proceed_on_no_checks` so a
    CI-gated repo is NEVER auto-merged:

      - ``"keep_polling"`` — :func:`proceed_on_no_checks` is False (the repo is
        CI-gated: ``has_ci=True``, or ``has_ci=None``/unknown). A missing check
        means 'not registered yet', never 'will never run', so today's
        alert-and-wait behavior is preserved EXACTLY — do NOT auto-merge.
      - ``"verify_tests"`` — the strand can be resolved AND the repo has a
        detectable test suite (:func:`should_run_no_ci_test_gate` is True): route
        through the SAME no-CI verify gate the definitive-``no_checks`` branch
        uses, so the merge is still gated on a ``verify_tests`` verdict.
      - ``"merge_unverified"`` — the strand can be resolved but the repo is truly
        test-less (or the gate kill-switch is off, or the base is an internal
        staging branch): proceed UNVERIFIED, mirroring the definitive-``no_checks``
        immediate-pass.

    Pure decision function (no I/O), so the orchestrator's inline backstop and its
    tests call the SAME predicate rather than a hand-copied branch.
    """
    if not proceed_on_no_checks(project, branch):
        return "keep_polling"
    if should_run_no_ci_test_gate(project, branch, gate_enabled=gate_enabled):
        return "verify_tests"
    return "merge_unverified"


# Outcome of mapping a MERGE_TEST_RESULT verdict onto the merge gate.
MergeTestOutcome = Literal["pass", "fail", "undetected"]


# Conservative threshold for flagging a PASSED suite as "heavily skipped": the
# run reported a best-effort ``coverage`` summary in which almost every test was
# skipped/deselected/ignored/filtered (e.g. ``5 passed, 1500 skipped``). This is
# purely an operator-visible signal — it NEVER changes the pass/fail decision —
# so the bar is deliberately high to avoid noise on ordinary runs that skip a
# handful of tests. ``_HEAVY_SKIP_MIN_TOTAL`` keeps tiny suites (where a couple
# of skips is meaningless) out of the signal; ``_HEAVY_SKIP_RATIO`` requires the
# skip fraction to be at/above 90% of the total.
_HEAVY_SKIP_MIN_TOTAL = 10
_HEAVY_SKIP_RATIO = 0.9


def _heavy_skip_coverage_summary(coverage: Any) -> str:
    """Return a concise ``"passed N, skipped M of T"`` summary when a PASSED
    suite's best-effort ``coverage`` block shows a heavy skip ratio, else ``""``.

    ``coverage`` is the optional ``{passed, skipped, total}`` block the agent
    attaches to a detected+passed verdict. Defensive about a missing / non-dict /
    malformed / older-agent block (returns ``""``), and about a tiny suite or a
    low skip ratio (returns ``""`` — behaves exactly as today). When the skip
    fraction is at/above :data:`_HEAVY_SKIP_RATIO` and the total is at/above
    :data:`_HEAVY_SKIP_MIN_TOTAL`, returns the human summary so the caller can
    surface it as ``detail`` WITHOUT changing the (non-blocking) ``"pass"``.
    """
    if not isinstance(coverage, dict):
        return ""
    try:
        total = int(coverage.get("total"))
        skipped = int(coverage.get("skipped"))
        passed = int(coverage.get("passed"))
    except (TypeError, ValueError):
        return ""
    if total < _HEAVY_SKIP_MIN_TOTAL or skipped < 0 or total <= 0:
        return ""
    if skipped / total < _HEAVY_SKIP_RATIO:
        return ""
    return f"passed {passed}, skipped {skipped} of {total}"


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

    ``detail`` is the failing-test output for ``"fail"`` and ``""`` otherwise —
    EXCEPT a detected+passed suite whose best-effort ``coverage`` summary
    (``{passed, skipped, total}``) shows a heavy skip ratio: there the outcome
    stays a non-blocking ``"pass"`` but ``detail`` carries a concise
    ``"passed N, skipped M of T"`` summary so an operator can distinguish a
    near-empty run (e.g. ``5 passed, 1500 skipped``) from a fully-exercised pass.
    Absent / below-threshold / malformed coverage keeps the empty detail exactly
    (older agents that emit no ``coverage`` are unaffected).

    Declared build probe (HIGH precedence, runs regardless of suite detection):
    when the project declares a ``build_cmd``, the agent runs it as a goal-level
    acceptance ``probe`` (carrying ``declared=true``) on BOTH the detected=false
    fall-open paths AND the detected=true + passed path. A CLEAR declared-build
    failure — it ran, did not pass, and was NOT a could-not-run degrade — maps to
    ``"fail"`` EVEN WHEN a unit suite was detected and passed, because a green
    suite must not mask a broken declared build (bundled frontends, tests that
    exercise source without the build step). See
    :func:`declared_build_is_clear_failure`.

    Generic acceptance probe (``detected=false`` only): when no unit suite ran, the
    agent may attach a cheap GENERIC goal-level ``probe`` ("does the changed code
    still compile/build/boot"). A CLEAR generic-probe failure — it ran, did not
    pass, and was NOT a could-not-run degrade — maps to ``"fail"`` on the
    detected=false path so the existing block-and-fix path fires carrying the probe
    output. Every other probe state (pass / could-not-run / absent / old-shape
    verdict) keeps today's ``"undetected"`` fall-open, so missing-deps /
    unprovisioned setup never hard-blocks a merge.

    Documented-command probe (HIGH precedence, runs regardless of suite
    detection): the agent may attach a ``documentedCommands`` block describing the
    outcome of running a command the PR INTRODUCES or DOCUMENTS (a newly added
    package.json script, Makefile/justfile/Taskfile target, declared
    console-script entrypoint, or a command shown in a touched README/docs block).
    A CLEAR documented-command failure — it ran, did not pass, and was NOT a
    could-not-run degrade — maps to ``"fail"`` EVEN WHEN a unit suite was detected
    and passed, because the documented command is a DISTINCT claim the PR
    introduced that must be exercised. Every other state (pass / could-not-run /
    absent / old-shape verdict) does NOT block — an older agent that emits no
    ``documentedCommands`` behaves exactly as before.

    Newly-disabled tests (VISIBILITY-ONLY, never blocks): the agent may attach a
    ``disabledTests`` block when the PR's own diff NEWLY introduces test-skip /
    disable markers. It is parsed defensively via :func:`disabled_tests_block` and
    NEVER changes the merge decision — on a non-blocking ``"pass"`` it only
    enriches the operator-facing ``detail`` with a concise "PR newly disables N
    test(s)" note. Absent / malformed / older-agent verdicts keep today's detail
    byte-for-byte (the note is appended ONLY when a well-formed block is present).
    """
    if not isinstance(verdict, dict):
        return ("undetected", "")
    # HIGH precedence: a documented / PR-introduced command that CLEARLY failed
    # blocks the merge regardless of whether a unit suite was detected & passed.
    documented = verdict.get("documentedCommands")
    if _documented_command_is_clear_failure(documented):
        output = str(documented.get("output") or "").strip()
        label = str(documented.get("label") or "").strip()
        prefix = (
            f"PR-documented command `{label}` failed: "
            if label
            else "PR-documented command failed: "
        )
        return (
            "fail",
            (prefix + output)
            if output
            else (prefix + "the documented command did not succeed."),
        )
    # Partial-setup failure: SOME-but-not-all test runners failed environment
    # setup, so at least one suite never executed (a polyglot repo where e.g. the
    # Python suite ran but the TS runner's ``npm ci`` failed). The runners that
    # ran may have passed, but a clean ``detected=false`` fall-open here would
    # silently merge with an entire suite unverified. Treat it as a BLOCK (fail)
    # — never a clean pass or an undetected fall-open — mirroring the
    # all-runners-failed escalation while staying distinguishable via the flag.
    # Checked BEFORE the detected/passed branches so it cannot be masked by a
    # sibling runner's pass. Defensive about an older agent that never emits the
    # flag (falsy → falls through to existing behavior).
    if verdict.get("partialSetupFailed"):
        output = str(verdict.get("output") or "").strip()
        return (
            "fail",
            output
            or "Partial setup failure: one or more test runners could not run "
            "their suite (environment/setup error); the suite is only partially "
            "verified.",
        )
    # PR-CAUSED SELF-SUPPRESSION (blocks): the PR's OWN diff newly introduced
    # skip/disable markers (a well-formed ``disabledTests`` block) AND the
    # resulting suite is non-verifying — either it all-skipped (every test
    # skipped) or it "passed" with a heavy/mostly-skipped coverage summary. An
    # agent must not be able to make a red suite green by disabling its own
    # tests. Checked AFTER documented-command / partial-setup but BEFORE the
    # ``detected is False`` fall-open (the all-skipped case) and BEFORE the
    # ``passed`` branch (the mostly-skipped case), so both shapes are
    # intercepted first. Conservative by construction: it fires ONLY on
    # PR-introduced self-suppression of the whole/near-whole suite — an
    # environmental all-skip (no ``disabledTests``) or a small flaky quarantine
    # (suite still genuinely runs) does NOT meet the conjunction and keeps its
    # existing non-blocking behavior.
    if pr_self_suppressed_suite(verdict):
        disabled = disabled_tests_block(verdict)
        count = disabled["count"] if disabled else 0
        marker_list = ", ".join(
            f"{m['marker']} in {m['file']}".strip()
            for m in (disabled["markers"] if disabled else [])
            if m.get("marker") or m.get("file")
        )
        suffix = f" (markers: {marker_list})" if marker_list else ""
        return (
            "fail",
            f"PR newly disabled {count} test(s) and thereby self-suppressed the "
            f"suite — the test run no longer verifies anything (all/near-all "
            f"tests skipped), so the suite was made green by skipping rather "
            f"than by passing{suffix}.",
        )
    # HIGH precedence: a DECLARED acceptance test (the named test the plan
    # promised would exercise the seam) that was ABSENT or SKIPPED in the PR's run
    # did NOT prove the claim — block the merge REGARDLESS of whether the unit
    # suite was detected and passed, exactly the way compute_experiment fails
    # closed when its declared success metric is absent from the result. A green
    # suite must not let a promised-but-missing/never-run test merge as proven. A
    # check that itself could_not_run (no targetable suite / environment failure)
    # does NOT block here — it falls open with the ``acceptance_test_not_run``
    # degraded reason via :func:`classify_unverified_fall_open_reason`.
    if acceptance_test_is_clear_failure(acceptance_test_block(verdict)):
        return ("fail", _acceptance_test_failure_detail(acceptance_test_block(verdict)))
    # HIGH precedence: a DECLARED acceptance test that RAN GREEN but whose test
    # FILE is NOT among the PR's changed files. It name-drops a pre-existing,
    # unmodified test, so the post-merge named_test gate would mint proven=False
    # and the coverage surface never flips — yet nothing warned pre-merge. Block
    # the merge so the failure is visible and fixable (co-modify the declared
    # test file) BEFORE the merge. Only fires on a definitive ``in_diff is False``;
    # an absent ``in_diff`` (older agent / diff unavailable) keeps today's behavior.
    if acceptance_test_not_in_diff_is_clear_failure(acceptance_test_block(verdict)):
        return (
            "fail",
            _acceptance_test_not_in_diff_detail(acceptance_test_block(verdict)),
        )
    # HIGH precedence: the operator's OWN declared build command CLEARLY failed.
    # A green unit suite must NOT mask a broken declared build (bundled frontends,
    # tests that exercise source without the build step), so a declared-build
    # clear failure blocks the merge REGARDLESS of whether a suite was detected and
    # passed. Generic (non-declared) probes keep the detected=false-only semantics
    # below. Checked here so a passing suite cannot fall through to "pass".
    if declared_build_is_clear_failure(verdict.get("probe")):
        return ("fail", _declared_build_failure_detail(verdict["probe"]))
    if verdict.get("detected") is False:
        if probe_is_clear_failure(verdict.get("probe")):
            # A declared-build clear failure was already handled at HIGH
            # precedence above (it blocks regardless of suite detection); only a
            # GENERIC compile/build probe failure reaches here.
            probe = verdict["probe"]
            output = str(probe.get("output") or "").strip()
            return (
                "fail",
                output
                or "Acceptance probe failed: the changed code does not compile/build.",
            )
        return ("undetected", "")
    if verdict.get("passed"):
        # A detected suite ran and passed → non-blocking ``"pass"``. If the agent
        # attached a best-effort ``coverage`` summary showing almost every test
        # was skipped/deselected (a near-empty run like ``5 passed, 1500
        # skipped``), enrich the operator-visible ``detail`` with a concise
        # coverage summary so the gate is distinguishable from a fully-exercised
        # pass. The OUTCOME stays ``"pass"`` — this is the conservative,
        # stabilize-phase gate and a heavily-skipped pass never blocks a merge.
        # Absent / below-threshold / malformed coverage keeps today's empty
        # detail exactly (older-agent compatible).
        detail = _heavy_skip_coverage_summary(verdict.get("coverage"))
        # VISIBILITY-ONLY: if the PR newly disabled tests, annotate the (still
        # non-blocking) pass so an operator can see a green suite may have been
        # made green by skipping. Never flips the outcome; appended ONLY when a
        # well-formed block is present, so an absent field keeps detail unchanged.
        disabled = disabled_tests_block(verdict)
        if disabled:
            note = f"PR newly disables {disabled['count']} test(s)"
            detail = f"{detail}; {note}" if detail else note
        return ("pass", detail)
    output = str(verdict.get("output") or "").strip()
    return ("fail", output or "Detected test suite failed (no output captured).")


def probe_is_clear_failure(probe: Any) -> bool:
    """Whether an acceptance ``probe`` block represents a CLEAR, blocking failure.

    True only when the probe ran, was not a could-not-run degrade, and did not
    pass — i.e. the changed code genuinely failed to compile/build. Defensive
    about a missing / non-dict / old-shape probe (returns False), so a verdict
    from an older agent that emits no probe keeps the undetected fall-open.

    Public so the orchestrator can redirect a setup-failure (FAILED) verify task
    that nonetheless ran the probe and caught a clear build break into a merge
    BLOCK, rather than the crash-retry-then-fall-open path.
    """
    if not isinstance(probe, dict):
        return False
    if not probe.get("ran"):
        return False
    if probe.get("could_not_run"):
        return False
    return probe.get("passed") is False


def declared_build_is_clear_failure(probe: Any) -> bool:
    """Whether an acceptance ``probe`` is the operator's OWN declared build that
    CLEARLY failed — a blocking failure that fires REGARDLESS of suite detection.

    Narrower than :func:`probe_is_clear_failure`: it additionally requires the
    probe to be the project's declared ``build_cmd`` (``declared is True``). A
    declared build that clearly failed blocks the merge even when a unit suite was
    detected and passed, because a green suite must not mask a broken declared
    build. A GENERIC (non-declared) compile probe is unaffected here — it keeps
    its existing detected=false-only handling. Defensive about a missing / non-dict
    / generic / old-shape probe (returns False).
    """
    if not isinstance(probe, dict):
        return False
    if probe.get("declared") is not True:
        return False
    return probe_is_clear_failure(probe)


def _declared_build_failure_detail(probe: dict[str, Any]) -> str:
    """Build the operator-facing ``detail`` for a CLEAR declared-build failure.

    Shared by the HIGH-precedence declared-build block (which fires regardless of
    suite detection) so the wording stays identical to the historical
    detected=false declared-build path. Surfaces the declared command label and
    its captured output so the spawned fix task knows the AUTHORITATIVE build (not
    a generic compile probe) is what broke.
    """
    output = str(probe.get("output") or "").strip()
    label = str(probe.get("label") or "").strip()
    prefix = (
        f"Declared build command `{label}` failed: "
        if label
        else "Declared build command failed: "
    )
    return (
        (prefix + output)
        if output
        else (prefix + "the declared build did not succeed.")
    )


def _documented_command_is_clear_failure(documented: Any) -> bool:
    """Whether a ``documentedCommands`` block represents a CLEAR, blocking failure.

    Mirrors :func:`_probe_is_clear_failure` for the diff-aware documented /
    PR-introduced command probe (same ``ran``/``could_not_run``/``passed`` shape).
    True only when the command ran, was not a could-not-run degrade, and did not
    pass. Defensive about a missing / non-dict / old-shape field (returns False),
    so a verdict from an older agent that emits no ``documentedCommands`` keeps
    today's gate behavior exactly.
    """
    if not isinstance(documented, dict):
        return False
    if not documented.get("ran"):
        return False
    if documented.get("could_not_run"):
        return False
    return documented.get("passed") is False


# Hard cap on disabled-test markers surfaced from a verdict, mirroring the
# agent-side ``MAX_DISABLED_TEST_MARKERS`` so a PR that mass-disables a suite
# cannot bloat the API response even if a malformed/oversized artifact slips in.
_MAX_DISABLED_TEST_MARKERS = 20


def disabled_tests_block(verdict: Any) -> dict[str, Any] | None:
    """Defensively normalize the optional ``disabledTests`` VISIBILITY block an
    agent may attach when a PR's own diff NEWLY introduces test-skip/disable
    markers (``it.skip``, ``@pytest.mark.skip``, ``t.Skip(``, ``#[ignore]`` …).

    Returns ``{"count": int, "markers": [{"file", "marker", "line"}, …]}`` when a
    well-formed block with a positive count is present, else ``None``. This is a
    pure read-side accessor — it NEVER affects the merge decision (see
    :func:`classify_merge_test_verdict`). Defensive about a missing / non-dict /
    malformed / older-agent block (returns ``None``) exactly like the other
    optional-block parsers, so an agent that omits it degrades silently and the
    gate is unchanged. Markers are bounded to :data:`_MAX_DISABLED_TEST_MARKERS`.
    """
    if not isinstance(verdict, dict):
        return None
    raw = verdict.get("disabledTests")
    if not isinstance(raw, dict):
        return None
    try:
        count = int(raw.get("count"))
    except (TypeError, ValueError):
        return None
    if count <= 0:
        return None
    markers: list[dict[str, str]] = []
    raw_markers = raw.get("markers")
    if isinstance(raw_markers, list):
        for m in raw_markers[:_MAX_DISABLED_TEST_MARKERS]:
            if not isinstance(m, dict):
                continue
            markers.append(
                {
                    "file": str(m.get("file") or ""),
                    "marker": str(m.get("marker") or ""),
                    "line": str(m.get("line") or ""),
                }
            )
    return {"count": count, "markers": markers}


def acceptance_test_block(verdict: Any) -> dict[str, Any] | None:
    """Defensively normalize the optional ``acceptanceTest`` block an agent
    attaches when the implementation task declared (at plan time) a named
    acceptance test (``acceptance_evidence`` with ``kind="test"``).

    Returns ``{"declared", "descriptor", "selector"?, "present", "ran",
    "status", "passed"}`` when a well-formed ``declared=true`` block with a
    recognized ``status`` is present, else ``None``. ``status`` is one of ``ran``
    / ``skipped`` / ``absent`` / ``could_not_run``. ``passed`` is a defensive bool
    (default ``False`` on a missing / non-bool value) marking that the TARGETED
    run of ONLY the declared test exited green — an older agent that omits the
    field simply yields ``passed=False`` (today's behavior, no green signal). Pure
    read-side accessor, mirroring :func:`disabled_tests_block`: defensive about a
    missing / non-dict / malformed / older-agent block (returns ``None``), so an
    agent that omits it degrades silently and the gate is unchanged.
    """
    if not isinstance(verdict, dict):
        return None
    raw = verdict.get("acceptanceTest")
    if not isinstance(raw, dict):
        return None
    if raw.get("declared") is not True:
        return None
    status = str(raw.get("status") or "").strip().lower()
    if status not in {"ran", "skipped", "absent", "could_not_run"}:
        return None
    descriptor = str(raw.get("descriptor") or "").strip()
    block: dict[str, Any] = {
        "declared": True,
        "descriptor": descriptor,
        "present": bool(raw.get("present")),
        "ran": bool(raw.get("ran")),
        "status": status,
        # Defensive: only a literal ``True`` counts as green. A missing /
        # non-bool value (older agent, malformed artifact) reads as False so no
        # spurious green signal is ever minted.
        "passed": raw.get("passed") is True,
    }
    selector = raw.get("selector")
    if isinstance(selector, str) and selector.strip():
        block["selector"] = selector.strip()
    # Optional pre-merge cross-check the agent computes against the PR's own
    # changed files — the SAME changed-files requirement the acceptance gate
    # (``acceptance_gate._evaluate_named_test``) checks FIRST. Carried through
    # ONLY when the agent emitted a literal bool: ``True``/``False`` are surfaced,
    # a missing / non-bool value (older agent) is left absent so the gate is
    # unchanged. A green run reported with ``in_diff=False`` can never earn
    # machine-proven coverage, so :func:`acceptance_test_is_green` withholds the
    # targeted-green mint for it, and
    # :func:`acceptance_test_not_in_diff_is_clear_failure` surfaces it as a CLEAR
    # pre-merge failure rather than letting it merge badge-only.
    in_diff = raw.get("in_diff")
    if isinstance(in_diff, bool):
        block["in_diff"] = in_diff
    return block


def acceptance_test_is_clear_failure(block: Any) -> bool:
    """Whether a declared acceptance-test block is a CLEAR, blocking failure — a
    promised test that was ABSENT or SKIPPED (it did not actually run).

    True only when the block is the declared acceptance test, it did NOT run, and
    its status is the definitive ``absent`` or ``skipped``. A ``could_not_run``
    status is NOT a clear failure: the check itself could not be performed (no
    targetable suite / environment failure), so it degrades to a fall-open
    (``acceptance_test_not_run``) rather than hard-blocking. Defensive about a
    missing / non-dict / old-shape block (returns False), so an older agent that
    emits no ``acceptanceTest`` keeps today's gate behavior exactly.
    """
    if not isinstance(block, dict):
        return False
    if block.get("declared") is not True:
        return False
    if block.get("ran") is True:
        return False
    return block.get("status") in {"absent", "skipped"}


def acceptance_test_is_green(block: Any) -> bool:
    """Whether a declared acceptance-test block proves the promised test ran GREEN.

    True only when the block is the declared acceptance test, it actually ran
    (``ran is True`` AND ``status == "ran"``), AND the targeted run of ONLY that
    declared test exited green (``passed is True``). Because the agent targets
    just the declared test, this is honest machine-checked proof of the exact
    claim WITHOUT running the whole suite — the no-CI merge gate can mint
    ``proven`` from it even when the full-suite gate fell open unverified.

    Defensive about a missing / non-dict / old-shape block, and about an older
    agent that omits ``passed`` (:func:`acceptance_test_block` defaults it to
    False): all yield False, so today's behavior is preserved and no green signal
    is minted on absence.

    A block that EXPLICITLY reports ``in_diff=False`` — the declared test is not
    among the PR's own changed files — is NOT green here even if it ran and passed:
    the acceptance gate checks the changed-files requirement FIRST and would
    hard-fail ``proven=False`` regardless of a targeted-green run, so minting a
    targeted-green signal for it would advertise a green the gate rejects and mask
    the real (pre-merge) verdict. An older agent that omits ``in_diff`` is
    unaffected (the key is absent, not ``False``), preserving today's behavior.
    """
    if not isinstance(block, dict):
        return False
    if block.get("declared") is not True:
        return False
    if block.get("in_diff") is False:
        return False
    if block.get("ran") is not True:
        return False
    if block.get("status") != "ran":
        return False
    return block.get("passed") is True


def acceptance_test_not_in_diff_is_clear_failure(block: Any) -> bool:
    """Whether a declared acceptance-test block names a test that RAN GREEN but
    whose test FILE is NOT among the PR's own changed files — a name-dropped,
    pre-existing/unmodified test that would silently merge badge-only.

    This is the gap the post-merge ``acceptance_gate._evaluate_named_test`` gate
    leaves invisible pre-merge: that gate mints ``proven=True`` ONLY when the
    declared test is among the changed files, so a PR that references a green but
    UNMODIFIED test merges with ``proven=False`` and never flips the coverage
    surface — yet nothing warns before merge. Surfacing this as a CLEAR failure
    lets the merge gate block-and-fix it (co-modify the declared test) BEFORE the
    merge instead of after.

    True only when the block is the declared acceptance test, it actually RAN
    (``ran is True`` AND ``status == "ran"`` — an ``absent``/``skipped`` test is
    already a clear failure via :func:`acceptance_test_is_clear_failure`), AND
    ``in_diff`` is definitively ``False``. An ABSENT ``in_diff`` (older agent, or
    the PR diff could not be computed) is indeterminate and never blocks — the
    merge keeps today's behavior. Defensive about a missing / non-dict / old-shape
    block (returns False).
    """
    if not isinstance(block, dict):
        return False
    if block.get("declared") is not True:
        return False
    if block.get("ran") is not True:
        return False
    if block.get("status") != "ran":
        return False
    return block.get("in_diff") is False


def _acceptance_test_not_in_diff_detail(block: dict[str, Any]) -> str:
    """Build the operator-facing ``detail`` for a declared acceptance test that
    ran green but is not among the PR's changed files, so the spawned fix task
    knows it must co-modify the declared test file to earn proven coverage."""
    name = str(block.get("selector") or block.get("descriptor") or "").strip()
    label = f"`{name}` " if name else ""
    return (
        f"Declared acceptance test {label}ran green but its test file is NOT among "
        "this PR's changed files — the merge would earn only a badge "
        "(proven=False) and never flip the coverage surface. Co-modify (add or "
        "edit) the declared test file in THIS PR so its coverage is proven."
    ).replace("  ", " ")


def _acceptance_test_failure_detail(block: dict[str, Any]) -> str:
    """Build the operator-facing ``detail`` for a CLEAR declared-acceptance-test
    failure so the spawned fix task knows which promised test must run."""
    name = str(block.get("selector") or block.get("descriptor") or "").strip()
    status = str(block.get("status") or "").strip()
    label = f"`{name}` " if name else ""
    if status == "absent":
        reason = "is absent — the promised test was not found in the PR's run"
    elif status == "skipped":
        reason = "was skipped — the promised test did not actually execute"
    else:  # defensive; only absent/skipped reach here
        reason = "did not run"
    return f"Declared acceptance test {label}{reason}.".replace("  ", " ")


def _suite_is_non_verifying(verdict: dict[str, Any]) -> bool:
    """Whether a verdict's suite did not really verify anything — EITHER it ran
    but every test was skipped (``noTestsReason`` in
    :data:`_ALL_SKIPPED_NO_TESTS_REASONS`) OR it ``passed`` with a heavy/mostly
    -skipped coverage summary (per :func:`_heavy_skip_coverage_summary`, the
    same 90%-skip threshold used for the operator-visible heavy-skip note).

    Read-only; does not change the merge decision on its own.
    """
    no_tests_reason = str(verdict.get("noTestsReason") or "").strip().lower()
    if no_tests_reason in _ALL_SKIPPED_NO_TESTS_REASONS:
        return True
    if verdict.get("passed") and _heavy_skip_coverage_summary(verdict.get("coverage")):
        return True
    return False


def capability_proof_trial_suite_verifying(result: Any) -> bool:
    """Whether a ``capability_proof_trial`` result's executed covering suite
    actually verified something — it ran at least one non-skipped test.

    Mirrors the intent of :func:`_suite_is_non_verifying`: an executed run where
    every collected test was skipped verified nothing and must NOT mint a proof.
    The agent's targeted-run classification already reports ``ran=False`` for an
    all-skipped / absent suite (so :func:`capability_proof_trial_is_green` rejects
    it), and this is the belt-and-suspenders check on the informational
    ``skipped_summary`` block: non-verifying ONLY on POSITIVE evidence of an
    all-skipped run (``total > 0 and skipped >= total``). A missing / partial /
    zero-total summary is treated as verifying, so an agent that could not parse
    exact counts (but did classify ``ran``) is not spuriously rejected.
    """
    if not isinstance(result, dict):
        return False
    summary = result.get("skipped_summary")
    if isinstance(summary, dict):
        total = summary.get("total")
        skipped = summary.get("skipped")
        if (
            isinstance(total, int)
            and total > 0
            and isinstance(skipped, int)
            and skipped >= total
        ):
            return False
    return True


def capability_proof_trial_is_green(result: Any) -> bool:
    """Whether a ``capability_proof_trial`` result proves the covering tests ran
    GREEN — the machine evidence that mints a genuine ``proven`` named_test proof.

    True only when the targeted run of ONLY the capability's declared covering
    tests actually executed and passed: ``could_not_run`` is not set, ``ran`` is
    True, ``passed`` is True, AND the suite was verifying (not all-skipped, per
    :func:`capability_proof_trial_suite_verifying`). Mirrors the conservative
    shape of :func:`acceptance_test_is_green`: every other combination — an
    un-targetable ref (``could_not_run``), a red/failing run, a skipped/absent
    suite, or a missing/old-shape result — yields False, so no proof is minted on
    ambiguity and a red trial never fabricates a green.
    """
    if not isinstance(result, dict):
        return False
    if result.get("could_not_run") is True:
        return False
    if result.get("ran") is not True:
        return False
    if result.get("passed") is not True:
        return False
    return capability_proof_trial_suite_verifying(result)


def criterion_coverage_trial_is_green(result: Any) -> bool:
    """Whether a ``criterion_coverage_trial`` result proves the covering tests ran
    GREEN against the enrolled checkout — the machine evidence that mints a genuine
    ``covered`` criterion-evidence tier (``executed_named_test``).

    Analogous to :func:`capability_proof_trial_is_green` but keyed on acceptance
    criteria rather than capabilities (the two earn paths are deliberately NOT
    entangled). True only when ALL hold:

      * ``could_not_run`` is not set — the targeted run was actually possible,
      * ``ran`` is True and ``passed`` is True — the tests executed and passed,
      * the executed checkout is identified: ``head_sha`` is a non-empty string
        (there must be a concrete SHA to PIN into the ``covered`` evidence), and
      * the executed SHA MATCHES the enrolled checkout: when the receipt echoes a
        non-empty ``queued_head_sha`` (the enrolled ``head_sha``), it must equal
        ``head_sha``. A ``queued_head_sha`` that is absent/blank means the
        enrollment was revision-unbound, so any executed ``head_sha`` is accepted
        and pinned (mirroring the capability trial's optional-enrolled-SHA shape).

    Every other combination — an un-targetable run (``could_not_run``), a
    red/failing run, a missing executed SHA, a SHA mismatch, or a missing/old-shape
    result — yields False, so no ``covered`` tier is minted on ambiguity and a red
    or drifted trial never fabricates coverage.
    """
    if not isinstance(result, dict):
        return False
    if result.get("could_not_run") is True:
        return False
    if result.get("ran") is not True:
        return False
    if result.get("passed") is not True:
        return False
    head_sha = result.get("head_sha")
    if not isinstance(head_sha, str) or not head_sha.strip():
        return False
    queued = result.get("queued_head_sha")
    if isinstance(queued, str) and queued.strip() and queued.strip() != head_sha.strip():
        return False
    return True


def pr_self_suppressed_suite(verdict: Any) -> bool:
    """Whether the PR's OWN diff made the suite stop verifying by disabling tests.

    True ONLY when BOTH hold:
      (a) the PR newly introduced skip/disable markers — :func:`disabled_tests_block`
          returns a non-``None`` block; AND
      (b) the resulting suite is non-verifying — :func:`_suite_is_non_verifying`
          (all-skipped, or a heavily/mostly-skipped pass).

    The conjunction is deliberately narrow so it stays safe for the stabilize
    phase: an environmental all-skip / heavy-skip where the PR did NOT introduce
    the skips (no ``disabledTests`` block) returns False and keeps falling open /
    passing; a small flaky quarantine where a few tests are disabled but the
    suite still genuinely runs (not all/near-all skipped) also returns False and
    keeps its visibility-only pass. Defensive about a missing / old-shape verdict
    (returns False) like the other optional-block accessors.
    """
    if not isinstance(verdict, dict):
        return False
    if disabled_tests_block(verdict) is None:
        return False
    return _suite_is_non_verifying(verdict)


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


# --- Single source of truth: benign-vs-degraded UNVERIFIED-merge reasons -----
#
# The no-CI merge gate records WHY a PR merged without real test verification in
# ``review_loop["unverified_merge_reason"]`` (the reason KEYS are minted by
# :func:`classify_unverified_fall_open_reason` here and at the orchestrator's
# two other fall-open decision sites). Two downstream layers must agree on
# whether a given reason is *benign* (the project legitimately has no runnable
# suite, or only positive execution signals were obtained — do NOT penalize) or
# *degraded* (verification was expected but an environment/setup failure
# prevented it — escalate loudly):
#   - :mod:`app.alert_dispatch` maps these onto ``AlertSeverity`` (WARNING vs
#     ERROR), and
#   - success-scoring analytics distinguish a *verified* merge from one the gate
#     merged UNVERIFIED.
# Keep that partition HERE, in one dependency-light leaf (this module imports
# only ``app.github``), so the two layers cannot drift. ``alert_dispatch``
# DERIVES ``_UNVERIFIED_REASON_SEVERITY`` from these sets rather than re-listing
# the keys. Their union is the canonical emitted reason set (pinned by
# ``test_invariants._UNVERIFIED_REASON_KEYS``); they are disjoint by contract.
DEGRADED_UNVERIFIED_REASONS = frozenset(
    {
        "verify_setup_failed",
        "undetected_runtime",
        "suite_all_skipped",
        "probe_could_not_run",
        "probe_no_artifact",
        "documented_command_could_not_run",
        "documented_command_no_artifact",
        "acceptance_test_not_run",
    }
)
BENIGN_UNVERIFIED_REASONS = frozenset(
    {
        "no_detectable_test_command",
        "kill_switch_off",
        "internal_staging_base",
        "probe_passed",
        "documented_command_passed",
    }
)


def is_degraded_unverified_reason(reason: str | None) -> bool:
    """Whether a no-CI UNVERIFIED-merge reason key marks a *degraded* fall-open.

    Degraded reasons mean verification was expected but an environment/setup
    failure prevented it (escalate). Benign reasons mean no verification was
    expected, or only positive execution signals were obtained. Unknown / None
    keys are treated as NOT degraded, mirroring ``alert_dispatch``'s WARNING
    default — a key with no explicit classification never silently escalates.
    """
    return reason in DEGRADED_UNVERIFIED_REASONS


def block_degraded_unverified_merge_enabled(project) -> bool:
    """Whether the project opted into the honest-block policy (default OFF).

    Reads ``build_config.block_degraded_unverified_merge``. Absent / falsy ==
    OFF == today's behavior for every existing project. Defensive about a
    missing / malformed ``build_config`` so it can be called on any project.
    """
    return bool(
        (getattr(project, "build_config", None) or {}).get(
            "block_degraded_unverified_merge", False
        )
    )


def should_block_degraded_unverified_merge(project, unverified_reason) -> bool:
    """Whether the no-CI merge gate should HONESTLY BLOCK (hold the PR open)
    instead of merging it UNVERIFIED.

    Returns True ONLY when BOTH hold:
      - the project's opt-in policy flag is ON
        (``build_config.block_degraded_unverified_merge``); AND
      - the fall-open reason is DEGRADED — the gate EXPECTED to verify but could
        NOT run the suite (:func:`is_degraded_unverified_reason`).

    NEVER True for a benign reason (there was honestly nothing to verify — those
    always merge UNVERIFIED as today) and NEVER True when the policy is off (the
    default), so today's behavior is byte-for-byte unchanged for every existing
    project. Defensive about ``project is None`` / missing build_config.
    """
    if not block_degraded_unverified_merge_enabled(project):
        return False
    return is_degraded_unverified_reason(unverified_reason)


def block_no_test_unverified_merge_enabled(project) -> bool:
    """Whether the project opted into the no-test honest-block policy (default OFF).

    Reads ``build_config.block_no_test_unverified_merge``. Absent / falsy ==
    OFF == today's behavior for every existing project. Defensive about a
    missing / malformed ``build_config`` so it can be called on any project.
    """
    return bool(
        (getattr(project, "build_config", None) or {}).get(
            "block_no_test_unverified_merge", False
        )
    )


def should_block_no_test_unverified_merge(project, unverified_reason) -> bool:
    """Whether the no-CI merge gate should HONESTLY BLOCK when there is no
    detectable test command, instead of merging UNVERIFIED.

    Returns True ONLY when BOTH hold:
      - the project's opt-in policy flag is ON
        (``build_config.block_no_test_unverified_merge``); AND
      - the fall-open reason is ``'no_detectable_test_command'`` (a BENIGN
        reason — nothing to verify — but the operator wants human review).

    NEVER True for DEGRADED reasons (those are covered by the separate
    ``block_degraded_unverified_merge`` policy) and NEVER True when the policy
    is off (the default), so today's behavior is byte-for-byte unchanged for
    every existing project. Defensive about ``project is None`` / missing
    build_config.
    """
    if not block_no_test_unverified_merge_enabled(project):
        return False
    return unverified_reason == "no_detectable_test_command"


def record_no_ci_gate_no_test_block(
    db_session,
    project,
    *,
    unverified_reason: str | None,
    when,
) -> None:
    """Best-effort persist that the no-CI gate HONESTLY BLOCKED a no-test
    unverified merge — the opt-in ``block_no_test_unverified_merge`` policy is
    ON and the gate fell open with ``no_detectable_test_command`` (no suite to run).

    Records the same terminal-outcome trend as any other fall-open via
    :func:`record_no_ci_gate_runtime` (the suite did NOT run, so
    ``executed_real_tests`` is False and the reason is tallied) AND additionally
    stamps block-specific fields so an operator can distinguish a PR that was
    deliberately HELD from one that merged UNVERIFIED:
      - ``last_no_test_block_at`` / ``last_no_test_block_reason``;
      - ``no_test_block_counts``: ``{reason_key: count}`` tally.

    Best-effort by contract: mutates the in-memory ``project`` and stages it on
    ``db_session`` (the caller's commit persists it) but NEVER raises — a
    failure here must not block or break the gate hot path.
    """
    try:
        if project is None:
            return
        record_no_ci_gate_runtime(
            db_session,
            project,
            executed_real_tests=False,
            unverified_reason=unverified_reason,
            when=when,
        )
        when_iso = when.isoformat() if hasattr(when, "isoformat") else str(when)
        runtime: dict[str, Any] = dict(
            getattr(project, "merge_gate_runtime", None) or {}
        )
        runtime["last_no_test_block_at"] = when_iso
        if unverified_reason:
            runtime["last_no_test_block_reason"] = str(unverified_reason)
            prior_counts = runtime.get("no_test_block_counts")
            counts: dict[str, int] = (
                dict(prior_counts) if isinstance(prior_counts, dict) else {}
            )
            key = str(unverified_reason)
            counts[key] = int(counts.get(key, 0) or 0) + 1
            runtime["no_test_block_counts"] = counts
        project.merge_gate_runtime = runtime
        from sqlalchemy.orm.attributes import flag_modified

        flag_modified(project, "merge_gate_runtime")
        if db_session is not None:
            db_session.add(project)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[CI_RESOLUTION] failed to record no-CI gate no-test block",
            exc_info=True,
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

    Declared-build no-op refinement: when the PASSED probe ran the project's OWN
    declared ``build_cmd`` and the agent confidently observed (via the additive
    ``wrote_no_artifact`` flag) that it exited 0 yet wrote no output to the fresh
    PR-head clone, return ``("declared build ran but emitted no artifact (...)",
    "probe_no_artifact")`` instead of the confident ``probe_passed`` phrasing — a
    no-op build does not honestly "build the deliverable". An absent / false flag
    (older agent, or an ambiguous/unobservable tree) keeps ``probe_passed`` exactly.

    Documented-command probe: when the merge falls open and a
    ``documentedCommands`` block is present (a command the PR INTRODUCES or
    DOCUMENTS), surface DISTINCT operator-visible reason keys —
    ``documented_command_could_not_run`` when missing deps / unprovisioned setup
    degraded it (the claim went unverified), ``documented_command_no_artifact``
    when a BUILD-INTENT documented command exited 0 yet (per the agent's
    ``wrote_no_artifact`` observation) wrote no output to the fresh PR-head clone
    (a likely no-op build — mirroring the declared-build ``probe_no_artifact``
    refinement), or ``documented_command_passed`` when the documented command was
    the positive execution evidence (no unit suite ran but the documented command
    executed and passed). This signal takes
    precedence over the acceptance-probe / all-skipped phrasing because a command
    the PR itself introduced is the most specific claim we can speak to. A CLEAR
    documented-command failure never reaches here — it blocks via
    :func:`classify_merge_test_verdict`.

    Purely a refinement of the operator-visible reason: it never changes the
    pass/fail/undetected merge decision.  Defensive about a missing / old-shape
    verdict (an older agent that does not emit ``noTestsReason`` /
    ``documentedCommands`` keeps the generic reason).
    """
    if isinstance(verdict, dict):
        # A DECLARED acceptance test that the gate could not confirm RAN takes
        # precedence over every other fall-open phrasing: it is the most specific
        # promise the plan made. A clear absent/skipped never reaches here (it
        # blocks via :func:`classify_merge_test_verdict`); this surfaces the
        # could_not_run degrade (no targetable suite / environment failure) so the
        # operator sees the promised test went unconfirmed and the merge is
        # excluded from verified-merge accounting.
        acceptance = acceptance_test_block(verdict)
        if acceptance is not None and not acceptance.get("ran"):
            name = str(
                acceptance.get("selector") or acceptance.get("descriptor") or ""
            ).strip()
            phrase = (
                f"a declared acceptance test could not be confirmed to run ({name})"
                if name
                else "a declared acceptance test could not be confirmed to run"
            )
            return (phrase, "acceptance_test_not_run")
        documented = verdict.get("documentedCommands")
        if isinstance(documented, dict) and documented.get("ran"):
            label = str(documented.get("label") or "").strip()
            if documented.get("could_not_run"):
                phrase = (
                    f"a PR-introduced/documented command could not be run ({label})"
                    if label
                    else "a PR-introduced/documented command could not be run "
                    "(missing deps/unprovisioned setup)"
                )
                return (phrase, "documented_command_could_not_run")
            if documented.get("passed") is True:
                # Honest refinement mirroring the declared-build ``probe_no_artifact``
                # path: a BUILD-INTENT documented/PR-introduced command that exited 0
                # but — per the agent's ``wrote_no_artifact`` observation — wrote
                # NOTHING to the fresh PR-head clone is a likely no-op build/generate.
                # A result-producing command that produced no result does not honestly
                # verify the claim, so surface a DISTINCT reason instead of the
                # confident ``documented_command_passed``. The agent sets the flag ONLY
                # for a build-intent command whose tree it confidently observed; an
                # absent / false flag (lint/test command, older agent, unobservable
                # tree) keeps today's ``documented_command_passed`` byte-for-byte.
                if documented.get("wrote_no_artifact") is True:
                    phrase = (
                        f"a PR-introduced/documented build ran but emitted no artifact "
                        f"({label}) — possible no-op build"
                        if label
                        else "a PR-introduced/documented build ran but emitted no "
                        "artifact (possible no-op build)"
                    )
                    return (phrase, "documented_command_no_artifact")
                phrase = (
                    f"a PR-introduced/documented command was executed and passed ({label})"
                    if label
                    else "a PR-introduced/documented command was executed and passed"
                )
                return (phrase, "documented_command_passed")
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
                    # Honest refinement of the declared-build fall-open: a
                    # declared build that exited 0 but — per the agent's
                    # ``wrote_no_artifact`` observation — wrote NOTHING to the
                    # fresh PR-head clone is a likely no-op build (an empty
                    # Makefile recipe, a script that silently does nothing, a
                    # generator that writes nowhere). The build's result IS the
                    # deliverable, so "exited 0 yet emitted no artifact" makes the
                    # confident "deliverable builds" phrasing dishonest. Distinct
                    # phrase + stable key. The agent sets the flag ONLY when it
                    # confidently observed the tree; an absent / false flag (an
                    # older agent, or an ambiguous/unobservable tree) keeps
                    # today's ``probe_passed`` behaviour byte-for-byte — the new
                    # reason is never emitted on uncertainty.
                    if probe.get("wrote_no_artifact") is True:
                        phrase = (
                            f"declared build ran but emitted no artifact ({label}) "
                            "— possible no-op build"
                            if label
                            else "declared build ran but emitted no artifact "
                            "(possible no-op build)"
                        )
                        return (phrase, "probe_no_artifact")
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
            "failed_logs": direct.get("failed_logs", "")
            if direct_state == _CI_RED
            else "",
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
            "failed_logs": (first_red_result or {}).get("failed_logs", ""),
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
