"""Shared runtime state used by both main.py and admin.py.

Extracted to its own module to avoid circular imports when admin
endpoints need to read or mutate autonomous-loop / runtime-config state.
"""

import os
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict

from slowapi import Limiter
from slowapi.util import get_remote_address
from sqlalchemy.orm.attributes import flag_modified

from app.failure_classification import INFRA_CLASSES as _DIRECTION_INFRA_CLASSES
from app.time_utils import _utcnow

logger = logging.getLogger(__name__)

# Canonical default for task scope when not explicitly set.
# Operational modules (orchestrator, review pipeline, research completion) use this.
# Analytics/tracking modules use "unknown" to distinguish missing data from a real scope.
DEFAULT_TASK_SCOPE = "small"

# Maps calibrated task scope to a baseline effort level used at spawn time.
# small work runs faster/cheaper; large work gets deeper decomposition/analysis.
# Used by research_completion and task_completion as a floor on top of
# finding/parent-specified effort via _effort_rank (finding wins when higher).
_SCOPE_EFFORT_MAP = {"small": "low", "medium": "medium", "large": "high"}


def scope_to_effort(scope: str | None) -> str:
    """Return the baseline effort for a calibrated scope.

    Unknown / missing scopes fall back to "medium" so callers can safely take
    the max against other effort signals without suppressing them.
    """
    return _SCOPE_EFFORT_MAP.get(scope or "", "medium")


# Maps a task type to its branch prefix. Must stay in sync with the agent's
# branch derivation (agent/src/index.ts, handlers/specialized.ts). When an
# orchestrator path needs to address an agent-pushed branch (review/fix spawn,
# operator merge), it must derive the name with this map — hardcoding
# "blueprint/task-{8hex}" silently broke review spawns for non-default task
# types (refactor → blueprint/refactor-{8hex}, etc.); see 2026-05-25 incident
# where PRs #5779/#5812/#5815/#5819 all stalled at review cap because the
# clone targeted blueprint/task-* against agents that had pushed
# blueprint/refactor-*.
TASK_TYPE_BRANCH_PREFIX: Dict[str, str] = {
    "create": "task",
    "qa": "task",
    "fix": "task",
    "refactor": "refactor",
    "docs": "docs",
    "deps": "deps",
    "migrate": "migrate",
}


# Default namespace (the "blueprint" segment of ``blueprint/{prefix}-{8hex}``).
# Per-project overrides come from project.task_branch_namespace (see
# get_project_task_branch_namespace below); a non-Blueprint install can ship
# its own namespace by setting build_config.task_branch_namespace.
TASK_BRANCH_NAMESPACE_DEFAULT = "blueprint"


def derive_task_branch(
    task_id: Any,
    task_type: str | None,
    namespace: str = TASK_BRANCH_NAMESPACE_DEFAULT,
) -> str:
    """Derive an agent work branch name (``{namespace}/{prefix}-{8hex}``)
    from the task type and id, matching the agent's branch derivation.

    ``namespace`` defaults to ``"blueprint"`` so call sites that pre-date the
    per-project namespace work unchanged for the self-repo. Callers that have
    a project in hand should resolve the namespace via
    :func:`get_project_task_branch_namespace` and pass it through.
    """
    prefix = TASK_TYPE_BRANCH_PREFIX.get(task_type or "", "task")
    return f"{namespace}/{prefix}-{str(task_id).replace('-', '')[:8]}"


def resolve_task_branch(task: Any) -> str:
    """Return the branch a task's agent actually pushed to.

    Precedence:
      1. ``task.context["review_loop"]["branch"]`` — set by review/fix spawn
      2. ``task.first_payload["branch"]`` — set when an agent claims a task
      3. ``task.context["branch"]`` — legacy fallback
      4. ``derive_task_branch(task.id, task_type)`` — last-resort synthesis

    Fix tasks reuse the parent's PR branch — synthesizing
    ``blueprint/task-{fix_id}`` from a fix child's own id yields a name no
    agent ever pushed, breaking downstream review respawns that try to clone
    it. The first three sources carry the real branch; derivation is only
    safe when none of them are present (a freshly-spawned create/refactor
    task with no payload-provided branch yet).
    """
    ctx = getattr(task, "context", None) or {}
    review_loop = ctx.get("review_loop") or {}
    branch = review_loop.get("branch")
    if branch:
        return branch
    payload = getattr(task, "first_payload", None) or {}
    branch = payload.get("branch")
    if branch:
        return branch
    branch = ctx.get("branch")
    if branch:
        return branch
    return derive_task_branch(getattr(task, "id", None), payload.get("type"))


# Rate limiter — shared between main.py and admin.py
# Only enforced in Cloud Run (K_REVISION set), disabled locally for dev/test
# Uses Redis for cross-instance coordination when REDIS_URL is set,
# falls back to per-instance in-memory storage otherwise.
_redis_url = os.getenv("REDIS_URL")
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=_redis_url if _redis_url else "memory://",
    enabled=bool(os.getenv("K_REVISION")),
)

# Lock for multi-key runtime config updates (prevents reading partial state between two assignments)
runtime_config_lock = asyncio.Lock()

# --- Runtime Config (adjustable via admin API) ---
runtime_config: Dict = {
    "max_concurrent_vms": int(os.getenv("MAX_CONCURRENT_VMS", "8")),
    # Per-task-type slot reservations for priority inversion prevention.
    # When the queue contains both reserved-type and non-reserved-type tasks
    # competing for the last slots, at least N slots are held for the listed
    # types so self-perpetuating review-fix cycles can't starve create/research.
    # Reservations only bite under queue pressure; unused reserved slots remain
    # available to any task type.
    "reserved_slots_for_types": {"create": 1, "research": 1},
    # Hard cap on the total number of concurrent review+fix tasks
    # (PROVISIONING + IN_PROGRESS) that may consume VM slots at once.
    # Review/fix chains are self-perpetuating (a review spawns a fix, which
    # spawns another review, up to MAX_REVIEW_FIX_ITERATIONS per PR), so
    # without a cap a few churning PRs can fill every slot and starve
    # research/create tasks. Default to 50% of max_concurrent_vms (4 of 8).
    # The orchestrator dispatch loop checks this pre-dispatch and defers
    # (keeps QUEUED) review/fix tasks above the budget — they run as soon
    # as a pipeline slot frees up. 0 disables the cap entirely.
    "max_pipeline_slots": int(os.getenv("MAX_PIPELINE_SLOTS", "4")),
    "max_auto_pause_duration_seconds": int(
        os.getenv("MAX_AUTO_PAUSE_DURATION_SECONDS", "7200")
    ),
    "auto_resume_grace_period_seconds": int(
        os.getenv("AUTO_RESUME_GRACE_PERIOD_SECONDS", "300")
    ),
    # Flap detection: N auto-resumes inside `flap_window_seconds` escalates
    # (ERROR alert, raised recovery bar, doubled grace period).
    "flap_window_seconds": int(os.getenv("FLAP_WINDOW_SECONDS", "1800")),
    "flap_threshold": int(os.getenv("FLAP_THRESHOLD", "3")),
    "max_retries": int(os.getenv("MAX_RETRIES", "3")),
    "max_provision_retries": int(os.getenv("MAX_PROVISION_RETRIES", "3")),
    "max_rollup_retries": int(os.getenv("MAX_ROLLUP_RETRIES", "5")),
    "queued_age_alert_hours": int(os.getenv("QUEUED_AGE_ALERT_HOURS", "2")),
    "retry_after_reset_alert_threshold": int(
        os.getenv("RETRY_AFTER_RESET_ALERT_THRESHOLD", "8")
    ),
    "max_autonomous_tasks_per_hour": 12,
    "max_global_autonomous_tasks_per_hour": 50,  # Cross-project cap (default 10x per-project)
    "autonomous_research_interval_minutes": 30,
    # Long-EVALUATING parents stop blocking new research on the same direction
    # once their pipeline-side merge cycle exceeds this threshold. Set above
    # the typical PR lifecycle (CI + review + fix + re-review + merge) so new
    # research only spawns when a parent is genuinely stuck, not just slow.
    "evaluating_pipeline_timeout_minutes": int(
        os.getenv("EVALUATING_PIPELINE_TIMEOUT_MINUTES", "120")
    ),
    # Hard kill for EVALUATING tasks waiting on CI/review verdict. Tuned above
    # a single review-fix iteration (5-15 min agent + queueing) so multi-pass
    # cycles don't trigger a premature kill of in-flight children.
    "ci_backstop_timeout_minutes": int(os.getenv("CI_BACKSTOP_TIMEOUT_MINUTES", "60")),
    # Orchestrator CI-poll resolution mode. True (default) routes through
    # ``ci_resolution.resolve_pr_ci_status`` — strict check-runs on branch HEAD,
    # PR-event merge SHA fallback when HEAD is bare, and a refusal to mark CI
    # green from the mere absence of checks.  False reverts to the legacy
    # ``get_check_runs_cached(treat_missing_as_success=True)`` lenient path; kept
    # as the operator-side rollback handle if the strict mode causes false
    # negatives in prod.
    "ci_resolution_strict_mode": os.getenv("CI_RESOLUTION_STRICT_MODE", "true").lower()
    not in ("0", "false", "no"),
    # Hard cap on ``ci_wait_started`` clock resets when GitHub responds with
    # "checks in progress" / "status check pending" to a merge attempt.  Each
    # reset bumps the CI-wait deadline by another full backstop window; without
    # a cap, a PR whose required checks never resolve can spin on this reset
    # path until the 6-hour parent timeout fires.  After the cap the resets stop
    # and the normal backstop runs out on schedule.
    "ci_in_progress_reset_cap": int(os.getenv("CI_IN_PROGRESS_RESET_CAP", "3")),
    # Hard EVALUATING-state backstop for parents not in CI-wait. The 6h legacy
    # default cascade-killed otherwise-recoverable children during operator-
    # intervention windows; bumped to 48h while silent-death stabilizes.
    "evaluating_fail_hours": int(os.getenv("EVALUATING_FAIL_HOURS", "48")),
    # Total research tasks (any non-terminal state, *including* EVALUATING)
    # allowed per project. Distinct from max_parallel_research_per_project,
    # which counts only QUEUED/PROVISIONING/IN_PROGRESS — that cap controls
    # active dispatch concurrency. This cap throttles pending-rollup
    # accumulation: each EVALUATING parent has children waiting to merge
    # into a research branch and then to main; piling up rollup PRs makes
    # each subsequent merge incrementally more conflict-prone.
    "max_total_research_per_project": int(
        os.getenv("MAX_TOTAL_RESEARCH_PER_PROJECT", "5")
    ),
    # Auto-remediation governors (residual CODE_ERROR fingerprint spawner).
    # Global cap on concurrent/queued auto-remediation tasks prevents queue
    # ballooning when many distinct fingerprints accumulate. Cooldown is
    # per-fingerprint and blocks re-spawn for the same fingerprint while a
    # prior remediation is in flight or recently resolved.
    "max_concurrent_auto_remediations": 2,
    "residual_remediation_cooldown_hours": 24,
    # Future-facing provider tier map used by multi-provider selection. v1 keeps
    # Anthropic as the only dispatch path but persists the structure now so v2
    # can enable provider-aware resolution without another config shape change.
    "provider_tier_model_map": {
        "anthropic": {
            "opus": "claude-opus-4-7",
            "sonnet": "claude-sonnet-4-6",
            "haiku": "claude-haiku-4-5-20251001",
        },
        "openai": {
            "opus": "gpt-5.4",
            "sonnet": "gpt-5.3-codex",
            "haiku": "gpt-5.4-mini",
        },
        "gemini": {
            "opus": "gemini-2.5-pro",
            "sonnet": "gemini-2.5-flash",
            "haiku": "gemini-2.5-flash-lite",
        },
    },
    # Task-type categories group related types so one routing entry covers
    # the whole group. Every task type listed here falls under the matching
    # category_routing block; unlisted types resolve via category_routing["_default"].
    "task_type_categories": {
        "strategic": ["strategic_review", "design_doc_update", "research", "plan"],
        "build": ["create", "refactor", "migrate"],
        "review": ["review", "qa", "security_audit", "perf", "debug", "fix"],
        "ops": ["docs", "deps", "smoketest"],
    },
    # Category routing — single source of truth for both "which providers is
    # this group allowed to use" and "at what effort level does it run". The
    # effort value indexes into provider_settings_by_effort below to pick the
    # concrete settings bag (including model).
    "category_routing": {
        "_default": {
            "providers": ["anthropic", "openai", "gemini"],
            "effort": "medium",
        },
        "strategic": {"providers": ["anthropic", "openai", "gemini"], "effort": "max"},
        "build": {"providers": ["anthropic", "openai", "gemini"], "effort": "medium"},
        "review": {"providers": ["anthropic", "openai", "gemini"], "effort": "high"},
        "ops": {"providers": ["anthropic", "openai", "gemini"], "effort": "low"},
    },
    # Sparse per-task-type routing overrides. Either field optional; the
    # other inherits from the task's category. Use for surgical cases like
    # "route debug tasks to openai at high effort regardless of category".
    "task_type_routing": {},
    # Optional per-task-type concrete model priority overrides. When present,
    # selection uses these ordered {provider, model} pairs before falling back
    # to provider preference + quality tier resolution.
    "task_type_model_preferences": {},
    # Provider selection mode — controls how the pool chooses across the
    # providers listed in category_routing / task_type_routing:
    #   "balanced"  — flatten all accounts across listed providers into one
    #                 pool; pick globally by (health_score, -active_tasks).
    #   "preferred" — strict ordering; try each provider in list order, only
    #                 fall through when the earlier provider has no capacity.
    #   "exclusive" — first listed provider only; never fall back cross-provider.
    "provider_selection_mode": "balanced",
    # DEPRECATED: legacy boolean superseded by provider_selection_mode. Kept
    # readable in GET/PATCH for backwards compat — True ≡ "preferred",
    # False ≡ "exclusive". If both keys are present, mode wins.
    "cross_provider_fallback_enabled": True,
    # Per-provider base settings that don't vary by effort (e.g. anthropic
    # betas). Rarely set — most settings belong in provider_settings_by_effort.
    "provider_settings_defaults": {
        "anthropic": {},
        "openai": {},
        "gemini": {},
    },
    # Per-provider effort matrix. Each cell is a settings bag (validated by
    # the provider's settings_schema) and typically pins a model + the
    # provider's effort knob. When a task resolves to (provider, effort), the
    # corresponding cell is merged on top of provider_settings_defaults.
    "provider_settings_by_effort": {
        "anthropic": {
            "low": {"model": "claude-haiku-4-5-20251001", "effort": "low"},
            "medium": {"model": "claude-sonnet-4-6", "effort": "medium"},
            "high": {"model": "claude-opus-4-7", "effort": "high"},
            "max": {"model": "claude-opus-4-7", "effort": "max"},
        },
        "openai": {
            # Verbosity is graded to task-type category, not capability:
            # build/ops (low/medium effort) produce code and tool calls, not
            # prose — terse output saves tokens without losing debug signal.
            # review (high) gets medium so review comments can explain.
            # strategic (max) gets high because the prose *is* the deliverable
            # (design docs, research, plans).
            "low": {
                "model": "gpt-5.4-mini",
                "reasoning_effort": "low",
                "verbosity": "low",
            },
            "medium": {
                "model": "gpt-5.4-mini",
                "reasoning_effort": "medium",
                "verbosity": "low",
            },
            "high": {
                "model": "gpt-5.4",
                "reasoning_effort": "high",
                "verbosity": "medium",
            },
            "max": {
                "model": "gpt-5.4",
                "reasoning_effort": "xhigh",
                "verbosity": "high",
            },
        },
        "gemini": {
            "low": {"model": "gemini-2.5-flash-lite", "thinking_budget": 1024},
            "medium": {"model": "gemini-2.5-flash", "thinking_budget": 4096},
            "high": {"model": "gemini-2.5-pro", "thinking_budget": 16384},
            "max": {"model": "gemini-2.5-pro", "thinking_budget": 32768},
        },
    },
    # Sparse per-(task_type, provider) surgical overrides. For cases that
    # don't fit the matrix — e.g. "when this task type routes to openai,
    # also force verbosity=low". Wins over the effort matrix.
    "task_type_provider_overrides": {},
    # Per-type extra instructions appended to the agent's base prompt
    # e.g. {"research": "Focus especially on performance bottlenecks.", "qa": "Use pytest only."}
    "agent_prompt_suffixes": {},
    # Periodic autonomous spawning for non-research task types (opt-in)
    "autonomous_periodic_tasks": {
        "security_audit": {"interval_hours": 168, "enabled": False},  # weekly
        "qa": {"interval_hours": 72, "enabled": False},  # every 3 days
        "deps": {"interval_hours": 168, "enabled": False},  # weekly
    },
    "autonomous_direction_cooldown_minutes": 240,
    "productive_direction_cooldown_minutes": 45,
    "direction_auto_recovery_hours": 48,
    # Non-merge churn guardrail (Rule 5 in _check_direction_health).
    # window_cycles: how many recent research cycles to evaluate (rolling window).
    # non_merge_threshold: minimum non-merging finished tasks to trigger the guardrail.
    # min_merge_rate: merge rate below this AND above threshold triggers the pause.
    # Conservative defaults prevent pausing productive high-throughput directions.
    "direction_churn_window_cycles": 5,
    "direction_churn_non_merge_threshold": 15,
    "direction_churn_min_merge_rate": 0.10,
    # Lifetime/cumulative burn backstop — catches slow-burn directions that stay
    # just under the rolling-window threshold every window but accumulate hundreds
    # of non-merging tasks over their lifetime. Thresholds are intentionally high
    # (order-of-hundreds) so healthy high-throughput directions are never paused.
    "direction_lifetime_non_merge_threshold": 150,
    "direction_lifetime_min_merge_rate": 0.10,
    # Outcome-progress (scaffolding) detector — ADVISORY, never auto-pauses.
    # Surfaces long-running directions that keep merging PRs at a healthy rate but
    # never advance their stated success_evidence (the "outcome-less scaffolding"
    # pattern). min_cycles honors the "caught within ~12 cycles" goal; merge_floor +
    # min_merge_rate exclude churn/burn directions (handled by the churn/lifetime
    # backstops); stall_run is the consecutive run of no-progress cycles required so
    # a single low-progress cycle cannot trip the flag; history_max caps the bounded
    # per-direction snapshot list stored on direction.outcome_progress_history.
    "direction_outcome_progress_min_cycles": 12,
    "direction_outcome_progress_merge_floor": 8,
    "direction_outcome_progress_min_merge_rate": 0.5,
    "direction_outcome_progress_stall_run": 6,
    "direction_outcome_progress_history_max": 20,
    "strategic_research_frequency": 6,  # Every Nth research cycle is a strategic vision review
    # Wall-clock backstop for strategic cycles: if it has been this long since the
    # last strategic_review/design_doc_update task was created (regardless of
    # outcome), force a strategic spawn. Decouples steering frequency from research
    # spawn rate so narrowing direction count does not throttle the agent's own
    # decision cadence. 0 disables the fallback. Cooldown still gates duplicates.
    "strategic_max_interval_minutes": 180,
    "architecture_review_frequency": 3,  # Every Nth strategic cycle includes architecture (design doc) review
    "max_active_directions": 10,
    "max_parallel_research_per_project": 6,
    "max_retained_images": 10,  # Agent VM image family pruning (keep N most recent; GCP per-project image quota is tight)
    "require_approval_gates": True,
    "daily_cost_ceiling_usd": 0,  # 0 = disabled; positive float enables budget alerts
    "spawn_confidence_threshold": 0.25,  # P(success) below this injects low_confidence_spawn=True into task context
    # During degraded operation only, tasks whose P(success) falls below this floor
    # are skipped this dispatch cycle (remain QUEUED) so scarce slots go to higher-
    # confidence work. Does not apply during healthy operation.
    "dispatch_confidence_floor": 0.10,
    # Fraction of low-confidence medium-scope findings that bypass the escalation
    # gate and run as medium tasks, breaking the self-reinforcing feedback loop
    # where no medium tasks run → medium pipeline rate stays low → all medium
    # findings escalate. Set to 0 to disable exploration.
    "medium_exploration_fraction": 0.2,
    # Auto-approve thresholds for trivially small diffs from create/refactor tasks.
    # When both limits are respected (and other safety gates pass — small-scope,
    # direction acceptance rate > 50%), the review task is skipped and the PR
    # proceeds directly to CI-gated merge. Set auto_approve_max_lines to 0 to
    # disable the fast-path entirely.
    "auto_approve_max_lines": 50,
    "auto_approve_max_files": 4,
    # Medium-scope auto-approve uses tighter caps than small. Set
    # auto_approve_medium_max_lines to 0 to disable medium auto-approve while
    # keeping the small-scope fast-path enabled.
    "auto_approve_medium_max_lines": 30,
    "auto_approve_medium_max_files": 2,
    # Cross-cutting safeguard gate: automatically spawns a security_audit task and
    # blocks merge (requires_approval) for PRs that span multiple top-level modules
    # or are large-scope. Thresholds are operator-adjustable.
    #   enabled:                False disables the gate entirely.
    #   medium_module_threshold: minimum number of top-level modules (backend/agent/
    #                            admin-ui) a medium-scope PR must span to be gated.
    #   overflow_line_threshold: a PR with scope_overflow_warned=True is also gated
    #                            when its diff exceeds this many lines.
    "cross_cutting_gate_enabled": True,
    "cross_cutting_gate_medium_module_threshold": 3,
    "cross_cutting_gate_overflow_line_threshold": 100,
    # How long a NEEDS_MANUAL_INTERVENTION regression case must observe its
    # underlying signal back below threshold before the dispatcher watchdog
    # auto-closes it as 'recovered_unattended'. 24h gives an unrelated PR or
    # platform fix time to land and the signal time to settle before we
    # reclaim the operator queue slot. Operator override via env var.
    "regression_recovered_unattended_window_hours": int(
        os.getenv("REGRESSION_RECOVERED_UNATTENDED_WINDOW_HOURS", "24")
    ),
    # TTL (days) for deferred findings carried forward by autonomous_research.
    # Findings older than this window are dropped rather than reprocessed, so
    # stale context doesn't keep surfacing work the system already declined to
    # spawn. Mirrors the hardcoded default in autonomous_research.py.
    "deferred_finding_ttl_days": 7,
    # Pipeline-wide scope merge-rate baselines used for cold-start scope calibration
    # when a direction/project has insufficient research_history. Default is empty
    # (feature disabled) — the prior defaults {"medium": 0.04, ...} were snapshotted
    # from an 8h window with an atypical merge-rate dip and permanently biased
    # cold-start toward large(decompose) even when medium was fine in aggregate.
    # Operators can populate this dict via runtime_config to re-enable cold-start
    # upgrades once there's enough real data to compute meaningful baselines.
    "pipeline_scope_baseline_rates": {},
    # Pipeline-wide merge rate per scope (e.g. {"medium": 0.04}). Acts as a
    # weakest-signal fallback in predict_spawn_success when direction- and
    # project-level scope_breakdown data are too sparse to fire. Auto-populated
    # by direction_outcomes._refresh_pipeline_scope_merge_rates after each
    # research_history update — only scopes with >= 5 effective data points are
    # published so the fallback stays conservative during early project state.
    "pipeline_scope_merge_rates": {},
    # Per-failure-class retry budgets: max attempt_count before permanent failure.
    # Overrides max_retries for specific failure classes. Operator-adjustable.
    #   CODE_ERROR:    2 — code won't self-correct; 3rd attempt wastes compute
    #   EXTERNAL_API:  4 — transient outages (GitHub 502, DNS) can last 15-60 min
    #   AGENT_TIMEOUT: 3 — keep current behavior with scope-reduction on 2nd timeout
    "retry_budgets_by_class": {
        "CODE_ERROR": 2,
        "EXTERNAL_API": 4,
        "AGENT_TIMEOUT": 3,
    },
    # Auto-archive terminal tasks (COMPLETED/FAILED/CANCELLED) via two caps:
    #   - age: older than `auto_archive_terminal_after_days`
    #   - count: per-project, keep the N most-recent terminal-unarchived; archive
    #     the rest (0 disables the count cap).
    # Either cap triggers archival. Keeps the default tasks list bounded so the
    # admin UI response stays under Cloud Run's 32 MB body limit and analytics
    # endpoints don't OOM loading a week of rows.
    "auto_archive_enabled": True,
    "auto_archive_terminal_after_days": 7,
    "auto_archive_max_terminal_tasks_per_project": 200,
    "auto_archive_interval_minutes": 60,
    # Reaper cycle-level circuit breaker. After reaper_cycle_halt_threshold
    # consecutive whole-cycle exceptions the reaper enters a soft HALTED state.
    # After reaper_cycle_halt_cooldown_seconds it attempts a half-open probe.
    # After reaper_half_open_probe_limit consecutive failed probes it escalates
    # to a hard halt that requires operator reset.
    "reaper_cycle_halt_threshold": 5,
    "reaper_cycle_halt_cooldown_seconds": 300,
    "reaper_half_open_probe_limit": 3,
    # Per-scope heartbeat-stale thresholds (seconds). Large-scope tasks
    # legitimately run longer stretches without a heartbeat, so reaping at the
    # small-scope bar flags them as dead while they're still making progress.
    "heartbeat_stale_seconds_by_scope": {
        "small": 300,
        "medium": 420,
        "large": 900,
    },
    # Phase 2 (warm-VM continuity): when True, the review pipeline pins a
    # downstream review/fix task to the source task's VM (`task.context.
    # warm_target_vm_id`) and the orchestrator's provisioning loop skips
    # cold-provisioning a fresh VM as long as the target VM has heartbeated
    # within the last 90s. The agent on that VM picks the task up via
    # /api/agent/next-role. Guarded by is_warm_continuity_suppressed() circuit
    # breaker which auto-disables warm pinning when pin-clear rate is elevated.
    "warm_continuity_enabled": True,
    # Circuit-breaker thresholds for warm-pin continuity. When pin-clear events
    # in the last hour exceed warm_pin_clear_rate_threshold, warm pinning is
    # auto-suppressed for warm_continuity_cooldown_seconds before resuming.
    "warm_pin_clear_rate_threshold": 10,
    "warm_continuity_cooldown_seconds": 1800,
    # Phase 2 backlog reuse (efficiency direction d-91d8dede): when True, an
    # agent VM that just went idle (its task reached a terminal state with no
    # chain follow-on) may claim the next eligible QUEUED task from the SAME
    # project's general backlog via /api/agent/next-role and run it warm —
    # not only a PR-chain-pinned follow-on. Independently switchable from
    # warm_continuity_enabled so it can be enabled, observed, and rolled back
    # on its own. Defaults to True — idle VMs claim eligible backlog work warm
    # instead of tearing down and forcing a cold provision; guarded by the
    # is_warm_backlog_reuse_suppressed() circuit breaker which auto-disables
    # reuse when the backlog-origin failure rate is elevated.
    "warm_backlog_reuse_enabled": True,
    # Circuit-breaker thresholds for warm-backlog reuse. When backlog-origin
    # claims result in FAILED tasks (or claim/finalize errors) exceeding the
    # threshold within a 1h rolling window, backlog reuse is auto-suppressed
    # for the cooldown before resuming. Mirrors warm_pin_clear_rate_threshold /
    # warm_continuity_cooldown_seconds for the chain-continuity breaker.
    "warm_backlog_reuse_failure_rate_threshold": 5,
    "warm_backlog_reuse_cooldown_seconds": 1800,
    # Maximum seconds the agent waits in /api/agent/next-role polling before
    # falling through to the normal exit path. Bounded by the warm-handoff
    # window and the typical role transition cadence; tunable down for
    # tighter slot budgets.
    "next_role_poll_timeout_secs": 180,
    # Task-branch namespace (the leading segment of ``{namespace}/{prefix}-{8hex}``
    # agent work branches). DEPRECATED transitional global fallback — per-project
    # ``build_config.task_branch_namespace`` is the real source of truth. Default
    # ``"blueprint"`` preserves byte-identical behavior for the self-repo.
    "task_branch_namespace": "blueprint",
    # Release qualifier (Phase 3+). promotion_enabled gates whether the
    # qualifier may promote a qualified automain candidate to main — default
    # False ships the qualifier in dry-run (score + log, never promote).
    # smoketest_enabled gates smoketest task spawns. Operator-tunable via
    # runtime_config_json; persisted across restarts.
    "promotion_enabled": False,
    "smoketest_enabled": False,
    # Broadcast alert safety cap — limits the number of projects receiving
    # orchestrator-wide broadcast alerts (errors, halts, rollbacks, recovery).
    # Global orchestrator safety cap, not per-project. Operator-tunable via
    # runtime_config_json; persisted across restarts.
    "project_broadcast_limit": 1000,
}

# Scope-aware per-class retry budgets. When a task's context includes a scope
# matching a key below, the scope-specific budget overrides retry_budgets_by_class.
# Missing scope (or a scope not in the inner dict) falls back to the uniform
# retry_budgets_by_class value. Rationale:
#   CODE_ERROR: small tasks are cheap to retry and often succeed with guidance;
#     large tasks failing with CODE_ERROR usually reflect a fundamental approach
#     problem — retrying burns compute without changing the outcome.
#   AGENT_TIMEOUT: large tasks legitimately need more time, so more timeout
#     retries are warranted; small tasks shouldn't need many retries.
#   EXTERNAL_API: transient third-party outages are not correlated with task
#     scope, but large-scope tasks are expensive to re-run from scratch, so
#     allow one extra retry over the uniform budget; small-scope tasks fail
#     cheaply and don't need the full budget.
SCOPE_AWARE_RETRY_BUDGETS: Dict[str, Dict[str, int]] = {
    "CODE_ERROR": {"small": 3, "medium": 2, "large": 1},
    "AGENT_TIMEOUT": {"small": 2, "medium": 3, "large": 4},
    "EXTERNAL_API": {"small": 3, "medium": 4, "large": 5},
}


def get_project_integration_branch(project) -> str:
    """Return the integration branch for a project.

    Fallback chain:
      1. project.integration_branch (per-project column, added by migration 0122+)
      2. "main"

    The deprecated runtime_config bridge was removed after the per-project
    column stabilized across the migration/backfill rollout.
    """
    return getattr(project, "integration_branch", None) or "main"


def get_project_task_branch_namespace(project) -> str:
    """Return the task-branch namespace for a project.

    Fallback chain:
      1. project.build_config['task_branch_namespace'] (per-project override)
      2. runtime_config['task_branch_namespace'] (DEPRECATED global fallback)
      3. ``TASK_BRANCH_NAMESPACE_DEFAULT`` (``"blueprint"``)

    A non-Blueprint install (e.g. a project named ``autoproj``) configures its
    namespace through ``build_config`` so its agent work branches land under
    ``autoproj/{prefix}-{8hex}`` instead of the Blueprint default.
    """
    if project is not None:
        build_config = getattr(project, "build_config", None) or {}
        per_project = build_config.get("task_branch_namespace")
        if per_project:
            return str(per_project)
    return runtime_config.get("task_branch_namespace", TASK_BRANCH_NAMESPACE_DEFAULT)


def get_project_pr_title_prefix(project) -> str:
    """Return the PR title prefix string for a project, with a trailing space.

    Reads ``build_config['pr_title_prefix']``.  If set, returns the value with
    a single trailing space so callers can write ``f"{prefix}Rollup: ..."``
    directly.  If absent or empty, returns ``""`` so no prefix is injected.

    Example ``build_config`` entry::

        {"pr_title_prefix": "[Blueprint]"}
    """
    if project is not None:
        build_config = getattr(project, "build_config", None) or {}
        per_project = build_config.get("pr_title_prefix")
        if per_project:
            return f"{per_project} "
    return ""


def ensure_project_pr_title_prefix(project, title: str) -> str:
    """Return *title* with the project's PR title prefix prepended if absent.

    Used by the backend PR-creation endpoint (``/api/agent/github/pull-requests``)
    to normalise agent-authored PR titles so they carry the project's configured
    branding prefix even when the agent's prompt did not include it explicitly.

    Idempotent: if *title* already starts with the prefix token (ignoring the
    trailing space that :func:`get_project_pr_title_prefix` appends), returns
    *title* unchanged.  When no prefix is configured returns *title* as-is.
    """
    prefix = get_project_pr_title_prefix(project)
    if not prefix:
        return title
    # prefix already carries a trailing space (e.g. "[Blueprint] ").
    # Compare against the bare token so "[Blueprint] feat: x" is not re-prefixed.
    if title.startswith(prefix.rstrip()):
        return title
    return f"{prefix}{title}"


def get_project_promotion_target(project) -> str:
    """Return the promotion target branch for a project.

    Fallback chain:
      1. project.promotion_target (per-project column, added by migration 0125+)
      2. "main"

    The deprecated runtime_config bridge was removed after the per-project
    column stabilized across the migration/backfill rollout.
    """
    return getattr(project, "promotion_target", None) or "main"


async def backfill_branch_columns(session) -> dict:
    """Populate NULL integration_branch / promotion_target from GitHub defaults.

    Runs once at startup (called from main.py lifespan) after migrations finish.
    Only writes to columns that are still NULL — skips rows already populated so
    the function is safe to re-run.  Self-repo projects with NULL integration_branch
    are left untouched (migration 0124 already set them to 'automain'; if still NULL
    they are in a state the LTS workflow manages separately).  Self-repo projects
    with NULL promotion_target get 'main' without a GitHub round-trip.

    For non-self-repo projects, resolves GitHub credentials and calls
    get_repo_default_branch().  When the GitHub API call fails, emits a per-project
    Alert so an operator can fix credentials and re-run rather than silently falling
    back to 'main'.

    Returns a summary dict:
      {
        "filled": int,     # columns written
        "skipped": int,    # projects already fully populated
        "errored": int,    # projects where GitHub API call failed
        "self_repo_skipped": int,  # self-repo integration_branch left as-is
      }
    """
    from app.models import Alert, AlertSeverity, Project
    from app.release_promotion import _resolve_github_with_session
    from app import github as _github
    from sqlmodel import select

    summary = {"filled": 0, "skipped": 0, "errored": 0, "self_repo_skipped": 0}

    try:
        result = await session.execute(
            select(Project).where(
                (Project.integration_branch.is_(None))
                | (Project.promotion_target.is_(None))
            )
        )
        projects = result.scalars().all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BRANCH_BACKFILL] Could not query projects: %s", exc)
        return summary

    for project in projects:
        need_integration = project.integration_branch is None
        need_promotion = project.promotion_target is None

        if not need_integration and not need_promotion:
            summary["skipped"] += 1
            continue

        # Self-repo: integration_branch managed by the LTS workflow — leave NULL
        # as-is (migration 0124 already backfilled is_self_repo rows to 'automain').
        if project.is_self_repo and need_integration:
            summary["self_repo_skipped"] += 1
            need_integration = False

        # Self-repo promotion_target: no GitHub round-trip needed.
        if project.is_self_repo and need_promotion:
            project.promotion_target = "main"
            flag_modified(project, "promotion_target")
            session.add(project)
            summary["filled"] += 1
            need_promotion = False

        if not need_integration and not need_promotion:
            continue

        # Non-self-repo: resolve GitHub credentials and fetch the default branch.
        try:
            resolved = await _resolve_github_with_session(session, project)
            if resolved is None:
                raise ValueError("no GitHub credentials available")
            token, owner, repo = resolved
            default_branch = await _github.get_repo_default_branch(token, owner, repo)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[BRANCH_BACKFILL] project %s (%s/%s): GitHub call failed: %s",
                project.id,
                getattr(project, "github_owner", "?"),
                getattr(project, "github_repo", "?"),
                exc,
            )
            try:
                alert = Alert(
                    project_id=project.id,
                    severity=AlertSeverity.WARNING,
                    title="Branch backfill failed — integration_branch or promotion_target still NULL",
                    message=(
                        f"Could not populate integration_branch/promotion_target for project "
                        f"{project.id} from GitHub: {str(exc)[:300]}. "
                        "Fix GitHub credentials and re-run the backfill (restart the service or "
                        "call POST /api/admin/projects/{id}/backfill-branches)."
                    ),
                )
                session.add(alert)
            except Exception:  # noqa: BLE001
                pass
            summary["errored"] += 1
            continue

        if need_integration:
            project.integration_branch = default_branch
            flag_modified(project, "integration_branch")
            summary["filled"] += 1
        if need_promotion:
            project.promotion_target = default_branch
            flag_modified(project, "promotion_target")
            summary["filled"] += 1
        session.add(project)

    if summary["filled"] or summary["errored"]:
        logger.info(
            "[BRANCH_BACKFILL] complete: filled=%d skipped=%d errored=%d self_repo_skipped=%d",
            summary["filled"],
            summary["skipped"],
            summary["errored"],
            summary["self_repo_skipped"],
        )

    return summary


def get_project_broadcast_limit(project) -> int:
    """Return the broadcast alert limit for a project.

    Fallback chain:
      1. project.design_doc['_broadcast_limit'] (per-project override via admin endpoint)
      2. runtime_config["project_broadcast_limit"] (global default)
      3. 1000
    """
    per_project = (getattr(project, "design_doc", None) or {}).get("_broadcast_limit")
    if per_project is not None:
        return int(per_project)
    return runtime_config.get("project_broadcast_limit", 1000)


def _migrate_legacy_provider_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """Translate legacy routing/effort keys to the unified shape in-place.

    The old shape split the same "which provider + what effort" decision
    across four dicts (provider_preference_by_category, task_type_provider_preference,
    effort_profiles, task_type_effort_profile). The new shape collapses
    them into two:
      - category_routing[cat] = {"providers": [...], "effort": "..."}
      - task_type_routing[task_type] = {"providers"?, "effort"?}
    plus a per-provider effort matrix:
      - provider_settings_by_effort[provider][level] = bag

    No-op when new keys are already present. Safe to call on every sync.
    """
    # Step 1: collapse preferences + profiles into category_routing.
    if "category_routing" not in config:
        legacy_pref = config.get("provider_preference_by_category") or {}
        legacy_task_profile = config.get("task_type_effort_profile") or {}
        legacy_categories = config.get("task_type_categories") or {}
        # Default effort from legacy _default mapping, else medium.
        default_effort = None
        if isinstance(legacy_task_profile, dict):
            raw = legacy_task_profile.get("_default")
            if isinstance(raw, str) and raw:
                default_effort = raw
        default_effort = default_effort or "medium"
        # Category effort: sample from the first member task type's profile.
        cat_routing: Dict[str, Dict[str, Any]] = {}
        if isinstance(legacy_pref, dict):
            for cat, providers in legacy_pref.items():
                if not isinstance(providers, list):
                    continue
                effort = default_effort
                members = (
                    legacy_categories.get(cat)
                    if isinstance(legacy_categories, dict)
                    else None
                )
                if isinstance(members, list):
                    for task in members:
                        if isinstance(legacy_task_profile, dict):
                            raw = legacy_task_profile.get(task)
                            if isinstance(raw, str) and raw:
                                effort = raw
                                break
                cat_routing[cat] = {"providers": list(providers), "effort": effort}
        if "_default" not in cat_routing:
            default_providers = (
                legacy_pref.get("_default") if isinstance(legacy_pref, dict) else None
            ) or ["anthropic", "openai", "gemini"]
            cat_routing["_default"] = {
                "providers": list(default_providers),
                "effort": default_effort,
            }
        if cat_routing:
            config["category_routing"] = cat_routing

    # Step 2: collapse per-task preference + profile into task_type_routing.
    if "task_type_routing" not in config:
        legacy_task_pref = config.get("task_type_provider_preference") or {}
        legacy_task_profile = config.get("task_type_effort_profile") or {}
        task_routing: Dict[str, Dict[str, Any]] = {}
        if isinstance(legacy_task_pref, dict):
            for task, providers in legacy_task_pref.items():
                if task == "_default" or not isinstance(providers, list):
                    continue
                task_routing.setdefault(task, {})["providers"] = list(providers)
        if isinstance(legacy_task_profile, dict):
            for task, profile in legacy_task_profile.items():
                if task == "_default" or not isinstance(profile, str):
                    continue
                task_routing.setdefault(task, {})["effort"] = profile
        if task_routing:
            config["task_type_routing"] = task_routing

    # Step 3: flip effort_profiles[profile][provider] into
    # provider_settings_by_effort[provider][level]. Keep model-less bags as-is.
    if "provider_settings_by_effort" not in config:
        legacy_profiles = config.get("effort_profiles") or {}
        if isinstance(legacy_profiles, dict):
            matrix: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for level, per_provider in legacy_profiles.items():
                if level == "_default" or not isinstance(per_provider, dict):
                    continue
                for provider, bag in per_provider.items():
                    if not isinstance(bag, dict):
                        continue
                    matrix.setdefault(provider, {})[level] = dict(bag)
            if matrix:
                config["provider_settings_by_effort"] = matrix

    return config


async def sync_runtime_config_from_state(state) -> list[str]:
    """Apply persisted DeploymentState values to in-memory runtime_config.

    Cloud Run scales to multiple instances (max_instance_count=5); admin
    mutations (pause, settings) only update the receiving instance's local
    dict. DB state is the shared source of truth — this helper propagates it
    to every instance when called periodically.

    PAUSED/HALTED always forces max_concurrent_vms=0 regardless of the
    persisted runtime_max_concurrent_vms value, so a pause on any instance
    converges across the fleet even if the dispatch cap itself was also
    changed recently.

    Returns the list of keys whose values actually changed — caller can log.
    """
    if state is None:
        return []

    # Lazy import to avoid module-level circular dependency with app.models
    from app.models import DeploymentStatus

    changed: list[str] = []

    def _set(key: str, new_val) -> None:
        old = runtime_config.get(key)
        if old != new_val:
            runtime_config[key] = new_val
            changed.append(key)

    paused = state.status in (DeploymentStatus.PAUSED, DeploymentStatus.HALTED)

    async with runtime_config_lock:
        try:
            # When paused, the override below clamps max_concurrent_vms to 0.
            # Skip the persisted-value restore so the in-memory cap doesn't
            # bounce 15→0 every cycle (which both spammed "Runtime config
            # drift corrected: ['max_concurrent_vms', 'max_concurrent_vms']"
            # and briefly exposed the pre-pause cap to lock-free readers).
            if state.runtime_max_concurrent_vms is not None and not paused:
                _set("max_concurrent_vms", int(state.runtime_max_concurrent_vms))
            if state.runtime_max_retries is not None:
                _set("max_retries", int(state.runtime_max_retries))
            if state.runtime_max_autonomous_tasks_per_hour is not None:
                _set(
                    "max_autonomous_tasks_per_hour",
                    int(state.runtime_max_autonomous_tasks_per_hour),
                )
            if (
                getattr(state, "runtime_max_global_autonomous_tasks_per_hour", None)
                is not None
            ):
                _set(
                    "max_global_autonomous_tasks_per_hour",
                    int(state.runtime_max_global_autonomous_tasks_per_hour),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[CONFIG-SYNC] Could not sync scalar config cols: {e}")

        try:
            if state.runtime_autonomous_research_interval_minutes is not None:
                _set(
                    "autonomous_research_interval_minutes",
                    int(state.runtime_autonomous_research_interval_minutes),
                )
            if (
                getattr(state, "runtime_autonomous_direction_cooldown_minutes", None)
                is not None
            ):
                _set(
                    "autonomous_direction_cooldown_minutes",
                    int(state.runtime_autonomous_direction_cooldown_minutes),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[CONFIG-SYNC] Could not sync autonomous cols: {e}")

        try:
            config_json = getattr(state, "runtime_config_json", None)
            if config_json and isinstance(config_json, dict):
                # Translate any legacy-shape keys to the new shape in-place so
                # downstream readers never see the old layout. Safe to call
                # every sync — no-ops when new keys are already present.
                config_json = _migrate_legacy_provider_config(dict(config_json))
                for key in (
                    "provider_tier_model_map",
                    "task_type_categories",
                    "category_routing",
                    "task_type_routing",
                    "task_type_model_preferences",
                    "provider_settings_by_effort",
                    "provider_selection_mode",
                    "cross_provider_fallback_enabled",
                    "agent_prompt_suffixes",
                    "provider_settings_defaults",
                    "task_type_provider_overrides",
                    "autonomous_periodic_tasks",
                    "health_check_urls",
                    "notification_config",
                ):
                    if key in config_json:
                        _set(key, config_json[key])
                if "strategic_research_frequency" in config_json:
                    _set(
                        "strategic_research_frequency",
                        int(config_json["strategic_research_frequency"]),
                    )
                if "strategic_max_interval_minutes" in config_json:
                    _set(
                        "strategic_max_interval_minutes",
                        int(config_json["strategic_max_interval_minutes"]),
                    )
                if "require_approval_gates" in config_json:
                    _set(
                        "require_approval_gates",
                        bool(config_json["require_approval_gates"]),
                    )
                if "daily_cost_ceiling_usd" in config_json:
                    _set(
                        "daily_cost_ceiling_usd",
                        float(config_json["daily_cost_ceiling_usd"]),
                    )
                if "auto_archive_enabled" in config_json:
                    _set(
                        "auto_archive_enabled",
                        bool(config_json["auto_archive_enabled"]),
                    )
                if "warm_continuity_enabled" in config_json:
                    _set(
                        "warm_continuity_enabled",
                        bool(config_json["warm_continuity_enabled"]),
                    )
                if "warm_backlog_reuse_enabled" in config_json:
                    _set(
                        "warm_backlog_reuse_enabled",
                        bool(config_json["warm_backlog_reuse_enabled"]),
                    )
                for int_key in (
                    "max_provision_retries",
                    "max_rollup_retries",
                    "architecture_review_frequency",
                    "max_active_directions",
                    "max_parallel_research_per_project",
                    "auto_archive_terminal_after_days",
                    "auto_archive_max_terminal_tasks_per_project",
                    "auto_archive_interval_minutes",
                    "max_pipeline_slots",
                    "evaluating_pipeline_timeout_minutes",
                    "ci_backstop_timeout_minutes",
                    "evaluating_fail_hours",
                    "max_total_research_per_project",
                    "next_role_poll_timeout_secs",
                    "direction_churn_window_cycles",
                    "direction_churn_non_merge_threshold",
                    "direction_lifetime_non_merge_threshold",
                    "warm_pin_clear_rate_threshold",
                    "warm_continuity_cooldown_seconds",
                    "warm_backlog_reuse_failure_rate_threshold",
                    "warm_backlog_reuse_cooldown_seconds",
                    "project_broadcast_limit",
                    "reaper_cycle_halt_threshold",
                    "reaper_cycle_halt_cooldown_seconds",
                    "reaper_half_open_probe_limit",
                ):
                    if int_key in config_json:
                        _set(int_key, int(config_json[int_key]))
                for float_key in (
                    "direction_churn_min_merge_rate",
                    "direction_lifetime_min_merge_rate",
                ):
                    if float_key in config_json:
                        _set(float_key, float(config_json[float_key]))
                if "reserved_slots_for_types" in config_json:
                    _set(
                        "reserved_slots_for_types",
                        config_json["reserved_slots_for_types"],
                    )
                if "promotion_enabled" in config_json:
                    _set(
                        "promotion_enabled",
                        bool(config_json["promotion_enabled"]),
                    )
                if "smoketest_enabled" in config_json:
                    _set(
                        "smoketest_enabled",
                        bool(config_json["smoketest_enabled"]),
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[CONFIG-SYNC] Could not sync runtime_config_json: {e}")

        # PAUSED/HALTED always forces the dispatch cap to 0. (We already
        # skipped the restore from runtime_max_concurrent_vms above when
        # paused, so this just zeros the in-memory value.)
        try:
            if paused:
                _set("max_concurrent_vms", 0)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[CONFIG-SYNC] Could not apply PAUSED override: {e}")

    return changed


async def runtime_config_sync_service():
    """Per-instance loop that refreshes runtime_config from DB every 15s.

    Cloud Run scales across multiple instances; admin mutations are
    instance-local. The orchestrator already syncs each cycle for the
    dispatch path, but non-dispatch paths (routers, background services
    reading prompt suffixes/notification config/etc.) still see stale
    values. This loop makes every instance converge on the DB state
    within ~15s regardless of which path reads runtime_config.
    """
    # Lazy imports avoid module-level circular deps.
    from app.database import AsyncSessionLocal
    from app.models import DeploymentState

    while True:
        try:
            await asyncio.sleep(15)
            async with AsyncSessionLocal() as session:
                state = await session.get(DeploymentState, 1)
                changed = await sync_runtime_config_from_state(state)
                if changed:
                    logger.info(f"[CONFIG-SYNC] Applied DB changes: {changed}")
        except asyncio.CancelledError:  # noqa: PERF203
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[CONFIG-SYNC] sync cycle failed: {e}", exc_info=True)


def resolve_retry_budget(failure_class: str, scope: str | None) -> int:
    """Return the retry budget for (failure_class, scope), falling back to the
    uniform retry_budgets_by_class value (or max_retries) if scope is not set
    or has no scope-specific override."""
    per_scope = SCOPE_AWARE_RETRY_BUDGETS.get(failure_class)
    if scope and per_scope and scope in per_scope:
        return per_scope[scope]
    return runtime_config.get("retry_budgets_by_class", {}).get(
        failure_class, runtime_config["max_retries"]
    )


# --- Autonomous Loop State ---
autonomous_failure_counts: Dict[str, int] = {}  # project_id -> consecutive failures
last_idle_check: datetime | None = None

# --- Persistent Anomaly Tracking ---
# project_id -> { anomaly_key -> {first_seen, last_seen, occurrence_count, severity, latest_details} }
persistent_anomalies: Dict[str, Dict[str, Dict]] = {}

# Last time _compute_research_anomalies completed successfully for ANY project.
# Used by /api/admin/anomaly-tracker/snapshot so operators can detect a silent
# writer failure (corrupted shared_state, swallowed exception). Ephemeral —
# not persisted across pod restarts.
last_anomaly_detection_run_at: datetime | None = None

# Key used inside Project.design_doc for anomaly fingerprint persistence.
_ANOMALY_FINGERPRINTS_KEY = "_anomaly_fingerprints"


async def hydrate_persistent_anomalies(session) -> int:
    """Load anomaly fingerprint state from Project.design_doc into the in-memory cache.

    Call once at startup so persistent_anomalies survives pod restarts.
    In-memory entries always win over stored ones (no clobber on concurrent startup).
    Returns the total number of anomaly entries loaded across all projects.
    """
    from app.models import Project
    from sqlmodel import select
    from sqlalchemy.orm import load_only

    loaded = 0
    try:
        projects = (
            (
                await session.execute(
                    select(Project).options(load_only(Project.id, Project.design_doc))
                )
            )
            .scalars()
            .all()
        )
        for project in projects:
            stored = (project.design_doc or {}).get(_ANOMALY_FINGERPRINTS_KEY)
            if not isinstance(stored, dict) or not stored:
                continue
            proj_key = str(project.id)
            target = persistent_anomalies.setdefault(proj_key, {})
            for key, entry in stored.items():
                if key not in target:
                    target[key] = entry
            loaded += len(stored)
    except Exception:  # noqa: BLE001
        logger.warning(
            "[ANOMALY_PERSIST] hydrate_persistent_anomalies failed", exc_info=True
        )
    return loaded


async def with_design_doc_locked(
    session,
    project_id: str,
    mutate_fn: Any,
) -> None:
    """Read-modify-write project.design_doc under a project row lock.

    Thin wrapper over the canonical ``task_locking.with_design_doc_locked``
    that additionally coerces ``project_id`` (which background callers pass
    as a str) to ``UUID`` and silently no-ops on malformed input. The
    lock/mutate/flag_modified logic lives in exactly one place — the
    canonical helper — so the two never drift apart.
    """
    from app.task_locking import with_design_doc_locked as _canonical
    import uuid as _uuid

    try:
        pid = _uuid.UUID(str(project_id))
    except (TypeError, ValueError):
        return
    await _canonical(session, pid, mutate_fn)


async def persist_anomaly_snapshot(
    project_id: str, proj_anomalies: dict, session
) -> None:
    """Write anomaly fingerprints for one project into Project.design_doc.

    Called after each anomaly detection cycle so the state survives the next
    pod restart. Safe to call within an already-open session (caller commits).
    """

    def _mutate(doc: dict) -> None:
        doc[_ANOMALY_FINGERPRINTS_KEY] = {
            k: dict(v) if isinstance(v, dict) else v for k, v in proj_anomalies.items()
        }

    await with_design_doc_locked(session, project_id, _mutate)


# --- Regression Signal State (first-tripped timestamps for MTTD) ---
# project_id -> {regression_class -> ISO datetime string}
# Populated at startup from Project.regression_signal_state and updated after
# each dispatcher spawn/recovery so first-tripped times survive pod restarts.
regression_signal_first_tripped: Dict[str, Dict[str, str]] = {}


async def hydrate_regression_signal_state(session) -> int:
    """Load regression signal first-tripped timestamps from Project.regression_signal_state.

    Call once at startup so timestamps survive pod restarts.
    In-memory entries always win over stored ones (no clobber on concurrent startup).
    Returns the total number of per-class entries loaded across all projects.
    """
    from app.models import Project
    from sqlmodel import select
    from sqlalchemy.orm import load_only

    loaded = 0
    try:
        projects = (
            (
                await session.execute(
                    select(Project).options(
                        load_only(Project.id, Project.regression_signal_state)
                    )
                )
            )
            .scalars()
            .all()
        )
        for project in projects:
            stored = project.regression_signal_state
            if not isinstance(stored, dict) or not stored:
                continue
            proj_key = str(project.id)
            target = regression_signal_first_tripped.setdefault(proj_key, {})
            for cls, iso_str in stored.items():
                if cls not in target:
                    target[cls] = iso_str
                    loaded += 1
    except Exception:  # noqa: BLE001
        logger.warning(
            "[REGRESSION_SIGNAL] hydrate_regression_signal_state failed", exc_info=True
        )
    return loaded


async def persist_regression_signal_snapshot(
    project_id: str, per_project_state: Dict[str, str], session
) -> None:
    """Write regression signal first-tripped timestamps for one project to DB.

    per_project_state maps regression_class -> ISO datetime string.
    Called after each spawn or recovery so timestamps survive pod restarts.
    Safe to call within an already-open session (caller commits).
    """
    from app.models import Project
    from sqlalchemy.orm.attributes import flag_modified
    import uuid as _uuid

    try:
        pid = _uuid.UUID(project_id)
    except (TypeError, ValueError):
        return
    project = await session.get(Project, pid)
    if project is None:
        return
    project.regression_signal_state = dict(per_project_state)
    flag_modified(project, "regression_signal_state")
    session.add(project)


# --- Anomaly-Driven Pipeline Adjustments ---
# When high-severity anomalies (PROVISION_FAILURE, EXTERNAL_API) persist,
# the system automatically reduces blast radius by lowering max_concurrent_vms
# and tightening the failure-rate dispatch gate.
anomaly_adjustments: Dict[str, Any] = {
    "active": False,
    "adjusted_at": None,  # ISO timestamp when adjustments were applied
    "trigger_anomalies": [],  # anomaly keys that triggered the adjustment
    "pre_adjustment_max_vms": None,  # stored to restore on clearing
    "pre_adjustment_failure_gate": None,
    "failure_gate_threshold": 0.8,  # default; lowered to 0.6 during anomaly response
    "forced_restoration_count": 0,  # cumulative count of max-duration forced restorations
    "forced_restored_at": None,  # ISO timestamp of last forced restoration
    "adjusted_max_vms": None,  # target max_concurrent_vms we set; used to detect deploy_health/admin overrides
}

# --- Circuit Breaker (Hystrix-inspired) ---
# Per-project circuit breaker state for autonomous task spawning
# States: CLOSED (normal), OPEN (tripped), HALF_OPEN (testing one task)
circuit_breakers: Dict[str, Dict] = {}  # project_id -> breaker state


def get_circuit_breaker(project_id: str) -> Dict:
    """Get or create circuit breaker state for a project."""
    if project_id not in circuit_breakers:
        circuit_breakers[project_id] = {
            "state": "CLOSED",  # CLOSED, OPEN, HALF_OPEN
            "failure_count": 0,  # consecutive code-quality failures (CODE_ERROR, AGENT_TIMEOUT)
            "infra_failure_count": 0,  # consecutive infra failures (PROVISION_FAILURE, RATE_LIMIT, EXTERNAL_API)
            "success_count": 0,  # consecutive successes (for half-open)
            "opened_at": None,  # when the breaker tripped
            "cooldown_seconds": 1800,  # 30 min initial cooldown
            "base_cooldown": 1800,  # for exponential backoff reset
            "half_open_task_id": None,  # the test task in HALF_OPEN
            "open_reason": None,  # "code" or "infra" — why the breaker opened
        }
    return circuit_breakers[project_id]


# --- User-submitted Auto-Debug Rate Limiting ---
# Tracks timestamps of user-submitted debug spawns per project to enforce hourly cap.
# Structure: project_id -> deque of datetime (UTC) of recent spawns
user_debug_spawn_times: Dict[str, deque] = {}

USER_DEBUG_MAX_PER_HOUR = (
    3  # max debug tasks spawned for user/admin tasks per project per hour
)


def record_user_debug_spawn(project_id: str, now: datetime) -> None:
    """Record a user-submitted debug spawn and prune old entries."""
    if project_id not in user_debug_spawn_times:
        user_debug_spawn_times[project_id] = deque()
    dq = user_debug_spawn_times[project_id]
    dq.append(now)
    cutoff = now - timedelta(hours=1)
    while dq and dq[0] < cutoff:
        dq.popleft()


def count_recent_user_debug_spawns(project_id: str, now: datetime) -> int:
    """Count user-submitted debug spawns in the last hour for a project."""
    dq = user_debug_spawn_times.get(project_id)
    if not dq:
        return 0
    cutoff = now - timedelta(hours=1)
    return sum(1 for t in dq if t >= cutoff)


# --- Cross-PR Merge Error Pattern Tracking ---
# When the same normalized merge error blocks multiple PRs in a project within a
# sliding window, that's a systemic blocker (branch protection change, CI env
# issue) — not a per-PR problem. Track (pr_number, ts) occurrences per
# (project_id, normalized_error) so ci_completion can pause wasteful retries
# and surface a shared alert.
merge_error_occurrences: Dict[tuple, deque] = {}

MERGE_PATTERN_WINDOW_SECONDS = int(os.getenv("MERGE_PATTERN_WINDOW_SECONDS", "1800"))
MERGE_PATTERN_MIN_PRS = int(os.getenv("MERGE_PATTERN_MIN_PRS", "3"))


def record_merge_error_occurrence(
    project_id: str, normalized_error: str, pr_number: int
) -> int:
    """Record a merge-error occurrence and return the count of distinct PRs
    hit by the same (project, normalized_error) within the sliding window."""
    if not project_id or not normalized_error or pr_number is None:
        return 0
    key = (project_id, normalized_error)
    dq = merge_error_occurrences.setdefault(key, deque(maxlen=50))
    now = _utcnow()
    dq.append((pr_number, now))
    cutoff = now - timedelta(seconds=MERGE_PATTERN_WINDOW_SECONDS)
    while dq and dq[0][1] < cutoff:
        dq.popleft()
    return len({pr for pr, _ in dq})


def count_merge_error_pattern_prs(project_id: str, normalized_error: str) -> int:
    """Distinct PRs in the sliding window for a (project, error) key."""
    if not project_id or not normalized_error:
        return 0
    dq = merge_error_occurrences.get((project_id, normalized_error))
    if not dq:
        return 0
    cutoff = _utcnow() - timedelta(seconds=MERGE_PATTERN_WINDOW_SECONDS)
    return len({pr for pr, ts in dq if ts >= cutoff})


# --- Warm-VM heartbeat tracking (Phase 2) ---
# Maps vm_instance_id -> last datetime the VM contacted us (claim, status,
# heartbeat, vm-heartbeat, or next-role poll). Used by the orchestrator's
# provisioning loop to decide whether a warm-target pin is still honored.
# Pruned by sweep_stale_in_memory_state — entries older than 1h are removed.
vm_last_heartbeat: Dict[str, datetime] = {}


def record_vm_heartbeat(vm_instance_id: str) -> None:
    """Record liveness for a VM (warm-VM continuity).

    Called from the agent's vm-heartbeat endpoint AND from the next-role
    polling endpoint (which doubles as a liveness signal). Stores the
    current naive UTC timestamp.
    """
    if not vm_instance_id:
        return
    vm_last_heartbeat[vm_instance_id] = _utcnow()


def is_vm_alive(vm_instance_id: str, max_age_seconds: int = 90) -> bool:
    """True iff the VM heartbeated within the last `max_age_seconds`.

    Used by the orchestrator's provisioning loop to decide whether a
    QUEUED task with `warm_target_vm_id` should skip provisioning (live
    target → agent will pick it up via /next-role) or clear the warm
    fields and provision normally (target unresponsive → cold path).
    """
    if not vm_instance_id:
        return False
    last = vm_last_heartbeat.get(vm_instance_id)
    if last is None:
        return False
    return (_utcnow() - last).total_seconds() <= max_age_seconds


# --- Warm-pin clear-out tracking (Phase 2e observability) ---
# Each entry: (timestamp, vm_id, reason). Reason is "deadline_expired" or
# "vm_unresponsive". Sustained >5/hour means warm VMs are dying mid-poll
# more often than expected — the agent's lifecycle code is leaking state
# or the reset routine is failing. Pruned by sweep_stale_in_memory_state.
warm_pin_clear_events: deque = deque(maxlen=200)


def record_warm_pin_clear(vm_id: str, reason: str) -> None:
    """Record a warm-pin clear-out event for the orchestrator's metric."""
    if not vm_id:
        return
    warm_pin_clear_events.append((_utcnow(), vm_id, reason))


def count_recent_warm_pin_clears(window_seconds: int = 3600) -> int:
    """Distinct clear-out events in the trailing window."""
    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    return sum(1 for ts, _, _ in warm_pin_clear_events if ts >= cutoff)


# Auto-suppression deadline set by is_warm_continuity_suppressed() when the
# pin-clear rate exceeds warm_pin_clear_rate_threshold. None means not suppressed.
warm_continuity_suppressed_until: datetime | None = None


# Per-call-site counter for pr_is_not_behind() outcomes. Keyed by a stable
# label that names the calling site (e.g. "ci_completion", "rollup_pre_merge").
# Each inner dict has integer buckets for the True/False/None outcomes plus a
# total. Updated synchronously by record_pr_is_not_behind() and snapshotted by
# the admin diagnostics endpoint. In-process only — not persisted.
PR_IS_NOT_BEHIND_CALL_SITES: tuple[str, ...] = (
    "ci_completion",
    "rollup_pre_merge",
    "rollup_post_merge",
    "orchestrator_ci",
)
pr_is_not_behind_counts: Dict[str, Dict[str, int]] = {}


def _empty_pr_is_not_behind_bucket() -> Dict[str, int]:
    return {"true": 0, "false": 0, "none": 0, "total": 0}


def record_pr_is_not_behind(call_site: str, result: bool | None) -> None:
    """Increment the bucket matching ``result`` for ``call_site``."""
    if not call_site:
        return
    bucket = pr_is_not_behind_counts.setdefault(
        call_site, _empty_pr_is_not_behind_bucket()
    )
    if result is True:
        bucket["true"] += 1
    elif result is False:
        bucket["false"] += 1
    else:
        bucket["none"] += 1
    bucket["total"] += 1


def snapshot_pr_is_not_behind_counts() -> Dict[str, Dict[str, int]]:
    """Return a deep-copied snapshot keyed by call site.

    Always includes the four canonical call sites with zeroed buckets so the
    response shape is stable from process start. Any other call sites that have
    been recorded (future expansions) are included as well.
    """
    snapshot: Dict[str, Dict[str, int]] = {
        site: _empty_pr_is_not_behind_bucket() for site in PR_IS_NOT_BEHIND_CALL_SITES
    }
    for site, bucket in pr_is_not_behind_counts.items():
        snapshot[site] = dict(bucket)
    return snapshot


def is_warm_continuity_suppressed() -> bool:
    """True iff warm continuity is auto-suppressed by the pin-clear circuit breaker.

    Checks the recent pin-clear rate against the configured threshold. When the
    rate exceeds the threshold, sets a suppression window so callers fall back to
    cold provisioning until the cooldown expires and the rate normalises.
    """
    global warm_continuity_suppressed_until
    now = _utcnow()
    if (
        warm_continuity_suppressed_until is not None
        and now < warm_continuity_suppressed_until
    ):
        return True
    threshold = runtime_config.get("warm_pin_clear_rate_threshold", 10)
    recent_clears = count_recent_warm_pin_clears(3600)
    if recent_clears >= threshold:
        cooldown = runtime_config.get("warm_continuity_cooldown_seconds", 1800)
        warm_continuity_suppressed_until = now + timedelta(seconds=cooldown)
        logger.warning(
            f"[WARM_CONTINUITY] Circuit breaker tripped: {recent_clears} pin-clear events "
            f"in last hour (threshold {threshold}). Suppressed until "
            f"{warm_continuity_suppressed_until.isoformat()}."
        )
        return True
    warm_continuity_suppressed_until = None
    return False


# --- Warm backlog-reuse circuit breaker (efficiency direction d-91d8dede) ---
# Each entry: (timestamp, reason). Sustained failures → auto-suppression
# for a bounded cooldown before the backlog-reuse path resumes.
# Bounded deque; pruned by sweep_stale_in_memory_state.
warm_backlog_reuse_failure_events: deque = deque(maxlen=200)

# Auto-suppression deadline set by is_warm_backlog_reuse_suppressed() when the
# failure rate exceeds warm_backlog_reuse_failure_rate_threshold. None = not suppressed.
warm_backlog_reuse_suppressed_until: datetime | None = None


def record_backlog_reuse_failure(reason: str = "task_failed") -> None:
    """Record a backlog-reuse failure event (FAILED task or finalize error).

    Called from the /next-role finalize-error path and from agent_status when
    a FAILED task originated from a backlog_reuse warm claim.
    """
    warm_backlog_reuse_failure_events.append((_utcnow(), reason))


def count_recent_backlog_reuse_failures(window_seconds: int = 3600) -> int:
    """Count backlog-reuse failure events in the trailing window."""
    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    return sum(1 for ts, _ in warm_backlog_reuse_failure_events if ts >= cutoff)


def is_warm_backlog_reuse_suppressed() -> bool:
    """True iff warm-backlog reuse is auto-suppressed by the failure circuit breaker.

    Checks the recent failure rate against the configured threshold. When the
    rate exceeds the threshold, sets a suppression window so callers fall back
    to cold provisioning until the cooldown expires and the rate normalises.
    """
    global warm_backlog_reuse_suppressed_until
    now = _utcnow()
    if (
        warm_backlog_reuse_suppressed_until is not None
        and now < warm_backlog_reuse_suppressed_until
    ):
        return True
    threshold = runtime_config.get("warm_backlog_reuse_failure_rate_threshold", 5)
    recent_failures = count_recent_backlog_reuse_failures(3600)
    if recent_failures >= threshold:
        cooldown = runtime_config.get("warm_backlog_reuse_cooldown_seconds", 1800)
        warm_backlog_reuse_suppressed_until = now + timedelta(seconds=cooldown)
        logger.warning(
            f"[BACKLOG_REUSE] Circuit breaker tripped: {recent_failures} failure events "
            f"in last hour (threshold {threshold}). Suppressed until "
            f"{warm_backlog_reuse_suppressed_until.isoformat()}."
        )
        return True
    warm_backlog_reuse_suppressed_until = None
    return False


# --- Warm backlog-reuse first-refusal (efficiency direction d-91d8dede) ---
# project_id (str) -> first-refusal deadline (naive UTC). While a project has
# an unexpired entry, the orchestrator's cold-dispatch loop holds off
# cold-provisioning eligible backlog tasks for that project so an idle warm VM
# polling /api/agent/next-role gets first claim. Set/refreshed on each idle-VM
# poll (and when a COMPLETED task's teardown is deferred). The atomic
# SKIP-LOCKED claim in /next-role still guarantees no double-provision if both
# race — this only biases the race toward reuse. Bounded: entries are short
# (WARM_BACKLOG_FIRST_REFUSAL_SECS) and pruned by sweep_stale_in_memory_state.
WARM_BACKLOG_FIRST_REFUSAL_SECS = 20
warm_backlog_first_refusal: Dict[str, datetime] = {}


def register_backlog_first_refusal(
    project_id: str, window_seconds: int = WARM_BACKLOG_FIRST_REFUSAL_SECS
) -> None:
    """Mark project_id as having an idle warm VM that wants backlog work.

    Sets the first-refusal deadline to now + window_seconds. Called from the
    /next-role poll path so the orchestrator holds off cold-provisioning the
    project's eligible backlog while a warm VM is actively polling.
    """
    if not project_id:
        return
    warm_backlog_first_refusal[str(project_id)] = _utcnow() + timedelta(
        seconds=window_seconds
    )


def is_backlog_first_refusal_active(project_id: str) -> bool:
    """True iff project_id has an unexpired backlog first-refusal window."""
    if not project_id:
        return False
    deadline = warm_backlog_first_refusal.get(str(project_id))
    if deadline is None:
        return False
    return _utcnow() <= deadline


# --- Background Tasks (populated by lifespan, used by admin restart endpoint) ---
background_tasks: Dict[str, asyncio.Task] = {}  # name -> asyncio.Task

# --- Leader Election State ---
# True on the instance that won the pg_try_advisory_lock; False on followers.
# Defaults to True so non-Postgres environments (tests, local dev without PG) behave normally.
is_leader: bool = True

# --- Deployment Monitoring (Phase A: detect + alert) ---
deploy_monitoring: Dict = {
    "window_start": None,  # datetime when monitoring began for current deploy
    "window_duration": 900,  # 15 minutes default
    "extended_window": 1800,  # 30 min for large/infra changes
    "is_extended": False,  # set True when infra files are touched
    "task_results": deque(
        maxlen=100
    ),  # (timestamp_iso, "success"|"failure", failure_class, task_type)
    "pre_deploy_failure_rate": 0.0,  # baseline from before deploy
    "health_checks": {
        "dispatch_working": True,  # orchestrator picked up a task in last 5 min
        "completions_received": True,  # at least one agent reported back
        "db_accessible": True,  # can read/write tasks
    },
    "last_dispatch_time": None,  # last time orchestrator dispatched a task
    "last_completion_time": None,  # last time an agent reported completion
    "consecutive_health_failures": 0,
    # Bounded ring buffer of pipeline-health events written by the reaper.
    # Each entry: {"type": str, "ts": datetime, "task_id": str,
    #              "vm_id": Optional[str], "zone": Optional[str],
    #              "account_id": Optional[str]}
    "pipeline_events": deque(maxlen=500),
    # event_type -> last datetime an alert fired (for dedup window)
    "pipeline_alert_last_ts": {},
}

# Pipeline-health event taxonomy. The reaper writes one of these into
# deploy_monitoring["pipeline_events"] at each terminal decision point.
PIPELINE_EVENT_TYPES = frozenset(
    {
        "heartbeat_timeout_post_claim",
        "provision_timeout_pre_claim",
        "absolute_timeout",
        "never_started",
        "account_blacklisted",
    }
)

# Threshold counts (per 1h window) that trigger a system-level alert.
# "reap_rate_per_hour" aggregates all reap-reason events to catch
# cascading infra failure even when no single category trips.
PIPELINE_ALERT_THRESHOLDS = {
    "heartbeat_timeout_post_claim": int(os.getenv("ALERT_HEARTBEAT_TIMEOUT", "10")),
    "provision_timeout_pre_claim": int(os.getenv("ALERT_PROVISION_TIMEOUT", "5")),
    "account_blacklisted": int(os.getenv("ALERT_ACCOUNT_BLACKLIST", "3")),
    "reap_rate_per_hour": int(os.getenv("ALERT_REAP_RATE", "15")),
}
PIPELINE_ALERT_DEDUP_SECONDS = 1800

# Reap-reason codes that contribute to the aggregate reap_rate counter.
_REAP_REASON_EVENT_TYPES = frozenset(
    {
        "heartbeat_timeout_post_claim",
        "provision_timeout_pre_claim",
        "absolute_timeout",
        "never_started",
    }
)


def record_pipeline_event(event_type: str, **kwargs: Any) -> None:
    """Append a pipeline-health event and, if a threshold is crossed, fire an alert.

    Unknown event types are dropped silently so callers don't break the reaper
    on a typo — the snapshot endpoint will simply not expose them.
    """
    if event_type not in PIPELINE_EVENT_TYPES:
        return
    event = {
        "type": event_type,
        "ts": _utcnow(),
        "task_id": kwargs.get("task_id"),
        "vm_id": kwargs.get("vm_id"),
        "zone": kwargs.get("zone"),
        "account_id": kwargs.get("account_id"),
    }
    deploy_monitoring["pipeline_events"].append(event)
    _maybe_fire_pipeline_alert(event_type)


def count_pipeline_events(event_type: str, window_seconds: int = 3600) -> int:
    """Count events of `event_type` (or all reap reasons for "reap_rate_per_hour")
    within the trailing `window_seconds`."""
    cutoff = _utcnow() - timedelta(seconds=window_seconds)
    if event_type == "reap_rate_per_hour":
        return sum(
            1
            for e in deploy_monitoring["pipeline_events"]
            if e.get("ts")
            and e["ts"] >= cutoff
            and e.get("type") in _REAP_REASON_EVENT_TYPES
        )
    return sum(
        1
        for e in deploy_monitoring["pipeline_events"]
        if e.get("ts") and e["ts"] >= cutoff and e.get("type") == event_type
    )


def get_pipeline_health_snapshot() -> Dict[str, Any]:
    """Snapshot for /api/health/detailed: rolling counts, last-seen timestamps,
    thresholds, and which alert categories are currently above threshold."""
    counts: Dict[str, int] = {}
    last_event_ts: Dict[str, str | None] = {}
    for et in PIPELINE_EVENT_TYPES:
        counts[et] = count_pipeline_events(et)
        last_event_ts[et] = None
    counts["reap_rate_per_hour"] = count_pipeline_events("reap_rate_per_hour")

    for e in deploy_monitoring["pipeline_events"]:
        et = e.get("type")
        ts = e.get("ts")
        if et in PIPELINE_EVENT_TYPES and ts is not None:
            iso = ts.isoformat() if isinstance(ts, datetime) else str(ts)
            prev = last_event_ts.get(et)
            if prev is None or iso > prev:
                last_event_ts[et] = iso

    alerts_active = [
        et
        for et, threshold in PIPELINE_ALERT_THRESHOLDS.items()
        if counts.get(et, 0) >= threshold
    ]

    return {
        "counts_1h": counts,
        "last_event_ts": last_event_ts,
        "thresholds": dict(PIPELINE_ALERT_THRESHOLDS),
        "alerts_active": alerts_active,
        "held_directions": get_held_directions(),
    }


def _maybe_fire_pipeline_alert(event_type: str) -> None:
    """Check whether `event_type` (and the aggregate reap_rate) crossed
    threshold and post a deduplicated system alert if so.

    Posts via logger.error + best-effort webhook so operators can wire alerts
    without depending on the project-scoped Alert table.
    """
    candidates = [event_type]
    if event_type in _REAP_REASON_EVENT_TYPES:
        candidates.append("reap_rate_per_hour")

    for category in candidates:
        threshold = PIPELINE_ALERT_THRESHOLDS.get(category)
        if threshold is None:
            continue
        count = count_pipeline_events(category)
        if count < threshold:
            continue
        last_ts = deploy_monitoring["pipeline_alert_last_ts"].get(category)
        now = _utcnow()
        if last_ts and (now - last_ts).total_seconds() < PIPELINE_ALERT_DEDUP_SECONDS:
            continue
        deploy_monitoring["pipeline_alert_last_ts"][category] = now
        _post_pipeline_alert(category, count, threshold)


def _post_pipeline_alert(category: str, count: int, threshold: int) -> None:
    """Emit the alert via structured log + webhook. Failures here must never
    break the reaper, so all errors are swallowed with a warning."""
    title = f"Pipeline health threshold crossed: {category}"
    message = (
        f"{count} {category} events in the last hour "
        f"(threshold {threshold}). Inspect /api/health/detailed for context."
    )
    logger.error(
        title,
        extra={
            "alert": True,
            "type": "pipeline_health",
            "category": category,
            "count": count,
            "threshold": threshold,
        },
    )
    try:
        from app.main import _send_webhook_notification

        # Detached task — webhook is fire-and-forget by design.
        # asyncio.get_running_loop() raises RuntimeError outside an event loop;
        # that's the correct signal to skip dispatch (e.g. sync tests).
        loop = asyncio.get_running_loop()
        loop.create_task(  # noqa: RUF006
            _send_webhook_notification("PIPELINE_HEALTH_THRESHOLD", title, message)
        )
    except RuntimeError:
        pass
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[PIPELINE_HEALTH] webhook dispatch skipped: {e}")


# --- Direction-Level Failure Streak Tracking ---
# direction_id -> {
#   consecutive_failures: int,
#   last_failure_class: str,
#   last_failure_at: datetime,
#   held: bool,
#   held_at: Optional[datetime],
#   project_id: str,
# }
direction_failure_streaks: Dict[str, Dict] = {}

# Number of consecutive CODE_ERROR failures before a direction is held
DIRECTION_HOLD_THRESHOLD = 3
# Automatic hold expiry (safety valve so a direction doesn't stay blocked forever)
DIRECTION_HOLD_TIMEOUT_HOURS = 2

# _DIRECTION_INFRA_CLASSES: imported from failure_classification at module top-level.
# Infrastructure failures (RATE_LIMIT, PROVISION_FAILURE, AGENT_TIMEOUT,
# EXTERNAL_API, SCOPE_OVERFLOW) do NOT count toward the direction streak —
# they're platform noise, not the direction's fault. Account health +
# provisioning tracking handle them upstream.


def record_direction_result(
    direction_id: str,
    success: bool,
    failure_class: str = "",
    project_id: str = "",
) -> None:
    """Update the per-direction failure streak on task completion.

    Tracks two parallel counters: code failures (CODE_ERROR and other non-infra)
    and infra failures (PROVISION_FAILURE, RATE_LIMIT, AGENT_TIMEOUT, EXTERNAL_API,
    SCOPE_OVERFLOW). Each has its own hold threshold. A success resets both
    counters and clears any existing hold.
    """
    if not direction_id:
        return

    streak = direction_failure_streaks.setdefault(
        direction_id,
        {
            "consecutive_failures": 0,
            "consecutive_infra_failures": 0,
            "last_failure_class": "",
            "last_failure_at": None,
            "held": False,
            "held_at": None,
            "held_reason": "",
            "held_failure_class": "",
            "project_id": project_id,
        },
    )
    # Backfill new fields for entries created before infra tracking existed.
    streak.setdefault("consecutive_infra_failures", 0)
    streak.setdefault("held_reason", "")
    streak.setdefault("held_failure_class", "")
    if project_id:
        streak["project_id"] = project_id

    if success:
        streak["consecutive_failures"] = 0
        streak["consecutive_infra_failures"] = 0
        streak["last_failure_class"] = ""
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""
        return

    if failure_class in _DIRECTION_INFRA_CLASSES:
        # Infra failures (RATE_LIMIT, PROVISION_FAILURE, AGENT_TIMEOUT,
        # EXTERNAL_API, SCOPE_OVERFLOW) are not the direction's fault — they
        # happen at the pool / VM / third-party level. Counting them against
        # the direction wrongly holds innocent directions when the platform
        # has a bad hour. Account health + VM provisioning tracking handle
        # these upstream; the direction's streak stays unaffected.
        return

    streak["consecutive_failures"] += 1
    streak["last_failure_class"] = failure_class
    streak["last_failure_at"] = _utcnow()
    if (
        streak["consecutive_failures"] >= DIRECTION_HOLD_THRESHOLD
        and not streak["held"]
    ):
        streak["held"] = True
        streak["held_at"] = _utcnow()
        streak["held_reason"] = "code"
        streak["held_failure_class"] = failure_class
        logger.warning(
            f"[DIRECTION HEALTH] Direction {direction_id} HELD after "
            f"{streak['consecutive_failures']} consecutive {failure_class} failures."
        )


def clear_direction_hold(direction_id: str) -> None:
    """Explicitly clear a direction hold (e.g. after research_context update)."""
    streak = direction_failure_streaks.get(direction_id)
    if streak:
        streak["consecutive_failures"] = 0
        streak["consecutive_infra_failures"] = 0
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""


def get_direction_hold_info(direction_id: str) -> Dict[str, Any]:
    """Return hold metadata for a direction.

    Shape:
      {
        "held": bool,
        "reason": str,
        "held_at": Optional[datetime],
        "held_until": Optional[datetime],
      }

    Applies timeout auto-clear semantics identically to is_direction_held.
    """
    base = {"held": False, "reason": "", "held_at": None, "held_until": None}
    if not direction_id:
        return base

    streak = direction_failure_streaks.get(direction_id)
    if not streak or not streak.get("held"):
        return base

    held_at = streak.get("held_at")
    if (
        held_at
        and (_utcnow() - held_at).total_seconds() > DIRECTION_HOLD_TIMEOUT_HOURS * 3600
    ):
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""
        logger.info(
            f"[DIRECTION HEALTH] Direction {direction_id} hold expired (timeout)."
        )
        return base

    held_until = (
        held_at + timedelta(hours=DIRECTION_HOLD_TIMEOUT_HOURS) if held_at else None
    )
    if streak.get("held_reason") == "infra":
        count = streak.get("consecutive_infra_failures", 0)
        fc = streak.get("held_failure_class") or streak["last_failure_class"] or "INFRA"
        reason = (
            f"Held: direction has {count} consecutive infra failures "
            f"(last: {fc}) — investigate scope/scale before retrying"
        )
    else:
        count = streak["consecutive_failures"]
        fc = (
            streak.get("held_failure_class")
            or streak["last_failure_class"]
            or "CODE_ERROR"
        )
        reason = f"Held: direction has {count} consecutive {fc} failures — awaiting investigation"

    return {
        "held": True,
        "reason": reason,
        "held_at": held_at,
        "held_until": held_until,
    }


def is_direction_held(direction_id: str) -> tuple[bool, str]:
    """Return (held, reason) for the direction.

    Checks timeout expiry: if the hold has been active for more than
    DIRECTION_HOLD_TIMEOUT_HOURS the hold is auto-cleared and (False, '') is returned.
    Returns (False, '') for unknown directions.
    """
    info = get_direction_hold_info(direction_id)
    return bool(info["held"]), str(info["reason"])


def get_held_directions() -> Dict[str, Dict]:
    """Return snapshot of all currently held directions (after applying timeout logic)."""
    result = {}
    for did, streak in direction_failure_streaks.items():
        held, reason = is_direction_held(did)
        if held:
            result[did] = {
                "consecutive_failures": streak["consecutive_failures"],
                "consecutive_infra_failures": streak.get(
                    "consecutive_infra_failures", 0
                ),
                "last_failure_class": streak["last_failure_class"],
                "last_failure_at": streak["last_failure_at"].isoformat()
                if streak.get("last_failure_at")
                else None,
                "held_at": streak["held_at"].isoformat()
                if streak.get("held_at")
                else None,
                "held_reason": streak.get("held_reason", "code"),
                "project_id": streak.get("project_id", ""),
                "reason": reason,
            }
    return result


# Retention thresholds for sweep_stale_in_memory_state. Tuned so entries that
# stopped carrying signal a while ago are reclaimed, while anything still in
# use (recent failures, current holds, live sliding windows) is preserved.
_STREAK_RETENTION_HOURS = 24
_BREAKER_RETENTION_HOURS = 24
_MERGE_ERROR_RETENTION_SECONDS = MERGE_PATTERN_WINDOW_SECONDS * 2
_DEBUG_SPAWN_RETENTION_SECONDS = 3600 * 2


def sweep_stale_in_memory_state() -> Dict[str, int]:
    """Prune entries from growing module-level state dicts.

    Each of these dicts gets populated by a unique key (direction_id,
    project_id, normalized error, etc.) and was not being removed once the
    underlying condition resolved. Over an instance's lifetime the keyspace
    grows monotonically — on a long-lived Cloud Run instance this contributes
    to the 4GB OOMs. Callers invoke this on a periodic cadence.

    Returns a summary dict of how many entries were pruned per bucket.
    """
    now = _utcnow()
    pruned = {
        "direction_failure_streaks": 0,
        "circuit_breakers": 0,
        "autonomous_failure_counts": 0,
        "merge_error_occurrences": 0,
        "user_debug_spawn_times": 0,
        "vm_last_heartbeat": 0,
    }

    streak_cutoff = now - timedelta(hours=_STREAK_RETENTION_HOURS)
    for did, streak in list(direction_failure_streaks.items()):
        if streak.get("held"):
            continue
        if streak.get("consecutive_failures", 0) or streak.get(
            "consecutive_infra_failures", 0
        ):
            continue
        last = streak.get("last_failure_at")
        if last is None or last < streak_cutoff:
            del direction_failure_streaks[did]
            pruned["direction_failure_streaks"] += 1

    breaker_cutoff = now - timedelta(hours=_BREAKER_RETENTION_HOURS)
    for pid, breaker in list(circuit_breakers.items()):
        if breaker.get("state") != "CLOSED":
            continue
        if breaker.get("failure_count", 0) or breaker.get("infra_failure_count", 0):
            continue
        opened_at = breaker.get("opened_at")
        if opened_at is None or opened_at < breaker_cutoff:
            del circuit_breakers[pid]
            pruned["circuit_breakers"] += 1

    for pid, count in list(autonomous_failure_counts.items()):
        if not count:
            del autonomous_failure_counts[pid]
            pruned["autonomous_failure_counts"] += 1

    merge_cutoff = now - timedelta(seconds=_MERGE_ERROR_RETENTION_SECONDS)
    for key, dq in list(merge_error_occurrences.items()):
        while dq and dq[0][1] < merge_cutoff:
            dq.popleft()
        if not dq:
            del merge_error_occurrences[key]
            pruned["merge_error_occurrences"] += 1

    debug_cutoff = now - timedelta(seconds=_DEBUG_SPAWN_RETENTION_SECONDS)
    for pid, dq in list(user_debug_spawn_times.items()):
        while dq and dq[0] < debug_cutoff:
            dq.popleft()
        if not dq:
            del user_debug_spawn_times[pid]
            pruned["user_debug_spawn_times"] += 1

    # Warm-VM heartbeat map: prune entries older than 1h. The orchestrator
    # only honors warm-pin VMs whose heartbeat is within 90s, so any entry
    # past 1h is permanently stale and only contributes to memory growth.
    vm_heartbeat_cutoff = now - timedelta(hours=1)
    for vm_id, last in list(vm_last_heartbeat.items()):
        if last < vm_heartbeat_cutoff:
            del vm_last_heartbeat[vm_id]
            pruned["vm_last_heartbeat"] += 1

    # Warm-pin clear-out events: bounded deque (maxlen=200) auto-evicts;
    # this prune pass drops anything outside the 24h reporting window so
    # count_recent_warm_pin_clears stays cheap.
    warm_clear_cutoff = now - timedelta(hours=24)
    pruned["warm_pin_clear_events"] = 0
    while warm_pin_clear_events and warm_pin_clear_events[0][0] < warm_clear_cutoff:
        warm_pin_clear_events.popleft()
        pruned["warm_pin_clear_events"] += 1

    # Backlog first-refusal: entries are short-lived (≈20s); drop anything
    # already expired so the map can't grow on a long-lived instance.
    pruned["warm_backlog_first_refusal"] = 0
    for pid, deadline in list(warm_backlog_first_refusal.items()):
        if deadline < now:
            del warm_backlog_first_refusal[pid]
            pruned["warm_backlog_first_refusal"] += 1

    # Backlog reuse failure events: bounded deque (maxlen=200) auto-evicts;
    # prune events older than 24h so count_recent_backlog_reuse_failures stays cheap.
    backlog_failure_cutoff = now - timedelta(hours=24)
    pruned["warm_backlog_reuse_failure_events"] = 0
    while (
        warm_backlog_reuse_failure_events
        and warm_backlog_reuse_failure_events[0][0] < backlog_failure_cutoff
    ):
        warm_backlog_reuse_failure_events.popleft()
        pruned["warm_backlog_reuse_failure_events"] += 1

    return pruned


def get_task_type_failure_rate(
    task_type: str, window_minutes: int = 15
) -> tuple[float, int]:
    """Compute failure rate for a specific task_type within the trailing window.

    Backward-compatible: entries without a task_type field (old format or empty
    string) are treated as matching any type filter and included in the sample.

    Returns (rate, sample_size).  Returns (0.0, 0) when no samples exist.
    """
    cutoff = (_utcnow() - timedelta(minutes=window_minutes)).isoformat()
    relevant = [
        r
        for r in deploy_monitoring["task_results"]
        if r[0] >= cutoff and (len(r) < 4 or not r[3] or r[3] == task_type)
    ]
    if not relevant:
        return 0.0, 0
    failures = sum(1 for r in relevant if r[1] == "failure")
    return failures / len(relevant), len(relevant)


# --- Prometheus-compatible metrics counters ---
# Gauges: updated by orchestrator each cycle
tasks_queued_gauge: int = 0
tasks_active_gauge: int = 0
# Counters: cumulative totals, incremented on terminal state transitions
tasks_completed_total: int = 0
tasks_failed_total: int = 0

# --- External Dependency Health ---
external_health: Dict = {
    "github_api": True,
    "anthropic_api": True,
    "openai_api": True,
    "gemini_api": True,
    "dispatch_api": True,
    "gcp": True,
    "db": True,
    "provider_health": {},  # provider_name -> structured provider health snapshot
    "dispatch_health": {},  # aggregate dispatchability summary
    "last_check": None,  # datetime of last health check
    "failed_deps": [],  # list of currently-failed dependency names
    "errors": {},  # dep_name -> last error message
    "consecutive_failures": {},  # dep_name -> int count of consecutive failed health checks
}

# --- GitHub API Quota Tracking ---
# Populated on every _github_request by reading X-RateLimit-Remaining /
# X-RateLimit-Reset headers off the response. Surfaced in /api/health/detailed
# and drives the dashboard quota widget + WARNING alert when remaining < 100.
# Treat None values as "not yet observed — no recent GitHub call".
github_quota: Dict = {
    "remaining": None,  # int, lowest remaining seen since last reset
    "limit": None,  # int, 5000 for GitHub App installs
    "reset_at": None,  # int unix ts, when quota resets
    "last_updated": None,  # isoformat str, last observation
}

# Separate bucket for App-JWT (~60/hr). Used by /app/installations/*/access_tokens
# and other /app/* endpoints that authenticate with a GitHub App JWT rather than
# an installation token. Tracked separately so we don't mix the two — a drained
# JWT bucket stalls token minting (which in turn stalls everything else), and we
# want to short-circuit calls to that bucket when it's empty instead of retrying
# and counting three 403s per logical request.
github_jwt_quota: Dict = {
    "remaining": None,
    "limit": None,
    "reset_at": None,
    "last_updated": None,
}

# --- GitHub REST Call Counters ---
# Every _github_request bumps the count for its normalized endpoint template
# (e.g. "GET /repos/{owner}/{repo}/pulls/{n}") so we can see which endpoints
# actually consume the 5000/hr REST budget. Reset on each quota-reset window
# rollover so the dashboard shows "this window"'s burn. Template normalization
# (numbers, SHAs, branch refs) collapses the natural cardinality explosion.
github_call_counts: Dict[str, int] = {}
github_call_counts_window_reset_at: int | None = None

# Cache-hit counters — exposed in /api/health/detailed so we can see whether
# the dedup caches are actually firing in production.  Each entry here is a
# request we didn't have to send to GitHub.  Reset on the same window as
# github_call_counts so "hits per window" is directly comparable to "calls
# per window" — if hits dominate, the caches are earning their keep.
github_cache_hits: Dict[str, int] = {
    "check_runs": 0,
    "scan_pr": 0,
    "pr_merge_negative": 0,
    "bucket_short_circuit": 0,
}

# Track last successful GitHub API operation (tokens, PR comments, etc.)
# Used by health check to passively verify GitHub is reachable without
# making an active API call that competes for the shared connection pool.
# Seed with current time so the first health check uses the passive path
# instead of an active probe that can fail on cold start.
last_github_success: datetime | None = (
    datetime.utcnow()
)  # Module-level init, _utcnow not available here


# --- Dispatcher Cycle Log (ring buffer for throughput/gating observability) ---
# Bounded to the last 1000 records; entries older than 30 days are excluded by
# get_dispatcher_cycles() callers but not proactively evicted (deque handles FIFO).
# Each record is a plain dict so it stays JSON-serialisable without extra deps.
_DISPATCHER_CYCLE_LOG_MAXLEN = 1000
_DISPATCHER_CYCLE_TTL_DAYS = 30

dispatcher_cycle_log: deque = deque(maxlen=_DISPATCHER_CYCLE_LOG_MAXLEN)


def record_dispatcher_cycle(
    *,
    project_id: str,
    cycle_started_at: datetime,
    cycle_finished_at: datetime,
    signals_evaluated: int,
    signals_tripped: int,
    spawns_emitted: int,
    rejections_by_reason: Dict[str, int] | None = None,
    mttd_p50_sec: float | None = None,
) -> None:
    """Append one dispatcher cycle record to the ring buffer.

    Called at the end of each pipeline_regression_dispatcher.run_once() tick.
    The deque maxlen caps memory; get_dispatcher_cycles() applies the 30-day TTL.
    """
    dispatcher_cycle_log.append(
        {
            "project_id": project_id,
            "cycle_started_at": cycle_started_at.isoformat(),
            "cycle_finished_at": cycle_finished_at.isoformat(),
            "signals_evaluated": signals_evaluated,
            "signals_tripped": signals_tripped,
            "spawns_emitted": spawns_emitted,
            "rejections_by_reason": dict(rejections_by_reason or {}),
            "mttd_p50_sec": mttd_p50_sec,
        }
    )


def get_dispatcher_cycles(
    project_id: str | None = None,
    window_hours: int = 24,
) -> list[Dict]:
    """Return dispatcher cycle records within the trailing window.

    Filters to `project_id` when provided; returns all projects otherwise.
    Records older than `window_hours` are excluded.
    """
    cutoff = (_utcnow() - timedelta(hours=window_hours)).isoformat()
    return [
        r
        for r in dispatcher_cycle_log
        if r.get("cycle_started_at", "") >= cutoff
        and (project_id is None or r.get("project_id") == project_id)
    ]


# --- Reaper Cycle-Level Halt State Access ---
# The authoritative halt state lives in reaper._reaper_halt_state (module-local
# to avoid a circular import — reaper.py already imports from shared_state.py).
# These helpers provide cross-module access via lazy imports so admin routers
# and the metrics endpoint can read and reset the halt state without importing
# reaper.py directly.


def get_reaper_halt_state() -> Dict[str, Any]:
    """Snapshot of the reaper's cycle-level circuit breaker state.

    Lazily imports from app.reaper to avoid circular imports at module load.
    Returns a safe default dict when the reaper module is not yet imported.
    """
    try:
        import app.reaper as _reaper_mod  # noqa: PLC0415

        state = _reaper_mod._reaper_halt_state
        ts = state.get("halt_timestamp")
        return {
            "halted": bool(state.get("halted")),
            "halt_timestamp": ts.isoformat() if ts else None,
            "last_error": state.get("last_error"),
            "crash_count": state.get("crash_count", 0),
            "hard_halted": bool(state.get("hard_halted")),
            "half_open_failures": state.get("half_open_failures", 0),
        }
    except Exception:  # noqa: BLE001
        return {
            "halted": False,
            "halt_timestamp": None,
            "last_error": None,
            "crash_count": 0,
            "hard_halted": False,
            "half_open_failures": 0,
        }


def reset_reaper_halt_state() -> bool:
    """Reset the reaper cycle-level circuit breaker (operator manual reset).

    Clears the in-memory halt state so the reaper service resumes on its next
    iteration.  Returns True if the reaper was halted and is now cleared,
    False if it was not halted.
    """
    try:
        import app.reaper as _reaper_mod  # noqa: PLC0415

        state = _reaper_mod._reaper_halt_state
        if not state.get("halted"):
            return False
        state.update(
            {
                "halted": False,
                "halt_timestamp": None,
                "last_error": None,
                "crash_count": 0,
                "hard_halted": False,
                "half_open_failures": 0,
            }
        )
        logger.info("[REAPER] Halt state cleared by operator reset.")
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[REAPER] reset_reaper_halt_state failed: {e}")
        return False
