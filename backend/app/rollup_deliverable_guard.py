"""Rollup child-deliverable survival guard (ghost-success detection).

Defect class (PR #17480, 2026-08-19): a conflict-fix task resolving a rollup
PR's merge conflicts resolved them WHOLESALE toward the base branch, silently
erasing a child PR's deliverable from the rollup branch. The rollup PR then
merged green and the rollup parent completed with "Rollup PR merged" — a ghost
success: the pipeline believed the work landed while part of it was destroyed
during conflict resolution.

This module is a DETECTION gate, not a resolution engine. Right before a
rollup parent is stamped COMPLETED off its merged rollup PR, it verifies that
each constituent child PR's deliverable survived into the merged rollup diff:
a staging-merged child whose GitHub-verified changed files are ENTIRELY absent
from the rollup PR's own changed-file set had its deliverable dropped.

Failure semantics (deliberately conservative — a guard that false-blocks
healthy rollups is itself a platform defect):

* ``"dropped"``       — at least one child's verified deliverable is entirely
  absent from the verified rollup diff AND a conflict-fix intervention on this
  PR is on record (the incident signature). The guard stamps a durable context
  receipt naming each dropped child and raises the operator failure alert; the
  caller must NOT stamp ghost success (it fails the parent with the WHY).
* ``"warned"``        — same verified zero-overlap but with NO conflict-fix
  evidence. Absence from the PR diff is then ambiguous (the base branch may
  have legitimately caught up with identical content, making the child's files
  disappear from the diff while the deliverable IS in the merged result), so
  the guard stamps a loud warning receipt + error log and lets completion
  proceed instead of hard-blocking.
* ``"intact"``        — every verifiable child overlaps the rollup diff (or
  there is nothing to verify).
* ``"indeterminate"`` — the rollup diff or credentials could not be resolved.
  Flag-trust is preserved: a transient GitHub outage never blocks completion
  (mirrors :func:`app.rollup_pr_verification.verify_rollup_merge_landed`).

Bounding: child changed files are read from the metadata already stamped at
merge time (``_pr_changed_files`` + provenance sentinels from ci_completion /
rollup propagation, PR #17159); at most ``_MAX_LIVE_CHILD_COMPARES`` live
commit-range compares fill gaps, and at most ONE live ``get_pr_files`` call
resolves the rollup diff when the stamped copy is unusable (truncated).
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.acceptance_provenance import PARENT_DERIVED_CHANGED_FILES_KEY
from app.landed_signal import (
    FORCE_DROP_UNBLOCK_REASONS,
    NON_MERGE_UNBLOCK_REASON_KEY,
    has_merge_unblock_signal,
)
from app.models import Project, Task, TaskStatus

logger = logging.getLogger(__name__)

# Durable context receipts on the rollup parent. DROPPED blocks ghost success;
# WARNING is the ambiguous-case loud receipt (completion still proceeds).
DROPPED_DELIVERABLES_KEY = "rollup_dropped_deliverables"
DROPPED_DELIVERABLES_REASON_KEY = "rollup_dropped_deliverables_reason"
DELIVERABLE_WARNING_KEY = "rollup_deliverable_warning"

# ── Machine-readable outcome vocabulary (``block_reason_code`` convention) ──
# Every guard invocation — pre-merge or post-merge — stamps exactly one of these
# on the rollup parent so operators and panels can attribute the outcome without
# regexing prose. Closed vocabulary: add a member here, never an ad-hoc string.
DELIVERABLE_REASON_CODE_KEY = "rollup_deliverable_reason_code"
RC_INTACT = "deliverables_intact"
RC_INDETERMINATE = "deliverable_check_indeterminate"
RC_WARNED_NO_CONFLICT_FIX = "deliverable_absent_no_conflict_fix"
RC_DROPPED_BY_CONFLICT_FIX = "deliverable_dropped_by_conflict_fix"
RC_PREMERGE_HOLD = "premerge_deliverable_drop_held"
RC_PREMERGE_HOLD_CAP_EXHAUSTED = "premerge_deliverable_hold_cap_exhausted"
RC_NOT_A_ROLLUP_MERGE = "not_a_rollup_pr_merge"

_ALL_DELIVERABLE_REASON_CODES: frozenset[str] = frozenset(
    {
        RC_INTACT,
        RC_INDETERMINATE,
        RC_WARNED_NO_CONFLICT_FIX,
        RC_DROPPED_BY_CONFLICT_FIX,
        RC_PREMERGE_HOLD,
        RC_PREMERGE_HOLD_CAP_EXHAUSTED,
        RC_NOT_A_ROLLUP_MERGE,
    }
)

_VERDICT_REASON_CODES = {
    "intact": RC_INTACT,
    "indeterminate": RC_INDETERMINATE,
    "warned": RC_WARNED_NO_CONFLICT_FIX,
    "dropped": RC_DROPPED_BY_CONFLICT_FIX,
}

# Pre-merge hold state. The hold is DELIBERATELY bounded: an unbounded hold on a
# rollup PR that is otherwise mergeable would wedge the parent forever, and a
# wedge is a worse platform defect than the loss it prevents. Each hold buys one
# more conflict-fix attempt that is explicitly told which files to restore; once
# the budget is spent the gate stops holding and the historical post-merge guard
# takes over (fail with the receipt), so behaviour degrades to today's rather
# than stranding. Shipped ON — this is permanent policy, not a rollout flag.
PREMERGE_HOLD_COUNT_KEY = "rollup_deliverable_premerge_holds"
MUST_PRESERVE_KEY = "rollup_deliverable_must_preserve"
RESTORATION_FIX_TASK_KEY = "rollup_deliverable_restoration_task_id"
PREMERGE_HOLD_CAP = 2

# Bounds: rollups have modest child counts; these caps only guard pathology.
_MAX_DESCENDANTS = 300
_MAX_LIVE_CHILD_COMPARES = 8
_MISSING_FILES_SAMPLE = 10


async def finalize_rollup_parent_after_merge(
    orig: Task,
    project: "Project | None",
    session,
    *,
    terminal: bool,
    base_branch,
    pr_number,
    github_credentials: tuple | None = None,
    ci_loop: dict | None = None,
    merge_task: "Task | None" = None,
    source: str,
) -> str:
    """Stamp the rollup parent's terminal state after its PR merged.

    Extracted from the (size-capped) ci_completion / task_lifecycle merge
    handlers. ``terminal=False`` (custom-staged rollup awaiting downstream
    landing) keeps today's deferred message. ``terminal=True`` runs the
    ghost-success deliverable guard first: verdict ``"dropped"`` fails the
    parent with the reason naming the dropped child PR(s); every other verdict
    (including a guard crash — the guard must never break completion) stamps
    the historical ``COMPLETED`` / "Rollup PR merged". Returns ``"deferred"``,
    ``"failed"``, or ``"completed"``.
    """
    if not terminal:
        orig.latest_status_message = (
            f"Rollup PR merged into {base_branch}; awaiting downstream landing"
        )
        return "deferred"
    verdict = "indeterminate"
    try:
        verdict = await verify_rollup_child_deliverables(
            orig,
            project,
            session,
            pr_number=pr_number,
            github_credentials=github_credentials,
            ci_loop=ci_loop,
            merge_task=merge_task,
            source=source,
        )
    except Exception:
        logger.warning(
            "[ROLLUP_DELIVERABLE] guard errored for task %s — not blocking "
            "completion (%s)",
            getattr(orig, "id", None),
            source,
            exc_info=True,
        )
    if verdict == "dropped":
        # NOT CODE_ERROR. The child did the work and its PR merged; the
        # pipeline's own conflict resolution erased it. Charging the agent for a
        # platform defect burns the retry budget, spawns a useless diagnosis
        # task, and feeds erosion/direction-health scoring with damage the
        # engine inflicted on itself.
        from app.failure_classification import (
            ROLLUP_DELIVERABLE_DROPPED,
            persist_failure_category,
        )

        orig.status = TaskStatus.FAILED
        orig.latest_status_message = (orig.context or {}).get(
            DROPPED_DELIVERABLES_REASON_KEY
        ) or (
            "Rollup PR merged but a child deliverable was dropped during "
            "conflict resolution"
        )
        persist_failure_category(orig, ROLLUP_DELIVERABLE_DROPPED)
        return "failed"
    orig.status = TaskStatus.COMPLETED
    orig.latest_status_message = "Rollup PR merged"
    return "completed"


def _stamp_reason_code(orig: "Task", verdict: str) -> str:
    """Record the machine-readable outcome next to the human prose.

    Mirrors the ``block_reason_code`` convention: the prose can be reworded at
    any time, so attribution must key off a closed-vocabulary identifier that an
    operator can grep for and a panel can bucket on.
    """
    code = _VERDICT_REASON_CODES[verdict]
    orig.context = {**(orig.context or {}), DELIVERABLE_REASON_CODE_KEY: code}
    return verdict


def _verified_own_changed_files(ctx: dict) -> list[str] | None:
    """The task's GitHub-verified OWN-PR changed files, or None if unusable.

    Parent-derived (rollup-union) provenance is rejected — comparing the
    parent's own diff against itself would be circular and always "survive".
    Truncated child lists remain usable for SURVIVAL testing: any listed file
    present in the rollup diff proves survival, and a wholesale drop erases
    every file, including the ≤50 stored ones.
    """
    if ctx.get("_pr_changed_files_github_verified") is not True:
        return None
    if ctx.get(PARENT_DERIVED_CHANGED_FILES_KEY) is True:
        return None
    raw = ctx.get("_pr_changed_files")
    if not isinstance(raw, list):
        return None
    return [p for p in raw if isinstance(p, str) and p.strip()]


def _child_is_deliverable_candidate(ctx: dict) -> bool:
    """Whether a descendant genuinely contributed a PR to the rollup branch.

    Force-dropped children (no PR artifact / malformed URL / respawn cap) and
    deliberate success-closes or supersessions never delivered code THROUGH
    this rollup, so their absence from the rollup diff is expected, not a drop.
    """
    if ctx.get(NON_MERGE_UNBLOCK_REASON_KEY) in FORCE_DROP_UNBLOCK_REASONS:
        return False
    if ctx.get("pr_closed_success") or ctx.get("pr_closed_empty"):
        return False
    return not (
        ctx.get("superseded") or ctx.get("pr_superseded") or ctx.get("descoped")
    )


def _pr_numbers_match(candidate, pr_number) -> bool:
    try:
        return candidate is not None and int(candidate) == int(pr_number)
    except (TypeError, ValueError):
        return False


def _child_pr_number(child: Task):
    """Best-effort PR number for naming the dropped child (never raises)."""
    from app.ci_merge_helpers import effective_pr_number

    number = effective_pr_number(child)
    if number is None:
        number = (child.first_payload or {}).get("pr_number")
    try:
        return int(number) if number is not None else None
    except (TypeError, ValueError):
        return None


async def _resolve_rollup_changed_files(
    orig: Task,
    session: AsyncSession,
    *,
    pr_number,
    credentials: tuple | None,
    prefer_live: bool = False,
) -> tuple[set[str] | None, tuple | None]:
    """The rollup PR's authoritative changed-file set.

    Prefers the GitHub-verified, non-truncated list already stamped at landing
    (PR #17159) over a live call. Returns ``(None, credentials)`` when no
    authoritative set can be resolved (caller degrades to indeterminate).

    ``prefer_live`` skips the stamped copy entirely. The PRE-MERGE gate must use
    it: the stamp is written when a PR *lands*, so pre-merge any stamp present
    describes a PREVIOUS PR of this parent, and testing a live child's files for
    absence against a stale diff would manufacture a false drop.
    """
    ctx = orig.context or {}
    stamped = None if prefer_live else _verified_own_changed_files(ctx)
    if stamped is not None and not ctx.get("_pr_changed_files_truncated"):
        return set(stamped), credentials

    # Stamped copy unusable (absent or truncated at 50 — absence testing on a
    # truncated list is unsound). One bounded live fetch, paginated internally.
    if credentials is None:
        try:
            from app.routers.agent import get_github_token_for_task

            credentials = await get_github_token_for_task(orig.id, session)
        except Exception:
            logger.debug(
                "[ROLLUP_DELIVERABLE] credentials unavailable for task %s",
                getattr(orig, "id", None),
            )
            return None, None
    if (
        not isinstance(credentials, tuple)
        or len(credentials) != 3
        or not all(isinstance(v, str) and v.strip() for v in credentials)
    ):
        return None, None
    try:
        from app.github import get_pr_files

        raw_files = await get_pr_files(
            credentials[0],
            credentials[1],
            credentials[2],
            int(pr_number),
            str(orig.project_id) if orig.project_id is not None else None,
        )
    except Exception:
        logger.warning(
            "[ROLLUP_DELIVERABLE] rollup PR #%s file list fetch failed for "
            "task %s — degrading to indeterminate",
            pr_number,
            getattr(orig, "id", None),
            exc_info=True,
        )
        return None, credentials
    return (
        {
            item["filename"]
            for item in (raw_files or [])
            if isinstance(item, dict)
            and isinstance(item.get("filename"), str)
            and item["filename"].strip()
        },
        credentials,
    )


async def verify_rollup_child_deliverables(
    orig: Task,
    project: "Project | None",
    session: AsyncSession,
    *,
    pr_number,
    github_credentials: tuple | None = None,
    ci_loop: dict | None = None,
    merge_task: "Task | None" = None,
    source: str,
    raise_alert: bool = True,
    prefer_live_rollup_files: bool = False,
) -> str:
    """Verify each constituent child deliverable survived the rollup diff.

    Run by the rollup completion sites (ci_completion / task_lifecycle) AFTER
    the rollup PR merged and BEFORE the parent is stamped COMPLETED. Returns
    ``"intact"``, ``"indeterminate"``, ``"warned"``, or ``"dropped"`` — see the
    module docstring for exact semantics. Never raises for GitHub / credential
    errors (degrades to ``"indeterminate"``); on ``"dropped"`` the durable
    receipt + operator alert have already been recorded and the caller must
    not stamp ghost success.
    """
    if pr_number is None or project is None:
        return _stamp_reason_code(orig, "indeterminate")

    # ── Walk the rollup subtree (BFS, bounded) ─────────────────────────────
    candidates: list[Task] = []
    conflict_fix_evidence = bool((ci_loop or {}).get("conflict_fix_spawned")) or bool(
        ((orig.context or {}).get("review_loop") or {}).get("conflict_fix_spawned")
    )
    if merge_task is not None and (merge_task.first_payload or {}).get(
        "fix_reason"
    ) == "merge_conflict":
        conflict_fix_evidence = True

    visited: set = {orig.id}
    queue: list = [orig.id]
    while queue and len(visited) < _MAX_DESCENDANTS:
        rows = (
            (
                await session.execute(
                    select(Task).where(
                        Task.parent_task_id.in_(queue),
                        Task.project_id == orig.project_id,
                    )
                )
            )
            .scalars()
            .all()
        )
        next_queue: list = []
        for row in rows:
            if row.id in visited:
                continue
            visited.add(row.id)
            next_queue.append(row.id)
            payload = row.first_payload or {}
            if payload.get("fix_reason") == "merge_conflict" and _pr_numbers_match(
                payload.get("pr_number"), pr_number
            ):
                # A conflict-fix child ran against THIS rollup PR — the
                # incident signature that authorizes the hard verdict.
                conflict_fix_evidence = True
                continue
            ctx = row.context or {}
            if has_merge_unblock_signal(row) and _child_is_deliverable_candidate(ctx):
                candidates.append(row)
        queue = next_queue

    if not candidates:
        return _stamp_reason_code(orig, "intact")

    # ── Resolve the authoritative rollup diff (stamped first, 1 live call) ─
    rollup_files, credentials = await _resolve_rollup_changed_files(
        orig,
        session,
        pr_number=pr_number,
        credentials=github_credentials,
        prefer_live=prefer_live_rollup_files,
    )
    if rollup_files is None:
        return _stamp_reason_code(orig, "indeterminate")

    # ── Per-child survival check off stamped metadata ──────────────────────
    dropped: list[dict] = []
    live_compare_budget = _MAX_LIVE_CHILD_COMPARES
    for child in candidates:
        files = _verified_own_changed_files(child.context or {})
        if files is None and live_compare_budget > 0 and credentials is not None:
            # Gap-fill with the child's exact PR commit range (the same helper
            # rollup propagation uses); it stamps the child context for reuse.
            live_compare_budget -= 1
            try:
                from app.acceptance_criteria import stamp_rollup_child_commit_range

                if await stamp_rollup_child_commit_range(
                    child,
                    gh_token=credentials[0],
                    owner=credentials[1],
                    repo=credentials[2],
                ):
                    session.add(child)
                    files = _verified_own_changed_files(child.context or {})
            except Exception:
                logger.debug(
                    "[ROLLUP_DELIVERABLE] child %s commit-range gap-fill failed",
                    getattr(child, "id", None),
                    exc_info=True,
                )
        if not files:
            continue  # unverifiable or empty diff — never a block signal
        if rollup_files.isdisjoint(files):
            dropped.append(
                {
                    "task_id": str(child.id),
                    "pr_number": _child_pr_number(child),
                    "title": (child.title or "")[:120],
                    "missing_files": files[:_MISSING_FILES_SAMPLE],
                    "file_count": len(files),
                }
            )

    if not dropped:
        return _stamp_reason_code(orig, "intact")

    def _name(entry: dict) -> str:
        pr_part = f"PR #{entry['pr_number']}" if entry["pr_number"] else "PR unknown"
        return (
            f"{pr_part} (task {entry['task_id'][:8]}, {entry['title']!r}: "
            f"0/{entry['file_count']} changed files present)"
        )

    named = "; ".join(_name(e) for e in dropped)
    if conflict_fix_evidence:
        reason = (
            f"Rollup PR #{pr_number} merged but dropped child deliverable(s): "
            f"{named}. A conflict-fix intervened on this PR and none of the "
            f"child's changed files survived into the merged rollup diff — "
            f"likely wholesale conflict resolution toward the base branch. "
            f"The dropped work must be re-landed ({source})."
        )
        orig.context = {
            **(orig.context or {}),
            DROPPED_DELIVERABLES_KEY: dropped,
            DROPPED_DELIVERABLES_REASON_KEY: reason,
        }
        logger.error(
            "[ROLLUP_DELIVERABLE] %s task=%s", reason, getattr(orig, "id", None)
        )
        if raise_alert:
            # The PRE-MERGE gate passes ``raise_alert=False``: it runs on every
            # merge sweep while the hold stands, and one operator alert per
            # sweep is alert spam, not signal. The alert is raised once, by the
            # terminal post-merge path.
            try:
                from app.alert_dispatch import _maybe_create_failure_alert

                await _maybe_create_failure_alert(orig, session, reason)
            except Exception:
                logger.warning(
                    "[ROLLUP_DELIVERABLE] dropped-deliverable alert failed for %s",
                    getattr(orig, "id", None),
                    exc_info=True,
                )
        return _stamp_reason_code(orig, "dropped")

    # No conflict-fix on record: zero overlap is ambiguous (the base branch may
    # have legitimately absorbed identical content, hiding the files from the
    # PR diff while the deliverable IS in the merged result). Loud receipt, no
    # block — false-blocking healthy rollups is also a platform defect.
    warning = (
        f"Rollup PR #{pr_number} merged with child deliverable(s) absent from "
        f"its diff: {named}. No conflict-fix intervened on this PR, so this "
        f"may be legitimate supersession — completing, but verify the child "
        f"work is present on the target branch ({source})."
    )
    orig.context = {**(orig.context or {}), DELIVERABLE_WARNING_KEY: warning}
    logger.error(
        "[ROLLUP_DELIVERABLE] %s task=%s", warning, getattr(orig, "id", None)
    )
    return _stamp_reason_code(orig, "warned")


# ═══════════════════════════════════════════════════════════════════════════
# PRE-MERGE GATE
# ═══════════════════════════════════════════════════════════════════════════
# Everything above this line is post-hoc: it discovers the loss AFTER the rollup
# PR merged, when the destroyed diff is already on the trunk and the only
# remaining action is to fail the parent. Production case 2026-08-24 — rollup PR
# #17804 merged having erased 12/12 files of child PR #17707; two days later the
# work was still absent from automain and the only consequence was research task
# 78ad7812 marked FAILED/CODE_ERROR.
#
# The same comparison is decidable BEFORE the merge: an open PR's changed-file
# set is one ``get_pr_files`` call away. Running it at the merge boundary turns
# an unrecoverable loss into a recoverable hold — the rollup PR stays open, the
# children's work stays intact on the staging branch, and the conflict fix gets
# another attempt that is told, by exact filename, what it must not delete.


class PremergeDeliverableVerdict:
    """Whether a rollup merge may proceed, and the reason code if not.

    Attribute-style (not a plain tuple) so a caller cannot silently swap the
    boolean and the reason, and so new fields never break unpacking.
    """

    __slots__ = ("allowed", "dropped", "reason", "reason_code")

    def __init__(
        self,
        allowed: bool,
        reason_code: str,
        reason: str | None = None,
        dropped: list[dict] | None = None,
    ) -> None:
        assert reason_code in _ALL_DELIVERABLE_REASON_CODES, reason_code
        self.allowed = allowed
        self.reason_code = reason_code
        self.reason = reason
        self.dropped = dropped or []

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (
            f"PremergeDeliverableVerdict(allowed={self.allowed!r}, "
            f"reason_code={self.reason_code!r})"
        )


def _premerge_hold_count(ctx: dict) -> int:
    try:
        return max(int(ctx.get(PREMERGE_HOLD_COUNT_KEY) or 0), 0)
    except (TypeError, ValueError):
        return 0


def _task_owns_rollup_pr(task: "Task", pr_number) -> bool:
    """Is *task* the rollup parent whose own rollup PR is ``pr_number``?

    Keyed structurally off the parent's durable rollup identity rather than off
    the call site, so no merge path can be added later that quietly skips the
    gate. A non-rollup PR merge answers False and the gate is a dict lookup.
    """
    ctx = task.context or {}
    return _pr_numbers_match(ctx.get("rollup_pr_number"), pr_number)


def build_must_preserve_prompt_block(task: "Task") -> str:
    """Conflict-fix prompt text naming the deliverables that must survive.

    Empty string when the parent carries no dropped-deliverable receipt, so the
    caller can concatenate unconditionally. The rollup conflict-fix prompt
    otherwise tells the agent the target branch is the source of truth and to
    re-apply only "changes that serve the rollup goal" — and the rollup goal is a
    500-character blurb that names none of the children's files. That instruction
    is what erased PR #17707: every one of its 12 files was, to the agent,
    unexplained content the base branch did not have.
    """
    entries = (task.context or {}).get(MUST_PRESERVE_KEY)
    if not isinstance(entries, list) or not entries:
        return ""
    lines: list[str] = [
        "",
        "",
        "MUST-PRESERVE — a previous conflict resolution on this PR DELETED the "
        "work below.",
        "These files come from child PRs that were reviewed and merged into "
        "this rollup's",
        "staging branch. They are the rollup's payload, not stray content: the "
        "rollup goal",
        "above does not describe them and you must NOT drop them for that "
        "reason.",
        "",
    ]
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        pr_number = entry.get("pr_number")
        label = f"PR #{pr_number}" if pr_number else "child PR (number unknown)"
        title = str(entry.get("title") or "").strip()
        lines.append(f"  {label}{f' — {title}' if title else ''}")
        lines.extend(f"    - {path}" for path in entry.get("missing_files") or [])
        count = entry.get("file_count")
        shown = len(entry.get("missing_files") or [])
        if isinstance(count, int) and count > shown:
            lines.append(f"    ... and {count - shown} more file(s) from this PR")
    lines.extend(
        [
            "",
            "After resolving, verify with git diff --name-only "
            "origin/<target>...HEAD:",
            "every path listed above must still appear. If one does not, your "
            "resolution",
            "reverted it and the rollup will be held again.",
        ]
    )
    return "\n".join(lines)


async def _resolve_rollup_parent(
    task: "Task", session: AsyncSession, pr_number, ci_loop: dict | None
) -> "Task | None":
    """Return the rollup PARENT that owns *pr_number*, or None.

    Two shapes reach the merge choke point: the rollup parent drives its own
    review loop (``task`` IS the parent), or a separate CI/review row drives it
    and names the parent at ``review_loop.original_task_id``. Resolving both
    here is why the gate cannot be keyed off the call site — the ci_completion
    path passes the CI row, and keying off ``task`` alone would silently no-op
    on exactly the path that merged PR #17804.
    """
    if _task_owns_rollup_pr(task, pr_number):
        return task
    orig_id = (ci_loop or {}).get("original_task_id")
    if not orig_id or str(orig_id) == str(task.id):
        return None
    try:
        parent = await session.get(Task, uuid.UUID(str(orig_id)))
    except (TypeError, ValueError):
        return None
    if parent is not None and _task_owns_rollup_pr(parent, pr_number):
        return parent
    return None


def _restoration_fix_prompt(
    parent: "Task", *, pr_number, branch: str | None, base_branch: str | None
) -> str:
    """The prompt for the fix task that puts a dropped deliverable back."""
    return (
        f"RESTORE DROPPED DELIVERABLES on rollup PR #{pr_number} "
        f"({branch or 'rollup branch'} -> {base_branch or 'target branch'}).\n\n"
        f"The merge of this rollup PR has been HELD. A previous conflict "
        f"resolution on this branch resolved wholesale toward "
        f"{base_branch or 'the target branch'} and erased every changed file of "
        f"one or more child PRs that had already been reviewed and merged into "
        f"this rollup's staging branch. Their work is NOT on the target branch "
        f"and it is NOT on this PR — it exists only on the child PRs' own "
        f"branches and in their merge commits.\n\n"
        f"Your job is to put that work back on this branch, adapted to "
        f"{base_branch or 'the target branch'}'s current structure, and nothing "
        f"else. Do not revert unrelated changes, do not re-resolve files that "
        f"are already correct.\n\n"
        f"Suggested approach for each dropped child PR listed below:\n"
        f"1. git fetch origin\n"
        f"2. Read the child PR's diff: gh pr diff <number>\n"
        f"3. Re-apply that diff on top of the current branch. If "
        f"{base_branch or 'the target branch'} refactored or moved the code the "
        f"child touched, adapt the change to the new structure — keep the "
        f"BEHAVIOUR, not the exact lines.\n"
        f"4. Verify with: git diff --name-only "
        f"origin/{base_branch or 'main'}...HEAD — every file listed below must "
        f"appear.\n"
        f"5. Run the tests covering the restored files, then push."
        f"{build_must_preserve_prompt_block(parent)}"
    )


async def _spawn_deliverable_restoration_fix(
    parent: "Task",
    session: AsyncSession,
    *,
    pr_number,
    branch: str | None,
    base_branch: str | None,
) -> str | None:
    """Spawn the fix task that restores the dropped child deliverables.

    Best-effort by design: the HOLD is the safety property and must stand even
    if the spawn is suppressed (iteration cap, in-flight sibling, project
    paused). Returns the spawned task id, or None.

    This is the "recovery" half. Holding a merge without spawning anything to
    repair it would only postpone the same loss to the hold budget's end, which
    is a worse outcome than today's post-hoc failure because it also burns two
    merge cycles.
    """
    try:
        from app.review_pipeline import _spawn_pipeline_task

        fix_task, _created = await _spawn_pipeline_task(
            parent,
            "fix",
            session,
            int(pr_number),
            branch or "",
            base_branch or "",
            fix_prompt=_restoration_fix_prompt(
                parent, pr_number=pr_number, branch=branch, base_branch=base_branch
            ),
            fix_reason="rollup_deliverable_restoration",
            original_task_id=str(parent.id),
        )
    except Exception:
        logger.warning(
            "[ROLLUP_DELIVERABLE] restoration fix spawn failed for rollup PR "
            "#%s on task %s — the merge stays held regardless",
            pr_number,
            getattr(parent, "id", None),
            exc_info=True,
        )
        return None
    return str(fix_task.id) if fix_task is not None else None


async def enforce_premerge_rollup_deliverable_guard(
    task: "Task",
    session: AsyncSession,
    *,
    pr_number,
    gh_token: str | None,
    owner: str | None,
    repo: str | None,
    ci_loop: dict | None = None,
    branch: str | None = None,
    base_branch: str | None = None,
) -> PremergeDeliverableVerdict:
    """Decide whether a rollup PR may merge without destroying a child's work.

    Called from the single structural merge choke point
    (:func:`app.review_merge_authority.attempt_merge_with_authority`) before any
    lock is taken, so every merge path — autonomous rollup, CI-driven, operator
    approval — is covered by construction rather than by remembering to call it.

    Fail-OPEN by contract. Only a positively verified drop
    (``verify_rollup_child_deliverables`` returning ``"dropped"``: zero overlap
    between a merged child's GitHub-verified files and the live rollup diff, WITH
    a conflict-fix intervention on record) holds the merge. Every other
    condition — not a rollup PR, indeterminate, ambiguous, guard crash — allows
    it. A gate that false-blocks healthy rollups is itself a platform defect.

    On a hold the gate does three things: stamps the durable must-preserve
    receipt naming the child PRs and files, spawns the restoration fix task that
    consumes that receipt, and returns ``allowed=False``.

    The hold is bounded by :data:`PREMERGE_HOLD_CAP`. Once the budget is spent
    the gate ALLOWS the merge and the historical post-merge guard fails the
    parent with the full receipt — the engine decides and degrades instead of
    wedging a mergeable PR forever.
    """
    parent = await _resolve_rollup_parent(task, session, pr_number, ci_loop)
    if parent is None:
        return PremergeDeliverableVerdict(True, RC_NOT_A_ROLLUP_MERGE)

    credentials = None
    if all(isinstance(v, str) and v.strip() for v in (gh_token, owner, repo)):
        credentials = (gh_token, owner, repo)

    try:
        project = None
        if parent.project_id is not None:
            project = await session.get(Project, parent.project_id)
        verdict = await verify_rollup_child_deliverables(
            parent,
            project,
            session,
            pr_number=pr_number,
            github_credentials=credentials,
            ci_loop=ci_loop or (parent.context or {}).get("review_loop") or {},
            merge_task=None if parent is task else task,
            source="premerge_merge_authority",
            raise_alert=False,
            prefer_live_rollup_files=True,
        )
    except Exception:
        logger.warning(
            "[ROLLUP_DELIVERABLE] pre-merge guard errored for task %s — "
            "allowing the merge (fail-open)",
            getattr(parent, "id", None),
            exc_info=True,
        )
        return PremergeDeliverableVerdict(True, RC_INDETERMINATE)

    if verdict != "dropped":
        return PremergeDeliverableVerdict(
            True, _VERDICT_REASON_CODES.get(verdict, RC_INDETERMINATE)
        )

    ctx = dict(parent.context or {})
    dropped = ctx.get(DROPPED_DELIVERABLES_KEY) or []
    holds = _premerge_hold_count(ctx)
    if holds >= PREMERGE_HOLD_CAP:
        ctx[DELIVERABLE_REASON_CODE_KEY] = RC_PREMERGE_HOLD_CAP_EXHAUSTED
        parent.context = ctx
        flag_modified(parent, "context")
        session.add(parent)
        logger.error(
            "[ROLLUP_DELIVERABLE] pre-merge hold budget (%s) exhausted for "
            "rollup PR #%s on task %s — allowing the merge; the post-merge "
            "guard will fail the parent with the receipt",
            PREMERGE_HOLD_CAP,
            pr_number,
            getattr(parent, "id", None),
        )
        return PremergeDeliverableVerdict(
            True, RC_PREMERGE_HOLD_CAP_EXHAUSTED, dropped=list(dropped)
        )

    named = "; ".join(
        f"PR #{e.get('pr_number')}" if e.get("pr_number") else "PR unknown"
        for e in dropped
        if isinstance(e, dict)
    )
    reason = (
        f"Rollup PR #{pr_number} merge HELD: a conflict-fix resolution on this "
        f"PR erased every changed file of merged child deliverable(s) {named}. "
        f"Merging would destroy reviewed work. Spawning a restoration fix "
        f"(hold {holds + 1}/{PREMERGE_HOLD_CAP})."
    )
    ctx[PREMERGE_HOLD_COUNT_KEY] = holds + 1
    ctx[MUST_PRESERVE_KEY] = list(dropped)
    ctx[DELIVERABLE_REASON_CODE_KEY] = RC_PREMERGE_HOLD
    parent.context = ctx
    flag_modified(parent, "context")
    parent.latest_status_message = reason[:500]
    session.add(parent)
    logger.error(
        "[ROLLUP_DELIVERABLE] %s task=%s", reason, getattr(parent, "id", None)
    )
    # Receipt first, spawn second: the fix prompt is BUILT from the receipt.
    spawned = await _spawn_deliverable_restoration_fix(
        parent, session, pr_number=pr_number, branch=branch, base_branch=base_branch
    )
    if spawned:
        ctx = dict(parent.context or {})
        ctx[RESTORATION_FIX_TASK_KEY] = spawned
        parent.context = ctx
        flag_modified(parent, "context")
        session.add(parent)
    return PremergeDeliverableVerdict(
        False, RC_PREMERGE_HOLD, reason, dropped=list(dropped)
    )
