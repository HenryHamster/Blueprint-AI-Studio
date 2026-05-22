"""Parent-child task lifecycle orchestration.

Extracted from main.py to reduce its size.  Contains the core task state
machine: _process_post_commit, _check_and_trigger_rollup, _spawn_pipeline_task,
and supporting functions.

All deferred imports from ``app.main`` are intentional — they avoid circular
imports (task_lifecycle is imported BY main) and preserve test-patchability
(tests mock via ``app.main.<name>``).
"""

import asyncio
import copy
import json
import logging
import random as _random
import re
import uuid
from typing import Dict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import load_only
from sqlalchemy.orm.attributes import flag_modified
from sqlmodel import select

from app.database import AsyncSessionLocal
from app.models import (
    Task,
    TaskHistory,
    Project,
    TaskStatus,
    ArtifactType,
    Alert,
    AlertSeverity,
    ProjectStatus,
    RegressionRemediationCase,
)
from app.schemas import AgentStatusPayload
from app.routers.agent import _has_local_cycle
from app.shared_state import runtime_config as _runtime_config, DEFAULT_TASK_SCOPE
import app.shared_state as _shared_state
from app.broadcast import broadcast_to_project
from app.payload_schemas import validate_internal_payload
from app.review_pipeline import _spawn_pipeline_task
from app.rollup_management import _check_and_trigger_rollup, _cleanup_branches
from app.notification_dispatch import (
    is_residual_remediation_task,
    post_remediation_result_message,
)
from app.failure_classification import (
    classify_failure,
    _timeout_subtype_from_message,
    TIMEOUT_OOM,
    TIMEOUT_HEARTBEAT_LOST,
    TIMEOUT_ITERATION_LIMIT,
    TIMEOUT_CASCADE,
    TIMEOUT_PROCESS_CRASH,
)
from app.retry_budget import (
    CODE_ERROR_STAGE_GUIDANCE,
    append_failure_class_history,
    classify_override_reason,
    compute_effective_max_requeues,
)

logger = logging.getLogger(__name__)


class PostCommitLocked(Exception):
    """Raised when _process_post_commit can't acquire the task row within the
    retry budget. Distinct from DBAPIError so the caller in agent_status.py
    can treat "row still held by peer" as a retryable pending, not an error.
    """


from app.failure_stage import (  # noqa: E402, F401, RUF100
    _TRANSIENT_SCAN_ERROR_PATTERNS,
    _derive_failure_stage,
    _is_transient_scan_error,
)


# Mirrors SCOPE_OVERFLOW_THRESHOLDS in agent/src/constants.ts — keep in sync.
_SCOPE_OVERFLOW_THRESHOLDS = {"small": 6, "medium": 10, "large": 20}


# Build/lint complaint patterns the reviewer's local VM might raise that
# correspond to a CI gate. When CI is GREEN for the matching job and the
# reviewer flags the gate as red, that's a reviewer-env issue, not a real
# defect — the fix agent should dispute via /tmp/fix_responses rather than
# edit config files to satisfy a phantom complaint.
_BUILD_GATE_PATTERN = re.compile(
    r"npm run build|npm ci|cannot resolve|"
    r"vite/client|@tailwindcss|tsc\b|--noEmit|"
    r"ruff(\s+(check|format))?|"
    r"build (?:fail|gate|broken|red)|"
    r"required verification gate|type definition|"
    r"package\.json.*(?:not.?install|missing)",
    re.IGNORECASE,
)


def _detect_ci_review_disagreement(
    issues: list,
    feedback: str,
    ci_check: dict | None,
    pr_number: int | None,
) -> str:
    """Return a fix-prompt preamble when reviewer claims a build/lint gate is
    red but CI shows the matching job green.

    Returns an empty string when there's no disagreement to surface; otherwise
    a short instructional block ending with ``\\n\\n`` that prepends the
    fix-prompt issue list. The agent is told to verify CI status via gh and,
    if confirmed green, write a structured `disputed` response with mechanical
    verify steps so the next reviewer is forced to re-run them.
    """
    if not ci_check or not ci_check.get("details"):
        return ""
    details = ci_check.get("details") or []
    green_jobs = {
        (d.get("name") or "").lower()
        for d in details
        if d.get("conclusion") == "success" and d.get("status") == "completed"
    }
    if not green_jobs:
        return ""
    haystack = (feedback or "").lower()
    for i in issues or []:
        desc = (i.get("description") or "").lower()
        haystack += "\n" + desc
    if not _BUILD_GATE_PATTERN.search(haystack):
        return ""
    # Map reviewer-complaint domains to CI job-name fragments. A green CI job
    # that matches a complained-about domain is the disagreement signal.
    flagged: list[str] = []
    if any(
        kw in haystack
        for kw in (
            "admin-ui",
            "vite",
            "tailwind",
            "vite/client",
            "frontend",
            "npm run build",
        )
    ) and any("frontend" in n for n in green_jobs):
        flagged.append("CI / Frontend")
    if any(kw in haystack for kw in ("agent", "tsc", "noemit", "type def")) and any(
        "agent" in n for n in green_jobs
    ):
        flagged.append("CI / Agent")
    if any(kw in haystack for kw in ("ruff", "backend", "py_compile", "lint")) and any(
        "backend" in n for n in green_jobs
    ):
        flagged.append("CI / Backend")
    if not flagged:
        return ""
    pr_ref = f"#{pr_number}" if pr_number else "this PR"
    job_list = ", ".join(sorted(set(flagged)))
    return (
        "## CI vs reviewer build-gate disagreement\n"
        f"GitHub Actions CI is currently GREEN on {pr_ref} for: {job_list}. "
        "However, the reviewer flagged build/lint gates as failing in their local environment.\n\n"
        "This usually means the reviewer's workspace lacks installed dependencies, NOT a real defect. "
        "Before re-fixing source code:\n"
        f"  1. Run `gh pr checks {pr_number or '<pr>'}` to confirm CI status.\n"
        "  2. If CI is green, write a `disputed` response to `/tmp/fix_responses_${TASK_ID}.json` "
        "with mechanical verify steps that prove the build passes from a fresh install — for example:\n"
        "     - `cd /tmp/workspace && npm ci && npm run build`\n"
        "     - `cd /tmp/workspace/admin-ui && npm ci && npm run build`\n"
        "     - reference the green CI run URL\n"
        "  3. The next reviewer will re-run those steps. If they pass, the issue is dropped; if they fail, "
        "the loop escalates to standoff and an operator is paged. Either way, you stop the thrash.\n"
        "  4. ONLY modify source code if you can REPRODUCE the build failure from a clean install. "
        "Do not edit config files just to satisfy a complaint you cannot reproduce locally.\n\n"
    )


def _build_scope_overflow_details(ctx: dict, task_scope: str) -> str:
    """Render a decomposition-hint block from the previous attempt's scope overflow.

    Uses ``_previous_changed_files`` (and optional ``_previous_diff_summary``) to
    surface: module distribution (files per top-level path), the expansion ratio
    vs. the scope threshold, and the specific files the agent touched. This lets
    the plan agent split along the actual fault lines (e.g. backend/app vs.
    agent/src) instead of re-analysing the whole scope. Returns "" if no signal.
    """
    changed_files = ctx.get("_previous_changed_files") or []
    if not changed_files:
        return ""

    module_counts: dict[str, int] = {}
    for f in changed_files:
        parts = str(f).split("/")
        key = "/".join(parts[:2]) if len(parts) >= 2 else parts[0]
        module_counts[key] = module_counts.get(key, 0) + 1

    lines = [
        "OVERFLOW DETAILS FROM PREVIOUS ATTEMPT:",
        f"- Files touched: {len(changed_files)}",
    ]
    threshold = _SCOPE_OVERFLOW_THRESHOLDS.get(task_scope)
    if threshold:
        lines.append(
            f"- Scope budget: {task_scope}={threshold} files "
            f"(over by {len(changed_files) - threshold}, "
            f"{len(changed_files) / threshold:.1f}x budget)"
        )
    if module_counts:
        dist = ", ".join(
            f"{m}={n}" for m, n in sorted(module_counts.items(), key=lambda kv: -kv[1])
        )
        lines.append(f"- Module distribution: {dist}")
        if len(module_counts) > 1:
            lines.append(
                "- Decomposition hint: split along the module boundaries above — "
                "each module can likely become a separate subtask."
            )
    lines.append("- Changed files: " + ", ".join(str(f) for f in changed_files[:10]))
    return "\n".join(lines) + "\n"


def _append_approve_to_review_history(
    review_loop: dict,
    changed_files: "list[str] | None" = None,
) -> list:
    """Append an APPROVE entry to ``review_history`` so the trajectory section
    shown to fix agents includes approved iterations alongside REQUEST_CHANGES
    ones.  PR #3435's iter 1 was approved, then iter 2's rebase regressed it —
    every later fix agent saw only the rejected iterations and could not see
    that the PR had ever been correct.  Surfacing the APPROVE entry lets the
    drift detector compute "files in current diff but absent from any approved
    iteration" — the load-bearing signal.

    Idempotent on (iteration, verdict) so re-entering the ci_waiting transition
    (e.g. retry after merge failure) does not append duplicate entries.
    """
    history = list(review_loop.get("review_history") or [])
    iteration = int(review_loop.get("iteration") or 0)
    if (
        history
        and history[-1].get("iteration") == iteration
        and history[-1].get("verdict") == "APPROVE"
    ):
        return history
    history.append(
        {
            "iteration": iteration,
            "feedback": "",
            "verdict": "APPROVE",
            "changed_files": list(changed_files or [])[:30],
        }
    )
    return history


def _record_parent_failure(parent: Task, failure_class: str) -> None:
    """Record a parent-task FAILED transition in deploy_monitoring and direction health."""
    from app.routers.agent_status import record_task_result

    record_task_result(
        success=False,
        failure_class=failure_class,
        task_id=str(parent.id),
        task_type=(parent.first_payload or {}).get("type", ""),
    )
    direction_id = str((parent.context or {}).get("direction_id") or "")
    if direction_id:
        _shared_state.record_direction_result(
            direction_id,
            success=False,
            failure_class=failure_class,
            project_id=str(parent.project_id) if parent.project_id else "",
        )


_RETRY_SUFFIX_AUTO_PREFIXES = re.compile(
    r"^(?:RETRY GUIDANCE:"
    r"|IMPORTANT: The previous attempt TIMED OUT"
    r"|NOTE: The previous attempt was CANCELLED"
    r"|WARNING: The same tests are failing).*?(?=\n\n|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)


def merge_retry_prompt_suffix(
    extra_guidance: str, existing_suffix: str, ctx: dict
) -> str:
    """Combine fresh retry guidance with carry-forward spawn-time suffix.

    Stamps ctx['_original_prompt_suffix'] on first use (with all known
    auto-generated leading blocks stripped from existing_suffix), then
    carries that snapshot forward verbatim across retries. Without this,
    each retry stacks another auto-block (observed on 911b3ed3 / 547a700d:
    two `IMPORTANT: The previous attempt TIMED OUT` blocks in the payload
    after a single reaper-driven retry).

    Used by both _requeue_parent_task (parent-level requeue) and the
    in-place reaper retry in reaper.py — keeping the dedup centralized
    so a third caller can't drift again.
    """
    if "_original_prompt_suffix" in ctx:
        carry = ctx.get("_original_prompt_suffix") or ""
    else:
        carry = _RETRY_SUFFIX_AUTO_PREFIXES.sub("\n", existing_suffix or "").strip()
        ctx["_original_prompt_suffix"] = carry
    return extra_guidance + ("\n" + carry if carry else "")


def _build_retry_prompt_suffix(
    failure_class: "str | None",
    original_status_message: "str | None",
    attempt: int,
    task_scope: str,
    ctx: dict,
) -> "tuple[str, str | None]":
    """Build failure-class-specific retry guidance for the agent prompt_suffix.

    Returns (extra_guidance, scope_override). scope_override is set to a
    different scope string when the failure mode should route through a
    different scope on retry:
      - AGENT_TIMEOUT with attempt ≥ 2: downgrade large→medium or medium→small
      - CODE_ERROR with attempt ≥ 2: downgrade large→medium or medium→small only
        when the previous failure stage is unknown or ctx indicates the scope
        itself overflowed (expansion_ratio > 1.5). Pure SCOPE_OVERFLOW is handled
        by its own branch above.
        Failures at identified code-defect stages (test/lint/build/review/merge)
        preserve the original scope since a tighter budget won't fix bugs.
      - SCOPE_OVERFLOW on a medium task: escalate medium→large so the plan
        agent decomposes the work into multiple small subtasks
      - SCOPE_OVERFLOW on a small task: escalate small→medium so the retry
        gets a 10-file budget instead of 6
    Otherwise None. Reads ``_previous_failing_tests`` and ``_previous_error_files``
    from ctx for CODE_ERROR hints.
    """
    extra_guidance = ""
    scope_override: str | None = None

    diagnosis_prefix = ""
    if ctx.get("diagnosis_task_id"):
        root_cause = str(ctx.get("diagnosis_root_cause") or "").strip()
        recommended_fix = str(ctx.get("diagnosis_recommended_fix") or "").strip()
        lines = [
            "DIAGNOSIS CONTEXT: This task was spawned from a diagnosed failure. "
            "Your previous attempt to implement the recommended fix failed — "
            "try a different approach to the same root cause rather than abandoning "
            "the diagnosis."
        ]
        if root_cause:
            lines.append(f"- Root cause identified: {root_cause[:800]}")
        if recommended_fix:
            lines.append(f"- Originally recommended fix: {recommended_fix[:800]}")
        affected = ctx.get("related_files") or []
        if affected:
            lines.append(
                "- Affected files: " + ", ".join(str(f) for f in affected[:10])
            )
        diagnosis_prefix = "\n".join(lines) + "\n"

    if failure_class == "AGENT_TIMEOUT":
        subtype = _timeout_subtype_from_message(original_status_message or "")
        if subtype == TIMEOUT_OOM:
            extra_guidance = (
                "IMPORTANT: The previous attempt TIMED OUT due to an out-of-memory condition. You MUST reduce memory usage:\n"
                "- Simplify data structures and avoid loading large files fully into memory\n"
                "- Stream or paginate reads instead of materializing entire result sets\n"
                "- Drop unused intermediate variables and large caches\n"
                "- Reduce scope so fewer files/transforms are held in memory at once\n"
            )
        elif subtype == TIMEOUT_HEARTBEAT_LOST:
            extra_guidance = (
                "IMPORTANT: The previous attempt TIMED OUT — the agent stopped sending heartbeats. Keep operations smaller:\n"
                "- Break long-running steps into shorter units that emit progress frequently\n"
                "- Avoid single commands that block for minutes; prefer batched/iterative work\n"
                "- Commit partial progress early so a later heartbeat loss doesn't erase it\n"
                "- Reduce scope to fit comfortably within the heartbeat window\n"
            )
        elif subtype == TIMEOUT_ITERATION_LIMIT:
            extra_guidance = (
                "IMPORTANT: The previous attempt TIMED OUT — the review-fix/iteration cap was exhausted. You MUST narrow scope:\n"
                "- Focus on the single most critical change; defer everything else\n"
                "- Produce changes that pass review on the first pass — avoid churn that triggers more fix cycles\n"
                "- Skip optional refactors, style tweaks, and speculative edits\n"
            )
        elif subtype == TIMEOUT_CASCADE:
            extra_guidance = (
                "NOTE: The previous attempt was CANCELLED by the orchestrator (cascade/supersession), not by a code defect.\n"
                "- Proceed with the originally planned approach; no need to reduce scope defensively\n"
                "- Check whether parent/sibling state has shifted before duplicating already-merged work\n"
            )
        elif subtype == TIMEOUT_PROCESS_CRASH:
            extra_guidance = (
                "IMPORTANT: The previous attempt TIMED OUT — the agent process crashed. Reduce scope and avoid risky operations:\n"
                "- Skip commands known to be flaky or resource-intensive on this VM\n"
                "- Keep changes small so a crash doesn't lose too much progress\n"
                "- Commit working changes early and often\n"
            )
        else:
            extra_guidance = (
                "IMPORTANT: The previous attempt TIMED OUT. You MUST reduce scope:\n"
                "- Focus on the single most critical change only\n"
                "- Skip non-essential improvements, refactors, or cleanup\n"
                "- Commit working changes early and often\n"
                "- If the task requires changes across many files, pick the 2-3 most important\n"
            )
        if subtype not in (TIMEOUT_CASCADE, TIMEOUT_ITERATION_LIMIT):
            if attempt >= 2 and task_scope == "large":
                scope_override = "medium"
            elif attempt >= 2 and task_scope == "medium":
                scope_override = "small"

    # Escalate medium→large when the previous attempt overflowed its scope budget.
    # Routing through "large" invokes the plan agent, which decomposes the work
    # into multiple small subtasks — turning a repeated same-scope failure into a
    # productive decomposition pass. Matches both the canonical SCOPE_OVERFLOW class
    # and the raw lowercase 'scope_overflow' label in case normalization didn't run.
    elif (
        failure_class == "SCOPE_OVERFLOW"
        or (ctx.get("failure_category") or "").lower() == "scope_overflow"
    ):
        if ctx.get("scope_gate_triggered") == "analysis_overflow":
            extra_guidance = (
                "RETRY GUIDANCE: The previous attempt could not produce a valid analysis plan within the scope budget.\n"
                "- Simplify the analysis approach; target the smallest coherent change that addresses the core issue\n"
                "- Plan against fewer files up front rather than exploring broadly\n"
                "- Avoid tangential code paths, speculative refactors, or unrelated cleanups\n"
            )
        else:
            extra_guidance = (
                "RETRY GUIDANCE: The previous attempt exceeded the scope's file-count budget.\n"
                "- Focus on the single most critical change; limit production edits to 1-3 files\n"
                "- Split unrelated concerns into follow-up work rather than bundling them here\n"
            )
        extra_guidance += _build_scope_overflow_details(ctx, task_scope)
        if task_scope == "medium":
            scope_override = "large"
        elif task_scope == "small":
            scope_override = "medium"

    elif failure_class == "CODE_ERROR":
        agent_raw = (ctx.get("agent_failure_category_raw") or "").lower()
        # Wrapper-fallback "incomplete": the agent process exited without
        # calling ./done|./fail|./pr — typically because the LLM printed a
        # unified diff to stdout instead of using its editing tools. The
        # status-message keyword detector frequently mis-matches "build" on
        # diff bodies that mention BuildConfig/etc., producing irrelevant
        # build-stage retry guidance. Short-circuit with a bespoke prompt
        # before any keyword-based routing fires.
        if agent_raw == "incomplete":
            # Branch on the previous attempt's provider: each agent CLI has a
            # different file-edit tool. Codex emitting a unified diff in its
            # final assistant message instead of calling apply_patch is the
            # primary observed failure mode here (3e1a042b incident).
            prev_provider = (ctx.get("_previous_provider") or "").lower()
            if prev_provider == "openai":
                tool_guidance = (
                    "  - Codex applies edits via the `apply_patch` tool. Your previous\n"
                    "    attempt printed a unified diff to stdout instead of calling\n"
                    "    apply_patch — printed text is NOT applied. Every file change\n"
                    "    must go through apply_patch; do NOT emit a diff in your final\n"
                    "    assistant message.\n"
                )
            elif prev_provider == "gemini":
                tool_guidance = (
                    "  - Gemini applies edits via its file-write tool calls. Your\n"
                    "    previous attempt printed a diff to stdout instead of\n"
                    "    invoking the edit tool — printed text is NOT applied.\n"
                )
            else:
                tool_guidance = (
                    "  - Use the file editing tools (Edit/Write/apply_patch) to\n"
                    "    actually apply changes to the working tree — do not just\n"
                    "    print a patch\n"
                )
            extra_guidance = (
                "RETRY GUIDANCE: Your previous attempt printed a diff to stdout but never\n"
                "applied any edits or shipped a PR. A unified diff printed as text does\n"
                "NOT modify the repository. The wrapper observed zero file changes and\n"
                "no ./done/./fail/./pr call, so it failed the task as 'incomplete'.\n"
                "You MUST:\n"
                + tool_guidance
                + "  - Run the build/tests if the task requires verification\n"
                "  - Open a PR via ./pr and call ./done to ship — OR call\n"
                '    ./fail "<reason>" if the task fundamentally cannot be completed\n'
                "Do NOT print a unified diff to stdout and exit.\n"
            )
            # Escape hatch: if this provider has now produced "incomplete" twice
            # in a row, it's likely a tool-layer mismatch the prompt won't fix.
            # Hint at ./fail so the orchestrator can surface the failure cleanly
            # rather than burning the full retry budget on the same wall.
            history = ctx.get("_failure_class_history") or []
            recent = [h for h in history[-2:] if isinstance(h, dict)]
            if (
                len(recent) >= 2
                and prev_provider
                and ctx.get("_previous_failure_stage") in (None, "", "incomplete")
            ):
                extra_guidance += (
                    "\nIf your tool layer is consistently failing to apply edits "
                    "(third attempt with the same provider), exit with "
                    '`./fail "tool-call layer not producing applied edits — needs '
                    'provider switch"` so the orchestrator can escalate.\n'
                )
        else:
            stage_guidance = CODE_ERROR_STAGE_GUIDANCE.get(
                ctx.get("_previous_failure_stage") or ""
            )
            if stage_guidance:
                extra_guidance = "RETRY GUIDANCE: " + stage_guidance + "\n"
            else:
                error_text = (original_status_message or "").lower()
                hints = []
                if any(
                    kw in error_text
                    for kw in ["test", "assert", "pytest", "jest", "vitest"]
                ):
                    hints.append(
                        "Fix the specific failing tests shown in the error log before making other changes."
                    )
                if any(kw in error_text for kw in ["lint", "ruff", "eslint", "tsc"]):
                    hints.append(
                        "Fix lint/type errors first — run the linter before committing."
                    )
                if hints:
                    extra_guidance = "RETRY GUIDANCE: " + " ".join(hints) + "\n"
                else:
                    extra_guidance = (
                        "RETRY GUIDANCE: The previous attempt failed with an unclassified code error.\n"
                        "- Verify changes compile and pass lint locally before committing\n"
                        "- Check for import errors or missing dependencies\n"
                        "- If the previous approach produced errors, try a simpler alternative\n"
                    )
        failing_tests = ctx.get("_previous_failing_tests", [])
        error_files = ctx.get("_previous_error_files", [])
        current_failing = _extract_failing_tests(original_status_message or "")
        overlap = [t for t in current_failing if t in set(failing_tests)]
        if overlap:
            extra_guidance = (
                "WARNING: The same tests are failing across attempts — try a fundamentally "
                "different approach rather than iterating on the current one.\n"
                "- Overlapping failures: " + ", ".join(overlap[:5]) + "\n"
            ) + extra_guidance
        if failing_tests:
            extra_guidance += "Previously failing tests:\n" + "".join(
                f"  {t}\n" for t in failing_tests
            )
        if error_files:
            extra_guidance += "Error file locations:\n" + "".join(
                f"  {f}\n" for f in error_files
            )
        prev_review_issues = ctx.get("_previous_review_issues") or []
        if prev_review_issues:
            extra_guidance += (
                "Unresolved review issues from previous attempt (address these proactively):\n"
                + "".join(f"  - {str(d)[:300]}\n" for d in prev_review_issues[:5])
            )
        if attempt >= 2 and task_scope in ("large", "medium"):
            prev_stage = ctx.get("_previous_failure_stage")
            prev_expansion = ctx.get("expansion_ratio")
            scope_related = (
                isinstance(prev_expansion, (int, float)) and prev_expansion > 1.5
            )
            # Downgrade only when the stage is unknown (no diagnostic signal that
            # points at a code defect) or when context indicates the scope itself
            # was the problem. Code-defect stages (test, lint, build, review, merge)
            # are not remedied by a tighter file budget — preserve original scope
            # and rely on stage-specific guidance instead.
            if scope_related or not prev_stage:
                scope_override = "medium" if task_scope == "large" else "small"

    elif failure_class == "EXTERNAL_API":
        error_text = original_status_message or ""
        lower = error_text.lower()
        if "github" in lower:
            service = "GitHub API"
        elif "anthropic" in lower or "claude" in lower:
            service = "Claude API"
        else:
            service = "external API"
        code_match = re.search(r"\b([45]\d{2})\b", error_text)
        code_hint = f" (status {code_match.group(1)})" if code_match else ""
        extra_guidance = (
            f"RETRY GUIDANCE: The previous attempt failed due to an {service} error{code_hint}.\n"
            "- Check the service's status page before making calls\n"
            "- Implement exponential backoff with jitter on retries\n"
            "- Handle transient 5xx and network errors gracefully\n"
        )

    elif failure_class == "RATE_LIMIT":
        extra_guidance = (
            "RETRY GUIDANCE: The previous attempt hit a rate limit.\n"
            "- Respect Retry-After headers when present\n"
            "- Use exponential backoff with jitter between retries\n"
            "- Batch or reduce request volume where possible\n"
        )

    cross_attempt_lines: list[str] = []
    if ctx.get("_previous_is_recurring"):
        cross_attempt_lines.append(
            "- This failure class has recurred 3+ times project-wide — it is a systemic "
            "pattern, not a one-off. Consider a structurally different approach."
        )
    prev_stage = ctx.get("_previous_failure_stage")
    if prev_stage:
        cross_attempt_lines.append(
            f"- Previous attempt failed at pipeline stage: {prev_stage}"
        )
    prev_self_review = ctx.get("_previous_self_review_outcome")
    if prev_self_review:
        summary = str(prev_self_review)[:300]
        cross_attempt_lines.append(
            f"- Previous attempt's self-review flagged: {summary}"
        )
    if cross_attempt_lines:
        extra_guidance += (
            "CROSS-ATTEMPT CONTEXT:\n" + "\n".join(cross_attempt_lines) + "\n"
        )

    if diagnosis_prefix:
        extra_guidance = diagnosis_prefix + extra_guidance

    return extra_guidance, scope_override


async def _has_active_strategic_task(project_id, session: AsyncSession) -> bool:
    """Return True if any QUEUED/PROVISIONING/IN_PROGRESS task is a design_doc_update or strategic_review.

    Takes a FOR UPDATE lock on the project row so the check+spawn is transactionally
    atomic. Without it, two concurrent callers (e.g. design_doc completion chaining
    to phase 2 and an autonomous-research tick) can both read "none active" in the
    same window and each insert a duplicate strategic task.

    Phase A1: skip_locked=True. If the project row is held by another caller,
    we return True (fail-safe — defer this spawn to the next cycle); the
    other caller will resolve the spawn in their own transaction. Returning
    False under contention would risk both callers concluding "none active"
    and double-spawning, the exact race this lock is here to prevent.

    Phase L-C4 audit: this aggregate read after the project lock is NOT
    upgraded to SERIALIZABLE because the Project lock acquired here is the
    canonical (Project before Task) gate for any strategic spawn — every
    spawn path goes through the same lock helper (now centralized in
    ``lock_project_for_strategic_transition``), so phantom ``Task`` rows
    appearing between this SELECT and the eventual INSERT are impossible
    by construction: any concurrent inserter would have to be holding the
    same project row, which our SKIP LOCKED here just rejected (we already
    returned True). Symmetry assumption confirmed: as long as Project is
    locked before any strategic-task SELECT/INSERT, repeatable-read is
    sufficient and SERIALIZABLE upgrades aren't required. See
    ``docs/concurrency.md`` for the full canonical-order argument.
    """
    result = await session.execute(
        select(Project.id)
        .where(Project.id == project_id)
        .with_for_update(skip_locked=True)
    )
    if result.first() is None:
        # Row missing or held — fail safe. The other holder will spawn (or
        # already has); our caller will skip and re-evaluate next cycle.
        return True
    # Phase B4 (H-P6): we only need first_payload.type per row to filter, so
    # drop status_log/plan_output JSONB. Loading payloads keeps first_payload
    # cheap (L-P2 — without payloads loaded, accessing first_payload would
    # trigger a per-row reload).
    active = (
        (
            await session.execute(
                select(Task)
                .options(
                    load_only(Task.id, Task.status, Task.payloads, Task.project_id)
                )
                .where(
                    Task.project_id == project_id,
                    Task.status.in_(
                        [
                            TaskStatus.QUEUED,
                            TaskStatus.PROVISIONING,
                            TaskStatus.IN_PROGRESS,
                        ]
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    return any(
        (t.first_payload).get("type") in ("design_doc_update", "strategic_review")
        for t in active
    )


async def _increment_research_spawn_count(
    project: Project, session: AsyncSession
) -> int:
    """Increment _research_spawn_count in project.vision and persist. Returns new count."""
    new_count = (project.vision or {}).get("_research_spawn_count", 0) + 1
    project.vision = {**(project.vision or {}), "_research_spawn_count": new_count}
    flag_modified(project, "vision")
    session.add(project)
    return new_count


def _extract_failing_tests(text: str) -> list[str]:
    """Extract up to 10 failing test identifiers from error output.

    Recognizes pytest FAILED path::test, Jest/Vitest FAIL suite/test,
    and generic Error: lines.
    """
    results: list[str] = []
    seen: set[str] = set()

    def _add(ident: str) -> None:
        if ident not in seen and len(results) < 10:
            seen.add(ident)
            results.append(ident)

    # pytest: FAILED path/to/test_file.py::test_name[param]
    for m in re.finditer(r"FAILED\s+(\S+::\S+)", text):
        _add(m.group(1))

    # Jest/Vitest: FAIL TestSuite/TestName or FAIL path/to/test.ts
    for m in re.finditer(r"\bFAIL\b\s+(\S+/\S+)", text):
        _add(m.group(1))

    # Generic "Error: message" lines (capped at 120 chars each)
    for m in re.finditer(r"^Error:\s+.+$", text, re.MULTILINE):
        _add(m.group(0).strip()[:120])

    return results


def _extract_error_files(text: str) -> list[str]:
    """Extract up to 5 file paths from error output.

    Recognizes Python tracebacks (File "path", line N) and
    TypeScript/ESLint errors (path/to/file.ts:line:col).
    """
    results: list[str] = []
    seen: set[str] = set()

    def _add(path: str) -> None:
        if path not in seen and len(results) < 5:
            seen.add(path)
            results.append(path)

    # Python tracebacks: File "path/to/file.py", line N
    for m in re.finditer(r'File "([^"]+)", line \d+', text):
        _add(m.group(1))

    # TypeScript/ESLint: path/to/file.ts:line:col (require path separator)
    for m in re.finditer(
        r"([a-zA-Z0-9_.][a-zA-Z0-9_./\-]*\.[a-z]{2,4}):(\d+):(\d+)", text
    ):
        path = m.group(1)
        if "/" in path:
            _add(path)

    return results


async def _requeue_parent_task(parent: Task, session: AsyncSession):
    """Re-evaluate a parent task by retrying its failed children.
    Cancels FAILED children and clones them as new QUEUED tasks.
    Remaps depends_on from old (cancelled) IDs to new (cloned) IDs.
    Leaves COMPLETED, CANCELLED, and active (QUEUED/IN_PROGRESS) children untouched.
    Sets parent back to EVALUATING — no VM needed."""
    from app.main import _maybe_create_failure_alert

    # Don't resurrect a cancelled parent
    if parent.status == TaskStatus.CANCELLED:
        logger.info(f"[REQUEUE] Skipping requeue — parent {parent.id} is CANCELLED")
        return 0

    # Cap re-evaluations to prevent infinite loops when children consistently fail
    MAX_PARENT_REQUEUES = 3
    parent_ctx = parent.context or {}
    requeue_count = parent_ctx.get("_requeue_count", 0)

    effective_max = await compute_effective_max_requeues(
        parent, session, MAX_PARENT_REQUEUES
    )

    if requeue_count >= effective_max:
        logger.warning(
            f"[REQUEUE] Parent {parent.id} hit re-evaluation limit "
            f"({requeue_count}/{effective_max} attempts) — failing permanently"
        )
        parent.status = TaskStatus.FAILED
        failed_children = (
            (
                await session.execute(
                    select(Task).where(
                        Task.parent_task_id == parent.id,
                        Task.status == TaskStatus.FAILED,
                    )
                )
            )
            .scalars()
            .all()
        )

        # Surface divergence cause in the status message when available so
        # operators on the kanban immediately see why the task was abandoned.
        _divergence_msg: str | None = None
        for _fc in failed_children:
            _abandoned = (_fc.context or {}).get("fix_loop_abandoned")
            if _abandoned:
                _divergence_msg = (
                    f"Abandoned early — fix loop stopped converging "
                    f"({_abandoned.get('exit_reason')} in {_abandoned.get('loop')} loop)"
                )
                break

        parent.latest_status_message = (
            _divergence_msg
            if _divergence_msg
            else f"Re-evaluation limit reached ({requeue_count} attempts) — children consistently failing"
        )
        from app.failure_classification import (
            worst_child_failure_category,
            persist_failure_category,
        )

        worst_cat = worst_child_failure_category(failed_children)
        persist_failure_category(parent, worst_cat)
        _record_parent_failure(parent, worst_cat)
        session.add(parent)
        await session.commit()
        # Create failure alert
        await _maybe_create_failure_alert(parent, session, parent.latest_status_message)
        return 0

    children = (
        (await session.execute(select(Task).where(Task.parent_task_id == parent.id)))
        .scalars()
        .all()
    )

    # Refresh base_branch from parent's staging branch — the original child's
    # base_branch may reference a deleted branch (e.g., plan/* after rollup merge)
    parent_staging = parent_ctx.get("plan_branch") or parent_ctx.get("research_branch")

    # Pass 1: Clone failed children, build old→new ID mapping
    old_to_new: Dict[str, str] = {}
    cloned_pairs: list = []  # (original, clone)
    retried_count = 0
    blocked_count = 0  # FAILED children skipped because of BLOCKED_PRECONDITION

    for child in children:
        if child.status != TaskStatus.FAILED:
            continue

        # Skip children that hit the review-fix cap: their PR is auto-closed and
        # marked terminal by _handle_cap_exhaustion. Cloning would spawn a fresh
        # create task + fresh PR, which typically re-hits the same merge conflict
        # that triggered the cap. Operators can manually requeue if desired.
        if (child.context or {}).get("failure_detail") == "review_cap_exhausted":
            logger.info(
                f"[REQUEUE] Skipping child {str(child.id)[:8]} — review_cap_exhausted, "
                f"PR already closed. Manual requeue required."
            )
            continue

        # Skip children whose fix loop concluded non-convergence — cloning would
        # re-discover the same divergence without making progress.
        if (child.context or {}).get("failure_detail") == "fix_loop_diverged":
            _abandoned = (child.context or {}).get("fix_loop_abandoned") or {}
            logger.info(
                f"[REQUEUE] Skipping child {str(child.id)[:8]} — fix_loop_diverged "
                f"({_abandoned.get('exit_reason')} in {_abandoned.get('loop')} loop). "
                f"Manual requeue required."
            )
            continue

        # Dedup: skip cloning if auto-remediation (debug → Fix: path) already spawned
        # a replacement for this child and that replacement is still active.  Cloning
        # here would produce two parallel tasks fixing the same thing (the race observed
        # in task b15b6813: _requeue_parent_task clone + debug-auto-fix both fired,
        # opened duplicate PRs, and the losing orphan burned its retry budget on an
        # empty-diff conflict).  We check the marker written by agent_status.py when it
        # creates the Fix: task; if the replacement failed/cancelled we fall through and
        # let requeue handle it normally.
        _debug_fix_id = (child.context or {}).get("_debug_fix_spawned_task_id")
        if _debug_fix_id:
            try:
                _debug_fix_uuid = uuid.UUID(_debug_fix_id)
                _debug_fix_task = (
                    await session.execute(
                        select(Task).where(
                            Task.id == _debug_fix_uuid,
                            Task.status.in_(
                                [
                                    TaskStatus.QUEUED,
                                    TaskStatus.PROVISIONING,
                                    TaskStatus.IN_PROGRESS,
                                    TaskStatus.EVALUATING,
                                ]
                            ),
                        )
                    )
                ).scalar_one_or_none()
                if _debug_fix_task is not None:
                    logger.info(
                        "[REQUEUE] Skipping clone of child %s — debug-fix task %s already active",
                        str(child.id)[:8],
                        _debug_fix_id[:8],
                    )
                    continue
            except Exception:
                logger.exception(
                    "[REQUEUE] Failed to check debug-fix task for child %s — proceeding with clone",
                    child.id,
                )

        # Capture before cancellation — cancellation overwrites both
        child_extra_logs = (child.context or {}).get("last_error", "")
        failure_class = classify_failure(
            child,
            logs=child_extra_logs or None,
            at_time=child.vm_stopped_at or child.last_heartbeat,
        )
        original_status_message = child.latest_status_message

        # BLOCKED_PRECONDITION: agent surfaced a "BLOCKER:" message and paused
        # because a precondition (referenced file/branch/PR) wasn't ready.
        # Cloning would hit the same wall on the next attempt — the precondition
        # has to land first. Skip the clone-and-retry path so the parent doesn't
        # burn its requeue budget. Operator manually requeues once the upstream
        # resolves (e.g. blocking rollup PR merges to main).
        if failure_class == "BLOCKED_PRECONDITION":
            logger.info(
                f"[REQUEUE] Skipping child {str(child.id)[:8]} — BLOCKED_PRECONDITION "
                f"({(original_status_message or '')[:120]!r}). Manual requeue once "
                f"the upstream precondition resolves."
            )
            blocked_count += 1
            continue

        # Skip retry if the current failure fingerprint matches the prior retry's
        # fingerprint — same code produced the same error, so retry is unlikely
        # to help. The recurring-failure-class check fires only after 3+ hits
        # project-wide, which is too late for individual task abort.
        try:
            from app.anomaly_detection import _error_fingerprint as _fp_fn

            # Audit W1: previous_error_fingerprint is now a column.
            prior_fp = child.previous_error_fingerprint
            current_fp = _fp_fn(original_status_message or "")
            if prior_fp and current_fp and prior_fp == current_fp:
                logger.info(
                    f"[REQUEUE] Skipping child {str(child.id)[:8]} — "
                    f"identical error fingerprint across retries"
                )
                continue
        except Exception:
            logger.exception(
                "[REQUEUE] Fingerprint comparison failed for task %s", child.id
            )

        # Auto-generate playbook entry if this failure pattern is recurring
        try:
            from app.playbook_learning import _maybe_auto_playbook

            await _maybe_auto_playbook(
                child, failure_class, original_status_message, session
            )
        except Exception:
            logger.exception(
                "[AUTO_PLAYBOOK] Failed to process playbook for task %s", child.id
            )

        # Cancel the failed child so rollup skips it
        child.status = TaskStatus.CANCELLED
        child.latest_status_message = "Parent requeued — replaced by retry"
        session.add(child)

        # Clone as a new task with fresh VM assignment — refresh base_branch
        # to prevent cloning into a deleted branch
        new_payloads = copy.deepcopy(child.payloads or [])
        if parent_staging and new_payloads:
            new_payloads[0]["base_branch"] = parent_staging

        # Inject retry guidance into prompt_suffix based on failure class
        child_ctx = child.context or {}
        task_scope = child_ctx.get("scope", DEFAULT_TASK_SCOPE)
        attempt = child_ctx.get("_attempt_number", 1)
        _ctx_keys_to_strip = {
            "max_plan_account_id",
            "llm_account_id",
            "llm_provider",
            "llm_auth_mode",
            "llm_model",
            "llm_execution_mode",
            "provision_failures",
            "provision_failures_infra",
            "_infra_wait",
            "blacklisted_accounts",
            "provision_failed_accounts",
            "_slot_released",
        }
        clone_ctx = {k: v for k, v in child_ctx.items() if k not in _ctx_keys_to_strip}

        # Preserve the prior attempt's provider/model under a non-stripped key
        # so retry guidance can branch on what actually failed (e.g. emit
        # apply_patch wording for Codex, Edit/Write wording for Claude). The
        # canonical llm_provider/llm_model fields are stripped above so the
        # retry can re-select; _previous_provider survives for diagnostic use.
        prev_provider = child_ctx.get("llm_provider")
        prev_model = child_ctx.get("llm_model")
        if prev_provider:
            clone_ctx["_previous_provider"] = prev_provider
        if prev_model:
            clone_ctx["_previous_model"] = prev_model

        # RATE_LIMIT: propagate the quarantine so the clone can't re-select the
        # same account before its cooldown elapses. agent_status.py already put
        # the failed account in child_ctx["blacklisted_accounts"] — re-applying
        # it here closes the gap where _ctx_keys_to_strip would otherwise clear
        # the list and the clone starts with a blank slate.
        if failure_class == "RATE_LIMIT":
            rl_acct_id = child_ctx.get("llm_account_id") or child_ctx.get(
                "max_plan_account_id"
            )
            if rl_acct_id:
                prior_bl = list(child_ctx.get("blacklisted_accounts") or [])
                if rl_acct_id not in prior_bl:
                    prior_bl.append(rl_acct_id)
                clone_ctx["blacklisted_accounts"] = prior_bl
                logger.info(
                    "[REQUEUE] RATE_LIMIT on account %s — propagating blacklist to clone "
                    "(total blocked: %d)",
                    rl_acct_id[:8],
                    len(prior_bl),
                )

        clone_ctx["_attempt_number"] = attempt + 1
        clone_ctx["_max_attempts"] = MAX_PARENT_REQUEUES + 1

        # Accumulate VM cost of prior failed attempts so the digest can compute
        # total compute burned on retries that eventually succeeded.
        try:
            from app.routers.admin_analytics import ON_DEMAND_RATE, SPOT_RATE

            if child.vm_provisioned_at and child.vm_stopped_at:
                _rate = SPOT_RATE if child.vm_spot else ON_DEMAND_RATE
                _attempt_cost = (
                    _rate
                    * (child.vm_stopped_at - child.vm_provisioned_at).total_seconds()
                )
                _prior_cost = float(clone_ctx.get("_prior_attempts_cost") or 0.0)
                clone_ctx["_prior_attempts_cost"] = round(
                    _prior_cost + max(_attempt_cost, 0.0), 6
                )
        except Exception:
            logger.exception(
                "[REQUEUE] Failed to accumulate prior attempt cost for task %s",
                child.id,
            )
        clone_ctx["_previous_error"] = (original_status_message or "")[:5000]
        clone_ctx["_previous_failure_class"] = failure_class
        clone_ctx["_failure_class_history"] = append_failure_class_history(
            clone_ctx.get("_failure_class_history"),
            failure_class,
        )

        # Enriched failure diagnosis: fingerprint, stage, error summary, and recurring flag
        try:
            from app.anomaly_detection import (
                _error_fingerprint,
                _is_recurring_failure_class,
            )

            error_msg = original_status_message or ""
            clone_ctx["_previous_error_fingerprint"] = _error_fingerprint(error_msg)
            failure_stage = _derive_failure_stage(failure_class, error_msg)
            if failure_stage:
                clone_ctx["_previous_failure_stage"] = failure_stage
            if failure_class == "CODE_ERROR":
                clone_ctx["_previous_error_summary"] = error_msg[:500]
            proj_anomalies = _shared_state.persistent_anomalies.get(
                str(child.project_id), {}
            )
            if _is_recurring_failure_class(proj_anomalies, failure_class):
                clone_ctx["_previous_is_recurring"] = True
        except Exception:
            logger.exception(
                "[REQUEUE] Failed to enrich retry context for task %s", child.id
            )

        # Propagate PR artifacts from the failed attempt to the retry context
        prev_pr_url = child_ctx.get("_pr_url", "")
        prev_changed_files = child_ctx.get("_pr_changed_files", [])
        prev_file_change_details = child_ctx.get("_file_change_details", [])
        if prev_pr_url:
            clone_ctx["_previous_pr_url"] = prev_pr_url
        if prev_changed_files:
            clone_ctx["_previous_changed_files"] = prev_changed_files
        if prev_file_change_details:
            clone_ctx["_previous_file_change_details"] = prev_file_change_details

        # Propagate enriched step-level progress for multi-file retry guidance
        prev_diff_summary = child_ctx.get("_pr_diff_summary", [])
        prev_test_failure_summary = child_ctx.get("_test_failure_summary", "")
        prev_self_review_outcome = child_ctx.get("_self_review_outcome")
        prev_analysis_plan = child_ctx.get("_analysis_plan", "")
        if prev_diff_summary:
            clone_ctx["_previous_diff_summary"] = prev_diff_summary
        if prev_test_failure_summary:
            clone_ctx["_previous_test_failure_summary"] = prev_test_failure_summary
        if prev_self_review_outcome:
            clone_ctx["_previous_self_review_outcome"] = prev_self_review_outcome
        if prev_analysis_plan:
            clone_ctx["_previous_analysis_plan"] = prev_analysis_plan

        # Extract last review iteration's unresolved CRITICAL/MAJOR issues so the
        # retry can address structured reviewer feedback that truncated status
        # messages often miss.
        try:
            review_history = (child_ctx.get("review_loop") or {}).get(
                "review_history"
            ) or []
            if review_history:
                last_issues = (review_history[-1] or {}).get("issues") or []
                filtered = [
                    str(iss.get("description") or "").strip()
                    for iss in last_issues
                    if isinstance(iss, dict)
                    and str(iss.get("severity") or "").upper() in ("CRITICAL", "MAJOR")
                    and (iss.get("description") or "")
                ]
                if filtered:
                    clone_ctx["_previous_review_issues"] = filtered[:5]
        except Exception:
            logger.exception(
                "[REQUEUE] Failed to extract review issues for task %s", child.id
            )

        # Carry forward the review-iteration count consumed by the prior attempt.
        # _max_review_iterations subtracts half of this from the new cap (floor 3)
        # so retries don't each receive a fresh full iteration budget.
        try:
            prior_iter = int((child_ctx.get("review_loop") or {}).get("iteration") or 0)
            if prior_iter > 0:
                clone_ctx["_prior_review_iterations_consumed"] = prior_iter
        except (TypeError, ValueError):
            pass

        if new_payloads and isinstance(new_payloads[0], dict):
            existing_suffix = new_payloads[0].get("prompt_suffix", "") or ""
            extra_guidance, scope_override = _build_retry_prompt_suffix(
                failure_class,
                original_status_message,
                attempt,
                task_scope,
                clone_ctx,
            )
            if scope_override:
                _override_reason = classify_override_reason(failure_class, clone_ctx)
                clone_ctx["scope"] = scope_override
                clone_ctx["_scope_reduced"] = True
                clone_ctx["scope_calibrated_from"] = f"{task_scope}:{_override_reason}"
            if extra_guidance:
                new_payloads[0]["prompt_suffix"] = merge_retry_prompt_suffix(
                    extra_guidance, existing_suffix, clone_ctx
                )

        new_payloads = [
            validate_internal_payload(p, "task_lifecycle:requeue_child")
            for p in new_payloads
        ]
        new_child = Task(
            project_id=child.project_id,
            title=child.title,
            description=child.description,
            parent_task_id=parent.id,
            payloads=new_payloads,
            context=clone_ctx,  # Strip runtime-only fields; may include _scope_reduced
            depends_on=[],  # Set in pass 2 after all clones exist
            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
            status=TaskStatus.QUEUED,
            created_by="ORCHESTRATOR",
            requires_approval=child.requires_approval,
        )
        session.add(new_child)
        await session.flush()  # Assigns new_child.id
        old_to_new[str(child.id)] = str(new_child.id)
        cloned_pairs.append((child, new_child))
        # Record the clone's ID in the cancelled child's context so the
        # orchestrator can resolve requeue-cancelled dependencies without
        # cascade-cancelling external tasks that depend on this child.
        child.context = {
            **(child.context or {}),
            "_replaced_by_task_id": str(new_child.id),
        }
        flag_modified(child, "context")
        retried_count += 1

        history = TaskHistory(
            task_id=new_child.id,
            action="SPAWNED",
            details={"spawned_by": "REQUEUE", "retrying": str(child.id)},
        )
        session.add(history)

    # Pass 2: Remap depends_on from old (cancelled) IDs to new (cloned) IDs
    for original, clone in cloned_pairs:
        remapped = []
        for dep_id in original.depends_on or []:
            if dep_id in old_to_new:
                remapped.append(old_to_new[dep_id])  # Point to the clone
            else:
                remapped.append(dep_id)  # Keep (COMPLETED sibling or still-active task)
        clone.depends_on = remapped

    # Pass 3: In-memory cycle check on cloned subgraph
    if cloned_pairs:
        graph = {str(c.id): c.depends_on or [] for _, c in cloned_pairs}
        if _has_local_cycle(graph):
            logger.warning(
                f"[REQUEUE] Circular deps detected in cloned children of {parent.id} — clearing all depends_on"
            )
            for _, clone in cloned_pairs:
                clone.depends_on = []

    # No failed children to clone — this is a no-op.  Don't burn the requeue
    # budget on callers that invoke us defensively (admin manual retry of a
    # COMPLETED parent, reaper self-timeout rescue, Step 4 all-children-terminal
    # sweep with no FAILED children).  Decide terminal state based on what
    # children actually exist so the parent doesn't sit in EVALUATING forever.
    if retried_count == 0:
        has_completed = any(c.status == TaskStatus.COMPLETED for c in children)
        has_active = any(
            c.status
            in (
                TaskStatus.QUEUED,
                TaskStatus.PROVISIONING,
                TaskStatus.IN_PROGRESS,
                TaskStatus.EVALUATING,
            )
            for c in children
        )
        if not has_completed and not has_active:
            # All children terminal and none succeeded — they're all CANCELLED
            # or skipped-FAILED (review_cap_exhausted, fix_loop_diverged,
            # BLOCKED_PRECONDITION, identical-fingerprint). Mark parent FAILED
            # with the right class so the reaper stops looping. Distinguish
            # the BLOCKED_PRECONDITION case so operators see "held on upstream"
            # rather than the generic "nothing to roll up" message.
            from app.failure_classification import (
                AGENT_TIMEOUT,
                BLOCKED_PRECONDITION,
                persist_failure_category,
            )

            parent.status = TaskStatus.FAILED
            if blocked_count > 0 and blocked_count == sum(
                1 for c in children if c.status == TaskStatus.FAILED
            ):
                parent_class = BLOCKED_PRECONDITION
                parent.latest_status_message = (
                    f"All {blocked_count} subtask(s) blocked on upstream precondition — "
                    f"manual requeue once it resolves"
                )
            else:
                parent_class = AGENT_TIMEOUT
                parent.latest_status_message = (
                    "No failed subtasks to retry and no successful subtasks to roll up"
                )
            persist_failure_category(parent, parent_class)
            _record_parent_failure(parent, parent_class)
            session.add(parent)
            await session.commit()
            await _maybe_create_failure_alert(
                parent, session, parent.latest_status_message
            )
            await broadcast_to_project(
                str(parent.project_id),
                "task_updated",
                {"task_id": str(parent.id), "status": TaskStatus.FAILED},
            )
            logger.warning(
                f"[REQUEUE] Parent {parent.id} has no failed children to retry and "
                f"no successful ones to roll up — marking FAILED ({parent_class})"
            )
            return 0
        # Has a COMPLETED child (reaper Step 3 + rollup will re-evaluate) or
        # active children (wait for them).  Nudge to EVALUATING so the rollup
        # sweep picks it up, but don't increment _requeue_count/attempt_count —
        # we didn't actually retry anything.
        parent.status = TaskStatus.EVALUATING
        parent.latest_status_message = (
            "No failed subtasks to retry — awaiting rollup re-evaluation"
        )
        session.add(parent)
        await session.commit()
        await broadcast_to_project(
            str(parent.project_id),
            "task_updated",
            {"task_id": str(parent.id), "status": TaskStatus.EVALUATING},
        )
        logger.info(
            f"[REQUEUE] Parent {parent.id} — no failed children to retry "
            f"(has_completed={has_completed}, has_active={has_active}); "
            f"kept at EVALUATING without burning requeue budget"
        )
        return 0

    parent.context = {**(parent.context or {}), "_requeue_count": requeue_count + 1}
    parent.status = TaskStatus.EVALUATING
    parent.latest_status_message = (
        f"Requeued — retrying {retried_count} failed subtask(s)"
    )
    parent.attempt_count += 1
    session.add(parent)
    await session.commit()

    for child in children:
        await session.refresh(child)
    await broadcast_to_project(
        str(parent.project_id),
        "task_updated",
        {"task_id": str(parent.id), "status": TaskStatus.EVALUATING},
    )
    logger.info(
        f"[REQUEUE] Parent {parent.id} re-evaluating — {retried_count} failed children retried"
    )
    return retried_count


async def _merge_with_retry(
    merge_pull_request_fn,
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    project_id: str,
    max_attempts: int = 3,
) -> dict:
    """Call merge_pull_request with exponential backoff for transient 5xx errors.

    Returns the merge result dict on success/conflict (same semantics as
    merge_pull_request).  Only retries on raised exceptions — non-200 merge
    results (405/409) are returned as-is without retrying.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            return await merge_pull_request_fn(
                gh_token, owner, repo, pr_number, project_id=project_id
            )
        except Exception as exc:  # noqa: PERF203 — per-attempt try is required for the retry loop
            last_exc = exc
            if attempt < max_attempts - 1:
                backoff = (2**attempt) * (1 + _random.random())  # ~1-2s, ~2-4s
                logger.warning(
                    f"[PIPELINE] Merge attempt {attempt + 1} for PR #{pr_number} raised {type(exc).__name__}, "
                    f"retrying in {backoff:.1f}s: {exc!r}"
                )
                await asyncio.sleep(backoff)
    raise last_exc  # type: ignore[misc]


async def _comment_with_retry(
    create_pr_comment_fn,
    gh_token: str,
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
    max_attempts: int = 3,
) -> None:
    """Call create_pr_comment with exponential backoff for transient GitHub failures.

    create_pr_comment raises HTTPException on any non-201 response, so a single
    upstream 5xx would otherwise abort the post-commit flow. Swallows failure
    after exhausting retries — a missing PR comment is not worth aborting the
    merge pipeline.
    """
    last_exc = None
    for attempt in range(max_attempts):
        try:
            await create_pr_comment_fn(gh_token, owner, repo, pr_number, body)
            return
        except Exception as exc:  # noqa: PERF203 — per-attempt try is required for the retry loop
            last_exc = exc
            if attempt < max_attempts - 1:
                backoff = (2**attempt) * (1 + _random.random())  # ~1-2s, ~2-4s
                logger.warning(
                    f"[PIPELINE] PR #{pr_number} comment attempt {attempt + 1} raised "
                    f"{type(exc).__name__}, retrying in {backoff:.1f}s: {exc!r}"
                )
                await asyncio.sleep(backoff)
    logger.error(
        f"[PIPELINE] PR #{pr_number} comment failed after {max_attempts} attempts: {last_exc!r}"
    )


async def _spawn_ci_auto_remediation_followup(
    task: Task,
    session: AsyncSession,
) -> bool:
    """Spawn an investigate task after a CI auto-remediation task fails.

    CI auto-remediation tasks lack a direction_id, so the standard
    code-defect investigation gate skips them. This trigger fires per
    failed auto-fix and dedups by (project, ci_failure_branch, fingerprint)
    over a 24h window to avoid runaway loops on flapping checks.
    Returns True when an investigation is spawned.
    """
    from datetime import timedelta
    from app.main import _utcnow

    ctx = task.context or {}
    branch = ctx.get("ci_failure_branch") or "main"
    fingerprint = ctx.get("_previous_error_fingerprint") or ""
    failed_checks = ctx.get("ci_failure_checks") or []
    failure_type = ctx.get("ci_failure_type") or "(unknown)"
    failed_prompt = (task.first_payload or {}).get("prompt", "") or ""
    error_summary = (task.latest_status_message or "")[:400]

    cutoff = _utcnow() - timedelta(hours=24)
    dedup_q = select(Task.id).where(
        Task.project_id == task.project_id,
        Task.created_at >= cutoff,
        Task.payloads[0]["type"].as_string() == "investigate",
        Task.context["spawned_by"].as_string() == "ci_auto_remediation_failure",
        Task.context["ci_failure_branch"].as_string() == str(branch),
    )
    if fingerprint:
        dedup_q = dedup_q.where(
            Task.context["_previous_error_fingerprint"].as_string() == str(fingerprint)
        )
    if (await session.execute(dedup_q.limit(1))).first() is not None:
        logger.info(
            f"[AUTO-REMEDIATION] Skip follow-up investigation for {task.id}: "
            f"recent investigation exists for branch={branch} fingerprint={fingerprint[:40]}"
        )
        return False

    if isinstance(failed_checks, (list, tuple)):
        checks_str = ", ".join(str(c) for c in failed_checks)[:300]
    else:
        checks_str = str(failed_checks)[:300]
    prompt_lines = [
        f"A CI auto-remediation task on branch '{branch}' failed to fix the "
        f"underlying CI failure. Diagnose why the auto-fix could not resolve it.",
        "",
        f"Failure type: {failure_type}",
        f"Failed checks: {checks_str or '(none captured)'}",
        f"Error summary: {error_summary or '(no message)'}",
    ]
    if fingerprint:
        prompt_lines.append(f"Error fingerprint: {fingerprint}")
    if failed_prompt:
        prompt_lines.extend(
            [
                "",
                f"Approach the auto-fix attempted (truncated):\n{failed_prompt[:1500]}",
            ]
        )
    prompt_lines.append(
        "\nProduce the structured diagnosis JSON report so a targeted "
        "remediation can be spawned. Consider whether the failure is "
        "infrastructure-related, a flaky check, or a deeper code defect "
        "that the original auto-fix prompt mis-scoped."
    )
    inv_payload = validate_internal_payload(
        {
            "type": "investigate",
            "prompt": "\n".join(prompt_lines)[:20000],
            "base_branch": _runtime_config.get("integration_branch", "main"),
        },
        "task_lifecycle:ci_auto_remediation_followup",
    )
    inv_ctx = {
        "spawned_by": "ci_auto_remediation_failure",
        "source_task_id": str(task.id),
        "ci_failure_branch": str(branch),
        "ci_failure_type": failure_type,
    }
    if fingerprint:
        inv_ctx["_previous_error_fingerprint"] = str(fingerprint)
        inv_ctx["failure_fingerprints"] = [str(fingerprint)]
    new_task = Task(
        project_id=task.project_id,
        title=f"Investigate CI auto-fix failure on {branch}"[:120],
        description=(
            f"Auto-spawned after CI auto-remediation task {str(task.id)[:8]} failed"
        )[:500],
        payloads=[inv_payload],
        context=inv_ctx,
        vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
        status=TaskStatus.QUEUED,
        created_by="ORCHESTRATOR",
    )
    session.add(new_task)
    session.add(
        TaskHistory(
            task_id=new_task.id,
            action="SPAWNED",
            details={
                "spawned_by": "CI_AUTO_REMEDIATION_FAILURE",
                "source_task_id": str(task.id),
                "ci_failure_branch": str(branch),
            },
        )
    )
    logger.info(
        f"[AUTO-REMEDIATION] Spawned follow-up investigation {new_task.id} "
        f"for failed auto-fix {task.id} (branch={branch}, "
        f"fingerprint={fingerprint[:40] if fingerprint else 'none'})"
    )
    return True


async def _regression_class_for_task(task: Task, session: AsyncSession) -> str | None:
    """Return the regression_class associated with a task, checking the parent chain.

    Fix tasks spawned by the investigate→fix chain carry diagnosis_task_id in
    their context pointing to the investigate task, which holds regression_class.
    This one-level lookup closes the wiring gap so close-loop handlers work for
    both the investigate task itself and any downstream fix task it spawned.
    """
    ctx = task.context or {}
    rc = ctx.get("regression_class")
    if rc:
        return rc
    diag_id_str = ctx.get("diagnosis_task_id")
    if not diag_id_str:
        return None
    try:
        parent = await session.get(Task, uuid.UUID(str(diag_id_str)))
    except Exception:
        return None
    return (parent.context or {}).get("regression_class") if parent else None


async def _close_loop_regression_case_on_merge(
    task: Task, pr_number: int, session: AsyncSession
) -> None:
    """Advance a RegressionRemediationCase to MERGED when its remediation PR merges."""
    regression_class = await _regression_class_for_task(task, session)
    if not regression_class:
        return
    result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == task.project_id,
            RegressionRemediationCase.regression_class == regression_class,
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        return
    snapshot = (task.context or {}).get("signal_snapshot") or {}
    if not snapshot:
        # Fall back to spawn-time snapshot; run_recovery_once requires a
        # non-empty snapshot to evaluate the MERGED→CLOSED transition.
        snapshot = dict(case.signal_snapshot_at_spawn or {})
    ctx = task.context or {}
    spawn_was_cross_layer = bool(ctx.get("cross_layer"))
    spawn_layers_enumerated = list(ctx.get("cross_layer_layers") or [])

    # Classify layers from PR changed files synchronously at merge time so
    # the compliance watchdog has layers_targeted immediately rather than
    # waiting for the async backfill (which can lag hours or fail silently).
    # Falls back gracefully; _backfill_layers_targeted_once remains as a net.
    layers_targeted: list[str] | None = None
    if spawn_was_cross_layer:
        try:
            from app.github import get_pr_files, get_repo_installation_token
            from app.models import Project
            from app.pipeline_regression_dispatcher import _classify_layers_from_paths

            project = await session.get(Project, task.project_id)
            if project and project.github_owner and project.github_repo:
                token = await get_repo_installation_token(
                    project.github_owner, project.github_repo
                )
                if token:
                    files = await get_pr_files(
                        token,
                        project.github_owner,
                        project.github_repo,
                        pr_number,
                        project_id=str(task.project_id),
                    )
                    paths = [
                        str(f.get("filename") or "")
                        for f in files
                        if isinstance(f, dict)
                    ]
                    layers_targeted = _classify_layers_from_paths(paths)
        except Exception as exc:
            logger.warning(
                "[REGRESSION-CASE] Synchronous layer classification failed for "
                "PR #%s project=%s: %s — backfill will retry",
                pr_number,
                task.project_id,
                exc,
            )

    case.mark_merged(
        pr_number,
        snapshot,
        layers_targeted=layers_targeted,
        spawn_was_cross_layer=spawn_was_cross_layer,
        spawn_layers_enumerated=spawn_layers_enumerated
        if spawn_was_cross_layer
        else None,
    )

    # Persist recommended_unmodifiable_layers from task context into the
    # fix-log entry so compliance accounting can credit documented
    # unmodifiable layers (e.g. CI_WORKFLOW) toward the >=2-layer mandate.
    if spawn_was_cross_layer:
        recommended_unmod = [
            layer
            for layer in (ctx.get("recommended_unmodifiable_layers") or [])
            if isinstance(layer, str)
        ]
        if recommended_unmod:
            log = list(case.fingerprint_to_fix_log or [])
            if (
                log
                and isinstance(log[-1], dict)
                and log[-1].get("fix_pr_number") == pr_number
            ):
                log[-1]["recommended_unmodifiable_layers"] = recommended_unmod
                case.fingerprint_to_fix_log = log

    session.add(case)


async def _close_loop_regression_case_on_failure(
    task: Task, session: AsyncSession
) -> None:
    """Re-arm or escalate a RegressionRemediationCase when its remediation task fails."""
    regression_class = await _regression_class_for_task(task, session)
    if not regression_class:
        return
    result = await session.execute(
        select(RegressionRemediationCase).where(
            RegressionRemediationCase.project_id == task.project_id,
            RegressionRemediationCase.regression_class == regression_class,
        )
    )
    case = result.scalar_one_or_none()
    if case is None:
        return
    try:
        case.last_failure_class = classify_failure(task)
    except Exception as exc:
        logger.warning(
            "classify_failure raised while capturing last_failure_class for case %s: %s",
            case.id,
            exc,
        )
    if task.latest_status_message:
        case.last_failure_summary = task.latest_status_message[:500]
    case.re_arm()
    session.add(case)


async def _process_post_commit(
    task_id: uuid.UUID,
    payload: AgentStatusPayload,
    _session_factory=None,
) -> dict:
    """Post-commit processing: review pipeline, completion handlers, rollup, aggregation.

    Uses its own session to avoid holding the FOR UPDATE lock from the
    status endpoint while making GitHub API calls (which can hang 15-120s).
    _session_factory is injectable for tests (defaults to AsyncSessionLocal).
    """
    from app.main import _utcnow
    from app.broadcast import broadcast_to_project
    from app.routers.agent import get_github_token_for_task
    from app.security import scan_pr_for_injection
    from app.github import (
        create_pr_comment,
        merge_pull_request,
        get_pull_request,
        get_pr_files,
        get_check_runs_cached,
        submit_pr_review,
        update_check_run,
        close_pull_request,
    )
    from app.task_completion import (
        _handle_plan_completion,
        _handle_design_doc_completion,
        _handle_security_audit_completion,
        _handle_perf_completion,
        _handle_self_audit_completion,
        _handle_investigation_completion,
        _handle_memo_completion,
        _record_remediation_if_terminal,
        _handle_investigation_terminal_failure,
        _record_regression_signal_if_matching,
    )
    from app.strategic_completion import (
        _handle_strategic_completion,
        _record_strategic_task_failure,
    )
    from app.research_completion import _handle_research_completion

    factory = _session_factory or AsyncSessionLocal
    async with factory() as session:
        # Round 3 C3 + C4: strategic tasks (design_doc_update, strategic_review)
        # have a downstream completion chain that needs both the Project and
        # the Task locked. The canonical order is **Project before Task,
        # always** (docs/concurrency.md). Locking Task first then Project
        # second reintroduces the cross-table deadlock vector C5 originally
        # closed, so we peek at the task type via a non-locking read and use
        # the canonical pair-locker for strategic types. Non-strategic types
        # use the existing task-only lock pattern.
        _peek_row = (
            await session.execute(
                select(Task.payloads, Task.project_id).where(Task.id == task_id)
            )
        ).first()
        if _peek_row is None:
            return {"status": "accepted"}
        _peek_payload_list = _peek_row[0] or []
        _peek_first = _peek_payload_list[0] if _peek_payload_list else {}
        _peek_type = _peek_first.get("type") if isinstance(_peek_first, dict) else None
        _peek_project_id = _peek_row[1]
        _is_strategic_type = _peek_type in ("design_doc_update", "strategic_review")

        # skip_locked + bounded retry: the row is typically only held briefly
        # (by the agent_status early-commit path that just released it, or by a
        # concurrent post-commit for the same task). A plain FOR UPDATE would
        # queue behind GitHub API calls (15-120s) and hit the Postgres
        # lock_timeout as LockNotAvailableError, which tears down the session.
        task = None
        # Round 3 C3 + C4: ``locked_project`` is set ONLY when the post-commit
        # is processing a strategic task and the canonical pair-lock succeeded.
        # The strategic-completion chain handlers below receive this via
        # ``project=`` so they skip their own (legacy) re-acquisition.
        locked_project: Project | None = None
        for _attempt in range(3):
            if _is_strategic_type and _peek_project_id is not None:
                # Canonical Project → Task ordering for strategic tasks. The
                # helper acquires Project first; if the task row is held it
                # yields ``(project, None)`` and we retry. If the project is
                # held it yields ``(None, None)`` and we retry as well.
                from app.task_locking import lock_project_then_task

                async with lock_project_then_task(
                    session,
                    _peek_project_id,
                    task_id,
                    skip_locked=True,
                ) as (_p, _t):
                    if _t is not None:
                        # Both locks acquired. Capture references and break.
                        # SQLAlchemy session-level FOR UPDATE locks persist
                        # past the contextmanager exit; the surrounding
                        # ``async with factory() as session`` commit/rollback
                        # at the end of post-commit releases both row locks.
                        task = _t
                        locked_project = _p
                        break
                    # Half-pair acquired (project but not task) or no pair
                    # (project also held). Release any held lock before
                    # retrying so we don't accumulate session-level locks
                    # across retry iterations and don't hold a project lock
                    # while a peer attempts the same task lock.
                    if _p is not None:
                        await session.rollback()
            else:
                task = (
                    (
                        await session.execute(
                            select(Task)
                            .where(Task.id == task_id)
                            .with_for_update(skip_locked=True)
                        )
                    )
                    .scalars()
                    .first()
                )
                if task is not None:
                    break
            # Confirm the row still exists before retrying — if it's gone the
            # peer holder is irrelevant.
            exists = (
                await session.execute(select(Task.id).where(Task.id == task_id))
            ).first()
            if exists is None:
                return {"status": "accepted"}
            await asyncio.sleep(2)
        if task is None:
            raise PostCommitLocked(f"task {task_id} row held by peer after 3 retries")

        # Phase 0 telemetry: persist warm-VM continuity timing fields into
        # task.context["_timing"] so cold-vs-warm setup cost is observable.
        # Mirrors the existing _shown_playbook_keys storage pattern in
        # routers/agent.py — write all non-None fields under a stable key,
        # respect prior _timing entries (e.g. claim_latency_secs from claim).
        _timing_payload: dict = {}
        if payload.setup_secs is not None:
            _timing_payload["setup_secs"] = float(payload.setup_secs)
        if payload.warm_start is not None:
            _timing_payload["warm_start"] = bool(payload.warm_start)
        if payload.prior_role_vm_id is not None:
            _timing_payload["prior_role_vm_id"] = str(payload.prior_role_vm_id)
        if payload.handler_duration_secs is not None:
            # Wave 5 follow-up: wall-time of the role's handler (post-claim,
            # post-setup). Combined with setup_secs this decomposes total
            # role time so cold-vs-warm gains can be attributed correctly.
            _timing_payload["handler_duration_secs"] = float(
                payload.handler_duration_secs
            )
        # Fallback: derive setup_secs from VM timestamps when the agent exits
        # before computing it (timeout, pre-tool crash). vm_provisioned_at is
        # set at orchestrator cold-provision time and reset to "now" on warm
        # claim, so the difference approximates cold-start boot latency without
        # polluting warm-start buckets. Only writes when not already present.
        if "setup_secs" not in _timing_payload:
            _prior_timing = dict((task.context or {}).get("_timing") or {})
            if (
                "setup_secs" not in _prior_timing
                and task.vm_provisioned_at is not None
                and task.vm_started_at is not None
            ):
                _derived = (task.vm_started_at - task.vm_provisioned_at).total_seconds()
                if _derived >= 0:
                    _timing_payload["setup_secs"] = round(_derived, 1)
        if _timing_payload:
            ctx = dict(task.context or {})
            existing = dict(ctx.get("_timing") or {})
            existing.update(_timing_payload)
            ctx["_timing"] = existing
            task.context = ctx
            flag_modified(task, "context")
            session.add(task)

        # Immediately unblock dependents when task terminally fails (retries exhausted).
        # Uses the same session (not a separate one) to avoid self-deadlock: a second
        # session scanning the task table would block on the FOR UPDATE lock held by
        # this session, causing a hang in Postgres (asyncio is single-threaded).
        if payload.status == TaskStatus.FAILED:
            try:
                dep_stmt = select(Task).where(
                    Task.project_id == task.project_id,
                    Task.status == TaskStatus.QUEUED,
                    Task.depends_on.contains([str(task.id)]),
                )
                dep_result = await session.execute(dep_stmt)
                waiting_tasks = dep_result.scalars().all()
                count = 0
                for wt in waiting_tasks:
                    wt.depends_on = [
                        d for d in (wt.depends_on or []) if d != str(task.id)
                    ]
                    session.add(wt)
                    count += 1
                if count:
                    # Don't commit here — let the session's final commit handle it.
                    # An intermediate commit would release the FOR UPDATE lock while
                    # failure-handling code below still needs the task.
                    logger.info(
                        f"[PIPELINE] Cleared terminal dependency {task.id} from {count} waiting tasks"
                    )
            except Exception as e:
                logger.error(
                    f"[PIPELINE] Failed to unblock dependents of {task.id}: {e}",
                    exc_info=True,
                )

            # Record strategic task failures for the circuit breaker and cycle history
            if task.first_payload.get("type") in (
                "design_doc_update",
                "strategic_review",
            ):
                try:
                    # Round 3 C3 + C4: the canonical pair-lock acquired at the
                    # top of _process_post_commit (Project before Task, see
                    # docs/concurrency.md) is passed down here as
                    # ``locked_project``. _record_strategic_task_failure skips
                    # its own re-acquisition when ``project=`` is provided.
                    # Falls back to None for legacy callers / non-strategic
                    # types (the helper does an unlocked session.get(Project)
                    # in that case — the failure-record path doesn't strictly
                    # require the project lock).
                    await _record_strategic_task_failure(
                        task, session, project=locked_project
                    )
                except Exception as _strat_exc:
                    logger.error(
                        f"[STRATEGIC] Failed to record strategic failure for {task.id}: {_strat_exc!r}",
                        exc_info=True,
                    )

            # Record research task failures so _check_direction_health can see
            # cycles that never reached _handle_research_completion (VM crash,
            # provision failure, agent timeout, etc.)
            if task.first_payload.get("type") == "research":
                try:
                    from app.research_completion import _record_research_task_failure

                    await _record_research_task_failure(task, session)
                except Exception as _res_exc:
                    logger.error(
                        f"[RESEARCH] Failed to record research failure for {task.id}: {_res_exc!r}",
                        exc_info=True,
                    )

            # Post result message for permanently failed auto-remediation tasks.
            # Routes to residual lifecycle for residual-fingerprint tasks,
            # CI auto-fix lifecycle otherwise.
            if task.auto_remediation:
                try:
                    await post_remediation_result_message(
                        session,
                        task,
                        result="failed",
                    )
                    await session.commit()
                except Exception as _ar_exc:
                    logger.warning(
                        "[AUTO-REMEDIATION] Failed to post failure result message: %s",
                        _ar_exc,
                    )

                # Spawn a follow-up investigation for the failed CI auto-fix.
                # The standard _spawn_task_failure_investigation path requires a
                # direction_id, which CI auto-remediation tasks lack — so trigger
                # the investigation directly here, deduped by branch+fingerprint
                # over a 24h window so a flapping check doesn't spawn N copies.
                try:
                    await _spawn_ci_auto_remediation_followup(task, session)
                except Exception as _ci_inv_exc:
                    logger.error(
                        f"[AUTO-REMEDIATION] Failed to spawn follow-up investigation for {task.id}: {_ci_inv_exc!r}",
                        exc_info=True,
                    )

            # Task-level CODE_ERROR investigation trigger: when a directed task
            # exhausts retries with a code_defect failure, spawn an investigate
            # task to diagnose the specific failure pattern. Direction-hold path
            # only fires after 3 consecutive failures on a direction; this fires
            # per-task so one-off recurring defects still get diagnosed.
            try:
                from app.autonomous_research import _spawn_task_failure_investigation

                await _spawn_task_failure_investigation(task, session)
            except Exception as _inv_exc:
                logger.error(
                    f"[INVESTIGATION] Failed to evaluate task-level trigger for {task.id}: {_inv_exc!r}",
                    exc_info=True,
                )

            # Close-loop: re-arm the RegressionRemediationCase when its fix task fails.
            try:
                await _close_loop_regression_case_on_failure(task, session)
            except Exception as _rc_fail_exc:
                logger.warning(
                    f"[REGRESSION-CASE] Failed to re-arm case on task failure {task.id}: {_rc_fail_exc!r}"
                )

        # Anti-poisoning: resolve playbook entries the task wrote while
        # IN_PROGRESS. On COMPLETED, clear pending_source so peers can see
        # them. On FAILED/CANCELLED, drop them entirely so a task that
        # never landed its code can't leave a "X exists" lie in the
        # playbook. Must run before commit so the design_doc mutation
        # is persisted alongside the rest of the post-commit work.
        if payload.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            try:
                from app.playbook_learning import finalize_pending_playbook_entries

                await finalize_pending_playbook_entries(
                    task,
                    session,
                    succeeded=(payload.status == TaskStatus.COMPLETED),
                )
            except Exception:
                logger.exception(
                    "[PLAYBOOK] Failed to finalise pending entries for task %s", task.id
                )

        # Audit 2026-04-25 A7/A12: release the FOR UPDATE row lock before the
        # review pipeline runs its slow GitHub calls (PR fetch, check-runs,
        # PR review submission, merge attempts — each can hit GitHub rate
        # limits and stretch into the tens of seconds, occasionally minutes).
        # Heartbeats arriving in that window would otherwise hit the locked
        # row, get None from skip_locked, and return 409. Multiple skipped
        # heartbeats over 5+ minutes would let the reaper false-orphan the
        # task. Commit here flushes all pending state mutations from sections
        # 1–4 above (dependent unblocking, failure recording, investigation  # noqa: RUF003
        # spawns) and releases the lock; the rest of post-commit processing
        # operates on the already-loaded task object without re-locking.
        if session.dirty or session.new:
            await session.commit()

        # 5. Review Pipeline — auto-spawn review/fix tasks based on completed task type
        if payload.status == TaskStatus.COMPLETED:
            task_payload = task.first_payload
            task_type = task_payload.get("type", "create")
            review_loop = (task.context or {}).get("review_loop", {})

            # Create task completed with PR → route based on target branch
            if (
                task_type in ("create", "refactor", "docs", "qa", "deps", "migrate", "")
                and payload.pr_url
                and payload.artifact_type == ArtifactType.PULL_REQUEST
            ):
                pr_number = int(payload.pr_url.rstrip("/").split("/")[-1])
                short_id = str(task.id).replace("-", "")[:8]
                branch = (
                    f"blueprint/docs-{short_id}"
                    if task_type == "docs"
                    else f"blueprint/deps-{short_id}"
                    if task_type == "deps"
                    else f"blueprint/migrate-{short_id}"
                    if task_type == "migrate"
                    else f"blueprint/refactor-{short_id}"
                    if task_type == "refactor"
                    else f"blueprint/task-{short_id}"
                )
                base_branch = task_payload.get("base_branch", "main")
                original_prompt = task_payload.get("prompt", task.description or "")
                is_intermediate = base_branch.startswith(
                    "research/"
                ) or base_branch.startswith("plan/")
                scope = (task.context or {}).get("scope", "unknown")

                # Auto-approve trivially small main-branch diffs: skip review and route
                # through the same security-scan + auto-merge path used for intermediate
                # small/medium scope. Gated by scope, direction acceptance history, and
                # diff size; CI still gates the merge.
                auto_approve_small = False
                if (
                    not is_intermediate
                    and scope == "small"
                    and _runtime_config.get("auto_approve_small_diff_enabled", True)
                ):
                    try:
                        max_lines = int(
                            _runtime_config.get("auto_approve_max_lines", 50)
                        )
                        max_files = int(
                            _runtime_config.get("auto_approve_max_files", 4)
                        )
                        _aa_tok, _aa_owner, _aa_repo = await get_github_token_for_task(
                            task.id, session
                        )
                        _aa_pr = await get_pull_request(
                            _aa_tok,
                            _aa_owner,
                            _aa_repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                        lines_changed = int(_aa_pr.get("additions", 0) or 0) + int(
                            _aa_pr.get("deletions", 0) or 0
                        )
                        files_changed = int(_aa_pr.get("changed_files", 0) or 0)
                        direction_id = (task.context or {}).get("direction_id")
                        acceptance = None
                        if direction_id:
                            _aa_proj = (
                                (
                                    await session.execute(
                                        select(Project).where(
                                            Project.id == task.project_id
                                        )
                                    )
                                )
                                .scalars()
                                .first()
                            )
                            _aa_dir = next(
                                (
                                    d
                                    for d in (
                                        getattr(_aa_proj, "directions", None) or []
                                    )
                                    if d.get("id") == direction_id
                                ),
                                None,
                            )
                            if _aa_dir:
                                from app.direction_metrics import (
                                    _compute_acceptance_rate,
                                )

                                acceptance = _compute_acceptance_rate(_aa_dir)
                        if (
                            0 < lines_changed <= max_lines
                            and 0 < files_changed <= max_files
                            and acceptance is not None
                            and acceptance > 0.5
                        ):
                            auto_approve_small = True
                            logger.info(
                                f"[PIPELINE] PR #{pr_number} auto-approved for merge "
                                f"({lines_changed} lines, {files_changed} files, acceptance={acceptance:.2f}) — skipping review"
                            )
                    except Exception as _aa_exc:
                        logger.info(
                            f"[PIPELINE] Auto-approve eligibility check failed for PR #{pr_number}: {_aa_exc!r} — defaulting to review"
                        )

                if (
                    is_intermediate and scope in ("small", "medium")
                ) or auto_approve_small:
                    # Small/medium scope on intermediate branch → security scan then auto-merge, skip review
                    # The rollup PR to main gets the full review — intermediate merges don't need it
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )

                        # Always run security scan before any merge
                        try:
                            scan_result = await scan_pr_for_injection(
                                gh_token, owner, repo, pr_number
                            )
                            logger.info(
                                f"[SECURITY] Small-scope scan: {scan_result['summary']}"
                            )
                            if scan_result["blocked"]:
                                task.requires_approval = True
                                task.latest_status_message = (
                                    f"Security scan blocked: {scan_result['summary']}"
                                )
                                session.add(task)
                                if task.auto_remediation:
                                    try:
                                        await post_remediation_result_message(
                                            session,
                                            task,
                                            result="review_needed",
                                            pr_number=pr_number,
                                        )
                                    except Exception as _ar_exc:
                                        logger.warning(
                                            "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                            _ar_exc,
                                        )
                                await session.commit()
                                if scan_result["findings"]:
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        scan_result["report"],
                                    )
                                return {"status": "accepted"}
                            if scan_result["findings"]:
                                await create_pr_comment(
                                    gh_token,
                                    owner,
                                    repo,
                                    pr_number,
                                    scan_result["report"],
                                )
                        except Exception as e:
                            if _is_transient_scan_error(e):
                                logger.warning(
                                    f"[SECURITY] Small-scope scan transient error for PR #{pr_number}, retrying: {e}"
                                )
                                await asyncio.sleep(5)
                                try:
                                    scan_result = await scan_pr_for_injection(
                                        gh_token, owner, repo, pr_number
                                    )
                                    logger.info(
                                        f"[SECURITY] Small-scope scan retry succeeded: {scan_result['summary']}"
                                    )
                                    if scan_result["blocked"]:
                                        task.requires_approval = True
                                        task.latest_status_message = f"Security scan blocked: {scan_result['summary']}"
                                        session.add(task)
                                        if task.auto_remediation:
                                            try:
                                                await post_remediation_result_message(
                                                    session,
                                                    task,
                                                    result="review_needed",
                                                    pr_number=pr_number,
                                                )
                                            except Exception as _ar_exc:
                                                logger.warning(
                                                    "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                                    _ar_exc,
                                                )
                                        await session.commit()
                                        if scan_result["findings"]:
                                            await create_pr_comment(
                                                gh_token,
                                                owner,
                                                repo,
                                                pr_number,
                                                scan_result["report"],
                                            )
                                        return {"status": "accepted"}
                                    if scan_result["findings"]:
                                        await create_pr_comment(
                                            gh_token,
                                            owner,
                                            repo,
                                            pr_number,
                                            scan_result["report"],
                                        )
                                except Exception as retry_exc:
                                    logger.error(
                                        f"[SECURITY] Small-scope scan retry also failed for PR #{pr_number}: {retry_exc}",
                                        exc_info=True,
                                    )
                                    task.requires_approval = True
                                    task.context = {
                                        **(task.context or {}),
                                        "scan_transient_failure": True,
                                    }
                                    task.latest_status_message = f"Security scan unavailable (infrastructure error) — requires manual approval: {type(retry_exc).__name__}: {retry_exc!r}"[
                                        :500
                                    ]
                                    session.add(task)
                                    if task.auto_remediation:
                                        try:
                                            await post_remediation_result_message(
                                                session,
                                                task,
                                                result="review_needed",
                                                pr_number=pr_number,
                                            )
                                        except Exception as _ar_exc:
                                            logger.warning(
                                                "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                                _ar_exc,
                                            )
                                    await session.commit()
                                    return {"status": "accepted"}
                            else:
                                logger.error(
                                    f"[SECURITY] Small-scope scan failed for PR #{pr_number}, blocking: {e}",
                                    exc_info=True,
                                )
                                task.requires_approval = True
                                task.latest_status_message = f"Security scan failed — requires manual approval: {type(e).__name__}: {e!r}"[
                                    :500
                                ]
                                session.add(task)
                                if task.auto_remediation:
                                    try:
                                        await post_remediation_result_message(
                                            session,
                                            task,
                                            result="review_needed",
                                            pr_number=pr_number,
                                        )
                                    except Exception as _ar_exc:
                                        logger.warning(
                                            "[AUTO-REMEDIATION] Failed to post review_needed message: %s",
                                            _ar_exc,
                                        )
                                await session.commit()
                                return {"status": "accepted"}

                        merge_result = await _merge_with_retry(
                            merge_pull_request,
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                        if merge_result.get("merged"):
                            task.context = {**(task.context or {}), "pr_merged": True}
                            session.add(task)
                            _t_ctx = task.context or {}
                            # Audit W1: auto_remediation is now a column.
                            if _t_ctx.get("diagnosis_task_id") or (
                                task.auto_remediation and _t_ctx.get("direction_id")
                            ):
                                try:
                                    from app.task_completion import (
                                        _record_remediation_outcome,
                                    )

                                    await _record_remediation_outcome(
                                        task, merged=True, session=session
                                    )
                                except Exception as _rr_exc:
                                    logger.warning(
                                        f"[DIAGNOSIS] Failed to record remediation outcome on merge for task {task.id}: {_rr_exc!r}"
                                    )
                            try:
                                await _close_loop_regression_case_on_merge(
                                    task, pr_number, session
                                )
                            except Exception as _rc_merge_exc:
                                logger.warning(
                                    f"[REGRESSION-CASE] Failed to advance case on auto-merge for task {task.id}: {_rc_merge_exc!r}"
                                )
                            await session.commit()
                            await _cleanup_branches(task, session)
                            logger.info(
                                f"[PIPELINE] Auto-merged PR #{pr_number} into {base_branch} (small scope, intermediate)"
                            )
                        else:
                            logger.error(
                                f"[PIPELINE] Auto-merge failed for PR #{pr_number}: {merge_result.get('message')}"
                            )
                            # Probe GitHub for the PR's mergeable_state so we can
                            # route the recovery correctly:
                            #   blocked / unstable → CI gating; queue GitHub auto-merge
                            #     and let it fire when checks clear.
                            #   dirty → real merge conflict (sibling cluster touched
                            #     overlapping files between PR creation and the merge
                            #     attempt). Spawn a `fix` task — fix is the rebase-
                            #     and-resolve primitive; review is the assess-and-
                            #     give-feedback primitive, the wrong tool here.
                            #     Without this, cluster sub-PRs orphan: tasks COMPLETE,
                            #     the dep chain stays QUEUED on PR_MERGED, and the
                            #     research rollup stalls (see PR#4070, PR#4044 incident
                            #     on 2026-05-06).
                            #   default → fall back to review-spawn (preserves
                            #     existing behavior for unknown states).
                            #
                            # Reviews still fire downstream: when the fix task
                            # COMPLETES, the fix-completion handler at line ~2899
                            # spawns a review task before merging. So a DIRTY-routed
                            # cluster sub-PR gets: create → conflict → fix (rebase)
                            # → review → auto-merge. This change does not bypass
                            # review for tasks that need it.
                            recovery_type = "review"
                            try:
                                from app.github import enable_auto_merge

                                pr_detail = await get_pull_request(
                                    gh_token,
                                    owner,
                                    repo,
                                    pr_number,
                                    project_id=str(task.project_id),
                                )
                                ms = (pr_detail.get("mergeable_state") or "").lower()
                                if ms in ("blocked", "unstable"):
                                    am_result = await enable_auto_merge(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        project_id=str(task.project_id),
                                    )
                                    if am_result.get("enabled"):
                                        logger.info(
                                            f"[PIPELINE] Enabled GitHub auto-merge on PR #{pr_number} (mergeable_state={ms})"
                                        )
                                    else:
                                        logger.info(
                                            f"[PIPELINE] Auto-merge enablement declined for PR #{pr_number}: {am_result.get('message')}"
                                        )
                                elif ms == "dirty":
                                    recovery_type = "fix"
                            except Exception as _am_exc:
                                logger.warning(
                                    f"[PIPELINE] Mergeable-state probe failed for PR #{pr_number}: {_am_exc}"
                                )
                            # Spawn the recovery task. For DIRTY conflicts on cluster
                            # sub-PRs we use `fix` (rebase + resolve); otherwise
                            # `review` (the existing default).
                            #
                            # Pass fix_reason="merge_conflict" so the fix-completion
                            # handler can route straight to security-scan + auto-merge
                            # WITHOUT a re-review. Rationale (per the small/medium
                            # intermediate path): the original task was scope-gated
                            # to skip review entirely; once the conflict is resolved
                            # there's no review-worthy logic change to re-assess —
                            # rebase output is mechanical. This keeps the cost model
                            # symmetric with the no-conflict happy path.
                            spawn_kwargs = {
                                "original_prompt": original_prompt,
                                "original_task_id": str(task.id),
                            }
                            if recovery_type == "fix":
                                spawn_kwargs["fix_reason"] = "merge_conflict"
                                spawn_kwargs["fix_prompt"] = (
                                    f"PR #{pr_number} cannot merge into `{base_branch}` due to a merge conflict "
                                    f"(GitHub mergeable_state='dirty'). The code review is already approved — "
                                    f"focus only on resolving the conflict.\n\n"
                                    f"After cloning:\n"
                                    f"  1. Run `git fetch origin {base_branch} && git rebase origin/{base_branch}`.\n"
                                    f"  2. Resolve every conflict marker — preserve the intent of both branches.\n"
                                    f"  3. Re-run lint/type/test suites for the affected files.\n"
                                    f"  4. Commit the resolution and force-push with `--force-with-lease`."
                                )
                            recovery_task, was_created = await _spawn_pipeline_task(
                                task,
                                recovery_type,
                                session,
                                pr_number,
                                branch,
                                base_branch,
                                **spawn_kwargs,
                            )
                            if was_created:
                                logger.info(
                                    f"[PIPELINE] Spawned {recovery_type} task {recovery_task.id} for PR #{pr_number} after auto-merge failure"
                                )
                            elif recovery_task is not None:
                                logger.info(
                                    f"[PIPELINE] Auto-merge failed for PR #{pr_number} — same-chain {recovery_task.first_payload.get('type')} task {recovery_task.id} ({recovery_task.status}) already handling it"
                                )
                            else:
                                logger.warning(
                                    f"[PIPELINE] Auto-merge failed and no {recovery_type} spawned for PR #{pr_number} (cap hit or guard) — parking in CI-wait"
                                )
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = f"Auto-merge failed for PR #{pr_number} — parked for CI-wait retry"
                                task.context = {
                                    **(task.context or {}),
                                    "review_loop": {
                                        "ci_waiting": True,
                                        "pr_number": pr_number,
                                        "branch": branch,
                                        "base_branch": base_branch,
                                        "original_task_id": str(task.id),
                                        "ci_wait_started": _utcnow().isoformat(),
                                    },
                                }
                                session.add(task)
                                await session.commit()
                    except Exception as e:
                        logger.error(
                            f"[PIPELINE] Auto-merge error for PR #{pr_number}: {e}",
                            exc_info=True,
                        )
                        # Fallback: spawn review so the pipeline doesn't die
                        try:
                            (
                                fallback_review,
                                fb_was_created,
                            ) = await _spawn_pipeline_task(
                                task,
                                "review",
                                session,
                                pr_number,
                                branch,
                                base_branch,
                                original_prompt=original_prompt,
                                original_task_id=str(task.id),
                            )
                            if fb_was_created:
                                logger.info(
                                    f"[PIPELINE] Spawned fallback review {fallback_review.id} for PR #{pr_number} after merge error"
                                )
                            elif fallback_review is not None:
                                logger.info(
                                    f"[PIPELINE] Merge errored for PR #{pr_number} — same-chain task {fallback_review.id} already handling it"
                                )
                            else:
                                logger.warning(
                                    f"[PIPELINE] Merge error and no fallback review spawned for PR #{pr_number} — parking in CI-wait"
                                )
                                task.status = TaskStatus.EVALUATING
                                task.latest_status_message = f"Auto-merge error for PR #{pr_number} — parked for CI-wait retry"
                                task.context = {
                                    **(task.context or {}),
                                    "review_loop": {
                                        "ci_waiting": True,
                                        "pr_number": pr_number,
                                        "branch": branch,
                                        "base_branch": base_branch,
                                        "original_task_id": str(task.id),
                                        "ci_wait_started": _utcnow().isoformat(),
                                    },
                                }
                                session.add(task)
                                await session.commit()
                        except Exception as inner_e:
                            logger.error(
                                f"[PIPELINE] Fallback review spawn failed for PR #{pr_number}: {inner_e}",
                                exc_info=True,
                            )
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = f"Pipeline recovery: merge and review spawn failed for PR #{pr_number} — CI monitor will retry"
                            task.context = {
                                **(task.context or {}),
                                "review_loop": {
                                    "ci_waiting": True,
                                    "pr_number": pr_number,
                                    "branch": branch,
                                    "base_branch": base_branch,
                                    "original_task_id": str(task.id),
                                    "ci_wait_started": _utcnow().isoformat(),
                                },
                            }
                            session.add(task)
                            await session.commit()
                else:
                    # Full pipeline: security scan → review (for main targets or medium+ scope)
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        scan_result = await scan_pr_for_injection(
                            gh_token, owner, repo, pr_number
                        )
                        logger.info(f"[SECURITY] {scan_result['summary']}")
                        if scan_result["blocked"]:
                            task.requires_approval = True
                            task.latest_status_message = scan_result["summary"]
                            session.add(task)
                            await session.commit()
                        if scan_result["findings"]:
                            await create_pr_comment(
                                gh_token, owner, repo, pr_number, scan_result["report"]
                            )
                    except Exception as e:
                        if _is_transient_scan_error(e):
                            logger.warning(
                                f"[SECURITY] Scan transient error for PR #{pr_number}, retrying: {e}"
                            )
                            await asyncio.sleep(5)
                            try:
                                scan_result = await scan_pr_for_injection(
                                    gh_token, owner, repo, pr_number
                                )
                                logger.info(
                                    f"[SECURITY] Scan retry succeeded: {scan_result['summary']}"
                                )
                                if scan_result["blocked"]:
                                    task.requires_approval = True
                                    task.latest_status_message = scan_result["summary"]
                                    session.add(task)
                                    await session.commit()
                                if scan_result["findings"]:
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        scan_result["report"],
                                    )
                            except Exception as retry_exc:
                                logger.error(
                                    f"[SECURITY] Scan retry also failed for PR #{pr_number}: {retry_exc}",
                                    exc_info=True,
                                )
                                task.requires_approval = True
                                task.context = {
                                    **(task.context or {}),
                                    "scan_transient_failure": True,
                                }
                                task.latest_status_message = f"Security scan unavailable (infrastructure error) — review will proceed but merge requires manual approval: {type(retry_exc).__name__}: {retry_exc!r}"[
                                    :500
                                ]
                                session.add(task)
                                await session.commit()
                        else:
                            logger.error(
                                f"[SECURITY] Scan failed for PR #{pr_number}, flagging for approval: {e}",
                                exc_info=True,
                            )
                            task.requires_approval = True
                            task.latest_status_message = f"Security scan failed — review will proceed but merge requires manual approval: {type(e).__name__}: {e!r}"[
                                :500
                            ]
                            session.add(task)
                            await session.commit()

                    # PR file-path flagging: flag infrastructure/security-critical file changes
                    # Don't block here — let review run, block at merge time.
                    # Gated by require_approval_gates — these paths are hardcoded for the
                    # Blueprint repo and irrelevant for other projects.
                    if _runtime_config.get("require_approval_gates", True):
                        try:
                            pr_files = await get_pr_files(
                                gh_token, owner, repo, pr_number
                            )
                            INFRA_FILES = {
                                "backend/app/main.py",
                                "backend/app/compute.py",
                                "backend/app/models.py",
                                "backend/app/database.py",
                                "backend/app/security.py",
                                "backend/app/auth.py",
                                "agent/src/index.ts",
                                "CLAUDE.md",
                                # Phase 4 trusted core — also in CRITICAL_FILES.
                                "backend/promotion_policy.yaml",
                            }
                            CRITICAL_FILES = {
                                "backend/app/security.py",
                                "backend/app/auth.py",
                                "CLAUDE.md",
                                # The release-qualifier promotion rubric — the
                                # human-owned trusted core. Autonomous changes to
                                # it must be human-approved before merge.
                                "backend/promotion_policy.yaml",
                            }
                            touched_infra = [
                                f["filename"]
                                for f in pr_files
                                if f["filename"] in INFRA_FILES
                            ]
                            touched_critical = [
                                f for f in touched_infra if f in CRITICAL_FILES
                            ]
                            if touched_infra:
                                task.context = {
                                    **(task.context or {}),
                                    "extended_monitoring": True,
                                    "risk_tier": "large",
                                    "touched_infra": touched_infra,
                                    "touched_critical": touched_critical,
                                }
                                task.latest_status_message = f"Infrastructure change flagged: {', '.join(touched_infra)}"
                                logger.info(
                                    f"[PIPELINE] PR #{pr_number} touches infra files: {touched_infra} (critical: {touched_critical})"
                                )
                                session.add(task)
                                await session.commit()
                                _shared_state.deploy_monitoring["is_extended"] = True
                        except Exception as e:
                            logger.info(
                                f"[PIPELINE] Could not check PR files for infra flagging: {e}"
                            )

                    # Always spawn the review — requires_approval gates MERGING
                    # (checked at merge time in the review-complete path), not
                    # reviewing. A security scan failure or blocking finding
                    # should not orphan the PR by preventing review entirely.
                    review_task, _ = await _spawn_pipeline_task(
                        task,
                        "review",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        original_prompt=original_prompt,
                        original_task_id=str(task.id),
                    )
                    if review_task:
                        if task.requires_approval:
                            logger.info(
                                f"[REVIEW PIPELINE] Spawned review for flagged PR #{pr_number} → task {review_task.id} (merge will require manual approval)"
                            )
                        else:
                            logger.info(
                                f"[REVIEW PIPELINE] Spawned review for PR #{pr_number} → task {review_task.id}"
                            )
                    else:
                        task.latest_status_message = f"Review not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Review spawn returned None for PR #{pr_number}"
                        )

            # Review task completed → parse verdict
            elif task_type == "review":
                pr_number = review_loop.get("pr_number") or task_payload.get(
                    "pr_number"
                )
                branch = review_loop.get("branch") or task_payload.get("branch")
                base_branch = review_loop.get("base_branch") or task_payload.get(
                    "base_branch", "main"
                )
                original_task_id = review_loop.get(
                    "original_task_id", ""
                ) or task_payload.get("original_task_id", "")
                original_prompt = task_payload.get("original_prompt", "")

                if not pr_number or not branch:
                    logger.info(
                        f"[REVIEW PIPELINE] Review task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline"
                    )
                    task.latest_status_message = (
                        "Review pipeline error: missing pr_number or branch"
                    )
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip review pipeline if PR is already merged or closed
                try:
                    (
                        gh_token_check,
                        owner_check,
                        repo_check,
                    ) = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(
                        gh_token_check, owner_check, repo_check, pr_number
                    )
                    pr_state = pr_data_check.get("state", "open")
                    if pr_state == "closed" or pr_data_check.get("merged"):
                        merged_label = (
                            "merged" if pr_data_check.get("merged") else "closed"
                        )
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} already {merged_label} — skipping review pipeline for task {task.id}"
                        )
                        task.latest_status_message = (
                            f"PR #{pr_number} already {merged_label} — review skipped"
                        )
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                except Exception as e:
                    logger.info(
                        f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding with review"
                    )

                verdict_text = payload.logs or task.latest_status_message or ""
                verdict = "REQUEST_CHANGES"
                feedback = verdict_text
                issues = []
                investigated_files: list[str] = []

                # Parse verdict: prefer structured verdict_json field, fall back to log parsing
                import re

                verdict_parsed = False
                verdict_unparseable = False

                # Method 1: verdict_json field (from /tmp/verdict.json on the agent)
                if payload.verdict_json:
                    try:
                        vj = json.loads(payload.verdict_json)
                        if vj.get("verdict"):
                            verdict = vj["verdict"]
                            feedback = vj.get("feedback", verdict_text)
                            issues = (
                                vj.get("issues", [])
                                if isinstance(vj.get("issues"), list)
                                else []
                            )
                            raw_investigated = vj.get("investigated_files")
                            if isinstance(raw_investigated, list):
                                investigated_files = [
                                    str(p)
                                    for p in raw_investigated
                                    if isinstance(p, str) and p
                                ][:25]
                            verdict_parsed = True
                            logger.info(
                                f"[REVIEW PIPELINE] Verdict from verdict_json field: {verdict}"
                            )
                    except (json.JSONDecodeError, TypeError):
                        logger.exception(
                            f"[REVIEW PIPELINE] verdict_json field present but invalid for task {task.id}"
                        )

                # Method 2: Parse JSON from log output (string-aware bracket counting)
                if not verdict_parsed:
                    try:
                        for m in re.finditer(r'\{[^{}]*"verdict"', verdict_text):
                            start = m.start()
                            depth = 0
                            in_string = False
                            escape_next = False
                            for i in range(start, len(verdict_text)):
                                c = verdict_text[i]
                                if escape_next:
                                    escape_next = False
                                    continue
                                if c == "\\" and in_string:
                                    escape_next = True
                                    continue
                                if c == '"' and not escape_next:
                                    in_string = not in_string
                                    continue
                                if in_string:
                                    continue
                                if c == "{":
                                    depth += 1
                                elif c == "}":
                                    depth -= 1
                                    if depth == 0:
                                        try:
                                            candidate = json.loads(
                                                verdict_text[start : i + 1]
                                            )
                                            if "verdict" in candidate:
                                                verdict = candidate["verdict"]
                                                feedback = candidate.get(
                                                    "feedback", verdict_text
                                                )
                                                issues = (
                                                    candidate.get("issues", [])
                                                    if isinstance(
                                                        candidate.get("issues"), list
                                                    )
                                                    else []
                                                )
                                                verdict_parsed = True
                                        except json.JSONDecodeError:
                                            pass
                                        break
                    except Exception as e:
                        logger.error(
                            f"[REVIEW PIPELINE] Verdict parsing error for task {task.id}: {e}",
                            exc_info=True,
                        )

                if not verdict_parsed:
                    # Fallback: look for APPROVE / REQUEST_CHANGES as standalone words
                    # (quoted or unquoted). COMMENT is no longer a valid verdict — a
                    # stray occurrence in prose is ignored so the text falls through
                    # to REQUEST_CHANGES (the safe default).
                    upper_text = verdict_text.upper()
                    approve_idx = max(
                        upper_text.rfind('"APPROVE"'),
                        upper_text.rfind("APPROVE"),
                        upper_text.rfind("APPROVED"),
                    )
                    rc_idx = max(
                        upper_text.rfind('"REQUEST_CHANGES"'),
                        upper_text.rfind("REQUEST_CHANGES"),
                        upper_text.rfind("REQUEST CHANGES"),
                    )
                    if approve_idx == -1 and rc_idx == -1:
                        verdict = "REQUEST_CHANGES"
                        feedback = "Review agent did not produce a verdict — manual review required"
                        logger.warning(
                            f"[REVIEW PIPELINE] No verdict found for task {task.id} (len={len(verdict_text)}) — defaulting to REQUEST_CHANGES, marking for manual review"
                        )
                        verdict_unparseable = True
                    elif approve_idx > rc_idx:
                        verdict = "APPROVE"
                    else:
                        verdict = "REQUEST_CHANGES"

                # Invariant: pipeline accepts only APPROVE or REQUEST_CHANGES.
                # Legacy COMMENT, typos, and other agent regressions normalize to
                # REQUEST_CHANGES so the pipeline always has a well-defined next step.
                if verdict not in ("APPROVE", "REQUEST_CHANGES"):
                    logger.warning(
                        f"[REVIEW PIPELINE] Task {task.id} produced unsupported verdict "
                        f"{verdict!r} — normalizing to REQUEST_CHANGES"
                    )
                    verdict = "REQUEST_CHANGES"

                # If verdict was unparseable, flag the original task for manual review
                if verdict_unparseable:
                    source_loop = (task.context or {}).get("review_loop", {})
                    original_task_id = source_loop.get("original_task_id")
                    if original_task_id:
                        try:
                            orig = await session.get(Task, uuid.UUID(original_task_id))
                            if orig:
                                orig.requires_approval = True
                                orig.latest_status_message = "Review agent did not produce a verdict — manual review required"
                                session.add(orig)
                                await session.commit()
                        except Exception as e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to mark requires_approval on {original_task_id}: {e}",
                                exc_info=True,
                            )

                # Post the review to the PR via GitHub Reviews API (falls back to plain comment)
                try:
                    gh_token, owner, repo = await get_github_token_for_task(
                        task.id, session
                    )
                    event = "APPROVE" if verdict == "APPROVE" else "REQUEST_CHANGES"
                    inline_comments = []
                    body_issues = []
                    for issue in issues:
                        if issue.get("file") and issue.get("line"):
                            sev = issue.get("severity", "minor").upper()
                            inline_comments.append(
                                {
                                    "path": issue["file"],
                                    "line": issue["line"],
                                    "body": f"[{sev}] {issue.get('description', '')}",
                                }
                            )
                        else:
                            body_issues.append(issue)
                    review_body = (
                        f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                    )
                    if body_issues:
                        issues_text = "\n".join(
                            f"- [{i.get('severity', '?').upper()}] {i.get('description', '')}"
                            for i in body_issues
                        )
                        review_body += f"\n\n**Issues:**\n{issues_text}"
                    result = await submit_pr_review(
                        gh_token,
                        owner,
                        repo,
                        pr_number,
                        event,
                        review_body,
                        inline_comments,
                    )
                    if result is None:
                        raise ValueError("PR not found")
                except Exception as e:
                    logger.warning(
                        f"[REVIEW PIPELINE] submit_pr_review failed, falling back: {e}"
                    )
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        review_comment = f"**Blueprint Code Review** ({verdict})\n\n{feedback[:3000]}"
                        await create_pr_comment(
                            gh_token, owner, repo, pr_number, review_comment
                        )
                    except Exception as e2:
                        logger.warning(
                            f"[REVIEW PIPELINE] Fallback comment also failed: {e2}",
                            exc_info=True,
                        )

                # Update Check Run with verdict conclusion (best-effort — errors are swallowed)
                check_run_id = (task.context or {}).get("check_run_id")
                if check_run_id:
                    try:
                        (
                            _cr_token,
                            _cr_owner,
                            _cr_repo,
                        ) = await get_github_token_for_task(task.id, session)
                        _severity_map = {
                            "critical": "failure",
                            "major": "warning",
                            "minor": "notice",
                        }
                        _conclusion = (
                            "success" if verdict == "APPROVE" else "action_required"
                        )
                        _annotations = []
                        for _issue in issues:
                            if _issue.get("file") and _issue.get("line"):
                                _lvl = _severity_map.get(
                                    _issue.get("severity", "minor"), "notice"
                                )
                                _ln = int(_issue["line"])
                                _annotations.append(
                                    {
                                        "path": _issue["file"],
                                        "start_line": _ln,
                                        "end_line": _ln,
                                        "annotation_level": _lvl,
                                        "message": _issue.get("description", ""),
                                    }
                                )
                        _issue_counts = {}
                        for _issue in issues:
                            _sev = _issue.get("severity", "minor")
                            _issue_counts[_sev] = _issue_counts.get(_sev, 0) + 1
                        _counts_line = (
                            ", ".join(
                                f"{v} {k}" for k, v in sorted(_issue_counts.items())
                            )
                            if _issue_counts
                            else "no issues"
                        )
                        _cr_summary = (
                            f"**Blueprint Code Review**: {verdict}\n\n{feedback[:3000]}"
                        )
                        if _issue_counts:
                            _cr_summary += f"\n\n**Issue summary**: {_counts_line}"
                        await update_check_run(
                            _cr_token,
                            _cr_owner,
                            _cr_repo,
                            check_run_id,
                            conclusion=_conclusion,
                            title=f"Blueprint Code Review: {verdict}",
                            summary=_cr_summary,
                            annotations=_annotations or None,
                        )
                        logger.info(
                            f"[CHECK RUN] Updated check run {check_run_id} → {_conclusion} for task {task.id}"
                        )
                    except Exception as _cr_exc:
                        logger.warning(
                            f"[CHECK RUN] Failed to update check run {check_run_id} for task {task.id}: {_cr_exc}"
                        )

                # Store Blueprint's verdict on the review task context so the
                # pull_request_review webhook handler can correlate quality signals.
                task.context = {
                    **(task.context or {}),
                    "blueprint_review_verdict": verdict,
                }
                session.add(task)

                action_taken = False
                head_sha = ""
                if verdict == "APPROVE":
                    # Rollup gate handoff: when the original task is parking in CI wait
                    # (rollup-to-main / large rollup path in rollup_management), the parent
                    # owns the merge gate. The review task must NOT merge directly — it
                    # writes review_approved=True on the parent and lets the CI gate fire
                    # the merge once both signals are in. Without this branch, the review
                    # races the parent's CI poll and whichever finishes first wins, which
                    # is exactly the "merged before review" bug.
                    if original_task_id:
                        try:
                            from app.ci_completion import (
                                _attempt_merge_with_retry_and_escalation,
                            )
                            from app.routers.agent import get_github_token_for_task

                            # ── Site 2 lock-during-IO fix ──
                            # Previously we acquired FOR UPDATE on `_orig`
                            # unconditionally, then only the
                            # `if _orig and _orig_loop.get("ci_waiting"):` branch
                            # consumed the lock. The fall-through (review
                            # task's own merge cluster: get_pull_request,
                            # create_pr_comment, get_check_runs, get_pr_files,
                            # merge_pull_request, delete_branch — 60-120s) ran
                            # with `_orig`'s row lock still held by this
                            # session, blocking concurrent CI/heartbeat writes
                            # on the rollup parent. Fix: do a non-locking read
                            # to peek at ci_waiting first, and only acquire
                            # FOR UPDATE when we're committing to the rollup-
                            # handoff branch.
                            _orig_peek = (
                                (
                                    await session.execute(
                                        select(Task.context).where(
                                            Task.id == uuid.UUID(original_task_id)
                                        )
                                    )
                                )
                                .scalars()
                                .first()
                            )
                            _orig_peek_loop = (
                                (_orig_peek or {}).get("review_loop", {})
                                if _orig_peek
                                else {}
                            )
                            if _orig_peek_loop.get("ci_waiting"):
                                # Phase A1: skip_locked + bounded retry. If the parent
                                # task row is held by a peer, defer the handoff —
                                # the parent's own poll will pick up review_approved
                                # on the next iteration once the lock clears.
                                _orig = None
                                for _attempt in range(3):
                                    _orig_q = (
                                        select(Task)
                                        .where(Task.id == uuid.UUID(original_task_id))
                                        .with_for_update(skip_locked=True)
                                    )
                                    _orig = (
                                        (await session.execute(_orig_q))
                                        .scalars()
                                        .first()
                                    )
                                    if _orig is not None:
                                        break
                                    _orig_exists = (
                                        await session.execute(
                                            select(Task.id).where(
                                                Task.id == uuid.UUID(original_task_id)
                                            )
                                        )
                                    ).first()
                                    if _orig_exists is None:
                                        break  # Genuinely missing
                                    await asyncio.sleep(0.5 * (_attempt + 1))
                                # Re-read the loop from the locked row — peer may
                                # have flipped ci_waiting in the gap between the
                                # peek and the lock acquisition.
                                _orig_loop = (
                                    (_orig.context or {}).get("review_loop", {})
                                    if _orig
                                    else {}
                                )
                                if _orig and _orig_loop.get("ci_waiting"):
                                    _orig_loop["review_approved"] = True
                                    _orig.context = {
                                        **(_orig.context or {}),
                                        "review_loop": _orig_loop,
                                    }
                                    flag_modified(_orig, "context")
                                    session.add(_orig)
                                    task.status = TaskStatus.COMPLETED
                                    task.latest_status_message = f"Review approved for rollup PR #{pr_number} — handed off to CI gate"
                                    session.add(task)

                                    if _orig_loop.get("ci_passed"):
                                        try:
                                            (
                                                _gh,
                                                _own,
                                                _rep,
                                            ) = await get_github_token_for_task(
                                                _orig.id, session
                                            )
                                            await _attempt_merge_with_retry_and_escalation(
                                                _orig,
                                                _gh,
                                                _own,
                                                _rep,
                                                session,
                                                source="review_approval",
                                            )
                                        except Exception as _gate_e:
                                            logger.error(
                                                f"[REVIEW PIPELINE] Gate merge failed for rollup PR #{pr_number}: {_gate_e}",
                                                exc_info=True,
                                            )

                                    await session.commit()
                                    await broadcast_to_project(
                                        str(_orig.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(_orig.id),
                                            "status": _orig.status,
                                        },
                                    )
                                    logger.info(
                                        f"[REVIEW PIPELINE] Rollup PR #{pr_number}: review approved, handed to CI gate (parent {_orig.id})"
                                    )
                                    return {"status": "accepted"}
                        except Exception as _rollup_e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Rollup gate handoff failed for PR #{pr_number}: {_rollup_e}",
                                exc_info=True,
                            )

                    head_sha = ""
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        # Validate PR head branch matches before merging
                        pr_data = await get_pull_request(
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                        pr_head = pr_data.get("head", {}).get("ref", "")
                        if pr_head and pr_head != branch:
                            logger.info(
                                f"[REVIEW PIPELINE] Branch mismatch: reviewed '{branch}' but PR #{pr_number} head is '{pr_head}' — skipping merge"
                            )
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint Review Pipeline**: Merge aborted — reviewed branch `{branch}` does not match PR head `{pr_head}`.",
                            )
                            task.latest_status_message = f"Branch mismatch: reviewed {branch}, PR head is {pr_head}"
                            session.add(task)
                            await session.commit()
                            raise ValueError(f"Branch mismatch: {branch} vs {pr_head}")

                        # Check CI status before merging the integration trunk.
                        #
                        # Pre-Phase-1 the integration branch IS "main", so this
                        # was historically written as `base_branch == "main"`.
                        # After the automain/LTS Phase 1 cutover rollup PRs
                        # target `automain` (the resolved integration branch),
                        # and a hard-coded "main" check would skip the pre-merge
                        # CI gate for every rollup — the orchestrator would merge
                        # rollups without waiting for CI.
                        #
                        # Gate on the resolved integration branch so the CI
                        # check runs for whichever branch the pipeline currently
                        # integrates onto. "main" is ALSO kept gated regardless:
                        # post-cutover it is the promotion / hotfix-rollup
                        # target, and a merge to main must never bypass CI.
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        _integration_branch = _runtime_config.get(
                            "integration_branch", "main"
                        )
                        is_ci_gated_merge = base_branch in (
                            _integration_branch,
                            "main",
                        )
                        ci_status = (
                            await get_check_runs_cached(
                                gh_token,
                                owner,
                                repo,
                                head_sha,
                                project_id=str(task.project_id),
                            )
                            if (head_sha and is_ci_gated_merge)
                            else None
                        )

                        if ci_status and ci_status["status"] != "completed":
                            # CI still running — park task in EVALUATING (no VM consumed)
                            try:
                                _approve_files = [
                                    f.get("filename", "")
                                    for f in (
                                        await get_pr_files(
                                            gh_token, owner, repo, pr_number
                                        )
                                    )
                                    if isinstance(f, dict) and f.get("filename")
                                ]
                            except Exception as _af_e:
                                logger.warning(
                                    f"[REVIEW PIPELINE] APPROVE entry: failed to fetch PR files for #{pr_number}: {_af_e}"
                                )
                                _approve_files = []
                            _approved_history = _append_approve_to_review_history(
                                review_loop, _approve_files
                            )
                            task.status = TaskStatus.EVALUATING
                            task.latest_status_message = (
                                f"Review approved, waiting for CI on PR #{pr_number}..."
                            )
                            task.context = {
                                **(task.context or {}),
                                "review_loop": {
                                    **review_loop,
                                    "ci_waiting": True,
                                    "review_approved": True,
                                    "review_history": _approved_history,
                                    "head_sha": head_sha,
                                    "pr_number": pr_number,
                                    "branch": branch,
                                    "base_branch": base_branch,
                                    "original_task_id": original_task_id,
                                    "ci_wait_started": _utcnow().isoformat(),
                                },
                            }
                            session.add(task)
                            await session.commit()
                            logger.info(
                                f"[CI] PR #{pr_number} approved, CI pending — waiting"
                            )
                            action_taken = True
                            return {"status": "accepted"}

                        if ci_status and ci_status["conclusion"] == "failure":
                            # CI failed — spawn fix with CI errors
                            ci_errors = (
                                ci_status["failed_logs"]
                                or "CI checks failed (no details available)"
                            )
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint CI**: Checks failed — spawning fix task.\n\n```\n{ci_errors[:2000]}\n```",
                            )
                            ci_fix_prompt = (
                                f"CI checks failed on PR #{pr_number} (code review already approved).\n\n"
                                f"CI errors:\n{ci_errors}\n\n"
                                f"Fix only the build/lint/test errors. The code review is already "
                                f"approved — don't change logic, just fix CI failures."
                            )
                            # Phase 2d: APPROVE branch must NOT propagate warm-VM
                            # continuity onto downstream spawns. The reviewer's VM
                            # is releasing on this APPROVE, so the new fix must
                            # cold-provision rather than wait for a dying poll.
                            fix_task, _ = await _spawn_pipeline_task(
                                task,
                                "fix",
                                session,
                                pr_number,
                                branch,
                                base_branch,
                                fix_prompt=ci_fix_prompt,
                                original_task_id=original_task_id,
                                fix_reason="ci_failure",
                                ci_failure_logs=ci_errors,
                                suppress_warm_handoff=True,
                            )
                            if fix_task:
                                logger.error(
                                    f"[CI] PR #{pr_number} CI failed, spawned fix → {fix_task.id}"
                                )
                            action_taken = True
                            return {"status": "accepted"}

                        # CI passed (or no CI checks exist) — proceed to merge
                        if ci_status and ci_status["conclusion"] == "success":
                            logger.info(f"[CI] PR #{pr_number} CI passed")

                        # Critical files gate: block merge if security-critical files were touched
                        if original_task_id and _runtime_config.get(
                            "require_approval_gates", True
                        ):
                            try:
                                orig_task = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                critical_touched = (
                                    (orig_task.context or {}).get(
                                        "touched_critical", []
                                    )
                                    if orig_task
                                    else []
                                )
                                if critical_touched:
                                    orig_task.requires_approval = True
                                    orig_task.status = TaskStatus.EVALUATING
                                    orig_task.latest_status_message = f"Review approved — awaiting human approval to merge (critical files: {', '.join(critical_touched)})"
                                    session.add(orig_task)
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(orig_task.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(orig_task.id),
                                            "status": TaskStatus.EVALUATING,
                                        },
                                    )
                                    await create_pr_comment(
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        f"**Blueprint Review Pipeline**: Review approved, but PR touches security-critical files ({', '.join(critical_touched)}). Merge requires human approval.",
                                    )
                                    logger.error(
                                        f"[PIPELINE] PR #{pr_number} review approved but merge blocked — critical files: {critical_touched}"
                                    )
                                    action_taken = True
                                    return {"status": "accepted"}
                            except Exception as e:
                                logger.error(
                                    f"[PIPELINE] Error checking critical files for merge gate: {e}",
                                    exc_info=True,
                                )

                        # Security / approval gate: if the original create task was
                        # flagged (security scan blocking, scan failure, etc.), park
                        # for manual merge. The review still ran and posted feedback,
                        # but auto-merge is not safe without human sign-off.
                        if original_task_id and _runtime_config.get(
                            "require_approval_gates", True
                        ):
                            try:
                                _approval_orig = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                if _approval_orig and _approval_orig.requires_approval:
                                    _approval_orig.status = TaskStatus.EVALUATING
                                    reason = (
                                        _approval_orig.latest_status_message
                                        or "requires manual approval"
                                    )
                                    _approval_orig.latest_status_message = f"Review approved — awaiting human approval to merge ({reason})"
                                    session.add(_approval_orig)
                                    await session.commit()
                                    await broadcast_to_project(
                                        str(_approval_orig.project_id),
                                        "task_updated",
                                        {
                                            "task_id": str(_approval_orig.id),
                                            "status": TaskStatus.EVALUATING,
                                        },
                                    )
                                    await _comment_with_retry(
                                        create_pr_comment,
                                        gh_token,
                                        owner,
                                        repo,
                                        pr_number,
                                        f"**Blueprint Review Pipeline**: Review approved, but merge requires human approval — {reason}",
                                    )
                                    logger.info(
                                        f"[PIPELINE] PR #{pr_number} review approved but merge blocked — requires_approval set on {original_task_id[:8]}"
                                    )
                                    action_taken = True
                                    return {"status": "accepted"}
                            except Exception as e:
                                logger.error(
                                    f"[PIPELINE] Error checking requires_approval for merge gate: {e}",
                                    exc_info=True,
                                )

                        merge_result = await _merge_with_retry(
                            merge_pull_request,
                            gh_token,
                            owner,
                            repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )

                        if merge_result.get("merged"):
                            await _comment_with_retry(
                                create_pr_comment,
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                "**Blueprint Review Pipeline**: PR auto-merged.",
                            )
                            # Mark original create task so dependents can proceed
                            if original_task_id:
                                try:
                                    orig = await session.get(
                                        Task, uuid.UUID(original_task_id)
                                    )
                                    if orig:
                                        orig.context = {
                                            **(orig.context or {}),
                                            "pr_merged": True,
                                        }
                                        # If this was a rollup PR review, mark the parent as COMPLETED
                                        is_rollup_merge = (
                                            orig.status == TaskStatus.EVALUATING
                                        )
                                        if is_rollup_merge:
                                            orig.status = TaskStatus.COMPLETED
                                            orig.latest_status_message = (
                                                "Rollup PR merged"
                                            )
                                        session.add(orig)
                                        _o_ctx = orig.context or {}
                                        # Audit W1: auto_remediation is now a column.
                                        if _o_ctx.get("diagnosis_task_id") or (
                                            orig.auto_remediation
                                            and _o_ctx.get("direction_id")
                                        ):
                                            try:
                                                from app.task_completion import (
                                                    _record_remediation_outcome,
                                                )

                                                await _record_remediation_outcome(
                                                    orig, merged=True, session=session
                                                )
                                            except Exception as _rr_exc:
                                                logger.warning(
                                                    f"[DIAGNOSIS] Failed to record remediation outcome on merge for task {orig.id}: {_rr_exc!r}"
                                                )
                                        # Post result message for auto-remediation tasks
                                        # (routes between CI auto-fix and residual lifecycles).
                                        if orig.auto_remediation:
                                            try:
                                                await post_remediation_result_message(
                                                    session,
                                                    orig,
                                                    result="merged",
                                                    pr_number=pr_number,
                                                )
                                                try:
                                                    from app.notification_dispatch import (
                                                        dispatch_external_notification,
                                                    )

                                                    if is_residual_remediation_task(
                                                        orig
                                                    ):
                                                        _wh_event = (
                                                            "RESIDUAL_FIX_MERGED"
                                                        )
                                                        _wh_payload = {
                                                            "fingerprint": (
                                                                orig.context or {}
                                                            ).get(
                                                                "error_fingerprint", ""
                                                            ),
                                                            "pr_number": pr_number,
                                                        }
                                                    else:
                                                        _ar_branch = (
                                                            orig.context or {}
                                                        ).get(
                                                            "ci_failure_branch",
                                                            base_branch,
                                                        )
                                                        _wh_event = "AUTO_FIX_MERGED"
                                                        _wh_payload = {
                                                            "branch": _ar_branch,
                                                            "pr_number": pr_number,
                                                        }
                                                    asyncio.ensure_future(  # noqa: RUF006
                                                        dispatch_external_notification(  # noqa: RUF006, RUF100
                                                            orig.project_id,
                                                            _wh_event,
                                                            _wh_payload,
                                                        )
                                                    )
                                                except Exception as _wh_exc:
                                                    logger.warning(
                                                        "[AUTO-REMEDIATION] Failed to dispatch merged webhook: %s",
                                                        _wh_exc,
                                                    )
                                            except Exception as _ar_exc:
                                                logger.warning(
                                                    "[AUTO-REMEDIATION] Failed to post result message: %s",
                                                    _ar_exc,
                                                )
                                        # Extract review feedback as persistent playbook entries
                                        # on every merge (captures first-attempt data at any iteration)
                                        try:
                                            from app.playbook_learning import (
                                                extract_review_feedback_to_playbook,
                                            )

                                            _orig_task_type = orig.first_payload.get(
                                                "type", "create"
                                            )
                                            await extract_review_feedback_to_playbook(
                                                task=task,
                                                review_loop=review_loop,
                                                pr_number=pr_number,
                                                original_task_type=_orig_task_type,
                                                session=session,
                                            )
                                        except Exception as _rp_exc:
                                            logger.warning(
                                                "[REVIEW_PLAYBOOK] Failed to extract review feedback: %s",
                                                _rp_exc,
                                            )
                                        # Close-loop: advance the RegressionRemediationCase to MERGED.
                                        try:
                                            await _close_loop_regression_case_on_merge(
                                                orig, pr_number, session
                                            )
                                        except Exception as _rc_merge_exc:
                                            logger.warning(
                                                f"[REGRESSION-CASE] Failed to advance case on review-merge for task {orig.id}: {_rc_merge_exc!r}"
                                            )
                                        await session.commit()
                                        # Clean up branches (staging + feature)
                                        await _cleanup_branches(orig, session)
                                        # Spawn a smoketest after rollup merges to main.
                                        # After Phase 1 (automain/LTS), normal rollups target
                                        # `automain`; only promotion PRs (automain → main) still
                                        # merge to `main`. So base_branch == "main" here means a
                                        # PROMOTION just landed — this smoketest acts as a
                                        # post-promotion production health-check, not a routine
                                        # integration check.
                                        # Supported runtime_config keys for smoketest:
                                        #   health_check_urls: list[str] — live URLs to probe with HTTP GET after local tests
                                        if (
                                            is_rollup_merge
                                            and base_branch == "main"
                                            and _runtime_config.get("smoketest_enabled")
                                        ):
                                            try:
                                                type_model = (
                                                    _runtime_config.get(
                                                        "task_type_models", {}
                                                    ).get("smoketest")
                                                    or None
                                                )
                                                type_effort = (
                                                    _runtime_config.get(
                                                        "task_type_efforts", {}
                                                    ).get("smoketest")
                                                    or None
                                                )
                                                health_check_urls = (
                                                    _runtime_config.get(
                                                        "health_check_urls"
                                                    )
                                                    or []
                                                )
                                                _smoketest_payload = validate_internal_payload(
                                                    {
                                                        "type": "smoketest",
                                                        "base_branch": base_branch,
                                                        **(
                                                            {"model": type_model}
                                                            if type_model
                                                            else {}
                                                        ),
                                                        **(
                                                            {"effort": type_effort}
                                                            if type_effort
                                                            else {}
                                                        ),
                                                        **(
                                                            {
                                                                "health_check_urls": health_check_urls
                                                            }
                                                            if health_check_urls
                                                            else {}
                                                        ),
                                                    },
                                                    "task_lifecycle:smoketest",
                                                )
                                                smoketest_task = Task(
                                                    project_id=task.project_id,
                                                    title=f"Smoketest after rollup merge to {base_branch}",
                                                    description=f"Post-merge health checks for {owner}/{repo}@{base_branch}",
                                                    payloads=[_smoketest_payload],
                                                    vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                    status=TaskStatus.QUEUED,
                                                    created_by="ORCHESTRATOR",
                                                )
                                                session.add(smoketest_task)
                                                await session.commit()
                                                await session.refresh(smoketest_task)
                                                history = TaskHistory(
                                                    task_id=smoketest_task.id,
                                                    action="SPAWNED",
                                                    details={
                                                        "spawned_by": "POST_ROLLUP_MERGE",
                                                        "base_branch": base_branch,
                                                    },
                                                )
                                                session.add(history)
                                                await session.commit()
                                                await broadcast_to_project(
                                                    str(task.project_id),
                                                    "task_updated",
                                                    {
                                                        "task_id": str(
                                                            smoketest_task.id
                                                        ),
                                                        "status": TaskStatus.QUEUED,
                                                    },
                                                )
                                                logger.info(
                                                    f"[SMOKETEST] Spawned smoketest task {smoketest_task.id} after rollup merge to {base_branch}"
                                                )
                                            except Exception as e:
                                                logger.warning(
                                                    f"[SMOKETEST] Failed to spawn smoketest after rollup merge: {e}",
                                                    exc_info=True,
                                                )
                                        # Back-merge: when a HOTFIX merges to main, open a
                                        # main -> automain PR so the branches don't diverge.
                                        # A hotfix is a `plan` task with base_branch="main" and
                                        # hotfix:true in its payload.  A missed back-merge makes
                                        # the next automain -> main promotion conflict.
                                        # (automain/LTS plan Phase 5)
                                        if (
                                            is_rollup_merge
                                            and base_branch == "main"
                                            and bool(orig.first_payload.get("hotfix"))
                                        ):
                                            try:
                                                from app import hotfix_backmerge

                                                _merged_pr_url = (
                                                    f"https://github.com/{owner}/{repo}/pull/{pr_number}"
                                                    if owner and repo and pr_number
                                                    else ""
                                                )
                                                await (
                                                    hotfix_backmerge.open_backmerge_pr(
                                                        session,
                                                        task.project_id,
                                                        merged_pr_url=_merged_pr_url,
                                                        hotfix_task_id=orig.id,
                                                    )
                                                )
                                                # open_backmerge_pr deliberately
                                                # does NOT commit (its docstring:
                                                # "the caller commits the
                                                # session"). On a back-merge
                                                # conflict / 405 it session.add +
                                                # flush()es an operator-facing
                                                # ERROR Alert and a high-priority
                                                # conflict-resolution Task. After
                                                # flush() the session is
                                                # flushed-but-clean, so the
                                                # function's final
                                                # ``if session.dirty or
                                                # session.new`` commit is SKIPPED
                                                # and the ``async with`` exit
                                                # would ROLL BACK that Alert/Task
                                                # — leaving a back-merge conflict
                                                # completely unsignalled. Commit
                                                # here, mirroring the
                                                # smoketest-spawn block above.
                                                # The merged-PR work is already
                                                # committed (~:3470); this commit
                                                # only flushes the back-merge's
                                                # own Alert/Task — safe.
                                                await session.commit()
                                            except Exception as _bm_exc:
                                                logger.warning(
                                                    f"[HOTFIX_BACKMERGE] Back-merge trigger failed for hotfix task {orig.id}: {_bm_exc!r}",
                                                    exc_info=True,
                                                )
                                        # Spawn security rescan if merged task's parent was a security_audit
                                        if orig.parent_task_id:
                                            try:
                                                audit_parent = await session.get(
                                                    Task, orig.parent_task_id
                                                )
                                                if (
                                                    audit_parent
                                                    and audit_parent.first_payload.get(
                                                        "type"
                                                    )
                                                    == "security_audit"
                                                    and audit_parent.status
                                                    == TaskStatus.COMPLETED
                                                ):
                                                    # Only rescan once per original audit
                                                    existing_rescan = (
                                                        (
                                                            await session.execute(
                                                                select(Task).where(
                                                                    Task.parent_task_id
                                                                    == audit_parent.id,
                                                                    Task.title.ilike(
                                                                        "%rescan%"
                                                                    ),
                                                                )
                                                            )
                                                        )
                                                        .scalars()
                                                        .first()
                                                    )
                                                    if not existing_rescan:
                                                        try:
                                                            pr_files = (
                                                                await get_pr_files(
                                                                    gh_token,
                                                                    owner,
                                                                    repo,
                                                                    pr_number,
                                                                )
                                                            )
                                                            changed_files = ", ".join(
                                                                f["filename"]
                                                                for f in pr_files
                                                            )
                                                        except Exception:
                                                            changed_files = "(could not fetch file list)"
                                                        audit_title = audit_parent.title
                                                        type_model = (
                                                            _runtime_config.get(
                                                                "task_type_models", {}
                                                            ).get("security_audit")
                                                            or None
                                                        )
                                                        type_effort = (
                                                            _runtime_config.get(
                                                                "task_type_efforts", {}
                                                            ).get("security_audit")
                                                            or None
                                                        )
                                                        _rescan_payload = validate_internal_payload(
                                                            {
                                                                "type": "security_audit",
                                                                "prompt": (
                                                                    f"Verify that the security fixes merged in PR #{pr_number} actually addressed "
                                                                    f"the reported vulnerabilities. Focus audit on these files: {changed_files}. "
                                                                    f"Output only PASS or remaining FAIL findings."
                                                                ),
                                                                **(
                                                                    {
                                                                        "model": type_model
                                                                    }
                                                                    if type_model
                                                                    else {}
                                                                ),
                                                                **(
                                                                    {
                                                                        "effort": type_effort
                                                                    }
                                                                    if type_effort
                                                                    else {}
                                                                ),
                                                            },
                                                            "task_lifecycle:security_audit",
                                                        )
                                                        rescan_task = Task(
                                                            project_id=orig.project_id,
                                                            title=f"Security rescan: verify {audit_title} fixes",
                                                            description=f"Verify that security fixes merged in PR #{pr_number} addressed reported vulnerabilities.",
                                                            payloads=[_rescan_payload],
                                                            parent_task_id=audit_parent.id,
                                                            vm_instance_id=f"agent-vm-{uuid.uuid4().hex}",
                                                            status=TaskStatus.QUEUED,
                                                            requires_approval=False,
                                                            created_by="ORCHESTRATOR",
                                                        )
                                                        session.add(rescan_task)
                                                        await session.commit()
                                                        await session.refresh(
                                                            rescan_task
                                                        )
                                                        history = TaskHistory(
                                                            task_id=rescan_task.id,
                                                            action="SPAWNED",
                                                            details={
                                                                "spawned_by": "POST_SECURITY_FIX_MERGE",
                                                                "pr_number": pr_number,
                                                                "audit_parent_id": str(
                                                                    audit_parent.id
                                                                ),
                                                            },
                                                        )
                                                        session.add(history)
                                                        await session.commit()
                                                        await broadcast_to_project(
                                                            str(orig.project_id),
                                                            "task_updated",
                                                            {
                                                                "task_id": str(
                                                                    rescan_task.id
                                                                ),
                                                                "status": TaskStatus.QUEUED,
                                                            },
                                                        )
                                                        logger.info(
                                                            f"[SECURITY RESCAN] Spawned rescan task {rescan_task.id} after security fix merge PR #{pr_number}"
                                                        )
                                            except Exception as e:
                                                logger.info(
                                                    f"[SECURITY RESCAN] Failed to spawn security rescan: {e}"
                                                )
                                except Exception as e:
                                    logger.warning(
                                        f"[REVIEW PIPELINE] Failed to set pr_merged on {original_task_id}: {e}",
                                        exc_info=True,
                                    )
                            logger.info(
                                f"[REVIEW PIPELINE] PR #{pr_number} approved and merged"
                            )
                            action_taken = True
                        else:
                            # Merge blocked — park in EVALUATING with ci_waiting so the
                            # ci_completion retry-with-backoff loop handles it instead of
                            # spawning a fix task immediately.  A first-attempt merge
                            # failure is frequently a stale GitHub mergeability flag
                            # (GitHub recomputes asynchronously after pushes); ci_completion
                            # absorbs that window via its internal 5/15/45s backoff inside
                            # a single merge attempt.  If the merge still fails after that
                            # quiet window, it spawns a rebase fix for the moving-parent
                            # case (sibling rollups advancing the base branch between
                            # fetches).  Fully autonomous; no requires_approval.
                            merge_msg = merge_result.get("message", "unknown")
                            await create_pr_comment(
                                gh_token,
                                owner,
                                repo,
                                pr_number,
                                f"**Blueprint Review Pipeline**: Merge blocked — {merge_msg}. Retrying with backoff.",
                            )
                            from app import pause_clock

                            try:
                                _approve_files = [
                                    f.get("filename", "")
                                    for f in (
                                        await get_pr_files(
                                            gh_token, owner, repo, pr_number
                                        )
                                    )
                                    if isinstance(f, dict) and f.get("filename")
                                ]
                            except Exception as _af_e:
                                logger.warning(
                                    f"[REVIEW PIPELINE] APPROVE entry: failed to fetch PR files for #{pr_number}: {_af_e}"
                                )
                                _approve_files = []
                            merge_wait_loop = dict(review_loop)
                            merge_wait_loop["review_history"] = (
                                _append_approve_to_review_history(
                                    review_loop, _approve_files
                                )
                            )
                            merge_wait_loop["ci_waiting"] = True
                            merge_wait_loop["review_approved"] = True
                            merge_wait_loop["ci_passed"] = (
                                True  # CI was already verified green above
                            )
                            merge_wait_loop["ci_wait_started"] = _utcnow().isoformat()
                            merge_wait_loop[
                                "ci_wait_pause_baseline"
                            ] = await pause_clock.current_baseline(session)
                            merge_wait_loop["ci_poll_interval"] = 30
                            merge_wait_loop["pr_number"] = pr_number
                            merge_wait_loop["branch"] = branch
                            merge_wait_loop["base_branch"] = base_branch
                            merge_wait_loop["original_task_id"] = original_task_id
                            merge_wait_loop["head_sha"] = head_sha
                            merge_wait_loop["merge_blocked_msg"] = merge_msg[:300]
                            task.status = TaskStatus.EVALUATING
                            task.context = {
                                **(task.context or {}),
                                "review_loop": merge_wait_loop,
                            }
                            task.latest_status_message = f"Review approved, merge blocked on PR #{pr_number} ({merge_msg[:120]}) — retrying with backoff"
                            session.add(task)
                            await session.commit()
                            logger.info(
                                f"[REVIEW PIPELINE] PR #{pr_number} merge blocked, parked for ci_completion retry: {merge_msg[:200]}"
                            )
                            action_taken = True
                    except Exception as e:
                        # Merge/CI-check failed — park in EVALUATING with ci_waiting so
                        # the orchestrator's CI monitoring loop retries the merge instead
                        # of silently orphaning the PR.
                        logger.error(
                            f"[REVIEW PIPELINE] Failed to merge PR #{pr_number}, parking in CI-wait: {e}",
                            exc_info=True,
                        )
                        # Best-effort: gh_token may not be in scope if the failure
                        # happened before the auth call.  An empty file list here
                        # still records that the iteration was approved.
                        try:
                            _approve_files = [
                                f.get("filename", "")
                                for f in (
                                    await get_pr_files(gh_token, owner, repo, pr_number)
                                )
                                if isinstance(f, dict) and f.get("filename")
                            ]
                        except Exception:
                            _approve_files = []
                        _approved_history = _append_approve_to_review_history(
                            review_loop, _approve_files
                        )
                        task.status = TaskStatus.EVALUATING
                        task.latest_status_message = f"Review approved, merge attempt failed ({type(e).__name__}) — CI monitor will retry"
                        task.context = {
                            **(task.context or {}),
                            "review_loop": {
                                **review_loop,
                                "ci_waiting": True,
                                "review_approved": True,
                                "review_history": _approved_history,
                                "head_sha": head_sha,
                                "pr_number": pr_number,
                                "branch": branch,
                                "base_branch": base_branch,
                                "original_task_id": original_task_id,
                                "ci_wait_started": _utcnow().isoformat(),
                            },
                        }
                        session.add(task)
                        await session.commit()
                        action_taken = True
                else:
                    # REQUEST_CHANGES — include CI errors if available (don't wait).
                    # After verdict normalization above this is the only non-APPROVE branch;
                    # any non-standard verdict (e.g., legacy COMMENT) has already been
                    # rewritten to REQUEST_CHANGES and lands here.

                    # Unparseable verdict short-circuit: the fix agent would receive no
                    # structured issues and a placeholder feedback string, consuming a VM
                    # cycle without useful guidance. The original task is already flagged
                    # requires_approval above, so human intervention is expected.
                    if verdict_unparseable:
                        logger.warning(
                            f"[REVIEW PIPELINE] PR #{pr_number}: verdict unparseable — skipping fix spawn, original task flagged requires_approval"
                        )
                        task.status = TaskStatus.COMPLETED
                        task.latest_status_message = f"Review verdict unparseable for PR #{pr_number} — fix skipped, manual review required"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}

                    # Empty-PR short-circuit: if the PR has no file changes (because a
                    # prior fix cycle rebased the branch flat so all its commits are in
                    # the base), spawning another fix is futile — there's nothing to
                    # fix. Close the PR and mark the rollup parent COMPLETED so it
                    # stops blocking the pipeline.
                    try:
                        (
                            _empty_gh,
                            _empty_owner,
                            _empty_repo,
                        ) = await get_github_token_for_task(task.id, session)
                        _pr_files = await get_pr_files(
                            _empty_gh,
                            _empty_owner,
                            _empty_repo,
                            pr_number,
                            project_id=str(task.project_id),
                        )
                    except Exception as _empty_err:
                        logger.warning(
                            f"[REVIEW PIPELINE] Empty-PR check failed for #{pr_number}: {_empty_err} — proceeding with fix spawn"
                        )
                        _pr_files = None
                    if _pr_files == []:
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} has no file changes — closing as empty rollup"
                        )
                        try:
                            await close_pull_request(
                                _empty_gh,
                                _empty_owner,
                                _empty_repo,
                                pr_number,
                                project_id=str(task.project_id),
                            )
                        except Exception as _close_err:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to close empty PR #{pr_number}: {_close_err}"
                            )
                        if original_task_id:
                            try:
                                # Phase A1: skip_locked + bounded retry. If the
                                # parent row is held, defer the empty-PR cleanup;
                                # the next poll cycle will retry. Better than
                                # 500'ing the review handler.
                                _orig = None
                                for _attempt in range(3):
                                    _orig_q = (
                                        select(Task)
                                        .where(Task.id == uuid.UUID(original_task_id))
                                        .with_for_update(skip_locked=True)
                                    )
                                    _orig = (
                                        (await session.execute(_orig_q))
                                        .scalars()
                                        .first()
                                    )
                                    if _orig is not None:
                                        break
                                    _orig_exists = (
                                        await session.execute(
                                            select(Task.id).where(
                                                Task.id == uuid.UUID(original_task_id)
                                            )
                                        )
                                    ).first()
                                    if _orig_exists is None:
                                        break
                                    await asyncio.sleep(0.5 * (_attempt + 1))
                                if _orig:
                                    _orig_loop = (_orig.context or {}).get(
                                        "review_loop", {}
                                    ) or {}
                                    _orig_loop.pop("ci_waiting", None)
                                    _orig.context = {
                                        **(_orig.context or {}),
                                        "review_loop": _orig_loop,
                                        "pr_closed_empty": True,
                                    }
                                    flag_modified(_orig, "context")
                                    _orig.status = TaskStatus.COMPLETED
                                    _orig.latest_status_message = f"✓ Rollup PR #{pr_number} closed — branch equivalent to {base_branch}, no new changes to merge"
                                    session.add(_orig)
                                else:
                                    logger.warning(
                                        f"[REVIEW PIPELINE] Empty-PR cleanup deferred — parent {original_task_id[:8]} contended"
                                    )
                            except Exception as _orig_err:
                                logger.warning(
                                    f"[REVIEW PIPELINE] Failed to update rollup parent after empty-PR close: {_orig_err}"
                                )
                        task.status = TaskStatus.COMPLETED
                        task.latest_status_message = f"Review complete — PR #{pr_number} closed as empty (obsolete rollup)"
                        session.add(task)
                        await session.commit()
                        if original_task_id:
                            try:  # noqa: SIM105
                                await broadcast_to_project(
                                    str(task.project_id),
                                    "task_updated",
                                    {
                                        "task_id": original_task_id,
                                        "status": TaskStatus.COMPLETED,
                                    },
                                )
                            except Exception:  # noqa: S110
                                pass
                        return {"status": "accepted"}

                    ci_errors_text = ""
                    ci_check: dict | None = None
                    try:
                        gh_token, owner, repo = await get_github_token_for_task(
                            task.id, session
                        )
                        pr_data = await get_pull_request(
                            gh_token, owner, repo, pr_number
                        )
                        head_sha = pr_data.get("head", {}).get("sha", "")
                        if head_sha:
                            ci_check = await get_check_runs_cached(
                                gh_token, owner, repo, head_sha
                            )
                            if ci_check.get("conclusion") == "failure" and ci_check.get(
                                "failed_logs"
                            ):
                                ci_errors_text = f"\n\nCI build errors (also need fixing):\n{ci_check['failed_logs']}"
                    except Exception as e:
                        logger.error(
                            f"[REVIEW PIPELINE] Could not fetch CI errors for fix context: {e}",
                            exc_info=True,
                        )
                    # CI vs reviewer build-gate disagreement detector. When the
                    # reviewer flags build/lint complaints but CI shows the
                    # corresponding job GREEN, the issue is almost always a
                    # broken reviewer-VM env — pre-warn the fix agent so it
                    # disputes via /tmp/fix_responses rather than thrashing on
                    # source code that isn't broken (PR #3884 saga, 2026-05-06).
                    disagreement_hint = _detect_ci_review_disagreement(
                        issues,
                        feedback,
                        ci_check,
                        pr_number,
                    )
                    if issues:

                        def _fmt_issue(i):
                            desc = i.get("description", "")
                            file_ = i.get("file")
                            line_ = i.get("line")
                            if file_ and line_ is not None:
                                return f"- [{file_}:{line_}] {desc}"
                            if file_:
                                return f"- [{file_}] {desc}"
                            return f"- {desc}"

                        critical = [
                            i for i in issues if i.get("severity") == "critical"
                        ]
                        major = [i for i in issues if i.get("severity") == "major"]
                        minor = [i for i in issues if i.get("severity") == "minor"]
                        structured = ""
                        if critical:
                            structured += (
                                "\n\nCRITICAL issues (must fix):\n"
                                + "\n".join(_fmt_issue(i) for i in critical)
                            )
                        if major:
                            structured += "\n\nMajor issues:\n" + "\n".join(
                                _fmt_issue(i) for i in major
                            )
                        if minor:
                            structured += "\n\nMinor issues:\n" + "\n".join(
                                _fmt_issue(i) for i in minor
                            )
                        fix_prompt_text = f"{disagreement_hint}A code review requested changes. Address the following issues:{structured}{ci_errors_text}"
                    else:
                        fix_prompt_text = f"{disagreement_hint}A code review requested changes. Please address the following feedback:\n\n{feedback}{ci_errors_text}"
                    # First-pass minor-only verdict: when every blocking finding
                    # from the initial review is minor-severity, the spawned fix
                    # only addresses nits. Mark the fix so its completion handler
                    # skips the follow-up re-review (CI gates the merge instead),
                    # saving one review iteration + one review-phase LLM call.
                    review_iteration = review_loop.get("iteration", 1)
                    first_attempt_minor_only = (
                        review_iteration == 1
                        and bool(issues)
                        and all(
                            (i.get("severity", "minor") or "minor").lower() == "minor"
                            for i in issues
                            if i.get("description")
                        )
                    )
                    fix_task, _ = await _spawn_pipeline_task(
                        task,
                        "fix",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        fix_prompt=fix_prompt_text,
                        original_prompt=original_prompt,
                        original_task_id=original_task_id,
                        current_feedback=feedback,
                        current_issues=issues,
                        investigated_files=investigated_files,
                        skip_rereview_minor_only=first_attempt_minor_only,
                    )
                    if fix_task:
                        logger.info(
                            f"[REVIEW PIPELINE] Spawned fix for PR #{pr_number} → task {fix_task.id}"
                        )
                        # Reset the parking parent's CI-wait clock: each spawned fix
                        # is a fresh cycle of work, so the 30-min backstop in
                        # services.py should count from here instead of the initial
                        # rollup-creation timestamp.  Without this, a rollup that
                        # legitimately cycles through 3-4 fix iterations gets killed
                        # mid-pipeline.
                        if original_task_id:
                            try:
                                # Phase A1: skip_locked + bounded retry. If the
                                # parent row is held, skip the ci_wait reset —
                                # it's an optimization (resets the 30-min
                                # backstop clock), not correctness; the parent
                                # will still terminate, just on the original
                                # baseline if reset is missed.
                                _orig = None
                                for _attempt in range(3):
                                    _orig_q = (
                                        select(Task)
                                        .where(Task.id == uuid.UUID(original_task_id))
                                        .with_for_update(skip_locked=True)
                                    )
                                    _orig = (
                                        (await session.execute(_orig_q))
                                        .scalars()
                                        .first()
                                    )
                                    if _orig is not None:
                                        break
                                    _orig_exists = (
                                        await session.execute(
                                            select(Task.id).where(
                                                Task.id == uuid.UUID(original_task_id)
                                            )
                                        )
                                    ).first()
                                    if _orig_exists is None:
                                        break
                                    await asyncio.sleep(0.5 * (_attempt + 1))
                                if _orig:
                                    _orig_loop = dict(
                                        (_orig.context or {}).get("review_loop", {})
                                        or {}
                                    )
                                    if _orig_loop.get("ci_waiting"):
                                        from app import pause_clock

                                        _orig_loop["ci_wait_started"] = (
                                            _utcnow().isoformat()
                                        )
                                        _orig_loop[
                                            "ci_wait_pause_baseline"
                                        ] = await pause_clock.current_baseline(session)
                                        _orig.context = {
                                            **(_orig.context or {}),
                                            "review_loop": _orig_loop,
                                        }
                                        flag_modified(_orig, "context")
                                        session.add(_orig)
                                else:
                                    logger.info(
                                        f"[REVIEW PIPELINE] ci_wait reset skipped for parent {original_task_id[:8]} — row contended"
                                    )
                            except Exception as _reset_err:
                                logger.warning(
                                    f"[REVIEW PIPELINE] Failed to reset ci_wait_started on parent {original_task_id[:8]}: {_reset_err}"
                                )
                    else:
                        task.latest_status_message = f"Fix not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        cap_alert_title = (
                            f"PR #{pr_number} stalled — iteration cap reached"
                        )
                        existing_cap_alert = (
                            (
                                await session.execute(
                                    select(Alert).where(
                                        Alert.project_id == task.project_id,
                                        Alert.severity == AlertSeverity.WARNING,
                                        Alert.dismissed_at.is_(None),
                                        Alert.title == cap_alert_title,
                                    )
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Review requested changes on PR #{pr_number} but the fix iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(
                                str(task.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(alert.id),
                                    "severity": "WARNING",
                                    "title": alert.title,
                                },
                            )
                        else:
                            await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Fix spawn returned None for PR #{pr_number}"
                        )
                    # On every review rejection, store which issue categories caused it
                    # on the original task.  Iteration 1 → _first_attempt_rejection_categories
                    # (existing field); all iterations → _all_iterations_rejection_categories
                    # (accumulated superset used by all-iterations catch-rate metric).
                    if issues and original_task_id:
                        try:
                            from app.playbook_learning import (
                                cache_first_attempt_classification,
                                _keyword_from_flat,
                            )

                            rh_items = [
                                {
                                    "description": i.get("description", ""),
                                    "severity": i.get("severity", "minor"),
                                    "iteration": review_iteration,
                                }
                                for i in issues
                                if i.get("description")
                            ]
                            if rh_items:
                                orig_task = await session.get(
                                    Task, uuid.UUID(original_task_id)
                                )
                                if orig_task:
                                    task_ctx = dict(orig_task.context or {})
                                    if review_iteration == 1:
                                        buckets = (
                                            await cache_first_attempt_classification(
                                                task_ctx, rh_items
                                            )
                                        )
                                    else:
                                        buckets = _keyword_from_flat(rh_items)
                                    rejection_cats = {
                                        cat: len(items)
                                        for cat, items in buckets.items()
                                        if items
                                    }
                                    if rejection_cats:
                                        if review_iteration == 1:
                                            task_ctx[
                                                "_first_attempt_rejection_categories"
                                            ] = rejection_cats
                                            task_ctx["_first_attempt_minor_only"] = (
                                                first_attempt_minor_only
                                            )
                                        existing_all = dict(
                                            task_ctx.get(
                                                "_all_iterations_rejection_categories"
                                            )
                                            or {}
                                        )
                                        for cat, cnt in rejection_cats.items():
                                            existing_all[cat] = (
                                                existing_all.get(cat, 0) + cnt
                                            )
                                        task_ctx[
                                            "_all_iterations_rejection_categories"
                                        ] = existing_all
                                    orig_task.context = task_ctx
                                    flag_modified(orig_task, "context")
                                    session.add(orig_task)
                                    await session.commit()
                        except Exception as _cat_e:
                            logger.warning(
                                f"[REVIEW PIPELINE] Failed to record rejection categories: {_cat_e}"
                            )
                    action_taken = True

                if not action_taken:
                    task.latest_status_message = f"Review completed but no action taken (verdict={verdict}) — check orchestrator logs"
                    session.add(task)
                    await session.commit()
                    logger.warning(
                        f"[REVIEW PIPELINE] WARNING: Review {task.id} for PR #{pr_number} completed with no downstream action (verdict={verdict})"
                    )

            # Fix task completed → security scan → spawn new review
            elif task_type == "fix":
                pr_number = review_loop.get("pr_number") or task_payload.get(
                    "pr_number"
                )
                branch = review_loop.get("branch") or task_payload.get("branch")
                base_branch = review_loop.get("base_branch") or task_payload.get(
                    "base_branch", "main"
                )
                original_task_id = review_loop.get(
                    "original_task_id", ""
                ) or task_payload.get("original_task_id", "")

                # Backend stamp: ensure fix_iteration is set on the self-review
                # outcome so the catch-rate dashboard and reviewer priming can
                # identify which fix iteration produced the SR. The agent stamps
                # it via code-change.ts readSelfReviewOutcome; this is the
                # fallback for agents that ran before that stamp was added.
                _fix_sro = (task.context or {}).get("_self_review_outcome")
                if isinstance(_fix_sro, dict) and "fix_iteration" not in _fix_sro:
                    _pipeline_iter = int(review_loop.get("iteration", 2))
                    # Pipeline sequence: review(1)→fix(2)→review(3)→fix(4)→...
                    # fix_iteration = pipeline_iter // 2
                    _fix_iter_n = max(1, _pipeline_iter // 2)
                    _stamped_ctx = dict(task.context)
                    _stamped_ctx["_self_review_outcome"] = {
                        **_fix_sro,
                        "fix_iteration": _fix_iter_n,
                    }
                    task.context = _stamped_ctx
                    flag_modified(task, "context")
                    session.add(task)

                if not pr_number or not branch:
                    logger.info(
                        f"[REVIEW PIPELINE] Fix task {task.id} missing pr_number ({pr_number}) or branch ({branch}) — cannot continue pipeline"
                    )
                    task.latest_status_message = (
                        "Fix pipeline error: missing pr_number or branch"
                    )
                    session.add(task)
                    await session.commit()
                    return {"status": "accepted"}

                # Early exit: skip if PR is already merged or closed
                try:
                    (
                        gh_token_check,
                        owner_check,
                        repo_check,
                    ) = await get_github_token_for_task(task.id, session)
                    pr_data_check = await get_pull_request(
                        gh_token_check, owner_check, repo_check, pr_number
                    )
                    if pr_data_check.get("state") == "closed" or pr_data_check.get(
                        "merged"
                    ):
                        merged_label = (
                            "merged" if pr_data_check.get("merged") else "closed"
                        )
                        logger.info(
                            f"[REVIEW PIPELINE] PR #{pr_number} already {merged_label} — skipping fix re-review for task {task.id}"
                        )
                        task.latest_status_message = f"PR #{pr_number} already {merged_label} — re-review skipped"
                        session.add(task)
                        await session.commit()
                        return {"status": "accepted"}
                except Exception as e:
                    logger.info(
                        f"[REVIEW PIPELINE] Could not check PR #{pr_number} state: {e} — proceeding"
                    )

                # Security scan fix output before re-review
                scan_blocked = False
                try:
                    gh_token, owner, repo = await get_github_token_for_task(
                        task.id, session
                    )
                    scan_result = await scan_pr_for_injection(
                        gh_token, owner, repo, pr_number
                    )
                    logger.info(f"[SECURITY] {scan_result['summary']}")
                    if scan_result["blocked"]:
                        scan_blocked = True
                        task.requires_approval = True
                        task.latest_status_message = scan_result["summary"]
                        session.add(task)
                        await session.commit()
                    if scan_result["findings"]:
                        await create_pr_comment(
                            gh_token, owner, repo, pr_number, scan_result["report"]
                        )
                except Exception as e:
                    logger.error(
                        f"[SECURITY] Scan failed for fix PR #{pr_number}, flagging for approval: {e}",
                        exc_info=True,
                    )
                    task.requires_approval = True
                    task.latest_status_message = f"Security scan failed — review will proceed but merge requires manual approval: {type(e).__name__}: {e!r}"[
                        :500
                    ]
                    session.add(task)
                    await session.commit()

                fix_reason = review_loop.get("fix_reason", "code_review")
                # First-pass minor-only fix: the initial review flagged only
                # minor nits, so this fix carries skip_rereview_minor_only (set
                # at spawn time in _spawn_pipeline_task). It skips the re-review
                # the same way ci_failure/merge_conflict fixes do.
                skip_rereview_minor_only = bool(
                    review_loop.get("skip_rereview_minor_only")
                )
                # Skip re-review for fixes whose origin doesn't add review-worthy
                # logic on top of an already-decided merge gate:
                #   - ci_failure  → original review already approved code; CI is
                #                   the only gate. Wait for CI then merge.
                #   - merge_conflict → cluster sub-PR rebase. The original task
                #                   was scope-gated to skip review (small/medium
                #                   intermediate); rebase output is mechanical;
                #                   re-reviewing would be inconsistent with the
                #                   no-conflict happy path. Wait for CI then merge.
                #   - minor-only first-pass verdict → the fix only addressed
                #                   minor nits; a re-review would near-certainly
                #                   approve, so spend CI as the only gate.
                skip_rereview = (
                    fix_reason in ("ci_failure", "merge_conflict")
                    or skip_rereview_minor_only
                )
                if not scan_blocked and skip_rereview:
                    from app import pause_clock

                    ci_loop = review_loop.copy()
                    ci_loop["ci_waiting"] = True
                    ci_loop["review_approved"] = True
                    ci_loop["ci_wait_started"] = _utcnow().isoformat()
                    ci_loop[
                        "ci_wait_pause_baseline"
                    ] = await pause_clock.current_baseline(session)
                    ci_loop["ci_poll_interval"] = 10
                    task.status = TaskStatus.EVALUATING
                    task.context = {**(task.context or {}), "review_loop": ci_loop}
                    if fix_reason == "ci_failure":
                        label = "CI-only fix"
                    elif fix_reason == "merge_conflict":
                        label = "merge-conflict fix"
                    else:
                        label = "minor-nit fix"
                    skip_detail = (
                        fix_reason
                        if fix_reason in ("ci_failure", "merge_conflict")
                        else "minor_only"
                    )
                    task.latest_status_message = f"{label} applied for PR #{pr_number} — waiting for CI (review skipped: {skip_detail})"
                    session.add(task)
                    await session.commit()
                    logger.info(
                        f"[REVIEW PIPELINE] {label} for PR #{pr_number} (skip reason={skip_detail}) — skipping re-review, waiting for CI"
                    )
                elif not scan_blocked:
                    review_task, _ = await _spawn_pipeline_task(
                        task,
                        "review",
                        session,
                        pr_number,
                        branch,
                        base_branch,
                        original_task_id=original_task_id,
                    )
                    if review_task:
                        logger.info(
                            f"[REVIEW PIPELINE] Spawned review after fix for PR #{pr_number} → task {review_task.id}"
                        )
                    else:
                        task.latest_status_message = f"Fix applied but review not spawned for PR #{pr_number} — iteration cap reached"
                        task.requires_approval = True
                        session.add(task)
                        cap_alert_title = (
                            f"PR #{pr_number} stalled — iteration cap reached"
                        )
                        existing_cap_alert = (
                            (
                                await session.execute(
                                    select(Alert).where(
                                        Alert.project_id == task.project_id,
                                        Alert.severity == AlertSeverity.WARNING,
                                        Alert.dismissed_at.is_(None),
                                        Alert.title == cap_alert_title,
                                    )
                                )
                            )
                            .scalars()
                            .first()
                        )
                        if not existing_cap_alert:
                            alert = Alert(
                                project_id=task.project_id,
                                severity=AlertSeverity.WARNING,
                                title=cap_alert_title,
                                message=f"Fix was applied to PR #{pr_number} but the review iteration cap was reached. Manual intervention required.",
                                task_id=task.id,
                            )
                            session.add(alert)
                            await session.commit()
                            await broadcast_to_project(
                                str(task.project_id),
                                "alert_created",
                                {
                                    "alert_id": str(alert.id),
                                    "severity": "WARNING",
                                    "title": alert.title,
                                },
                            )
                        else:
                            await session.commit()
                        logger.warning(
                            f"[REVIEW PIPELINE] WARNING: Review spawn after fix returned None for PR #{pr_number}"
                        )

            # Completion handlers — wrapped in try/except to prevent VM leak.
            # If a handler throws after the early commit, BackgroundTasks (including
            # delete_agent_vm) won't run on a 500 response. We catch, log, and ensure
            # VM cleanup via asyncio.create_task as a fallback.
            try:
                if task_type == "plan":
                    plan_logs = payload.logs or task.latest_status_message or ""
                    await _handle_plan_completion(task, plan_logs, session)

                elif task_type == "design_doc_update":
                    doc_logs = payload.logs or task.latest_status_message or ""
                    # Round 3 C3 + C4: ``locked_project`` was acquired in
                    # canonical Project → Task order at the top of
                    # _process_post_commit via ``lock_project_then_task``.
                    # The handler skips its own re-acquisition when given
                    # a pre-locked Project. None propagates the legacy
                    # bounded-retry path inside the handler (defensive
                    # only — the canonical pair-lock should have produced
                    # a non-None project for design_doc_update tasks).
                    # Canonical order is Project before Task — see
                    # docs/concurrency.md.
                    await _handle_design_doc_completion(
                        task, doc_logs, session, project=locked_project
                    )

                elif task_type == "strategic_review":
                    review_logs = payload.logs or task.latest_status_message or ""
                    # Round 3 C3 + C4: see _handle_design_doc_completion above.
                    # ``locked_project`` was acquired in canonical
                    # Project → Task order at the top of _process_post_commit.
                    await _handle_strategic_completion(
                        task, review_logs, session, project=locked_project
                    )

                elif task_type == "research":
                    research_logs = payload.logs or task.latest_status_message or ""
                    await _handle_research_completion(task, research_logs, session)

                elif task_type == "security_audit":
                    audit_logs = payload.logs or task.latest_status_message or ""
                    await _handle_security_audit_completion(task, audit_logs, session)

                elif task_type == "perf":
                    perf_logs = payload.logs or task.latest_status_message or ""
                    await _handle_perf_completion(task, perf_logs, session)

                elif task_type == "self_audit":
                    self_audit_logs = payload.logs or task.latest_status_message or ""
                    await _handle_self_audit_completion(task, self_audit_logs, session)

                elif task_type == "memo":
                    memo_logs = payload.logs or task.latest_status_message or ""
                    await _handle_memo_completion(task, memo_logs, session)

                elif payload.artifact_type == ArtifactType.INVESTIGATION_REPORT:
                    await _handle_investigation_completion(task, payload, session)
            except Exception as handler_exc:
                logger.error(
                    f"[STATUS] Completion handler failed for {task_type} task {task.id}: {handler_exc!r}",
                    exc_info=True,
                )
                # Detect transient errors (GitHub API, DB, network) and requeue instead of FAILED
                err_str = str(handler_exc).lower()
                is_transient = any(
                    kw in err_str
                    for kw in [
                        "api.github.com",
                        "502",
                        "503",
                        "connection",
                        "timeout",
                        "rate limit",
                        "ssl",
                        "name resolution",
                        "connection refused",
                    ]
                )
                if is_transient and task.attempt_count < _runtime_config["max_retries"]:
                    task.status = TaskStatus.QUEUED
                    # The earlier COMPLETED/FAILED assignment fired the SQLAlchemy set
                    # event which added task.id to _terminated_task_ids, revoking the
                    # JWT. Since the completion handler hit a transient error and we're
                    # requeuing, drop the cache entry so the next VM can claim.
                    from app.routers.agent import _terminated_task_ids

                    _terminated_task_ids.pop(str(task.id), None)
                    task.attempt_count += 1
                    task.vm_instance_id = f"agent-vm-{uuid.uuid4().hex}"
                    task.last_heartbeat = None
                    task.started_at = None
                    # Handler ran after a successful VM provision — reset provision budget
                    # so a transient handler failure doesn't consume retries meant for VM issues.
                    handler_ctx = dict(task.context or {})
                    handler_ctx.pop("provision_failures", None)
                    task.context = handler_ctx
                    task.latest_status_message = f"Handler hit transient error, requeuing (attempt {task.attempt_count}): {type(handler_exc).__name__}"
                    logger.warning(
                        f"[HANDLER] Transient error for {task.id}, requeuing: {handler_exc}"
                    )
                else:
                    task.status = TaskStatus.FAILED
                    task.latest_status_message = f"Completed but handler failed: {type(handler_exc).__name__}: {str(handler_exc)[:200]}"
                    from app.failure_classification import persist_failure_category
                    from app.routers.agent_status import record_task_result

                    _fc_handler = classify_failure(task)
                    persist_failure_category(task, _fc_handler)
                    record_task_result(
                        success=False,
                        failure_class=_fc_handler,
                        task_id=str(task.id),
                        task_type=(task.first_payload or {}).get("type", ""),
                    )
                session.add(task)

        # 6. Parent task rollup — delegated to shared function
        if task.parent_task_id and payload.status in [
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ]:
            try:
                await _check_and_trigger_rollup(task, session)
            except Exception as e:
                logger.error(
                    f"[ROLLUP] Rollup check failed for task {task.id}: {e!r}",
                    exc_info=True,
                )

        # 6b. Update direction research history outcomes for child tasks of a research cycle
        _task_ctx = task.context or {}
        if (
            _task_ctx.get("direction_id")
            and _task_ctx.get("research_task_id")
            and payload.status
            in [TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED]
        ):
            try:
                from app.direction_outcomes import _update_direction_research_outcomes

                await _update_direction_research_outcomes(
                    task.project_id,
                    _task_ctx["direction_id"],
                    _task_ctx["research_task_id"],
                    session,
                )
            except Exception as _outcome_exc:
                logger.warning(
                    f"[RESEARCH] Failed to update direction research outcomes for task {task.id}: {_outcome_exc!r}"
                )

        await _record_remediation_if_terminal(task, payload.status, session)
        await _record_regression_signal_if_matching(task, payload.status, session)
        await _handle_investigation_terminal_failure(task, payload.status, session)

        # Release-qualifier finalization — fires from EVERY terminal path
        # (COMPLETED / FAILED / CANCELLED) for BOTH a release_audit task and a
        # qualifier-smoketest task. This deliberately lives in the terminal
        # section (not the COMPLETED-only review-pipeline block above): a
        # qualifier child that fails/cancels must still advance qualification,
        # or the ReleaseVersion stays {status:"pending"} forever and the
        # version is wedged.
        #
        # Why here and not in agent_status.py: this runs after the status
        # endpoint's early commit, on a fresh post-commit session. The task's
        # terminal status AND any SMOKETEST_RESULT / RELEASE_AUDIT_REPORT
        # Artifact the agent posted are therefore already committed and visible
        # to the finalize SELECT (_latest_artifact_logs). The old
        # agent_status.py hook ran pre-flush with autoflush disabled and could
        # not see its own just-added Artifact.
        #
        # maybe_finalize_qualification opens its OWN dedicated DB session
        # (AsyncSessionLocal) for the entire finalize→promote chain — it does
        # NOT use this post-commit ``session``. That decoupling is deliberate:
        # promote() merges to production main and issues an intermediate
        # commit() to release its row lock before 30-120s of GitHub I/O; on a
        # caller's session that commit would prematurely commit this handler's
        # unrelated dirty work (the completed task's terminal status,
        # TaskHistory, Artifacts, remediation/regression records). Because it
        # owns its session, this call also cannot self-deadlock against the
        # Task-row lock this handler holds (it locks the ReleaseVersion row, a
        # different row). It is idempotent (it FOR UPDATE-locks the version and
        # no-ops unless last_qualification.status == "pending" and the required
        # children are terminal) and a cheap O(1) no-op — no session opened, no
        # I/O — for the overwhelming majority of tasks (those with no
        # release_version_id in context). Keyed on context["release_version_id"]
        # and/or task_type == "release_audit" / context["qualifier_smoketest"].
        if payload.status in (
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        ):
            _qual_ctx = task.context or {}
            _qual_first_payload = task.first_payload or {}
            _is_qualifier_child = bool(
                _qual_ctx.get("release_version_id")
                or _qual_first_payload.get("type") == "release_audit"
                or _qual_ctx.get("qualifier_smoketest")
            )
            if _is_qualifier_child:
                try:
                    from app import release_qualifier

                    # maybe_finalize_qualification opens its OWN dedicated
                    # session (see the comment above) — never this post-commit
                    # ``session``. ``_session_factory`` is threaded through so
                    # the dedicated session is opened from the SAME factory
                    # this handler runs on: in production that is None →
                    # AsyncSessionLocal (a genuine independent connection,
                    # preserving the transaction-decoupling); under the test
                    # harness it is the test-engine factory, so the finalize
                    # sees the committed seed data and its commit is visible to
                    # the test's assertions instead of opening an unrelated
                    # connection on a different event loop.
                    await release_qualifier.maybe_finalize_qualification(
                        task, session_factory=_session_factory
                    )
                except Exception as _qual_exc:
                    logger.error(
                        f"[RELEASE_QUALIFIER] maybe_finalize_qualification failed "
                        f"for terminal task {task.id} (status={payload.status}): "
                        f"{_qual_exc!r}",
                        exc_info=True,
                    )

        # 7. Project State Aggregation
        # Only consider executable tasks (those with a VM) for aggregation.
        # Parent/logical tasks are updated to match the project outcome.
        #
        # 2026-05-05 fix: a QUEUED leaf task waiting on warm-handoff also has
        # ``vm_instance_id=None`` (the warm target VM is in ``warm_target_vm_id``,
        # the real ``vm_instance_id`` is set only at claim). Without the QUEUED
        # exclusion below, an aggregation pass triggered by a sibling completing
        # will sweep that QUEUED task into ``parent_tasks`` and force-FAIL it
        # with "Project aggregation: child task failed" — the task never even
        # ran. Real parent/logical tasks (research, plan) sit in EVALUATING
        # after children spawn, never in QUEUED. Production incident: task
        # 582a0210 force-failed at 19:47:30 while waiting on a warm-handoff
        # from sibling 609b380c on agent-vm-540ee.
        all_tasks_stmt = select(Task).where(Task.project_id == task.project_id)
        all_tasks = (await session.execute(all_tasks_stmt)).scalars().all()
        executable_statuses = [t.status for t in all_tasks if t.vm_instance_id]
        parent_tasks = [
            t
            for t in all_tasks
            if not t.vm_instance_id
            and t.status
            not in [
                TaskStatus.QUEUED,  # leaf awaiting claim/warm-handoff, not a parent
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ]
        ]

        project_stmt = select(Project).where(Project.id == task.project_id)
        project = (await session.execute(project_stmt)).scalars().first()

        if (
            project
            and project.status != ProjectStatus.CANCELLED
            and executable_statuses
        ):
            # 2026-05-05 fix: only transition project status when ALL executable
            # tasks are terminal. Previously the ``any FAILED`` branch fired
            # any time an old FAILED row sat in ``executable_statuses`` (which
            # spans every task that ever had a VM, including stale FAILED
            # tasks not yet auto-archived for up to 7 days). For autonomous
            # projects with continuous work, that pinned the project at FAILED
            # forever and re-fired the parent-aggregation sweep on every new
            # task transition. Requiring all-terminal makes the aggregation a
            # no-op for autonomous projects (always something in flight) while
            # preserving finite-project semantics.
            all_terminal = all(
                s in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED)
                for s in executable_statuses
            )
            new_project_status = None
            if all_terminal:
                if all(s == TaskStatus.COMPLETED for s in executable_statuses):
                    new_project_status = ProjectStatus.COMPLETED
                elif any(s == TaskStatus.FAILED for s in executable_statuses):
                    new_project_status = ProjectStatus.FAILED
                elif all(
                    s in [TaskStatus.COMPLETED, TaskStatus.CANCELLED]
                    for s in executable_statuses
                ):
                    new_project_status = ProjectStatus.CANCELLED

            if new_project_status:
                project.status = new_project_status
                session.add(project)
                # Update parent/logical tasks to match the project outcome
                terminal_task_status = (
                    TaskStatus.COMPLETED
                    if new_project_status == ProjectStatus.COMPLETED
                    else TaskStatus.FAILED
                    if new_project_status == ProjectStatus.FAILED
                    else TaskStatus.CANCELLED
                )
                for pt in parent_tasks:
                    pt.status = terminal_task_status
                    if terminal_task_status == TaskStatus.FAILED:
                        pt.latest_status_message = (
                            "Project aggregation: child task failed"
                        )
                    elif terminal_task_status == TaskStatus.COMPLETED:
                        pt.latest_status_message = "Project completed"
                    else:
                        pt.latest_status_message = "Project cancelled"
                    session.add(pt)
                await session.commit()
                await broadcast_to_project(
                    str(project.id), "project_updated", {"status": new_project_status}
                )

        # Audit W2 (D2/D4): backstop for the synchronous strategic chain.
        # If a strategic task (design_doc_update or strategic_review) reached
        # a terminal status but the completion handlers above failed to update
        # the StrategicCycle row, the cycle would stay "phase*_running"
        # forever and the stalled-cycle reaper would have to clean it up
        # later. Catch it here while the task lifecycle still has context.
        try:
            cycle_id = task.strategic_cycle_id or (task.context or {}).get(
                "strategic_cycle_id"
            )
            if cycle_id and payload.status in (
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            ):
                from app import strategic_cycle

                cycle_row = await strategic_cycle.get(session, cycle_id)
                if (
                    cycle_row is not None
                    and cycle_row.outcome in strategic_cycle.ACTIVE_OUTCOMES
                ):
                    task_type = task.first_payload.get("type", "")
                    failed_phase = (
                        "phase1" if task_type == "design_doc_update" else "phase2"
                    )
                    logger.warning(
                        f"[STRATEGIC] Task {task.id} ({task_type}) reached {payload.status} "
                        f"but cycle {cycle_id} still in {cycle_row.outcome} — backstop marking failed_{failed_phase}"
                    )
                    await strategic_cycle.mark_failure(
                        session,
                        cycle_id,
                        phase=failed_phase,
                        reason=f"lifecycle_backstop: task {task.id} terminal in {payload.status} without completion-handler cycle update",
                    )
                    await session.commit()
        except Exception as _bk_exc:
            logger.error(
                f"[STRATEGIC] Lifecycle backstop failed for task {task.id}: {_bk_exc!r}",
                exc_info=True,
            )

        # Final commit for any deferred changes (status, artifacts, spawned tasks)
        if session.dirty or session.new:
            await session.commit()

        # Broadcast AFTER all processing is complete (artifacts saved, pipeline spawned)
        await broadcast_to_project(
            str(task.project_id),
            "task_updated",
            {"task_id": str(task.id), "status": payload.status},
        )

        return {
            "status": "received",
            "message": f"Status update for task {payload.task_id} recorded.",
        }
