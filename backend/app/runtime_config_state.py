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

from app.task_capabilities import (
    ALL_TASK_TYPES,
    AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES,
    AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES,
)

logger = logging.getLogger(__name__)

# Lock for multi-key runtime config updates (prevents reading partial state between two assignments)
runtime_config_lock = asyncio.Lock()

_GATE_MODES = ("off", "observe", "enforce")


def resolve_capability_proof_gate_mode_default(default: str = "observe") -> str:
    """Resolve the boot-time ``capability_proof_gate_mode`` from the environment.

    Reads ``CAPABILITY_PROOF_GATE_MODE`` and validates it against the three legal
    modes, falling back to ``default`` (``"observe"`` — see the staged-rollout
    note on the ``runtime_config`` entry) for an unset or unrecognized value.

    Extracted as a named pure helper (rather than an inline dict-literal
    expression) so the shipped default is directly unit-testable without reloading
    this heavy module.
    """
    raw = os.getenv("CAPABILITY_PROOF_GATE_MODE", default).lower()
    return raw if raw in _GATE_MODES else default

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
    # Rollup merge train: after a rollup PR merges, the post-merge handler
    # proactively update-branches the project's sibling rollup PRs (FIFO by PR
    # created_at) instead of waiting for lazy CI-poll behind-recovery, and
    # conflict-fix spawners defer dirty observations made <5 min after a queue
    # update so the queue and the agent fix path never double-fire. Genuine
    # dirty conflicts outlive the window and use the agent path unchanged.
    "rollup_merge_queue_enabled": True,
    "queued_age_alert_hours": int(os.getenv("QUEUED_AGE_ALERT_HOURS", "2")),
    "retry_after_reset_alert_threshold": int(
        os.getenv("RETRY_AFTER_RESET_ALERT_THRESHOLD", "8")
    ),
    "max_autonomous_tasks_per_hour": 12,
    # HARD cross-project backstop ceiling. Deliberately raised 50 -> 500
    # (2026-07-05): the old 50/hr counted ALL orchestrator tasks (including
    # review/fix servicing churn) and was checked BEFORE the per-project research
    # caps, starving legitimate research. This global ceiling is enforced in ALL
    # budget modes (off/observe/enforce) — build_autonomous_spawn_budget_guard
    # blocks on total_count >= global_cap independent of mode — so a real hard
    # cross-project ceiling always remains, even under the shipped observe default.
    # Per-project caps (max_autonomous_tasks_per_hour=12) still bound any single
    # project, so 50/hr only binds in aggregate across the whole fleet.
    "max_global_autonomous_tasks_per_hour": 50,
    # Rollout mode for the CATEGORIZED spawn budget (initiative vs servicing).
    #   off/observe — the tighter initiative cap below is advisory (recorded, not
    #                 blocking); the 50/hr global backstop above still binds.
    #   enforce     — the initiative cap additionally blocks initiative spawns.
    # Ships "observe" as a deliberate gradual-rollout posture so operators can
    # measure the initiative/servicing split before switching the tighter gate on.
    "autonomous_spawn_budget_mode": "observe",
    # Tighter cross-project cap on INITIATIVE-category spawns (net-new research/
    # create, excluding review/fix servicing churn). Only binds when
    # autonomous_spawn_budget_mode == "enforce"; pending its dedicated rollout.
    "max_global_autonomous_initiative_tasks_per_hour": 50,
    "autonomous_spawn_budget_categories": {
        "initiative": sorted(AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES),
        "servicing": sorted(AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES),
    },
    "autonomous_research_interval_minutes": 30,
    # --- Utilization-triggered evergreen backlog auto-fill (d-2877ddc7) ---
    # Phase-0, defaulted OFF. When idle (low utilization) or all active
    # directions are parked/proof-waiting, spawn ONE low-priority research probe
    # from the curated EvergreenBacklogItem ledger — additive parallelism that
    # never competes with the release-critical path.
    #   off     — never act (default).
    #   observe — evaluate + record the would-be decision, create nothing.
    #   enforce — act on a CREATE decision.
    "evergreen_fill_mode": os.getenv("EVERGREEN_FILL_MODE", "off").lower()
    if os.getenv("EVERGREEN_FILL_MODE", "off").lower() in ("off", "observe", "enforce")
    else "off",
    # Throttle interval for the orchestrator's _check_evergreen_fill() sibling.
    "evergreen_fill_interval_minutes": int(
        os.getenv("EVERGREEN_FILL_INTERVAL_MINUTES", "15")
    ),
    # Utilization at/above which the fleet is "busy" — no idle-fill trigger.
    "evergreen_fill_util_threshold": float(
        os.getenv("EVERGREEN_FILL_UTIL_THRESHOLD", "0.5")
    ),
    # Minimum free VM slots required before an evergreen probe may spawn.
    "evergreen_fill_min_free_slots": int(
        os.getenv("EVERGREEN_FILL_MIN_FREE_SLOTS", "2")
    ),
    # Hard cap on concurrently-live evergreen probes (status == spawned).
    "evergreen_fill_max_active": int(os.getenv("EVERGREEN_FILL_MAX_ACTIVE", "1")),
    # Per-project daily cap on evergreen probe spawns.
    "evergreen_fill_daily_cap": int(os.getenv("EVERGREEN_FILL_DAILY_CAP", "3")),
    # Allow enrolling backlog items from deferred research memos (provenance).
    "evergreen_fill_deferred_memo_enrollment": os.getenv(
        "EVERGREEN_FILL_DEFERRED_MEMO_ENROLLMENT", "false"
    ).lower()
    in ("1", "true", "yes"),
    # --- Structural-health audit lane (resource/scalability audit) ---
    # Recurring leader-only audit (reaper sweep) that detects the structural
    # class that caused a Postgres saturation outage: unbounded JSONB fat-row
    # growth in task.context. When it trips, it enrolls a DEDUPLICATED finding
    # into the EXISTING evergreen ledger (same triage as a correctness finding)
    # — no new queue, no schema change. Mirrors evergreen_fill_mode exactly.
    #   off     — never act.
    #   observe — measure + record the would-be finding, enroll nothing (default).
    #   enforce — enroll the deduped finding.
    "structural_health_audit_mode": os.getenv(
        "STRUCTURAL_HEALTH_AUDIT_MODE", "observe"
    ).lower()
    if os.getenv("STRUCTURAL_HEALTH_AUDIT_MODE", "observe").lower()
    in ("off", "observe", "enforce")
    else "observe",
    # Cadence (minutes) between structural-health audit sweeps in the reaper loop.
    "structural_health_audit_interval_minutes": int(
        os.getenv("STRUCTURAL_HEALTH_AUDIT_INTERVAL_MINUTES", "60")
    ),
    # Per-row task.context byte-size threshold at/above which a row is a fat-row
    # finding. Default 1 MiB — far above a healthy row (low single-digit KiB),
    # well below the multi-megabyte rows that drove the saturation outage.
    "structural_health_audit_fat_row_bytes": int(
        os.getenv("STRUCTURAL_HEALTH_AUDIT_FAT_ROW_BYTES", "1048576")
    ),
    # --- Metrics-integrity sweep lane (standing self-audit of counters) ---
    # Recurring leader-only reaper sweep that recomputes selected self-reported
    # counters (landed-vs-reported merges, playbook confirmation integrity,
    # non-code outcome window accounting) from ground truth and FLAGS drift
    # WITHOUT ever mutating the audited source counter. Mode mirrors
    # structural_health_audit_mode exactly:
    #   off     — never run.
    #   observe — recompute + record to the in-memory observability ring (default).
    #   enforce — additionally enroll a deduped evergreen finding per flagged drift.
    "metrics_integrity_sweep_mode": os.getenv(
        "METRICS_INTEGRITY_SWEEP_MODE", "observe"
    ).lower()
    if os.getenv("METRICS_INTEGRITY_SWEEP_MODE", "observe").lower()
    in ("off", "observe", "enforce")
    else "observe",
    # Cadence (minutes) between metrics-integrity sweeps in the reaper loop.
    "metrics_integrity_sweep_interval_minutes": int(
        os.getenv("METRICS_INTEGRITY_SWEEP_INTERVAL_MINUTES", "60")
    ),
    # Max registered checks run per tick (backstop against an over-grown registry).
    "metrics_integrity_sweep_max_checks": int(
        os.getenv("METRICS_INTEGRITY_SWEEP_MAX_CHECKS", "50")
    ),
    # Max projects scanned per per-project check (bounds the scan fan-out).
    "metrics_integrity_sweep_max_projects": int(
        os.getenv("METRICS_INTEGRITY_SWEEP_MAX_PROJECTS", "25")
    ),
    # Max rows sampled per DB-backed check (bounds the recompute cost).
    "metrics_integrity_sweep_sample_limit": int(
        os.getenv("METRICS_INTEGRITY_SWEEP_SAMPLE_LIMIT", "200")
    ),
    # How far back (days) a windowed check recomputes ground truth.
    "metrics_integrity_sweep_window_days": int(
        os.getenv("METRICS_INTEGRITY_SWEEP_WINDOW_DAYS", "14")
    ),
    # --- Message supersession lane (auto-resolve stale operator messages) ---
    # When a release version reaches a terminal state (promoted/abandoned), the
    # promotion/abandon path auto-resolves still-open, non-bug-class operator
    # messages that were created strictly before the closing version's window
    # (i.e. from a version at least min_versions_back back) AND older than
    # min_age_days. Mode mirrors metrics_integrity_sweep_mode exactly:
    #   off     — never supersede.
    #   observe — measure the would-be resolutions + log, mutate nothing.
    #   enforce — stamp the eligible messages resolved (default).
    # Unresolved BUG_REPORT/BLOCKER messages are ALWAYS left open (a bug does not
    # become fixed just because a version promoted).
    "message_supersession_mode": os.getenv(
        "MESSAGE_SUPERSESSION_MODE", "enforce"
    ).lower()
    if os.getenv("MESSAGE_SUPERSESSION_MODE", "enforce").lower()
    in ("off", "observe", "enforce")
    else "enforce",
    # A message must be at least this many days old to be superseded (safety margin).
    "message_supersession_min_age_days": int(
        os.getenv("MESSAGE_SUPERSESSION_MIN_AGE_DAYS", "14")
    ),
    # A message must belong to a version at least this many versions behind the
    # current open version (2 => never touch the current or immediately-prior one).
    "message_supersession_min_versions_back": int(
        os.getenv("MESSAGE_SUPERSESSION_MIN_VERSIONS_BACK", "2")
    ),
    # Upper bound on messages resolved per version close (runaway-batch backstop).
    "message_supersession_max_resolve": int(
        os.getenv("MESSAGE_SUPERSESSION_MAX_RESOLVE", "500")
    ),
    # --- Completion-debt pressure signal graduation gate ---
    # Operator-facing graduation knob for blending the qualification debt-pressure
    # signal into the promotion composite (release_qualifier._load_policy →
    # compute_composite).  Mirrors evergreen_fill_mode / structural_health_audit_mode.
    #   off      — never blend; the completion_debt weight is forced to 0.0.
    #   observe  — (default) the debt-pressure score is recorded in sub_scores but
    #              the effective completion_debt weight is 0.0 (never blended).
    #   enforce  — the configured completion_debt weight is applied (falling back
    #              to a sensible nonzero default when the operator left it unset),
    #              so open completion debt actually erodes promotion confidence.
    # Graduated to "enforce" (three-state adjudication, decision #7): the debt-
    # pressure counterfactual has been computed for every qualify run under
    # observe, so the configured completion_debt weight now blends into the
    # composite by default. No longer a dormant knob — removed from
    # _DORMANT_MODE_KNOBS below. An operator may still pin "off"/"observe" via
    # the env var / PATCH settings for a per-deployment rollback.
    "completion_debt_weight_mode": os.getenv(
        "COMPLETION_DEBT_WEIGHT_MODE", "enforce"
    ).lower()
    if os.getenv("COMPLETION_DEBT_WEIGHT_MODE", "enforce").lower()
    in ("off", "observe", "enforce")
    else "enforce",
    # Rollout mode for the capability proof gate — controls whether prerequisite
    # resolution gates on machine-verified proof (enforce) or allows self-reported
    # working status (off/observe).
    #   off     — proof status is advisory only; never blocks prerequisite resolution.
    #   observe — proof status is recorded and surfaced but does not block.
    #   enforce — unproven capabilities do not satisfy prerequisite resolution.
    #
    # STAGED ROLLOUT (audit finding #4 — "default-off means none of this
    # enforcement binds by default"): the gate now ships default **"observe"**,
    # not "off". Observe is the safe first stage — it records and surfaces the
    # unproven-capability delta and logs on every self-reported resolution WITHOUT
    # changing any resolution or promotion behaviour, so the enforcement plumbing
    # is exercised (and its blind spots become visible) the moment the platform
    # boots, instead of lying inert until an operator remembers to flip it.
    #   Operational risk of default-observe: NONE beyond extra WARNING logs and a
    #   recorded (never-blocking) release dimension — observe cannot gate a spawn,
    #   a blocked-resume, or a promotion.
    #   Explicit flip criterion off/observe -> enforce (operator-gated, via
    #   PATCH /api/admin/settings or the auto-graduation sweep): flip to "enforce"
    #   only once fleet-wide proof coverage of *working* capabilities clears
    #   ``capability_proof_gate_auto_graduation_enforce_threshold`` (0.95) — the
    #   same ratio the auto-graduation sweep already tracks — so enforce does not
    #   strand real work behind capabilities that are genuinely proven but simply
    #   have not been through the earn loop yet.
    "capability_proof_gate_mode": resolve_capability_proof_gate_mode_default(),
    # Auto-graduation sweep for the capability proof gate. When enabled, the
    # metrics-integrity sweep automatically advances the gate mode (off→observe→enforce)
    # based on fleet-wide proof coverage. Disabled by default — operators must opt in.
    # The thresholds are the minimum proof-coverage ratios required for each step:
    # observe_threshold for off→observe, enforce_threshold for observe→enforce.
    "capability_proof_gate_auto_graduation_enabled": os.getenv(
        "CAPABILITY_PROOF_GATE_AUTO_GRADUATION_ENABLED", "false"
    ).lower()
    in ("1", "true", "yes"),
    "capability_proof_gate_auto_graduation_observe_threshold": float(
        os.getenv(
            "CAPABILITY_PROOF_GATE_AUTO_GRADUATION_OBSERVE_THRESHOLD", "0.5"
        )
    ),
    "capability_proof_gate_auto_graduation_enforce_threshold": float(
        os.getenv(
            "CAPABILITY_PROOF_GATE_AUTO_GRADUATION_ENFORCE_THRESHOLD", "0.95"
        )
    ),
    # Machine-verified capability-proof earn path: gates the bounded,
    # executed-named_test trial dispatch/enrollment.  Default ON now that the
    # complete path is wired (discovery -> targeted execution -> green-result
    # ingestion -> proven evidence). Operators retain the PATCH/env kill switch
    # for a fleet that needs to pause trial dispatch.
    "capability_proof_execution_trials_enabled": os.getenv(
        "CAPABILITY_PROOF_EXECUTION_TRIALS_ENABLED", "true"
    ).lower()
    in ("1", "true", "yes"),
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
    # Graduated rollout mode for the safeguard merge-gate challenge.
    #   "off"     — no challenge; gate held unconditionally on any
    #               critical/high finding. Preserves pre-v9 behavior exactly.
    #   "observe" — (default) shadow/observe mode: the challenge is spawned and its verdict
    #               is recorded on the safeguard task as
    #               ``safeguard_observe_verdict``, but the gate is NEVER released
    #               by the verdict. Lets operators measure the false-positive rate
    # Per-finding second-opinion challenge for high-stakes research-finding
    # verdicts. When True, a single borderline/absent verdict (a reproduce-on-HEAD
    # 'absent' hard-drop or a low-actionability decay) is independently re-examined
    # by a bounded read-only `investigate` side-car
    # (task_evaluators.spawn_finding_challenge) BEFORE the finding is dropped or
    # dispatched: the source research task parks EVALUATING, the side-car emits a
    # structured second-opinion verdict, and the recorded FindingChallengeRecord
    # routes back into research completion. Distinct from the batch
    # research_quality_grader_enabled gate (which grades the WHOLE artifact); this
    # challenges ONE finding's verdict. Ships DARK (default OFF) — OFF preserves
    # the legacy single-pass drop/dispatch behavior exactly.
    "research_finding_challenge_enabled": os.getenv(
        "RESEARCH_FINDING_CHALLENGE_ENABLED", "false"
    ).lower()
    in ("1", "true", "yes"),
    # Narrow pilot for the optional third research-finding challenge boundary:
    # an indeterminate reproduce-on-HEAD verdict that would otherwise dispatch a
    # defect finding unverified. Kept separate from
    # research_finding_challenge_enabled so enabling absent/decay hardening cannot
    # fan out into broad pre-dispatch challenge traffic.
    "research_finding_challenge_indeterminate_dispatch_enabled": os.getenv(
        "RESEARCH_FINDING_CHALLENGE_INDETERMINATE_DISPATCH_ENABLED", "false"
    ).lower()
    in ("1", "true", "yes"),
    # DEFAULT-ON per-finding adversarial challenge gate for high-stakes research
    # findings. When True (default), each finding that carries large blast radius
    # (scope=large, risk=high) OR a partial-staleness / over-claim signal
    # (downgraded verification_basis, broken_refs, ungrounded_refs,
    # needs_revalidation) is independently challenged by a refute-first grader
    # before it spawns. A challenged finding that fails is DEFERRED (not dropped)
    # so strategic review sees it. Low-stakes findings are never escalated.
    # Set to False to disable (settable via PATCH /api/admin/settings, no deploy).
    "adversarial_spawn_gate_enabled": os.getenv(
        "ADVERSARIAL_SPAWN_GATE_ENABLED", "true"
    ).lower()
    not in ("0", "false", "no"),
    # Observe/enforce rollback for exhausted adversarial-grader infrastructure
    # failures. "observe" (default) lets the high-stakes finding spawn with an
    # auditable unverified flag when the challenger infra failed after retries;
    # "enforce" restores strict fail-closed behavior.
    "adversarial_spawn_gate_infra_failure_mode": os.getenv(
        "ADVERSARIAL_SPAWN_GATE_INFRA_FAILURE_MODE", "observe"
    ).lower(),
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
    # Cap on a project's concurrent in-flight compute-experiment disposition
    # follow-ons (the always-dispose reason-and-ship / diagnose-and-fix children
    # spawned by _spawn_act_on_verdict_followon). When this many non-terminal
    # type=create disposition children already exist for the project, a new
    # terminal experiment skips its spawn (and completes/fails clean) rather than
    # piling on; the dedup key is NOT stamped so a re-dispatch retries once the
    # fleet drains. 0 disables the cap. Operator-tunable via runtime_config_json.
    "max_concurrent_act_on_verdict_followons": 4,
    # Future-facing provider tier map used by multi-provider selection. v1 keeps
    # Anthropic as the only dispatch path but persists the structure now so v2
    # can enable provider-aware resolution without another config shape change.
    "provider_tier_model_map": {
        "anthropic": {
            "opus": "claude-opus-4-8",
            "sonnet": "claude-sonnet-5",
            "haiku": "claude-haiku-4-5-20251001",
        },
        "openai": {
            # GPT-5.6 picker trio (2026-07): sol=frontier, terra=balanced,
            # luna=fast/affordable. gpt-5.3-codex left the picker this cycle.
            "opus": "gpt-5.6-sol",
            "sonnet": "gpt-5.6-terra",
            "haiku": "gpt-5.6-luna",
        },
        "gemini": {
            "opus": "gemini-2.5-pro",
            "sonnet": "gemini-2.5-flash",
            "haiku": "gemini-2.5-flash-lite",
        },
        "zai": {
            "opus": "glm-5.2",
            "sonnet": "glm-5.2",
            "haiku": "glm-5.2",
        },
        "deepseek": {
            "opus": "deepseek-v4-pro",
            "sonnet": "deepseek-v4-pro",
            "haiku": "deepseek-v4-flash",
        },
    },
    # Task-type categories group related types so one routing entry covers
    # the whole group. Every task type listed here falls under the matching
    # category_routing block; unlisted types resolve via category_routing["_default"].
    "task_type_categories": {
        "strategic": ["strategic_review", "design_doc_update"],
        "initiative": ["research", "plan"],
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
            "providers": ["deepseek", "anthropic", "openai", "gemini"],
            "effort": "medium",
        },
        "strategic": {
            "providers": ["zai", "anthropic", "deepseek", "openai", "gemini"],
            "effort": "max",
        },
        "initiative": {
            "providers": ["deepseek", "anthropic", "openai", "gemini"],
            "effort": "medium",
        },
        "build": {
            "providers": ["deepseek", "anthropic", "openai", "gemini"],
            "effort": "medium",
        },
        "review": {
            "providers": ["anthropic", "deepseek", "openai", "gemini"],
            "effort": "high",
        },
        "ops": {
            "providers": ["deepseek", "anthropic", "openai", "gemini"],
            "effort": "low",
        },
    },
    # Sparse per-task-type routing overrides. Either field optional; the
    # other inherits from the task's category. Use for surgical cases like
    # "route debug tasks to openai at high effort regardless of category".
    "task_type_routing": {},
    # Optional per-task-type concrete model priority overrides. When present,
    # selection uses these ordered {provider, model} pairs before falling back
    # to provider preference + quality tier resolution.
    # The release smoke deliberately exercises the frontier Codex path and the
    # CLI baked into the agent image, rather than whichever low-cost provider
    # happens to lead the ops route.
    "task_type_model_preferences": {
        "smoketest": [{"provider": "openai", "model": "gpt-5.6-sol"}],
    },
    # Provider selection mode — controls how the pool chooses across the
    # providers listed in category_routing / task_type_routing:
    #   "balanced"  — flatten all accounts across listed providers into one
    #                 pool; pick globally by (health_score, -active_tasks).
    #   "preferred" — strict ordering; try each provider in list order, only
    #                 fall through when the earlier provider has no capacity.
    #   "exclusive" — first listed provider only; never fall back cross-provider.
    "provider_selection_mode": "preferred",
    # Deploy-level identity: the ``"owner/repo"`` of the GitHub repo THIS
    # deployment was built from. Determines which project's Cloud Run serving
    # signals (backend_revision / agent_image artifact providers) and live
    # DeploymentState reads are observable — replaces the per-project
    # ``is_self_repo`` gate with a deploy-level property. Seeded from the
    # ``DEPLOY_REPO_IDENTITY`` env (stamped on the Cloud Run revision alongside
    # ``DEPLOY_SHA``); operator-settable live via PATCH /api/admin/settings.
    # None ⇒ no deploy identity (only the merge-level promotion_merge provider
    # is observable, for every GitHub project).
    "deploy_repo_identity": (os.getenv("DEPLOY_REPO_IDENTITY", "").strip() or None),
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
        "zai": {},
        "deepseek": {},
    },
    # Per-provider effort matrix. Each cell is a settings bag (validated by
    # the provider's settings_schema) and typically pins a model + the
    # provider's effort knob. When a task resolves to (provider, effort), the
    # corresponding cell is merged on top of provider_settings_defaults.
    "provider_settings_by_effort": {
        "anthropic": {
            "low": {"model": "claude-haiku-4-5-20251001", "effort": "low"},
            "medium": {"model": "claude-sonnet-5", "effort": "medium"},
            "high": {"model": "claude-opus-4-8", "effort": "high"},
            "max": {"model": "claude-opus-4-8", "effort": "max"},
        },
        "openai": {
            # Verbosity is graded to task-type category, not capability:
            # build/ops (low/medium effort) produce code and tool calls, not
            # prose — terse output saves tokens without losing debug signal.
            # review (high) gets medium so review comments can explain.
            # strategic (max) gets high because the prose *is* the deliverable
            # (design docs and strategic reviews).
            "low": {
                "model": "gpt-5.6-luna",
                "reasoning_effort": "low",
                "verbosity": "low",
            },
            "medium": {
                "model": "gpt-5.6-terra",
                "reasoning_effort": "medium",
                "verbosity": "low",
            },
            "high": {
                "model": "gpt-5.6-sol",
                "reasoning_effort": "high",
                "verbosity": "medium",
            },
            # xhigh (not the 5.6-only max/ultra levels) so a fallback or an
            # operator override to an older-gen model keeps a valid level.
            "max": {
                "model": "gpt-5.6-sol",
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
        "zai": {
            "low": {"model": "glm-5.2", "effort": "low"},
            "medium": {"model": "glm-5.2", "effort": "medium"},
            "high": {"model": "glm-5.2", "effort": "high"},
            "max": {"model": "glm-5.2", "effort": "max"},
        },
        "deepseek": {
            "low": {"model": "deepseek-v4-flash", "effort": "low"},
            "medium": {"model": "deepseek-v4-pro", "effort": "medium"},
            "high": {"model": "deepseek-v4-pro", "effort": "high"},
            "max": {"model": "deepseek-v4-pro", "effort": "max"},
        },
    },
    "inline_llm": {},
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
    "directed_research_cooldown_minutes": 120,
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
    # Churn-family pause-ladder threshold: how many churn-family auto-pause cycles
    # must accumulate before a flagged, strategist-reviewable retirement
    # RECOMMENDATION is recorded (non-terminal — the direction stays paused, no
    # auto-retire). Replaces the hardcoded _MAX_CHURN_PAUSE_CYCLES=2 constant in
    # direction_lifecycle.py so operators can tune the ladder without a redeploy.
    "direction_review_recommendation_threshold": 2,
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
    # Gate mode for the strategic evidence-progress challenge. Three values:
    #   "enforce"  — fail-closed (default, current behavior): a suppressing
    #                evidence-progress verdict is ignored when the challenge did
    #                not approve it, keeping the deterministic scaffolding detector
    #                active.
    #   "advisory" — record-and-warn: the gate would have blocked but does not;
    #                a warning is logged and gate_would_block is stamped on the
    #                persisted verdict so operators can calibrate before enforcing.
    #   "observe"  — record-only: the gate would-block decision is stamped but
    #                suppression is always allowed; no warning is emitted.
    "strategic_challenge_gate_mode": "enforce",
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
    "max_retained_images": 5,  # Agent VM image family pruning (keep N most recent; GCP per-project image quota is tight)
    "require_approval_gates": True,
    "daily_cost_ceiling_usd": 0,  # 0 = disabled; positive float enables budget alerts
    # Monthly budget target (USD). 0 = disabled. When >0, Blueprint's own cost
    # telemetry compares month-to-date total spend (VM + compute + pool idle)
    # against this target and fires Alerts at 50/90/100% thresholds — live from
    # the admin UI, no deploy needed. The GCP-native billing budget in
    # infra/terraform/budget.tf stays as a high backstop ($5000 default).
    "monthly_budget_target_usd": 0,  # 0 = disabled; positive float enables monthly budget alerts
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
    # Effort-escalation-on-execution-timeout kill-switch. When True (default),
    # a task requeued purely because its wall-clock execution budget ran out
    # (the AGENT_TIMEOUT "execution_timeout" subtype — NOT oom/crash/heartbeat
    # loss) has its per-attempt effort tier bumped one step before the retry, so
    # the retry gets a larger wall-clock budget (and a stronger model where
    # effort drives model selection) instead of repeating the same doomed
    # budget. False restores the prior same-effort retry behavior fleet-wide.
    "effort_escalation_on_execution_timeout_enabled": True,
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
    # Master switch for the v10 persistent improvement lane substrate (knowledge
    # doc 18840e55). Default False so ALL lane code is INERT. When this is True
    # and review_fix_lane_pilot_enabled is False, the review-fix path runs in
    # SHADOW: eligible self-repo chains create armless lanes and exercise real
    # SHA-verified checkpoint/CAS transitions, but take no workspace reservation,
    # attach no warm-resume prompt/dispatch context, and do not alter spawning.
    "improvement_lanes_enabled": False,
    # Review-fix lane pilot (knowledge doc 18840e55, pilot/matched-control/kill
    # sections). Lane-izes ONLY the fix side of the review -> fix -> re-review
    # loop for the Blueprint self-repo. Requires improvement_lanes_enabled too.
    # False means either fully inert (master off) or SHADOW (master on); True
    # means full pilot with ITT arm assignment, warm reservation, fix-side lane
    # dispatch/prompt context, recovery requeues, and KILL/ADOPT measurement.
    # A KILL verdict flips this back to False so every chain leaves full pilot.
    "review_fix_lane_pilot_enabled": False,
    # Event-driven qualification trigger mode.  Controls whether the sweep-based
    # qualification trigger resolver (qualification_triggers.py) actually fires
    # run_qualification for matched versions:
    #   "observe" (default) — resolver runs and logs would-fire decisions without
    #                          spawning any qualification.  Compare against actual
    #                          strategist/sweep activity before flipping to "on".
    #   "on"                — resolver fires run_qualification for matched versions.
    #   "off"               — resolver does not run at all (legacy behavior).
    "auto_qualification_triggers": "observe",
    # Account-agnostic lease: when True, a same-tenant IDLE pool member may be
    # leased for a task whose resolved account (and model) differ from the
    # member's frozen boot account, and the warm-claim path re-materializes the
    # resolved account's credentials before the role runs. Lets ONE small pool
    # serve a project's whole account mix instead of needing a member per
    # account. Default False (strict per-account equality). Cross-PROVIDER
    # retarget is a further opt-in below.
    "warm_pool_account_retarget_enabled": False,
    "warm_pool_cross_provider_retarget_enabled": False,
    # Cloud Run Jobs routing allowlist. When non-empty, task types listed here
    # that are also in CLOUD_RUN_CANDIDATE_TASK_TYPES (all read-only types) are
    # dispatched to Cloud Run Jobs instead of a dedicated e2-standard-2 GCE VM.
    # Default EMPTY — no types are routed to Cloud Run until an operator adds
    # them, so the change is completely inert on merge. Validated at PATCH time:
    # only read-only (non-merging) task types are accepted.
    # Set via /api/admin/settings.
    "cloud_run_task_type_allowlist": [],
    # Operator-approved, audited Alembic migration-divergence exemptions. Each
    # entry is a dict carrying the AuthorizedDivergence match fields (revision,
    # parent_revision, deployed_identity, integration_identity, created_table,
    # integration_revision) PLUS audit metadata (added_by, added_at ISO-8601,
    # reason). The effective exemption list the deployed migration-safety gates
    # consult is (baked AUTHORIZED_DIVERGENCES) UNION (valid entries here), so a
    # NEW operator-approved exemption is honored WITHOUT a redeploy — see
    # migration_reconciliation.effective_authorized_divergences(). Default EMPTY
    # (no runtime exemptions until an operator adds one), so the change is inert
    # on merge. Malformed entries are dropped defensively on rehydrate — this
    # value gates a safety check and a garbage row must never weaken it.
    "migration_divergence_exemptions": [],
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
    # Per-project concurrent-VM cap ({project_id: int}). OPTIONAL — an EMPTY map
    # (the default) means NO per-project cap, preserving today's behavior exactly.
    # A non-zero entry for a project bounds how many CONCURRENT VMs (PROVISIONING
    # or IN_PROGRESS tasks) that single project may occupy. Candidates from a
    # project at-or-over its cap are DEFERRED for that dispatch cycle (skip-not-
    # block: stay QUEUED, never failed, never blocking other projects). They
    # dispatch normally once the project's active count drops below the cap.
    # Values are clamped to 1..50; 0 or absent means no cap for that project.
    # Set via /api/admin/settings. Read by resolve_project_concurrency_cap.
    "max_concurrent_tasks_by_project": {},
    # Per-project concurrent compute-job cap ({project_id: int}). OPTIONAL — an
    # EMPTY map (the default) means NO per-project cap, preserving today's behavior.
    # A non-zero entry bounds how many concurrent active ComputeJob rows a project
    # may have simultaneously. Launch is rejected (ComputeLaunchPreconditionFailed)
    # when the project is at or above its cap. 0 or absent means no cap.
    # Values are clamped to 1..50. Set via /api/admin/settings.
    # Read by resolve_project_compute_job_concurrency_cap.
    "max_concurrent_compute_jobs_by_project": {},
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
    # SOFT TTL (seconds) for a lane-scoped workspace reservation set on pool
    # member release. A member returned to IDLE with a lane id is held for the
    # matching lane's next phase for this window. The maintainer's liveness-aware
    # sweep RE-STAMPS this forward while the owning lane is still PARKED awaiting
    # its fix (bounded by warm_pool_lane_reservation_hard_cap_seconds), so a soft
    # expiry no longer immediately surrenders the member mid-review. Re-grounded
    # from 15 min to 30 min: a lane-arm review is deliberately forced COLD, so the
    # reservation must span cold VM provisioning + a full review agent run +
    # evaluation + the fix's queue/dispatch latency. The admin lane-diag endpoint
    # (GET /api/admin/lane-diag) per-PR ``review_seconds`` medians show that cold
    # window routinely exceeds 15 min; 30 min comfortably spans a real cold review
    # before the liveness sweep even has to extend it. Inert when
    # improvement_lanes_enabled is False.
    "warm_pool_lane_reservation_ttl_seconds": 1800,
    # HARD absolute ceiling (seconds) on a lane reservation's TOTAL lifetime. The
    # liveness-aware sweep may re-stamp the soft TTL above to keep a member
    # reserved across a long review, but it can NEVER extend past this cap (anchored
    # by PoolVm.reservation_hard_expires_at, stamped once at reservation creation
    # and never re-stamped). Once the cap elapses the sweep ALWAYS frees the member
    # even when the lane still looks alive, so a stuck/zombie lane cannot pin a warm
    # VM indefinitely. Defaults to 90 min — generous headroom over the re-grounded
    # 30 min soft TTL for a worst-case slow review while still bounding the pin.
    "warm_pool_lane_reservation_hard_cap_seconds": 5400,
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
    # Rate-limit tail optimization: when enabled, a RATE_LIMIT / provider-quota
    # requeue may keep its warm VM alive and re-claim the same task on an
    # alternate account instead of tearing the VM down immediately. Ships dark
    # until the agent-side park loop also lands.
    "park_and_retarget_enabled": False,
    # Bounded hold window for the park-and-retarget path. Must stay below the
    # stale retry_after sweep's far-future guard, so the default is conservative
    # and admin validation clamps the operator override.
    "park_and_retarget_budget_seconds": 600,
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
    # Qualifier promotion remains separately gated and uses promotion PR CI.
    "promotion_enabled": False,
    # Ordinary post-promotion deployment smoke only; never a qualifier child.
    "smoketest_enabled": False,
    # walkthrough_e2e_enabled gates the qualifier-spawned ``walkthrough_e2e``
    # child task (the live-stack Playwright walkthrough evidence lane).
    # Defaults True: the walkthrough_e2e lane is armed fleet-wide, producing
    # SHA-pinned CriterionEvidence for the click-to-talk acceptance criterion.
    # Failure is evidence-only (never cascades a fix).  Operator-tunable via
    # runtime_config_json; persisted across restarts.
    "walkthrough_e2e_enabled": True,
    # Per-project walkthrough_e2e enablement overrides ({project_id: bool}).
    # An EMPTY map (the default) means every project falls back to the fleet-wide
    # ``walkthrough_e2e_enabled`` above.  A per-project entry can HOLD the
    # walkthrough evidence lane for one project without touching the fleet default.
    # Mirrors ``promotion_enabled_by_project``.
    "walkthrough_e2e_enabled_by_project": {},
    # acceptance_earn_execution_trials_enabled gates the acceptance earn-loop's
    # execution-trial lane: when ON, the reaper step-21a backfill enrolls a bounded
    # ``criterion_coverage_trial`` that actually RUNS a merged task's declared
    # named_test covering tests and, on green, mints the machine-verified
    # ``covered`` evidence tier (instead of only the lower-confidence ``backfilled``
    # tier). Ships DARK (default False): the lane spawns real targeted test runs,
    # so it is armed fleet-wide only once proven. When off, the backfill is
    # byte-for-byte identical to the backfilled-only behaviour. Operator-tunable
    # via PATCH /api/admin/settings (runtime_config_json); persisted across restarts.
    "acceptance_earn_execution_trials_enabled": False,
    # env_preflight_enabled gates the boot-prerequisite hard gate (Slice 3).
    # Armed True: a new candidate must declare its required boot-time env vars;
    # missing or absent declarations block promotion.  This gate was built to
    # prevent the v25 GITHUB_PAT_ENCRYPTION_KEY prod boot-crash.
    "env_preflight_enabled": True,
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
    # The effective default stays the shipped 0.90 / 4:3 audit-to-erosion rubric.
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
    # Minimum fraction of meter-eligible accounts that must have a fresh known
    # snapshot before usage_headroom_gate_enabled is considered safe to arm.
    # Expressed as an integer percentage (0..100). Default 80 is conservative:
    # at least 4 out of 5 eligible accounts must report fresh data. An operator
    # can lower this to 0 to arm regardless of coverage (not recommended).
    "usage_headroom_min_coverage_pct": 80,
    # Tri-state control for autonomously arming ``usage_headroom_gate_enabled``
    # from sustained meter reliability (the ``armable`` verdict computed by
    # get_meter_reliability_stats). The leader orchestrator runs a reconciler on
    # the usage-meter cadence:
    #   "auto"   — the leader ARMS the gate once ``armable`` has held True
    #              CONTINUOUSLY across usage_headroom_auto_arm_observation_minutes,
    #              and fail-safe DISARMS it immediately the instant ``armable``
    #              regresses (no window on the way down).
    #   "manual" — the reconciler never touches the gate; the operator owns
    #              usage_headroom_gate_enabled (today's behavior).
    #   "off"    — force-disarm kill switch: the reconciler drives the gate to
    #              False and holds it there.
    # DEFAULT MUST be "auto": defaulting to "manual"/"off" would reproduce the
    # exact 'a human must flip it' bug this closed loop exists to fix.
    "usage_headroom_auto_arm_mode": "auto",
    # Minutes the ``armable`` verdict must hold CONTINUOUSLY before auto mode
    # arms the gate — sustained above threshold, not a single instantaneous
    # sample, so a transient healthy blip does not arm. Disarm is always
    # immediate (fail-safe); this window governs arming only.
    "usage_headroom_auto_arm_observation_minutes": 60,
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
    # Deterministic holdback fraction for the language-aware repo-map trial.
    # Tasks whose stable per-task hash falls below this threshold run WITHOUT
    # the repo map, allowing digest_metrics' without_repo_map arm to be
    # populated by real holdback runs rather than map-build failures only.
    # Default-on trial: keep a small holdback arm alive until the capability
    # gets a durable verdict. Operators can still dial it to 0.0 to disable the
    # trial or override it per deployment via runtime_config_json.
    "repo_map_holdback_fraction": 0.1,
    # Deterministic holdback fractions for the three newly-assignment-based
    # capability trials (module_interfaces, related_tests, affected_consumers).
    # Conservative 0.1 default keeps a small holdback arm alive so trials can
    # accumulate intent-to-treat evidence. Operators can dial up/down via
    # runtime_config_json or PATCH /api/admin/settings once adoption signals
    # are visible in the digest.
    "module_interfaces_holdback_fraction": 0.1,
    "related_tests_holdback_fraction": 0.1,
    "affected_consumers_holdback_fraction": 0.1,
    # Deterministic holdback fraction for the diagnostics-after-edit trial.
    # Mirrors repo_map_holdback_fraction semantics: tasks hashed below this
    # threshold run WITHOUT the post-edit diagnostics hook, populating the
    # without-arm for intent-to-treat analysis. Default 10% holdback.
    "diagnostics_after_edit_holdback_fraction": 0.1,
    # Kill switch for the diagnostics-after-edit capability trial. When False,
    # the agent must not arm the post-edit diagnostics hook regardless of arm
    # assignment. Default True (trial active).
    "diagnostics_after_edit_enabled": True,
    # Gap-scoped recently-retired direction block (direction_gap_respawn_block_mode).
    # When a direction is proposed for a gap that already has a recently-retired
    # direction, this mode controls whether the respawn is blocked:
    #   observe  — record the match, never block (default).
    #   enforce  — block same-gap re-spawns within the cooldown window.
    # Registered here so operators can graduate from observe→enforce via
    # PATCH /api/admin/settings without a redeploy.
    "direction_gap_respawn_block_mode": "observe",
    # Cooldown window (days) for the gap-respawn block. A retired direction blocks
    # same-gap re-spawns for this many days after retirement. Default 14.
    "direction_gap_respawn_cooldown_days": 14,
}


def config_get(key: str):
    """Get a runtime config value by its registered key.

    The ``runtime_config`` dict IS the registry — every callable key has a
    registered default baked in at module load.  Use this instead of
    ``runtime_config.get(key, hardcoded_fallback)`` so the fallback always comes
    from the registration itself, not a per-call literal that can silently drift.

    Raises ``KeyError`` for unregistered keys so drift is caught at development
    time rather than silently defaulting to an inconsistent value.
    """
    return runtime_config[key]


# Completion-debt seed: runtime-config knobs whose current default/observe value
# means an obligation is standing open (a feature ships dark, or a capability is
# measured-but-not-enforced). ``iter_dormant_knob_debts`` converts these into
# ``dormant_knob`` completion-debt seed rows.  Kept as literal kind/source
# strings (mirroring ``app.completion_debt``) so this module stays free of any
# import edge onto the ledger.
_DORMANT_KNOB_VALUES = frozenset({"off", "observe"})
_DORMANT_MODE_KNOBS = {
    "evergreen_fill_mode": (
        "Evergreen auto-fill capability enabled but not enforcing (off/observe)"
    ),
    "structural_health_audit_mode": (
        "Structural-health audit measuring but not enrolling findings"
    ),
    "metrics_integrity_sweep_mode": (
        "Metrics-integrity sweep recomputing counters but not enrolling drift findings"
    ),
    "autonomous_spawn_budget_mode": (
        "Autonomous spawn-budget mode observe-only pending dedicated rollout"
    ),
    "direction_gap_respawn_block_mode": (
        "Direction-gap respawn block in observe-only mode"
    ),
    # completion_debt_weight_mode graduated to "enforce" (decision #7) — no
    # longer dormant, so it is intentionally absent from this registry.
    "capability_proof_gate_mode": (
        "Capability proof gate not yet enforcing (off/observe)"
    ),
}
# Master feature flags that ship OFF still owe an enablement decision.
_DORMANT_OFF_FLAGS: dict = {}


def iter_dormant_knob_debts() -> list[dict]:
    """Enumerate runtime-config knobs currently in a dormant/default-off/observe
    state as completion-debt seed rows.

    Each returned dict is a kwargs bundle for
    ``app.completion_debt.record_completion_debt`` (stable ``dedupe_key``, the
    ``dormant_knob`` kind, a human ``title``, and bounded ``detail`` capturing
    the knob's current value). Deployment-global — no ``project_id``.
    """
    debts: list[dict] = []
    for key, title in _DORMANT_MODE_KNOBS.items():
        value = str(runtime_config.get(key, "")).lower()
        if value in _DORMANT_KNOB_VALUES:
            debts.append(
                {
                    "dedupe_key": f"dormant_knob:{key}",
                    "kind": "dormant_knob",
                    "title": title,
                    "project_id": None,
                    "source": "runtime_config_state",
                    "detail": {"knob": key, "value": value},
                }
            )
    for key, title in _DORMANT_OFF_FLAGS.items():
        if not runtime_config.get(key, False):
            debts.append(
                {
                    "dedupe_key": f"dormant_knob:{key}",
                    "kind": "dormant_knob",
                    "title": title,
                    "project_id": None,
                    "source": "runtime_config_state",
                    "detail": {"knob": key, "value": bool(runtime_config.get(key))},
                }
            )
    return debts


# Promotion-policy key set — the rubric controls that live in runtime_config.
# Used both by the seed below and by _apply_settings validation.
PROMOTION_POLICY_KEYS = ("promotion_threshold", "promotion_weights")
_PROMOTION_WEIGHT_KEYS = ("audit", "test_erosion")
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
    test_erosion, with an optional legacy smoketest key, float-coercible and summing to ~1.0) and a
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
        legacy_smoke = float(weights_raw.get("smoketest", 0.0))
        threshold = float(parsed["threshold"])
        weight_sum = sum(weights.values()) + legacy_smoke
        if abs(weight_sum - 1.0) > 0.01:
            raise ValueError(
                f"promotion_policy.yaml weights sum to {weight_sum:.4f}, expected ~1.0"
            )
        from decimal import Decimal

        active_decimals = {
            key: Decimal(str(weights_raw[key])) for key in _PROMOTION_WEIGHT_KEYS
        }
        active_sum = sum(active_decimals.values(), Decimal("0"))
        if active_sum <= 0:
            raise ValueError("active promotion weights must be positive")
        weights = {
            key: float(value / active_sum) for key, value in active_decimals.items()
        }
        # Optional completion_debt blend factor — SEPARATE from the active core
        # weights that must sum to ~1.0 (mirrors release_qualifier resolution).
        # Read + preserve it (default 0.0, clamped to [0, 0.5]) so a YAML rubric
        # that documents a completion_debt weight seeds it into runtime_config.
        from app.release_scoring import coerce_completion_debt_weight

        _cd = coerce_completion_debt_weight(weights_raw)
        if _cd > 0.0:
            weights["completion_debt"] = _cd
        result: dict = {"weights": weights, "threshold": threshold}
        if "merge_grade_block_threshold" in parsed:
            try:  # noqa: SIM105
                result["merge_grade_block_threshold"] = float(
                    parsed["merge_grade_block_threshold"]
                )
            except (TypeError, ValueError):
                pass
        # Slice 3 (design 245212a4): seed the deployed-side env-preflight gate
        # flag so _policy_from_runtime_config() carries it and the short-circuit
        # path (runtime_config has promotion_threshold) does NOT silently drop it.
        if "env_preflight_enabled" in parsed:
            result["env_preflight_enabled"] = bool(parsed["env_preflight_enabled"])
        return result
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
    if "merge_grade_block_threshold" in policy:
        runtime_config["merge_grade_block_threshold"] = policy[
            "merge_grade_block_threshold"
        ]
    if "env_preflight_enabled" in policy:
        runtime_config["env_preflight_enabled"] = policy["env_preflight_enabled"]


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


def _copy_default_provider_matrix(provider: str) -> dict[str, dict[str, Any]]:
    raw = config_get("provider_settings_by_effort").get(provider, {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(level): dict(bag)
        for level, bag in raw.items()
        if isinstance(level, str) and isinstance(bag, dict)
    }


def _ensure_route_defaults(
    category_routing: dict[str, Any], category: str
) -> dict[str, Any]:
    entry = category_routing.get(category)
    if not isinstance(entry, dict):
        default_entry = runtime_config["category_routing"].get(category) or {}
        entry = {
            "providers": list(default_entry.get("providers") or []),
            "effort": default_entry.get("effort") or "medium",
        }
        category_routing[category] = entry
    providers = entry.get("providers")
    if not isinstance(providers, list) or not providers:
        default_entry = runtime_config["category_routing"].get(category) or {}
        entry["providers"] = list(default_entry.get("providers") or [])
    if not isinstance(entry.get("effort"), str) or not entry.get("effort"):
        default_entry = runtime_config["category_routing"].get(category) or {}
        entry["effort"] = default_entry.get("effort") or "medium"
    return entry


# Last warning set emitted by the DeepSeek-first routing check in
# _backfill_provider_runtime_defaults; the check runs on every config sync,
# so it logs only when the set CHANGES. None = never evaluated.
_last_deepseek_routing_warnings: list[str] | None = None


def _validate_deepseek_routing_invariant(config: Dict[str, Any]) -> list[str]:
    """Return warnings when DeepSeek is not first for low/medium-effort categories.

    Categories _default, initiative, build, and ops route low/medium-effort
    tasks and should prefer DeepSeek as the primary provider. This invariant
    guards against silent regression in the provider ordering, which matters
    when ``provider_selection_mode`` is ``"preferred"`` (the default).
    """
    warnings: list[str] = []
    cat_routing = config.get("category_routing", {})
    deepseek_first = {"_default", "initiative", "build", "ops"}
    for category in sorted(deepseek_first):
        entry = cat_routing.get(category)
        if not isinstance(entry, dict):
            continue
        providers = entry.get("providers")
        if not isinstance(providers, list) or not providers:
            continue
        if providers[0] != "deepseek":
            warnings.append(
                f"category_routing.{category}: expected DeepSeek first, got {providers[0]}"
            )
    return warnings


def _backfill_provider_runtime_defaults(config: Dict[str, Any]) -> None:
    """Add provider defaults introduced after the settings-bag migration.

    Existing routes are operator-owned and must remain byte-for-byte stable.
    This helper runs on every persisted-config sync, not just once during an
    upgrade, so rewriting an existing provider list here would silently undo a
    successful settings PATCH on the next sync cycle. Missing categories are
    still seeded from the current defaults by ``_ensure_route_defaults``.
    """
    categories = config.setdefault("task_type_categories", {})
    if isinstance(categories, dict):
        default_categories = runtime_config.get("task_type_categories", {})
        if isinstance(default_categories, dict):
            for category, members in default_categories.items():
                if isinstance(members, list):
                    categories.setdefault(category, list(members))

        strategic_members = categories.get("strategic")
        if isinstance(strategic_members, list):
            categories["strategic"] = [
                member
                for member in strategic_members
                if member not in ("research", "plan")
            ]

        initiative_members = categories.setdefault("initiative", [])
        if isinstance(initiative_members, list):
            for task_type in ("research", "plan"):
                present_elsewhere = any(
                    isinstance(members, list) and task_type in members
                    for members in categories.values()
                    if members is not initiative_members
                )
                if not present_elsewhere and task_type not in initiative_members:
                    initiative_members.append(task_type)

    openai_defaults_upgraded = False
    tier_map = config.setdefault("provider_tier_model_map", {})
    if isinstance(tier_map, dict):
        # The live pre-5.6 config used gpt-5.5 for both upper tiers and
        # gpt-5.4-mini for haiku. That shape was a rollout constraint (the old
        # backend could not validate 5.6 ids), not an operator preference. Only
        # rewrite the exact known shape so custom/partially pinned maps remain
        # operator-owned.
        openai_tiers = tier_map.get("openai")
        old_openai_defaults = {
            "opus": "gpt-5.5",
            "sonnet": "gpt-5.5",
            "haiku": "gpt-5.4-mini",
        }
        openai_defaults_upgraded = isinstance(openai_tiers, dict) and all(
            openai_tiers.get(tier) == model
            for tier, model in old_openai_defaults.items()
        )
        if openai_defaults_upgraded:
            current_openai_defaults = runtime_config.get(
                "provider_tier_model_map", {}
            ).get("openai", {})
            if isinstance(current_openai_defaults, dict):
                for tier in ("opus", "sonnet", "haiku"):
                    model = current_openai_defaults.get(tier)
                    if isinstance(model, str) and model:
                        openai_tiers[tier] = model

        for provider in ("zai", "deepseek"):
            defaults = runtime_config.get("provider_tier_model_map", {}).get(provider)
            if not isinstance(defaults, dict):
                continue
            target = tier_map.setdefault(provider, {})
            if isinstance(target, dict):
                for tier, model in defaults.items():
                    target.setdefault(tier, model)

    defaults_map = config.setdefault("provider_settings_defaults", {})
    if isinstance(defaults_map, dict):
        for provider in ("zai", "deepseek"):
            defaults_map.setdefault(provider, {})

    effort_matrix = config.setdefault("provider_settings_by_effort", {})
    if isinstance(effort_matrix, dict):
        if openai_defaults_upgraded:
            # Keep each effort cell's non-model knobs, but advance only models
            # from the known old default family. A custom model in any cell is
            # an explicit operator choice and is left untouched.
            openai_matrix = effort_matrix.get("openai")
            current_openai_matrix = runtime_config.get(
                "provider_settings_by_effort", {}
            ).get("openai", {})
            known_old_by_effort = {
                "low": {"gpt-5.4-mini"},
                "medium": {"gpt-5.4-mini", "gpt-5.4", "gpt-5.5"},
                "high": {"gpt-5.4", "gpt-5.5"},
                "max": {"gpt-5.4", "gpt-5.5"},
            }
            if isinstance(openai_matrix, dict) and isinstance(
                current_openai_matrix, dict
            ):
                for effort, old_models in known_old_by_effort.items():
                    bag = openai_matrix.get(effort)
                    default_bag = current_openai_matrix.get(effort)
                    if (
                        isinstance(bag, dict)
                        and bag.get("model") in old_models
                        and isinstance(default_bag, dict)
                        and isinstance(default_bag.get("model"), str)
                    ):
                        bag["model"] = default_bag["model"]

        for provider in ("zai", "deepseek"):
            defaults = _copy_default_provider_matrix(provider)
            target = effort_matrix.setdefault(provider, {})
            if not isinstance(target, dict):
                effort_matrix[provider] = defaults
                continue
            for effort, bag in defaults.items():
                target.setdefault(effort, dict(bag))

    model_preferences = config.setdefault("task_type_model_preferences", {})
    if isinstance(model_preferences, dict):
        default_preferences = runtime_config.get("task_type_model_preferences", {})
        smoke_default = (
            default_preferences.get("smoketest")
            if isinstance(default_preferences, dict)
            else None
        )
        # Missing means this deployment predates the Codex compatibility smoke.
        # An explicit smoketest entry (including []) is operator-owned.
        if "smoketest" not in model_preferences and isinstance(smoke_default, list):
            model_preferences["smoketest"] = [
                dict(pref) for pref in smoke_default if isinstance(pref, dict)
            ]

    category_routing = config.setdefault("category_routing", {})
    if isinstance(category_routing, dict):
        for category in ("_default", "strategic", "initiative", "build", "review", "ops"):
            _ensure_route_defaults(category_routing, category)

    if config.get("provider_selection_mode") not in (
        "balanced",
        "preferred",
        "exclusive",
    ):
        config["provider_selection_mode"] = runtime_config["provider_selection_mode"]

    # Observability only — detection is decoupled from repair. Operator routes
    # are preserved verbatim above, so a route that violates the DeepSeek-first
    # cost default is a deliberate choice; log it ONCE per distinct warning set
    # (this runs on every config sync, so an unconditional log would spam).
    global _last_deepseek_routing_warnings
    routing_warnings = _validate_deepseek_routing_invariant(config)
    if routing_warnings != _last_deepseek_routing_warnings:
        _last_deepseek_routing_warnings = routing_warnings
        if routing_warnings:
            logger.warning(
                "DeepSeek-first routing default not met (operator routing is "
                "preserved; informational only): %s",
                "; ".join(routing_warnings),
            )


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
    had_unified_provider_config = any(
        key in config
        for key in (
            "category_routing",
            "task_type_routing",
            "provider_settings_by_effort",
            "provider_settings_defaults",
        )
    )

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
            ) or ["deepseek", "anthropic", "openai", "gemini"]
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

    if had_unified_provider_config:
        _backfill_provider_runtime_defaults(config)

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
                    "inline_llm",
                    "provider_selection_mode",
                    "cross_provider_fallback_enabled",
                    "agent_prompt_suffixes",
                    "provider_settings_defaults",
                    "task_type_provider_overrides",
                    "autonomous_periodic_tasks",
                    "health_check_urls",
                    "deploy_repo_identity",
                ):
                    if key in config_json:
                        _set(key, config_json[key])
                if "notification_config" in config_json:
                    nc = config_json["notification_config"]
                    if isinstance(nc, dict) and nc.get("webhook_url"):
                        from app.field_encryption import decrypt_field
                        nc = {**nc, "webhook_url": decrypt_field(nc["webhook_url"]) or ""}
                    _set("notification_config", nc)
                if "autonomous_spawn_budget_mode" in config_json:
                    raw_mode = (
                        str(config_json["autonomous_spawn_budget_mode"] or "")
                        .strip()
                        .lower()
                    )
                    if raw_mode in {"off", "observe", "enforce"}:
                        _set("autonomous_spawn_budget_mode", raw_mode)
                if "max_global_autonomous_initiative_tasks_per_hour" in config_json:
                    try:
                        raw_cap = int(
                            config_json[
                                "max_global_autonomous_initiative_tasks_per_hour"
                            ]
                        )
                    except (TypeError, ValueError):
                        raw_cap = -1
                    if 0 <= raw_cap <= 500:
                        _set("max_global_autonomous_initiative_tasks_per_hour", raw_cap)
                if "autonomous_spawn_budget_categories" in config_json:
                    raw_categories = config_json["autonomous_spawn_budget_categories"]
                    if isinstance(raw_categories, dict):
                        normalized_categories: dict[str, list[str]] = {}
                        valid_categories = all(
                            str(key) in {"initiative", "servicing"}
                            for key in raw_categories
                        )
                        for category_key, default_types in (
                            ("initiative", AUTONOMOUS_SPAWN_INITIATIVE_TASK_TYPES),
                            ("servicing", AUTONOMOUS_SPAWN_SERVICING_TASK_TYPES),
                        ):
                            if not valid_categories:
                                break
                            raw_types = raw_categories.get(category_key, default_types)
                            if isinstance(raw_types, str):
                                raw_types = raw_types.split(",")
                            if not isinstance(raw_types, (list, tuple, set)):
                                valid_categories = False
                                break
                            normalized_types = sorted(
                                {str(t).strip() for t in raw_types if str(t).strip()}
                            )
                            if any(t not in ALL_TASK_TYPES for t in normalized_types):
                                valid_categories = False
                                break
                            normalized_categories[category_key] = normalized_types
                        overlap = set(
                            normalized_categories.get("initiative", [])
                        ) & set(normalized_categories.get("servicing", []))
                        if valid_categories and not overlap:
                            _set(
                                "autonomous_spawn_budget_categories",
                                normalized_categories,
                            )
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
                if "strategic_challenge_gate_mode" in config_json:
                    raw_mode = (
                        str(config_json["strategic_challenge_gate_mode"])
                        .strip()
                        .lower()
                    )
                    if raw_mode in {"observe", "advisory", "enforce"}:
                        _set("strategic_challenge_gate_mode", raw_mode)
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
                if "monthly_budget_target_usd" in config_json:
                    _set(
                        "monthly_budget_target_usd",
                        float(config_json["monthly_budget_target_usd"]),
                    )
                if "compute_experiment_spend_threshold_usd" in config_json:
                    _set(
                        "compute_experiment_spend_threshold_usd",
                        float(config_json["compute_experiment_spend_threshold_usd"]),
                    )
                for _int_key in (
                    "strategic_continuation_ttl_seconds",
                    "strategic_local_hold_timeout_seconds",
                    "strategic_session_idle_tick_seconds",
                    "strategic_session_turn_timeout_seconds",
                    "strategic_session_min_tick_seconds",
                    "strategic_session_max_ops_per_turn",
                    "strategic_session_autonomous_turn_timeout_seconds",
                    "strategic_session_max_unattended_seconds",
                ):
                    if _int_key in config_json:
                        try:
                            _set(_int_key, int(config_json[_int_key]))
                        except (TypeError, ValueError):
                            pass
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
                if "rollup_merge_queue_enabled" in config_json:
                    _set(
                        "rollup_merge_queue_enabled",
                        bool(config_json["rollup_merge_queue_enabled"]),
                    )
                if "evergreen_fill_deferred_memo_enrollment" in config_json:
                    _set(
                        "evergreen_fill_deferred_memo_enrollment",
                        bool(config_json["evergreen_fill_deferred_memo_enrollment"]),
                    )
                if "direction_gap_respawn_block_mode" in config_json:
                    raw_mode = (
                        str(config_json["direction_gap_respawn_block_mode"] or "")
                        .strip()
                        .lower()
                    )
                    if raw_mode in ("observe", "enforce"):
                        _set("direction_gap_respawn_block_mode", raw_mode)
                if "direction_gap_respawn_cooldown_days" in config_json:
                    try:
                        _cooldown = float(
                            config_json["direction_gap_respawn_cooldown_days"]
                        )
                        if 1.0 <= _cooldown <= 365.0:
                            _set("direction_gap_respawn_cooldown_days", _cooldown)
                    except (TypeError, ValueError):
                        pass
                for _guardrail_int_key in (
                    "direction_churn_window_cycles",
                    "direction_churn_non_merge_threshold",
                    "direction_lifetime_non_merge_threshold",
                    "direction_review_recommendation_threshold",
                ):
                    if _guardrail_int_key in config_json:
                        try:  # noqa: SIM105
                            _set(
                                _guardrail_int_key,
                                int(config_json[_guardrail_int_key]),
                            )
                        except (TypeError, ValueError):
                            pass
                for _guardrail_float_key in (
                    "direction_churn_min_merge_rate",
                    "direction_lifetime_min_merge_rate",
                ):
                    if _guardrail_float_key in config_json:
                        try:
                            _fval = float(config_json[_guardrail_float_key])
                            if 0.0 <= _fval <= 1.0:
                                _set(_guardrail_float_key, _fval)
                        except (TypeError, ValueError):
                            pass
                if "evergreen_fill_mode" in config_json:
                    raw_mode = (
                        str(config_json["evergreen_fill_mode"] or "").strip().lower()
                    )
                    if raw_mode in ("off", "observe", "enforce"):
                        _set("evergreen_fill_mode", raw_mode)
                if "structural_health_audit_mode" in config_json:
                    raw_mode = (
                        str(config_json["structural_health_audit_mode"] or "").strip().lower()
                    )
                    if raw_mode in ("off", "observe", "enforce"):
                        _set("structural_health_audit_mode", raw_mode)
                if "completion_debt_weight_mode" in config_json:
                    raw_mode = (
                        str(config_json["completion_debt_weight_mode"] or "").strip().lower()
                    )
                    if raw_mode in ("off", "observe", "enforce"):
                        _set("completion_debt_weight_mode", raw_mode)
                if "structural_health_audit_interval_minutes" in config_json:
                    try:  # noqa: SIM105
                        _set(
                            "structural_health_audit_interval_minutes",
                            int(config_json["structural_health_audit_interval_minutes"]),
                        )
                    except (TypeError, ValueError):
                        pass
                if "structural_health_audit_fat_row_bytes" in config_json:
                    try:  # noqa: SIM105
                        _set(
                            "structural_health_audit_fat_row_bytes",
                            int(config_json["structural_health_audit_fat_row_bytes"]),
                        )
                    except (TypeError, ValueError):
                        pass
                # Metrics-integrity sweep: mode + bounded numeric knobs. Persisted
                # so an operator enable/tune survives a Cloud Run restart and
                # reaches the leader across instances (mirrors the structural-health
                # audit knobs above).
                if "metrics_integrity_sweep_mode" in config_json:
                    raw_mode = (
                        str(config_json["metrics_integrity_sweep_mode"] or "")
                        .strip()
                        .lower()
                    )
                    if raw_mode in ("off", "observe", "enforce"):
                        _set("metrics_integrity_sweep_mode", raw_mode)
                # Capability proof gate: mode + auto-graduation config. Persisted
                # so an operator enable/tune survives a Cloud Run restart and
                # reaches the leader across instances.
                if "capability_proof_gate_mode" in config_json:
                    raw_mode = (
                        str(config_json["capability_proof_gate_mode"] or "")
                        .strip()
                        .lower()
                    )
                    if raw_mode in ("off", "observe", "enforce"):
                        _set("capability_proof_gate_mode", raw_mode)
                if "capability_proof_gate_auto_graduation_enabled" in config_json:
                    _set(
                        "capability_proof_gate_auto_graduation_enabled",
                        bool(config_json["capability_proof_gate_auto_graduation_enabled"]),
                    )
                if "capability_proof_execution_trials_enabled" in config_json:
                    _set(
                        "capability_proof_execution_trials_enabled",
                        bool(config_json["capability_proof_execution_trials_enabled"]),
                    )
                if "acceptance_earn_execution_trials_enabled" in config_json:
                    _set(
                        "acceptance_earn_execution_trials_enabled",
                        bool(config_json["acceptance_earn_execution_trials_enabled"]),
                    )
                for _cap_grad_float_key in (
                    "capability_proof_gate_auto_graduation_observe_threshold",
                    "capability_proof_gate_auto_graduation_enforce_threshold",
                ):
                    if _cap_grad_float_key in config_json:
                        try:
                            _fval = float(config_json[_cap_grad_float_key])
                            if 0.0 <= _fval <= 1.0:
                                _set(_cap_grad_float_key, _fval)
                        except (TypeError, ValueError):
                            pass
                for _mi_int_key in (
                    "metrics_integrity_sweep_interval_minutes",
                    "metrics_integrity_sweep_max_checks",
                    "metrics_integrity_sweep_max_projects",
                    "metrics_integrity_sweep_sample_limit",
                    "metrics_integrity_sweep_window_days",
                ):
                    if _mi_int_key in config_json:
                        try:
                            _mi_val = int(config_json[_mi_int_key])
                            if _mi_val > 0:
                                _set(_mi_int_key, _mi_val)
                        except (TypeError, ValueError):
                            pass
                # Message supersession: mode + bounded numeric margins. Persisted
                # so an operator enable/tune survives a Cloud Run restart and
                # reaches the leader across instances (mirrors the metrics-integrity
                # sweep knobs above).
                if "message_supersession_mode" in config_json:
                    raw_mode = (
                        str(config_json["message_supersession_mode"] or "")
                        .strip()
                        .lower()
                    )
                    if raw_mode in ("off", "observe", "enforce"):
                        _set("message_supersession_mode", raw_mode)
                for _ms_int_key in (
                    "message_supersession_min_age_days",
                    "message_supersession_min_versions_back",
                    "message_supersession_max_resolve",
                ):
                    if _ms_int_key in config_json:
                        try:
                            _ms_val = int(config_json[_ms_int_key])
                            if _ms_val > 0:
                                _set(_ms_int_key, _ms_val)
                        except (TypeError, ValueError):
                            pass
                if "cochange_decomposition_enabled" in config_json:
                    _set(
                        "cochange_decomposition_enabled",
                        bool(config_json["cochange_decomposition_enabled"]),
                    )
                if "adversarial_spawn_gate_enabled" in config_json:
                    _set(
                        "adversarial_spawn_gate_enabled",
                        bool(config_json["adversarial_spawn_gate_enabled"]),
                    )
                if "adversarial_spawn_gate_infra_failure_mode" in config_json:
                    raw_mode = (
                        str(
                            config_json["adversarial_spawn_gate_infra_failure_mode"]
                            or ""
                        )
                        .strip()
                        .lower()
                    )
                    if raw_mode in ("observe", "enforce"):
                        _set("adversarial_spawn_gate_infra_failure_mode", raw_mode)
                if "warm_retarget_enabled" in config_json:
                    _set(
                        "warm_retarget_enabled",
                        bool(config_json["warm_retarget_enabled"]),
                    )
                if "park_and_retarget_enabled" in config_json:
                    _set(
                        "park_and_retarget_enabled",
                        bool(config_json["park_and_retarget_enabled"]),
                    )
                if "park_and_retarget_budget_seconds" in config_json:
                    try:
                        raw_budget = int(
                            config_json["park_and_retarget_budget_seconds"]
                        )
                    except (TypeError, ValueError):
                        raw_budget = 600
                    _set(
                        "park_and_retarget_budget_seconds",
                        max(30, min(1800, raw_budget)),
                    )
                if "usage_headroom_gate_enabled" in config_json:
                    _set(
                        "usage_headroom_gate_enabled",
                        bool(config_json["usage_headroom_gate_enabled"]),
                    )
                if "usage_headroom_auto_arm_mode" in config_json:
                    raw_mode = str(
                        config_json["usage_headroom_auto_arm_mode"]
                    ).strip().lower()
                    if raw_mode in ("auto", "manual", "off"):
                        _set("usage_headroom_auto_arm_mode", raw_mode)
                if "usage_headroom_auto_arm_observation_minutes" in config_json:
                    try:
                        raw_obs = int(
                            config_json["usage_headroom_auto_arm_observation_minutes"]
                        )
                    except (TypeError, ValueError):
                        raw_obs = 60
                    _set(
                        "usage_headroom_auto_arm_observation_minutes",
                        max(1, min(1440, raw_obs)),
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
                if "improvement_lanes_enabled" in config_json:
                    _set(
                        "improvement_lanes_enabled",
                        bool(config_json["improvement_lanes_enabled"]),
                    )
                if "review_fix_lane_pilot_enabled" in config_json:
                    _set(
                        "review_fix_lane_pilot_enabled",
                        bool(config_json["review_fix_lane_pilot_enabled"]),
                    )
                if "warm_pool_enabled" in config_json:
                    _set(
                        "warm_pool_enabled",
                        bool(config_json["warm_pool_enabled"]),
                    )
                if "cloud_run_task_type_allowlist" in config_json:
                    # Persisted Cloud Run task-type allowlist. Normalize to a
                    # sorted list of strings — mirroring the admin PATCH surface —
                    # so every instance agrees on the allowlist and the setting
                    # survives across restarts.
                    raw_allow = config_json["cloud_run_task_type_allowlist"]
                    if isinstance(raw_allow, str):
                        raw_allow = raw_allow.split(",")
                    if isinstance(raw_allow, (list, tuple, set)):
                        _set(
                            "cloud_run_task_type_allowlist",
                            sorted({str(t).strip() for t in raw_allow if str(t).strip()}),
                        )
                if "migration_divergence_exemptions" in config_json:
                    # Persisted operator-approved Alembic migration-divergence
                    # exemptions. Validate/normalize each entry through the SAME
                    # helper the resolver uses so the DB store and the live gate
                    # agree on what counts as well-formed, and DROP malformed
                    # rows — this value gates a deploy-safety check, so a garbage
                    # row must never weaken it. Lazy import avoids a module-level
                    # cycle (migration_reconciliation reads runtime_config).
                    raw_exemptions = config_json["migration_divergence_exemptions"]
                    if isinstance(raw_exemptions, (list, tuple)):
                        from app.migration_reconciliation import (
                            normalize_runtime_divergence_exemption,
                        )

                        normalized_exemptions = [
                            norm
                            for entry in raw_exemptions
                            if (norm := normalize_runtime_divergence_exemption(entry))
                            is not None
                        ]
                        _set(
                            "migration_divergence_exemptions",
                            normalized_exemptions,
                        )
                if "warm_pool_account_retarget_enabled" in config_json:
                    _set(
                        "warm_pool_account_retarget_enabled",
                        bool(config_json["warm_pool_account_retarget_enabled"]),
                    )
                if "warm_pool_cross_provider_retarget_enabled" in config_json:
                    _set(
                        "warm_pool_cross_provider_retarget_enabled",
                        bool(config_json["warm_pool_cross_provider_retarget_enabled"]),
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
                        _set("warm_pool_target_size_by_project", normalized_targets)
                if "max_concurrent_tasks_by_project" in config_json:
                    # Persisted per-project concurrent-VM cap. Normalize to a map of
                    # trimmed, non-empty string project-id keys to ints clamped to
                    # 1..50. Values of 0 or less mean "no cap" and are excluded so
                    # resolve_project_concurrency_cap can treat a missing key as
                    # "no cap" without distinguishing 0 from absent.
                    raw_caps = config_json["max_concurrent_tasks_by_project"]
                    if isinstance(raw_caps, dict):
                        normalized_caps: dict[str, int] = {}
                        for proj_key, proj_val in raw_caps.items():
                            sid = str(proj_key).strip()
                            if not sid:
                                continue
                            try:
                                ival = int(proj_val)
                            except (TypeError, ValueError):
                                continue
                            if ival > 0:
                                normalized_caps[sid] = max(1, min(50, ival))
                        _set("max_concurrent_tasks_by_project", normalized_caps)
                if "max_concurrent_compute_jobs_by_project" in config_json:
                    # Persisted per-project concurrent compute-job cap. Same
                    # normalization as max_concurrent_tasks_by_project: keys are
                    # trimmed non-empty project-id strings, values clamped to 1..50,
                    # and 0-or-below entries are excluded (absent key = no cap).
                    raw_job_caps = config_json["max_concurrent_compute_jobs_by_project"]
                    if isinstance(raw_job_caps, dict):
                        normalized_job_caps: dict[str, int] = {}
                        for proj_key, proj_val in raw_job_caps.items():
                            sid = str(proj_key).strip()
                            if not sid:
                                continue
                            try:
                                ival = int(proj_val)
                            except (TypeError, ValueError):
                                continue
                            if ival > 0:
                                normalized_job_caps[sid] = max(1, min(50, ival))
                        _set(
                            "max_concurrent_compute_jobs_by_project",
                            normalized_job_caps,
                        )
                for key in (
                    "direction_refractory_minutes",
                    "direction_saturation_refractory_minutes",
                ):
                    if key in config_json:
                        raw_val = config_json[key]
                        if raw_val is None:
                            if key in runtime_config:
                                runtime_config.pop(key, None)
                                changed.append(key)
                            continue
                        try:
                            _set(key, int(raw_val))
                        except (TypeError, ValueError):
                            logger.warning(
                                f"[CONFIG-SYNC] Ignoring invalid {key} value: {raw_val!r}"
                            )
                    elif key in runtime_config:
                        runtime_config.pop(key, None)
                        changed.append(key)
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
                    "max_concurrent_act_on_verdict_followons",
                    "evergreen_fill_interval_minutes",
                    "evergreen_fill_min_free_slots",
                    "evergreen_fill_max_active",
                    "evergreen_fill_daily_cap",
                ):
                    if int_key in config_json:
                        _set(int_key, int(config_json[int_key]))
                for float_key in (
                    "direction_churn_min_merge_rate",
                    "direction_lifetime_min_merge_rate",
                    "repo_map_holdback_fraction",
                    "module_interfaces_holdback_fraction",
                    "related_tests_holdback_fraction",
                    "affected_consumers_holdback_fraction",
                    "diagnostics_after_edit_holdback_fraction",
                    "evergreen_fill_util_threshold",
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
                if "env_preflight_enabled" in config_json:
                    _set(
                        "env_preflight_enabled",
                        bool(config_json["env_preflight_enabled"]),
                    )
                if "smoketest_enabled" in config_json:
                    _set(
                        "smoketest_enabled",
                        bool(config_json["smoketest_enabled"]),
                    )
                if "no_ci_test_gate_enabled" in config_json:
                    _set(
                        "no_ci_test_gate_enabled",
                        bool(config_json["no_ci_test_gate_enabled"]),
                    )
                if "diagnostics_after_edit_enabled" in config_json:
                    _set(
                        "diagnostics_after_edit_enabled",
                        bool(config_json["diagnostics_after_edit_enabled"]),
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
    return config_get("retry_budgets_by_class").get(
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


def walkthrough_e2e_enabled_by_project_map() -> Dict[str, bool]:
    """Per-project walkthrough_e2e overrides, as ``{project_id: bool}``.

    Read from the durable ``walkthrough_e2e_enabled_by_project`` runtime-config
    value. Normalizes to trimmed, non-empty string project-id keys mapping to
    bools, silently dropping malformed entries. An EMPTY map (the default) means
    every project falls back to the fleet-wide ``walkthrough_e2e_enabled``.
    Parallel to :func:`promotion_enabled_by_project_map`.
    """
    raw = runtime_config.get("walkthrough_e2e_enabled_by_project") or {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, bool] = {}
    for key, value in raw.items():
        sid = str(key).strip()
        if not sid:
            continue
        out[sid] = bool(value)
    return out


def resolve_project_walkthrough_e2e_enabled(project_id) -> bool:
    """A project's EFFECTIVE walkthrough_e2e gate.

    Returns the project's explicit per-project value from
    ``walkthrough_e2e_enabled_by_project`` when one is set, otherwise falls back
    to the fleet-wide ``walkthrough_e2e_enabled`` default (True).  Mirrors
    :func:`resolve_project_promotion_enabled` for the walkthrough evidence lane.
    """
    per_project = walkthrough_e2e_enabled_by_project_map()
    sid = str(project_id)
    if sid in per_project:
        return per_project[sid]
    return bool(runtime_config.get("walkthrough_e2e_enabled"))


def resolve_project_concurrency_cap(project_id) -> int | None:
    """Return the per-project concurrent-VM cap, or None if no cap is set.

    Reads from ``max_concurrent_tasks_by_project`` in runtime_config.  An absent
    or zero entry means no cap — returns None and the dispatch loop skips the
    filter for that project (behavior-preserving for unconfigured projects).
    Mirrors :func:`resolve_project_promotion_enabled` / the warm-pool resolver
    pattern.
    """
    raw = runtime_config.get("max_concurrent_tasks_by_project") or {}
    if not isinstance(raw, dict):
        return None
    sid = str(project_id)
    val = raw.get(sid)
    if val is None:
        return None
    try:
        ival = int(val)
    except (TypeError, ValueError):
        return None
    return ival if ival > 0 else None


def resolve_project_compute_job_concurrency_cap(project_id) -> int | None:
    """Return the per-project concurrent compute-job cap, or None if no cap is set.

    Reads from ``max_concurrent_compute_jobs_by_project`` in runtime_config.  An
    absent or zero entry means no cap — returns None so create_compute_job skips
    the check for unconfigured projects (behavior-preserving). Mirrors
    :func:`resolve_project_concurrency_cap` for the compute-job lane.
    """
    raw = runtime_config.get("max_concurrent_compute_jobs_by_project") or {}
    if not isinstance(raw, dict):
        return None
    sid = str(project_id)
    val = raw.get(sid)
    if val is None:
        return None
    try:
        ival = int(val)
    except (TypeError, ValueError):
        return None
    return ival if ival > 0 else None


def is_improvement_lanes_enabled() -> bool:
    """Return True when the v10 persistent improvement lane substrate is active.

    Default False (master kill-switch). When False, all lane code is INERT and
    no existing code path changes. Mirrors ``warm_pool_enabled`` gating style.
    """
    return bool(config_get("improvement_lanes_enabled"))


def is_review_fix_lane_pilot_enabled() -> bool:
    """Return True when the review-fix lane pilot is active.

    Requires BOTH the substrate master switch (``improvement_lanes_enabled``)
    and the pilot flag (``review_fix_lane_pilot_enabled``). A KILL verdict flips
    the pilot flag to False, degrading every chain to classic spawning. Mirrors
    the canonical gate in ``app.review_fix_lane_pilot`` so callers that only
    need the flag check can avoid importing the pilot module.
    """
    if not is_improvement_lanes_enabled():
        return False
    return bool(config_get("review_fix_lane_pilot_enabled"))


def is_review_fix_lane_shadow_enabled() -> bool:
    """Return True for checkpoint-only review-fix lane shadow mode.

    Shadow is the first enabled state of the substrate: the master switch is on
    but the full pilot flag is off. Callers must still apply project eligibility
    and durable KILL checks before creating shadow lanes.
    """
    return is_improvement_lanes_enabled() and not bool(
        config_get("review_fix_lane_pilot_enabled")
    )


def get_lane_pilot_mode() -> str:
    """Return the three-value pilot mode: 'off', 'shadow', or 'on'.

    Derives the mode from the boolean flags (improvement_lanes_enabled +
    review_fix_lane_pilot_enabled) so existing deployments that only use the
    boolean flags continue to work.  The explicit ``review_fix_lane_pilot_mode``
    key was deleted per the default-off-is-guilty principle.
    """
    # Derive from the boolean flags.
    if is_review_fix_lane_pilot_enabled():
        return "on"
    if is_review_fix_lane_shadow_enabled():
        # Classic armless shadow (master on, pilot off) is "off" for the
        # three-value scale — accessible via the substrate's boolean semantics.
        return "off"
    return "off"


def is_review_fix_lane_pilot_shadow_mode_enabled() -> bool:
    """Return True when the pilot shadow mode is active.

    Shadow mode (mode == 'shadow') runs arm assignment and checkpoint writing
    but skips warm-pool reservations and fix-task lane routing.  Requires the
    substrate master switch (improvement_lanes_enabled) to be True so lane rows
    can actually be created.
    """
    return is_improvement_lanes_enabled() and get_lane_pilot_mode() == "shadow"


def get_auto_qualification_triggers_mode() -> str:
    """Return the event-driven qualification trigger mode.

    Values: "observe" (default, logs decisions without spawning),
    "on" (actively fires run_qualification), "off" (resolver does not run).
    """
    raw = config_get("auto_qualification_triggers")
    if raw in ("on", "observe", "off"):
        return raw
    return "observe"
