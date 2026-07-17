"""Retry-prompt builders extracted from task_lifecycle.py.

Pure string/dict builders that assemble the 'Previous Fix Attempts' /
retry-guidance suffix and related helpers.  No AsyncSession, no app.main.

Extracted from task_lifecycle.py — see that module for the re-exports that
preserve existing call sites.
"""

import re

from app.context_bounds import bound_review_history
from app.failure_classification import (
    _timeout_subtype_from_message,
    TIMEOUT_CASCADE,
    TIMEOUT_HEARTBEAT_LOST,
    TIMEOUT_ITERATION_LIMIT,
    TIMEOUT_OOM,
    TIMEOUT_PROCESS_CRASH,
)
from app.retry_budget import CODE_ERROR_STAGE_GUIDANCE


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

_RETRY_SUFFIX_AUTO_PREFIXES = re.compile(
    r"^(?:RETRY GUIDANCE:"
    r"|IMPORTANT: The previous attempt TIMED OUT"
    r"|NOTE: The previous attempt was CANCELLED"
    r"|WARNING: The same tests are failing).*?(?=\n\n|\Z)",
    flags=re.MULTILINE | re.DOTALL,
)

_ANALYSIS_ARTIFACT_KEYS = (
    ("_pr_diff_summary", "_previous_diff_summary"),
    ("_test_failure_summary", "_previous_test_failure_summary"),
    ("_self_review_outcome", "_previous_self_review_outcome"),
    ("_analysis_plan", "_previous_analysis_plan"),
)

_OPERATOR_RETRY_GUIDANCE_HEADING = "## Operator retry guidance"


def materialize_operator_retry_guidance(payload: dict, guidance: str) -> None:
    """Persist the latest operator retry instruction in the delivered payload.

    ``context['_original_prompt_suffix']`` is an internal dedup snapshot, not an
    agent-facing prompt field. Manual retries therefore use this helper to put
    guidance in ``payload.prompt_suffix`` -- the field rendered by handlers.
    The prior operator block is replaced (not stacked), while every non-operator
    suffix supplied when the task was spawned is preserved verbatim.
    """
    guidance = guidance.strip()
    previous = payload.get("operator_retry_guidance")
    existing = payload.get("prompt_suffix")
    existing = existing if isinstance(existing, str) else ""

    if isinstance(previous, str) and previous.strip():
        previous_block = f"{_OPERATOR_RETRY_GUIDANCE_HEADING}\n{previous.strip()}"
        # Defensive loop for rows written during an earlier duplicate retry.
        while previous_block in existing:
            existing = existing.replace(previous_block, "", 1).strip()

    current_block = f"{_OPERATOR_RETRY_GUIDANCE_HEADING}\n{guidance}"
    payload["operator_retry_guidance"] = guidance
    payload["prompt_suffix"] = (
        f"{existing.rstrip()}\n\n{current_block}" if existing.strip() else current_block
    )


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


def _copy_analysis_artifacts_to_previous(ctx: dict) -> None:
    # Mirror failed-attempt artifacts into _previous_* so the agent's buildRetryContext
    # surfaces them — matches task_lifecycle._requeue_parent_task.
    for src, dst in _ANALYSIS_ARTIFACT_KEYS:
        if ctx.get(src):
            ctx[dst] = ctx[src]


def _detect_ci_review_disagreement(
    issues: list,
    feedback: str,
    ci_check: "dict | None",
    pr_number: "int | None",
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
        "with mechanical verify steps that prove the gates pass from a fresh install — for example:\n"
        "     - Run `cd /tmp/workspace/admin-ui && npm ci && npm run typecheck` to verify frontend code compiles and has no type errors\n"
        "     - Reference the green CI run URL as the authoritative build proof (the full vite bundle runs successfully in CI)\n"
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
    # Bound on the way out (both returns) so this APPROVE write path enforces the
    # same review_history cap as the review_pipeline spawn store: it carries the
    # existing list forward, which on a pre-fix in-flight row may still hold a
    # multi-MB log-scraping `feedback` blob — re-bounding here self-heals it. The
    # APPROVE entry itself stores empty feedback (never a fat source). The
    # idempotency check below reads only structured `iteration`/`verdict`, so the
    # bound does not change it.
    if (
        history
        and history[-1].get("iteration") == iteration
        and history[-1].get("verdict") == "APPROVE"
    ):
        return bound_review_history(history)
    history.append(
        {
            "iteration": iteration,
            "feedback": "",
            "verdict": "APPROVE",
            "changed_files": list(changed_files or [])[:30],
        }
    )
    return bound_review_history(history)


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
