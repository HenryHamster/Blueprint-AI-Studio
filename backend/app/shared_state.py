"""Shared runtime state used by both main.py and admin.py.

Extracted to its own module to avoid circular imports when admin
endpoints need to read or mutate autonomous-loop / runtime-config state.
"""

import os
import asyncio
import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from slowapi import Limiter
from slowapi.util import get_remote_address

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


def scope_to_effort(scope: Optional[str]) -> str:
    """Return the baseline effort for a calibrated scope.

    Unknown / missing scopes fall back to "medium" so callers can safely take
    the max against other effort signals without suppressing them.
    """
    return _SCOPE_EFFORT_MAP.get(scope or "", "medium")

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
    "max_auto_pause_duration_seconds": int(os.getenv("MAX_AUTO_PAUSE_DURATION_SECONDS", "7200")),
    "auto_resume_grace_period_seconds": int(os.getenv("AUTO_RESUME_GRACE_PERIOD_SECONDS", "300")),
    # Flap detection: N auto-resumes inside `flap_window_seconds` escalates
    # (ERROR alert, raised recovery bar, doubled grace period).
    "flap_window_seconds": int(os.getenv("FLAP_WINDOW_SECONDS", "1800")),
    "flap_threshold": int(os.getenv("FLAP_THRESHOLD", "3")),
    "max_retries": int(os.getenv("MAX_RETRIES", "3")),
    "max_provision_retries": int(os.getenv("MAX_PROVISION_RETRIES", "3")),
    "max_rollup_retries": int(os.getenv("MAX_ROLLUP_RETRIES", "5")),
    "queued_age_alert_hours": int(os.getenv("QUEUED_AGE_ALERT_HOURS", "2")),
    "retry_after_reset_alert_threshold": int(os.getenv("RETRY_AFTER_RESET_ALERT_THRESHOLD", "8")),
    "retry_after_reset_fail_threshold": int(os.getenv("RETRY_AFTER_RESET_FAIL_THRESHOLD", "16")),
    "max_autonomous_tasks_per_hour": 12,
    "max_global_autonomous_tasks_per_hour": 50,  # Cross-project cap (default 10x per-project)
    "autonomous_research_interval_minutes": 30,
    # Long-EVALUATING parents stop blocking new research on the same direction
    # once their pipeline-side merge cycle exceeds this threshold. Set above
    # the typical PR lifecycle (CI + review + fix + re-review + merge) so new
    # research only spawns when a parent is genuinely stuck, not just slow.
    "evaluating_pipeline_timeout_minutes": int(os.getenv("EVALUATING_PIPELINE_TIMEOUT_MINUTES", "120")),
    # Hard kill for EVALUATING tasks waiting on CI/review verdict. Tuned above
    # a single review-fix iteration (5-15 min agent + queueing) so multi-pass
    # cycles don't trigger a premature kill of in-flight children.
    "ci_backstop_timeout_minutes": int(os.getenv("CI_BACKSTOP_TIMEOUT_MINUTES", "60")),
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
        "_default": {"providers": ["anthropic", "openai", "gemini"], "effort": "medium"},
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
            "low": {"model": "gpt-5.4-mini", "reasoning_effort": "low", "verbosity": "low"},
            "medium": {"model": "gpt-5.4-mini", "reasoning_effort": "medium", "verbosity": "low"},
            "high": {"model": "gpt-5.4", "reasoning_effort": "high", "verbosity": "medium"},
            "max": {"model": "gpt-5.4", "reasoning_effort": "xhigh", "verbosity": "high"},
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
        "qa": {"interval_hours": 72, "enabled": False},               # every 3 days
        "deps": {"interval_hours": 168, "enabled": False},            # weekly
    },
    "autonomous_direction_cooldown_minutes": 240,
    "productive_direction_cooldown_minutes": 45,
    "direction_auto_recovery_hours": 48,
    "strategic_research_frequency": 6,  # Every Nth research cycle is a strategic vision review
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
    # Per-scope heartbeat-stale thresholds (seconds). Large-scope tasks
    # legitimately run longer stretches without a heartbeat, so reaping at the
    # small-scope bar flags them as dead while they're still making progress.
    "heartbeat_stale_seconds_by_scope": {
        "small": 300,
        "medium": 420,
        "large": 900,
    },
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
                members = legacy_categories.get(cat) if isinstance(legacy_categories, dict) else None
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
                (legacy_pref.get("_default") if isinstance(legacy_pref, dict) else None)
                or ["anthropic", "openai", "gemini"]
            )
            cat_routing["_default"] = {"providers": list(default_providers), "effort": default_effort}
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
    from app.models import DeploymentStatus  # noqa: PLC0415

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
                _set("max_autonomous_tasks_per_hour", int(state.runtime_max_autonomous_tasks_per_hour))
            if getattr(state, "runtime_max_global_autonomous_tasks_per_hour", None) is not None:
                _set("max_global_autonomous_tasks_per_hour", int(state.runtime_max_global_autonomous_tasks_per_hour))
        except Exception as e:
            logger.warning(f"[CONFIG-SYNC] Could not sync scalar config cols: {e}")

        try:
            if state.runtime_autonomous_research_interval_minutes is not None:
                _set("autonomous_research_interval_minutes", int(state.runtime_autonomous_research_interval_minutes))
            if getattr(state, "runtime_autonomous_direction_cooldown_minutes", None) is not None:
                _set("autonomous_direction_cooldown_minutes", int(state.runtime_autonomous_direction_cooldown_minutes))
        except Exception as e:
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
                    "provider_settings_defaults", "task_type_provider_overrides",
                    "autonomous_periodic_tasks", "health_check_urls", "notification_config",
                ):
                    if key in config_json:
                        _set(key, config_json[key])
                if "strategic_research_frequency" in config_json:
                    _set("strategic_research_frequency", int(config_json["strategic_research_frequency"]))
                if "require_approval_gates" in config_json:
                    _set("require_approval_gates", bool(config_json["require_approval_gates"]))
                if "daily_cost_ceiling_usd" in config_json:
                    _set("daily_cost_ceiling_usd", float(config_json["daily_cost_ceiling_usd"]))
                if "auto_archive_enabled" in config_json:
                    _set("auto_archive_enabled", bool(config_json["auto_archive_enabled"]))
                for int_key in (
                    "max_provision_retries", "max_rollup_retries",
                    "architecture_review_frequency", "max_active_directions",
                    "max_parallel_research_per_project",
                    "auto_archive_terminal_after_days",
                    "auto_archive_max_terminal_tasks_per_project",
                    "auto_archive_interval_minutes",
                    "max_pipeline_slots",
                    "evaluating_pipeline_timeout_minutes",
                    "ci_backstop_timeout_minutes",
                ):
                    if int_key in config_json:
                        _set(int_key, int(config_json[int_key]))
                if "reserved_slots_for_types" in config_json:
                    _set("reserved_slots_for_types", config_json["reserved_slots_for_types"])
        except Exception as e:
            logger.warning(f"[CONFIG-SYNC] Could not sync runtime_config_json: {e}")

        # PAUSED/HALTED always forces the dispatch cap to 0. (We already
        # skipped the restore from runtime_max_concurrent_vms above when
        # paused, so this just zeros the in-memory value.)
        try:
            if paused:
                _set("max_concurrent_vms", 0)
        except Exception as e:
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
    from app.database import AsyncSessionLocal  # noqa: PLC0415
    from app.models import DeploymentState  # noqa: PLC0415

    while True:
        try:
            await asyncio.sleep(15)
            async with AsyncSessionLocal() as session:
                state = await session.get(DeploymentState, 1)
                changed = await sync_runtime_config_from_state(state)
                if changed:
                    logger.info(f"[CONFIG-SYNC] Applied DB changes: {changed}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[CONFIG-SYNC] sync cycle failed: {e}", exc_info=True)


def resolve_retry_budget(failure_class: str, scope: Optional[str]) -> int:
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
last_idle_check: Optional[datetime] = None

# --- Persistent Anomaly Tracking ---
# project_id -> { anomaly_key -> {first_seen, last_seen, occurrence_count, severity, latest_details} }
persistent_anomalies: Dict[str, Dict[str, Dict]] = {}

# --- Anomaly-Driven Pipeline Adjustments ---
# When high-severity anomalies (PROVISION_FAILURE, EXTERNAL_API) persist,
# the system automatically reduces blast radius by lowering max_concurrent_vms
# and tightening the failure-rate dispatch gate.
anomaly_adjustments: Dict[str, Any] = {
    "active": False,
    "adjusted_at": None,             # ISO timestamp when adjustments were applied
    "trigger_anomalies": [],         # anomaly keys that triggered the adjustment
    "pre_adjustment_max_vms": None,  # stored to restore on clearing
    "pre_adjustment_failure_gate": None,
    "failure_gate_threshold": 0.8,   # default; lowered to 0.6 during anomaly response
    "forced_restoration_count": 0,   # cumulative count of max-duration forced restorations
    "forced_restored_at": None,      # ISO timestamp of last forced restoration
    "adjusted_max_vms": None,        # target max_concurrent_vms we set; used to detect deploy_health/admin overrides
}

# --- Circuit Breaker (Hystrix-inspired) ---
# Per-project circuit breaker state for autonomous task spawning
# States: CLOSED (normal), OPEN (tripped), HALF_OPEN (testing one task)
circuit_breakers: Dict[str, Dict] = {}  # project_id -> breaker state

def get_circuit_breaker(project_id: str) -> Dict:
    """Get or create circuit breaker state for a project."""
    if project_id not in circuit_breakers:
        circuit_breakers[project_id] = {
            "state": "CLOSED",           # CLOSED, OPEN, HALF_OPEN
            "failure_count": 0,          # consecutive code-quality failures (CODE_ERROR, AGENT_TIMEOUT)
            "infra_failure_count": 0,    # consecutive infra failures (PROVISION_FAILURE, RATE_LIMIT, EXTERNAL_API)
            "success_count": 0,          # consecutive successes (for half-open)
            "opened_at": None,           # when the breaker tripped
            "cooldown_seconds": 1800,    # 30 min initial cooldown
            "base_cooldown": 1800,       # for exponential backoff reset
            "half_open_task_id": None,   # the test task in HALF_OPEN
            "open_reason": None,         # "code" or "infra" — why the breaker opened
        }
    return circuit_breakers[project_id]

# --- User-submitted Auto-Debug Rate Limiting ---
# Tracks timestamps of user-submitted debug spawns per project to enforce hourly cap.
# Structure: project_id -> deque of datetime (UTC) of recent spawns
user_debug_spawn_times: Dict[str, deque] = {}

USER_DEBUG_MAX_PER_HOUR = 3  # max debug tasks spawned for user/admin tasks per project per hour

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


# --- Background Tasks (populated by lifespan, used by admin restart endpoint) ---
background_tasks: Dict[str, asyncio.Task] = {}  # name -> asyncio.Task

# --- Deployment Monitoring (Phase A: detect + alert) ---
deploy_monitoring: Dict = {
    "window_start": None,           # datetime when monitoring began for current deploy
    "window_duration": 900,         # 15 minutes default
    "extended_window": 1800,        # 30 min for large/infra changes
    "is_extended": False,           # set True when infra files are touched
    "task_results": deque(maxlen=100),  # (timestamp_iso, "success"|"failure", failure_class, task_type)
    "pre_deploy_failure_rate": 0.0, # baseline from before deploy
    "health_checks": {
        "dispatch_working": True,   # orchestrator picked up a task in last 5 min
        "completions_received": True, # at least one agent reported back
        "db_accessible": True,       # can read/write tasks
    },
    "last_dispatch_time": None,     # last time orchestrator dispatched a task
    "last_completion_time": None,   # last time an agent reported completion
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
PIPELINE_EVENT_TYPES = frozenset({
    "heartbeat_timeout_post_claim",
    "provision_timeout_pre_claim",
    "absolute_timeout",
    "never_started",
    "account_blacklisted",
})

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
_REAP_REASON_EVENT_TYPES = frozenset({
    "heartbeat_timeout_post_claim",
    "provision_timeout_pre_claim",
    "absolute_timeout",
    "never_started",
})


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
            1 for e in deploy_monitoring["pipeline_events"]
            if e.get("ts") and e["ts"] >= cutoff and e.get("type") in _REAP_REASON_EVENT_TYPES
        )
    return sum(
        1 for e in deploy_monitoring["pipeline_events"]
        if e.get("ts") and e["ts"] >= cutoff and e.get("type") == event_type
    )


def get_pipeline_health_snapshot() -> Dict[str, Any]:
    """Snapshot for /api/health/detailed: rolling counts, last-seen timestamps,
    thresholds, and which alert categories are currently above threshold."""
    counts: Dict[str, int] = {}
    last_event_ts: Dict[str, Optional[str]] = {}
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
        et for et, threshold in PIPELINE_ALERT_THRESHOLDS.items()
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
        extra={"alert": True, "type": "pipeline_health", "category": category,
               "count": count, "threshold": threshold},
    )
    try:
        from app.main import _send_webhook_notification  # noqa: PLC0415
        # Detached task — webhook is fire-and-forget by design.
        # asyncio.get_running_loop() raises RuntimeError outside an event loop;
        # that's the correct signal to skip dispatch (e.g. sync tests).
        loop = asyncio.get_running_loop()
        loop.create_task(
            _send_webhook_notification("PIPELINE_HEALTH_THRESHOLD", title, message)
        )
    except RuntimeError:
        pass
    except Exception as e:
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

    streak = direction_failure_streaks.setdefault(direction_id, {
        "consecutive_failures": 0,
        "consecutive_infra_failures": 0,
        "last_failure_class": "",
        "last_failure_at": None,
        "held": False,
        "held_at": None,
        "held_reason": "",
        "held_failure_class": "",
        "project_id": project_id,
    })
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
    if streak["consecutive_failures"] >= DIRECTION_HOLD_THRESHOLD and not streak["held"]:
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
    if held_at and (_utcnow() - held_at).total_seconds() > DIRECTION_HOLD_TIMEOUT_HOURS * 3600:
        streak["held"] = False
        streak["held_at"] = None
        streak["held_reason"] = ""
        streak["held_failure_class"] = ""
        logger.info(f"[DIRECTION HEALTH] Direction {direction_id} hold expired (timeout).")
        return base

    held_until = held_at + timedelta(hours=DIRECTION_HOLD_TIMEOUT_HOURS) if held_at else None
    if streak.get("held_reason") == "infra":
        count = streak.get("consecutive_infra_failures", 0)
        fc = streak.get("held_failure_class") or streak["last_failure_class"] or "INFRA"
        reason = (
            f"Held: direction has {count} consecutive infra failures "
            f"(last: {fc}) — investigate scope/scale before retrying"
        )
    else:
        count = streak["consecutive_failures"]
        fc = streak.get("held_failure_class") or streak["last_failure_class"] or "CODE_ERROR"
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
                "consecutive_infra_failures": streak.get("consecutive_infra_failures", 0),
                "last_failure_class": streak["last_failure_class"],
                "last_failure_at": streak["last_failure_at"].isoformat() if streak.get("last_failure_at") else None,
                "held_at": streak["held_at"].isoformat() if streak.get("held_at") else None,
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
    }

    streak_cutoff = now - timedelta(hours=_STREAK_RETENTION_HOURS)
    for did, streak in list(direction_failure_streaks.items()):
        if streak.get("held"):
            continue
        if streak.get("consecutive_failures", 0) or streak.get("consecutive_infra_failures", 0):
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

    return pruned


def get_task_type_failure_rate(task_type: str, window_minutes: int = 15) -> tuple[float, int]:
    """Compute failure rate for a specific task_type within the trailing window.

    Backward-compatible: entries without a task_type field (old format or empty
    string) are treated as matching any type filter and included in the sample.

    Returns (rate, sample_size).  Returns (0.0, 0) when no samples exist.
    """
    cutoff = (_utcnow() - timedelta(minutes=window_minutes)).isoformat()
    relevant = [
        r for r in deploy_monitoring["task_results"]
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
    "provider_health": {},          # provider_name -> structured provider health snapshot
    "dispatch_health": {},          # aggregate dispatchability summary
    "last_check": None,             # datetime of last health check
    "failed_deps": [],              # list of currently-failed dependency names
    "errors": {},                   # dep_name -> last error message
    "consecutive_failures": {},     # dep_name -> int count of consecutive failed health checks
}

# --- GitHub API Quota Tracking ---
# Populated on every _github_request by reading X-RateLimit-Remaining /
# X-RateLimit-Reset headers off the response. Surfaced in /api/health/detailed
# and drives the dashboard quota widget + WARNING alert when remaining < 100.
# Treat None values as "not yet observed — no recent GitHub call".
github_quota: Dict = {
    "remaining": None,              # int, lowest remaining seen since last reset
    "limit": None,                  # int, 5000 for GitHub App installs
    "reset_at": None,               # int unix ts, when quota resets
    "last_updated": None,           # isoformat str, last observation
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
last_github_success: Optional[datetime] = datetime.utcnow()  # Module-level init, _utcnow not available here
