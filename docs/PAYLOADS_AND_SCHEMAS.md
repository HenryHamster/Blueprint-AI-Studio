# Task Payloads & Schemas

This guide documents the payload structure for each Blueprint task type.

---

## Overview

Each task has a **payload** — a JSON object containing task-specific parameters. The payload is validated at task creation time and passed to the agent handler.

**Example:**

```json
{
  "task_type": "create",
  "payload": {
    "prompt": "Add user authentication to the app",
    "base_branch": "main",
    "target_branch": "feature/auth"
  }
}
```

Payloads are validated via Pydantic schemas in `backend/app/payload_schemas.py`.

---

## Code-Change Task Types

These tasks modify code and typically create PRs.

### `create`

Implement a new feature from a natural-language prompt.

**Payload:**

```json
{
  "prompt": "string",              // Feature description (required)
  "base_branch": "string",         // Target branch for PR (required, default: "main")
  "target_branch": "string",       // Feature branch name (auto-generated if omitted)
  "effort": "low|medium|high",     // Effort level (default: "medium")
  "model": "string"                // LLM model override (default: configured model)
}
```

**Example:**

```json
{
  "prompt": "Implement OAuth2 login flow with Google",
  "base_branch": "main",
  "effort": "high"
}
```

**Agent behavior:**
1. Clone repo to feature branch
2. Run Claude Code with prompt
3. Fix lint/test failures in loop
4. Create PR when done

**Output artifacts:**
- `PULL_REQUEST` — PR URL
- `GIT_COMMIT` — Commit hash(es)

---

### `refactor`

Improve code quality without changing behavior.

**Payload:**

```json
{
  "description": "string",              // Refactoring goal (required)
  "scope": "string | string[]",         // Files/dirs to refactor (required)
  "focus_areas": "string[]",            // What to focus on (optional)
  "effort": "low|medium|high"           // Effort level (default: "medium")
}
```

**Valid focus areas:**
- `readability`
- `performance`
- `maintainability`
- `type_safety`
- `testing`

**Example:**

```json
{
  "description": "Extract auth logic into separate service",
  "scope": ["src/auth/", "src/middleware/"],
  "focus_areas": ["maintainability", "type_safety"]
}
```

---

### `fix`

Address issues identified in a code review.

**Payload:**

```json
{
  "pr_number": number,                   // PR to fix (required)
  "issues": [                            // Issues to address
    {
      "severity": "CRITICAL|MAJOR|MINOR",
      "message": "string",
      "file": "string",
      "line": number
    }
  ],
  "previous_attempts": [                 // Previous fix attempts (optional)
    {
      "issue_id": "string",
      "approach": "string",
      "result": "success|failure",
      "error_message": "string"
    }
  ]
}
```

**Notes:**
- Agent has 5-iteration limit to prevent loops
- Each iteration attempts all issues, reports progress
- Issues can be marked "deferred" if unsolvable

**Example:**

```json
{
  "pr_number": 123,
  "issues": [
    {
      "severity": "CRITICAL",
      "message": "Missing input validation on login",
      "file": "src/auth.ts",
      "line": 42
    }
  ]
}
```

---

### `qa`

Write or run quality assurance tests.

**Payload:**

```json
{
  "test_type": "unit|integration|e2e",   // Test type (required)
  "scope": "string | string[]",          // Files to test (required)
  "focus_areas": "string[]",             // Test focus (optional)
  "existing_tests": boolean              // Run existing tests? (default: true)
}
```

**Example:**

```json
{
  "test_type": "integration",
  "scope": "src/api/",
  "focus_areas": ["error_handling", "edge_cases"]
}
```

---

### `docs`

Write or update documentation.

**Payload:**

```json
{
  "documentation_task": "string",   // What docs to write (required)
  "scope": "string | string[]",     // Files to document (optional)
  "format": "markdown|docstring|api_doc",  // Format (default: "markdown")
  "effort": "low|medium|high"       // Effort level (default: "medium")
}
```

**Example:**

```json
{
  "documentation_task": "Write API documentation for POST /api/users",
  "scope": ["src/api/users.ts"],
  "format": "api_doc"
}
```

---

### `deps`

Update dependencies and manage versions.

**Payload:**

```json
{
  "target": "python|typescript|all",     // Dependency target (required)
  "strategy": "latest|compatible|patch", // Update strategy (default: "compatible")
  "constraints": {                       // Version constraints (optional)
    "package_name": ">=1.0.0"
  }
}
```

**Example:**

```json
{
  "target": "typescript",
  "strategy": "compatible",
  "constraints": {
    "react": "^18.0.0",
    "typescript": "^5.0.0"
  }
}
```

---

## Review Task Types

### `review`

Review a PR and leave detailed comments.

**Payload:**

```json
{
  "pr_number": number,                   // PR to review (required)
  "focus_areas": "string[]",             // What to focus on (optional)
  "include_tests": boolean,              // Review test changes? (default: true),
  "effort": "low|medium|high"            // Review depth (default: "medium")
}
```

**Valid focus areas:**
- `correctness` — Logic bugs, edge cases
- `security` — Injection, XSS, auth issues
- `performance` — N+1 queries, inefficiency
- `maintainability` — Code clarity, duplication
- `test_coverage` — Missing tests

**Example:**

```json
{
  "pr_number": 42,
  "focus_areas": ["security", "test_coverage"],
  "effort": "high"
}
```

**Output artifacts:**
- `RESEARCH_FINDINGS` — Structured review comments

---

## Research & Planning Task Types

### `research`

Investigate a codebase area and produce findings.

**Payload:**

```json
{
  "research_topic": "string",            // Topic to investigate (required)
  "scope": "string | string[]",          // Files to analyze (optional)
  "focus_areas": "string[]",             // Areas of interest (optional)
  "depth": "shallow|medium|deep",        // Analysis depth (default: "medium")
  "research_mode": "autonomous|manual|strategic"  // Mode (default: "manual")
}
```

**Valid focus areas:**
- `performance`
- `security`
- `maintainability`
- `test_coverage`
- `scalability`

**Example:**

```json
{
  "research_topic": "Identify performance bottlenecks in API",
  "scope": ["src/api/", "src/database/"],
  "depth": "deep",
  "focus_areas": ["performance", "scalability"]
}
```

**Output artifacts:**
- `RESEARCH_FINDINGS` — Structured findings (problems, opportunities, recommendations)

**Note:** When `research_mode="strategic"`, the task feeds into autonomous research cycle and generates new directions.

---

### `plan`

Generate an implementation plan for future work.

**Payload:**

```json
{
  "objective": "string",                 // Goal (required)
  "scope": "string | string[]",          // Scope (optional)
  "constraints": "string[]",             // Constraints (optional)
  "effort_estimate": "t-shirt|story_points"  // Effort unit (default: "t-shirt")
}
```

**Valid constraints:**
- `timeline:N_weeks`
- `team:N_people`
- `budget:$amount`
- `no_breaking_changes`

**Example:**

```json
{
  "objective": "Migrate from Redux to TanStack Query",
  "scope": ["src/state/", "src/components/"],
  "constraints": ["timeline:4_weeks", "no_breaking_changes"]
}
```

**Output artifacts:**
- `RESEARCH_FINDINGS` — Implementation plan with phases, dependencies, risks

---

## Specialized Task Types

### `security_audit`

Audit code for security vulnerabilities.

**Payload:**

```json
{
  "scope": "string | string[]",          // Files to audit (optional)
  "standards": "string[]",               // Standards to check (optional)
  "severity": "all|high|critical"        // Report severity filter (default: "all")
}
```

**Valid standards:**
- `owasp_top_10`
- `cwe` (Common Weakness Enumeration)
- `crypto`
- `injection`
- `authentication`
- `authorization`

**Example:**

```json
{
  "scope": ["src/api/", "src/auth/"],
  "standards": ["owasp_top_10", "crypto"],
  "severity": "high"
}
```

**Output artifacts:**
- `SECURITY_REPORT` — Vulnerabilities with severity, CWE, remediation

---

### `perf`

Profile and optimize performance.

**Payload:**

```json
{
  "area": "string",                      // Area to profile (required)
  "metrics": "string[]",                 // Metrics to track (optional)
  "target": "baseline|improvement"       // Goal (default: "improvement")
}
```

**Valid metrics:**
- `latency_p50`, `latency_p95`, `latency_p99`
- `throughput`
- `memory_usage`
- `cpu_usage`
- `cache_hit_rate`

**Example:**

```json
{
  "area": "Database query performance",
  "metrics": ["latency_p99", "memory_usage"],
  "target": "improvement"
}
```

**Output artifacts:**
- `PERF_REPORT` — Profile, bottlenecks, optimization recommendations

---

### `debug`

Investigate and diagnose a bug.

**Payload:**

```json
{
  "bug_description": "string",           // Bug description (required)
  "reproduction_steps": "string[]",      // How to reproduce (optional)
  "affected_code": "string[]",           // Likely files (optional)
  "suspected_cause": "string"            // Initial hypothesis (optional)
}
```

**Example:**

```json
{
  "bug_description": "Login fails on mobile browsers",
  "reproduction_steps": [
    "Open on mobile Safari",
    "Enter credentials",
    "Click login button"
  ],
  "affected_code": ["src/auth/", "src/mobile/"],
  "suspected_cause": "CORS issue or localStorage not available"
}
```

**Output artifacts:**
- `DEBUG_RESULT` — Root cause analysis, reproduction case, fix recommendation

---

### `migrate`

Generate or execute database migrations.

**Payload:**

```json
{
  "migration_type": "schema|data|both",  // Migration type (required)
  "description": "string",               // What's being migrated (required)
  "target_version": "string",            // Target version (optional)
  "rollback_plan": boolean               // Include rollback? (default: true)
}
```

**Example:**

```json
{
  "migration_type": "schema",
  "description": "Add users.phone_number column",
  "rollback_plan": true
}
```

**Output artifacts:**
- `GIT_COMMIT` — Migration file (Alembic, SQL, etc.)

---

### `smoketest`

Run smoke tests against a deployed environment.

**Payload:**

```json
{
  "environment": "staging|production",   // Target env (required)
  "test_suite": "string",                // Which tests to run (optional)
  "alert_on_failure": boolean            // Send alert if fail? (default: true)
}
```

**Example:**

```json
{
  "environment": "staging",
  "test_suite": "critical_flows",
  "alert_on_failure": true
}
```

**Output artifacts:**
- `SMOKETEST_RESULT` — Test results, pass/fail, metrics

---

### `self_audit`

Self-review checklist for autonomous agents.

**Payload:**

```json
{
  "checklist_type": "code_quality|security|coverage",  // Type (required)
  "scope": "string | string[]"           // Files to audit (optional)
}
```

**Example:**

```json
{
  "checklist_type": "code_quality",
  "scope": ["src/"]
}
```

---

## Pipeline & Verification Task Types

### `verify_tests`

Clone a PR head, run the detected test suite, and post a pass/fail verdict. Used as a merge-gate step in the CI pipeline.

**Payload:**

```json
{
  "branch": "string",                    // PR head branch to test (optional, system-populated)
  "pr_number": number                    // PR being gated (optional, for artifact correlation)
}
```

**Output artifacts:**
- `VERIFY_TESTS_RESULT` — Structured verdict with `detected`, `passed`, test list, and output

---

### `compute_experiment`

Launch a budgeted compute job on a separate VM and monitor it to a terminal state. The agent parks itself after launch — the backend wakes it when the job completes, freeing the agent VM slot during the run.

**Payload:**

```json
{
  "command": "string",                   // Shell command to run on the compute VM
  "spend_cap_usd": number,               // Budget ceiling in USD (required by agent)
  "deadline_seconds": number,            // Wall-clock timeout (required by agent)
  "machine_type": "string",              // GCP machine type (optional)
  "accelerator": "string",               // GPU short name e.g. "T4", "L4" (optional)
  "cwd": "string",                       // Working directory on compute VM (optional)
  "env": { "KEY": "VALUE" },             // Extra env vars (optional)
  "experiment": "string",                // Experiment label echoed to result artifact
  "hypothesis": "string",                // Hypothesis text echoed to result artifact
  "metric": "string",                    // Primary metric name
  "success_criterion": {                 // Optional acceptance gate
    "metric": "string",
    "operator": ">=|<=|>|<|==|!=",
    "threshold": number
  },
  "require_durable_artifact": boolean    // Fail unless result links a gs:// artifact
}
```

**Output artifacts:**
- `COMPUTE_RESULT` — Terminal job outcome, final cost, and optional result reference

---

## System Task Types

### `release_audit_challenge`

Independently re-examine release-audit findings and emit structured per-finding verdicts. Spawned as a binding second-opinion pass alongside `release_audit`.

**Payload:**

```json
{
  "candidate_sha": "string",             // Commit SHA being audited (system-populated)
  "diff_range": "string",                // Git diff range (system-populated)
  "primary_audit_report": {}             // Findings from the primary audit pass (optional)
}
```

**Output artifacts:**
- `RELEASE_AUDIT_CHALLENGE` — Per-finding `upheld`/`overturned`/`inconclusive` verdicts

---

### `web_research`

Web-first research that produces a cited knowledge document. The task runs with web access and emits a structured document graded by a forward-quality gate before being persisted as a `KnowledgeDocument`.

**Payload:**

```json
{
  "prompt": "string",                    // Research question (required)
  "topic": "string",                     // Topic label for categorization (optional)
  "tags": ["string"],                    // Tags for the knowledge document (optional)
  "direction_id": "uuid",                // Originating direction (optional)
  "project_name": "string"               // Project context label (optional)
}
```

**Output artifacts:**
- `KNOWLEDGE_DOCUMENT` — Cited research document (title, summary, body, citations, tags)

---

### `design`

Codebase-grounded design work that produces a durable design document. The task runs read-only over a repo clone and emits a structured knowledge document (same shape as `web_research`) graded by a forward-quality gate.

**Payload:**

```json
{
  "prompt": "string",                    // Design question (required)
  "topic": "string",                     // Topic label (optional)
  "tags": ["string"],                    // Tags (optional)
  "direction_id": "uuid",                // Originating direction (optional)
  "project_name": "string"               // Project context label (optional)
}
```

**Output artifacts:**
- `KNOWLEDGE_DOCUMENT` — Design document (title, summary, body, citations, tags)

---

### `memo`

Produce a structured reasoning artifact without opening a PR.

**Payload:**

```json
{
  "prompt": "string",                    // What to analyze/reason about (required)
  "scope": "string | string[]",          // Files to consider (optional)
  "format": "markdown|json|structured"   // Output format (default: "markdown")
}
```

**Example:**

```json
{
  "prompt": "Analyze API design for extensibility",
  "scope": ["src/api/"],
  "format": "json"
}
```

**Output artifacts:**
- `MEMO` — Reasoning/analysis output (no PR created)

---

## Payload Validation

Payloads are validated at task creation:

```python
# backend/app/payload_schemas.py
def validate_task_payload(task_type: str, payload: dict) -> bool:
    """Validate payload against task type schema."""
```

**Validation errors:**
- Returns `422 Unprocessable Entity` if validation fails
- Error details included in response
- Task is NOT created if validation fails

**Common validation errors:**

| Error | Cause | Fix |
|-------|-------|-----|
| `Missing required field: "prompt"` | `create` task missing prompt | Add `"prompt"` to payload |
| `Invalid enum value: "foo"` | Unknown task type or focus area | Check spelling, use valid enum value |
| `"pr_number" must be positive integer` | Invalid PR number format | Use integer >= 1 |
| `"scope" must be string or array` | Wrong scope type | Use `"path.ts"` or `["path1.ts", "path2.ts"]` |

---

## Payload Size Limits

Some payloads have size constraints:

| Limit | Value |
|-------|-------|
| Max payload JSON size | 100 KB |
| Max prompt length | 10,000 characters |
| Max research scope | 100 files |
| Max plan phases | 20 |

**Exceeding limits:** Returns validation error.

---

## Complete Example: Multi-field Payload

```json
{
  "task_type": "create",
  "payload": {
    "prompt": "Implement OAuth2 integration with Google",
    "base_branch": "main",
    "target_branch": "feature/oauth",
    "effort": "high",
    "model": "claude-opus-4.1-20250805"
  },
  "priority": "high",
  "tags": ["security", "auth"]
}
```

---

## Agent Handler Implementation

Each handler receives the payload and task details:

```typescript
// agent/src/handlers/standard.ts
async function handleCreate(
  taskId: string,
  payload: Record<string, unknown>,
  task: Record<string, unknown>,
  ghToken: string,
  owner: string,
  repo: string,
  ws: WebSocket | null,
  statusRef: { value: string },
  model?: string,
  effort?: string
) {
  const { prompt, base_branch, target_branch } = payload as CreatePayload;
  // Implementation...
}
```

---

## Extending Payloads

To add a new field to a task type:

1. **Update schema** — `backend/app/payload_schemas.py`
   ```python
   class CreatePayload(BaseModel):
       prompt: str
       base_branch: str = "main"
       new_field: Optional[str] = None  # Add here
   ```

2. **Update validation** — Ensure constraints are enforced

3. **Update handler** — `agent/src/handlers/standard.ts`
   ```typescript
   const { prompt, base_branch, new_field } = payload;
   // Use new_field...
   ```

4. **Update docs** — Add field to this guide

5. **Test** — Add test for new field validation

---

## See Also

- [API_REFERENCE.md](API_REFERENCE.md) — HTTP endpoints that accept payloads
- [AGENT_HANDLERS.md](AGENT_HANDLERS.md) — Handler implementations
- [backend/app/payload_schemas.py](../backend/app/payload_schemas.py) — Source of truth for validation
