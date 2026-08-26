"""Task failure classification for deployment monitoring in Blueprint AI Studio."""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

from app.execution_capability_requirements import (
    classify_execution_capability_failure_frames,
)
from app.time_utils import _utcnow
from app.review_park_disposition import REVIEW_PARK_ABANDONED_FAILURE_DETAIL

# Residual-remediation / fingerprinting / per-account-failure-log seam lives in
# the sibling module; re-exported here so all callers and tests keep importing
# these names from ``app.failure_classification`` with an unchanged interface.
from app.failure_residual import (  # noqa: F401
    _account_failure_log,
    _ERROR_LINE_PATTERNS,
    _extract_error_signature,
    _fingerprint_message,
    _looks_like_error,
    _mark_residual_case_closed,
    _reactivate_residual_case,
    _RE_HEX,
    _RE_MEM_PREFIX,
    _RE_NUM,
    _RE_SIZE_TOKEN,
    _RE_UUID,
    _RESIDUAL_MAX_FINGERPRINTS,
    _RESIDUAL_SEEN_MAX,
    _residual_bg_tasks,
    _residual_closed_fingerprints,
    _residual_seen_task_ids,
    _residual_store,
    _schedule_residual_db_upsert,
    _spawn_bg_task,
    _upsert_residual_case_async,
    _viewport_residual_seen_task_ids,
    _viewport_residual_store,
    get_account_failure_class_counts,
    get_account_failure_class_records,
    get_top_residual_fingerprints,
    get_top_viewport_residual_fingerprints,
    normalize_error_fingerprint,
    record_account_failure_class,
    record_residual_fingerprint,
    record_viewport_regression_fingerprint,
)

if TYPE_CHECKING:
    from app.models import Task

logger = logging.getLogger(__name__)


def _trusted_execution_capability_unavailable(
    task: "Task", logs: str | None = None
) -> bool:
    """Use only Blueprint-owned exact sentinels for capability infra routing."""

    return bool(
        classify_execution_capability_failure_frames(
            task.latest_status_message or "",
            logs or "",
        )
    )


@dataclass(frozen=True)
class FailureClassification:
    failure_class: str
    confidence: float


def _resolve_task_provider_auth(task: "Task"):
    """Resolve (provider, auth_mode) enums for a task, or (None, None) on any failure.

    Shared entry point for both _task_rate_limit_patterns (legacy tuple-of-strings)
    and _task_rate_limit_signal (structured result). Keeps the provider/auth-mode
    lookup logic in one place.
    """
    from app.max_plan_pool import get_task_llm_auth_mode, get_task_llm_provider
    from app.models import LLMAuthMode, LLMProviderName

    provider_raw = get_task_llm_provider(task)
    auth_mode_raw = get_task_llm_auth_mode(task)
    if not provider_raw or not auth_mode_raw:
        return None, None
    try:
        return LLMProviderName(provider_raw), LLMAuthMode(auth_mode_raw)
    except ValueError as exc:
        try:
            ctx = dict(getattr(task, "context", {}) or {})
            ctx["llm_runtime_resolution_failed"] = True
            ctx["llm_runtime_resolution_error"] = "invalid_provider_or_auth_mode"
            ctx["llm_provider_raw"] = provider_raw
            ctx["llm_auth_mode_raw"] = auth_mode_raw
            task.context = ctx
        except Exception:
            logger.exception(
                "[FAILURE_CLASS] Failed to mark invalid provider/auth context for task %s",
                getattr(task, "id", None),
            )
        logger.warning(
            "[FAILURE_CLASS] Invalid task provider/auth mode for task %s: provider=%r auth_mode=%r (%s)",
            getattr(task, "id", None),
            provider_raw,
            auth_mode_raw,
            exc,
        )
        return None, None


def _task_rate_limit_patterns(task: "Task") -> tuple[str, ...]:
    from app.max_plan_pool import RATE_LIMIT_PATTERNS
    from app.providers.registry import get_provider

    provider, auth_mode = _resolve_task_provider_auth(task)
    if provider is None or auth_mode is None:
        return tuple(RATE_LIMIT_PATTERNS)
    try:
        return get_provider(provider).rate_limit_patterns(auth_mode)
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] Failed to resolve rate-limit patterns for provider=%s auth_mode=%s",
            provider,
            auth_mode,
        )
        return tuple(RATE_LIMIT_PATTERNS)


def _task_rate_limit_signal(task: "Task", combined: str):
    """Return a RateLimitSignal for the task+text, or None.

    Uses the task's provider/auth_mode to pick the right classifier. When
    provider/auth_mode are unresolvable (e.g. legacy task without structured
    context), falls back to None — callers treat that as "legacy rate-limit
    match" with no kind/retry info. Never raises.
    """
    from app.providers.registry import get_provider

    provider, auth_mode = _resolve_task_provider_auth(task)
    if provider is None or auth_mode is None:
        return None
    try:
        return get_provider(provider).classify_rate_limit_signal(auth_mode, combined)
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] classify_rate_limit_signal failed for provider=%s auth_mode=%s",
            provider,
            auth_mode,
        )
        return None


# Canonical failure classes that the classifier and rollups use.
# Any mutation here must be reflected in the analytics sets that import INFRA_CLASSES.
CODE_ERROR = "CODE_ERROR"
PROVISION_FAILURE = "PROVISION_FAILURE"
AGENT_TIMEOUT = "AGENT_TIMEOUT"
EXTERNAL_API = "EXTERNAL_API"
RATE_LIMIT = "RATE_LIMIT"
# Structural mismatch between task scope and actual change footprint — the agent
# planned/produced more files than the scope budget allows. Not a code defect
# and not an infra outage; retry logic uses this class to route through scope
# escalation (medium→large decomposition) instead of a same-scope retry.
SCOPE_OVERFLOW = "SCOPE_OVERFLOW"
# Migration-specific code defect: alembic chain breaks, duplicate heads,
# missing migrations causing UndefinedColumn at runtime, EXPECTED_SCHEMA_REVISION
# mismatches, or downgrade NotImplementedError/OperationalError. Distinct from
# CODE_ERROR so auto-remediation can apply migration-specific fixes (rechain
# down_revision, regenerate migration, bump EXPECTED_SCHEMA_REVISION).
MIGRATION_ERROR = "MIGRATION_ERROR"
# Agent surfaced a "BLOCKER:" message and stopped — a precondition (e.g. an
# upstream rollup hasn't landed on main yet, a referenced file/PR doesn't
# exist where the prompt expects it). The agent did the right thing by
# refusing to proceed; this is NOT a code defect. Distinct class so the
# parent-requeue loop in task_lifecycle.py skips the clone-and-retry path
# instead of burning attempts on a task that will hit the same block until
# the precondition resolves. Production case 2026-05-05: tasks 12679e04 +
# 79a4a5ec each consumed a retry slot on a missing-on-main dispatcher file
# that was waiting on an upstream rollup PR (#3768) to land.
BLOCKED_PRECONDITION = "BLOCKED_PRECONDITION"
# Clone-time branch-gone: the branch the task targets no longer exists on the
# remote — almost always because its PR already merged (or closed) and the
# branch was auto-deleted before the agent could clone it. The agent reports a
# generic "fatal" CODE_ERROR from the failed `git clone --branch <gone>`, but
# this is an orchestration race, not a code defect: retrying is futile (the
# branch will not reappear) and the work usually already landed. Distinct,
# VISIBLE class (in ALL_FAILURE_CLASSES + INFRA_CLASSES) so operators can SEE
# how often branch-deletion-suppression falls through, while it stays
# budget-exempt — no retry slot, no CODE_ERROR auto-remediation/debug spawn,
# no direction-health penalty. Production case 2026-06-03: review fbd497dd of
# PR #7547 cloned plan/763859dc 0.25h after the PR merged + branch was deleted;
# burned 3 retries + a debug task before the respawned review skip-detected the
# merge. The respawn already skips merged PRs gracefully (review_pipeline); this
# only stops the FIRST attempt from being mislabeled a code defect.
BRANCH_GONE = "BRANCH_GONE"
# The repository has no commits at all, so its default branch does not exist and
# nothing can be cloned from it. Git reports this with the SAME text as a deleted
# branch ("Remote branch <trunk> not found in upstream origin"), so without this
# class it lands in BRANCH_GONE and is shown to operators as "Branch deleted (PR
# merged before clone)" — a benign, transient-sounding lie about a permanent,
# structural condition. Distinct class so the reason is the truth: the repo was
# never seeded. Blueprint seeds empty repos at onboarding
# (`ensure_repo_initialized`); seeing this class means that seed did not happen
# or was undone, which is an operator fix, not something a retry can resolve.
REPO_UNINITIALIZED = "REPO_UNINITIALIZED"
# Agent completed the task but the PR branch was already deleted (merged and
# auto-deleted), so there was nothing to review or fix. Similar to SCOPE_EMPTY
# (task had no work to do), routed to that class so it doesn't consume retry
# budget or penalize direction health.
PR_BRANCH_DELETED = "PR_BRANCH_DELETED"
# Cluster sibling pre-emption — the agent's declared related_files were already
# covered by a sibling cluster's merged PR before this task ran. The agent did
# the right thing by detecting the overlap and exiting cleanly; this is NOT a
# code defect and must NOT consume the retry budget or penalize direction health.
# Agent emits a "SCOPE_EMPTY: <reason>" status message (or calls ./blocked with
# that prefix) and exits; the classifier routes to this class exactly as it does
# for BLOCKED_PRECONDITION / "BLOCKER:" prefix detection.
SCOPE_EMPTY = "SCOPE_EMPTY"
# Agent self-terminated because the task is no longer assigned to its VM — a
# non-transient heartbeat 4xx (401/403/404) signalling reassignment/deauth, or
# the task was cancelled/superseded during startup. The agent did the right
# thing by handing off gracefully (it posts this category synchronously from the
# self-terminate path so the task isn't left IN_PROGRESS until the reaper times
# it out → false AGENT_TIMEOUT). This is NOT a code defect: it must NOT consume
# the retry budget or penalize direction health, so it lives in INFRA_CLASSES.
REASSIGNED = "REASSIGNED"
# The agent's llm-no-progress watchdog fired on a GENUINELY silent LLM call (no
# stream-json progress signal for >LLM_NO_PROGRESS_STALL_MS) but NO confirmed
# rate-limit message was present (no RATE_LIMIT_PATTERNS match). This is a
# transient provider hang of UNKNOWN cause -- NOT a confirmed account rate-limit.
# Distinct from RATE_LIMIT so the requeue path leaves the account AVAILABLE (does
# NOT block its model / apply the canonical cooldown): the pipeline runs many
# Max-20x accounts that are not actually rate-limited, so blocking a healthy
# account on an inferred hang is a self-inflicted cascade that mimics rate-limiting
# (incident 2026-05-31). Like RATE_LIMIT it requeues without burning the retry
# budget; unlike RATE_LIMIT it never touches account state. A CONFIRMED rate-limit
# message still routes to RATE_LIMIT (block + cooldown) -- that path is unchanged.
# Lowercase to match the literal failure_category string the agent posts.
NO_PROGRESS_TIMEOUT = "no_progress_timeout"
# Compute backends persist this exact terminal reason when their watchdog cuts a
# command.  Keeping it adjacent to the canonical failure class prevents backend
# emitters and the completion taxonomy from drifting apart.
NO_PROGRESS_TIMEOUT_TERMINAL_REASON = NO_PROGRESS_TIMEOUT

# ── Shell-level environment exit codes ─────────────────────────────────────────
# POSIX reserves two exit statuses for the SHELL's own verdict on whether the
# command could be run at all. A command body can never choose them: a Python
# traceback exits 1, pytest exits 1-5, a SIGKILL surfaces as 137. So observing
# one of these is direct evidence that the execution ENVIRONMENT did not have
# what the command needed -- no interpreter on PATH, a binary that is not
# executable, a noexec mount, a wrong-arch shim. That is a provisioning fault,
# not a defect in the experiment's algorithm.
#
# Why this matters: every compute backend used to launder its failure into the
# prose string "compute job failed" / "local subprocess exited with code N",
# which classify_compute_terminal_reason substring-matches against nothing and
# therefore returned CODE_ERROR -- the class reserved for a genuine work defect.
# Exit 127 ("no working pip found", "python3: command not found") is the single
# commonest bootstrap death on self-hosted runners, and every one of them burned
# the owning task's retry budget and blamed the experiment's author, producing
# rounds of hand-patching retries instead of one platform fix.
ENVIRONMENT_EXIT_CODES: dict[int, str] = {
    126: "command found but not executable",
    127: "command not found",
}
# Exact sentinel the compute backends prefix onto a terminal reason when they
# observe an environment exit code. classify_compute_terminal_reason keys on
# this constant BEFORE its generic substring rules, so emitter and classifier
# share one source of truth and cannot silently drift the way the 125 /
# no_progress_timeout pair nearly did. The word "provisioning" is also present so
# the generic rule below (and any not-yet-deployed reader) still routes it
# correctly during a rolling deploy.
ENVIRONMENT_FAILURE_TERMINAL_REASON = "environment provisioning failed"


def is_environment_exit_code(exit_code: object) -> bool:
    """True when ``exit_code`` is a shell-reserved environment-failure status.

    ``bool`` is an ``int`` subclass, so a stray ``True`` must not read as 1.
    """
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        return False
    return exit_code in ENVIRONMENT_EXIT_CODES


def environment_failure_terminal_reason(
    exit_code: int, existing_reason: str | None = None
) -> str:
    """Build the terminal reason a backend records for an environment exit code.

    The substrate's own reason (when it carries anything beyond the generic
    default) is appended after the sentinel so the reclassification discards no
    diagnostic evidence.
    """
    detail = ENVIRONMENT_EXIT_CODES.get(exit_code, "environment unusable")
    base = f"{ENVIRONMENT_FAILURE_TERMINAL_REASON}: {detail} (exit {exit_code})"
    trimmed = (existing_reason or "").strip()
    if trimmed and trimmed.lower() not in base.lower():
        return f"{base}; {trimmed}"
    return base


# ── VM self-destruct clock terminal reasons ────────────────────────────────────
# The GCP startup script arms a TWO-PHASE self-destruct clock. Phase 1 bounds
# provisioning (boot, repo clone, image pull, checkpoint hydration, setup step);
# phase 2, armed at the same point as the no-progress watchdog, bounds the
# experiment's own runtime. Each phase records a DIFFERENT terminal reason so the
# taxonomy can tell "we never got the experiment started" apart from "the
# experiment ran and exceeded its declared deadline".
#
# Named here, next to the classes they map to, for the same reason as the 125 /
# no_progress_timeout pair above: the emitter (compute_backends) and the consumer
# (classify_compute_terminal_reason) must not drift. They HAD drifted, on the
# VM-side token specifically: the classifier's prose rule "runtime deadline"
# covers the SERVER-side deadline cuts (compute_jobs.py emits the literal
# "compute job exceeded runtime deadline" from three call sites: the poll-status
# timeout branch, the terminate-and-record path, and the poll-failure deadline
# check), but the GCP startup script's phase-2 token ``runtime_ttl`` shares no
# substring with that prose and fell through every rule to CODE_ERROR. An
# intentional platform kill was therefore billed to the experiment's author as a
# work defect.
#
# Do NOT "clean up" the prose rule as dead: deleting it would re-bill every
# server-side runtime-deadline kill to the author. Both spellings are live and
# both are load-bearing.
PROVISIONING_TTL_TERMINAL_REASON = "provisioning_ttl"
RUNTIME_TTL_TERMINAL_REASON = "runtime_ttl"

# Emitted by EVERY substrate (local wrapper, GCP startup script, preflight smoke)
# when the declared container_image cannot be resolved to something runnable.
#
# This token matched NO classifier rule, so it fell through to the default and
# was billed as CODE_ERROR — the class reserved for a genuine defect in the
# experiment's own command. The environment was never provisioned; the command
# never ran at all. The cost of that misattribution is not just a wrong label: a
# CODE_ERROR burns the owning task's retry budget, penalizes direction health,
# and spawns a fix task against a command that was never the problem — so the
# fix task fails identically, on the same unresolvable image.
#
# It is pinned as an exact constant, ahead of the default, for the same reason
# the sentinels above are: a rename must break loudly rather than silently drop
# the mapping back to CODE_ERROR.
CONTAINER_IMAGE_UNRESOLVED_TERMINAL_REASON = "container_image_pull_failed"


# Agent process is alive (fresh heartbeat) but has made no tool or LLM progress
# for longer than the scope-aware threshold (8 min small / 12 min medium /
# 18 min large). Detected by the reaper's progress-staleness sweep, which
# checks last_tool_use_at against _progress_stale_cutoff() while verifying
# last_heartbeat is fresh. Distinct from AGENT_TIMEOUT (process dead / reaped
# for heartbeat absence) so the dispatcher can track wedged-agent regressions
# separately from crash/OOM regressions. Lowercase to match the literal string
# the reaper writes into failure_category context.
AGENT_STALLED = "agent_stalled"
# Viewport rendering regression — npm run test:viewport reported overflow,
# text-truncation, or touch-target violations (expectNoOverflowAt /
# expectNoTextTruncationAt / expectMinTouchTargetAt).
# Distinct from CODE_ERROR so the regression dispatcher and operator panels can
# track mobile-rendering regressions separately from unit-test or build breaks.
VIEWPORT_REGRESSION = "VIEWPORT_REGRESSION"
# Viewport coverage erosion — a recently merged PR dropped assertion count
# (deleted/skipped tests) or added data-viewport-allow-* escape-hatch
# attributes without a legitimate component removal. Used as the regression_class
# key in RegressionRemediationCase rows created by the coverage-drop signal.
VIEWPORT_COVERAGE_DROP = "viewport_coverage_drop"

# Park/wake orchestration outcomes for the task park-and-wake primitive. A PARKED
# task waits on an external, non-agent wake source (e.g. a ComputeJob reaching a
# terminal state) via a durable ``TaskWakeRegistration``. Two ways that wait can
# end terminally WITHOUT the agent's code being at fault:
#   * PARK_WAKE_TIMEOUT  — the source never fired before the registration's
#     ``deadline_at``; the reaper aged the task out (``task_lifecycle.expire_wake``).
#   * PARK_WAKE_STRANDED — the task sat PARKED past the strand threshold with no
#     live (PENDING/FIRED) registration left to wake it — its registration row was
#     lost — so the wake-delivery sweep failed it rather than strand it forever
#     (``reaper_sweeps.sweep_deliver_task_wakes`` pass 3).
# Both are orchestration faults, not code defects, so they are budget-exempt
# (INFRA_CLASSES — no retry burn, no direction-health penalty) and route to the
# infra taxonomy bucket. Registering them as canonical recognized classes (with
# human labels + ALL_FAILURE_CLASSES membership) stops failure distributions from
# presenting them as generic code-like residual fingerprints. The string VALUES
# match the literals already written into ``task.context['failure_category']`` /
# ``['failure_class']`` by the expire/strand paths, so the persisted-verdict
# short-circuit in ``_classify_failure_class`` returns them unchanged.
PARK_WAKE_TIMEOUT = "park_wake_timeout"
PARK_WAKE_STRANDED = "park_wake_stranded"

# Account's OAuth session was permanently terminated (user logged out, admin
# revoked OAuth, session TTL expired) — unrecoverable without operator re-auth.
# Distinct from RATE_LIMIT (transient throttle) and CODE_ERROR (work defect):
# the provider auth layer is dead and retrying hits the same 401 every time.
# On detection: fleet-wide hard-disable the account (operator must re-auth to
# re-enable) and requeue the task once onto a different account. Budget-exempt
# (INFRA_CLASSES) so failed attempts don't consume the task's retry budget.
DEAD_AUTH = "DEAD_AUTH"

# Review-fix cap exhausted after the PR had already been approved. The work was
# signed off, but merge mechanics (CI/rebase churn) burned the loop cap and the
# PR was auto-closed. This must be visible in analytics/digests without being
# treated as CODE_ERROR, PR_REJECTED, or direction-health damage.
APPROVED_CAP_PR_CLOSED = "APPROVED_CAP_PR_CLOSED"

# A rollup PR merged (or was about to merge) while a constituent child PR's
# ENTIRE verified deliverable had been erased from the rollup branch by a
# conflict-fix intervention. This is a PLATFORM defect, not a work defect: the
# child did the work, its PR was reviewed and merged into the staging branch,
# and the pipeline's own conflict resolution discarded it (the rollup
# conflict-fix prompt tells the agent the target branch is the source of truth
# and to keep only "changes that serve the rollup goal" — a 500-char blurb that
# names none of the children's files). Charging this to CODE_ERROR burns the
# retry budget, spawns a pointless diagnosis task, and penalises the direction's
# health for damage the engine inflicted on itself. Production case 2026-08-24:
# rollup PR #17804 dropped 12/12 files of child PR #17707 and research task
# 78ad7812 was FAILED as CODE_ERROR.
#
# Deliberately in INFRA_ANALYTICS_CLASSES (analytics/erosion bucket) but NOT in
# INFRA_CLASSES — same reasoning as UNPLACEABLE_EXECUTION_SITE: the
# reaper_infra_recovery Step 5 resurrection sweep flips INFRA_CLASSES members
# back to QUEUED, and a blind requeue of a rollup parent whose PR already merged
# would re-drive rollup creation without the dropped work being restored. The
# targeted recovery is the pre-merge hold + restore fix in
# ``app.rollup_deliverable_guard``, not a generic resurrection.
ROLLUP_DELIVERABLE_DROPPED = "ROLLUP_DELIVERABLE_DROPPED"

# A compute_experiment declared a kind=metric success_criterion but produced NO
# numeric value for that metric (the experiment ran, yet the hypothesis was never
# measured). Written by ``task_completion._handle_compute_experiment_completion``
# as a DISTINCT, visible failure_category so 'the job ran' can never masquerade as
# 'the hypothesis was measured' under a clean outcome=SUCCESS. This is a genuine
# work/measurement defect — the command must be fixed to emit the declared metric
# — so it routes to CODE_ERROR (burns the retry budget, drives the diagnosis
# pipeline), NOT an infra class. Distinct from kind=artifact_produced criteria
# (which gate on durable_file_count) and from intentional budget/deadline kills
# (which stay clean terminal outcomes).
COMPUTE_UNMEASURED_CRITERION = "compute_unmeasured_criterion"

# Command died on the local DB-less pre-flight smoke before any full VM was
# provisioned. A genuine work defect (the command must be fixed to run on the
# substrate), so it routes to CODE_ERROR and burns the retry budget.
COMPUTE_PREFLIGHT_SMOKE_FAILED = "compute_preflight_smoke_failed"

# The compute job reached a terminal outcome that is neither SUCCESS nor an
# intentional budget/deadline/operator kill -- i.e. the substrate's own verdict
# on the run is FAILURE. Before this class existed the completion gate asked
# only whether SOME terminal outcome and SOME cost were present, so a job the
# platform itself had judged failed still recorded the task COMPLETED and the
# research loop read it as a finished experiment. Routes to CODE_ERROR: the
# command failed and must be fixed, so it should burn the retry budget and
# drive diagnosis rather than be treated as a transient infra blip.
COMPUTE_JOB_FAILED = "compute_job_failed"

# A QUEUED task whose registry-declared execution site no dispatch path can
# place (e.g. the qualifier's live-local-runner ``walkthrough_e2e`` site once
# the task has been requeued and its site-pinned runner is gone). This is a
# PERMANENT routing-contract violation — the declared site and the runner
# inventory disagree — NOT a transient provisioning blip: retrying or
# resurrecting the task re-queues it into the same dead end every cycle. It is
# a terminal class deliberately OUTSIDE INFRA_CLASSES so the resurrection sweep
# (which flips only INFRA_CLASSES members back to QUEUED) never re-queues it,
# and OUTSIDE CODE_ERROR's retry-budget semantics because the work never ran.
# The failure_reason must name the declared execution site so the routing
# mismatch is diagnosable without reading source.
UNPLACEABLE_EXECUTION_SITE = "UNPLACEABLE_EXECUTION_SITE"

# The task's OWN payload cannot satisfy the contract its declared task type
# requires — a required field is absent or malformed.  Canonical case: a ``fix``
# payload with no ``branch``, which the agent's ``handleFix``
# (agent/src/handlers/review-fix.ts) rejects on its first line of real work with
# ``Fatal error: branch is required`` before any model call happens.
#
# This is a PLATFORM defect in whatever SPAWNED the task, not a defect in the
# agent's work.  Charging it to CODE_ERROR misattributes a spawner bug to the
# agent and feeds that misattribution into failure-attribution and the
# research/erosion scoring.  It is also DETERMINISTIC in the persisted row:
# every retry re-reads the same malformed payload and dies identically, so a
# retry can never succeed — the class is in ``NON_RETRYABLE_PLATFORM_CLASSES``
# and terminal on FIRST detection.
#
# Deliberately OUTSIDE INFRA_CLASSES (the reaper's Step 5 resurrection sweep
# flips only INFRA_CLASSES members back to QUEUED, and re-queuing walks straight
# back into the same dead end) but INSIDE INFRA_ANALYTICS_CLASSES and
# ALL_FAILURE_CLASSES, so the platform kill stays VISIBLE to operators and is
# bucketed as a platform condition rather than an agent code defect — the same
# shape as UNPLACEABLE_EXECUTION_SITE above.
#
# Production 2026-08-23 (Blueprint project 37d445e1): four
# ``guard_suppression_remediation`` tasks (4fdd6113 / 9bfa5831 / 50531efe /
# b4121926) each carried a branchless ``fix`` payload, provisioned four VMs
# apiece, and all four died ``[CODE_ERROR] Retries exhausted (3/3)``.
MALFORMED_TASK_PAYLOAD = "MALFORMED_TASK_PAYLOAD"

# Failure classes that a retry can never clear because the cause is a permanent
# property of the task ROW, not of the attempt.  The agent-status retry gate
# refuses to re-queue these: the task lands terminal on first detection with a
# reason instead of burning the whole budget on identical deaths.
NON_RETRYABLE_PLATFORM_CLASSES: frozenset[str] = frozenset(
    {
        MALFORMED_TASK_PAYLOAD,
    }
)


# The RUNNER-ENTRY contract, mirrored field-for-field from the agent handlers.
# These are the fields a handler dereferences and hard-throws on BEFORE any
# model call, clone, or useful work — i.e. the payload defects that make a task
# unrunnable by construction rather than merely under-specified.
#
# This is deliberately NARROWER than the authoring schema in
# app.payload_schemas.validate_task_payload. The authoring schema also enforces
# planning-quality fields (acceptance_evidence, prompt length caps, research
# envelopes) whose absence produces a WORSE task, not an IMPOSSIBLE one, and
# many legitimately-created historical rows omit them. Terminalizing on the
# authoring schema would kill runnable work; terminalizing on the runner-entry
# contract kills only tasks that were already guaranteed to die.
#
# Keep in lockstep with the agent. Each entry cites its throw site:
#   fix     branch      agent/src/handlers/review-fix.ts:1350 validateBranchName
#           pr_number   agent/src/handlers/review-fix.ts:1354 Invalid pr_number
#           prompt      agent/src/handlers/review-fix.ts:1359 missing prompt
#   review  pr_number   agent/src/handlers/review-fix.ts:670  Invalid pr_number
#           branch      agent/src/handlers/review-fix.ts:671  validateBranchName
# (validateBranchName itself: agent/src/index.ts:2892.)
_REQ_BRANCH = "branch_name"
_REQ_POSITIVE_INT = "positive_int"
_REQ_NON_EMPTY_STR = "non_empty_str"

_RUNNER_ENTRY_CONTRACT: dict[str, tuple[tuple[str, str], ...]] = {
    "fix": (
        ("branch", _REQ_BRANCH),
        ("pr_number", _REQ_POSITIVE_INT),
        ("prompt", _REQ_NON_EMPTY_STR),
    ),
    "review": (
        ("pr_number", _REQ_POSITIVE_INT),
        ("branch", _REQ_BRANCH),
    ),
}

# Same character class the agent's validateBranchName enforces
# (agent/src/index.ts:2893).
_AGENT_BRANCH_RE = re.compile(r"^[a-zA-Z0-9_./-]+$")


def _runner_entry_field_defect(value: object, kind: str) -> str | None:
    """Reason this value fails the agent's entry check, or None if it passes."""
    if kind == _REQ_BRANCH:
        if not isinstance(value, str) or not value:
            return "is required"
        if not _AGENT_BRANCH_RE.match(value):
            return "has characters the agent rejects"
        if ".." in value or value.startswith("-"):
            return "is not a valid branch name"
        return None
    if kind == _REQ_POSITIVE_INT:
        # The agent does Number(payload.pr_number) then Number.isInteger(...) &&
        # > 0, so a numeric string passes there too.
        try:
            if isinstance(value, bool) or value is None or value == "":
                return "is required"
            as_int = int(str(value).strip())
        except (TypeError, ValueError):
            return "is not a positive integer"
        return None if as_int > 0 else "is not a positive integer"
    if kind == _REQ_NON_EMPTY_STR:
        if not isinstance(value, str) or not value.strip():
            return "is required"
        return None
    return None


def malformed_task_payload_reason(task: "Task") -> str | None:
    """One-line reason the payload can never clear the agent's entry checks.

    Returns None when the payload is runnable, and also when the payload does
    not DECLARE a task type this contract covers — an unknown or absent type is
    a different question and must not be terminalized by this class.

    Never raises: a classifier that can crash is worse than one that declines to
    reclassify.
    """
    try:
        payload = getattr(task, "first_payload", None)
        if not isinstance(payload, dict):
            return None
        declared_type = payload.get("type")
        if not isinstance(declared_type, str):
            return None
        contract = _RUNNER_ENTRY_CONTRACT.get(declared_type.strip().lower())
        if not contract:
            return None
        defects = [
            f"{field} {defect}"
            for field, kind in contract
            if (defect := _runner_entry_field_defect(payload.get(field), kind))
        ]
        if not defects:
            return None
        return (
            f"{declared_type} payload cannot satisfy the agent's entry contract: "
            + "; ".join(defects)
        )
    except Exception as exc:  # classification must never crash
        logger.warning(
            "[FAILURE_CLASS] malformed_task_payload_reason failed on task %s: %s",
            getattr(task, "id", "?"),
            exc,
        )
        return None


def classify_compute_terminal_reason(
    terminal_reason: "str | None",
    vm_phase: "str | None",
) -> str:
    """Map a ComputeJob terminal_reason + vm_phase to a canonical failure class.

    Pure function — no I/O, no Task object required. Used by
    task_completion._handle_compute_experiment_completion to classify pre-metric
    failures so they don't all collapse to CODE_ERROR.

    Mapping rules (first match wins):
    - vm_phase == "provisioning" → PROVISION_FAILURE (VM never started)
    - reason contains ``ENVIRONMENT_FAILURE_TERMINAL_REASON`` → PROVISION_FAILURE
      (the shell reported exit 126/127: the execution environment did not have
      what the command needed. Pinned on the exact constant, ahead of the generic
      substring rule, so a rename of the sentinel cannot silently drop the
      mapping back to CODE_ERROR)
    - reason contains ``CONTAINER_IMAGE_UNRESOLVED_TERMINAL_REASON`` →
      PROVISION_FAILURE (the declared container_image resolved to nothing
      runnable on any substrate, so the command never ran — an environment
      fault, not a defect in the command)
    - reason contains "provisioning" → PROVISION_FAILURE (launch/RPC failure,
      the ``provisioning_ttl`` phase-1 self-destruct expiry, and the runner's own
      "cannot establish a Python interpreter" bootstrap reasons, which all carry
      the same word)
    - reason contains ``RUNTIME_TTL_TERMINAL_REASON``, "runtime deadline" or
      "spend cap" → AGENT_TIMEOUT (intentional platform kills; these should
      normally PASS the gate, but if they somehow reach a failure branch the
      class must be AGENT_TIMEOUT, not CODE_ERROR)
    - reason contains "cancelled" or "operator" → ADMIN_CANCEL
    - reason contains ``NO_PROGRESS_TIMEOUT_TERMINAL_REASON`` → NO_PROGRESS_TIMEOUT
      (job was cut early by the no-progress watchdog — a configurable platform
      kill, not a code defect)
    - default → CODE_ERROR (genuine command/work defect)
    """
    if vm_phase == "provisioning":
        return PROVISION_FAILURE
    if terminal_reason:
        lower = terminal_reason.lower()
        if ENVIRONMENT_FAILURE_TERMINAL_REASON in lower:
            return PROVISION_FAILURE
        if PROVISIONING_TTL_TERMINAL_REASON in lower:
            return PROVISION_FAILURE
        if CONTAINER_IMAGE_UNRESOLVED_TERMINAL_REASON in lower:
            return PROVISION_FAILURE
        if "provisioning" in lower:
            return PROVISION_FAILURE
        if (
            RUNTIME_TTL_TERMINAL_REASON in lower
            or "runtime deadline" in lower
            or "spend cap" in lower
        ):
            return AGENT_TIMEOUT
        if "cancelled" in lower or "operator" in lower:
            return ADMIN_CANCEL
        if NO_PROGRESS_TIMEOUT_TERMINAL_REASON in lower:
            return NO_PROGRESS_TIMEOUT
    return CODE_ERROR

# Administrative cancellation — orchestrator or operator terminated the task before
# it ran meaningful work: cascade cancels ("Cancelled: dependency task failed"),
# parent-dependency cancels ("Cancelled: parent evaluation timeout"), or system
# supersession ("Superseded by fix on PR #N"). These are never-started
# administrative operations, not genuine LLM hangs or code defects. Distinct from
# AGENT_TIMEOUT so agent_timeout_rate stays a clean signal for real LLM/VM deaths.
# Budget-exempt (INFRA_CLASSES) — no retry burn, no direction-health penalty.
ADMIN_CANCEL = "ADMIN_CANCEL"

# Context key stamped on a co-deferred dependent task's context. When a research
# cycle decomposes findings, a dependent finding whose prerequisite was deferred
# in the SAME cycle is "co-deferred": its spawned task is set to CANCELLED and the
# finding is re-shelved so the dependent/prerequisite pair re-surfaces together in
# a later cycle. This is a deliberate, RECOVERABLE shelving — the work is coming
# back — NOT a genuine death. It must NOT be overloaded onto SCOPE_EMPTY (a
# completed no-op): the distinct key lets the death-semantics policy
# (:func:`is_non_death_cancellation`) recognize it without conflating the two.
CO_DEFERRED_SHELVED = "_co_deferred_shelved"


# Task types that are deployment-health checks, not candidate-code-authoring.
# When these tasks fail with a generic code-error label (claude_code_failed),
# the cause is typically an infrastructure condition (stale engine, deployment
# deadlock) rather than a code defect in the candidate — route to an INFRA
# class so the CODE_ERROR count in pipeline health dashboards is not inflated.
_DEPLOYMENT_CHECK_TASK_TYPES: frozenset[str] = frozenset({"smoketest"})


def _is_deployment_check_task(task: "Task") -> bool:
    """True when the task is a post-deployment health check (smoketest)."""
    payload = getattr(task, "first_payload", None)
    if not isinstance(payload, dict):
        return False
    return payload.get("type") in _DEPLOYMENT_CHECK_TASK_TYPES


def is_non_death_cancellation(task: "Task") -> bool:
    """True if ``task`` is a terminal-but-NOT-genuinely-dead cancellation.

    Single source of truth for the "this terminal task is not a dead foundation"
    policy, consulted by both the resume system's dead-foundation detector
    (:func:`app.blocked_need_resolution.need_matched_dead_foundation`) and the
    dispatch-time cascade-cancel classifier
    (:func:`app.orchestrator_health._check_candidate_task_deps`). Recognizes four
    non-death markers, all of which mean the work either continues elsewhere, is
    coming back, or was deliberately stopped:

    - ``_replaced_by_task_id`` — a requeue-replacement clone; work continues under
      the replacement.
    - ``failure_category == SCOPE_EMPTY`` — a deliberate "scope already covered"
      no-op exit; the branch is intact.
    - ``failure_category == ADMIN_CANCEL`` — an operator/admin deliberately ended
      the task; this is not an agent timeout or code death.
    - ``_co_deferred_shelved`` — a co-deferred dependent re-shelved alongside its
      same-cycle-deferred prerequisite; recoverable shelving, not a failure.
    """
    ctx = getattr(task, "context", None) or {}
    if ctx.get("_replaced_by_task_id"):
        return True
    if ctx.get("failure_category") in (ADMIN_CANCEL, SCOPE_EMPTY):
        return True
    if ctx.get(CO_DEFERRED_SHELVED):
        return True
    return False


# Substrings emitted by expectNoOverflowAt() / expectNoTextTruncationAt() /
# expectMinTouchTargetAt() in admin-ui/src/test/viewport.ts. Kept narrow so
# only true viewport-suite failures get this class rather than any test
# mentioning "viewport".
_VIEWPORT_PATTERNS: tuple[str, ...] = (
    "expectnooverflowat(",
    "expectnotexttruncationat(",
    "expectmintouchtargetat(",
)

# Sentinel emitted by check-viewport.sh when the viewport gate cannot bootstrap
# (Chromium missing, Docker fallback unavailable). Must be distinct from
# _VIEWPORT_PATTERNS so infra shortfalls route to PROVISION_FAILURE rather
# than VIEWPORT_REGRESSION or CODE_ERROR.
#
# Also includes raw Chromium/GTK host-library bootstrap-failure signatures that
# surface when the agent exhausts in-task retries before the clean sentinel is
# written. Cross-language contract with:
#   check-viewport.sh  → BOOTSTRAP_PATTERNS regex
#   agent/src/index.ts → VIEWPORT_BOOTSTRAP_PATTERNS array
# Keep all three in sync so the layers agree on what constitutes a bootstrap failure.
_VIEWPORT_INFRA_UNAVAILABLE_PATTERNS: tuple[str, ...] = (
    "viewport_gate_infra_unavailable",
    # GTK / Chromium shared-library names — only appear when the host VM is
    # missing required Linux display/audio libraries for Playwright Chromium.
    "libatk",
    "libnss3",
    "libgbm",
    "libxkbcommon",
    "libxcomposite",
    "libasound",
    # Generic library-load error (co-occurs with lib names above in practice).
    "cannot open shared object file",
    # Playwright-specific error messages.
    "host system is missing dependencies",
    "executable doesn't exist at",
    "missing chromium",
    "missing browser",
    # Recurring production failure signature.
    "playwright chromium install requires sudo",
    "admin-ui viewport gate is blocked",
)


# Agent heartbeat substrings (lowercased) emitted by agent/src/index.ts when the
# agent is in a live-work state — mid-PR-push (sendHeartbeatSync) or mid-tool-call
# (statusRef.value).  Tasks reaped in these states (OOM, SIGKILL, VM kill) leave
# the stale heartbeat as latest_status_message; they are infrastructure deaths, not
# code defects and must NOT fall through to CODE_ERROR.
# Cross-language contract: must stay in sync with agent/src/index.ts emissions.
# Verified by TestCrossLanguageClassificationContracts in test_failure_classification.py.
_AGENT_LIVE_WORK_PATTERNS: tuple[str, ...] = (
    "creating pull request for blueprint/",  # sendHeartbeatSync() during PR push
    "using tool: ",  # statusRef.value during tool calls
)


# Orchestrator-emitted defer markers. When the dispatcher cannot satisfy a
# precondition yet (a file is being edited by another in-flight task, or the
# task overlaps a pending rollup), it leaves the task QUEUED and stamps one of
# these "Waiting: ..." status messages (see orchestrator_filters.py
# filter_file_conflicts / filter_rollup_conflicts). These are precondition
# blockers surfaced by the orchestrator itself — semantically identical to an
# agent-emitted "BLOCKER:" — so they route to BLOCKED_PRECONDITION (a
# budget-exempt INFRA class) rather than burning a retry slot on CODE_ERROR.
# Kept narrow on purpose: "Waiting: serializing migration" (line 495) is a
# deliberate ordering wait that stays AGENT_TIMEOUT, and a bare "file conflict"
# mention without the "Waiting:" defer prefix must not over-match.
_ORCHESTRATOR_DEFER_MARKERS: tuple[str, ...] = (
    "waiting: file conflict",
    "waiting: overlaps pending rollup",
)


# Substrings found in GitHub Actions CI job logs that indicate a transient
# infrastructure failure (not a code defect): npm registry unreachable, Docker
# Hub rate-limit / network error, Playwright browser installation failure, or
# generic ECONNREFUSED / ETIMEDOUT network errors.  Matched case-insensitively
# against the raw log text by _is_ci_infra_flake_log().  Kept specific enough
# to avoid false-positives on legitimate test output (e.g. tests that intentionally
# test connection-refused behaviour).
_CI_INFRA_LOG_PATTERNS: tuple[str, ...] = (
    # npm registry / network errors
    "npm error code econnrefused",
    "npm error code etimedout",
    "npm error code enotfound",
    "npm err! network",
    "npm error network",
    "request to https://registry.npmjs.org",
    # Docker Hub rate-limit / network failures
    "error pulling image",
    "toomanyrequests: you have reached",
    "docker pull",
    # Playwright browser installation failure
    "playwright install",
    "failed to install playwright",
    # Generic setup-phase connectivity errors
    "error: connect econnrefused",
    "error: connect etimedout",
    "registry unreachable",
    "failed to fetch",
    "getaddrinfo enotfound",
    # Postgres service-container startup failures (the CI "Start PostgreSQL"
    # step).  The promotion gate runs 8 backend shards, each pulling postgres:15
    # from Docker Hub; a registry blip / rate-limit outlasts the pull retries and
    # surfaces as these harness-emitted lines — the pytest step then never runs,
    # so it is NOT fixable by a code change (the #12492 promotion-gate incident,
    # where two shards died here and blocked the whole promotion).  postgres:15 is
    # a fixed public tag (a pull failure is essentially always transient) and
    # readiness is a service-container health issue, not app code — negligible
    # false-positive risk against legitimate connection-refused tests.
    #
    # Deliberately NOT matched (review, 2026-07-02): "pull failed (attempt" is a
    # PER-ATTEMPT breadcrumb the retry loop emits even when the pull RECOVERS on
    # attempt 2/3, and it co-mingles with real pytest output in the same shard log
    # — it could mask a genuine regression.  "npm ci failed after" fires on a
    # DETERMINISTIC package.json/lockfile mismatch as readily as on a registry
    # outage (the retry wrapper can't tell them apart), so classifying it as infra
    # would convert a self-healing lockfile fix into a doomed re-qualify loop.
    "could not pull postgres",
    "postgresql did not become ready",
)


def _is_ci_infra_flake_log(logs: str) -> bool:
    """Return True if the CI job logs indicate a transient infrastructure failure.

    Matches known patterns for npm-registry / Docker-Hub / Playwright network
    failures that cannot be fixed by a code change.  Used by the promotion-gate
    harvest to skip spawning a doomed fix task and instead re-run the workflow.
    """
    if not logs:
        return False
    logs_lower = logs.lower()
    return any(pat in logs_lower for pat in _CI_INFRA_LOG_PATTERNS)


# Substrings that uniquely identify migration-related failures. Kept narrow so
# generic database connectivity errors stay in EXTERNAL_API.
_MIGRATION_PATTERNS: tuple[str, ...] = (
    "can't locate revision",
    "cant locate revision",
    "revisionerror",
    "multiple head revisions",
    "only a single head supported",
    "multiple heads are present",
    "undefinedcolumn",
    "undefinedcolumnerror",
    "schema version mismatch",
    "expected_schema_revision",
    "alembic.util.exc.commanderror",
)


# Provider model/config rejections are upstream runtime incompatibilities, not
# repository code defects. Keep this list narrow so ordinary test failures that
# mention "model" still fall through normally.
_PROVIDER_MODEL_CONFIG_PATTERNS: tuple[str, ...] = (
    "modelnotfounderror",
    "model not found",
    "is not found for api version",
    "is not supported for generatecontent",
    "unsupported service_tier",
    "unsupported value: 'service_tier'",
    # OpenAI: model access errors ("the model ... does not exist or you do not have
    # access to it"; "the selected model may not exist or you may not have access")
    "does not exist or you do not have access",
    "may not exist or you may not have access",
    # Codex ChatGPT OAuth rejects provider-foreign model ids with this exact
    # 400. This is a dispatch/configuration defect, never account throttling.
    "is not supported when using codex with a chatgpt account",
    "unsupported model",
)


def _provider_model_config_rejection(combined: str) -> bool:
    if any(kw in combined for kw in _PROVIDER_MODEL_CONFIG_PATTERNS):
        return True
    # Keep the generic form anchored on an actual model rejection. Do not
    # match ordinary language such as "unsupported operand type".
    return bool(
        re.search(
            r"(?:the\s+)?['\"]?[a-z0-9_.:-]+['\"]?\s+model\s+is\s+not\s+supported",
            combined,
        )
    )


# Deterministic provider-CLI installation/startup failures. A JS shim being
# present on PATH is not sufficient when its platform package is absent (the
# Codex npm package's optional native dependency is the production incident).
# These failures happen before any provider request and must never cool down an
# account or participate in rate-limit correlation.
_RUNTIME_UNAVAILABLE_PATTERNS: tuple[str, ...] = (
    "missing optional dependency @openai/codex-",
    "@openai/codex-linux-x64",
    "@openai/codex-linux-arm64",
    "could not find native codex binary",
    "codex cli is not runnable",
    "codex: command not found",
    "command not found: codex",
    "spawn codex enoent",
)


def _runtime_unavailable(combined: str) -> bool:
    return any(kw in combined for kw in _RUNTIME_UNAVAILABLE_PATTERNS)


_EXPLICIT_PROVIDER_RATE_LIMIT_PATTERNS: tuple[str, ...] = (
    "rate limit reached",
    "rate_limit_error",
    "rate_limit_exceeded",
    "too many requests",
    "api usage exhausted",
    "usage limit reached",
    "you've hit your limit",
    "you've hit your usage limit",
    "insufficient_quota",
)


def _explicit_provider_rate_limit_evidence(combined: str) -> bool:
    """True only for provider-emitted quota/throttle signatures.

    Intentionally excludes bare ``429`` and generic ``quota`` so GitHub/GCP
    failures and prose in task output cannot cool an LLM account.
    """
    return any(kw in combined for kw in _EXPLICIT_PROVIDER_RATE_LIMIT_PATTERNS)


# Deterministic agent/CLI permission-flag rejections. The model CLIs refuse
# their permission-skip flags under root/sudo:
#   claude -p ... --dangerously-skip-permissions
#       → "--dangerously-skip-permissions cannot be used with root/sudo
#          privileges for security reasons"
#   codex exec ... --dangerously-bypass-approvals-and-sandbox  (analogous)
# These are 100% deterministic deployment/config defects (e.g. a container that
# runs the agent as root) — they reproduce identically on every retry. They must
# NEVER be reclassified as RATE_LIMIT by the account/pool cooldown correlation —
# doing so requeues them into a retry storm (each retry hits the same wall, and
# the requeue path may blacklist a perfectly healthy LLM account).
_AGENT_CONFIG_REJECTION_PATTERNS: tuple[str, ...] = (
    "cannot be used with root/sudo",
    "dangerously-skip-permissions cannot be used",
    "dangerously-bypass-approvals-and-sandbox cannot be used",
)


def _agent_config_rejection(combined: str) -> bool:
    return any(kw in combined for kw in _AGENT_CONFIG_REJECTION_PATTERNS)


# Git signatures emitted when the target branch was deleted before clone/fetch.
# `git clone --branch <gone>` prints the "could not find remote branch" warning
# then fails; a fetch/checkout of a deleted ref prints "couldn't find remote
# ref". Kept narrow so ordinary "not found" code errors don't over-match.
_BRANCH_GONE_PATTERNS: tuple[str, ...] = (
    "could not find remote branch",
    "couldn't find remote ref",
    "couldnt find remote ref",
)


# Signatures emitted when the remote repository has no commits at all. The agent
# probes the remote (`git ls-remote`) on a missing-branch clone failure and says
# so explicitly, because git's own error text cannot tell an empty repo apart
# from a deleted branch; the GitHub API phrasings are matched too.
#
# Two agent shapes produce these:
#   1. clone-FAILS path: `git clone --branch <trunk>` of a commitless repo dies
#      with the branch-missing text; `cloneWithRetry` (index.ts) probes the
#      remote, finds zero refs, and throws "the repository has no commits…".
#   2. clone-SUCCEEDS path: a commitless repo clones with exit 0 (git only warns
#      "you appear to have cloned an empty repository"), leaving no HEAD;
#      `verifyCloneDelivered` (discovery.ts) probes the remote, finds zero refs,
#      self-reports the emptiness, and throws the same canonical phrase.
# ``"clone produced an empty working tree"`` is the narrow no-HEAD fallback from
# shape 2 — it only appears when the agent has explicitly confirmed the remote is
# commitless, so an unrelated clone failure (partial/wrong checkout) cannot match
# it. The GitHub API phrasings are matched too.
_REPO_UNINITIALIZED_PATTERNS: tuple[str, ...] = (
    "repository has no commits",
    "clone produced an empty working tree",
    "this repository is empty",
    "git repository is empty",
)


# Signature emitted by the agent's clone-delivery guard (verifyCloneDelivered in
# agent/src/prompts/discovery.ts) when the DURABLE signal says the repo is NOT
# empty (repo_is_empty === false) but the freshly-cloned working tree is
# empty/near-empty. This is the specific "durable-vs-live disagreement" that a
# stale ``repo_is_empty=False`` on a genuinely empty repo produces on EVERY task
# — distinct from an ordinary transient clone_failed (network blip, partial
# fetch) so the report path can self-heal the durable signal instead of looping
# clone_failed forever. Cross-language contract: the substring must stay in sync
# with the message thrown in discovery.ts (branch (2) of verifyCloneDelivered).
_CLONE_DELIVERY_DISAGREEMENT_PATTERNS: tuple[str, ...] = (
    "the clone did not deliver the expected content",
)


def is_clone_delivery_disagreement(combined: str) -> bool:
    """True when a clone_failed report is the durable/live emptiness disagreement.

    Matches the ``verifyCloneDelivered`` message emitted when the stored
    ``repo_is_empty=False`` contradicts an empty cloned tree — the signature of
    a STALE durable signal — and NOT a generic transient clone failure (missing
    HEAD, wrong checkout, network error). Case-insensitive; safe on empty/None.
    """
    if not combined:
        return False
    lowered = combined.lower()
    return any(kw in lowered for kw in _CLONE_DELIVERY_DISAGREEMENT_PATTERNS)


def _is_repo_uninitialized(combined: str) -> bool:
    """True when the failure text shows the remote repo has zero commits.

    Must be checked BEFORE :func:`_is_branch_gone`: an empty repo's clone error
    also matches the branch-gone patterns, and the more specific cause wins.
    """
    return any(kw in combined for kw in _REPO_UNINITIALIZED_PATTERNS)


def is_repo_uninitialized(combined: str) -> bool:
    """True when the failure text shows the remote repo has zero commits.

    Public alias for :func:`_is_repo_uninitialized`.  Used by
    ``agent_status.py`` to route the commitless-repo clone-failure signature to
    the self-heal path.  Case-insensitive; safe on empty/None.
    """
    if not combined:
        return False
    return _is_repo_uninitialized(combined.lower())


def _is_branch_gone(combined: str) -> bool:
    """True when the failure text shows the target branch/ref no longer exists.

    Matches the clone-warning / fetch-ref forms directly, plus the
    "fatal: Remote branch <name> not found in upstream origin" form (whose branch
    name varies) — anchored on the git-specific "not found in upstream" tail so a
    generic "... not found" code error does not over-match."""
    if any(kw in combined for kw in _BRANCH_GONE_PATTERNS):
        return True
    return "remote branch" in combined and "not found in upstream" in combined


# Failure classes that don't penalize direction health / research productivity —
# the task never really ran because the infra (VMs, rate limits, upstream APIs,
# agent lifecycle) got in the way, or the scope was structurally mismatched
# (SCOPE_OVERFLOW — wrong budget, not bad code). Imported by direction_outcomes
# and research_completion so both stay in sync.
INFRA_CLASSES: frozenset[str] = frozenset(
    {
        PROVISION_FAILURE,
        RATE_LIMIT,
        AGENT_TIMEOUT,
        EXTERNAL_API,
        SCOPE_OVERFLOW,
        BLOCKED_PRECONDITION,
        BRANCH_GONE,
        REPO_UNINITIALIZED,
        AGENT_STALLED,
        SCOPE_EMPTY,
        REASSIGNED,
        NO_PROGRESS_TIMEOUT,
        PARK_WAKE_TIMEOUT,
        PARK_WAKE_STRANDED,
        DEAD_AUTH,
        APPROVED_CAP_PR_CLOSED,
        ADMIN_CANCEL,
    }
)

# Analytics superset of INFRA_CLASSES.  Consumers that bucket failures into
# infra vs code-defect for ANALYTICS (direction health, streaks, success rates,
# lane waste, review-cap counting) gate on a membership set; INFRA_CLASSES
# deliberately EXCLUDES UNPLACEABLE_EXECUTION_SITE because the resurrection
# sweep (reaper_infra_recovery Step 5) flips only INFRA_CLASSES members back to
# QUEUED and an unplaceable task is terminal.  But for analytics bucketing an
# unplaceable task is a routing/platform condition, not a code defect — taxonomy
# routes it to TAXONOMY_INFRA_RELATED — so it belongs in the infra bucket.
# This explicit superset names that membership once instead of re-deriving it at
# every call site.  Consumers that gate on INFRA_CLASSES for RESURRECTION /
# transient-retry semantics must keep using INFRA_CLASSES.
INFRA_ANALYTICS_CLASSES: frozenset[str] = INFRA_CLASSES | {
    UNPLACEABLE_EXECUTION_SITE,
    # Conflict resolution destroyed a landed child deliverable — a platform
    # defect, so it must not depress direction health / erosion scoring.  Kept
    # OUT of INFRA_CLASSES for the same resurrection reason as the entry above.
    ROLLUP_DELIVERABLE_DROPPED,
    MALFORMED_TASK_PAYLOAD,
}

# ── Infra-resurrection eligibility: ONE predicate, two call sites ────────────
#
# Two places decide whether a FAILED task will be auto-resurrected by the
# reaper's Step 5 infra-recovery sweep:
#
#   * ``reaper_infra_recovery.run_infra_recovery_and_hold_maintenance`` — the
#     sweep that performs the FAILED -> QUEUED flip, and
#   * ``rollup_management._check_and_trigger_rollup`` — which HOLDS an
#     EVALUATING parent ("Recovering: … eligible for auto-resurrection") while
#     one of its review/fix children waits for that flip.
#
# They used to carry two hand-copied predicates, and those had drifted into
# being DISJOINT rather than merely unequal: the sweep refuses outright to
# resurrect a review/fix task carrying a ``pr_number`` (it must be re-driven
# through canonical review admission, not a generic QUEUED flip), while the
# rollup's copy never learned that rule — and the rollup's copy only ever looks
# at review/fix children. So an EVALUATING parent whose review/fix child failed
# on an infra class was held for the full recovery window waiting on a
# resurrection the other side guaranteed would never happen, with every child
# already terminal: a permanent wedge, advertised to the operator as recovery
# in progress. Naming the predicate ONCE makes that divergence unrepresentable.
#
# Reason codes rather than a bare bool: a caller that declines to act has to be
# able to say why, in logs and in operator-facing status.
RESURRECTION_BLOCK_PR_PIPELINE_TASK = "pr_pipeline_requires_review_admission"
RESURRECTION_BLOCK_NOT_INFRA_CLASS = "failure_class_not_infra"
RESURRECTION_BLOCK_ALREADY_RESURRECTED = "already_resurrected"
RESURRECTION_BLOCK_PR_TERMINAL_CLOSURE = "pr_terminal_closure"
RESURRECTION_BLOCK_OOM = "oom_not_transient"


def infra_resurrection_block_reason(task: "Task") -> str | None:
    """Why Step 5 will not resurrect ``task``, or ``None`` if it may.

    Intrinsic reasons only — properties of the task itself, stable across
    cycles. Situational gates that can flip between cycles (upstream API health
    zone availability, a live duplicate holding the idempotency key, the task
    being a parent) stay in the sweep: a caller that HOLDS on this predicate is
    asserting the block is permanent, and must not hold on a transient one.
    """
    ctx = task.context or {}
    payload = task.first_payload or {}

    # PR review/fix failures must be re-driven from current GitHub state
    # through canonical review admission. A generic FAILED -> QUEUED flip can
    # resurrect a stale review target or writer and wastes an agent even though
    # the claim fence would ultimately reject it.
    if payload.get("type") in {"review", "fix"} and payload.get("pr_number"):
        return RESURRECTION_BLOCK_PR_PIPELINE_TASK

    # Only infra-class failures are resurrectable at all.
    if ctx.get("failure_category", "") not in INFRA_CLASSES:
        return RESURRECTION_BLOCK_NOT_INFRA_CLASS

    # At most one resurrection per task, ever.
    if int(ctx.get("_resurrection_count", 0) or 0) >= 1:
        return RESURRECTION_BLOCK_ALREADY_RESURRECTED

    # Terminal PR-closure outcomes. classify_failure routes "closed without
    # merging" / empty-rollup closures into EXTERNAL_API (it shares the
    # merge-blocking GitHub bank), but operator rejection and empty-rollup
    # short-circuit are not infrastructure failures — re-running produces the
    # same rejected/empty PR. Mirrors the closure markers documented in
    # services._pr_possibly_open.
    if (
        ctx.get("pr_closed_unmerged") is True
        or ctx.get("pr_closed_empty") is True
        or ctx.get("pr_rejected") is True
    ):
        return RESURRECTION_BLOCK_PR_TERMINAL_CLOSURE

    # Process crashes and heartbeat timeouts are transient per-VM issues. OOM
    # kills are NOT: the task scope hasn't changed and it will OOM again on the
    # same VM type. Let scope reduction handle it.
    if ctx.get("timeout_subtype") == "oom":
        return RESURRECTION_BLOCK_OOM

    return None


# Cause frozensets for adversarial grader non-response routing.  Each set
# names the cause categories (from classify_grader_nonresponse_cause) that
# map to a specific retry strategy.  Defined here so research_completion.py
# can route without duplicating the failure-taxonomy vocabulary.

# Time-based failures: the grader ran out of time.  Retry strategy: reduce the
# challenge manifest so the next grader fits within the time budget.  Findings
# excluded from the reduced manifest fail closed via the manifest-exclusion path.
GRADER_TIMEOUT_CAUSES: frozenset[str] = frozenset(
    {
        AGENT_TIMEOUT,
        NO_PROGRESS_TIMEOUT,
        PARK_WAKE_TIMEOUT,
        PARK_WAKE_STRANDED,
    }
)

# Transient infra failures: the grader failed for a reason unrelated to the
# challenge content (rate-limit, provisioning failure, external API, etc.).
# Retry strategy: identical manifest on a fresh grader (existing wholesale
# retry), counted against the wholesale budget rather than adding a new slot.
GRADER_TRANSIENT_INFRA_CAUSES: frozenset[str] = frozenset(
    {
        RATE_LIMIT,
        PROVISION_FAILURE,
        EXTERNAL_API,
        REASSIGNED,
        DEAD_AUTH,
        AGENT_STALLED,
    }
)

# Ordered tuple of all canonical failure classes for per-account health panel display.
ALL_FAILURE_CLASSES: tuple[str, ...] = (
    CODE_ERROR,
    RATE_LIMIT,
    DEAD_AUTH,
    EXTERNAL_API,
    PROVISION_FAILURE,
    MIGRATION_ERROR,
    VIEWPORT_REGRESSION,
    AGENT_TIMEOUT,
    AGENT_STALLED,
    SCOPE_OVERFLOW,
    BLOCKED_PRECONDITION,
    BRANCH_GONE,
    REPO_UNINITIALIZED,
    SCOPE_EMPTY,
    REASSIGNED,
    NO_PROGRESS_TIMEOUT,
    PARK_WAKE_TIMEOUT,
    PARK_WAKE_STRANDED,
    APPROVED_CAP_PR_CLOSED,
    ADMIN_CANCEL,
    UNPLACEABLE_EXECUTION_SITE,
    ROLLUP_DELIVERABLE_DROPPED,
    MALFORMED_TASK_PAYLOAD,
)


# Taxonomy sub-categories — finer-grained grouping for failure-mode analysis.
# INFRA_RELATED covers everything in INFRA_CLASSES; the other three partition
# CODE_ERROR failures into review-quality (test/lint/style), scope-related
# (complexity/size), and residual code defects.
TAXONOMY_REVIEW_QUALITY = "review_quality"
TAXONOMY_SCOPE_RELATED = "scope_related"
TAXONOMY_CODE_DEFECT = "code_defect"
TAXONOMY_INFRA_RELATED = "infra_related"


_REVIEW_QUALITY_KEYWORDS = (
    "pytest",
    "jest",
    "ruff",
    "eslint",
    "tsc",
    "mypy",
    "lint_failed",
    "test_failed",
    "build_failed",
    "lint error",
    "lint failed",
    "tests failed",
    "test failure",
    "type error",
    "typecheck",
    "type check",
    "style",
    "formatting",
)

_SCOPE_KEYWORDS = (
    "too many changes",
    "too many files",
    "multi-file conflict",
    "scope too large",
    "exceeds scope",
    "scope complexity",
    "implementation complexity exceeds",
    "complexity budget",
)


def classify_failure_taxonomy(task: "Task") -> str:
    """Return a 4-category taxonomy label for a failed task.

    Buckets CODE_ERROR failures into review_quality / scope_related / code_defect
    based on status-message signals and task context; all INFRA_CLASSES map to
    infra_related. Reuses classify_failure() for the base class.
    """
    base = classify_failure(task)
    # SCOPE_OVERFLOW is in INFRA_CLASSES for retry/budget routing, but it
    # represents a scope-estimation mismatch, not an infra outage. Route it
    # to scope_related so operators see it in the correct analytics bucket.
    if base == SCOPE_OVERFLOW:
        return TAXONOMY_SCOPE_RELATED
    if base in INFRA_CLASSES:
        return TAXONOMY_INFRA_RELATED
    # UNPLACEABLE_EXECUTION_SITE is deliberately NOT in INFRA_CLASSES (so the
    # resurrection sweep never re-queues it), but it is an infra/routing
    # condition, not a code defect — keep it in the infra analytics bucket.
    if base == UNPLACEABLE_EXECUTION_SITE:
        return TAXONOMY_INFRA_RELATED
    # MALFORMED_TASK_PAYLOAD is likewise outside INFRA_CLASSES (terminal, never
    # resurrected) but is a spawner/platform defect, not agent-written code.
    if base == MALFORMED_TASK_PAYLOAD:
        return TAXONOMY_INFRA_RELATED
    # Migration errors are code defects (the schema is wrong / migration missing),
    # not review-quality or scope issues — route to code_defect bucket directly.
    if base == MIGRATION_ERROR:
        return TAXONOMY_CODE_DEFECT

    status_msg = (task.latest_status_message or "").lower()
    ctx = task.context or {}

    # A review-fix loop that exhausted its iteration cap and one that was
    # ABANDONED when its bounded operator-disposition window expired are the same
    # finding: the review -> fix loop did not converge. That is review quality,
    # not a code defect the subtask introduced. Without this the disposition
    # sweep's rows fall through to the TAXONOMY_CODE_DEFECT default and every
    # abandoned park is reported as a fresh code defect.
    if ctx.get("failure_detail") in (
        "review_cap_exhausted",
        REVIEW_PARK_ABANDONED_FAILURE_DETAIL,
    ):
        return TAXONOMY_REVIEW_QUALITY

    if any(kw in status_msg for kw in _REVIEW_QUALITY_KEYWORDS):
        return TAXONOMY_REVIEW_QUALITY

    expansion_ratio = ctx.get("expansion_ratio")
    has_scope_evidence = any(kw in status_msg for kw in _SCOPE_KEYWORDS) or (
        isinstance(expansion_ratio, (int, float)) and expansion_ratio > 1.5
    )
    # scope_calibrated_from alone is not evidence — a calibrated task can fail
    # for unrelated code defects. Require corroborating scope evidence.
    if has_scope_evidence:
        return TAXONOMY_SCOPE_RELATED

    return TAXONOMY_CODE_DEFECT


# Task types excluded from code_defect investigation triggers — investigate tasks
# would recurse, and smoketests are exploratory probes whose failures don't
# represent a code defect worth diagnosing.
_INVESTIGATION_EXCLUDED_TYPES: frozenset[str] = frozenset({"investigate", "smoketest"})


def should_trigger_review_quality_investigation(task: "Task") -> bool:
    """Return True if a failed task is a candidate for a review-quality pattern
    investigation (gating only — streak check is performed at the spawn site
    because it requires an async DB query).

    Same exclusions as ``should_trigger_code_defect_investigation``: no
    direction_id → skip; investigate/smoketest types → skip. Taxonomy must be
    review_quality (classify_failure_taxonomy).
    """
    payload = (task.payloads or [{}])[0] if hasattr(task, "payloads") else {}
    task_type = (payload or {}).get("type") if isinstance(payload, dict) else None
    if task_type in _INVESTIGATION_EXCLUDED_TYPES:
        return False
    ctx = task.context or {}
    if not ctx.get("direction_id"):
        return False
    return classify_failure_taxonomy(task) == TAXONOMY_REVIEW_QUALITY


def should_trigger_code_defect_investigation(task: "Task") -> bool:
    """Return True if a failed task warrants spawning a CODE_ERROR investigation.

    Gates the task-level investigation trigger: only fires for CODE_ERROR
    failures with code_defect taxonomy (review_quality and scope_related have
    their own handling), skips investigate/smoketest tasks to prevent self-loops,
    and requires a direction_id so the investigation can be linked.
    """
    payload = (task.payloads or [{}])[0] if hasattr(task, "payloads") else {}
    task_type = (payload or {}).get("type") if isinstance(payload, dict) else None
    if task_type in _INVESTIGATION_EXCLUDED_TYPES:
        return False
    ctx = task.context or {}
    if not ctx.get("direction_id"):
        return False
    if classify_failure(task) != CODE_ERROR:
        return False
    return classify_failure_taxonomy(task) == TAXONOMY_CODE_DEFECT


# AGENT_TIMEOUT sub-types that indicate a code-caused failure (resource exhaustion
# or process crash) worth investigating. Operational sub-types (cascade, iteration
# limit, execution timeout) are excluded — they reflect orchestrator decisions or
# wall-clock limits, not defects in the code under test.
INVESTIGATABLE_TIMEOUT_SUBTYPES: frozenset[str] = frozenset(
    {
        "oom",
        "process_crash",
        "heartbeat_lost",
    }
)


def should_trigger_timeout_investigation(task: "Task") -> bool:
    """Return True if an AGENT_TIMEOUT failure warrants spawning an investigation.

    Mirrors ``should_trigger_code_defect_investigation`` but for the subset of
    timeout sub-types that indicate code-caused resource issues (OOM, crash,
    heartbeat loss) rather than operational cancellations.
    """
    payload = (task.payloads or [{}])[0] if hasattr(task, "payloads") else {}
    task_type = (payload or {}).get("type") if isinstance(payload, dict) else None
    if task_type in _INVESTIGATION_EXCLUDED_TYPES:
        return False
    ctx = task.context or {}
    if not ctx.get("direction_id"):
        return False
    if classify_failure(task) != AGENT_TIMEOUT:
        return False
    subtype = ctx.get("timeout_subtype") or classify_timeout_subtype(task)
    return subtype in INVESTIGATABLE_TIMEOUT_SUBTYPES


_AGENT_CATEGORY_TO_CLASS: dict[str, str] = {
    "clone_failed": PROVISION_FAILURE,
    # build_failed = test setup couldn't run (Postgres sidecar startup, Docker
    # build, dependency install). Agent emits this when the test environment
    # itself failed to come up, NOT when user code failed to compile — the
    # latter would surface as test_failed or claude_code_failed. Routing as
    # PROVISION_FAILURE gets the 5-min infra backoff and resurrection-sweep
    # eligibility instead of burning the code-error retry budget on something
    # the agent can't fix by editing code.
    "build_failed": PROVISION_FAILURE,
    # ``diff_range_unresolvable`` — a release audit / audit-challenge / merge-grade
    # handler could not fetch the pinned candidate commit (or verify the three-dot
    # diff range) before invoking the agent. Same family as clone_failed /
    # build_failed / vm_config_missing: a workspace candidate-fetch transient
    # (a single GitHub I/O blip on `git fetch origin <sha>`), NOT a code defect.
    # Routing to PROVISION_FAILURE lets the release qualifier respawn the audit /
    # challenge leg under _MAX_AUDIT_RL_RESPAWNS instead of finalizing the whole
    # attempt infra_failure. (The agent-side fetch now retries with backoff; this
    # is defense-in-depth for the residual case where all retries blip out.)
    # Production 2026-06-26: an attempt died on this category, yet the SAME
    # candidate re-fetched and COMPLETED ~9 min later — proving it transient.
    "diff_range_unresolvable": PROVISION_FAILURE,
    "test_failed": CODE_ERROR,
    "lint_failed": CODE_ERROR,
    "pr_creation_failed": EXTERNAL_API,
    "external_api": EXTERNAL_API,
    "oauth_rotation_failed": EXTERNAL_API,
    "claude_code_failed": CODE_ERROR,
    "parse_failed": CODE_ERROR,
    "fatal": CODE_ERROR,
    "rate_limited": RATE_LIMIT,
    "reset_failed": PROVISION_FAILURE,
    "scope_overflow": SCOPE_OVERFLOW,
    "timeout": AGENT_TIMEOUT,
    # 2026-05-04: emitted by the new Claude-driven flow.
    # ``agent_self_failed`` — Claude called ``./fail "<reason>"`` because the
    # task fundamentally couldn't be completed (scope wrong, blocker, etc.).
    # The reason text is preserved as latest_status_message; the category
    # routes through the CODE_ERROR retry budget so the orchestrator's
    # diagnosis pipeline (direction-hold investigation, etc.) still fires.
    "agent_self_failed": CODE_ERROR,
    # ``incomplete`` — wrapper-fallback when Claude exited without calling
    # ./done|./fail and didn't open a PR. Distinct from claude_code_failed
    # (which is for non-zero exit) so analytics can separate "Claude bailed
    # silently" from "Claude crashed."
    "incomplete": CODE_ERROR,
    # ``agent_stalled`` — reaper-emitted when heartbeat is fresh but no tool/LLM
    # progress for longer than the scope-aware threshold. Written as lowercase by
    # the reaper; fast-path maps it to the canonical AGENT_STALLED constant.
    "agent_stalled": AGENT_STALLED,
    # ``scope_empty`` — agent-emitted when declared related_files were already
    # covered by a sibling cluster's merged PR; exits cleanly without retries.
    "scope_empty": SCOPE_EMPTY,
    # ``pr_branch_deleted`` — agent completed the task but the PR branch was
    # already deleted (merged and auto-deleted), so there was nothing to review
    # or fix. Similar to SCOPE_EMPTY (task had no work to do), routed to that
    # class so it doesn't consume retry budget or penalize direction health.
    "pr_branch_deleted": SCOPE_EMPTY,
    # ``runtime_unavailable`` — agent detected a required CLI binary (e.g.
    # ``codex``) is not installed on the runner BEFORE spawning. The agent exits
    # with code 42 so the runner skips the failure-streak increment. Routes to
    # PROVISION_FAILURE (INFRA class) so it requeues without burning retry budget
    # and the orchestrator can blacklist the unavailable runtime for this runner.
    "runtime_unavailable": PROVISION_FAILURE,
    # ``reassigned`` — agent self-terminated gracefully because the task is no
    # longer assigned to its VM (non-transient heartbeat 4xx = reassignment/
    # deauth, or cancel/supersede during startup). Posted synchronously from the
    # self-terminate path (cluster C7) so the task gets a terminal FAILED status
    # instead of being stranded IN_PROGRESS until the reaper times it out (false
    # AGENT_TIMEOUT, discarded work). An intentional handoff is NOT a code
    # defect — REASSIGNED is in INFRA_CLASSES so it neither burns the retry
    # budget nor penalizes direction health.
    "reassigned": REASSIGNED,
    # ``no_progress_timeout`` -- agent's llm-no-progress watchdog fired on a
    # genuinely silent call with NO confirmed rate-limit message. Transient
    # provider hang of unknown cause; routes to NO_PROGRESS_TIMEOUT (in
    # INFRA_CLASSES) so it requeues without burning retry AND without blocking the
    # account. A CONFIRMED rate-limit message is reported as ``rate_limited``
    # (-> RATE_LIMIT, block + cooldown) instead — see runners.ts.
    "no_progress_timeout": NO_PROGRESS_TIMEOUT,
    # ``vm_config_missing`` — the provider CLI died because a boot/retarget-
    # materialized config file is absent from the VM (canonical case: Claude CLI
    # exits in seconds with "Claude configuration file not found" when
    # ~/.claude.json was destroyed by a warm re-target that died mid-sequence).
    # Emitted by VmConfigMissingError (agent runners.ts) and by the agent's
    # claim-time config guard. A VM/infra fault, not a code defect and NOT usage
    # exhaustion: route to PROVISION_FAILURE so the task gets the infra backoff +
    # requeue on a fresh VM instead of burning the code-error retry budget or
    # cooling down a healthy account. Incident 2026-06-06 (tasks 1330d291 /
    # cf28097f): without this label the generic ``fatal`` + init-banner-only tail
    # was reclassified to RATE_LIMIT ("usage exhausted") above.
    "vm_config_missing": PROVISION_FAILURE,
    # ``github_token_fetch_failed`` — the agent's startup github/token fetch
    # (get_github_token_for_task) timed out or failed. The backend legitimately
    # blocks up to ~120s while _github_request sleeps on a near-empty rate-limit
    # bucket, so a credential-fetch failure is an infra/transient event, not a
    # code fault. Route to PROVISION_FAILURE so the task requeues on a fresh VM
    # without burning the terminal retry budget or cascading the parent plan.
    "github_token_fetch_failed": PROVISION_FAILURE,
    # ``park_wake_timeout`` / ``park_wake_stranded`` — park/wake orchestration
    # outcomes written by the reaper's wake-delivery sweep / expire path (lowercase
    # literals matching the canonical class VALUES). Mapped here so the
    # failure_category fast-path canonicalizes them explicitly instead of routing
    # an unknown label through classify_failure(); both are budget-exempt INFRA
    # classes (see INFRA_CLASSES) so neither burns the retry budget.
    "park_wake_timeout": PARK_WAKE_TIMEOUT,
    "park_wake_stranded": PARK_WAKE_STRANDED,
    # ``compute_unmeasured_criterion`` — a compute_experiment declared a
    # kind=metric success_criterion but produced no numeric value for it. A
    # genuine measurement/work defect (the command must emit the declared
    # metric), so it routes to CODE_ERROR for retry-budget + diagnosis while the
    # distinct failure_category literal stays visible on task.context for the
    # spawning agent and operator panels.
    "compute_unmeasured_criterion": CODE_ERROR,
    "compute_preflight_smoke_failed": CODE_ERROR,
    # ``unplaceable_execution_site`` — orchestrator-emitted when a QUEUED task's
    # registry-declared execution site has no eligible runner. Canonical terminal
    # class outside INFRA_CLASSES so the resurrection sweep never re-queues it.
    "unplaceable_execution_site": UNPLACEABLE_EXECUTION_SITE,
    # ``malformed_task_payload`` — the task row's own payload cannot satisfy its
    # declared type contract. Canonicalized here so the label round-trips; the
    # generic-``fatal`` branch of normalize_agent_failure_category also DERIVES
    # this class from the payload itself, because the agent reports only
    # ``fatal`` when its handler rejects a missing required field.
    "malformed_task_payload": MALFORMED_TASK_PAYLOAD,
}


_GENERIC_AGENT_CODE_LABELS: frozenset[str] = frozenset(
    {
        "claude_code_failed",
        "fatal",
    }
)

# Wider set of generic failure labels where account-state correlation applies.
# These produce no specific failure signal (no test/lint/pr error), so a
# rate-limited account is a plausible cause that the text scan can't see.
_ALL_GENERIC_CODE_ERROR_LABELS: frozenset[str] = frozenset(
    {
        "claude_code_failed",
        "fatal",
        "incomplete",
        "agent_self_failed",
        "parse_failed",
    }
)


def _persist_rate_limit_signal_context(task: "Task", signal) -> None:
    """Persist rate-limit signal metadata onto task.context.

    Audit 2026-04-25 (W5/B1): defensive ``getattr`` instead of direct
    attribute access. If the provider returns a partial signal (missing
    .kind etc.) the previous version silently swallowed the AttributeError
    and downstream retry logic had no rate_limit_kind to read — RATE_LIMIT
    was returned but the cooldown key was missing, so requeue ran
    immediately instead of waiting. Now we always write *something* (kind=
    "unknown" when the source is malformed) plus an explicit
    ``rate_limit_persistence_failed`` flag so the requeue path can apply a
    conservative cooldown.
    """
    kind = getattr(signal, "kind", None)
    retry_after = getattr(signal, "retry_after_seconds", None)
    matched = getattr(signal, "matched_pattern", None)
    try:
        ctx = dict(task.context or {})
        ctx["rate_limit_kind"] = kind or "unknown"
        if retry_after is not None:
            ctx["rate_limit_retry_after_seconds"] = retry_after
        if matched is not None:
            ctx["rate_limit_matched_pattern"] = matched
        if kind is None:
            ctx["rate_limit_persistence_failed"] = True
            logger.warning(
                "[FAILURE_CLASS] Task %s rate-limit signal missing .kind; persisted with kind=unknown",
                task.id,
            )
        task.context = ctx
    except Exception:
        logger.exception("[FAILURE_CLASS] Failed to persist rate_limit_* keys")


def _persist_correlation_reclassification_context(task: "Task") -> None:
    """Persist a marker on task.context when RATE_LIMIT is inferred via account/pool
    correlation with no text/keyword signal matched.

    Writes rate_limit_kind="unknown" (if not already set) and
    rate_limit_correlation_reclassified=True so operators and downstream digest
    consumers can distinguish correlation-based reclassifications from text-matched ones,
    and so the cooldown bookkeeping path has a rate_limit_kind value to act on.
    """
    try:
        ctx = dict(task.context or {})
        if "rate_limit_kind" not in ctx:
            ctx["rate_limit_kind"] = "unknown"
        ctx["rate_limit_correlation_reclassified"] = True
        task.context = ctx
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] Failed to persist correlation reclassification marker"
        )


def _account_was_rate_limited(task: "Task", at_time: "datetime | None" = None) -> bool:
    """Return True if the task's assigned account was in rate-limit cooldown.

    Reads the in-memory pool state (_account_by_id). Returns False when the
    account is unresolvable (pool empty, standalone test path, no account
    assigned) so callers degrade to today's text-only classification.
    Never raises — any exception returns False.

    at_time: reference timestamp for the cooldown check. Pass task.last_heartbeat
    (the failure instant) so a reaper lag between failure and sweep cannot cause
    a rate-limited silent death to miss the correlation. Defaults to _utcnow().
    """
    try:
        from app.max_plan_pool import (
            _account_by_id,
            _cooldown_seconds_for_info,
            get_task_llm_account_id,
        )

        account_id = get_task_llm_account_id(task)
        if not account_id:
            return False
        acct = _account_by_id.get(str(account_id))
        if not acct:
            return False
        dm = acct.get("disabled_models") or {}
        if not dm:
            return False
        # Use naive UTC to match max_plan_pool's stored 'at' datetimes (naive via
        # _utcnow at write time). Strip tzinfo if caller passed a tz-aware datetime
        # to avoid TypeError in the subtraction instead of silently swallowing it.
        now = at_time if at_time is not None else _utcnow()
        if getattr(now, "tzinfo", None) is not None:
            now = now.replace(tzinfo=None)
        for info in dm.values():
            at_dt = info.get("at")
            if at_dt is None:
                continue
            if (now - at_dt).total_seconds() < _cooldown_seconds_for_info(info):
                return True
        return False
    except Exception:
        logger.debug(
            "[FAILURE_CLASS] _account_was_rate_limited degraded to False for task %s",
            getattr(task, "id", None),
        )
        return False


def _local_pool_view_sufficient(scope: dict) -> bool:
    """Return True if the local in-memory pool can be trusted for a *scoped*
    pool-wide rate-limit reclassification verdict.

    Multi-instance hazard (design-audit ``failure-class-multiinstance``):
    ``pool_wide_rate_limit_cooldown`` forms an "every eligible account is
    blocked" verdict by reading process-local pool state (``_account_by_id`` /
    ``_max_plan_pools``). Cooldowns are recorded into the in-memory pool by
    whichever Cloud Run instance handled the rate-limited request, and a
    cooldown another instance has since *cleared* (probe succeeded) lingers
    stale in this instance's view. An instance classifying a failure may
    therefore reach a True verdict from a view that does not actually contain —
    or no longer faithfully represents — the candidate set the task was routed
    to. That spuriously reclassifies a genuine CODE_ERROR as RATE_LIMIT.

    Bounded gate (no migration, no new shared store): when the task has a
    *resolvable dispatch scope* (a routed provider or the owning user_id), the
    pool-wide verdict is only trustworthy if the local pool actually holds at
    least one enabled account in that scope. A scoped verdict over a local view
    that contains zero matching accounts cannot have inspected the real
    candidate set — it reflects a partial cross-instance view, so decline to
    reclassify and let the caller fall back to keyword classification (or to the
    durable ``db_rl_override`` signal the authoritative reaper / agent_status
    paths supply).

    Scope-less correlation (no provider, no account — pre-assignment /
    accountless silent death) is left untouched: there is no candidate set to
    compare against, so the global verdict stands exactly as before. This keeps
    the change behavior-preserving on a single instance — a single instance
    loads every account wholesale, so a routed task's scope always resolves to
    locally-present accounts (blocked or not), and the gate returns True.
    """
    try:
        from app.max_plan_pool import (
            _max_plan_pools,
            _normalize_provider_name,
        )
        from app.models import LLMProviderName

        providers = scope.get("providers")
        user_id = scope.get("user_id")

        # No resolvable scope → global/accountless correlation. There is no
        # candidate set to validate the local view against, so preserve the
        # existing global-verdict behavior (sufficiency is not assessable here).
        if not providers and not user_id:
            return True

        accts = _max_plan_pools.get(user_id, []) if user_id else None
        if accts is None:
            # provider-scoped without a user_id → consider the whole local pool.
            from app.max_plan_pool import _all_accounts

            accts = _all_accounts()

        if providers:
            norm_providers = {
                _normalize_provider_name(p) or LLMProviderName.ANTHROPIC.value
                for p in providers
            }
            accts = [
                a
                for a in accts
                if (
                    _normalize_provider_name(a.get("provider"))
                    or LLMProviderName.ANTHROPIC.value
                )
                in norm_providers
            ]

        # At least one enabled candidate must be visible locally for the scoped
        # pool-wide verdict to reflect the real candidate set. (Disabled-but-
        # rate-limited accounts still count as "visible" — they're exactly what
        # the legitimate single-instance whole-pool-blocked path relies on.)
        return any(a.get("enabled", True) for a in accts)
    except Exception:
        logger.debug("[FAILURE_CLASS] _local_pool_view_sufficient degraded to False")
        return False


def _pool_was_rate_limited(
    task: "Task | None" = None, at_time: "datetime | None" = None
) -> bool:
    """Return True if the eligible account pool was in rate-limit cooldown.

    Scopes the check to the candidate set the task could actually dispatch to:
    - providers: the task's routed provider (when resolvable from context)
    - model: the task's routed model (when stored in context)
    - user_id: the user who owns the task's assigned account (when assigned)

    Prevents accounts for unrelated users or providers from changing the verdict.
    Falls back to the global pool when task is None or context is missing.

    Multi-instance safety: gated on ``_local_pool_view_sufficient`` so a partial
    or stale process-local pool view cannot drive a spurious RATE_LIMIT
    reclassification (design-audit ``failure-class-multiinstance``). When the
    local pool has no in-scope candidates, returns False and the caller degrades
    to keyword classification.

    Never raises — returns False with a debug log on any error.
    """
    try:
        from app.max_plan_pool import (
            get_task_rate_limit_scope,
            pool_wide_rate_limit_cooldown,
        )

        norm_time = at_time
        if norm_time is not None and getattr(norm_time, "tzinfo", None) is not None:
            norm_time = norm_time.replace(tzinfo=None)
        scope = get_task_rate_limit_scope(task) if task is not None else {}

        result = pool_wide_rate_limit_cooldown(
            at_time=norm_time,
            providers=scope.get("providers"),
            model=scope.get("model"),
            user_id=scope.get("user_id"),
        )
        if not result:
            logger.debug(
                "[FAILURE_CLASS] pool_wide_rate_limit_cooldown: pool not fully blocked"
            )
            return False

        # Gate the positive verdict on the local view being representative.
        # A multi-instance deploy can show "all visible accounts blocked" from a
        # partial local pool; only trust the verdict when in-scope candidates are
        # actually present locally.
        if not _local_pool_view_sufficient(scope):
            logger.debug(
                "[FAILURE_CLASS] pool_wide_rate_limit_cooldown verdict ignored: "
                "local pool view insufficient (multi-instance partial view)"
            )
            return False
        return True
    except Exception:
        logger.debug(
            "[FAILURE_CLASS] pool_wide_rate_limit_cooldown check degraded to False"
        )
        return False


def _stuck_llm_call_no_progress(task: "Task") -> bool:
    """True when the agent started >=1 LLM call but received 0 output tokens.

    This is the signature of a provider-side hang/throttle — an Anthropic 429/529
    that the Claude Code SDK absorbed via internal retry/backoff, emitting NO
    rate-limit text the pattern matcher can see — as opposed to a genuine
    agent/code stall (which would have produced output tokens before stalling).
    Such a hang rides to the agent's 45-min wall-clock timeout (or is reaped as
    agent_stalled) and is otherwise misclassified AGENT_TIMEOUT, burning the
    retry budget and terminal-failing a healthy task (incident 2026-05-29).

    Read from task.api_call_summary, written by the agent heartbeat
    (routers/agent.py) on a last-write-wins basis, so it reflects the current
    attempt's cumulative LLM telemetry. Never raises.
    """
    # api_call_summary may be deferred (load_only) or expired on analytics call
    # sites (digest_activity / admin activity) — reading it then triggers a
    # synchronous lazy-load under async → MissingGreenlet, which getattr's
    # ``default`` does NOT catch (it only catches AttributeError). Guard the
    # access so classify_failure stays safe on partially-loaded entities; when
    # unreadable we simply can't determine the signal and decline to reclassify
    # (the authoritative reaper / agent_status sites pass fully-loaded entities,
    # and any reclassification is persisted to context for analytics to read).
    # Regression fix-forward: #6864 broke GET /api/admin/activity + digest with
    # MissingGreenlet.
    try:
        summary = task.api_call_summary
    except Exception:
        return False
    if not isinstance(summary, dict):
        return False
    try:
        calls = int(summary.get("llm_call_count") or 0)
        out_tokens = int(summary.get("total_output_tokens") or 0)
    except (TypeError, ValueError):
        return False
    return calls >= 1 and out_tokens == 0


def _persist_stuck_call_reclassification(
    task: "Task", inferred_from: str = "stuck_llm_call_no_output"
) -> None:
    """Mark a task whose failure was reclassified to RATE_LIMIT from an
    infra-death signal (stuck 0-output-token LLM call, init-banner-only CLI
    death, or a mid-stream cut). Sets the ``rate_limit_kind`` key the requeue
    path needs for cooldown bookkeeping plus an operator breadcrumb explaining
    the inference."""
    try:
        ctx = dict(task.context or {})
        ctx.setdefault("rate_limit_kind", "unknown")
        ctx["rate_limit_correlation_reclassified"] = True
        ctx["rate_limit_inferred_from"] = inferred_from
        task.context = ctx
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] Failed to persist stuck-call reclassification marker"
        )


# ── Structured agent error report (primary classification signal) ────────────
# The agent runner (agent/src/providers/runners.ts) records a structured report
# for EVERY provider CLI run — exit code, exit signal, stderr tail, structured
# provider error events from the stream, and the run shape (model_output_seen /
# result_event_seen) — and ships it with the terminal FAILED status post
# (AgentStatusPayload.error_report). agent_status.py sanitizes + persists it at
# task.context[AGENT_ERROR_REPORT_CONTEXT_KEY]. Classification consumes it as
# the PRIMARY signal: a report proving the provider CLI died without completing
# a model run (a provider-layer death — usage exhaustion, auth burp, severed
# stream) routes to the RATE_LIMIT requeue path (account rotation + cooldown,
# no retry burn), NEVER CODE_ERROR. The output-text tail heuristics below
# (_cli_startup_death_tail / _cli_stream_cut_tail) remain ONLY as the fallback
# for old agents that never sent a report. Incident 2026-06-06: a create task
# died 5s after CLI start (exit 1, init-banner-only tail — a provider error on
# the first API call), was classified CODE_ERROR, and burned all 3 retries in
# seconds with no account rotation.
AGENT_ERROR_REPORT_CONTEXT_KEY = "agent_error_report"

_ERROR_REPORT_STDERR_TAIL_CHARS = 2000
_ERROR_REPORT_MAX_PROVIDER_ERRORS = 5
_ERROR_REPORT_ERROR_MESSAGE_CHARS = 500

# ---------------------------------------------------------------------------
# Credential redaction — applied to stderr_tail and provider error messages
# before storage so secrets never reach task.context or the DB.
# ---------------------------------------------------------------------------
_CRED_FIELD_NAMES = (
    "access_token",
    "refresh_token",
    "id_token",
    "api_key",
    "client_secret",
    "private_key",
    "token",
)

# Match JSON-style "field": "value" and key=value / key: value pairs.
# Group 1: key name + separator (+ optional opening quote of value)
# Group 2: the credential value (8+ chars to skip booleans/nulls); includes '@' so that
#           OAuth tokens like ya29.a0@abc... are fully redacted (keep in sync with
#           broadcast.py _CRED_FIELD_RE and agent/src/utils/sanitize.ts)
# Group 3: optional closing quote
# The optional leading quote is consumed BEFORE the \b word-boundary check so
# that "token": "value" (JSON) is matched while OPENAI_API_KEY=value is not:
# inside OPENAI_API_KEY the underscore before API_KEY is \w, so \b does not
# fire there.
_CRED_FIELD_RE = re.compile(
    r'(["\']?\b(?:'
    + "|".join(_CRED_FIELD_NAMES)
    + r")"
    + r'\b["\']?\s*[:=]\s*["\']?)([A-Za-z0-9_.+=\-/@]{8,})(["\']?)',
    re.IGNORECASE,
)

# Ordered substitution table: (compiled-pattern, replacement-string).
# Apply sk-ant- before generic sk- so Anthropic keys are not double-matched.
# Apply sk-proj- before generic sk- for the same reason.
_CRED_REDACT_SUBS: tuple[tuple[re.Pattern, str], ...] = (
    # Embedded git URL credentials: https://x-access-token:<token>@host
    (re.compile(r"https://x-access-token:[^@]+@"), "https://x-access-token:***@"),
    # GitHub tokens
    (re.compile(r"ghp_[A-Za-z0-9_]+"), "ghp_***"),
    (re.compile(r"ghs_[A-Za-z0-9_]+"), "ghs_***"),
    (re.compile(r"ghu_[A-Za-z0-9_]+"), "ghu_***"),
    (re.compile(r"gho_[A-Za-z0-9_]+"), "gho_***"),
    (re.compile(r"ghr_[A-Za-z0-9_]+"), "ghr_***"),
    (re.compile(r"github_pat_[A-Za-z0-9_]+"), "github_pat_***"),
    # Anthropic API keys (sk-ant-…)
    (re.compile(r"sk-ant-[A-Za-z0-9_-]+"), "sk-ant-***"),
    # OpenAI project-scoped API keys (sk-proj-…); {10,} is intentionally more aggressive
    # than broadcast.py's {20,} — stored error reports tolerate more false positives
    # than the live WS broadcast path, so we redact shorter sk-proj- strings here.
    (re.compile(r"sk-proj-[A-Za-z0-9_-]{10,}"), "sk-proj-***"),
    # OpenAI generic API keys (sk-…); minimum 20 chars to avoid false positives
    (re.compile(r"sk-[A-Za-z0-9_-]{20,}"), "sk-***"),
    # AWS IAM access keys (AKIA + exactly 16 uppercase alphanumeric)
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AKIA***"),
    # Stripe API keys (sk_live_, sk_test_, rk_live_) and webhook signing secrets (whsec_)
    (re.compile(r"sk_live_[A-Za-z0-9_-]+"), "sk_live_***"),
    (re.compile(r"sk_test_[A-Za-z0-9_-]+"), "sk_test_***"),
    (re.compile(r"rk_live_[A-Za-z0-9_-]+"), "rk_live_***"),
    (re.compile(r"whsec_[A-Za-z0-9_-]+"), "whsec_***"),
    # Database connection strings with embedded user:password (including driver-qualified forms)
    (re.compile(r"postgresql(?:\+\w+)?://[^:/@]+:[^@]+@"), "postgresql://***:***@"),
    (re.compile(r"postgres://[^:/@]+:[^@]+@"), "postgres://***:***@"),
    # Google AIza API keys (AIza + 20+ alphanumeric — open-ended to catch truncated or over-length keys)
    (re.compile(r"AIza[A-Za-z0-9_-]{20,}"), "AIza***"),
    # Google OAuth access tokens (ya29.…)
    (re.compile(r"ya29\.[A-Za-z0-9_.\-]+"), "ya29***"),
    # JWT tokens: three base64url-encoded parts separated by dots
    (
        re.compile(r"eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"),
        "eyJ***",
    ),
    # Bearer tokens
    (re.compile(r"Bearer\s+[^\s,'\"]+", re.IGNORECASE), "Bearer ***"),
    # OAuth token query parameters
    (re.compile(r"oauth_token[=:]\s*[^\s&'\"]+", re.IGNORECASE), "oauth_token=***"),
    (
        re.compile(
            r"([\"']?\b(?:AGENT_SECRET_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|OPENAI_API_KEY|CODEX_API_KEY|GEMINI_API_KEY|ZAI_API_KEY|DEEPSEEK_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|X-Agent-Token)\b[\"']?\s*[:=]\s*)(?!['\"]?AIza\*\*\*)(?:(['\"])(?:[^'\"\\]|\\.)+\2|[^\s,'\";&\])\n]{4,})",
            re.IGNORECASE,
        ),
        r"\1***",
    ),
    # PEM private keys (multiline)
    (
        re.compile(
            r"-----BEGIN[A-Z\s]+PRIVATE\s+KEY-----[\s\S]*?-----END[A-Z\s]+PRIVATE\s+KEY-----"
        ),
        "***PRIVATE KEY***",
    ),
)


def _redact_credentials(text: str) -> str:
    """Redact known credential patterns from *text* before storage.

    Covers GitHub tokens, Anthropic/OpenAI/Google API keys, AWS IAM access keys
    (AKIA…), Stripe API keys (sk_live_, sk_test_, rk_live_) and webhook secrets
    (whsec_), database connection strings with embedded passwords
    (postgresql:// and postgres://), JWTs, Bearer and OAuth tokens, PEM private
    keys, and named credential fields in JSON/key-value pairs (access_token,
    refresh_token, id_token, api_key, client_secret, private_key, token).
    Preserves field names; replaces values with ``***``.
    """
    if not text:
        return text
    for pattern, replacement in _CRED_REDACT_SUBS:
        text = pattern.sub(replacement, text)
    # Named credential fields — applied last so the specific prefix patterns
    # above run first and their redacted placeholders (which are short and
    # contain '*') are not re-matched by the alphanumeric value pattern.
    text = _CRED_FIELD_RE.sub(r"\1***\3", text)
    return text


def sanitize_agent_error_report(raw: object) -> dict | None:
    """Validate and bound an agent-posted structured error report for storage.

    Whitelists known keys and clamps string/list sizes so a malformed or
    oversized report can neither bloat task.context nor smuggle unexpected
    structure into classification. Returns None when the payload is not a dict
    (callers then behave exactly as if no report was sent).
    """
    if not isinstance(raw, dict):
        return None
    out: dict = {}
    schema = raw.get("schema")
    out["schema"] = (
        schema if isinstance(schema, int) and not isinstance(schema, bool) else 1
    )
    for key, limit in (
        ("provider", 50),
        ("auth_mode", 50),
        ("exit_signal", 20),
        ("last_stream_event", 100),
        ("classified", 50),
    ):
        val = raw.get(key)
        if isinstance(val, str) and val:
            out[key] = val[:limit]
    exit_code = raw.get("exit_code")
    out["exit_code"] = (
        exit_code
        if isinstance(exit_code, int) and not isinstance(exit_code, bool)
        else None
    )
    duration = raw.get("duration_ms")
    if (
        isinstance(duration, (int, float))
        and not isinstance(duration, bool)
        and duration >= 0
    ):
        out["duration_ms"] = int(duration)
    out["model_output_seen"] = bool(raw.get("model_output_seen"))
    out["result_event_seen"] = bool(raw.get("result_event_seen"))
    stderr_tail = raw.get("stderr_tail")
    if isinstance(stderr_tail, str) and stderr_tail:
        out["stderr_tail"] = _redact_credentials(
            stderr_tail[-_ERROR_REPORT_STDERR_TAIL_CHARS:]
        )
    provider_errors: list[dict] = []
    raw_errors = raw.get("provider_errors")
    if isinstance(raw_errors, list):
        for entry in raw_errors[:_ERROR_REPORT_MAX_PROVIDER_ERRORS]:
            if not isinstance(entry, dict):
                continue
            message = entry.get("message")
            if not isinstance(message, str) or not message:
                continue
            cleaned: dict = {
                "message": _redact_credentials(
                    message[:_ERROR_REPORT_ERROR_MESSAGE_CHARS]
                )
            }
            source = entry.get("source")
            if isinstance(source, str) and source:
                cleaned["source"] = source[:50]
            code = entry.get("code")
            if isinstance(code, str) and code:
                cleaned["code"] = code[:100]
            provider_errors.append(cleaned)
    out["provider_errors"] = provider_errors
    return out


def get_agent_error_report(task: "Task") -> dict | None:
    """Return the persisted structured agent error report, or None.

    Guarded so partially-loaded entities (load_only / expired attributes on
    analytics paths) degrade to "no report" instead of raising."""
    try:
        ctx = task.context or {}
    except Exception:
        return None
    report = ctx.get(AGENT_ERROR_REPORT_CONTEXT_KEY)
    return report if isinstance(report, dict) else None


def _error_report_provider_death_signal(report: dict) -> str | None:
    """Return an inferred-from label when the structured report proves the
    provider CLI run died without completing (a provider-layer death), or None
    when the report shows a completed run / is unreadable.

    Provider-agnostic by construction — every runner feeds the same run-shape
    fields, so no per-provider patterns are needed here:
      • non-zero exit + the model never produced output → the CLI died on/before
        its first API call (the incident signature). Can never be a
        code-authoring defect: no code was written by this run.
      • non-zero exit + structured provider error events + no terminal result
        event → the provider aborted the run mid-flight.
      • non-zero exit + model output but no terminal result event → the stream
        was severed mid-write (mid-run usage death). Mirrors
        _cli_stream_cut_tail, from truthful run-shape instead of text.
    A clean (zero) exit — or a non-zero exit whose run still reached its
    terminal result event — is NOT a provider death; the failure belongs to
    the work itself and specific labels / CODE_ERROR handling apply.
    """
    try:
        exit_code = report.get("exit_code")
        if exit_code == 0:
            return None
        model_output_seen = bool(report.get("model_output_seen"))
        result_event_seen = bool(report.get("result_event_seen"))
        provider_errors = report.get("provider_errors")
        has_provider_errors = (
            isinstance(provider_errors, list) and len(provider_errors) > 0
        )
        if not model_output_seen:
            return "error_report_startup_death"
        if result_event_seen:
            return None
        if has_provider_errors:
            return "error_report_provider_error"
        return "error_report_stream_cut"
    except Exception:
        return None


def _task_dead_auth_patterns(task: "Task") -> tuple[str, ...]:
    """Return provider dead-auth patterns for the task, or () on any failure."""
    from app.providers.registry import get_provider

    provider, auth_mode = _resolve_task_provider_auth(task)
    if provider is None or auth_mode is None:
        return ()
    try:
        return get_provider(provider).dead_auth_patterns(auth_mode)
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] dead_auth_patterns failed for provider=%s auth_mode=%s",
            provider,
            auth_mode,
        )
        return ()


def _check_dead_auth_from_stderr(task: "Task", report: dict) -> str | None:
    """Return the matched dead-auth pattern from the report's stderr_tail, or None.

    Only fires when the report shows startup-death (no model output) — a CLI
    that produced model output before dying authenticated successfully, so its
    death can't be a dead OAuth session.
    """
    try:
        if bool(report.get("model_output_seen")):
            return None
        stderr_tail = report.get("stderr_tail")
        if not isinstance(stderr_tail, str) or not stderr_tail:
            return None
        patterns = _task_dead_auth_patterns(task)
        if not patterns:
            return None
        lowered = stderr_tail.lower()
        for pattern in patterns:
            if pattern and pattern in lowered:
                return pattern
        return None
    except Exception:
        return None


def _persist_dead_auth_context(task: "Task", matched_pattern: str) -> None:
    """Persist dead-auth classification metadata onto task.context."""
    try:
        ctx = dict(task.context or {})
        ctx["dead_auth_matched_pattern"] = matched_pattern
        task.context = ctx
    except Exception:
        logger.exception("[FAILURE_CLASS] Failed to persist dead_auth context")


def _persist_operational_reclassification_context(
    task: "Task", inferred_from: str
) -> None:
    """Record a non-quota provider/runtime inference without rate-limit keys."""
    try:
        ctx = dict(task.context or {})
        ctx["provider_failure_inferred_from"] = inferred_from
        # Remove breadcrumbs from an older attempt. Their presence must not
        # make a runtime/config failure look eligible for account cooldown.
        ctx.pop("rate_limit_correlation_reclassified", None)
        ctx.pop("rate_limit_inferred_from", None)
        task.context = ctx
    except Exception:
        logger.exception(
            "[FAILURE_CLASS] Failed to persist operational reclassification marker"
        )


def _error_report_text(report: dict) -> str:
    parts: list[str] = []
    stderr_tail = report.get("stderr_tail")
    if isinstance(stderr_tail, str):
        parts.append(stderr_tail)
    provider_errors = report.get("provider_errors")
    if isinstance(provider_errors, list):
        for entry in provider_errors:
            if not isinstance(entry, dict):
                continue
            for key in ("code", "message"):
                value = entry.get(key)
                if isinstance(value, str):
                    parts.append(value)
    return " ".join(parts).lower()


def _agent_error_report_reclass(task: "Task") -> str | None:
    """Consume the structured agent error report as the primary signal for a
    generic CODE_ERROR-labelled failure.

    Only explicit provider quota evidence returns RATE_LIMIT. A non-zero exit
    before model output is merely a run-shape fact: it can be a missing native
    package, unsupported model, bad auth, or provider quota. Treating that shape
    itself as quota created false account cooldowns and retry storms.

    Deterministic runtime startup failures return PROVISION_FAILURE; model/API
    compatibility rejections return EXTERNAL_API; unexplained startup deaths
    return PROVISION_FAILURE; unexplained mid-stream cuts return
    NO_PROGRESS_TIMEOUT. Returns None for completed/readable runs."""
    report = get_agent_error_report(task)
    if report is None:
        return None
    signal = _error_report_provider_death_signal(report)
    if signal is None:
        return None
    report_text = _error_report_text(report)
    if _runtime_unavailable(report_text):
        _persist_operational_reclassification_context(task, "runtime_unavailable")
        return PROVISION_FAILURE
    if _provider_model_config_rejection(report_text):
        _persist_operational_reclassification_context(task, "provider_model_config")
        return EXTERNAL_API
    # Dead-auth check before rate-limit — a dead OAuth session is permanent
    # (operator must re-auth); a rate limit is transient. Misclassifying
    # permanent as transient burns attempts on an account that keeps dying.
    # Only startup-death can be dead-auth (model never produced output means
    # the CLI never passed auth; stream-cuts and provider-error signals happen
    # AFTER auth succeeds).
    if signal == "error_report_startup_death":
        dead_auth_pattern = _check_dead_auth_from_stderr(task, report)
        if dead_auth_pattern:
            _persist_dead_auth_context(task, dead_auth_pattern)
            return DEAD_AUTH
    text_override = _text_provider_failure_override(
        task, report_text, structured_provider_error=True
    )
    if text_override:
        return text_override
    if "overloaded" in report_text:
        _persist_operational_reclassification_context(task, "provider_overloaded")
        return EXTERNAL_API
    _persist_operational_reclassification_context(task, signal)
    if signal == "error_report_startup_death":
        return PROVISION_FAILURE
    return NO_PROGRESS_TIMEOUT


# The agent wrapper embeds the last 10KB of Claude CLI output in the ERROR_LOG
# after this marker (agent/src/handlers/code-change.ts). The tail is the only
# forensic record of HOW the CLI died, so the detectors below scope their
# pattern checks to it rather than the whole combined text.
_CLI_OUTPUT_TAIL_MARKER = "--- claude output tail ---"

# Stream-json markers proving the model produced real output in the tail.
_TAIL_MODEL_OUTPUT_MARKERS = (
    '"type":"assistant"',
    '"type": "assistant"',
    "content_block_start",
    '"tool_use"',
)


def _cli_output_tail(combined_lower: str) -> str | None:
    """Return the captured CLI output tail (lowercased), or None if absent."""
    idx = combined_lower.rfind(_CLI_OUTPUT_TAIL_MARKER)
    if idx < 0:
        return None
    return combined_lower[idx + len(_CLI_OUTPUT_TAIL_MARKER) :]


def _cli_startup_death_tail(combined_lower: str) -> bool:
    """True when the CLI died at startup: the init banner is in the output
    tail but no assistant/tool output ever followed.

    A Claude CLI that prints its init system event and then exits (typically
    ~2s in, exit code 1) died on its first API call — usage exhaustion, auth
    rejection, or provider error — before the model produced a single token.
    That can never be a code-authoring defect. Incident 2026-06-06: this
    signature burned full 3-attempt budgets across multiple lineages while
    classified CODE_ERROR."""
    tail = _cli_output_tail(combined_lower)
    if tail is None:
        return False
    if '"subtype":"init"' not in tail and '"subtype": "init"' not in tail:
        return False
    return not any(m in tail for m in _TAIL_MODEL_OUTPUT_MARKERS)


def _cli_stream_cut_tail(combined_lower: str) -> bool:
    """True when the CLI died mid-stream: content deltas present in the output
    tail but no terminal ``result`` event ever arrived.

    A normal run — even a failed one — ends with a stream-json
    ``{"type":"result",...}`` event inside the captured 10KB tail. Raw
    content_block/signature deltas as the final output mean the provider
    stream was severed mid-write (mid-run usage death), not that the agent
    wrote bad code."""
    tail = _cli_output_tail(combined_lower)
    if tail is None:
        return False
    has_delta = "content_block_delta" in tail or '"signature_delta"' in tail
    has_result = '"type":"result"' in tail or '"type": "result"' in tail
    return has_delta and not has_result


# Execution-stall text signatures. Combined with a stuck (0-output-token) LLM
# call, these indicate a silent provider hang (the agent's 45-min
# ClaudeTimeoutError, or a mid-stream tool_use stall) rather than a cancellation,
# heartbeat loss, VM death, or OOM — all of which can also show 0 output tokens
# but are NOT rate-limits. Used to scope the stuck-call→RATE_LIMIT
# reclassification narrowly on the agent-reported (classifier) path.
_EXECUTION_STALL_SIGNATURES = (
    "time limit",
    "execution timed out",
    "tool in-flight",
    "tool-use stall",
    "tool_use stall",
)


def _text_provider_failure_override(
    task: "Task",
    combined: str,
    *,
    structured_provider_error: bool = False,
) -> str | None:
    """Return a strong provider/API class from text, or None.

    Used only to override generic agent labels like "fatal"; precise agent
    categories such as test_failed/lint_failed remain authoritative.
    """
    if _runtime_unavailable(combined):
        return PROVISION_FAILURE

    if _provider_model_config_rejection(combined):
        return EXTERNAL_API

    # A generic agent label can wrap GitHub/backend traffic too. Exclude those
    # sources before any LLM throttle phrase; only a structured provider error
    # may use the cross-provider phrase bank directly.
    if any(
        marker in combined
        for marker in (
            "github",
            "api.github.com",
            "internal api",
            "dispatch api",
            "blueprint api",
            "/api/agent",
        )
    ) and any(
        marker in combined
        for marker in ("429", "too many requests", "rate limit")
    ):
        return EXTERNAL_API

    if structured_provider_error and _explicit_provider_rate_limit_evidence(combined):
        return RATE_LIMIT

    signal = _task_rate_limit_signal(task, combined)
    if signal is not None:
        _persist_rate_limit_signal_context(task, signal)
        return RATE_LIMIT

    if any(kw in combined for kw in _task_rate_limit_patterns(task)):
        return RATE_LIMIT

    return None


# Explicit phrase match so status messages like
# "No heartbeat received after 300s since provisioning" (which aren't
# provision failures — they're heartbeat/startup timeouts) don't get
# misclassified just because they contain the substring "provision".
# The pattern matches the verbs/nouns that clearly mean "provisioning broke"
# ("failed to provision", "provisioning failed", "provision error", "VM provisioning …")
# while rejecting benign references like "since provisioning".
_PROVISION_PHRASE_RE = re.compile(
    r"\b("
    r"failed to provision"
    r"|provision(?:ing)? failed"
    r"|provision(?:ing)? error"
    r"|vm provisioning"
    r")\b",
    re.IGNORECASE,
)


_SEVERITY: dict[str, int] = {
    MIGRATION_ERROR: 3,
    CODE_ERROR: 3,
    VIEWPORT_REGRESSION: 3,
    AGENT_TIMEOUT: 2,
    AGENT_STALLED: 2,
    PROVISION_FAILURE: 1,
    EXTERNAL_API: 1,
    RATE_LIMIT: 1,
    SCOPE_OVERFLOW: 1,
    SCOPE_EMPTY: 1,
    # A routing-contract failure, not a code defect — ranked with infra classes
    # so worst_child_failure_category never escalates it to CODE_ERROR.
    UNPLACEABLE_EXECUTION_SITE: 1,
}


def worst_child_failure_category(children: list["Task"]) -> str:
    """Return the most severe failure category among a list of failed child tasks.

    Severity ranking: CODE_ERROR (3) > AGENT_TIMEOUT (2) > infra classes (1).
    Unclassified children and unknown categories are treated as CODE_ERROR.
    Falls back to CODE_ERROR if the list is empty.
    """
    best_rank = 0
    best_cat = CODE_ERROR
    for child in children:
        raw_cat = (child.context or {}).get("failure_category") or CODE_ERROR
        cat = raw_cat if raw_cat in _SEVERITY else CODE_ERROR
        rank = _SEVERITY[cat]
        if rank > best_rank:
            best_rank = rank
            best_cat = cat
    return best_cat


def persist_failure_category(
    task: "Task",
    failure_class: str,
    logs: str | None = None,
    confidence: float | None = None,
) -> None:
    """Write ``failure_class`` onto ``task.context['failure_category']`` in place.

    All terminal-FAILED code paths must call this (or write the key themselves)
    so direction rollups and research dedup can tell infra from code failures.
    For AGENT_TIMEOUT, also persists ``timeout_subtype`` (computed from the
    task's status message plus optional ``logs``) so downstream systems —
    direction_outcomes, scope calibration, analytics — can correlate subtype
    with retry outcomes without recomputing from the (possibly overwritten)
    status message later.
    """
    ctx = dict(task.context or {})
    ctx["failure_category"] = failure_class
    if failure_class == AGENT_TIMEOUT:
        status_msg = task.latest_status_message or ""
        ctx["timeout_subtype"] = _timeout_subtype_from_message(
            status_msg + " " + (logs or "")
        )
    if failure_class == EXTERNAL_API:
        status_msg = task.latest_status_message or ""
        ctx["external_api_subtype"] = _external_api_subtype_from_message(
            status_msg + " " + (logs or "")
        )
    task.context = ctx
    account_id = ctx.get("llm_account_id") or ctx.get("max_plan_account_id")
    if account_id:
        record_account_failure_class(
            str(account_id), failure_class, confidence=confidence
        )


def normalize_agent_failure_category(
    raw: str | None,
    task: "Task",
    *,
    at_time: "datetime | None" = None,
    db_rl_override: bool = False,
) -> str:
    """Canonicalize an agent-reported ``failure_category`` label to an uppercase class name.

    The agent posts lowercase short labels (``rate_limited``, ``claude_code_failed``, …)
    but every consumer (rollups, dedup, analytics) compares against the uppercase
    canonical names. Normalize at the write boundary so the rest of the system sees
    a single vocabulary.

    at_time: reference timestamp for rate-limit cooldown checks. Pass
    ``task.last_heartbeat`` (the failure instant) so a lag between failure and
    report time cannot cause a now-elapsed cooldown to be missed.

    db_rl_override: pre-computed result of a durable DB rate-limit check. Pass
    True when the caller has already confirmed (via db_account_was_rate_limited)
    that the account was in cooldown at at_time, even if the in-memory pool
    shows no cooldown (cross-instance case). Defaults to False (no override).
    """
    if not raw:
        return classify_failure(task, at_time=at_time, db_rl_override=db_rl_override)
    label = raw.strip()
    label_lower = label.lower()
    mapped = _AGENT_CATEGORY_TO_CLASS.get(label_lower)
    if mapped:
        if mapped == CODE_ERROR and label_lower in _ALL_GENERIC_CODE_ERROR_LABELS:
            # The task's own payload is checked FIRST. A payload that violates
            # its declared type contract is unrunnable no matter what else the
            # attempt hit: no provider rotation, backoff or retry can clear it,
            # and blaming the agent for a spawner's missing required field
            # poisons failure-attribution and erosion scoring. A PRECISE agent
            # label (test_failed / lint_failed / ...) is a real verdict about
            # the work and never reaches this branch, so this cannot overwrite
            # one.
            _malformed_payload = malformed_task_payload_reason(task)
            if _malformed_payload:
                return MALFORMED_TASK_PAYLOAD
            # Structured agent error report — the PRIMARY signal. A report
            # proving a provider-layer death (CLI died without completing a
            # model run) routes to the RATE_LIMIT requeue path (account
            # rotation, no retry burn) before any text heuristic runs.
            report_class = _agent_error_report_reclass(task)
            if report_class:
                return report_class
            combined = (task.latest_status_message or "").lower()
            override = _text_provider_failure_override(task, combined)
            if override:
                return override
            if _trusted_execution_capability_unavailable(task):
                return PROVISION_FAILURE
            if any(kw in combined for kw in _VIEWPORT_INFRA_UNAVAILABLE_PATTERNS):
                return PROVISION_FAILURE
            # Deployment check tasks (smoketest): a generic code error is an
            # infrastructure condition (stale engine, deployment deadlock),
            # not a code defect in the candidate.
            if _is_deployment_check_task(task):
                return PROVISION_FAILURE
        return mapped
    if label in INFRA_CLASSES or label == CODE_ERROR:
        return label
    logger.warning(
        "[FAILURE_CLASS] Unknown agent failure_category=%r on task %s; falling back to classify_failure",
        raw,
        getattr(task, "id", "?"),
    )
    return classify_failure(task, at_time=at_time, db_rl_override=db_rl_override)


# AGENT_TIMEOUT sub-types — finer breakdown of the catch-all timeout class so
# digest consumers can tell a heartbeat loss from an OOM from a cascade cancel.
# Keywords mirror the same phrases classify_failure() uses to route to AGENT_TIMEOUT.
TIMEOUT_HEARTBEAT_LOST = "heartbeat_lost"
TIMEOUT_OOM = "oom"
TIMEOUT_EXECUTION = "execution_timeout"
TIMEOUT_CASCADE = "cascade_cancellation"
TIMEOUT_ITERATION_LIMIT = "iteration_limit"
TIMEOUT_PROCESS_CRASH = "process_crash"
TIMEOUT_UNKNOWN = "unknown"


def _timeout_subtype_from_message(combined: str) -> str:
    """Route a combined status+logs string to an AGENT_TIMEOUT sub-type label.

    Order matters: cascade/supersession and iteration caps are checked before
    heartbeat/OOM keywords because a cascade-cancelled task may transit through
    any state and its status message is the authoritative signal.
    """
    combined = (combined or "").lower()
    stripped = combined.lstrip()

    if stripped.startswith("cancelled:") or "superseded" in combined:
        return TIMEOUT_CASCADE
    if any(
        kw in combined
        for kw in (
            "iteration cap",
            "review not spawned",
            "fix not spawned",
            "review-fix cap reached",
            "review-fix cycle limit",
            "ci wait exceeded",
            "ci wait timed out",
        )
    ):
        return TIMEOUT_ITERATION_LIMIT
    if any(
        kw in combined
        for kw in (
            "out of memory",
            "oom-kill",
            "memory limit",
            "cannot allocate",
            "cgroup",
            "exit code 137",
            "oom killer",
        )
    ):
        return TIMEOUT_OOM
    if any(kw in combined for kw in ("no heartbeat", "reaped", "never started")):
        return TIMEOUT_HEARTBEAT_LOST
    if any(
        kw in combined
        for kw in ("[fatal:", "fatal error", "process crashed", "node process killed")
    ):
        return TIMEOUT_PROCESS_CRASH
    if any(kw in combined for kw in ("execution timed out", "sigterm")):
        return TIMEOUT_EXECUTION
    return TIMEOUT_UNKNOWN


def classify_timeout_subtype(task: "Task", logs: str | None = None) -> str:
    """Return a sub-type label for an AGENT_TIMEOUT failure."""
    status_msg = task.latest_status_message or ""
    return _timeout_subtype_from_message(status_msg + " " + (logs or ""))


# AGENT_TIMEOUT sub-types that indicate VM-layer deaths (as opposed to code-level
# cascade/iteration cancellations). Used by classify_vm_exit_reason() and exported
# so callers can build predicates without re-enumerating the set.
VM_LAYER_TIMEOUT_SUBTYPES: frozenset[str] = frozenset(
    {TIMEOUT_HEARTBEAT_LOST, TIMEOUT_PROCESS_CRASH, TIMEOUT_EXECUTION, TIMEOUT_UNKNOWN}
)

# VM-layer exit reasons used by the agent_vm_resource_exhaustion dispatcher signal.
# Exported so both failure_classification and pipeline_regression_dispatcher agree
# on the vocabulary without duplication.
VM_RESOURCE_EXIT_REASONS: frozenset[str] = frozenset(
    {"oom", "timeout", "spot_preempt", "vm_provisioning_failure"}
)


def classify_vm_exit_reason(task: "Task") -> str | None:
    """Return the VM-layer exit reason for a failed task, or None.

    Maps task context fields (failure_category, timeout_subtype, spot_preemption_count)
    to the canonical exit-reason strings the agent_vm_resource_exhaustion signal uses.
    Returns None when the failure is code-level or a cascade/iteration cancellation.
    """
    ctx = task.context or {}
    failure_category = ctx.get("failure_category", "")
    timeout_subtype = ctx.get("timeout_subtype", "")
    spot_count = int(ctx.get("spot_preemption_count") or 0)

    if failure_category == PROVISION_FAILURE:
        return "vm_provisioning_failure"
    if spot_count > 0:
        return "spot_preempt"
    if failure_category == AGENT_TIMEOUT:
        if timeout_subtype == TIMEOUT_OOM:
            return "oom"
        if timeout_subtype in VM_LAYER_TIMEOUT_SUBTYPES:
            return "timeout"
    return None


# EXTERNAL_API sub-types — finer breakdown so auto-remediation can apply
# tailored recovery patterns per failure mode.
EXTERNAL_API_SUBTYPE_PROVIDER_AUTH = "provider_auth"
EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA = "provider_quota"
EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG = "provider_model_config"
EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE = "github_pr_create"
EXTERNAL_API_SUBTYPE_GITHUB_RATE_LIMIT = "github_rate_limit"
EXTERNAL_API_SUBTYPE_INTERNAL_RATE_LIMIT = "internal_rate_limit"
EXTERNAL_API_SUBTYPE_PROVIDER_OVERLOAD = "provider_overload"
EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY = "db_connectivity"
EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT = "network_transient"
EXTERNAL_API_SUBTYPE_OTHER = "other"


def _external_api_subtype_from_message(combined: str) -> str:
    """Route a combined status+logs string to an EXTERNAL_API sub-type label.

    Order matters: specific failure signatures are checked before generic ones.
    """
    combined = (combined or "").lower()

    # Database connectivity — checked first because psycopg/asyncpg errors can
    # contain generic "connection" substrings that would otherwise match network_transient.
    if any(
        kw in combined
        for kw in (
            "could not connect to server",
            "connection to server",
            "database connection",
            "psycopg",
            "asyncpg",
            "operator does not exist",
        )
    ):
        return EXTERNAL_API_SUBTYPE_DB_CONNECTIVITY

    if _provider_model_config_rejection(combined):
        return EXTERNAL_API_SUBTYPE_PROVIDER_MODEL_CONFIG

    rate_markers = ("rate limit", "429", "too many requests")
    if any(marker in combined for marker in ("github", "api.github.com")) and any(
        marker in combined for marker in rate_markers
    ):
        return EXTERNAL_API_SUBTYPE_GITHUB_RATE_LIMIT
    if any(
        marker in combined
        for marker in (
            "internal api",
            "dispatch api",
            "blueprint api",
            "/api/agent",
            "/api/runner",
        )
    ) and any(marker in combined for marker in rate_markers):
        return EXTERNAL_API_SUBTYPE_INTERNAL_RATE_LIMIT
    if "overloaded" in combined:
        return EXTERNAL_API_SUBTYPE_PROVIDER_OVERLOAD

    # Provider quota / rate exhaustion / capacity (non-auth upstream limits).
    # Check this before generic auth because some OpenAI-compatible gateways pair
    # 401-ish wording with balance/quota text; those should cooldown/retry rather
    # than hard-disable a still-valid key.
    if any(
        kw in combined
        for kw in (
            "rate limit",
            "429",
            "too many requests",
            "context_length_exceeded",
            "token limit exceeded",
            "quota exceeded",
            "insufficient quota",
            "insufficient_quota",
            "insufficient balance",
            "insufficient_balance",
            "balance not enough",
            "api usage exhausted",
            "usage_not_included",
            "provider capacity",
            "service capacity",
            "server capacity",
            "at capacity",
            "capacity exceeded",
            "concurrency limit",
            "resource exhausted",
        )
    ):
        return EXTERNAL_API_SUBTYPE_PROVIDER_QUOTA

    # Provider auth — token/credential failures from any upstream API.
    # `oauth_org_not_allowed` is the Anthropic 403 raised when an org has
    # disabled OAuth for Claude Code; matched by its specific token (NOT bare
    # "403", which collides with GitHub/FastAPI 403s) so it routes to infra/auth
    # instead of burning task retries as CODE_ERROR.
    if any(
        kw in combined
        for kw in (
            # Anchored "http 401" instead of bare "401" so benign lines that
            # merely contain the digits 401 (timestamps, counts, ids) don't get
            # routed to provider_auth. `combined` is lowercased so this also
            # matches "HTTP 401". Genuine auth failures still carry
            # "unauthorized"/"bad credentials"/"token …" below.
            "http 401",
            "unauthorized",
            "bad credentials",
            "token expired",
            "token invalid",
            "token revoked",
            "authentication_failed",
            "authentication failed",
            "failed to authenticate",
            "invalid authentication credentials",
            "invalid api key",
            "oauth_org_not_allowed",
        )
    ):
        return EXTERNAL_API_SUBTYPE_PROVIDER_AUTH

    # GitHub PR creation / merge-block failures.
    if ("failed to create" in combined and "branch" in combined) or any(
        kw in combined
        for kw in (
            "failed to create pull request",
            "rollup pr creation failed",
            "merge blocked",
            "merge attempt",
            "merge conflicts",
            "merge failed",
            "merge hard-cap",
            "merge hard cap",
            "merge check failed",
            "not mergeable",
            "repository rule",
            "rule violations",
            "required status check",
            "branch protection",
        )
    ):
        return EXTERNAL_API_SUBTYPE_GITHUB_PR_CREATE

    # Transient network / TLS / DNS errors.
    if any(
        kw in combined
        for kw in (
            "connection timed out",
            "name resolution failed",
            # Anchored "ssl handshake failed" instead of bare "ssl" so benign
            # SSL/TLS mentions (a module path containing "openssl", "ssl context
            # reused", etc.) don't get routed to network_transient. Real TLS
            # failures still carry this phrase or "certificate" below.
            "ssl handshake failed",
            "certificate",
            "could not resolve hostname",
            "name or service not known",
            "network unreachable",
            "500 internal server error",
            "502 bad gateway",
            "503 service unavailable",
        )
    ):
        return EXTERNAL_API_SUBTYPE_NETWORK_TRANSIENT

    return EXTERNAL_API_SUBTYPE_OTHER


def classify_external_api_subtype(task: "Task", logs: str | None = None) -> str:
    """Return a sub-type label for an EXTERNAL_API failure."""
    status_msg = task.latest_status_message or ""
    combined = status_msg + " " + (logs or "")
    return _external_api_subtype_from_message(combined)


def classify_failure_with_confidence(
    task: "Task",
    logs: str | None = None,
    at_time: "datetime | None" = None,
    db_rl_override: bool = False,
) -> FailureClassification:
    """Classify a failure and return a cheap rule-derived confidence score."""
    failure_class = _classify_failure_class(
        task, logs, at_time=at_time, db_rl_override=db_rl_override
    )
    agent_category = (task.context or {}).get("failure_category")
    combined = (
        (task.latest_status_message or "").lower() + " " + (logs or "").lower()
    ).strip()
    combined_lower = combined.lower()
    # A zero-output execution stall is operational, but is not evidence that
    # the assigned account was throttled. Route through the budget-exempt
    # no-progress path without mutating account cooldown state.
    # Scoped to execution-stall signatures so cancellations / heartbeat-loss /
    # OOM (which can also show 0 output) are NOT swept in. Runs before the
    # agent_category early-exit so the agent's own 'fatal' label can't mask it.
    # Reaper-reaped agent_stalled stalls are handled directly in reaper.py.
    # See incident 2026-05-29.
    if (
        failure_class == AGENT_TIMEOUT
        and _stuck_llm_call_no_progress(task)
        and any(sig in combined_lower for sig in _EXECUTION_STALL_SIGNATURES)
    ):
        _persist_operational_reclassification_context(task, "stuck_llm_call_no_output")
        return FailureClassification(
            failure_class=NO_PROGRESS_TIMEOUT, confidence=0.7
        )
    # CLI-death reclassification for CODE_ERROR. A Claude CLI process that died
    # without the model ever producing output (init-banner-only tail,
    # 0-output-token telemetry) or that was severed mid-stream (delta events
    # with no terminal result) is an infra/provider death — usage exhaustion,
    # auth rejection, or a cut stream — not an agent code defect. The wrapper
    # can only label these with generic categories (claude_code_failed /
    # incomplete / fatal), and CODE_ERROR retries burn the attempt budget with
    # no account blacklist or backoff; RATE_LIMIT routes them to the canonical
    # cooldown + blacklist + budget-free requeue instead. Scoped to the generic
    # labels so specific code-error signals (test/lint/merge text) are never
    # swept. Runs before the agent_category early-exit for the same reason as
    # the AGENT_TIMEOUT rule above. Incident 2026-06-06: 24 infra deaths burned
    # 3-attempt budgets across 8 lineages overnight as CODE_ERROR.
    if (
        failure_class == CODE_ERROR
        and str(agent_category or "").lower() in _ALL_GENERIC_CODE_ERROR_LABELS
    ):
        # Structured reports are interpreted by the same evidence-based helper
        # as the write-boundary path. Run shape alone never means quota.
        report = get_agent_error_report(task)
        if report is not None:
            report_class = _agent_error_report_reclass(task)
            if report_class:
                return FailureClassification(
                    failure_class=report_class, confidence=0.9
                )
        else:
            cli_death_signal = (
                "stuck_llm_call_no_output"
                if _stuck_llm_call_no_progress(task)
                else "cli_startup_death_init_only_tail"
                if _cli_startup_death_tail(combined_lower)
                else "cli_stream_cut_tail"
                if _cli_stream_cut_tail(combined_lower)
                else None
            )
            if cli_death_signal:
                _persist_operational_reclassification_context(task, cli_death_signal)
                fallback_class = (
                    PROVISION_FAILURE
                    if cli_death_signal == "cli_startup_death_init_only_tail"
                    else NO_PROGRESS_TIMEOUT
                )
                return FailureClassification(
                    failure_class=fallback_class, confidence=0.7
                )
    if agent_category:
        return FailureClassification(failure_class=failure_class, confidence=0.9)
    if failure_class == MIGRATION_ERROR:
        if any(kw in combined_lower for kw in _MIGRATION_PATTERNS):
            return FailureClassification(failure_class=failure_class, confidence=0.9)
        if "downgrade" in combined_lower and any(
            kw in combined_lower for kw in ("notimplementederror", "operationalerror")
        ):
            return FailureClassification(failure_class=failure_class, confidence=0.9)
        return FailureClassification(failure_class=failure_class, confidence=0.8)
    if failure_class == RATE_LIMIT:
        if _task_rate_limit_signal(task, combined_lower) is not None:
            return FailureClassification(failure_class=failure_class, confidence=0.8)
        if any(kw in combined_lower for kw in _task_rate_limit_patterns(task)):
            return FailureClassification(failure_class=failure_class, confidence=0.8)
    if failure_class == PROVISION_FAILURE and _PROVISION_PHRASE_RE.search(combined):
        return FailureClassification(failure_class=failure_class, confidence=0.8)
    if failure_class == EXTERNAL_API:
        if any(
            kw in combined_lower
            for kw in (
                "could not connect to server",
                "connection to server",
                "database connection",
                "psycopg",
                "asyncpg",
                "operator does not exist",
            )
        ):
            return FailureClassification(failure_class=failure_class, confidence=0.8)
        if _provider_model_config_rejection(combined_lower):
            return FailureClassification(failure_class=failure_class, confidence=0.8)
    if failure_class == AGENT_TIMEOUT and any(
        kw in combined_lower
        for kw in (
            "no heartbeat",
            "reaped",
            "never started",
            "cancelled:",
            "superseded",
        )
    ):
        return FailureClassification(failure_class=failure_class, confidence=0.7)
    if failure_class == BLOCKED_PRECONDITION:
        # The "BLOCKER:" prefix is a deliberate agent signal — match is high-signal.
        return FailureClassification(failure_class=failure_class, confidence=0.9)
    if failure_class == SCOPE_EMPTY:
        # The "SCOPE_EMPTY:" prefix is a deliberate agent signal — match is high-signal.
        return FailureClassification(failure_class=failure_class, confidence=0.9)
    if failure_class == CODE_ERROR:
        return FailureClassification(failure_class=failure_class, confidence=0.5)
    return FailureClassification(failure_class=failure_class, confidence=0.7)


def persist_authoritative_verdict(task: "Task", failure_class: str) -> None:
    """Persist the authoritative failure verdict to task.context['failure_class'].

    Call this from authoritative classification sites (reaper, agent_status) at
    the failure instant so analytics paths that invoke classify_failure() hours
    later short-circuit to the same verdict without re-running rate-limit
    correlation against post-cooldown account state.
    """
    ctx = dict(task.context or {})
    ctx["failure_class"] = failure_class
    task.context = ctx


def _classify_failure_class(
    task: "Task",
    logs: str | None = None,
    at_time: "datetime | None" = None,
    db_rl_override: bool = False,
) -> str:
    """Classify a task failure for deployment monitoring.
    Returns: CODE_ERROR, PROVISION_FAILURE, AGENT_TIMEOUT, EXTERNAL_API,
    RATE_LIMIT, SCOPE_OVERFLOW, MIGRATION_ERROR, BLOCKED_PRECONDITION."""

    # BLOCKER: / SCOPE_EMPTY: prefixes — deliberate agent signals. Checked BEFORE
    # the agent_category fast-path because the agent may emit ``agent_self_failed``
    # / generic ``CODE_ERROR`` alongside these status messages; the prefix is the
    # authoritative routing signal.
    _status_msg_raw = (task.latest_status_message or "").lstrip()
    if _status_msg_raw[:12].lower().startswith("scope_empty:"):
        return SCOPE_EMPTY
    if _status_msg_raw[:8].lower().startswith("blocker:"):
        return BLOCKED_PRECONDITION

    # Orchestrator-surfaced precondition blockers. Checked alongside the agent
    # prefixes (before the agent_category fast-path) so a precondition recognized
    # by the *orchestrator* is treated identically to one surfaced by the
    # *agent*: routed to a budget-exempt class instead of consuming a retry slot
    # / dinging direction health. See the marker definitions for why the matched
    # substrings are deliberately narrow.
    _status_msg_lower = _status_msg_raw.lower()
    if any(marker in _status_msg_lower for marker in _ORCHESTRATOR_DEFER_MARKERS):
        return BLOCKED_PRECONDITION

    # Prerequisite-escalation / co-defer cancellations — research_completion stamps
    # ``prerequisite_escalated_to_parent=True`` or ``CO_DEFERRED_SHELVED`` on a
    # CANCELLED child task when a finding's declared prerequisite has no live/deferred
    # provider this cycle, or when a dependent is co-deferred alongside its same-cycle-
    # deferred foundation. These are recoverable orchestration sequencing conditions
    # (the work re-surfaces in a later cycle) — NOT code defects. Checked before the
    # persisted-class short-circuit so they override any erroneously persisted
    # ``failure_class=CODE_ERROR`` written before this classification branch existed.
    _ctx_check = task.context if isinstance(task.context, dict) else {}
    if _ctx_check.get("prerequisite_escalated_to_parent"):
        return BLOCKED_PRECONDITION
    if _ctx_check.get(CO_DEFERRED_SHELVED):
        return BLOCKED_PRECONDITION

    # Authoritative LLM rate-limit signal from the Claude Code CLI: the agent
    # emits a structured ``{"type":"rate_limit_event", ...}`` line in its output
    # when the provider rate-limits the request. Must run before both the
    # persisted failure_class short-circuit and the agent_category fast path
    # because a downstream task handler (memo, audit, investigate, …) can
    # preempt-write its own CODE_ERROR verdict when the agent dies mid-stream
    # — e.g. the memo handler reports
    # ``exitCode=1, "couldn't extract JSON"`` because the agent never produced
    # the required output. Without this check, that generic CODE_ERROR verdict
    # takes the short-circuit / fast path → no account blacklist → retry hits
    # the same dead account →
    # 3 strikes → terminal FAIL. Incident 2026-05-24: 5 tasks terminal-failed
    # this way when 3 Claude accounts weekly-burned at once.
    _classify_combined_lower = (
        (task.latest_status_message or "").lower() + " " + (logs or "").lower()
    )
    if '"type":"rate_limit_event"' in _classify_combined_lower:
        return RATE_LIMIT

    # Clone-time branch-gone — the target branch was deleted (usually its PR
    # already merged) before the agent could clone it. High-signal git output, so
    # like the rate_limit_event check it runs BEFORE the persisted-verdict
    # short-circuit and the agent_category fast-path: the agent labels the failed
    # clone a generic "fatal" CODE_ERROR, and we must override that to a
    # budget-exempt, visible class so no debug/remediation spawns and no retry
    # slot is burned on a branch that will not reappear.
    # Checked before branch-gone: a commitless repo fails the clone with the same
    # "Remote branch ... not found in upstream origin" text as a deleted branch,
    # so branch-gone would swallow it and report a permanent setup gap as a
    # benign merged-PR race. The specific cause must win over the generic one.
    if _is_repo_uninitialized(_classify_combined_lower):
        return REPO_UNINITIALIZED

    if _is_branch_gone(_classify_combined_lower):
        return BRANCH_GONE

    # Belt-and-suspenders for the Codex `exitCode=0` mid-turn silent-death
    # failure mode (cause C, 2026-05-26). The Codex CLI (gpt-5.4-mini) emits
    # `{"type":"thread.started"}` + `{"type":"turn.started"}` to stdout, then
    # exits CLEAN before reaching `turn.completed`. This is an upstream
    # OpenAI-side disconnect (token cap / silent refusal / 5xx) that the CLI
    # swallows as success. The memo handler now reclassifies on its own
    # (see agent/src/handlers/memo.ts), but mirror the override here so any
    # other Codex consumer that forgets the agent-side fix still routes
    # through EXTERNAL_API (proper backoff) instead of CODE_ERROR (burns
    # retries). Bounded by len < 8KB because a real CLI death produces only
    # the Claude/Codex init banner — anything larger means actual work output
    # and the parse_failed/code_error label is probably correct.
    if (
        len(_classify_combined_lower) < 8192
        and '"type":"thread.started"' in _classify_combined_lower
        and '"type":"turn.completed"' not in _classify_combined_lower
        and "exitcode=0" in _classify_combined_lower
    ):
        return EXTERNAL_API

    # Short-circuit: if reaper/agent_status already persisted the authoritative
    # verdict at the failure instant via persist_authoritative_verdict(), return
    # it without re-running stateful rate-limit correlation. The structured
    # rate_limit_event check above is still safe to run here because it is a
    # deterministic signal embedded in the task output, not a cooldown lookup.
    persisted_class = (task.context or {}).get("failure_class")
    if isinstance(persisted_class, str) and persisted_class:
        return persisted_class

    # Fast path: agent already reported a structured category — prefer it over regex.
    # Accept both the legacy lowercase labels (pre-normalization writes) and the
    # canonical uppercase names written by normalize_agent_failure_category().
    agent_category = (task.context or {}).get("failure_category")
    if agent_category:
        agent_label = str(agent_category).strip()
        agent_lower = agent_label.lower()
        if agent_lower in _AGENT_CATEGORY_TO_CLASS:
            mapped = _AGENT_CATEGORY_TO_CLASS[agent_lower]
            if mapped == CODE_ERROR:
                # Structured agent error report — primary signal for generic
                # labels; a provider-layer death routes to RATE_LIMIT before
                # any text heuristic runs.
                if agent_lower in _ALL_GENERIC_CODE_ERROR_LABELS:
                    report_class = _agent_error_report_reclass(task)
                    if report_class:
                        return report_class
                    # Deployment check tasks (smoketest): a generic code
                    # error is an infrastructure condition (stale engine,
                    # deployment deadlock), not a code defect in candidate.
                    if _is_deployment_check_task(task):
                        return PROVISION_FAILURE
                combined = (
                    (task.latest_status_message or "").lower()
                    + " "
                    + (logs or "").lower()
                )
                if agent_lower in _GENERIC_AGENT_CODE_LABELS:
                    override = _text_provider_failure_override(task, combined)
                    if override:
                        return override
                if _trusted_execution_capability_unavailable(task, logs):
                    return PROVISION_FAILURE
                if any(kw in combined for kw in _VIEWPORT_INFRA_UNAVAILABLE_PATTERNS):
                    return PROVISION_FAILURE
                if any(kw in combined for kw in _VIEWPORT_PATTERNS):
                    return VIEWPORT_REGRESSION
            return mapped
        if agent_category in INFRA_CLASSES or agent_category == CODE_ERROR:
            return agent_category
        # Orchestrator-internal: task auto-failed after too many retry_after cycles
        # because all accounts were rate-limited. Root cause is RATE_LIMIT.
        if agent_category == "retry_after_exhausted":
            return RATE_LIMIT

    status_msg = (task.latest_status_message or "").lower()
    logs_lower = (logs or "").lower()
    combined = status_msg + " " + logs_lower

    # Empty/whitespace-only output is an agent timeout. Another task's account
    # cooldown is not evidence about this attempt and must not rewrite it as a
    # rate limit.
    if not combined.strip():
        return AGENT_TIMEOUT

    # Cascade cancellations and supersessions — administrative orchestrator decisions
    # that must be classified before ANY keyword-matching loop so that substrings
    # in the tail (e.g. "evaluation timeout" inside "Cancelled: parent evaluation
    # timeout") cannot override the administrative intent.
    if status_msg.startswith("cancelled:"):
        return ADMIN_CANCEL
    if "superseded" in status_msg:
        return ADMIN_CANCEL

    # Migration-specific failures — alembic chain/head errors, missing migration
    # column drift, schema-revision mismatches, downgrade failures. Checked before
    # the database-connection EXTERNAL_API bank because UndefinedColumn errors
    # surface via psycopg/asyncpg but indicate a missing migration, not a DB outage.
    if any(kw in combined for kw in _MIGRATION_PATTERNS):
        return MIGRATION_ERROR
    if "downgrade" in combined and any(
        kw in combined for kw in ("notimplementederror", "operationalerror")
    ):
        return MIGRATION_ERROR

    # Database / internal service unavailable — checked before provision failures because
    # PostgreSQL errors like "could not connect to server: Connection refused" contain
    # "connection refused" which would otherwise be misclassified as PROVISION_FAILURE.
    if any(
        kw in combined
        for kw in [
            "could not connect to server",
            "connection to server",
            "database connection",
            "psycopg",
            "asyncpg",
            "operator does not exist",
        ]
    ):
        return "EXTERNAL_API"

    # Local CLI/package readiness failures occur before a provider request.
    # Check them before any run-shape/report inference and never cool an LLM
    # account for a broken runner installation.
    if _runtime_unavailable(combined):
        return PROVISION_FAILURE

    # Provision failures — GCP quota, network, VM startup issues, SSH/init script errors, disk exhaustion.
    # "provision" is matched via _PROVISION_PHRASE_RE so benign mentions like
    # "No heartbeat received after 300s since provisioning" (heartbeat timeout, not
    # provision failure) don't get misclassified.
    #
    # NOTE: Bare "quota" used to be in this bank, but it's far too greedy —
    # Gemini/OpenAI/Anthropic quota-exhausted errors also contain "quota"
    # and were being misclassified as PROVISION_FAILURE, which bypasses
    # the rate-limit requeue path in agent_status.py (RATE_LIMIT/
    # EXTERNAL_API get no-retry-burn requeue; PROVISION_FAILURE burns
    # attempt_count). Result: a Gemini daily-quota hit would eat all 3
    # retries in one sitting. Rely on the specific GCP quota keywords
    # (cpus_all_regions, in_use_addresses) and `compute.googleapis.com`
    # for actual provisioning-quota errors; LLM-provider quota errors
    # fall through to the rate-limit signal detection below.
    if _PROVISION_PHRASE_RE.search(combined) or any(
        kw in combined
        for kw in [
            "cpus_all_regions",
            "in_use_addresses",
            "vm failed to start",
            "connection refused",
            "connection reset by peer",
            "timed out waiting for operation",
            "sshd",
            "startup script",
            "no space left on device",
            "disk quota exceeded",
            "filesystem is full",
            "address already in use",
            "failed to connect",
            "service is temporarily unavailable",
            "compute.googleapis.com",
            "resource not found",
            "container killed",
        ]
    ):
        return PROVISION_FAILURE

    # Anthropic 529 overload — server-side capacity, NOT account-specific. Must
    # requeue transiently but MUST NOT disable the account.
    if "overloaded" in combined:
        return "EXTERNAL_API"

    # GitHub rate limits — external API issue, NOT Claude account exhaustion.
    # RATE_LIMIT_PATTERNS is now specific enough that a plain "API rate limit
    # exceeded" from GitHub can't match it, but we still route GitHub errors to
    # EXTERNAL_API so they don't fall through to CODE_ERROR.
    if any(gh_kw in combined for gh_kw in ["github", "api.github.com"]) and any(
        kw in combined for kw in ["rate limit", "429", "too many requests"]
    ):
        return "EXTERNAL_API"

    # Claude API rate limit — exact Claude Code / Anthropic API error strings only.
    # See RATE_LIMIT_PATTERNS docstring for why broad keywords were removed.
    # Prefer structured signal (provider + kind + retry-hint) when resolvable;
    # fall back to legacy pattern-list scan for tasks without provider context.
    signal = _task_rate_limit_signal(task, combined)
    if signal is not None:
        _persist_rate_limit_signal_context(task, signal)
        return "RATE_LIMIT"
    rate_limit_patterns = _task_rate_limit_patterns(task)
    if any(kw in combined for kw in rate_limit_patterns):
        return "RATE_LIMIT"

    # Pipeline-internal rate-limit status messages — backend-generated strings written by
    # the orchestrator and reaper on tasks held or requeued due to provider account
    # exhaustion. These contain "rate limit" or "rate-limit" but are not from GitHub
    # (already caught above) and don't match provider-specific API error patterns because
    # they're internal status strings, not API responses. Classify as RATE_LIMIT so they
    # don't fall through to CODE_ERROR and create spurious auto-remediation fingerprints.
    # Examples:
    #   "Rate limited — requeued, waiting for account cooldown"
    #   "All accounts rate-limited — waiting for cooldown"
    #   "Auto-failed after N retry_after cycles; Pool state: all accounts in rate-limit cooldown"
    if any(rl in combined for rl in ("rate limited", "rate-limit")) and any(
        pl in combined for pl in ("requeued", "cooldown")
    ):
        return RATE_LIMIT

    # Workflow limits — iteration caps, spawn failures due to pipeline state (not code errors).
    # Checked before merge-blocking EXTERNAL_API so a message like "merge blocked, fix not spawned
    # (iteration cap)" is correctly classified as AGENT_TIMEOUT rather than EXTERNAL_API.
    if any(
        kw in combined
        for kw in [
            "iteration cap",
            "review not spawned",
            "fix not spawned",
            "review-fix cap reached",
            "review-fix cycle limit",
            "ci wait exceeded",
            "ci wait timed out",
            "ci in progress",
            "stuck in ci-wait",
            "backstop timeout",
            "evaluation timeout",
        ]
    ):
        return "AGENT_TIMEOUT"

    # Merge-blocking GitHub responses — branch protection, rule violations, not-mergeable states.
    # These are GitHub operation blocks, not agent code defects.
    if any(
        kw in combined
        for kw in [
            "merge blocked",
            "merge attempt",
            "merge conflicts",
            "merge failed",
            "merge hard-cap",
            "merge hard cap",
            "merge check failed",
            "not mergeable",
            "repository rule",
            "rule violations",
            "required status check",
            "branch protection",
            "closed without merging",
        ]
    ):
        return "EXTERNAL_API"

    # External API failures — GitHub, Claude API outages, DNS/TLS/network errors.
    # Also catches branch creation failures (GitHub API errors from _ensure_branch).
    # Provider model-config rejections (model-not-found, unsupported service_tier,
    # unsupported-auth-for-model) land here too: they're upstream 400s on the
    # provider's model surface, not code defects, so we don't want them
    # penalizing direction health or triggering code-defect investigations.
    # The OpenAI/Gemini adapters gate known-bad combinations at settings_to_cli
    # time, but the classifier is the last line of defense if a new
    # incompatibility lands before the gate list is updated.
    if _provider_model_config_rejection(combined):
        return "EXTERNAL_API"
    if ("failed to create" in combined and "branch" in combined) or any(
        kw in combined
        for kw in [
            "api.github.com",
            "api.anthropic.com",
            "api.z.ai",
            "500 internal server error",
            "502 bad gateway",
            "503 service unavailable",
            "github api error",
            "claude api",
            "connection timed out",
            "name resolution failed",
            # Anchored: bare "ssl" matched benign log lines that merely mention
            # SSL/TLS (e.g. "ssl context reused", a module path containing
            # "openssl") and misrouted them to EXTERNAL_API. False positives grow
            # with log volume, so require the actual failure phrase. `combined`
            # is lowercased so this also matches "SSL handshake failed".
            "ssl handshake failed",
            "certificate",
            "context_length_exceeded",
            "token limit exceeded",
            "could not resolve hostname",
            "name or service not known",
            "network unreachable",
            "failed to create pull request",
            "rollup pr creation failed",
            "staging branch",
            # Anchored: bare "401" matched any log line containing the digits 401
            # (timestamps, byte counts, exit codes, ids) and misrouted them.
            # Match the HTTP-status-anchored form; genuine auth failures still
            # also carry "unauthorized"/"bad credentials"/"token …" below.
            "http 401",
            "unauthorized",
            "bad credentials",
            "token expired",
            "token invalid",
            "token revoked",
            "authentication_failed",
            "authentication failed",
            "failed to authenticate",
            "invalid authentication credentials",
            "invalid api key",
            # Anthropic 403 when an org has disabled OAuth for Claude Code —
            # an auth/infra failure, not a code defect. Specific token only.
            "oauth_org_not_allowed",
        ]
    ):
        return "EXTERNAL_API"

    # Agent timeout — reaped by heartbeat, execution timeout, process killed by OS/orchestrator, OOM.
    # "[fatal:" matches agent _crashReport() labels: [FATAL:uncaughtException], [FATAL:unhandledRejection], etc.
    # NOTE: "timed out" and "killed" intentionally omitted — too broad and cause misclassification.
    # Only use specific forms: "execution timed out", "node process killed", "oom killer".
    # Live-work heartbeats (_AGENT_LIVE_WORK_PATTERNS) route here too — see module-level comment.
    if any(
        kw in combined
        for kw in [
            "no heartbeat",
            "reaped",
            "never started",
            "execution timed out",
            "sigterm",
            "out of memory",
            "oom-kill",
            "memory limit exceeded",
            "cannot allocate memory",
            "exit code 137",
            "exit code 143",
            "exit code 124",
            "exit code 139",
            "node process killed",
            "oom killer",
            "killed by",
            "cgroup",
            "[fatal:",
            "fatal error",
            "process crashed",
            *_AGENT_LIVE_WORK_PATTERNS,
        ]
    ):
        return "AGENT_TIMEOUT"

    # Orchestration constraints — dependency waits are not code defects
    if any(
        kw in combined
        for kw in ["waiting on", "dependencies cleared", "serializing migration"]
    ):
        return "AGENT_TIMEOUT"

    # Viewport gate infra-unavailable sentinel — Chromium missing or Docker fallback absent.
    # Checked before VIEWPORT_REGRESSION so infra shortfalls don't land in CODE_ERROR residuals.
    if _trusted_execution_capability_unavailable(task, logs):
        return PROVISION_FAILURE
    if any(kw in combined for kw in _VIEWPORT_INFRA_UNAVAILABLE_PATTERNS):
        return PROVISION_FAILURE

    # Viewport rendering regression — must be checked before the generic CODE_ERROR
    # fallthrough so it doesn't dissolve into residual fingerprints.
    if any(kw in combined for kw in _VIEWPORT_PATTERNS):
        record_viewport_regression_fingerprint(task)
        return VIEWPORT_REGRESSION

    # Deterministic agent/CLI config rejection (e.g. a permission-skip flag
    # refused under root). A definite code/config defect that reproduces on every
    # retry — classify as CODE_ERROR so it fails terminally, NOT as RATE_LIMIT.
    # Checked before BOTH the structured-report reclass (which could read a
    # nonzero-exit/no-output run as a provider death) AND the account-correlation
    # fallback (which would otherwise mask it as RATE_LIMIT during a cooldown
    # window and retry-storm it). The explicit error text is the strongest signal.
    if _agent_config_rejection(combined):
        record_residual_fingerprint(task, logs=logs)
        return "CODE_ERROR"

    # Structured agent error report — a generic failure (no keywords matched
    # above, no agent category) whose report proves a provider-layer death is
    # a RATE_LIMIT-path infra death, not a code defect. Checked before the
    # account-correlation fallback because the report is a per-task truthful
    # signal, not an inference from pool state.
    report_class = _agent_error_report_reclass(task)
    if report_class:
        return report_class

    # Default: code error — record fingerprint for residual-pattern analysis.
    # Pass `logs` so the fingerprint keys on the actual error signature
    # (Traceback/Error/FAIL line) when available, not on whatever agent
    # heartbeat happened to be in latest_status_message.
    record_residual_fingerprint(task, logs=logs)
    return "CODE_ERROR"


def classify_failure(
    task: "Task",
    logs: str | None = None,
    at_time: "datetime | None" = None,
    db_rl_override: bool = False,
) -> str:
    """Backward-compatible shim that returns only the failure class string."""
    return classify_failure_with_confidence(
        task, logs, at_time=at_time, db_rl_override=db_rl_override
    ).failure_class


def is_pure_execution_timeout(task: "Task") -> bool:
    """Return True iff a task failed PURELY because its wall-clock budget ran out.

    True only for the ``AGENT_TIMEOUT`` failure class whose sub-type is
    ``TIMEOUT_EXECUTION`` ("execution timed out" / "sigterm"). This deliberately
    EXCLUDES oom / process_crash / heartbeat_lost (resource or code issues that
    route to a separate timeout investigation and that more wall-clock will not
    fix), as well as every non-timeout class (rate-limit, spot-preemption,
    provisioning, external-API, code error). Side-effect-free.
    """
    if classify_failure(task) != AGENT_TIMEOUT:
        return False
    ctx = task.context or {}
    subtype = ctx.get("timeout_subtype") or classify_timeout_subtype(task)
    return subtype == TIMEOUT_EXECUTION


def escalated_effort_for_execution_timeout(task: "Task") -> str | None:
    """Return the next-higher effort tier to use when retrying ``task``, or None.

    The single shared policy for the "retry a wall-clock-timed-out task with a
    bigger budget" behavior. Returns a new effort string ONLY when ALL hold:

      * the kill-switch ``effort_escalation_on_execution_timeout_enabled`` is on;
      * the failure is a pure execution timeout (see
        :func:`is_pure_execution_timeout`); and
      * the task's currently-resolved effort can step up a tier (i.e. it is not
        already at the top tier "max").

    Otherwise returns None and the caller leaves the effort unchanged. The
    returned value is the per-attempt effort the requeue site should persist
    where the agent reads it (``context["effort"]``). Does NOT mutate the task.
    """
    # Deferred imports: avoid import cycles (max_plan_pool imports
    # runtime_config_state which the branch_resolution facade also touches).
    from app.branch_resolution import escalate_effort
    from app.max_plan_pool import resolve_effective_effort
    from app.shared_state import runtime_config

    if not runtime_config.get("effort_escalation_on_execution_timeout_enabled", True):
        return None
    if not is_pure_execution_timeout(task):
        return None

    payload = task.first_payload or {}
    ctx = task.context or {}
    task_type = payload.get("type") if isinstance(payload, dict) else None
    current_effort = resolve_effective_effort(payload, ctx, task_type, runtime_config)
    return escalate_effort(current_effort)


# ── Spawn-gate / forward-quality grader non-response cause (observability) ────
# When the research spawn-gate's adversarial grader (or the dark forward-quality
# grader) resolves through task_lifecycle._resolve_quality_grader_completion, the
# parse-level outcome (challenger_failed / empty_logs / no_json_array /
# invalid_json / no_hash_matches) answers WHAT failed at parse level but never
# WHY. These helpers turn the boundary's already-present material — the resolved
# grader's failure taxonomy, the parse_outcome, the grader's output, and the
# per-grader VM provenance — into a small machine-readable cause object. Pure
# observability: NO fail-open/fail-closed decision, retry count, or deferral
# behavior reads any of this.

# For a COMPLETED-but-unparseable grader, distinguish a model refusal (prose that
# declines to answer, no verdict structure) from a format miss (output present
# but not in the expected verdict shape, or empty/garbled). A FAILED grader does
# not use these — its cause is drawn from the existing failure taxonomy.
GRADER_CAUSE_REFUSAL = "grader_refusal"
GRADER_CAUSE_FORMAT_MISS = "grader_format_miss"
# The grader DID respond with a parseable verdict — any individual finding left
# unresolved is a per-finding gap, not a grader-level non-response.
GRADER_CAUSE_RESPONSIVE = "grader_responsive"

# Bounded per-attempt grader-resolution audit trail. Each resolution appends one
# entry instead of overwriting the single legacy ``adversarial_gate_diagnostic``
# record, so the bounded wholesale-retry sequence is visible after the fact.
GRADER_DIAGNOSTIC_TRAIL_KEY = "grader_diagnostic_trail"
_GRADER_DIAGNOSTIC_TRAIL_MAX = 5

# Lowercased prose markers of a model refusal. Kept narrow so an ordinary
# analytical sentence ("I cannot find a backend reference") does not over-match a
# genuine refusal; matched only on the no-verdict-array path.
_GRADER_REFUSAL_MARKERS: tuple[str, ...] = (
    "i cannot comply",
    "i can't comply",
    "i cannot assist",
    "i can't assist",
    "i cannot help with",
    "i can't help with",
    "i will not",
    "i won't",
    "i must decline",
    "i refuse",
    "unable to provide",
    "cannot provide this",
    "i'm not able to",
    "i am not able to",
)


def _refusal_or_format_miss(parse_outcome: str | None, logs: str | None) -> str:
    """Bounded refusal-vs-format-miss distinction for a completed-but-unparseable
    grader, keyed off the existing ``parse_outcome`` plus a light log inspection.

    * structurally empty output → format miss (nothing to refuse with)
    * garbled JSON / wrong-hash JSON → format miss (tried in-format, missed schema)
    * prose with no verdict array → refusal iff the prose reads as a refusal,
      else a format miss (the model talked but never produced the verdict shape)
    """
    from app.task_evaluators import (
        ADVERSARIAL_PARSE_EMPTY_LOGS,
        ADVERSARIAL_PARSE_INVALID_JSON,
        ADVERSARIAL_PARSE_NO_HASH_MATCHES,
    )

    text = (logs or "").strip()
    if not text or parse_outcome == ADVERSARIAL_PARSE_EMPTY_LOGS:
        return GRADER_CAUSE_FORMAT_MISS
    if parse_outcome in (
        ADVERSARIAL_PARSE_INVALID_JSON,
        ADVERSARIAL_PARSE_NO_HASH_MATCHES,
    ):
        return GRADER_CAUSE_FORMAT_MISS
    low = text.lower()
    if any(marker in low for marker in _GRADER_REFUSAL_MARKERS):
        return GRADER_CAUSE_REFUSAL
    return GRADER_CAUSE_FORMAT_MISS


async def _grader_image_stale_tag(
    grader_source_sha: str | None,
    session,
    project_id,
) -> bool | None:
    """Best-effort staleness verdict for a grader VM's baked source SHA.

    Returns ``True`` when ``grader_source_sha`` predates (is a strict ancestor
    of) the SHA the live fleet is currently serving, ``False`` when it matches or
    descends, and ``None`` when undeterminable. The expected-serving SHA read and
    the ancestry comparison are async/GitHub-aware, so the whole body is wrapped:
    any error, missing credential, or unknown SHA yields ``None`` so the caller
    OMITS the tag. NEVER raises and NEVER blocks or alters grader resolution.
    """
    try:
        if not grader_source_sha or session is None or project_id is None:
            return None
        from app.compute import expected_serving_source_sha

        expected_sha = await expected_serving_source_sha(session, project_id=project_id)
        if not expected_sha:
            return None
        if str(grader_source_sha) == str(expected_sha):
            return False

        # Different SHA — resolve GitHub creds best-effort and ask whether the
        # grader's SHA is an ancestor of (predates) the serving SHA.
        from app import github_refs
        from app.models import Project
        from app.release_promotion import _resolve_github_with_session

        try:
            project = await session.get(Project, project_id)
        except Exception:
            project = None
        if project is None:
            return None
        resolved = await _resolve_github_with_session(session, project)
        if not resolved:
            return None
        token, owner, repo = resolved
        verdict = await github_refs.is_ancestor_of(
            token,
            owner,
            repo,
            str(grader_source_sha),
            str(expected_sha),
            project_id=str(project_id),
        )
        if verdict is True:
            return True
        if verdict is False:
            return False
        return None
    except Exception:
        logger.debug(
            "[FAILURE_CLASS] grader stale-image tag undeterminable",
            exc_info=True,
        )
        return None


async def classify_grader_nonresponse_cause(
    grader: "Task",
    parse_outcome: str | None,
    gate_provenance: dict | None,
    *,
    completed: bool,
    logs: str | None = None,
    session=None,
    project_id=None,
) -> dict:
    """Return a small machine-readable cause object for a grader resolution.

    Rules (observability only — invents NO parallel cause vocabulary for the
    FAILED case):
      * FAILED grader → ``category`` drawn from the EXISTING failure taxonomy via
        :func:`classify_failure` (which already consults the persisted
        ``failure_category``), e.g. AGENT_TIMEOUT / RATE_LIMIT / PROVISION_FAILURE.
      * COMPLETED grader that DID parse (``parse_outcome == "ok"`` for the
        adversarial lane, or ``None`` for the forward-quality lane) → responsive.
      * COMPLETED-but-unparseable grader → refusal vs format miss via
        :func:`_refusal_or_format_miss`.
    Plus a ``stale_agent_image`` boolean tag (best-effort) present ONLY when the
    grader VM's recorded source SHA can be compared against the expected serving
    SHA; absent on any error or unknown SHA. The tag COMPLEMENTS, and does not
    replace, the separate fleet-wide serving-SHA operator surface.
    """
    from app.task_evaluators import ADVERSARIAL_PARSE_CHALLENGER_FAILED

    cause: dict = {"parse_outcome": parse_outcome}
    # Observability must NEVER break grader resolution — the whole body is
    # defensive so a classification bug degrades to a minimal cause object
    # rather than propagating an exception into the resolution path.
    try:
        if (not completed) or parse_outcome == ADVERSARIAL_PARSE_CHALLENGER_FAILED:
            try:
                category = classify_failure(grader)
            except Exception:
                category = CODE_ERROR
            cause["category"] = category
            cause["source"] = "failure_taxonomy"
        elif parse_outcome in ("ok", None):
            cause["category"] = GRADER_CAUSE_RESPONSIVE
            cause["source"] = "parse_outcome"
        else:
            cause["category"] = _refusal_or_format_miss(parse_outcome, logs)
            cause["source"] = "output_inspection"
    except Exception:
        logger.debug(
            "[FAILURE_CLASS] grader non-response cause classification degraded",
            exc_info=True,
        )
        cause.setdefault("category", CODE_ERROR)
        cause.setdefault("source", "error")

    try:
        stale = await _grader_image_stale_tag(
            (gate_provenance or {}).get("grader_agent_source_sha"),
            session,
            project_id,
        )
    except Exception:
        stale = None
    if stale is not None:
        cause["stale_agent_image"] = stale
    return cause


def synthesize_forward_quality_parse_state(
    completed: bool, logs: str | None, verdict_reason: str | None
) -> str:
    """Map a forward-quality grader's single artifact-level verdict onto the same
    parse-state vocabulary the adversarial lane uses, so both lanes feed one cause
    classifier. FAILED → challenger_failed; empty output → empty_logs; an
    unparseable fail-closed verdict → no_json_array; otherwise → ok.
    """
    from app.task_evaluators import (
        ADVERSARIAL_PARSE_CHALLENGER_FAILED,
        ADVERSARIAL_PARSE_EMPTY_LOGS,
        ADVERSARIAL_PARSE_NO_JSON_ARRAY,
        ADVERSARIAL_PARSE_OK,
    )

    if not completed:
        return ADVERSARIAL_PARSE_CHALLENGER_FAILED
    if not (logs or "").strip():
        return ADVERSARIAL_PARSE_EMPTY_LOGS
    if "unparseable" in (verdict_reason or "").lower():
        return ADVERSARIAL_PARSE_NO_JSON_ARRAY
    return ADVERSARIAL_PARSE_OK


def append_grader_diagnostic_trail(
    ctx: dict,
    *,
    grader_task_id,
    parse_outcome: str | None,
    cause: dict | None,
    vm_provenance: dict | None,
) -> dict:
    """Append a bounded per-attempt entry to ``ctx[GRADER_DIAGNOSTIC_TRAIL_KEY]``.

    Keeps the last ``_GRADER_DIAGNOSTIC_TRAIL_MAX`` entries so successive
    resolutions accumulate rather than overwrite the single diagnostic record.
    Auto-numbers ``attempt`` monotonically from the highest attempt already in
    the trail (NOT its length — capping must not reset the counter). Mutates
    ``ctx`` in place and returns the appended entry. Observability only.
    """
    trail = ctx.get(GRADER_DIAGNOSTIC_TRAIL_KEY)
    if not isinstance(trail, list):
        trail = []
    _prior_attempts = [
        e.get("attempt")
        for e in trail
        if isinstance(e, dict) and isinstance(e.get("attempt"), int)
    ]
    entry = {
        "attempt": (max(_prior_attempts) + 1) if _prior_attempts else 1,
        "grader_task_id": str(grader_task_id) if grader_task_id is not None else None,
        "parse_outcome": parse_outcome,
        "cause": cause,
        "vm_provenance": vm_provenance or {},
    }
    trail = [*trail, entry][-_GRADER_DIAGNOSTIC_TRAIL_MAX:]
    ctx[GRADER_DIAGNOSTIC_TRAIL_KEY] = trail
    return entry


# ── Infra-recovery policy primitive ─────────────────────────────────────────
# How many infra-class entries in the per-task _failure_class_history constitute
# "budget exhausted" for a single fix task.  Mirrors the AGENT_TIMEOUT repeat-
# count rule in compute_effective_max_requeues: two occurrences reduce the parent
# requeue budget; three signals structural breakage rather than transient infra.
_INFRA_BUDGET_THRESHOLD = 3


def is_recoverable_infra_terminal(task: "Task") -> bool:
    """True when a terminally-FAILED fix task's PR should be preserved, not closed.

    A terminal infrastructure failure (AGENT_TIMEOUT, VM preemption, provisioning
    failure, etc.) does not indicate a code defect — the PR code may be entirely
    valid and the agent may never have touched it.  The reaper should NOT close
    the PR in this case; the review pipeline can re-drive the next fix iteration.

    Returns False (PR should be closed / abandoned) when any of:
    - The failure class is not infrastructure (genuine merit / code failure).
    - The review-loop per-chain iteration budget is exhausted
      (``iteration >= max_iterations`` — _spawn_pipeline_task enforces the same
      guard; this prevents the close-skip from creating an unbounded re-drive loop
      after the chain cap fires).
    - The per-task infra-repeat count has reached or exceeded _INFRA_BUDGET_THRESHOLD,
      mirroring the retry-budget repeat-count rule in
      ``compute_effective_max_requeues`` (AGENT_TIMEOUT × 3+ on the SAME task
      signals a structurally broken task, not a transient infrastructure condition).

    Returns True (keep the PR open, allow the review loop to re-drive) otherwise.
    """
    ctx = task.context or {}
    failure_category = ctx.get("failure_category")
    if failure_category not in INFRA_CLASSES:
        return False

    # Review-loop iteration budget: the fix task's review_loop carries the current
    # iteration number and the per-chain cap set at spawn time.  When both are
    # present and the cap is reached, further re-drives cannot spawn a new
    # iteration, so the PR should be closed to unblock sibling rollups.
    review_loop = ctx.get("review_loop") or {}
    iteration = review_loop.get("iteration")
    max_iterations = review_loop.get("max_iterations")
    if iteration is not None and max_iterations is not None:
        if iteration >= max_iterations:
            return False

    # Compose with the retry-budget repeat-count rule: inspect the per-task
    # _failure_class_history (accumulated across prior RETRY cycles for this
    # task ID).  Three or more infra-class entries indicate the task itself is
    # structurally broken — the resurrection sweep already capped its own
    # budget at _resurrection_count=1, so a high repeat count here means the
    # task kept failing even after being resurrected.  At that point the review
    # loop should spawn a FRESH fix task rather than continuously re-driving
    # this broken one, so we return False and let the caller close the PR.
    from app.retry_budget import failure_class_history_classes

    history = failure_class_history_classes(ctx.get("_failure_class_history"))
    infra_count = sum(1 for c in history if c in INFRA_CLASSES)
    if infra_count >= _INFRA_BUDGET_THRESHOLD:
        return False

    return True
