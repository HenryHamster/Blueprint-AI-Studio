"""Post-commit / orphan / sidecar / bootstrap recovery sweeps.

Extracted from ``app.recovery_sweeps`` alongside ``app.recovery_evaluating`` to
keep each module below its size cap.  This module is now a thin re-export hub:

* ``app.recovery_post_commit_claim`` — atomic pending-recovery ownership helpers
* ``app.recovery_post_commit_pending`` — post-commit-pending sweep and the
  marker-less childless recovery guard
* ``app.recovery_post_commit_orphans`` — orphan-recovery sweep
  (``_spawn_review_for_recovered_task``, ``_recover_orphaned_tasks``)
* ``app.recovery_sidecar`` — stranded-sidecar sweep
* ``app.recovery_infrastructure`` — lane-baseline backfill + bootstrap recovery

Every public name is re-exported from ``app.recovery_sweeps`` (and in turn from
``app.services``) so existing call sites and test patch targets keep resolving
unchanged. Shared bounding infrastructure lives in ``app.services`` and is
referenced through the ``services`` module object so test patches against
``app.services.<name>`` (and ``app.recovery_sweeps.services.<name>``) still take
effect on the moved code.
"""

import logging

from app.recovery_sidecar import (  # noqa: F401
    _AWAITING_SIDECAR_KEYS,
    _SIDECAR_VERDICT_APPLICATION_PENDING_KEY,
    _SIDECAR_SWEEP_MAX_PER_CYCLE,
    _sweep_stranded_sidecar_sources,
)
from app.recovery_infrastructure import (  # noqa: F401
    _LANE_BASELINE_BACKFILL_BATCH_SIZE,
    _sweep_lane_baseline_backfill,
    _BOOTSTRAP_SWEEP_BATCH_SIZE,
    _bootstrap_spawn_lock_key,
    _try_bootstrap_spawn_advisory_lock,
    _sweep_bootstrap_recovery,
)
from app.recovery_post_commit_claim import (  # noqa: F401
    _claim_and_bump_total_attempts,
    _build_post_commit_abandoned_context,
)
from app.recovery_post_commit_pending import (  # noqa: F401
    POST_COMMIT_MAX_PENDING_AGE_S,
    _sweep_post_commit_pending,
    _CHILDLESS_EVALUATING_NO_MARKER_GRACE_S,
    _recover_childless_evaluating_without_marker,
)
from app.recovery_post_commit_orphans import (  # noqa: F401
    _spawn_review_for_recovered_task,
    _recover_orphaned_tasks,
)
from app.recovery_research_spawn_pending import (  # noqa: F401
    RESEARCH_SPAWN_RECOVERY_KEY,
    RESEARCH_SPAWN_RECOVERY_GRACE_S,
    _claim_markerless_research_spawn,
    _finish_markerless_research_spawn_claim,
    _sweep_markerless_research_spawn_pending,
)
from app.post_commit_errors import PostCommitLocked

logger = logging.getLogger(__name__)


# Substrings that mark a post-commit retry failure as TRANSIENT (the
# underlying cause is expected to clear on its own) rather than persistent.
# Transient failures must NOT count against the sweeper's 5-attempt cap —
# otherwise a multi-hour GitHub-quota outage or sustained row contention burns
# the whole budget and the sweep gives up (post_commit_failed) on a task that
# would have recovered, silently dropping the deferred side effects
# (review/fix spawn, rollup nudge). The cap exists to bound a genuinely
# non-transient defect (e.g. a malformed payload, a bug in a completion
# handler), so only persistent failures should increment it.
_POST_COMMIT_TRANSIENT_SUBSTRINGS = (
    "rate limit",
    "ratelimit",
    "secondary rate",
    "rate_limit",
    "429",
    "quota",
    "lock_timeout",
    "lock not available",
    "locknotavailable",
    "could not obtain lock",
    "held by peer",
)


def _post_commit_retry_is_transient(exc: BaseException) -> bool:
    """Classify a post-commit retry exception as transient (retriable without
    counting against the cap) vs persistent (counts toward giving up).

    Transient classes:
      - ``PostCommitLocked``: the task row is still held by a peer
        (the inline status pass or a concurrent post-commit). Pure contention —
        retrying next sweep is correct.
      - GitHub/LLM rate-limit or quota exhaustion (string-matched): clears once
        the quota window resets; counting it against the cap would abandon a
        task during a transient outage.
      - Row-lock contention (LockNotAvailableError / "lock_timeout"): same as
        PostCommitLocked but surfaced as a DBAPI error rather than the typed
        exception.

    NB: ``asyncio.TimeoutError`` (the ``_bounded`` outer ceiling) is treated as
    PERSISTENT and DOES count — a post-commit pass that wedges past its own 240s
    budget every time is the non-transient case the cap is meant to bound.
    """
    if isinstance(exc, PostCommitLocked):
        return True
    msg = f"{type(exc).__name__}: {exc}".lower()
    return any(s in msg for s in _POST_COMMIT_TRANSIENT_SUBSTRINGS)
