"""Leader-cycle recovery sweeps — thin re-export hub.

The recovery sweeps were originally extracted from ``app.services`` into this
module; they have since been split again across two submodules to keep each
file below its size cap, following the same ``services.py`` → submodule
re-export pattern:

* ``app.recovery_evaluating`` — the EVALUATING-timeout sweep
  (``_check_evaluating_timeouts``) and its chain-inspection / CI-backstop /
  spend-hold helpers.
* ``app.recovery_post_commit`` — the post-commit-pending, orphan-recovery,
  childless-EVALUATING, stranded-sidecar, lane-baseline-backfill, and
  bootstrap-recovery sweeps, plus the post-commit transient-retry classifier.

Every public name is re-exported here so existing ``from app.recovery_sweeps
import ...`` call sites and test patch targets (e.g.
``app.recovery_sweeps._check_evaluating_timeouts``,
``app.recovery_sweeps._sweep_bootstrap_recovery``) keep resolving against
``app.recovery_sweeps`` unchanged. ``app.services`` in turn re-exports them from
here.

``services`` is re-imported here so that test patches targeting
``app.recovery_sweeps.services.<name>`` (e.g. ``AsyncSessionLocal``) continue to
mutate the shared ``app.services`` module object that the moved sweep code reads
through its own ``services`` binding.
"""

from app import services  # noqa: F401

from app.recovery_evaluating import (  # noqa: F401
    _last_status_transition_at,
    _chain_has_active_pipeline_work,
    _approval_gate_chain_busy,
    _chain_has_recoverable_failed_work,
    _chain_has_unexhausted_optimistic_retry_work,
    _summarize_latest_review_for_chain,
    _QUEUED_PIPELINE_SUPERSEDE_AFTER_SECONDS,
    _ci_backstop_timeout_reason,
    _supersede_long_queued_pipeline_tasks,
    _attempt_ci_backstop_recovery,
    _verify_ci_before_backstop,
    _dispatch_spend_hold_notification,
    _emit_spend_hold_created_signals,
    _check_evaluating_timeouts,
)
from app.recovery_post_commit import (  # noqa: F401
    POST_COMMIT_MAX_PENDING_AGE_S,
    _POST_COMMIT_TRANSIENT_SUBSTRINGS,
    _post_commit_retry_is_transient,
    _sweep_post_commit_pending,
    _CHILDLESS_EVALUATING_NO_MARKER_GRACE_S,
    _recover_childless_evaluating_without_marker,
    RESEARCH_SPAWN_RECOVERY_KEY,
    RESEARCH_SPAWN_RECOVERY_GRACE_S,
    _claim_markerless_research_spawn,
    _finish_markerless_research_spawn_claim,
    _sweep_markerless_research_spawn_pending,
    _recover_orphaned_tasks,
    _AWAITING_SIDECAR_KEYS,
    _SIDECAR_VERDICT_APPLICATION_PENDING_KEY,
    _sweep_stranded_sidecar_sources,
    _LANE_BASELINE_BACKFILL_BATCH_SIZE,
    _sweep_lane_baseline_backfill,
    _BOOTSTRAP_SWEEP_BATCH_SIZE,
    _bootstrap_spawn_lock_key,
    _try_bootstrap_spawn_advisory_lock,
    _sweep_bootstrap_recovery,
)
