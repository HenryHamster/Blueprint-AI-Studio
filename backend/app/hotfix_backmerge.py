"""Periodic back-merge of the LTS branch into the integration trunk.

Ensures the integration trunk stays in sync with the LTS branch. In Blueprint's
case, this is a ``main → automain`` back-merge; in other repos with different
branch configuration, the back-merge adapts to the configured ``promotion_target``
and ``integration_branch`` names.

The integration trunk and LTS branch can diverge when:

  * a **hotfix** — an expedited fix branched off the LTS branch that merges
    straight back (the post-rollup-merge handler in task_lifecycle.py calls
    :func:`open_backmerge_pr` once per hotfix);
  * a **direct push** to the LTS branch — a human landing a fix straight on the
    LTS branch, which carries no task and no ``hotfix:true`` marker.

Either way, the branches diverge. A missed back-merge makes the next
promotion from integration trunk to LTS conflict — so every advance of the LTS
branch MUST be followed by a back-merge into the integration trunk.

Two entry points:

  * :func:`open_backmerge_pr` — opens + merges one back-merge PR. The exact
    inverse of ``release_qualifier.promote()``. Called once by the
    hotfix-merge handler (``periodic=False``).
  * :func:`reconcile_main_into_automain` — the periodic reconciler. Runs every
    scheduler cycle and catches whatever the hotfix trigger missed — most
    importantly direct pushes, which have no marker to trigger on. Delegates
    to :func:`open_backmerge_pr` with ``periodic=True``.

It deliberately reuses ``promote``'s structure and the same GitHub helpers
(``create_pull_request`` / ``merge_pull_request``) and credential resolver
(``release_qualifier._resolve_github_with_session``).

Failure philosophy (design doc §6 gotcha 6 — "the back-merge conflict-alert is
mandatory, not optional"): a back-merge that does not complete must never pass
silently. The behaviour differs by caller:

  * ``periodic=False`` (a one-shot hotfix back-merge) — every non-merge raises
    an Alert; the operator must see a stuck one-shot back-merge.
  * ``periodic=True`` (the reconciler, which retries every cycle) — alerting is
    deduped, never suppressed: a blocked back-merge raises exactly one
    unresolved Alert per project (the first stuck cycle escalates; later cycles
    stay quiet until it is dismissed), and a genuine conflict's Alert +
    resolution plan are likewise deduped project-wide. So a persistent failure
    surfaces once — not never, and not every cycle.

``open_backmerge_pr`` is idempotent: ``github.create_pull_request`` returns the
existing open PR on a 422 "already exists", so calling it repeatedly (the
periodic case) reuses the in-flight PR and re-attempts its merge rather than
opening duplicates.

The caller commits the session.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app import github
from app.broadcast import broadcast_to_project

# release_qualifier does NOT import this module, so importing from it here is
# safe — no circular import.  _resolve_github_with_session is the shared
# credential resolver used by the whole LTS machinery.
from app.release_qualifier import _lts_branch_names, _resolve_github_with_session
from app.size_limit_policy import _is_chronic_offender, parse_bump_history
from testing.check_size_limits import (
    _COVERAGE_THRESHOLD as _UNCAPPED_SIZE_THRESHOLD,
    _is_agent_excluded,
    _is_frontend_excluded,
)
from app.models import (
    Alert,
    AlertSeverity,
    Artifact,
    ArtifactType,
    Project,
    TASK_ORIGINATION_HOTFIX,
    Task,
    TaskHistory,
    TaskStatus,
)
from app.payload_schemas import PayloadTooLargeError, validate_internal_payload
from app.rollup_migration_validation import (
    _plan_backmerge_migration_reconciliation,
    _validate_rollup_migration_chain,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _utcnow

from app.message_audience import is_operator_visible, route_event

logger = logging.getLogger(__name__)

# Priority for the conflict-resolution task — expedited (design doc:
# back-merge conflict resolution gates the next promotion; >= 90).
_RESOLUTION_PRIORITY = 95

# Path to the module size limits JSON, relative to this file
# (backend/app/ -> backend/testing/).
_MODULE_SIZE_LIMITS_PATH = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "testing", "module_size_limits.json")
)

# Maps a file-path prefix to its section name in module_size_limits.json.
# Listed longest-first so a more-specific prefix always wins on a match.
_FILE_PREFIX_MAP: list[tuple[str, str]] = [
    ("backend/app/", "backend"),
    ("agent/src/", "agent"),
    ("admin-ui/src/", "frontend"),
]

# Maps section names to their uncapped-file exclusion predicate.  Mirrors the
# per-section exclusion rules in check_size_limits._SECTION_SCAN so that the
# same files that check_size_limits.py --fix would skip are also skipped here.
_SECTION_EXCLUSION: dict[str, object] = {
    "agent": _is_agent_excluded,
    "frontend": _is_frontend_excluded,
}

# Task type for the conflict-resolution task.  ``create`` (not ``plan``): runs
# the agent directly against automain so the resolver prompt reaches the
# executor verbatim.  A plan task would decompose into subtasks via the
# planner, and the per-conflict guardrails do not survive that indirection
# (subtask prompts are written by the planner, not copied verbatim).
#
# ``create`` over ``refactor``/``fix``:
#   - ``refactor``'s standard-handler boilerplate forbids behavior changes
#     ("Do NOT change any observable behavior"), which contradicts the whole
#     point — absorbing ``main``'s bugfixes IS a behavior change.
#   - ``fix`` is hard-coded to clone an existing PR branch (the back-merge
#     PR's head is ``main``, which agents must never push to).
#   - ``create`` cleanly branches from automain, runs the agent with the
#     prompt unmodified, pushes a new ``blueprint/task-...`` branch, and
#     opens a PR back to automain — exactly the shape we want.
_RESOLUTION_TASK_TYPE = "create"

# Terminal task statuses — a resolution plan in one of these no longer counts
# as "open" for the (fast, GH-free) duplicate-spawn guard.
_TERMINAL_STATUSES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

# Lookback window for the cross-cycle dedup's GitHub leg.  A resolver task goes
# COMPLETED the instant it OPENS its resolution PR — long before that PR merges
# into the integration trunk (CI + review tail can take many minutes, or never
# if CI stays red).  During that window the back-merge is still DIRTY, so the
# fast (non-terminal) guard returns False and the next reconciler cycle spawns a
# DUPLICATE resolver (real incident: competing PRs #7476 CI-red-orphaned vs
# #7478 clean-merged).  So a RECENTLY-terminal resolver whose resolution PR is
# still OPEN is treated as in-flight.  The window bounds the (rare) GitHub call
# to genuinely-recent resolvers: an old terminal resolver whose PR was never
# landed must not block a fresh respawn forever, and a stale row never triggers
# a network lookup.  ``Task.created_at`` is naive-UTC (``_utcnow``); the cutoff
# uses the same source so the comparison is apples-to-apples.
_RESOLVER_PR_LOOKBACK = timedelta(hours=24)


# ---------------------------------------------------------------------------
# Module size-violation helpers (step 3c)
# ---------------------------------------------------------------------------


def _load_size_limits() -> dict:
    """Load module_size_limits.json; return {} on any read/parse error."""
    try:
        with open(_MODULE_SIZE_LIMITS_PATH) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001 — advisory; never propagate
        return {}


def _map_pr_file_to_section(filename: str) -> tuple[str, str] | None:
    """Map a PR changed-file path to ``(section, rel_key)`` for cap lookup.

    Returns ``None`` for files that belong to no tracked section.
    """
    for prefix, section in _FILE_PREFIX_MAP:
        if filename.startswith(prefix):
            return section, filename[len(prefix) :]
    return None


async def _check_module_size_violations(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    ref: str,
    project_id: uuid.UUID,
) -> list[dict]:
    """Return cap violations for tracked modules changed in the PR.

    Fetches the PR file list, maps each changed file to its section/rel key in
    ``module_size_limits.json``, retrieves its content at ``ref`` (the LTS
    branch head), counts lines, and compares against the cap.  Returns one
    record per violation: ``{section, rel, count, cap}``.

    Returns ``[]`` when the limits file is absent, when no tracked modules are
    changed, or when all changed tracked modules are within cap.
    """
    limits = await asyncio.to_thread(_load_size_limits)
    if not limits:
        return []

    pr_files = await github.get_pr_files(token, owner, repo, pr_number, str(project_id))
    violations: list[dict] = []
    for f in pr_files:
        filename = f.get("filename") or ""
        mapping = _map_pr_file_to_section(filename)
        if mapping is None:
            continue
        section, rel = mapping
        cap = (limits.get(section) or {}).get(rel)
        if cap is None:
            # No cap entry — skip files that the canonical guardrail excludes
            # (agent .test.ts/.d.ts; frontend .test.ts/.test.tsx/.d.ts/test/*).
            # check_size_limits.py --fix will never add a cap for these, so
            # alerting on them would send operators on a wild goose chase.
            exclude_fn = _SECTION_EXCLUSION.get(section)
            if exclude_fn and exclude_fn(rel):  # type: ignore[operator]
                continue
        text = await github.get_file_text_at_ref(
            filename,
            token=token,
            owner=owner,
            repo=repo,
            ref=ref,
            project_id=str(project_id),
        )
        if text is None:
            continue
        count = len(text.splitlines())
        if cap is None:
            if count > _UNCAPPED_SIZE_THRESHOLD:
                violations.append(
                    {"section": section, "rel": rel, "count": count, "cap": None}
                )
        elif count > cap:
            violations.append(
                {"section": section, "rel": rel, "count": count, "cap": cap}
            )
    return violations


def _fetch_chronic_info(rel: str, section: str) -> tuple[list, bool]:
    """Sync helper: run parse_bump_history + chronic check in a thread.

    Isolated so asyncio.to_thread can wrap a single callable without
    calling parse_bump_history twice.
    """
    history = parse_bump_history(rel, section)
    return history, _is_chronic_offender(history)


async def _emit_size_violation_alerts(
    session: AsyncSession,
    project_id: uuid.UUID,
    violations: list[dict],
    pr_number: int,
    trigger_desc: str,
    *,
    project: Project | None = None,
) -> None:
    """Emit a deduped Alert for each module size violation.

    First-time breaches produce a WARNING.  Modules that have been bumped
    repeatedly (chronic offenders per ``_is_chronic_offender``) produce an
    ERROR with a decomposition-required message instead of the advisory WARNING.

    For WARNING alerts the title encodes the violation in 100-line buckets so
    the same alert is not re-emitted on every periodic back-merge cycle, but a
    new alert fires when the module grows into the next bucket.  For chronic
    ERROR alerts the title is stable (no bucket) so the dedup is project-wide
    and persistent until dismissed.
    """
    # Audience gate: size violations are strategist-internal bookkeeping and
    # must never reach the operator.  This check keeps the function fast
    # (does nothing) so the registry is the single policy source.
    if not is_operator_visible(route_event("hotfix_backmerge.size_violation")):
        logger.debug(
            "[BACKMERGE] Size-violation alerts suppressed (audience=strategist-internal)"
        )
        return
    # ── Pre-load existing alert titles for dedup ──────────────────────
    all_possible_titles: list[str] = []
    for _v in violations:
        _section = _v["section"]
        _rel = _v["rel"]
        _count = _v["count"]
        _cap = _v["cap"]
        if _cap is None:
            _bucket = (_count // 100) * 100
            all_possible_titles.append(
                f"Uncapped module in back-merge: {_section}/{_rel} (~{_bucket} lines, no cap)"
            )
        else:
            _bucket = (_count // 100) * 100
            all_possible_titles.append(
                f"Module size violation in back-merge: {_section}/{_rel} (~{_bucket} lines, cap {_cap})"
            )
            all_possible_titles.append(
                f"Chronic size offender: {_section}/{_rel} (decomposition required)"
            )

    existing_titles: set[str] = set()
    if all_possible_titles:
        _rows = (
            (
                await session.execute(
                    select(Alert.title).where(
                        Alert.project_id == project_id,
                        Alert.title.in_(all_possible_titles),
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        existing_titles = set(_rows)
        _rows = None  # Free result set before the main loop

    for v in violations:
        section = v["section"]
        rel = v["rel"]
        count = v["count"]
        cap = v["cap"]

        trigger_clause = f" (triggered by {trigger_desc})" if trigger_desc else ""

        if cap is None:
            # Uncapped large file — no history, no chronic check.
            bucket = (count // 100) * 100
            uncapped_title = (
                f"Uncapped module in back-merge: {section}/{rel} "
                f"(~{bucket} lines, no cap)"
            )
            if uncapped_title in existing_titles:
                logger.debug(
                    "[BACKMERGE] uncapped module alert already open for %s/%s; skipping",
                    section,
                    rel,
                )
                continue
            uncapped_alert = Alert(
                project_id=project_id,
                severity=AlertSeverity.WARNING,
                title=uncapped_title,
                event_type="hotfix_backmerge.size_violation",
                message=(
                    f"Back-merge PR #{pr_number}{trigger_clause} includes `{rel}` "
                    f"({section}) at approximately {bucket}+ lines with no cap entry "
                    f"in module_size_limits.json. Run "
                    f"`python3 backend/testing/check_size_limits.py --fix` to add a cap, "
                    f"or consider decomposing `{rel}` into smaller modules. "
                    f"The back-merge proceeded normally."
                ),
            )
            session.add(uncapped_alert)
            await session.flush()
            await _broadcast_alert(project_id, uncapped_alert)
            logger.warning(
                "[BACKMERGE] uncapped module: %s/%s has %d lines (no cap) "
                "in back-merge PR #%s",
                section,
                rel,
                count,
                pr_number,
            )
            continue

        # Advisory bump-history lookup — never blocks the alert path.
        # parse_bump_history calls subprocess.run (blocking); offload to a thread.
        try:
            history = await asyncio.to_thread(parse_bump_history, rel, section)
            chronic = _is_chronic_offender(history)
        except Exception:  # noqa: BLE001
            history = []
            chronic = False

        if chronic:
            bump_count = len(history)
            first_cap = history[0]["old_cap"] if history else cap
            growth_pct = (
                round((cap - first_cap) / first_cap * 100) if first_cap > 0 else 0
            )
            alert_title = (
                f"Chronic size offender: {section}/{rel} (decomposition required)"
            )
            alert_message = (
                f"`{rel}` ({section}) has been bumped {bump_count} times "
                f"(cap grew from {first_cap} → {cap}, +{growth_pct}%). "
                f"This module requires DECOMPOSITION, not another cap bump. "
                f"Back-merge PR #{pr_number}{trigger_clause} detected the latest "
                f"violation ({count} lines, cap {cap}). "
                f"Do not bump the cap without operator approval."
            )
            severity = AlertSeverity.ERROR
        else:
            bucket = (count // 100) * 100
            alert_title = (
                f"Module size violation in back-merge: {section}/{rel} "
                f"(~{bucket} lines, cap {cap})"
            )
            alert_message = (
                f"Back-merge PR #{pr_number}{trigger_clause} includes `{rel}` "
                f"({section}) at approximately {bucket}+ lines, exceeding its "
                f"{cap}-line cap. Consider (a) decomposing `{rel}` into smaller "
                f"modules or (b) bumping the cap in module_size_limits.json with "
                f"justification. The back-merge proceeded normally."
            )
            severity = AlertSeverity.WARNING

        if alert_title in existing_titles:
            logger.debug(
                "[BACKMERGE] size violation alert already open for %s/%s; skipping",
                section,
                rel,
            )
            continue

        alert = Alert(
            project_id=project_id,
            severity=severity,
            title=alert_title,
            event_type="hotfix_backmerge.size_violation",
            message=alert_message,
        )
        session.add(alert)
        await session.flush()
        await _broadcast_alert(project_id, alert)
        if chronic:
            logger.warning(
                "[BACKMERGE] chronic size offender: %s/%s has %d lines (cap %d) "
                "— bumped %d times; decomposition required (back-merge PR #%s)",
                section,
                rel,
                count,
                cap,
                len(history),
                pr_number,
            )
        else:
            logger.warning(
                "[BACKMERGE] size violation: %s/%s has %d lines (cap %d) "
                "in back-merge PR #%s",
                section,
                rel,
                count,
                cap,
                pr_number,
            )


async def reconcile_main_into_automain(
    session: AsyncSession,
    project_id: uuid.UUID,
) -> dict[str, Any]:
    """Periodically back-merge the LTS branch into the integration trunk.

    The hotfix-triggered back-merge (:func:`open_backmerge_pr` from the
    post-rollup-merge handler) only fires for tasks marked ``hotfix:true``. A
    *direct push* to the LTS branch — a human landing a fix straight on the LTS
    branch — carries no such marker, so nothing back-merges it and the branches
    silently diverge until the next promotion conflicts. This reconciler closes
    that gap: it runs every scheduler cycle regardless of HOW the LTS branch
    advanced.

    No-op when ``integration_branch == promotion_target`` (LTS model is inactive).
    When these branches are the same, there is no separate trunk to reconcile.

    Delegates to :func:`open_backmerge_pr` with ``periodic=True``: a back-merge
    merely waiting on CI is logged and retried next cycle rather than raising
    an Alert every cycle; a genuine conflict still raises one Alert and spawns
    one resolution plan (deduped project-wide).

    The caller commits the session. Returns a status dict — ``open_backmerge_pr``'s
    result, or ``{"status": "skipped", "reason": "lts_inactive"}`` when the LTS
    branch model is not active.
    """
    project = await session.get(Project, project_id)
    if project is None:
        return {"status": "skipped", "reason": "project_not_found"}
    integration_branch, promotion_target = _lts_branch_names(project)
    if integration_branch == promotion_target:
        return {"status": "skipped", "reason": "lts_inactive"}
    return await open_backmerge_pr(session, project_id, periodic=True)


async def open_backmerge_pr(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    merged_pr_url: str = "",
    hotfix_task_id: uuid.UUID | str | None = None,
    periodic: bool = False,
) -> dict[str, Any]:
    """Open and merge a back-merge PR from the LTS branch into the integration trunk.

    The inverse of ``release_qualifier.promote()``. Steps:

      1. Load the project; resolve ``(token, owner, repo)``.
      2. ``create_pull_request`` from promotion_target to integration_branch.
         Idempotent — on a 422 "already exists" ``create_pull_request`` returns
         the existing open PR, so a repeated (periodic) call reuses the in-flight PR.
      3. If the PR result has ``empty_diff`` truthy (no differences to back-merge),
         return ``{"status": "noop_empty_diff"}``.
      4. ``merge_pull_request`` with ``merge_method="merge"`` (preserve commit history).
      5. On ``merged`` truthy, return ``{"status": "merged", "pr_url": ...}``.
      6. On ``conflict`` (a 409), raise an ``Alert`` AND spawn a high-priority
         ``create`` task instructing conflict resolution; return ``{"status": "conflict"}``.
         The resolver task does NOT carry ``hotfix:true``. Alert + resolver
         are deduped project-wide — only one resolution is ever needed at a
         time. The resolver is a single direct task (not a plan/decomposer)
         so the resolution guardrails reach the executing agent verbatim.
      7. On any other non-merge (a 405) or any exception, raise an ``Alert``
         (``periodic=False``) — a one-shot back-merge that fails must never
         pass silently — and return ``{"status": "blocked"}``.

    ``periodic`` — set ``True`` by :func:`reconcile_main_into_automain`, which
    calls this every scheduler cycle. A periodic caller dedups its alerting so
    it neither storms (an Alert every cycle) nor goes silent: a blocked
    non-merge (step 7) raises one unresolved Alert per project until dismissed,
    and the conflict path (step 6) raises one Alert + one resolver task, both
    deduped.

    ``merged_pr_url`` / ``hotfix_task_id`` describe the hotfix that triggered
    this back-merge, when there was one; the periodic reconciler passes
    neither. They are recorded in the PR body, the Alert message, and the
    resolver task's context for traceability.

    The caller commits the session. Returns a status dict.
    """
    hotfix_task_id_str = str(hotfix_task_id) if hotfix_task_id is not None else None
    trigger_desc = _describe_trigger(merged_pr_url, hotfix_task_id_str)

    # populate_existing=True forces a fresh SELECT instead of returning a
    # possibly-expired identity-map hit. The branch-reconciliation service
    # (lifecycle._branch_reconciliation_service) reuses one long-lived session
    # across all projects, committing/rolling back per iteration — both expire
    # identity-map instances. Without the refresh, a later `project.github_owner`
    # read in _resolve_github_with_session lazy-loads under async and raises
    # MissingGreenlet, crash-looping the reconciler (so main→automain never
    # back-merges and the branches silently diverge).
    project = await session.get(Project, project_id, populate_existing=True)
    if project is None:
        logger.warning(
            "[BACKMERGE] open_backmerge_pr: project %s not found",
            project_id,
        )
        return {"status": "blocked", "error": "project_not_found"}

    integration_branch, promotion_target = _lts_branch_names(project)

    # 0. Single-branch repo guard.  When integration_branch == promotion_target
    #    there is no separate trunk to back-merge into: opening a
    #    promotion_target -> integration_branch PR would target the very branch
    #    work already merges to (head == base — an erroneous self-merge / no-op).
    #    Skip cleanly as inactive, mirroring reconcile_main_into_automain's guard
    #    and release_promotion.promote()'s single-branch skip.  This makes the
    #    one-shot hotfix back-merge path (task_lifecycle.py) safe for external
    #    single-branch projects, not just the periodic reconciler.
    if integration_branch == promotion_target:
        logger.info(
            "[BACKMERGE] project %s: integration_branch == promotion_target "
            "(%s) — single-branch repo, nothing to back-merge",
            project_id,
            integration_branch,
        )
        return {"status": "skipped", "reason": "lts_inactive"}

    # 1. Resolve GitHub credentials (App token → installation → PAT).
    resolved = await _resolve_github_with_session(session, project)
    if resolved is None:
        logger.warning(
            "[BACKMERGE] no GitHub credentials for project %s — cannot back-merge",
            project_id,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            trigger_desc,
            project=project,
            reason="no GitHub credentials available",
            periodic=periodic,
        )
        return {"status": "blocked", "error": "no_credentials"}
    token, owner, repo = resolved

    # 2. Open the main → automain back-merge PR.  create_pull_request returns
    #    the existing open PR on a 422 "already exists", so this is idempotent
    #    when the periodic reconciler calls it repeatedly.
    trigger_line = f"Triggered by: {trigger_desc}\n\n" if trigger_desc else ""
    try:
        pr = await github.create_pull_request(
            token,
            owner,
            repo,
            title=f"Back-merge: {promotion_target} -> {integration_branch}",
            head=promotion_target,
            base=integration_branch,
            body=(
                f"Automated back-merge of the LTS branch `{promotion_target}` into the "
                f"integration trunk `{integration_branch}`.\n\n"
                f"{trigger_line}"
                f"Without this back-merge `{promotion_target}` and `{integration_branch}` diverge and the "
                f"next `{integration_branch} -> {promotion_target}` promotion conflicts (see "
                "docs/plans/automain-lts-plan-v1.md Phase 5).\n\n"
                "Opened by the back-merge machinery "
                "(backend/app/hotfix_backmerge.py)."
            ),
            project_id=str(project_id),
        )
    except Exception as exc:  # noqa: BLE001 — a PR-creation failure blocks, never raises
        logger.warning(
            "[BACKMERGE] failed to open back-merge PR for project %s: %s",
            project_id,
            exc,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            trigger_desc,
            project=project,
            reason=f"could not open the back-merge PR: {str(exc)[:200]}",
            periodic=periodic,
        )
        return {"status": "blocked", "error": "pr_creation_failed"}

    # 3. Empty diff — main == automain, nothing to back-merge.  A degenerate
    #    but valid outcome: create_pull_request returns
    #    {"empty_diff": True, "merged": True}.
    if pr.get("empty_diff"):
        logger.info(
            "[BACKMERGE] project %s: main == automain (empty diff) — "
            "nothing to back-merge",
            project_id,
        )
        return {"status": "noop_empty_diff"}

    pr_number = pr.get("number")
    pr_url = pr.get("html_url", "")
    if not pr_number:
        logger.warning(
            "[BACKMERGE] back-merge PR for project %s has no number",
            project_id,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            trigger_desc,
            project=project,
            reason="the back-merge PR was created without a PR number",
            periodic=periodic,
        )
        return {"status": "blocked", "error": "pr_missing_number"}

    # Snapshot the exact PR target that the validation below is about to
    # inspect.  GitHub's create/reuse response is a full PR resource, so both
    # sides are backend-observed rather than inferred from branch names.
    initial_head = pr.get("head") or {}
    initial_base = pr.get("base") or {}
    initial_head_sha = str(initial_head.get("sha") or "").strip()
    initial_base_sha = str(initial_base.get("sha") or "").strip()
    if (
        not initial_head_sha
        or not initial_base_sha
        or initial_head.get("ref") != promotion_target
        or initial_base.get("ref") != integration_branch
    ):
        logger.warning(
            "[BACKMERGE] project %s: PR #%s target could not be verified "
            "before validation (head=%s@%s base=%s@%s) — leaving open for "
            "the next sweep",
            project_id,
            pr_number,
            initial_head.get("ref"),
            initial_head_sha[:8] or "missing",
            initial_base.get("ref"),
            initial_base_sha[:8] or "missing",
        )
        return {"status": "deferred", "error": "pr_target_unverified", "pr_url": pr_url}

    # 3b. Combined Alembic migration-chain guard — runs BEFORE the merge.
    #     A hotfix migration that landed on the promotion target plus a feature
    #     migration added on the integration branch since the last promotion can
    #     each be green in isolation yet, once back-merged, produce two Alembic
    #     heads or a duplicate revision id — `alembic upgrade head` then fails and
    #     wedges the integration branch for every subsequent task (the same class
    #     the rollup auto-merge path already guards against). Reuse the rollup
    #     guard's target-branch-aware combined-chain validator: validate the
    #     migrations this back-merge PR adds (extracted from its diff) against the
    #     integration branch's CURRENT migration chain. Fail-safe: on an inability
    #     to fetch/parse the migration files DEFER (never merge unvalidated — the
    #     periodic reconciler retries next cycle); on a genuine collision / dual
    #     head BLOCK (do not call the merge API at all), raise an Alert, and spawn
    #     a high-priority dual-head-aware resolution task.
    alembic_status, alembic_reason = await _validate_rollup_migration_chain(
        token, owner, repo, pr_number, integration_branch
    )
    if alembic_status == "defer":
        logger.info(
            "[BACKMERGE] deferring back-merge PR #%s for project %s — could not "
            "validate the combined Alembic chain: %s",
            pr_number,
            project_id,
            alembic_reason,
        )
        return {"status": "deferred", "error": "alembic_unverified", "pr_url": pr_url}
    if alembic_status == "block":
        # A dual-head collision is NEVER reconciled inline by pushing to the
        # live integration branch: the renumbered integration migration chains
        # onto the promotion-target hotfix revision, which is NOT on the
        # integration branch until the merge lands. Pushing the rewrite to the
        # live target before the merge would leave automain with a dangling
        # migration / second Alembic head (wedging `alembic upgrade head` for
        # every concurrent task) for as long as the merge is held off by branch
        # protection, a CI gate, or an unrelated conflict — and the next
        # periodic cycle would misread that transient state as a fresh dual
        # head. Instead ALWAYS route to the guided resolver: it does the
        # renumber + `git merge origin/main` on a fresh branch off automain and
        # lands the combined fix atomically through the normal PR gate, after
        # which automain is clean and this back-merge PR is an empty diff.
        # `_handle_migration_block` dedups against an already-open resolver
        # (via `_resolution_task_already_open`), so a periodic re-entry spawns
        # no duplicate task and raises no new alert.
        logger.warning(
            "[BACKMERGE] back-merge PR #%s for project %s would create an "
            "invalid Alembic chain (%s) — blocking the merge and spawning a "
            "resolver",
            pr_number,
            project_id,
            alembic_reason,
        )
        await _handle_migration_block(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id_str,
            trigger_desc,
            reason=str(alembic_reason),
            project=project,
            periodic=periodic,
            gh_token=token,
            owner=owner,
            repo=repo,
            integration_branch=integration_branch,
            promotion_target=promotion_target,
        )
        return {"status": "migration_conflict", "pr_url": pr_url}

    # 3c. Module size violation check — non-blocking, best-effort advisory.
    #     Detects oversized tracked modules before CI runs so the operator
    #     can prepare a decomposition or cap-bump.  The merge is NEVER blocked
    #     by this check: any exception is caught and logged at DEBUG level.
    try:
        violations = await _check_module_size_violations(
            token, owner, repo, pr_number, promotion_target, project_id
        )
        if violations:
            await _emit_size_violation_alerts(
                session,
                project_id,
                violations,
                pr_number,
                trigger_desc,
                project=project,
            )
    except Exception as exc:  # noqa: BLE001 — advisory; never block the merge
        logger.debug(
            "[BACKMERGE] size-violation check for PR #%s (project %s) failed: "
            "%s — continuing to merge",
            pr_number,
            project_id,
            exc,
        )

    # Serialize the final submission to this repository/base and PR with the
    # same canonical advisory-lock order used by review/fix publication.  The
    # live re-read happens inside both locks immediately before the expected-
    # head PUT.  A push to the source branch or an advance of the integration
    # base while validation was running therefore defers for fresh evidence.
    from app.task_locking import acquire_merge_submission_locks

    await acquire_merge_submission_locks(
        session,
        project_id=project_id,
        repo_identity=f"{owner}/{repo}",
        base_branch=integration_branch,
        pr_number=int(pr_number),
    )
    try:
        fresh_pr = await github.get_pull_request(
            token, owner, repo, pr_number, project_id=str(project_id)
        )
    except Exception as exc:  # noqa: BLE001 — transient read, retry next sweep
        logger.warning(
            "[BACKMERGE] project %s: could not re-read PR #%s target after "
            "validation (%s) — leaving open for the next sweep",
            project_id,
            pr_number,
            exc,
        )
        return {"status": "retry", "error": "pr_target_unavailable", "pr_url": pr_url}

    fresh_head = fresh_pr.get("head") or {}
    fresh_base = fresh_pr.get("base") or {}
    fresh_head_sha = str(fresh_head.get("sha") or "").strip()
    fresh_base_sha = str(fresh_base.get("sha") or "").strip()
    target_changed = (
        fresh_pr.get("state") != "open"
        or fresh_head.get("ref") != promotion_target
        or fresh_base.get("ref") != integration_branch
        or fresh_head_sha != initial_head_sha
        or fresh_base_sha != initial_base_sha
    )
    if target_changed:
        logger.info(
            "[BACKMERGE] project %s: PR #%s target changed during validation "
            "(%s@%s/%s@%s -> %s@%s/%s@%s, state=%s) — deferring",
            project_id,
            pr_number,
            initial_base.get("ref"),
            initial_base_sha[:8] or "missing",
            initial_head.get("ref"),
            initial_head_sha[:8] or "missing",
            fresh_base.get("ref"),
            fresh_base_sha[:8] or "missing",
            fresh_head.get("ref"),
            fresh_head_sha[:8] or "missing",
            fresh_pr.get("state"),
        )
        return {"status": "deferred", "error": "pr_target_changed", "pr_url": pr_url}

    # 4. Merge the PR — merge_method="merge" preserves the main commit history.
    #    merge_pull_request does NOT raise on 405/409; it returns merged=False.
    try:
        merge_result = await github.merge_pull_request(
            token,
            owner,
            repo,
            pr_number,
            merge_method="merge",
            project_id=str(project_id),
            expected_head_sha=initial_head_sha,
        )
    except Exception as exc:  # noqa: BLE001 — a 5xx raises HTTPException; treat as blocked
        # A RATE-LIMITED merge (503, raised by github_pulls._raise_merge_pr_retryable)
        # is not a merge failure at all: the PUT never reached GitHub's merge
        # machinery, so the PR is neither merged nor blocked — it simply has to be
        # re-attempted after the quota window resets. Raising the persistent
        # "blocked" operator alert here would page a human about a transient quota
        # bucket, and that alert would still be sitting there long after the next
        # back-merge sweep quietly succeeded. Retry instead.
        #
        # This branch is the ONLY reason the 503 reclassification changes any
        # behaviour: every other consumer of merge_pull_request catches bare
        # Exception and cannot tell a 503 from a 500. If you delete this branch,
        # put the 500 back — otherwise the status code is decorative.
        if (
            isinstance(exc, HTTPException)
            and exc.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        ):
            logger.warning(
                "[BACKMERGE] project %s: merge of back-merge PR #%s was RATE-LIMITED "
                "(503) — the merge was never attempted upstream. Leaving the PR open "
                "for the next sweep instead of raising a blocked alert. detail=%s",
                project_id,
                pr_number,
                str(exc.detail)[:200],
            )
            return {"status": "retry", "error": "rate_limited", "pr_url": pr_url}
        logger.warning(
            "[BACKMERGE] merge raised for back-merge PR #%s (project %s): %s",
            pr_number,
            project_id,
            exc,
        )
        await _raise_blocked_alert(
            session,
            project_id,
            trigger_desc,
            project=project,
            reason=f"merging back-merge PR #{pr_number} raised: {str(exc)[:200]}",
            pr_url=pr_url,
            periodic=periodic,
        )
        return {"status": "blocked", "error": "merge_error"}

    # 5. Success.
    if merge_result.get("merged"):
        logger.info(
            "[BACKMERGE] project %s: main -> automain back-merge merged via PR #%s",
            project_id,
            pr_number,
        )
        return {"status": "merged", "pr_url": pr_url}

    msg = merge_result.get("message", "merge not completed")

    # 6. Conflict — raise an Alert AND spawn a conflict-resolution plan.
    #    merge_pull_request flags an explicit 409 conflict, but a PR that is
    #    ALREADY dirty/conflicting returns 405 "not mergeable" (so ``conflict``
    #    is unset).  Without also consulting the PR's ``mergeable_state`` such a
    #    genuinely-conflicting back-merge falls through to the generic "blocked"
    #    path below and spins there forever — alerting every cycle but NEVER
    #    spawning a resolver (observed on PR #5277).  So when the merge didn't
    #    land and wasn't flagged a conflict, fetch the PR and treat
    #    ``mergeable_state == "dirty"`` as a conflict too.
    is_conflict = bool(merge_result.get("conflict"))
    pr_detail: dict[str, Any] | None = None
    if not is_conflict:
        try:
            pr_detail = await github.get_pull_request(
                token, owner, repo, pr_number, project_id=str(project_id)
            )
            # 5b. ALREADY MERGED. ``merge_pull_request`` collapses two different
            #     outcomes onto {"merged": False}: a real block, and an
            #     already-merged PR (GitHub's merge endpoint 405s once a PR is
            #     merged). The second happens for real: the merge PUT lands
            #     server-side but the response is lost to a 5xx, _github_request
            #     retries, and the retry gets the 405. Reading only
            #     ``mergeable_state`` cannot see that — so a back-merge that
            #     LANDED was reported {"status": "blocked"} with a persistent
            #     operator alert. The PR resource's own ``merged`` / ``merged_at``
            #     is authoritative and flips synchronously on merge; consult it
            #     BEFORE any block classification (mirrors ci_completion.py's
            #     ``_recheck_pr.get("merged")`` success-close branch).
            if pr_detail.get("merged") or pr_detail.get("merged_at"):
                logger.info(
                    "[BACKMERGE] project %s: back-merge PR #%s reports merged=False "
                    "from the merge call but the PR resource says MERGED (merged_at=%s)"
                    " — the merge landed (lost 5xx response / already-merged 405). "
                    "Recording success instead of a false block.",
                    project_id,
                    pr_number,
                    pr_detail.get("merged_at"),
                )
                return {"status": "merged", "pr_url": pr_url}
            if str(pr_detail.get("mergeable_state") or "").lower() == "dirty":
                is_conflict = True
        except Exception as exc:  # noqa: BLE001 — best-effort; fall through to blocked
            logger.debug(
                "[BACKMERGE] could not read mergeable_state for PR #%s: %s",
                pr_number,
                exc,
            )

    if is_conflict:
        logger.warning(
            "[BACKMERGE] back-merge PR #%s for project %s hit a merge conflict: %s",
            pr_number,
            project_id,
            msg,
        )
        await _handle_conflict(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id_str,
            trigger_desc,
            project=project,
            message=str(msg),
            periodic=periodic,
            token=token,
            owner=owner,
            repo=repo,
        )
        return {"status": "conflict", "pr_url": pr_url}

    # 7a. Non-conflict block where CI is RED on the merged tree. A back-merge can
    #    merge TEXTUALLY clean (no conflict) yet fail CI — a *semantic* merge
    #    break: e.g. `main` references a symbol the integration branch's refactor
    #    moved/renamed/removed, so the 3-way text merge succeeds but the result
    #    doesn't import/pass. The textual conflict resolver (step 6) never sees
    #    this. It IS auto-remediable, so spawn a CI-fix task that re-does the
    #    merge on a branch, makes CI green, and PRs back to the integration
    #    branch — mirroring the conflict path so no human is needed.
    failed_checks = await _backmerge_failed_checks(
        token, owner, repo, pr_number, project_id, pr_detail=pr_detail
    )
    if failed_checks is not None:
        logger.warning(
            "[BACKMERGE] back-merge PR #%s for project %s is mergeable but CI is "
            "RED (%s) — spawning a CI-fix task",
            pr_number,
            project_id,
            ", ".join(failed_checks[:5]) or "unknown checks",
        )
        await _handle_ci_failure(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id_str,
            trigger_desc,
            failed_checks=failed_checks,
            project=project,
            message=str(msg),
            periodic=periodic,
            token=token,
            owner=owner,
            repo=repo,
        )
        return {"status": "ci_failure", "pr_url": pr_url}

    # 7b. Any other non-merge — checks still PENDING, or a reviews-only gate, or
    #    an unknown block. NOT auto-fixable (no failure to fix), so escalate:
    #    _raise_blocked_alert always alerts a one-shot caller, once-deduped for
    #    the periodic reconciler — a persistently stuck back-merge surfaces
    #    without a storm, and a pending build clears itself next cycle.
    logger.warning(
        "[BACKMERGE] back-merge PR #%s for project %s did not merge: %s",
        pr_number,
        project_id,
        msg,
    )
    await _raise_blocked_alert(
        session,
        project_id,
        trigger_desc,
        project=project,
        reason=(
            f"back-merge PR #{pr_number} did not merge "
            f"(branch protection / checks): {str(msg)[:200]}"
        ),
        pr_url=pr_url,
        periodic=periodic,
    )
    return {"status": "blocked", "error": "merge_blocked", "pr_url": pr_url}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_trigger(merged_pr_url: str, hotfix_task_id: str | None) -> str:
    """Human-readable reference to what advanced ``main``, or ``""`` if unknown.

    A hotfix-triggered back-merge passes the merged hotfix PR URL and/or the
    hotfix task id.  The periodic reconciler passes neither — a direct push to
    ``main`` has no task — in which case this returns ``""`` and callers omit
    the trigger clause entirely rather than printing a misleading "hotfix".
    """
    parts: list[str] = []
    if merged_pr_url:
        parts.append(merged_pr_url)
    if hotfix_task_id:
        parts.append(f"task {hotfix_task_id}")
    if not parts:
        return ""
    return "hotfix " + " / ".join(parts)


async def _resolution_task_already_open(
    session: AsyncSession,
    project_id: uuid.UUID,
    *,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> bool:
    """Return True if a back-merge conflict-resolution task is already in flight.

    Only ONE such task is ever needed at a time: it runs ``git merge
    origin/main`` into ``automain``, which reconciles ALL divergence regardless
    of how many hotfixes or direct pushes caused it.  The guard is therefore
    project-wide (not per-hotfix) — it dedupes the spawn for every trigger: the
    hotfix path retried for one hotfix, a second hotfix, and the periodic
    reconciler called every cycle.

    A resolution task is identified by ``context["backmerge_conflict"] is
    True``.  "In flight" is broader than "non-terminal", because a resolver
    ``create`` task goes COMPLETED the instant it OPENS its resolution PR —
    NOT when that PR merges into the integration trunk.  The back-merge is not
    actually resolved until that PR lands, and during the CI + review tail the
    back-merge PR stays DIRTY, so a "non-terminal only" guard would let the
    next reconciler cycle spawn a duplicate resolver against the same still-open
    resolution PR (real incident: competing PRs #7476 vs #7478).  So a task is
    treated as in flight when EITHER:

      1. (fast path, no GitHub call) it is non-terminal; OR
      2. it is RECENTLY-terminal (within :data:`_RESOLVER_PR_LOOKBACK`) and its
         resolution PR is STILL OPEN on GitHub (state == "open" and not merged).

    Leg 2 only runs when ``token``/``owner``/``repo`` are supplied AND a
    candidate recently-terminal resolver exists, so the common no-conflict path
    (and unit callers that omit creds) makes ZERO GitHub calls.  A resolver whose
    PR is MERGED or CLOSED-unmerged does NOT block a fresh respawn — that is the
    legitimate "the first resolver genuinely failed, try again" path.

    The GitHub leg is FAIL-SAFE: any lookup exception is treated as "still
    open" (returns True), so a transient GitHub hiccup can never let a duplicate
    resolver through.
    """
    # Fetch ALL tasks for the project; the Python-side filter on
    # ``context["backmerge_conflict"]`` keeps the dialect-agnostic shape the
    # original guard used (SQLite local tests have no JSONB operators).  Leg 1
    # (non-terminal) and leg 2 (recently-terminal-with-open-PR) both partition
    # this list, so — unlike the prior version — terminal rows are NOT pre-
    # filtered at the DB level.
    rows = (
        (await session.execute(select(Task).where(Task.project_id == project_id)))
        .scalars()
        .all()
    )

    candidates = [
        task for task in rows if (task.context or {}).get("backmerge_conflict") is True
    ]

    # Leg 1 — any non-terminal resolver blocks (fast, GitHub-free).
    if any(task.status not in _TERMINAL_STATUSES for task in candidates):
        return True

    # Leg 2 — a recently-terminal resolver whose resolution PR is still OPEN is
    # in flight.  Skip entirely without creds (unit fast path) — leg 1 already
    # answered for non-terminal rows.
    if not (token and owner and repo):
        return False

    cutoff = _utcnow() - _RESOLVER_PR_LOOKBACK
    recent_terminal = [
        task
        for task in candidates
        if task.status in _TERMINAL_STATUSES
        and task.created_at is not None
        and task.created_at >= cutoff
    ]
    for task in recent_terminal:
        if await _resolver_pr_still_open(session, task, project_id, token, owner, repo):
            return True
    return False


async def _resolver_pr_still_open(
    session: AsyncSession,
    task: Task,
    project_id: uuid.UUID,
    token: str,
    owner: str,
    repo: str,
) -> bool:
    """Return True if ``task``'s resolution PR exists and is still OPEN on GitHub.

    The resolver does NOT store its resolution PR number in context (only the
    BACK-MERGE PR is recorded there as ``backmerge_pr_number``).  The resolution
    PR is recorded as a ``PULL_REQUEST`` :class:`Artifact` row on the task —
    written atomically with PR creation by the agent-status path.  This mirrors
    :func:`reaper._task_has_open_pr`'s artifact read (and its PR-number parse
    from ``pr_url``), then asks GitHub for the live state.

    Returns:
      * ``True``  — the PR is OPEN and not merged (resolver is in flight), OR the
        GitHub lookup raised (FAIL-SAFE: never let a transient hiccup spawn a
        duplicate).
      * ``False`` — no PR artifact yet, an unparseable URL, or the PR is MERGED /
        CLOSED-unmerged (a fresh respawn is legitimate).
    """
    artifact = (
        (
            await session.execute(
                select(Artifact).where(
                    Artifact.task_id == task.id,
                    Artifact.artifact_type == ArtifactType.PULL_REQUEST,
                )
            )
        )
        .scalars()
        .first()
    )
    if artifact is None or not artifact.pr_url:
        # No resolution PR recorded — the prior resolver never opened one, so it
        # genuinely failed; a respawn is legitimate.
        return False
    try:
        pr_number = int(artifact.pr_url.rstrip("/").rsplit("/", 1)[-1])
    except (ValueError, IndexError):
        # Unparseable URL — cannot confirm "still open"; allow respawn rather
        # than block forever on a malformed artifact.
        return False
    try:
        pr = await github.get_pull_request(
            token, owner, repo, pr_number, project_id=str(project_id)
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe: assume still open
        logger.warning(
            "[BACKMERGE] could not read resolution PR #%s for task %s "
            "(project %s): %s — assuming still open to avoid a duplicate spawn",
            pr_number,
            task.id,
            project_id,
            exc,
        )
        return True
    state = str(pr.get("state") or "").lower()
    merged = bool(pr.get("merged"))
    # Only an OPEN, not-yet-merged PR blocks a respawn.  A merged or
    # closed-unmerged PR means the resolver is done (success) or genuinely
    # failed (closed) — either way a fresh resolver is allowed.
    return state == "open" and not merged


async def _handle_conflict(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    trigger_desc: str,
    *,
    message: str,
    periodic: bool,
    project: Project | None = None,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> None:
    """Raise a conflict Alert and (if not already open) spawn a resolution task.

    A back-merge conflict needs human/agent resolution — it is never transient
    — so it always escalates.  Alert + resolution task are spawned together,
    both gated by :func:`_resolution_task_already_open`:

      * ``periodic=False`` (one-shot hotfix back-merge) — when a resolution
        task is already open the task is not duplicated, but an Alert is still
        raised so the operator sees a recurrence of a one-shot back-merge.
      * ``periodic=True`` (the reconciler retries every cycle) — when a
        resolution task is already open NEITHER a duplicate Alert NOR a
        duplicate task is created, so a persistent conflict yields one of each
        instead of one per cycle.

    Concurrency note: two concurrent callers — the promotion-conflict hook and
    the periodic reconciler ticking in the same instant — can each pass the
    read-committed dedup check before either commits.  A transaction-scoped
    PostgreSQL advisory lock serializes them: the first caller acquires the
    lock and proceeds; the second fails to acquire it and returns immediately,
    yielding exactly one plan task.  Non-PostgreSQL backends (SQLite in local
    tests) do not support ``pg_try_advisory_xact_lock``; the except path falls
    through to the existing read-committed dedup as a safe fallback.
    """
    # Serialize concurrent _handle_conflict calls for the same project.
    try:
        lock_acquired = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": f"blueprint:backmerge_conflict_resolution:{project_id}"},
            )
        ).scalar()
    except Exception as _lock_err:  # noqa: BLE001 — non-PG backend: fall through to dedup
        # Non-PostgreSQL backend — fall through to read-committed dedup.
        lock_acquired = True
        logger.debug(
            "[BACKMERGE] advisory lock unavailable (%s) — using read-committed dedup",
            _lock_err,
        )
    if not lock_acquired:
        logger.info(
            "[BACKMERGE] advisory lock not acquired for project %s — "
            "concurrent caller is mid-spawn; returning without spawning",
            project_id,
        )
        return

    already_open = await _resolution_task_already_open(
        session, project_id, token=token, owner=owner, repo=repo
    )

    if periodic and already_open:
        # The reconciler runs every cycle — a resolution task is already open
        # and being worked; staying quiet keeps a persistent conflict from
        # stacking an Alert + task each cycle.
        logger.info(
            "[BACKMERGE] back-merge conflict for project %s — a resolution "
            "task is already open; periodic reconciler stays quiet",
            project_id,
        )
        return

    creation_admission = "already_open"
    if not already_open:
        from app.task_locking import (
            AUTOMATIC_TASK_CREATION_ALLOWED,
            acquire_automatic_task_creation_authority,
        )

        creation_admission = await acquire_automatic_task_creation_authority(
            session, project_id
        )
    task_creation_allowed = already_open or (
        creation_admission == AUTOMATIC_TASK_CREATION_ALLOWED
    )

    integration_branch, promotion_target = _lts_branch_names(project)
    conflict_alert_title = (
        f"Back-merge conflict: {promotion_target} -> {integration_branch}"
    )
    trigger_clause = f" (triggered by {trigger_desc})" if trigger_desc else ""
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.ERROR,
        title=conflict_alert_title,
        event_type="rollup.backmerge_blocked",
        message=(
            f"The automated {promotion_target} -> {integration_branch} back-merge "
            f"(PR #{pr_number}{trigger_clause}) hit a merge conflict and "
            f"could not be merged automatically. "
            f"{'A resolution task is already active or has been spawned.' if task_creation_allowed else f'Automatic resolution work is held ({creation_admission}).'} "
            f"Until {integration_branch} absorbs {promotion_target}, the next "
            f"{integration_branch} -> {promotion_target} promotion will conflict. ({message[:160]})"
        ),
    )
    session.add(alert)
    await session.flush()

    spawned_task: Task | None = None
    if already_open:
        logger.info(
            "[BACKMERGE] a back-merge conflict-resolution task is already "
            "open for project %s — not spawning a duplicate",
            project_id,
        )
    elif task_creation_allowed:
        spawned_task = await _spawn_resolution_task(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id,
            trigger_desc,
            project=project,
        )
    else:
        logger.info(
            "[BACKMERGE] conflict-resolution task held for project %s: %s",
            project_id,
            creation_admission,
        )

    await _broadcast_alert(project_id, alert)
    if spawned_task is not None:
        await broadcast_to_project(
            str(project_id),
            "task_updated",
            {"task_id": str(spawned_task.id), "status": TaskStatus.QUEUED},
        )


async def _spawn_resolution_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    trigger_desc: str,
    *,
    project: Project | None = None,
) -> Task:
    """Create + queue a high-priority refactor task to resolve the conflict.

    The task targets ``automain`` (``base_branch="automain"``) and instructs a
    ``git merge origin/main`` conflict resolution.  It is a NORMAL
    automain-bound fix — it deliberately does NOT carry ``hotfix:true``.

    Single direct task (not a plan/decomposer): the per-conflict reasoning
    rules in the prompt must reach the executing agent verbatim, and a plan
    task would route the prompt through the planner-decomposer first — the
    subtask prompts the planner writes are not guaranteed to carry the parent
    guardrails.  Refactor matches the semantics (structural integration, no
    new behavior, branch from automain, push, open PR back to automain) and
    its branch prefix ``blueprint/refactor-…`` identifies the work clearly.

    ``context["backmerge_conflict"] = True`` is the marker field both for
    dedup (:func:`_resolution_task_already_open`) and for the merge-method
    gate in task_lifecycle.py (a back-merge resolution PR must be merged with
    ``merge_method="merge"``, never squash — otherwise ``main`` does not
    appear as a parent of ``automain`` and the merge-base never advances).
    """
    integration_branch, promotion_target = _lts_branch_names(project)
    trigger_clause = f" ({trigger_desc})" if trigger_desc else ""
    prompt = (
        f"The automated `{promotion_target} -> {integration_branch}` back-merge PR #{pr_number} "
        f"({pr_url or 'no URL'}) hit a merge conflict and could not be merged. "
        f"`{integration_branch}` must absorb `{promotion_target}`{trigger_clause}, or the next "
        f"`{integration_branch} -> {promotion_target}` promotion will conflict.\n\n"
        f"From a fresh branch off `{integration_branch}`, run `git merge origin/{promotion_target}`, "
        f"resolve every conflict per the policy below, commit the merge, push, "
        f"and open a PR back to `{integration_branch}` so the orchestrator can land it.\n\n"
        f"## How to reason about each conflict\n"
        f"1. **Read intent on both sides first.** `{promotion_target}` commits are usually "
        f"small, targeted bugfixes / hotfixes / behavioral corrections — they "
        f"were written to fix something specific. `{integration_branch}` changes tend to "
        f"be wider — decompositions, refactors, new modules being integrated. "
        f"Use `git log --oneline HEAD..origin/{promotion_target} -- <file>` and the recent "
        f"`{integration_branch}` commits touching the same file before resolving each hunk.\n"
        f"2. **Default rule: preserve both intents.** Re-apply `{promotion_target}`'s fix "
        f"*on top of* `{integration_branch}`'s new structure. If `{integration_branch}` renamed, moved, "
        f"or decomposed code that `{promotion_target}` then patched, locate the equivalent "
        f"spot in the new structure and apply the patch there. Don't discard "
        f"the fix because the surrounding code shifted.\n"
        f"3. **When intents genuinely conflict** (e.g. `{promotion_target}` reverted a "
        f"behavior `{integration_branch}` depends on, or `{promotion_target}` deleted code "
        f"`{integration_branch}` still calls): `{promotion_target}` wins on behavior — it is the "
        f"human-reviewed LTS branch — and `{integration_branch}`'s dependent code must be "
        f"updated to match. Call out such cases in the PR body so reviewers can sanity-check.\n"
        f"4. **Pure additions on disjoint lines:** keep both.\n\n"
        f"## File-class policies\n"
        f"- `module_size_limits.json` and other size-cap files: take the "
        f"higher cap.\n"
        f"- Lockfiles (`package-lock.json`, `uv.lock`, etc.): regenerate, "
        f"don't hand-merge.\n"
        f"- Generated files / snapshots: take `{promotion_target}` and regenerate if a "
        f"generator exists.\n"
        f"- Test files: keep both sides' tests; if they conflict on the same "
        f"assertion, prefer `{promotion_target}`'s assertion and adjust `{integration_branch}`'s test "
        f"infrastructure to satisfy it.\n\n"
        f"## Guardrails\n"
        f"- This is a normal {integration_branch}-targeted task, NOT a hotfix.\n"
        f"- Do NOT delete or skip tests just to make the merge build pass.\n"
        f"- Do NOT clobber either side wholesale — every conflict needs a "
        f"per-hunk decision per the rules above.\n"
        f"- In the PR body, list each non-trivial conflict and which side won "
        f"(or how you synthesized)."
    )

    payload: dict[str, Any] = {
        "type": _RESOLUTION_TASK_TYPE,
        "prompt": prompt,
        "base_branch": integration_branch,
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get(
        _RESOLUTION_TASK_TYPE
    )
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get(
        _RESOLUTION_TASK_TYPE
    )
    if type_effort:
        payload["effort"] = type_effort
    try:
        payload = validate_internal_payload(
            payload, "hotfix_backmerge:conflict_resolution_task"
        )
    except PayloadTooLargeError:
        # The resolution-task payload is tiny; a too-large error is a real bug.
        raise

    task = Task(
        project_id=project_id,
        title=f"Resolve back-merge conflict: {promotion_target} -> {integration_branch}",
        description=(
            f"The automated {promotion_target} -> {integration_branch} back-merge PR #{pr_number} hit a "
            f"merge conflict; resolve it on {integration_branch} so the next promotion "
            f"does not conflict."
        ),
        priority=_RESOLUTION_PRIORITY,
        payloads=[payload],
        context={
            # Marker field — _resolution_task_already_open dedupes on this AND
            # the merge sites in task_lifecycle.py read it to pick
            # merge_method="merge" instead of the default squash.
            "backmerge_conflict": True,
            # Traceability only (the dedup is project-wide, not keyed on this).
            "hotfix_task_id": hotfix_task_id,
            "backmerge_pr_number": pr_number,
            "backmerge_pr_url": pr_url,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        origination=TASK_ORIGINATION_HOTFIX,
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "BACKMERGE_CONFLICT",
                "priority": _RESOLUTION_PRIORITY,
                "backmerge_pr_number": pr_number,
                "hotfix_task_id": hotfix_task_id,
            },
        )
    )
    logger.warning(
        "[BACKMERGE] spawned conflict-resolution task %s "
        "(priority=%d, type=%s) for back-merge PR #%s",
        task.id,
        _RESOLUTION_PRIORITY,
        _RESOLUTION_TASK_TYPE,
        pr_number,
    )
    return task


async def _backmerge_failed_checks(
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: uuid.UUID,
    *,
    pr_detail: dict[str, Any] | None = None,
) -> list[str] | None:
    """Return the failing check names when the back-merge PR's CI is RED, else None.

    ``None`` means "not a fixable CI failure" — a green/pending build, a
    no-checks repo, a reviews-only block, or ANY lookup error. The error case is
    deliberately fail-SAFE: a transient GitHub hiccup must never spawn a CI-fix
    task on a back-merge that is merely waiting on a pending build (or healthy).
    A non-``None`` list means a completed build with at least one failed check;
    the names are surfaced in the fix task's prompt.
    """
    from app.ci_resolution import _CI_RED, _classify_check_runs

    head_sha = ((pr_detail or {}).get("head") or {}).get("sha")
    if not head_sha:
        try:
            fresh = await github.get_pull_request(
                token, owner, repo, pr_number, project_id=str(project_id)
            )
            head_sha = ((fresh or {}).get("head") or {}).get("sha")
        except Exception as exc:  # noqa: BLE001 — fail-safe to "not a CI failure"
            logger.debug(
                "[BACKMERGE] could not resolve head SHA for PR #%s: %s", pr_number, exc
            )
            return None
    if not head_sha:
        return None
    try:
        checks = await github.get_check_runs_cached(
            token, owner, repo, head_sha, project_id=str(project_id)
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe to "not a CI failure"
        logger.debug(
            "[BACKMERGE] could not read check-runs for PR #%s: %s", pr_number, exc
        )
        return None
    if _classify_check_runs(checks) != _CI_RED:
        return None
    return [
        d.get("name") or "?"
        for d in (checks.get("details") or [])
        if (d.get("conclusion") or "")
        in ("failure", "cancelled", "timed_out", "action_required")
    ]


async def _handle_ci_failure(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    trigger_desc: str,
    *,
    failed_checks: list[str],
    message: str,
    periodic: bool,
    project: Project | None = None,
    token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
) -> None:
    """Raise a CI-blocked Alert and (if no back-merge remediation is open) spawn a CI-fix task.

    Mirrors :func:`_handle_conflict` exactly — same advisory-lock serialization
    and the SAME project-wide dedup (:func:`_resolution_task_already_open`),
    which works because the CI-fix task carries ``backmerge_conflict=True`` too.
    That unifies the guard: a conflict-resolution task already in flight will
    also do the full ``git merge origin/main`` and surface this CI failure, so
    we never stack a second remediation; and once a CI-fix task is open the
    conflict path dedups against it as well.
    """
    try:
        lock_acquired = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": f"blueprint:backmerge_ci_fix:{project_id}"},
            )
        ).scalar()
    except Exception as _lock_err:  # noqa: BLE001 — non-PG backend: fall through to dedup
        lock_acquired = True
        logger.debug(
            "[BACKMERGE] advisory lock unavailable (%s) — using read-committed dedup",
            _lock_err,
        )
    if not lock_acquired:
        logger.info(
            "[BACKMERGE] CI-fix advisory lock not acquired for project %s — "
            "concurrent caller is mid-spawn; returning without spawning",
            project_id,
        )
        return

    already_open = await _resolution_task_already_open(
        session, project_id, token=token, owner=owner, repo=repo
    )
    if periodic and already_open:
        logger.info(
            "[BACKMERGE] back-merge CI failure for project %s — a remediation "
            "task is already open; periodic reconciler stays quiet",
            project_id,
        )
        return

    creation_admission = "already_open"
    if not already_open:
        from app.task_locking import (
            AUTOMATIC_TASK_CREATION_ALLOWED,
            acquire_automatic_task_creation_authority,
        )

        creation_admission = await acquire_automatic_task_creation_authority(
            session, project_id
        )
    task_creation_allowed = already_open or (
        creation_admission == AUTOMATIC_TASK_CREATION_ALLOWED
    )

    integration_branch, promotion_target = _lts_branch_names(project)
    checks_str = ", ".join(failed_checks[:5]) or "required checks"
    trigger_clause = f" (triggered by {trigger_desc})" if trigger_desc else ""
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.ERROR,
        title=f"Back-merge blocked by CI: {promotion_target} -> {integration_branch}",
        event_type="rollup.backmerge_blocked",
        message=(
            f"The automated {promotion_target} -> {integration_branch} back-merge "
            f"(PR #{pr_number}{trigger_clause}) merges cleanly but CI is red on the "
            f"merged result ({checks_str}) — a semantic merge break. "
            f"{'A CI-fix task is already active or has been spawned.' if task_creation_allowed else f'Automatic CI-fix work is held ({creation_admission}).'} "
            f"Until {integration_branch} absorbs {promotion_target}, "
            f"the next {integration_branch} -> {promotion_target} promotion will conflict."
        ),
    )
    session.add(alert)
    await session.flush()

    spawned_task: Task | None = None
    if already_open:
        logger.info(
            "[BACKMERGE] a back-merge remediation task is already open for "
            "project %s — not spawning a duplicate CI-fix task",
            project_id,
        )
    elif task_creation_allowed:
        spawned_task = await _spawn_ci_fix_task(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id,
            trigger_desc,
            failed_checks=failed_checks,
            project=project,
        )
    else:
        logger.info(
            "[BACKMERGE] CI-fix task held for project %s: %s",
            project_id,
            creation_admission,
        )

    await _broadcast_alert(project_id, alert)
    if spawned_task is not None:
        await broadcast_to_project(
            str(project_id),
            "task_updated",
            {"task_id": str(spawned_task.id), "status": TaskStatus.QUEUED},
        )


async def _spawn_ci_fix_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    trigger_desc: str,
    *,
    failed_checks: list[str],
    project: Project | None = None,
) -> Task:
    """Create + queue a high-priority task to fix a CI-red (but clean-merging) back-merge.

    Same task type / priority / branch as :func:`_spawn_resolution_task`, and it
    also sets ``context["backmerge_conflict"] = True`` so it inherits the
    merge-method=merge gate (the fix PR must merge, not squash, so ``main`` stays
    a parent of ``automain``) and the unified dedup. ``backmerge_ci_failure`` and
    the failing-check list are recorded for traceability.
    """
    integration_branch, promotion_target = _lts_branch_names(project)
    trigger_clause = f" ({trigger_desc})" if trigger_desc else ""
    checks_str = ", ".join(failed_checks) or "required checks"
    prompt = (
        f"The automated `{promotion_target} -> {integration_branch}` back-merge PR #{pr_number} "
        f"({pr_url or 'no URL'}) MERGES CLEANLY (no conflict markers) but FAILS CI on the "
        f"merged result, so the orchestrator cannot land it and `{integration_branch}` will "
        f"not absorb `{promotion_target}`{trigger_clause}.\n\n"
        f"Failing checks: {checks_str}.\n\n"
        f"This is almost always a SEMANTIC merge break: the 3-way text merge succeeded, but "
        f"`{promotion_target}` added or changed code that references a symbol `{integration_branch}` "
        f"moved, renamed, split into a sub-module, or deleted in a refactor — so the merged "
        f"tree imports/builds/tests differently than either side alone. There are NO conflicts "
        f"to resolve; the fix is to make the merged code consistent again.\n\n"
        f"## What to do\n"
        f"1. From a fresh branch off `{integration_branch}`, run `git merge origin/{promotion_target}` "
        f"(it merges cleanly — do not manufacture conflicts).\n"
        f"2. Reproduce the failure that the failing check covers (e.g. the backend test suite "
        f"for a `CI / Backend` shard) and read the ACTUAL error.\n"
        f"3. Fix the root cause on the merged tree: find where `{integration_branch}`'s refactor "
        f"moved/renamed the symbol that `{promotion_target}`'s code now references, and update the "
        f"merged-in code to match the new structure (fix the import, call the relocated "
        f"function, adjust to a changed signature). Prefer making the code robust (e.g. a "
        f"local import) over reverting either side.\n"
        f"4. Re-run until the failing check is green, push, and open a PR back to "
        f"`{integration_branch}` so the orchestrator can land it.\n\n"
        f"## Guardrails\n"
        f"- This is a normal {integration_branch}-targeted task, NOT a hotfix.\n"
        f"- Do NOT delete or skip the failing tests to go green — they are catching a real "
        f"break in the merged result. Fix the code.\n"
        f"- Preserve BOTH intents: do not discard `{promotion_target}`'s change or "
        f"`{integration_branch}`'s refactor; only reconcile where they interact.\n"
        f"- If a check is genuinely failing for an infra/flaky reason (not a defect on the "
        f"merged tree), say so in the PR body rather than masking it.\n"
        f"- In the PR body, name each failing check and the merged-tree fix you applied."
    )

    payload: dict[str, Any] = {
        "type": _RESOLUTION_TASK_TYPE,
        "prompt": prompt,
        "base_branch": integration_branch,
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get(
        _RESOLUTION_TASK_TYPE
    )
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get(
        _RESOLUTION_TASK_TYPE
    )
    if type_effort:
        payload["effort"] = type_effort
    payload = validate_internal_payload(payload, "hotfix_backmerge:ci_fix_task")

    task = Task(
        project_id=project_id,
        title=f"Fix back-merge CI failure: {promotion_target} -> {integration_branch}",
        description=(
            f"The automated {promotion_target} -> {integration_branch} back-merge PR #{pr_number} "
            f"merges cleanly but CI is red on the merged result ({checks_str}); fix it on "
            f"{integration_branch} so the next promotion does not conflict."
        ),
        priority=_RESOLUTION_PRIORITY,
        payloads=[payload],
        context={
            # backmerge_conflict=True gives the merge-method=merge gate + unified
            # dedup; backmerge_ci_failure is the traceability/flavor marker.
            "backmerge_conflict": True,
            "backmerge_ci_failure": True,
            "failed_checks": failed_checks,
            "hotfix_task_id": hotfix_task_id,
            "backmerge_pr_number": pr_number,
            "backmerge_pr_url": pr_url,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        origination=TASK_ORIGINATION_HOTFIX,
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "BACKMERGE_CI_FAILURE",
                "priority": _RESOLUTION_PRIORITY,
                "backmerge_pr_number": pr_number,
                "failed_checks": failed_checks,
                "hotfix_task_id": hotfix_task_id,
            },
        )
    )
    logger.warning(
        "[BACKMERGE] spawned CI-fix task %s (priority=%d, type=%s) for back-merge "
        "PR #%s — failing: %s",
        task.id,
        _RESOLUTION_PRIORITY,
        _RESOLUTION_TASK_TYPE,
        pr_number,
        checks_str,
    )
    return task


async def _handle_migration_block(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    trigger_desc: str,
    *,
    reason: str,
    periodic: bool,
    project: Project | None = None,
    gh_token: str | None = None,
    owner: str | None = None,
    repo: str | None = None,
    integration_branch: str | None = None,
    promotion_target: str | None = None,
) -> None:
    """Raise a migration-block Alert and (if none open) spawn a dual-head resolver.

    Mirrors :func:`_handle_conflict` / :func:`_handle_ci_failure` exactly — same
    advisory-lock serialization and the SAME project-wide dedup
    (:func:`_resolution_task_already_open`), which works because the spawned
    resolver carries ``backmerge_conflict=True`` too. That unifies the guard: a
    conflict / CI-fix resolver already in flight does the full ``git merge
    origin/<promotion_target>`` and will surface this dual head as well, so we
    never stack a second remediation.

    When the GitHub credentials are threaded in (``gh_token`` / ``owner`` /
    ``repo`` / ``integration_branch`` / ``promotion_target``) AND a resolver is
    actually being spawned (i.e. after the project-wide dedup check passes), this
    consults :func:`_plan_backmerge_migration_reconciliation` for a concrete,
    already-validated integration-side renumber plan. The plan is computed ONLY
    on the spawn path so deduped periodic re-entries make no extra GitHub calls.
    It is best-effort and fail-safe: any planner exception / ``None`` result
    degrades cleanly to the existing prose-only resolver prompt — it never
    blocks the spawn and never pushes anything to the live integration branch.
    """
    try:
        lock_acquired = (
            await session.execute(
                text("SELECT pg_try_advisory_xact_lock(hashtext(:key))"),
                {"key": f"blueprint:backmerge_migration_block:{project_id}"},
            )
        ).scalar()
    except Exception as _lock_err:  # noqa: BLE001 — non-PG backend: fall through to dedup
        lock_acquired = True
        logger.debug(
            "[BACKMERGE] advisory lock unavailable (%s) — using read-committed dedup",
            _lock_err,
        )
    if not lock_acquired:
        logger.info(
            "[BACKMERGE] migration-block advisory lock not acquired for project %s "
            "— concurrent caller is mid-spawn; returning without spawning",
            project_id,
        )
        return

    already_open = await _resolution_task_already_open(
        session,
        project_id,
        token=gh_token,
        owner=owner,
        repo=repo,
    )
    if periodic and already_open:
        logger.info(
            "[BACKMERGE] back-merge migration block for project %s — a remediation "
            "task is already open; periodic reconciler stays quiet",
            project_id,
        )
        return

    creation_admission = "already_open"
    if not already_open:
        from app.task_locking import (
            AUTOMATIC_TASK_CREATION_ALLOWED,
            acquire_automatic_task_creation_authority,
        )

        creation_admission = await acquire_automatic_task_creation_authority(
            session, project_id
        )
    task_creation_allowed = already_open or (
        creation_admission == AUTOMATIC_TASK_CREATION_ALLOWED
    )

    integration_branch, promotion_target = _lts_branch_names(project)
    trigger_clause = f" (triggered by {trigger_desc})" if trigger_desc else ""
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.ERROR,
        title=f"Back-merge blocked by Alembic chain: {promotion_target} -> {integration_branch}",
        event_type="rollup.backmerge_blocked",
        message=(
            f"The automated {promotion_target} -> {integration_branch} back-merge "
            f"(PR #{pr_number}{trigger_clause}) was BLOCKED before merge: it would "
            f"create an invalid Alembic migration chain ({reason}). A hotfix "
            f"migration on {promotion_target} and a feature migration on "
            f"{integration_branch} collide (dual head / duplicate revision). "
            f"{'A resolution task is already active or has been spawned.' if task_creation_allowed else f'Automatic resolution work is held ({creation_admission}).'} "
            f"Until {integration_branch} absorbs "
            f"{promotion_target}, the next {integration_branch} -> {promotion_target} "
            f"promotion will conflict."
        ),
    )
    session.add(alert)
    await session.flush()

    spawned_task: Task | None = None
    if already_open:
        logger.info(
            "[BACKMERGE] a back-merge remediation task is already open for "
            "project %s — not spawning a duplicate migration resolver",
            project_id,
        )
    elif task_creation_allowed:
        # Compute the concrete renumber plan ONLY now that we know a resolver
        # is actually being spawned (a deduped periodic re-entry returned above
        # and made no GitHub calls). Best-effort + fail-safe: any exception or a
        # None result leaves `plan` None and the resolver falls back to the
        # existing prose-only guidance.
        plan: dict[str, Any] | None = None
        if gh_token and owner and repo and integration_branch and promotion_target:
            try:
                plan = await _plan_backmerge_migration_reconciliation(
                    gh_token,
                    owner,
                    repo,
                    pr_number,
                    integration_branch,
                    promotion_target,
                )
            except Exception as exc:  # noqa: BLE001 — planner is advisory; never block the spawn
                logger.debug(
                    "[BACKMERGE] renumber planner failed for PR #%s (project %s): "
                    "%s — falling back to prose resolver",
                    pr_number,
                    project_id,
                    exc,
                )
                plan = None
        spawned_task = await _spawn_migration_resolution_task(
            session,
            project_id,
            pr_number,
            pr_url,
            hotfix_task_id,
            trigger_desc,
            reason=reason,
            project=project,
            plan=plan,
        )
    else:
        logger.info(
            "[BACKMERGE] migration-resolution task held for project %s: %s",
            project_id,
            creation_admission,
        )

    await _broadcast_alert(project_id, alert)
    if spawned_task is not None:
        await broadcast_to_project(
            str(project_id),
            "task_updated",
            {"task_id": str(spawned_task.id), "status": TaskStatus.QUEUED},
        )


def _render_renumber_plan_section(
    plan: dict[str, Any] | None,
    integration_branch: str,
    promotion_target: str,
) -> str:
    """Render the concrete renumber plan as a prompt section, or ``""`` if None.

    The plan comes from :func:`_plan_backmerge_migration_reconciliation` and is
    the already-chain-validated integration-side renumber for the unambiguous
    single-collision case. It is presented as a known-correct CANDIDATE to
    verify-then-apply against the live integration branch (which may have
    advanced since the block fired), NOT a blind apply — a stale plan is
    self-correcting because the resolver re-derives the chain on its fresh
    branch. A ``None`` plan yields the empty string, leaving the prose-only
    guidance untouched.
    """
    if not plan:
        return ""
    old_path = plan.get("old_path")
    new_path = plan.get("new_path")
    old_revision = plan.get("old_revision")
    new_revision = plan.get("new_revision")
    new_down_revision = plan.get("new_down_revision")
    expected_schema_revision = plan.get("expected_schema_revision")
    upserts = plan.get("upserts") or {}
    return (
        f"## Known-good renumber plan (verify, then apply)\n"
        f"The system already computed and chain-validated the exact mechanical "
        f"renumber for this single-collision block. Treat it as a known-correct "
        f"CANDIDATE: confirm each step still holds against the CURRENT "
        f"`{integration_branch}` state on your fresh branch (it may have advanced "
        f"since this was computed), then apply it. A stale plan is "
        f"self-correcting — re-derive from the live chain if anything no longer "
        f"matches; do NOT apply blindly.\n"
        f"- Rename the colliding `{integration_branch}`-side migration "
        f"`{old_path}` -> `{new_path}`.\n"
        f"- Renumber its revision id `{old_revision}` -> `{new_revision}` "
        f"(the next free revision above every existing one).\n"
        f"- Rechain it: set its `down_revision` to `{new_down_revision}` (the "
        f"unified `{promotion_target}` head).\n"
        f"- Renumber the ENTIRE linear descendant tail (if any) monotonically "
        f"into the contiguous block of fresh revisions above `{new_revision}`, "
        f"rechaining each migration onto its renumbered predecessor, so the "
        f"NNNN order matches the chain order and the numeric-max revision is "
        f"the head (see the rewritten sources below for the exact result).\n"
        f"- Bump `EXPECTED_SCHEMA_REVISION` in `backend/app/main.py` to "
        f"`{expected_schema_revision}` (the resulting single head).\n"
        f"## Rewritten migration sources\n"
        f"Use these exact file bodies if the live branch still matches the "
        f"precomputed plan:\n"
        + "".join(
            f"### `{path}`\n```python\n{source}\n```\n\n"
            for path, source in upserts.items()
        )
    )


async def _spawn_migration_resolution_task(
    session: AsyncSession,
    project_id: uuid.UUID,
    pr_number: int,
    pr_url: str,
    hotfix_task_id: str | None,
    trigger_desc: str,
    *,
    reason: str,
    project: Project | None = None,
    plan: dict[str, Any] | None = None,
) -> Task:
    """Create + queue a high-priority Alembic-dual-head-aware resolution task.

    Same task type / priority / branch as :func:`_spawn_resolution_task`, and it
    also sets ``context["backmerge_conflict"] = True`` so it inherits the
    merge-method=merge gate (the resolution PR must merge, not squash, so the
    promotion target stays a parent of the integration branch) and the unified
    project-wide dedup. The prompt is dual-head-aware: the promotion-target
    migration is immutable (already applied in production), so the
    integration-side migration is the one to renumber and rechain.

    When ``plan`` is supplied (the unambiguous single-collision case, from
    :func:`_plan_backmerge_migration_reconciliation`), a concrete "## Known-good
    renumber plan" section is prepended to the prompt with the exact file to
    rename, the old/new revision ids, the ``down_revision`` to rechain onto, and
    the resulting ``EXPECTED_SCHEMA_REVISION``; the structured plan is also stored
    under ``context["backmerge_renumber_plan"]``. The plan is presented as a
    known-correct candidate to VERIFY-then-apply against the live integration
    branch (which may have advanced since the block fired) — a stale plan is
    self-correcting, NOT a blind apply. When ``plan`` is ``None`` (multiple
    collisions, fan-out tail, parse failure, stale non-shared anchor, empty diff,
    or fetch error) the prompt is the existing prose guidance UNCHANGED.
    """
    integration_branch, promotion_target = _lts_branch_names(project)
    trigger_clause = f" ({trigger_desc})" if trigger_desc else ""
    plan_section = _render_renumber_plan_section(
        plan, integration_branch, promotion_target
    )
    prompt = (
        f"The automated `{promotion_target} -> {integration_branch}` back-merge PR "
        f"#{pr_number} ({pr_url or 'no URL'}) was BLOCKED before merge because the "
        f"combined Alembic migration chain is invalid: {reason}.\n\n"
        f"This is a dual-head / duplicate-revision collision: a hotfix migration "
        f"landed on `{promotion_target}` and a feature migration was added on "
        f"`{integration_branch}` since the last promotion. They each branched from the "
        f"same head and took the same NNNN revision number, so once merged Alembic "
        f"sees two heads (or a duplicate revision id) and `alembic upgrade head` fails "
        f"— wedging `{integration_branch}` for every later task.\n\n"
        f"{plan_section}"
        f"## How to resolve{trigger_clause}\n"
        f"1. From a fresh branch off `{integration_branch}`, run "
        f"`git merge origin/{promotion_target}`.\n"
        f"2. The `{promotion_target}` migration is IMMUTABLE — it is already applied "
        f"in production, so it is NOT the one to change. Renumber the "
        f"`{integration_branch}`-side migration to the next free NNNN revision and "
        f"rechain it (set its `down_revision` to the current single head). "
        f"Alternatively, unify the two heads with an `alembic merge` migration that "
        f"lists both as down_revisions.\n"
        f"3. Bump `EXPECTED_SCHEMA_REVISION` in `backend/app/main.py` to match the "
        f"resulting single head.\n"
        f"4. Verify with `alembic upgrade head` — it MUST resolve to exactly ONE "
        f"head. Then push and open a PR back to `{integration_branch}`.\n\n"
        f"## Guardrails\n"
        f"- This is a normal {integration_branch}-targeted task, NOT a hotfix.\n"
        f"- NEVER modify an already-applied migration on `{promotion_target}` — "
        f"applied history is immutable.\n"
        f"- Do NOT delete or skip migrations to force a single head; rechain them "
        f"correctly.\n"
        f"- In the PR body, name both colliding revisions and the final single head."
    )

    payload: dict[str, Any] = {
        "type": _RESOLUTION_TASK_TYPE,
        "prompt": prompt,
        "base_branch": integration_branch,
    }
    type_model = (_runtime_config.get("task_type_models", {}) or {}).get(
        _RESOLUTION_TASK_TYPE
    )
    if type_model:
        payload["model"] = type_model
    type_effort = (_runtime_config.get("task_type_efforts", {}) or {}).get(
        _RESOLUTION_TASK_TYPE
    )
    if type_effort:
        payload["effort"] = type_effort
    payload = validate_internal_payload(
        payload, "hotfix_backmerge:migration_resolution_task"
    )

    task = Task(
        project_id=project_id,
        title=f"Resolve back-merge Alembic dual head: {promotion_target} -> {integration_branch}",
        description=(
            f"The automated {promotion_target} -> {integration_branch} back-merge PR "
            f"#{pr_number} would create an invalid Alembic chain ({reason}); renumber "
            f"and rechain the {integration_branch}-side migration so the back-merge "
            f"resolves to a single head."
        ),
        priority=_RESOLUTION_PRIORITY,
        payloads=[payload],
        context={
            # backmerge_conflict=True gives the merge-method=merge gate + unified
            # dedup; backmerge_migration_conflict is the traceability/flavor marker.
            "backmerge_conflict": True,
            "backmerge_migration_conflict": True,
            "alembic_reason": reason,
            "hotfix_task_id": hotfix_task_id,
            "backmerge_pr_number": pr_number,
            "backmerge_pr_url": pr_url,
            # The concrete, already-validated integration-side renumber plan for
            # the unambiguous single-collision case (None for everything else).
            # The resolver re-validates it against the live integration branch.
            "backmerge_renumber_plan": plan,
        },
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
        origination=TASK_ORIGINATION_HOTFIX,
    )
    session.add(task)
    await session.flush()
    session.add(
        TaskHistory(
            task_id=task.id,
            action="SPAWNED",
            details={
                "spawned_by": "BACKMERGE_MIGRATION_BLOCK",
                "priority": _RESOLUTION_PRIORITY,
                "backmerge_pr_number": pr_number,
                "alembic_reason": reason,
                "hotfix_task_id": hotfix_task_id,
            },
        )
    )
    logger.warning(
        "[BACKMERGE] spawned Alembic dual-head resolver task %s (priority=%d, "
        "type=%s) for back-merge PR #%s — %s",
        task.id,
        _RESOLUTION_PRIORITY,
        _RESOLUTION_TASK_TYPE,
        pr_number,
        reason,
    )
    return task


async def _raise_blocked_alert(
    session: AsyncSession,
    project_id: uuid.UUID,
    trigger_desc: str,
    *,
    project: Project | None = None,
    reason: str,
    pr_url: str | None = None,
    periodic: bool = False,
) -> None:
    """Surface a non-conflict back-merge failure (missing creds, PR-creation
    failure, merge exception, 405 non-merge).

    ``periodic=False`` (a one-shot hotfix back-merge) — always create an ERROR
    Alert: a failed one-shot back-merge must never pass silently (design doc §6
    gotcha 6).

    ``periodic=True`` (the reconciler, which retries every cycle) — create an
    ERROR Alert too, but DEDUPED: only when no unresolved back-merge-blocked
    Alert is already open for the project.  A periodic caller must not go
    silent on a persistently stuck back-merge (CI red forever, revoked
    credentials) — that would leave main and automain diverged with no signal
    — but it must not stack an Alert every 10-minute cycle either.  So the
    first stuck cycle escalates with exactly one Alert; later cycles log at
    WARNING and stay quiet until that Alert is dismissed.

    No fix task is spawned either way — a 405 is a branch-protection / CI
    problem the operator resolves, not a code conflict.
    """
    integration_branch, promotion_target = _lts_branch_names(project)
    blocked_alert_title = (
        f"Back-merge failed: {promotion_target} -> {integration_branch}"
    )
    if periodic:
        existing = (
            (
                await session.execute(
                    select(Alert).where(
                        Alert.project_id == project_id,
                        Alert.title == blocked_alert_title,
                        Alert.dismissed_at.is_(None),
                    )
                )
            )
            .scalars()
            .first()
        )
        if existing is not None:
            logger.warning(
                "[BACKMERGE] periodic back-merge for project %s still "
                "blocked: %s — an unresolved Alert is already open; staying "
                "quiet until it is dismissed",
                project_id,
                reason,
            )
            return
        logger.warning(
            "[BACKMERGE] periodic back-merge for project %s blocked: %s — "
            "raising one Alert (will not repeat until dismissed)",
            project_id,
            reason,
        )

    trigger_clause = f" (triggered by {trigger_desc})" if trigger_desc else ""
    message = (
        f"The automated {promotion_target} -> {integration_branch} back-merge{trigger_clause} did not "
        f"complete: {reason}. {promotion_target} and {integration_branch} remain diverged — the next "
        f"{integration_branch} -> {promotion_target} promotion will conflict until this is resolved "
        f"manually."
    )
    if pr_url:
        message += f" Back-merge PR: {pr_url}"
    alert = Alert(
        project_id=project_id,
        severity=AlertSeverity.ERROR,
        title=blocked_alert_title,
        event_type="rollup.backmerge_blocked",
        message=message,
    )
    session.add(alert)
    await session.flush()
    await _broadcast_alert(project_id, alert)


async def _broadcast_alert(project_id: uuid.UUID, alert: Alert) -> None:
    """Best-effort websocket broadcast of a created Alert to the project."""
    try:
        await broadcast_to_project(
            str(project_id),
            "alert_created",
            {
                "alert_id": str(alert.id),
                "severity": alert.severity,
                "title": alert.title,
            },
        )
    except Exception as exc:  # noqa: BLE001 — a broadcast failure must not block
        logger.debug(
            "[BACKMERGE] alert broadcast failed for project %s: %s",
            project_id,
            exc,
        )
