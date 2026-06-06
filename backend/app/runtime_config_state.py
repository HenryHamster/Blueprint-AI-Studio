"""Runtime config + per-class retry budgets (split out of shared_state).

This module owns the process-global ``runtime_config`` dict, its update lock,
the release-promotion policy seed, the legacy-config migration helper, the
DB→memory sync routines, and the per-failure-class retry-budget resolution.

It is imported back into :mod:`app.shared_state` as a thin facade so the many
existing importers (``from app.shared_state import runtime_config``, etc.) keep
working unchanged. Behaviour is byte-identical to the pre-split code.
"""

import os
import asyncio
import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

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
    # Co-change auto-decomposition: split a medium research finding's files into
    # independent co-change/dir clusters and spawn one small sub-finding per
    # cluster (research_completion -> cochange_decomposition.py). Default OFF:
    # live data (2026-06-03) showed 0/14 sub-findings merged while ~3x-ing task
    # fan-out, and the per-direction self-suppressor only learns reactively.
    # Flip on (settable via PATCH /api/admin/settings, no deploy) to re-enable
    # globally. NOTE: this gates only the splitting; the cochange_index signal
    # that also feeds scope_complexity scoring + agent-prompt hints is untouched.
    "cochange_decomposition_enabled": os.getenv(
        "COCHANGE_DECOMPOSITION_ENABLED", "false"
    ).lower()
    in ("1", "true", "yes"),
    # Forward-quality grader for research findings. When True, a research task
    # whose findings would SPAWN downstream implementation work is gated on an
    # INDEPENDENT grader (task_evaluators.spawn_forward_quality_grader): the
    # research task parks EVALUATING, a fresh grader agent scores the findings
    # against a rubric, and only a PASS verdict lets the findings spawn children
    # (a FAIL fails the research task and spawns nothing). Ships DARK (default
    # OFF) — flip on (settable via PATCH /api/admin/settings, no deploy) behind
    # spawn-quality monitoring. OFF preserves the legacy trust-the-findings
    # spawn path exactly. Findings that would spawn nothing are never escalated
    # regardless of this flag (the gate is proportional to blast radius).
    "research_quality_grader_enabled": os.getenv(
        "RESEARCH_QUALITY_GRADER_ENABLED", "false"
    ).lower()
    in ("1", "true", "yes"),
    # Same forward-quality escalation for security_audit findings: a critical/high
    # audit finding spawns code-change (create) PRs, the same blast radius as a
    # research finding, so the audit output must clear an INDEPENDENT grader before
    # any fix tasks spawn. Ships DARK (default OFF) — OFF preserves the legacy
    # trust-the-findings spawn path exactly. Audits with no critical/high findings
    # are never escalated regardless of this flag (the gate is proportional to
    # blast radius).
    "security_audit_quality_grader_enabled": os.getenv(
        "SECURITY_AUDIT_QUALITY_GRADER_ENABLED", "false"
    ).lower()
    in ("1", "true", "yes"),
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
    # No-CI test-suite merge gate kill-switch. When True (default), a PR on an
    # EXTERNAL no-CI project (build_config.requires_ci_checks=False) targeting a
    # non-staging base is gated on a verify_tests verdict — the repo's detected
    # suite is run on the PR head and must pass before the merge proceeds.
    # Set False to fall back to the legacy immediate-pass (merge unverified on a
    # definitive no_checks) without a deploy if the gate misbehaves. Repos with
    # genuinely no detectable test command, and internal staging rollups, keep
    # the immediate-pass regardless of this flag.
    "no_ci_test_gate_enabled": os.getenv("NO_CI_TEST_GATE_ENABLED", "true").lower()
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
    # Recovery-sweep window for DELIBERATE spend/budget approval holds
    # (tasks held with requires_approval=True AND
    # context.approval_hold_reason="spend_budget"). Distinct from the review-
    # escalation 2h hard-FAIL: an out-of-band cost decision an operator simply
    # has not reached is not a pipeline failure, so this window is longer and,
    # on expiry, resolves to a CLEAN NON-FAILURE CANCELLED terminal (not FAILED).
    # Default 24h; PATCH endpoint validates 1..168.
    "spend_approval_timeout_hours": int(
        os.getenv("SPEND_APPROVAL_TIMEOUT_HOURS", "24")
    ),
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
            "opus": "claude-opus-4-8",
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
            "high": {"model": "claude-opus-4-8", "effort": "high"},
            "max": {"model": "claude-opus-4-8", "effort": "max"},
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
    # Budget on the strategist's autonomous spawn_task path (research / plan /
    # web_research / design tasks created directly from a strategic review). Each
    # such task jumps the queue at elevated priority and can carry real per-job
    # cost, so an unbounded review could flood the pipeline. These caps are sized
    # generously so normal steering never trips them — they exist solely to
    # contain runaway spawning. per_review caps one strategic review; window caps
    # the rolling time window across consecutive reviews; dedup_overlap_threshold
    # is the word-overlap count above which a new spawn title is treated as a
    # semantic duplicate of a recent same-type spawn (must stay >=4 so distinct
    # investigations like "Investigate A"/"Investigate B" are not collapsed).
    "strategist_spawn_per_review_cap": 12,
    "strategist_spawn_window_minutes": 60,
    "strategist_spawn_window_cap": 20,
    "strategist_spawn_dedup_overlap_threshold": 4,
    "max_active_directions": 10,
    # Staleness threshold (days) for staged directions whose target version has
    # NOT yet promoted. A staged direction is inert work that only begins when
    # its target version opens; a version can stay open far longer than the
    # ~14-day build-to-stabilize gate (deferred by repeated qualification
    # failures or operator inaction), stranding staged work indefinitely. When a
    # staged direction has waited beyond this age (anchored on its staging
    # timestamp), the reaper raises a per-direction operator alert so the work
    # can be retargeted, force-activated, or cancelled. Default 21 (> the
    # ~14-day stall gate so a normally-stabilizing version never trips it).
    "staged_direction_stale_alert_days": 21,
    # Fail-safe age (days) applied when a staged direction is missing or has an
    # unparseable staging timestamp, so a malformed/legacy staged row can still
    # be surfaced as stale rather than waiting silently forever.
    "staged_direction_stale_fallback_days": 21,
    "max_parallel_research_per_project": 6,
    "max_retained_images": 10,  # Agent VM image family pruning (keep N most recent; GCP per-project image quota is tight)
    "require_approval_gates": True,
    "daily_cost_ceiling_usd": 0,  # 0 = disabled; positive float enables budget alerts
    # Proposed compute_experiment spend cap (USD) above which an autonomously
    # authored launch is HELD for operator approval (requires_approval=True at
    # spawn) rather than dispatched. A per-direction override is honored when a
    # direction dict carries its own "compute_experiment_spend_threshold_usd"
    # key (directions are flat JSON dicts that already hold per-direction floats
    # like cost_budget_usd — no new column/migration). This sits ABOVE the hard
    # global clamp in compute_jobs.py (MAX_COMPUTE_JOB_SPEND_CAP_USD), which
    # remains an untouched backstop. <=0 means hold every positive-cap launch.
    "compute_experiment_spend_threshold_usd": 10.0,
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
    # Auto-debug spawn kill-switch (#8528): gates should_auto_debug in
    # agent_status.py. False stops Debug:/Fix: replacement spawns fleet-wide —
    # the storm brake for usage-exhaustion nights where debug children burn
    # VMs re-diagnosing infra deaths.
    "auto_debug_enabled": True,
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
    # Per-cycle orphan-processing cap. Each orphan costs serial GitHub/VM I/O
    # (~30-120s), so a large backlog can blow past the reaper liveness watchdog
    # window even with intra-cycle heartbeats. The reaper processes the highest-
    # priority orphans (rate-limited > spot > provision > other) up to this cap;
    # the rest re-qualify next cycle via the status-based Step-1 scan. Default 60
    # matches reaper._REAPER_MAX_ORPHANS_PER_CYCLE_DEFAULT.
    "reaper_max_orphans_per_cycle": 60,
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
    # Warm-VM POOL (efficiency direction d-5fd69fe1): master switch for the
    # pre-provisioned, credentialed, idle-VM pool. When True the orchestrator
    # may cold-boot taskless pool VMs (compute.provision_pool_vm), register them
    # in the PoolVm table, and lease an IDLE compatible member to a newly-queued
    # task instead of cold-provisioning. Defaults to False so ALL warm-pool code
    # (provisioning entrypoint, VM-scoped tokens, registry leasing) is INERT in
    # production until the full pool machinery (maintainer + dispatch hook,
    # sibling tasks) lands and the flag is flipped. Independently switchable from
    # warm_continuity_enabled / warm_backlog_reuse_enabled.
    "warm_pool_enabled": False,
    # Account-agnostic lease: when True, a same-tenant IDLE pool member may be
    # leased for a task whose resolved account (and model) differ from the
    # member's frozen boot account, and the warm-claim path re-materializes the
    # resolved account's credentials before the role runs. Lets ONE small pool
    # serve a project's whole account mix instead of needing a member per
    # account. Default False (strict per-account equality). Cross-PROVIDER
    # retarget is a further opt-in below.
    "warm_pool_account_retarget_enabled": False,
    "warm_pool_cross_provider_retarget_enabled": False,
    # Per-project warm-pool enablement scope (allowlist). An EMPTY list (the
    # default) means the pool is fleet-wide — the legacy all-or-nothing behavior
    # where the maintainer reconciles every demand-active project once
    # warm_pool_enabled is True. A NON-EMPTY list scopes ALL pool provisioning
    # (the autonomous maintainer AND the demand-driven entrypoints) to exactly
    # the listed project ids, so an operator can switch the pool on for ONE
    # project, watch its lift and cost, then widen the scope — without flipping
    # the whole fleet warm at once. warm_pool_enabled stays the outer
    # kill-switch; this only narrows WHICH projects the enabled pool serves.
    # Read by warm_pool.warm_pool_project_scope(); set via /api/admin/settings.
    "warm_pool_project_allowlist": [],
    # Warm-pool maintainer / idle-reaper knobs (efficiency direction d-5fd69fe1).
    # All inert while warm_pool_enabled is False. Target size is the number of
    # IDLE pool members the maintainer keeps warm PER PROJECT that already has
    # pool activity; env-overridable so an operator can size the pool without a
    # code change.
    "warm_pool_target_size": int(os.getenv("WARM_POOL_TARGET_SIZE", "2")),
    # Per-project warm-pool IDLE target overrides ({project_id: int}). An EMPTY
    # map (the default) means every scoped project reconciles toward the single
    # fleet-wide ``warm_pool_target_size``. A per-project entry overrides that
    # for exactly one project so an operator can size each project's pool to its
    # own demand, instead of every project being held at the same number. Values
    # are clamped to the same 0..50 range as the global target; 0 keeps that
    # project's pool empty while leaving the master switch on. Modeled exactly on
    # ``warm_pool_project_allowlist`` so it persists in ``runtime_config_json``
    # with no schema migration. Read by
    # ``warm_pool.resolve_project_warm_pool_target``; set via
    # /api/admin/settings and the per-project warm-pool target endpoint.
    "warm_pool_target_size_by_project": {},
    # Hard ceiling on concurrent live (PROVISIONING/IDLE/LEASED) pool members
    # fleet-wide. The maintainer never provisions past this even if every
    # project is below its target — a dedicated pool budget distinct from the
    # dispatch max_concurrent_vms budget.
    "warm_pool_max_vms": 20,
    # Idle-TTL: an IDLE pool member nobody leased for this long is reaped. Sized
    # well above a normal claim latency so a member is only retired when it is
    # genuinely surplus, not mid-handoff. Two keys are read by distinct call
    # sites and both must exist: the maintainer/idle-reaper reads
    # ``warm_pool_idle_ttl_seconds`` (warm_pool_maintainer.py) while the admin
    # settings/health surface reads ``warm_pool_idle_ttl_secs``. Keep them in
    # sync; both default to 30 min.
    "warm_pool_idle_ttl_seconds": int(os.getenv("WARM_POOL_IDLE_TTL_SECS", "1800")),
    "warm_pool_idle_ttl_secs": int(os.getenv("WARM_POOL_IDLE_TTL_SECS", "1800")),
    # Liveness window for the zombie check: a pool member that has not
    # heartbeated within this window is treated as dead and reaped.
    "warm_pool_heartbeat_stale_seconds": 300,
    # Maintainer tick cadence (leader-only background service).
    "warm_pool_maintainer_interval_seconds": 120,
    # Demand lookback window (minutes) for bootstrap-from-zero pool formation.
    # A project with ZERO live pool members is still reconciled (bootstrapped up
    # to target) when it has real dispatch demand: eligible QUEUED backlog OR a
    # cold dispatch (Task.vm_dispatched_at) within this window. Sized comfortably
    # above the maintainer tick interval (120s) so a project that dispatched
    # moments ago is still seen as demand-active on the next tick; a DORMANT
    # project (no recent demand, no members) is never seeded. Read by
    # warm_pool_maintainer._demand_active_projects.
    "warm_pool_demand_window_minutes": 30,
    # Circuit-breaker thresholds for POOL-served claims. When pool leases or
    # pool-leased tasks fail (lease/finalize error, a leased VM found tearing
    # down, or a FAILED pool task) more than the threshold within a 1h rolling
    # window — e.g. a bad pool image silently failing a stream of tasks — the
    # maintainer stops replenishing and the dispatch hook stops leasing (falling
    # back to cold provisioning) until the cooldown expires. Mirrors the
    # warm-continuity / backlog-reuse breakers.
    "warm_pool_failure_rate_threshold": 5,
    "warm_pool_cooldown_seconds": 1800,
    # Circuit-breaker thresholds for warm-backlog reuse. When backlog-origin
    # claims result in FAILED tasks (or claim/finalize errors) exceeding the
    # threshold within a 1h rolling window, backlog reuse is auto-suppressed
    # for the cooldown before resuming. Mirrors warm_pin_clear_rate_threshold /
    # warm_continuity_cooldown_seconds for the chain-continuity breaker.
    "warm_backlog_reuse_failure_rate_threshold": 5,
    "warm_backlog_reuse_cooldown_seconds": 1800,
    # Warm-VM re-targeting (parent direction: re-target reused VM provider on
    # warm claim). When True, a warm VM may claim a same-project QUEUED task
    # whose resolved provider/auth/model/account diverges from the VM's frozen
    # boot manifest: the warm-claim finalizer re-resolves the task's LLM runtime
    # (select_llm_runtime_for_task), acquires the resolved account's slot, stamps
    # it onto task.context, and hands the agent the resolved account's secret
    # manifest in the claim response so it can re-materialize credentials.
    # Defaults to FALSE — the whole capability ships dark, so this leaves
    # eligibility and the claim response byte-for-byte unchanged until enabled.
    "warm_retarget_enabled": False,
    # Warm-backlog claim scan / teardown window. The scan limit keeps each
    # /next-role poll cheap; the defer window gives the agent time to claim
    # a backlog task before the idle VM is torn down.
    "warm_backlog_scan_limit": 10,
    "warm_backlog_teardown_defer_secs": 20,
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
    # Per-project live-promotion enablement overrides ({project_id: bool}).
    # An EMPTY map (the default) means every project falls back to the
    # fleet-wide ``promotion_enabled`` above. A per-project entry arms or holds
    # live promotion for that single project without touching the fleet default
    # or any other project — modeled on ``warm_pool_target_size_by_project``.
    # Persisted in ``runtime_config_json``; read by
    # ``resolve_project_promotion_enabled``; set via /api/admin/settings and the
    # per-project promotion-enablement endpoint.
    "promotion_enabled_by_project": {},
    # Release-qualifier promotion rubric — the composite-confidence THRESHOLD a
    # candidate must clear to promote, plus the per-sub-score WEIGHTS. These are
    # seeded once at module load from backend/promotion_policy.yaml (falling back
    # to the hardcoded _DEFAULT_POLICY rubric in app.release_scoring) by
    # _seed_promotion_policy() below, then made live + admin-tunable via the
    # /api/admin/settings PATCH so calibrating the bar no longer needs a
    # redeploy. release_qualifier._load_policy() reads these at decision time.
    # The effective default stays the shipped 0.90 / 0.40-0.30-0.30 rubric.
    "promotion_threshold": None,  # set by _seed_promotion_policy()
    "promotion_weights": None,  # set by _seed_promotion_policy()
    # Broadcast alert safety cap — limits the number of projects receiving
    # orchestrator-wide broadcast alerts (errors, halts, rollbacks, recovery).
    # Global orchestrator safety cap, not per-project. Operator-tunable via
    # runtime_config_json; persisted across restarts.
    "project_broadcast_limit": 1000,
    # Proactive account-usage headroom (advisory). When enabled, the max_plan
    # pool consults a per-account, ~60s-cached provider usage-probe snapshot to
    # (a) skip near-exhausted accounts for long-running task types at claim time
    # and (b) prefer more remaining headroom in selection ranking. Ships DARK
    # (default OFF) — when disabled, selection behaves exactly as before and no
    # usage probing happens. The probe is advisory: any failure/unknown snapshot
    # makes selection behave as it does today, and the reactive rate-limit
    # cooldowns remain the backstop. Tunable via /api/admin/settings, no deploy.
    "usage_headroom_gate_enabled": False,
    # Utilization percentage (5h OR 7d) at/above which an account is skipped for
    # long-running task types when the gate is enabled. 0..100.
    "usage_headroom_threshold_pct": 90,
    # Task types the claim-time gate applies to (long-running work that burns a
    # large share of an account's budget if it dies mid-run and restarts). The
    # headroom RANKING preference applies to all task types when enabled; only
    # the skip-gate is scoped to this set.
    "usage_headroom_long_running_task_types": [
        "create",
        "refactor",
        "migrate",
        "research",
        "plan",
        "security_audit",
    ],
}

# Promotion-policy key set — the rubric controls that live in runtime_config.
# Used both by the seed below and by _apply_settings validation.
PROMOTION_POLICY_KEYS = ("promotion_threshold", "promotion_weights")
_PROMOTION_WEIGHT_KEYS = ("audit", "test_erosion", "smoketest")
# backend/promotion_policy.yaml lives two parents up from this file
# (backend/app/runtime_config_state.py).
_PROMOTION_POLICY_PATH = (
    __import__("pathlib").Path(__file__).resolve().parent.parent
    / "promotion_policy.yaml"
)


def _read_promotion_policy_file() -> dict | None:
    """Read + validate backend/promotion_policy.yaml, or return None on any failure.

    Mirrors release_qualifier._load_policy's parse + validation contract: the
    file must parse to a mapping with a ``weights`` sub-mapping (audit /
    test_erosion / smoketest, each float-coercible and summing to ~1.0) and a
    float ``threshold``.  ANY failure (missing file, bad YAML, missing/non-numeric
    keys, mis-weighted sum) returns None so the caller falls back to the hardcoded
    _DEFAULT_POLICY rubric rather than honouring a broken file.  Never raises.
    """
    try:
        import yaml

        parsed = yaml.safe_load(_PROMOTION_POLICY_PATH.read_text(encoding="utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("promotion_policy.yaml did not parse to a mapping")
        weights_raw = parsed.get("weights")
        if not isinstance(weights_raw, dict):
            raise ValueError("promotion_policy.yaml missing 'weights' mapping")
        weights = {k: float(weights_raw[k]) for k in _PROMOTION_WEIGHT_KEYS}
        threshold = float(parsed["threshold"])
        weight_sum = sum(weights.values())
        if abs(weight_sum - 1.0) > 0.01:
            raise ValueError(
                f"promotion_policy.yaml weights sum to {weight_sum:.4f}, expected ~1.0"
            )
        return {"weights": weights, "threshold": threshold}
    except Exception as exc:  # noqa: BLE001 — fall back on any failure, never raise
        logger.warning(
            "[CONFIG] failed to seed promotion policy from promotion_policy.yaml "
            "(%s); seeding from hardcoded defaults",
            exc,
        )
        return None


def _seed_promotion_policy() -> None:
    """Seed runtime_config['promotion_threshold'/'promotion_weights'] once at load.

    Reads backend/promotion_policy.yaml (via _read_promotion_policy_file); on any
    failure falls back to the hardcoded _DEFAULT_POLICY rubric in
    app.release_scoring.  Behaviour-preserving: the seeded values are exactly the
    rubric the qualifier used before this lived in runtime_config.  A persisted
    operator override in runtime_config_json wins later via
    sync_runtime_config_from_state — this only sets the boot default.
    """
    # release_scoring is a pure module (logging + typing only) — safe to import
    # at module load without a circular dependency.
    from app.release_scoring import _DEFAULT_POLICY

    policy = _read_promotion_policy_file()
    if policy is None:
        policy = {
            "weights": dict(_DEFAULT_POLICY["weights"]),
            "threshold": _DEFAULT_POLICY["threshold"],
        }
    runtime_config["promotion_threshold"] = float(policy["threshold"])
    runtime_config["promotion_weights"] = dict(policy["weights"])


_seed_promotion_policy()

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
                if "compute_experiment_spend_threshold_usd" in config_json:
                    _set(
                        "compute_experiment_spend_threshold_usd",
                        float(config_json["compute_experiment_spend_threshold_usd"]),
                    )
                if "auto_archive_enabled" in config_json:
                    _set(
                        "auto_archive_enabled",
                        bool(config_json["auto_archive_enabled"]),
                    )
                if "auto_debug_enabled" in config_json:
                    _set(
                        "auto_debug_enabled",
                        bool(config_json["auto_debug_enabled"]),
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
                if "cochange_decomposition_enabled" in config_json:
                    _set(
                        "cochange_decomposition_enabled",
                        bool(config_json["cochange_decomposition_enabled"]),
                    )
                if "warm_retarget_enabled" in config_json:
                    _set(
                        "warm_retarget_enabled",
                        bool(config_json["warm_retarget_enabled"]),
                    )
                if "usage_headroom_gate_enabled" in config_json:
                    _set(
                        "usage_headroom_gate_enabled",
                        bool(config_json["usage_headroom_gate_enabled"]),
                    )
                if "usage_headroom_long_running_task_types" in config_json:
                    raw_types = config_json["usage_headroom_long_running_task_types"]
                    if isinstance(raw_types, str):
                        raw_types = raw_types.split(",")
                    if isinstance(raw_types, (list, tuple, set)):
                        _set(
                            "usage_headroom_long_running_task_types",
                            sorted(
                                {str(t).strip() for t in raw_types if str(t).strip()}
                            ),
                        )
                if "warm_pool_enabled" in config_json:
                    _set(
                        "warm_pool_enabled",
                        bool(config_json["warm_pool_enabled"]),
                    )
                if "warm_pool_account_retarget_enabled" in config_json:
                    _set(
                        "warm_pool_account_retarget_enabled",
                        bool(config_json["warm_pool_account_retarget_enabled"]),
                    )
                if "warm_pool_cross_provider_retarget_enabled" in config_json:
                    _set(
                        "warm_pool_cross_provider_retarget_enabled",
                        bool(
                            config_json["warm_pool_cross_provider_retarget_enabled"]
                        ),
                    )
                if "warm_pool_project_allowlist" in config_json:
                    # Persisted per-project enablement scope. Normalize to a
                    # sorted list of trimmed, non-empty string ids — mirroring
                    # warm_pool.warm_pool_project_scope() / the admin PATCH
                    # surface — so every instance agrees on the scoped set and a
                    # garbage entry can't silently widen or corrupt the pool.
                    raw_allow = config_json["warm_pool_project_allowlist"]
                    if isinstance(raw_allow, str):
                        raw_allow = raw_allow.split(",")
                    if isinstance(raw_allow, (list, tuple, set)):
                        _set(
                            "warm_pool_project_allowlist",
                            sorted(
                                {str(p).strip() for p in raw_allow if str(p).strip()}
                            ),
                        )
                if "warm_pool_target_size_by_project" in config_json:
                    # Persisted per-project IDLE target overrides. Normalize to a
                    # map of trimmed, non-empty string project-id keys to integer
                    # values clamped to the same 0..50 range the global target
                    # uses — mirroring warm_pool.warm_pool_project_target_map() /
                    # the admin PATCH surface — and drop malformed entries so a
                    # garbage value can't corrupt a project's reconcile target.
                    raw_targets = config_json["warm_pool_target_size_by_project"]
                    if isinstance(raw_targets, dict):
                        normalized_targets: dict[str, int] = {}
                        for proj_key, proj_val in raw_targets.items():
                            sid = str(proj_key).strip()
                            if not sid:
                                continue
                            try:
                                ival = int(proj_val)
                            except (TypeError, ValueError):
                                continue
                            normalized_targets[sid] = max(0, min(50, ival))
                        _set(
                            "warm_pool_target_size_by_project", normalized_targets
                        )
                for int_key in (
                    "max_provision_retries",
                    "max_rollup_retries",
                    "architecture_review_frequency",
                    "max_active_directions",
                    "staged_direction_stale_alert_days",
                    "staged_direction_stale_fallback_days",
                    "max_parallel_research_per_project",
                    "auto_archive_terminal_after_days",
                    "auto_archive_max_terminal_tasks_per_project",
                    "auto_archive_interval_minutes",
                    "max_pipeline_slots",
                    "evaluating_pipeline_timeout_minutes",
                    "ci_backstop_timeout_minutes",
                    "evaluating_fail_hours",
                    "spend_approval_timeout_hours",
                    "max_total_research_per_project",
                    "productive_direction_cooldown_minutes",
                    "next_role_poll_timeout_secs",
                    "direction_churn_window_cycles",
                    "direction_churn_non_merge_threshold",
                    "direction_lifetime_non_merge_threshold",
                    "warm_pin_clear_rate_threshold",
                    "warm_continuity_cooldown_seconds",
                    "warm_backlog_reuse_failure_rate_threshold",
                    "warm_backlog_reuse_cooldown_seconds",
                    "warm_backlog_scan_limit",
                    "warm_backlog_teardown_defer_secs",
                    "warm_pool_target_size",
                    "warm_pool_max_vms",
                    "warm_pool_idle_ttl_seconds",
                    "warm_pool_idle_ttl_secs",
                    "warm_pool_heartbeat_stale_seconds",
                    "warm_pool_maintainer_interval_seconds",
                    "warm_pool_demand_window_minutes",
                    "warm_pool_failure_rate_threshold",
                    "warm_pool_cooldown_seconds",
                    "project_broadcast_limit",
                    "reaper_cycle_halt_threshold",
                    "reaper_cycle_halt_cooldown_seconds",
                    "reaper_half_open_probe_limit",
                    "reaper_max_orphans_per_cycle",
                    "usage_headroom_threshold_pct",
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
                if "promotion_enabled_by_project" in config_json:
                    # Persisted per-project live-promotion overrides. Normalize to
                    # a map of trimmed, non-empty string project-id keys to bools —
                    # mirroring promotion_enabled_by_project_map() / the admin PATCH
                    # surface — and drop empty keys so a garbage entry can't corrupt
                    # a project's promotion gate.
                    raw_promo = config_json["promotion_enabled_by_project"]
                    if isinstance(raw_promo, dict):
                        normalized_promo: dict[str, bool] = {}
                        for proj_key, proj_val in raw_promo.items():
                            sid = str(proj_key).strip()
                            if not sid:
                                continue
                            normalized_promo[sid] = bool(proj_val)
                        _set("promotion_enabled_by_project", normalized_promo)
                # Release-qualifier promotion rubric (admin-tunable, persisted).
                # A None persisted value means "never overridden" — keep the
                # boot-seeded YAML/default value rather than clobbering it.
                if config_json.get("promotion_threshold") is not None:
                    _set(
                        "promotion_threshold",
                        float(config_json["promotion_threshold"]),
                    )
                if config_json.get("promotion_weights") is not None:
                    pw = config_json["promotion_weights"]
                    if isinstance(pw, dict):
                        _set(
                            "promotion_weights",
                            {k: float(v) for k, v in pw.items()},
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


def promotion_enabled_by_project_map() -> Dict[str, bool]:
    """Per-project live-promotion overrides, as ``{project_id: bool}``.

    Read from the durable ``promotion_enabled_by_project`` runtime-config value
    (modeled on ``warm_pool.warm_pool_project_target_map``). Normalizes to
    trimmed, non-empty string project-id keys mapping to bools, silently dropping
    malformed entries. An EMPTY map (the default) means every project falls back
    to the fleet-wide ``promotion_enabled``. Parallel to
    :func:`resolve_project_promotion_enabled`.
    """
    raw = runtime_config.get("promotion_enabled_by_project") or {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, bool] = {}
    for key, value in raw.items():
        sid = str(key).strip()
        if not sid:
            continue
        out[sid] = bool(value)
    return out


def resolve_project_promotion_enabled(project_id) -> bool:
    """A project's EFFECTIVE live-promotion gate.

    Returns the project's explicit per-project value from
    ``promotion_enabled_by_project`` when one is set, otherwise falls back to the
    fleet-wide ``promotion_enabled`` default so behavior is byte-identical for
    projects without an override. Mirrors
    :func:`warm_pool.resolve_project_warm_pool_target` for the promotion gate.
    """
    per_project = promotion_enabled_by_project_map()
    sid = str(project_id)
    if sid in per_project:
        return per_project[sid]
    return bool(runtime_config.get("promotion_enabled"))
