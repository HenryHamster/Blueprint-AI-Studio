# Agent Handlers Reference

Guide to task handler implementations in the TypeScript agent (`agent/src/handlers/`).

Each task type (create, review, fix, research, etc.) is handled by a dedicated function that runs on the agent VM and orchestrates the Claude Code subprocess.

---

## Overview

### Handler Architecture

```
Task dispatched to agent
    ↓
Agent claims task via /api/agent/claim
    ↓
Match task_type to handler (index.ts)
    ↓
Handler function called:
    - repo clone
    - Claude Code invocation (streaming)
    - logs captured & streamed back
    - artifacts generated
    - exit state reported via /api/agent/status
    ↓
Task marked COMPLETED or FAILED
```

**Handler signature:**

```typescript
type Handler = (
  taskId: string,
  payload: Record<string, unknown>,    // Task-specific input
  task: Record<string, unknown>,       // Full task object
  ghToken: string,                     // GitHub OAuth token
  owner: string,                       // GitHub owner
  repo: string,                        // GitHub repo
  ws: WebSocket | null,                // Broadcast channel for logs
  statusRef: { value: string },        // Current status (IN_PROGRESS, etc.)
  model?: string,                      // LLM model override
  effort?: string                      // Effort level (low/medium/high)
) => Promise<void>;
```

---

## Standard Code-Change Handlers

These handlers clone the repo, run Claude Code, and optionally create a PR.

### `handleCreate` (standard.ts)

**Task type:** `create`

**Purpose:** Implement a new feature from a natural-language prompt.

**Input payload:**
```json
{
  "prompt": "string",          // Feature description
  "base_branch": "string",     // Target branch (e.g., "main")
  "target_branch": "string"    // Feature branch name
}
```

**Workflow:**
1. Clone repo to target branch
2. Invoke Claude Code with the prompt
3. Stream output back to admin UI
4. Run lint/test checks (fix loop)
5. Create PR with auto-generated title
6. Report commit hash + PR URL

**Output:**
```json
{
  "artifact_type": "PULL_REQUEST",
  "pr_url": "https://github.com/...",
  "commit_hash": "abc123..."
}
```

---

### `handleRefactor` (standard.ts)

**Task type:** `refactor`

**Purpose:** Improve code quality without changing behavior.

**Input payload:**
```json
{
  "description": "string",      // Refactoring goal
  "scope": "string | string[]"  // Files/directories to refactor
}
```

**Similar to `create` but:**
- Scope-limited to specified files
- Emphasizes maintainability over features
- Stricter on test coverage (all existing tests must pass)

---

### `handleDocs` (standard.ts)

**Task type:** `docs`

**Purpose:** Write or update documentation.

**Input payload:**
```json
{
  "documentation_task": "string",   // What docs to write
  "scope": "string | string[]"      // Files to document
}
```

**Workflow:**
1. Clone repo
2. Claude generates documentation (README, docstrings, API docs)
3. Run the normal code-change loop for docs work (checks, then PR creation with a `docs:` title prefix)
4. Report artifact type: `RESEARCH_FINDINGS` or custom

---

### `handleDeps` (standard.ts)

**Task type:** `deps`

**Purpose:** Update dependencies and manage versions.

**Input payload:**
```json
{
  "target": "string",                // "python" or "typescript" or "all"
  "constraints": {"package": ">=1.0"}
}
```

---

### `handleQA` (standard.ts)

**Task type:** `qa`

**Purpose:** Write or run quality assurance tests.

**Input payload:**
```json
{
  "test_type": "unit|integration|e2e",
  "scope": "string | string[]"
}
```

---

## Review & Fix Handlers

### `handleReview` (review-fix.ts)

**Task type:** `review`

**Purpose:** Review a PR and leave detailed comments.

**Input payload:**
```json
{
  "pr_number": 123,
  "focus_areas": ["performance", "security", "correctness"],
  "include_tests": true
}
```

**Workflow:**
1. Fetch PR details (changed files, diffs)
2. Invoke Claude as a code reviewer
3. Generate structured findings (CRITICAL, MAJOR, MINOR)
4. Post findings as PR comments
5. Submit review ("Changes Requested" or "Approved")

**Output artifact:**
```json
{
  "artifact_type": "RESEARCH_FINDINGS",
  "findings": [
    {
      "severity": "CRITICAL",
      "file": "src/auth.ts",
      "line": 42,
      "message": "Missing input validation"
    },
    ...
  ]
}
```

---

### `handleFix` (review-fix.ts)

**Task type:** `fix`

**Purpose:** Address issues identified in a code review.

**Input payload:**
```json
{
  "pr_number": 123,
  "issues": [
    { "severity": "CRITICAL", "message": "..." },
    { "severity": "MAJOR", "message": "..." }
  ],
  "previous_attempts": [
    { "issue_id": "...", "approach": "...", "result": "..." }
  ]
}
```

**Workflow:**
1. Clone repo and checkout the PR branch
2. For each issue, attempt a fix
3. Run tests to verify the fix doesn't break things
4. Create a new commit with fix details
5. Push and report
6. On next iteration (if reviewer finds issues), repeat

**Fix loop telemetry:**
```json
{
  "iteration": 1,
  "issues_addressed": 3,
  "issues_deferred": 1,
  "exit_reason": "all_resolved|max_iterations|timeout"
}
```

**Note:** Fix agent has a 5-iteration limit per PR to prevent infinite loops.

---

## Research & Planning Handlers

### `handleResearch` (research-plan.ts)

**Task type:** `research` (only when `research_mode` is *not* `"strategic"` — see note below)

**Purpose:** Investigate a codebase area and produce structured findings.

**Input payload:**
```json
{
  "research_topic": "string",
  "scope": ["path/to/file.py", "path/to/dir/"],
  "research_mode": "autonomous|manual|strategic",
  "focus_areas": ["performance", "security", "maintainability"],
  "depth": "shallow|medium|deep"
}
```

**Workflow:**
1. Clone repo
2. Analyze code in scope (async traversal)
3. Invoke Claude with analysis prompt
4. Generate structured findings (problems, opportunities, recommendations)
5. Return findings as artifact

**Output artifact:**
```json
{
  "artifact_type": "RESEARCH_FINDINGS",
  "findings": {
    "scope_summary": "Analyzed 12 files, ~5000 LOC",
    "problems": [
      { "category": "performance", "severity": "major", "description": "..." }
    ],
    "opportunities": [...],
    "recommendations": [...]
  }
}
```

**Strategic mode:**
The `research` task type is registered to a `dispatchResearch` wrapper (see
[Handler Registration](#handler-registration)). When `research_mode="strategic"`,
that wrapper routes to `handleStrategic` (the same handler the `strategic_review`
task type uses) instead of `handleResearch`; the strategic path generates the
next autonomous-research directions. For all other `research_mode` values the
wrapper calls `handleResearch` as described above.

---

### `handlePlan` (research-plan.ts)

**Task type:** `plan`

**Purpose:** Generate an implementation plan for future work.

**Input payload:**
```json
{
  "objective": "string",
  "scope": "string | string[]",
  "constraints": ["timeline", "budget", "team"]
}
```

**Output:** Structured plan artifact with:
- Phase breakdown
- Estimated effort per phase
- Dependencies
- Risk assessment

---

## Specialized Task Handlers

All in `specialized.ts`.

### `handleSecurityAudit`

**Task type:** `security_audit`

Audit code for security vulnerabilities (OWASP Top 10, injection, XSS, etc.).

**Output:**
```json
{
  "artifact_type": "SECURITY_REPORT",
  "vulnerabilities": [
    { "severity": "critical", "cwe": "CWE-79", "description": "..." }
  ]
}
```

---

### `handleSelfAudit`

**Task type:** `self_audit`

Self-review checklist for autonomous agents. Checks own code for correctness, coverage, security.

---

### `handleMigrate`

**Task type:** `migrate`

Generate database migration scripts (SQL, Alembic, etc.).

---

### `handleSmoketest`

**Task type:** `smoketest`

Run smoke tests against a deployed environment (health checks, basic workflows).

**Output:**
```json
{
  "artifact_type": "SMOKETEST_RESULT",
  "status": "pass|fail",
  "test_results": [...]
}
```

---

### `handleDebug`

**Task type:** `debug`

Investigate a bug report and produce diagnostic findings.

---

### `handlePerf`

**Task type:** `perf`

Profile and optimize performance (latency, memory, throughput).

**Output:**
```json
{
  "artifact_type": "PERF_REPORT",
  "profile": {...},
  "bottlenecks": [...],
  "recommendations": [...]
}
```

---

### `handleInvestigate`

**Task type:** `investigate`

Open-ended investigation of an issue (root cause analysis, post-mortem, feasibility study).

---

### `handleRepoBootstrap`

**Task type:** `repo_bootstrap`

Performs a first-cycle repository bootstrap: reads repo structure, confirms build/test/lint commands, and writes a structured repo overview so the first strategic cycle starts with grounded knowledge.

**Workflow:**
1. Clone repo at base branch (read-only; no edits, no PR)
2. Read build manifests, CI configs, and declared `files_to_read`
3. Attempt to run detected build, test, and lint commands
4. Write back confirmed commands via `PATCH /api/agent/projects/{id}/build-config/commands`
5. Emit repo overview and command-memory outcome files

**Output** (returned in COMPLETED status payload and `/tmp/bootstrap_overview_<taskId>.json`):
```json
{
  "tech_stack": "...",
  "directory_layout": "...",
  "first_steps_summary": "...",
  "entry_points": "...",
  "test_layout": "...",
  "key_conventions": "...",
  "environment_requirements": "...",
  "source_revision": "abc123..."
}
```

**Command-memory outcome** (in `/tmp/command_memory_<taskId>.json`):
```json
{
  "status": "success | missing_commands | writeback_failed",
  "detail": "optional short explanation",
  "updated_paths": [".", "backend", "admin-ui"]
}
```

---

### `handleReleaseAudit`

**Task type:** `release_audit`

Verify code is production-ready before release.

---

## Verify Tests Handler

### `handleVerifyTests` (verify-tests.ts)

**Task type:** `verify_tests`

Clone a PR head, detect and run the test suite, and post a `VERIFY_TESTS_RESULT` verdict. Used as a merge-gate step in the CI pipeline.

**Workflow:**
1. Clone repo at PR head branch
2. Auto-detect test runners (language-specific discovery)
3. Run the test suite with a bounded timeout
4. On setup failure, retry up to `MAX_VERIFY_SETUP_RETRIES` times
5. Emit `VERIFY_TESTS_RESULT` artifact with `detected`, `passed`, and test list

**Key result fields:**
- `detected` — whether any test suite was found
- `passed` — whether all detected tests passed
- `setupFailed` — set when infrastructure (dep install, runner spawn) failed
- `partialSetupFailed` — set when only some runners in a polyglot repo failed setup
- `recovered` — set when the stored build config was stale and a replacement suite was found

---

## Compute Experiment Handler

### `handleComputeExperiment` (compute-experiment.ts)

**Task type:** `compute_experiment`

Launch a budgeted compute job via `POST /api/agent/compute-jobs` and immediately park the task on a `compute_job_terminal` wake registration, releasing the agent VM slot. The backend wakes the task when the job reaches a terminal state.

**Workflow:**
1. Validate launch fields (command, spend cap, deadline)
2. POST job to `/api/agent/compute-jobs`
3. Register a `compute_job_terminal` park-and-wake keyed to the launched job ID
4. Agent VM is released; backend monitor accrues cost and enforces limits
5. On wake: backend finalizes the task with the job's terminal outcome and result artifact

**No repo clone is performed.** All cloud side-effects are mediated by the backend.

---

## Web Research Handler

### `handleWebResearch` (web-research.ts)

**Task type:** `web_research`

Web-first research producing a cited knowledge document. The agent runs with web access, searches for sources, and emits a structured document that a forward-quality gate grades before it is persisted as a `KnowledgeDocument`.

**Workflow:**
1. No repo clone (web-access only)
2. Fetch prior knowledge context from existing documents
3. Run Claude Code with web search tools enabled
4. Parse structured output (title/summary/body/citations/tags/topic)
5. Repair/validate JSON with `repairStructuredOutput`
6. Submit to forward-quality gate; persist as `KnowledgeDocument` on pass

---

## Design Handler

### `handleDesign` (design.ts)

**Task type:** `design`

Codebase-grounded design producing a durable design document. Mirrors `handleWebResearch` but runs over a repo clone instead of the web. Emits a structured `KnowledgeDocument` graded by the same forward-quality gate.

**Workflow:**
1. Clone repo (read-only; no edits, no PR)
2. Fetch prior knowledge and design-doc context
3. Run Claude Code over the codebase
4. Parse structured output (title/summary/body/citations/tags/topic)
5. Submit to forward-quality gate; persist as `KnowledgeDocument` on pass

---

## Release Audit Challenge Handler

### `handleReleaseAuditChallenge` (audit-challenge.ts)

**Task type:** `release_audit_challenge`

Independently re-examines release-audit findings and emits per-finding verdicts (`confirmed`/`refuted_false_positive`/`uncertain`). Spawned as a binding second-opinion pass alongside `release_audit`.

**Output artifact:**
```json
{
  "artifact_type": "RELEASE_AUDIT_CHALLENGE",
  "challenged_candidate_sha": "abc123...",
  "verdicts": [
    {
      "severity": "critical",
      "summary": "...",
      "file": "path/to/file.ts",
      "verdict": "confirmed",
      "reason": "Reproduced at line 42 — ..."
    }
  ]
}
```

**Verdict values:**
- `confirmed` — the challenger independently reproduced the issue; it is a genuine problem.
- `refuted_false_positive` — the challenger investigated and the issue does NOT hold.
- `uncertain` — the challenger could not confidently confirm or refute the finding.

**Workflow:**
1. Clone repo at `candidate_sha`
2. Provision audit fetch helpers and design-doc context
3. Run Claude Code instructed to challenge each finding independently
4. Emit structured `RELEASE_AUDIT_CHALLENGE` artifact with per-finding verdicts

---

## Release Merge Grade Handler

### `handleReleaseMergeGrade` (merge-grade.ts)

**Task type:** `release_merge_grade`

Grade whether the merged release diff satisfies the version goal. An independent agent evaluates goal coverage without knowledge of strategic progress narrative.

**Input payload:**
```json
{
  "candidate_sha": "string",        // Exact commit to grade (pinned)
  "goal": "string",                 // Version goal statement to evaluate against
  "base_branch": "string"           // Base branch for diff context
}
```

**Output artifact:**
```json
{
  "artifact_type": "RELEASE_MERGE_GRADE",
  "graded_candidate_sha": "<40-hex>",
  "covers_goal": true,
  "coverage_score": 0.7,
  "biggest_gap": "Authentication flow incomplete — token refresh not implemented",
  "rationale": "PR merges core auth structure but defers token rotation to next sprint"
}
```

**Output fields:**
- `covers_goal` — Boolean: true if merged diff fully satisfies the stated goal
- `coverage_score` — Float [0.0, 1.0]: degree of goal satisfaction (1.0 = complete)
- `biggest_gap` — String: single most critical thing still unmet toward the goal (required, non-empty)
- `rationale` — String: brief justification for the score

**Workflow:**
1. Clone repo at pinned `candidate_sha` (remains fixed even if release advances)
2. Provision audit helpers and design-doc context for framing
3. Run Claude Code instructed to grade merged diff against stated goal, blind to narrative
4. Emit structured `RELEASE_MERGE_GRADE` artifact with coverage verdict

**Contract:** The output shape is a stable contract consumed by the release qualifier's blind merge-diff gate (`release_qualifier.py`). Field names must not change without updating the consumer.

---

## Design Doc Handler

### `handleDesignDocUpdate` (design-doc.ts)

**Task type:** `design_doc_update`

Update or create design documentation.

**Input payload:**
```json
{
  "doc_title": "string",
  "doc_section": "string",
  "changes": "string"
}
```

---

## Memo Handler

### `handleMemo` (memo.ts)

**Task type:** `memo`

Document a piece of reasoning without touching the repo.

**Input payload:**
```json
{
  "topic": "string",
  "reasoning_kind": "string",
  "prompt": "string",
  "source_finding": {...},
  "direction_id": "uuid | null"
}
```

**Workflow:**
1. No repo clone
2. Claude generates reasoning artifact
3. Persisted as MEMO artifact (citable by strategic review)

**Use case:** Strategic directions can cite memos as part of their decision rationale.

---

## Error Handling

All handlers wrap their work in try/catch and:
1. Capture error logs
2. Classify failure (clone_failed, lint_failed, test_failed, etc.)
3. Report via `/api/agent/status` with failure_category
4. Optionally trigger retry

**Failure categories:**
- `clone_failed` — Git clone error
- `build_failed` — Build/compilation error
- `test_failed` — Test suite failure
- `lint_failed` — Linter violations
- `pr_creation_failed` — PR submission error
- `claude_code_failed` — Claude exited with error
- `parse_failed` — Couldn't parse Claude output
- `timeout` — Handler exceeded timeout
- `rate_limited` — API rate limit hit
- `oauth_rotation_failed` — GitHub token refresh failed

---

## Logging & Telemetry

Handlers use centralized logging:
- `sendLog(taskId, message)` — Emit to admin UI
- `persistAndPostStatus()` — Save logs to task.context
- `sendHeartbeatSync()` — Keep-alive + metrics

**Metrics captured:**
- `handler_duration_secs` — Total handler runtime
- `setup_secs` — Pre-handler setup time (repo clone, etc.)
- `fix_loop_events` — Iteration telemetry (lint, test fixes)
- `prompt_fingerprint` — Prompt hash for caching analysis
- `self_review_outcome` — Agent's self-assessment

---

## Handler Registration

All handlers are registered in `handlers/index.ts`:

```typescript
// research task type forwards to the strategic handler when research_mode is
// 'strategic'; otherwise it runs the tactical research path. strategic_review
// always goes directly to handleStrategic.
const dispatchResearch: Handler = (taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort) => {
  if (payload.research_mode === 'strategic') {
    return handleStrategic(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
  }
  return handleResearch(taskId, payload, task, ghToken, owner, repo, ws, statusRef, model, effort);
};

export const handlers: Record<AgentTaskType, Handler> = {
  create: handleCreate,
  review: handleReview,
  fix: handleFix,
  verify_tests: handleVerifyTests,
  plan: handlePlan,
  research: dispatchResearch,
  docs: handleDocs,
  security_audit: handleSecurityAudit,
  self_audit: handleSelfAudit,
  qa: handleQA,
  deps: handleDeps,
  migrate: handleMigrate,
  smoketest: handleSmoketest,
  refactor: handleRefactor,
  debug: handleDebug,
  perf: handlePerf,
  investigate: handleInvestigate,
  design_doc_update: handleDesignDocUpdate,
  strategic_review: handleStrategic,
  release_audit: handleReleaseAudit,
  release_audit_challenge: handleReleaseAuditChallenge,
  memo: handleMemo,
  compute_experiment: handleComputeExperiment,
  web_research: handleWebResearch,
  design: handleDesign,
  repo_bootstrap: handleRepoBootstrap,
};
```

To add a new task type:
1. Create handler function in appropriate file (standard.ts, specialized.ts, etc.)
2. Export from that file
3. Import and register in index.ts
4. Add to `AgentTaskType` enum in `agent/src/taskCapabilities.ts`

---

## Handler Testing

Each handler has a corresponding test file:
- `standard.test.ts` — Tests for create, refactor, docs, deps, qa
- `review-fix.test.ts` — Tests for review, fix
- `research-plan.test.ts` — Tests for research, plan
- `specialized.test.ts` — Tests for security_audit, migrate, etc.
- `memo.test.ts` — Tests for memo
- `verify-tests.test.ts` — Tests for verify_tests
- `compute-experiment.test.ts` — Tests for compute_experiment
- `web-research.test.ts` — Tests for web_research
- `design.test.ts` — Tests for design
- `audit-challenge.test.ts` — Tests for release_audit_challenge

Tests mock:
- Git operations
- Claude Code subprocess
- GitHub API calls
- File system

Run tests: `cd agent && npm run test`

---

## Common Patterns

### Repo Cloning

Most handlers start with:
```typescript
await cloneRepo(taskId, owner, repo, targetBranch, ghToken, ws);
```

This is tracked as part of `setup_secs` for warm-VM analysis.

### Claude Code Invocation

```typescript
const result = await runClaudeCode(
  taskId, ws, fullPrompt, codeMode, model, effort, statusRef
);
```

Claude output is streamed to the WebSocket and captured for logs.

### Fix Loop

For code-change handlers, a fix loop retries on lint/test failures:
1. Run linter, collect errors
2. If errors, invoke Claude with error output
3. Max `MAX_LINT_FIX_ATTEMPTS` (default 5)
4. Similar for test failures: `MAX_TEST_FIX_ATTEMPTS`

### PR Creation

Standard handlers that modify code end with:
```typescript
const prResult = await createPullRequest(ghToken, owner, repo, {
  title: "...",
  body: "...",
  head: targetBranch,
  base: baseBranch,
});
```

---

## Effort Levels

Handlers accept an `effort` parameter (low, medium, high) that scales:
- Prompt verbosity
- Timeout budget
- Retry budget
- Expected output depth

Map:
```
low:    20-30 min timeout, minimal detailed output
medium: 45 min timeout, balanced
high:   60+ min timeout, comprehensive analysis
```

---

## Summary Table

| Task Type | Handler | Repo Access | Creates PR | Output Type |
|-----------|---------|------------|------------|------------|
| create | standard.ts | Clone+edit | Yes | PULL_REQUEST |
| refactor | standard.ts | Clone+edit | Yes | PULL_REQUEST |
| fix | review-fix.ts | Clone+edit | Yes | PULL_REQUEST |
| review | review-fix.ts | Clone (RO) | No | RESEARCH_FINDINGS |
| plan | research-plan.ts | Clone (RO) | No | RESEARCH_FINDINGS |
| research | research-plan.ts | Clone (RO) | No | RESEARCH_FINDINGS |
| docs | standard.ts | Clone+edit | No | RESEARCH_FINDINGS |
| security_audit | specialized.ts | Clone (RO) | No | SECURITY_REPORT |
| perf | specialized.ts | Clone (RO) | No | PERF_REPORT |
| debug | specialized.ts | Clone (RO) | No | RESEARCH_FINDINGS |
| memo | memo.ts | None | No | MEMO |
| repo_bootstrap | specialized.ts | Clone (RO) | No | bootstrap_overview (JSON) |

RO = Read-only (no edits)
