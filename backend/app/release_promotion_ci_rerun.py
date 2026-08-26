"""Bounded CI rerun gate for a RED ``automain -> main`` promotion PR.

Why this module exists
----------------------
An ordinary task PR that goes red gets a bounded ``rerun-failed-jobs`` flake
gate before the pipeline burns a fix cycle (``app.ci_completion``,
``_CI_RERUN_BUDGET``).  The promotion PR -- whose ``pull_request``-shaped run is
the *only* promotion-equivalent CI evidence the system recognises
(``app.release_readiness.compute_promotion_ci_green_gate``) -- got zero.  The
reconciler's red branch closed the PR and deleted ``release-v{N}`` on the FIRST
red observation, so by the time ``_harvest_promotion_ci_failures`` ran there was
nothing left to re-run (that function says so in its own comment).  One flaky
shard therefore destroyed the whole attempt and required a human-visible re-cut.

What this module does NOT do
----------------------------
It never merges, never marks a check green, never fabricates a verdict, and
never lowers a required check.  A red promotion PR stays unmerged: the caller
only proceeds to merge when a *fresh* read classifies ``_CI_GREEN``.  The single
behaviour change is that the attempt is HELD (PR open, branch intact, version
still ``promotion_pending``) instead of destroyed, so a re-run -- or a human
pushing a fix -- has something to act on.

Bounded escape (no forever-wedge)
---------------------------------
Three independent bounds, all of which end in today's terminal teardown:

* ``promotion_ci_rerun_budget`` (default 1) reruns per promotion attempt.  Once
  spent, the next red sweep falls straight through to the existing
  ``required_check_failed`` path.
* ``promotion_ci_rerun_observe_seconds`` (default 20 min) caps how long a single
  issued rerun may suppress teardown while GitHub republishes its verdict.
* The pre-existing 24h ``promotion_pending`` wall-clock deadline
  (``_PROMOTION_PENDING_TIMEOUT_SECONDS_DEFAULT``) is untouched and still
  reaps the waiter regardless of what this gate decides.

Every hold writes a ``check_snapshot`` carrying a ``reason`` and a human-readable
``detail`` naming the failing checks, the budget consumed, and what releases the
hold.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app import release_version
from app.ci_resolution import _CI_RED
from app.release_promotion_pending import (
    _increment_pending_transient_red_count,
    _managed_check_wait_snapshot,
    _persist_managed_check_wait_snapshot,
)
from app.shared_state import runtime_config as _runtime_config
from app.time_utils import _coerce_naive_utc, _utcnow

_MODULE_SIZE_GOVERNANCE_CAP: int = 520

logger = logging.getLogger(__name__)

# One rerun per promotion attempt, mirroring app.ci_completion._CI_RERUN_BUDGET
# for ordinary task PRs.  Override via runtime_config["promotion_ci_rerun_budget"].
# Unlike the transient-red sweep cap, an explicit 0 IS honoured here: 0 restores
# the pre-fix behaviour (tear down on the first genuine red), which is the
# fail-SAFE direction, so a deliberate disable must not be silently ignored.
# Only a negative / unparseable value falls back to the default.
_PROMOTION_CI_RERUN_BUDGET_DEFAULT = 1
# How long a single issued rerun may suppress teardown.  This window is required
# because GitHub keeps serving the just-superseded ``completed/failure`` verdict
# for a short period after ``rerun-failed-jobs`` is accepted (and the check-runs
# cache holds it for up to 30s), so without it the very next sweep would read the
# stale red and tear the attempt down anyway -- making the rerun pointless.
# A non-positive / unparseable override falls back to the default: a zero window
# would spend the budget and then discard the result, which is never intended.
_PROMOTION_CI_RERUN_OBSERVE_SECONDS_DEFAULT = 20 * 60

# Conclusions that block a promotion merge.  ``cancelled``/``timed_out`` are
# transient (see _check_result_has_genuine_failure) but still block, so they are
# reported to operators as failing check names.
_BLOCKING_CHECK_CONCLUSIONS = frozenset(
    {
        "failure",
        "cancelled",
        "timed_out",
        "action_required",
    }
)


def blocking_promotion_check_names(check_result: dict | None) -> list[str]:
    """Names of the individual checks whose conclusion blocks the merge."""

    details = (check_result or {}).get("details")
    if not isinstance(details, list):
        return []
    return [
        d.get("name", "unknown")
        for d in details
        if isinstance(d, dict) and d.get("conclusion") in _BLOCKING_CHECK_CONCLUSIONS
    ]


def _promotion_ci_rerun_budget() -> int:
    """Resolve the per-attempt promotion CI rerun budget (0 disables the gate)."""

    raw = _runtime_config.get("promotion_ci_rerun_budget")
    if raw is None:
        return _PROMOTION_CI_RERUN_BUDGET_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return _PROMOTION_CI_RERUN_BUDGET_DEFAULT
    if value < 0:
        return _PROMOTION_CI_RERUN_BUDGET_DEFAULT
    return value


def _promotion_ci_rerun_observe_seconds() -> float:
    """Resolve how long one issued rerun may suppress terminal teardown."""

    raw = _runtime_config.get("promotion_ci_rerun_observe_seconds")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return _PROMOTION_CI_RERUN_OBSERVE_SECONDS_DEFAULT
    if value <= 0:
        return _PROMOTION_CI_RERUN_OBSERVE_SECONDS_DEFAULT
    return value


def _parse_rerun_at(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return _coerce_naive_utc(datetime.fromisoformat(raw))
    except (TypeError, ValueError):
        return None


async def _read_promotion_ci_rerun_state(
    session,
    version_id: uuid.UUID,
    *,
    pr_number: int,
) -> dict:
    """Read this PR waiter's recorded rerun ledger.

    Returns ``{"count": int, "at": datetime | None, "head_sha": str | None}``.
    Any unexpected shape reads as "no reruns recorded", which is the
    conservative answer: it can only ever cause one extra rerun, never an
    unbounded loop (the durable write in :func:`_record_promotion_ci_rerun` is
    what actually authorises a hold).
    """

    empty = {"count": 0, "at": None, "head_sha": None}
    try:
        row: release_version.ReleaseVersion | None = (
            (
                await session.execute(
                    select(release_version.ReleaseVersion)
                    .where(release_version.ReleaseVersion.id == version_id)
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            return empty
        pending = (row.last_qualification or {}).get("promotion_pending")
        if not isinstance(pending, dict) or pending.get("pr_number") != pr_number:
            return empty
        try:
            count = int(pending.get("ci_rerun_count") or 0)
        except (TypeError, ValueError):
            count = 0
        head_sha = pending.get("ci_rerun_head_sha")
        return {
            "count": max(0, count),
            "at": _parse_rerun_at(pending.get("ci_rerun_at")),
            "head_sha": head_sha if isinstance(head_sha, str) else None,
        }
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] promotion_ci_rerun: failed to read rerun state "
            "for version %s",
            version_id,
        )
        return empty


async def _record_promotion_ci_rerun(
    session,
    version_id: uuid.UUID,
    *,
    pr_number: int,
    head_sha: str | None,
    run_ids: list[int],
    at: datetime,
) -> int:
    """Durably record one consumed rerun; return the new count (0 on failure).

    A 0 return means the ledger could NOT be written, and the caller must refuse
    to hold -- holding without a durable counter is exactly how an unbounded
    rerun loop would be created.
    """

    try:
        session.expire_all()
        locked: release_version.ReleaseVersion | None = (
            (
                await session.execute(
                    select(release_version.ReleaseVersion)
                    .where(release_version.ReleaseVersion.id == version_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            )
            .scalars()
            .first()
        )
        if locked is None or locked.status != "open":
            return 0
        qualification = dict(locked.last_qualification or {})
        if qualification.get("status") != "pending":
            return 0
        pending = qualification.get("promotion_pending")
        if not isinstance(pending, dict) or pending.get("pr_number") != pr_number:
            return 0
        updated_pending = dict(pending)
        try:
            prior = int(updated_pending.get("ci_rerun_count") or 0)
        except (TypeError, ValueError):
            prior = 0
        new_count = max(0, prior) + 1
        updated_pending["ci_rerun_count"] = new_count
        updated_pending["ci_rerun_at"] = at.isoformat()
        updated_pending["ci_rerun_head_sha"] = head_sha
        updated_pending["ci_rerun_run_ids"] = list(run_ids[:10])
        qualification["promotion_pending"] = updated_pending
        locked.last_qualification = qualification
        flag_modified(locked, "last_qualification")
        session.add(locked)
        await session.commit()
        return new_count
    except Exception:
        logger.exception(
            "[RELEASE_QUALIFIER] promotion_ci_rerun: failed to persist rerun "
            "ledger for version %s",
            version_id,
        )
        try:
            await session.rollback()
        except Exception:
            logger.exception(
                "[RELEASE_QUALIFIER] promotion_ci_rerun: rollback failed for "
                "version %s",
                version_id,
            )
        return 0


async def _snapshot_rerun_hold(
    session,
    version_id: uuid.UUID,
    *,
    pr_number: int,
    head_sha: str | None,
    check_result: dict,
    reason: str,
    detail: str,
) -> None:
    """Persist the operator-visible WHY for a rerun hold (best effort)."""

    await _persist_managed_check_wait_snapshot(
        session,
        version_id,
        pr_number=pr_number,
        ci_state=_CI_RED,
        snapshot=_managed_check_wait_snapshot(
            ci_state=_CI_RED,
            classified_at=_utcnow(),
            head_sha=head_sha,
            check_result=check_result,
            reason=reason,
            detail=detail,
        ),
    )


async def _rerun_red_promotion_ci(
    session,
    version_id: uuid.UUID,
    version_number: int,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr_head_sha: str | None,
    check_result: dict,
    failing_names: list[str],
) -> bool:
    """Return True when the red attempt is HELD for a bounded CI rerun."""

    budget = _promotion_ci_rerun_budget()
    if budget <= 0:
        return False

    state = await _read_promotion_ci_rerun_state(
        session, version_id, pr_number=pr_number
    )
    issued_at = state["at"]
    observe_seconds = _promotion_ci_rerun_observe_seconds()
    now = _utcnow()
    if (
        issued_at is not None
        and state["head_sha"] == pr_head_sha
        and 0 <= (now - issued_at).total_seconds() < observe_seconds
    ):
        # A rerun is in flight for this exact head.  GitHub can still be serving
        # the superseded failure, so hold rather than act on a stale verdict.
        # Nothing merges here: the merge path requires a fresh _CI_GREEN read.
        logger.warning(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s for v%d "
            "is red while a CI rerun issued %.0fs ago is still settling "
            "(window %.0fs) -- holding PR and release branch",
            pr_number,
            version_number,
            (now - issued_at).total_seconds(),
            observe_seconds,
        )
        await _snapshot_rerun_hold(
            session,
            version_id,
            pr_number=pr_number,
            head_sha=pr_head_sha,
            check_result=check_result,
            reason="promotion_ci_rerun_awaiting",
            detail=(
                f"red: {', '.join(failing_names[:3]) or 'unknown'}; awaiting "
                f"rerun {state['count']}/{budget} verdict - releases on a green "
                f"re-read or after {observe_seconds:.0f}s"
            ),
        )
        return True

    if state["count"] >= budget:
        logger.warning(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s for v%d "
            "red with rerun budget exhausted (%d/%d) -- proceeding to "
            "required_check_failed",
            pr_number,
            version_number,
            state["count"],
            budget,
        )
        return False

    run_ids = [
        r
        for r in (check_result.get("failed_run_ids") or [])
        if isinstance(r, int) and not isinstance(r, bool)
    ]
    if not run_ids:
        logger.warning(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s for v%d "
            "red but no failed workflow run ids were reported -- cannot rerun, "
            "proceeding to required_check_failed",
            pr_number,
            version_number,
        )
        return False

    from app.github_checks import invalidate_check_runs_cache, rerun_failed_jobs

    reran: list[int] = []
    for run_id in run_ids:
        if await rerun_failed_jobs(token, owner, repo, run_id):
            reran.append(run_id)
    if not reran:
        logger.warning(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s for v%d "
            "red and GitHub accepted no rerun request -- proceeding to "
            "required_check_failed",
            pr_number,
            version_number,
        )
        return False

    new_count = await _record_promotion_ci_rerun(
        session,
        version_id,
        pr_number=pr_number,
        head_sha=pr_head_sha,
        run_ids=reran,
        at=now,
    )
    if not new_count:
        # Ledger write failed -- refuse to hold rather than rerun unbounded.
        logger.warning(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s for v%d "
            "re-ran CI but the rerun ledger could not be persisted -- "
            "proceeding to required_check_failed rather than holding unbounded",
            pr_number,
            version_number,
        )
        return False

    if pr_head_sha:
        invalidate_check_runs_cache(owner, repo, pr_head_sha)
    logger.warning(
        "[RELEASE_QUALIFIER] reconcile managed_check_wait: PR #%s for v%d has "
        "red CI (%s) -- re-ran failed workflow runs %s (%d/%d) and is HOLDING "
        "the PR and release branch; merge still requires a green re-read",
        pr_number,
        version_number,
        failing_names,
        reran,
        new_count,
        budget,
    )
    await _snapshot_rerun_hold(
        session,
        version_id,
        pr_number=pr_number,
        head_sha=pr_head_sha,
        check_result=check_result,
        reason="promotion_ci_rerun_issued",
        detail=(
            f"red: {', '.join(failing_names[:3]) or 'unknown'}; re-ran "
            f"{len(reran)} run(s) {new_count}/{budget} - releases on a green "
            f"re-read, else tears down after {observe_seconds:.0f}s"
        ),
    )
    return True


async def hold_red_promotion_ci(
    session,
    version: release_version.ReleaseVersion,
    token: str,
    owner: str,
    repo: str,
    pr_number: int,
    pr_head_sha: str | None,
    check_result: dict,
    failing_names: list[str],
    *,
    genuine_failure: bool,
    max_transient_sweeps: int,
) -> bool:
    """Decide whether a RED promotion PR is held instead of torn down.

    Returns True when the caller must leave the attempt pending (PR open,
    ``release-v{N}`` intact).  Returns False when the caller proceeds with the
    unchanged terminal path: close the PR, delete the branch, record
    ``required_check_failed``, harvest the failures into a fix task.

    Red is still blocking in BOTH branches -- this function has no path that
    merges, marks a check passed, or records a promotion success.
    """

    # Snapshot before any helper commits: the persistence helpers expire the
    # session, and reading an expired attribute afterwards is a lazy-IO hazard.
    version_id = version.id
    version_number = version.version_number

    if not genuine_failure:
        # Only transient conclusions (cancelled / timed_out) -- do not tear down
        # the PR or release branch.  Increment the sweep counter and leave
        # pending until it hits the cap.
        new_count = await _increment_pending_transient_red_count(
            session,
            version_id,
            pr_number=pr_number,
        )
        if new_count < max_transient_sweeps:
            logger.warning(
                "[RELEASE_QUALIFIER] reconcile managed_check_wait: "
                "PR #%s for v%d has transient-only red CI "
                "(cancelled/timed_out, sweep %d/%d) -- leaving pending",
                pr_number,
                version_number,
                new_count,
                max_transient_sweeps,
            )
            return True
        logger.warning(
            "[RELEASE_QUALIFIER] reconcile managed_check_wait: "
            "PR #%s for v%d reached transient-red sweep cap (%d) "
            "-- escalating to required_check_failed",
            pr_number,
            version_number,
            max_transient_sweeps,
        )

    return await _rerun_red_promotion_ci(
        session,
        version_id,
        version_number,
        token,
        owner,
        repo,
        pr_number,
        pr_head_sha,
        check_result,
        failing_names,
    )
