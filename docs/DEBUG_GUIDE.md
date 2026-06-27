# Debugging Failed Tasks

This guide helps you troubleshoot why tasks fail and how to diagnose issues.

---

## Finding Failed Tasks

### In Admin UI

The top nav has four tabs: **Home**, **Board**, **Directions**, **Compute**.

1. On the **Home** tab, the task-status section surfaces recent failures and
   failure groups; the **Board** tab shows a `Failed` column you can scan.
2. Click any task to open its detail drawer (see "Using the Admin UI to
   Debug" below).
3. Filter the Board by status (`FAILED`), task type, or failure category.

### Via API

```bash
# List all failed tasks in a project
curl -H "Authorization: Bearer $FIREBASE_TOKEN" \
  "http://localhost:8080/api/projects/{project_id}/tasks?status=FAILED"
```

---

## Understanding Failure Categories

Tasks fail for different reasons. Blueprint categorizes them:

| Category | Meaning | Recoverable? |
|----------|---------|--------------|
| `clone_failed` | Git clone failed (auth, network, repo doesn't exist) | Yes, with correct token |
| `build_failed` | Compilation/build step failed | Yes, fix code |
| `test_failed` | Tests failed | Yes, fix code |
| `lint_failed` | Linting errors | Yes, fix code |
| `pr_creation_failed` | Failed to create/push PR | Yes, check GitHub token |
| `claude_code_failed` | Claude Code subprocess crashed | No, log a bug |
| `timeout` | Agent heartbeat timed out (5 min) | Maybe, try again |
| `no_progress_timeout` | No output for 30+ min | Maybe, try again |
| `rate_limited` | Hit API rate limits | Yes, retry later |
| `fatal` | Unrecoverable error | No |

**View category:** Open the task drawer → **Details** tab (shows the failure
category and status timeline); the raw error output is on the **Logs** tab.

---

## Viewing Task Logs

### Real-time Logs (while task is running)

1. Click the task to open its drawer
2. Switch to the **Logs** tab
3. Logs stream live via WebSocket
4. Scroll up to see earlier lines

### Completed Task Logs

1. Click on failed task
2. Switch to **Logs** tab
3. Full log history visible (stored in Cloud Storage)
4. Search within logs using `Ctrl+F`

### Direct Log URL

```bash
# Task logs are stored as GCS artifacts
# From task details, copy the "error_log" artifact URL
# Open in browser to download full log file
```

---

## Diagnosing Common Failures

### 1. "clone_failed" — Repository Inaccessible

**Symptoms:**
```
fatal: could not read Username for 'https://github.com':
No such device or address
```

**Causes:**
- Agent doesn't have GitHub token
- GitHub token lacks `repo` scope
- Repository is private and token is invalid
- Network issue

**Fix:**
1. Check GitHub token in admin UI (Settings > GitHub)
2. Verify token has `repo:read` and `repo:write` scopes
3. Try reconnecting OAuth flow
4. Check if repository is accessible from the internet

**Code path:** `agent/src/handlers/standard.ts` → `runGitClone()`

---

### 2. "build_failed" — Compilation Error

**Symptoms:**
```
error: undefined variable 'foo'
error: missing dependency 'bar'
```

**Diagnosis:**
1. Review the error message in logs
2. Check if it's a code issue or dependency issue
3. Verify the code change that caused the failure

**Fix:**
- Usually requires a new `fix` task to address the build error
- Can be automated: orchestrator spawns fix tasks on CI failure

**Code path:** `backend/app/failure_classification.py` → `classify_build_failure()`

---

### 3. "test_failed" — Unit/Integration Test Failure

**Symptoms:**
```
AssertionError: expected 'foo' but got 'bar'
Tests run: 100, Passed: 95, Failed: 5
```

**Diagnosis:**
1. Review test output in logs
2. Identify which tests failed
3. Check if code change broke something

**Fix:**
- Update code to fix the failing test
- Or update test if expected behavior changed
- Often spawns automatic `fix` task

**Code path:** `backend/app/ci_resolution.py`

---

### 4. "lint_failed" — Code Style Violation

**Symptoms:**
```
src/foo.ts:42:1 error: Unexpected var, use const
src/bar.py:10:1 error: Line too long (>100 chars)
```

**Diagnosis:**
1. Review linting errors in logs
2. Identify files and line numbers
3. Check lint rules for the project

**Fix:**
- Claude Code agent has a built-in fix loop
- If Claude didn't fix it, the fix loop hit the max iterations limit
- Manual fix may be needed for complex cases

**Code path:** `agent/src/handlers/standard.ts` → `runLintFix()`

---

### 5. "timeout" — Heartbeat Timeout

**Symptoms:**
```
Task IN_PROGRESS for >5 minutes with no heartbeat
```

**Causes:**
- Agent process hung (e.g., waiting on network)
- VM resource exhaustion (CPU/memory)
- VM preemption (if on spot)
- Network partition

**Fix:**
- Check VM metrics (GCP Console → Compute Engine)
- Increase `MAX_EXECUTION_TIME` if task is legitimately slow
- Check if VM was preempted (spot instance)
- Retry the task

**Code path:** `backend/app/reaper.py`

---

### 6. "no_progress_timeout" — Silent Agent

**Symptoms:**
```
No output from Claude Code for >30 minutes
Task still marked IN_PROGRESS
```

**Causes:**
- Claude API hung
- Network timeout on Claude API call
- Large codebase analysis taking too long
- Bug in agent code

**Fix:**
- Retry with smaller scope or different parameters
- Check Claude API status (Anthropic dashboard)
- Increase timeout if analysis is expected to be slow

**Code path:** `agent/src/index.ts` → `runClaudeCode()` timeout

---

### 7. "pr_creation_failed" — GitHub PR Creation Error

**Symptoms:**
```
403 Forbidden: Insufficient permission
409 Conflict: A pull request already exists
```

**Causes:**
- GitHub token doesn't have `pull_requests:write` scope
- Base branch doesn't exist
- PR already exists
- Force push conflict

**Fix:**
1. Check GitHub token scopes
2. Verify base branch exists
3. Check if PR already open (different task)
4. Retry

**Code path:** `agent/src/handlers/standard.ts` → `createPR()`

---

### 8. "rate_limited" — Hit API Rate Limit

**Symptoms:**
```
429 Too Many Requests: GitHub API limit exceeded
```

**Causes:**
- Too many concurrent tasks calling GitHub API
- Burning through GitHub API quota
- Anthropic API rate limit hit

**Fix:**
- Wait and retry (usually resolves in minutes)
- Reduce concurrency (`MAX_CONCURRENT_VMS`)
- Upgrade GitHub App to higher tier

**Code path:** `agent/src/providers/runners.ts` → Rate limit detection

---

## Using the Admin UI to Debug

### Task Detail Drawer

Click a task to open its detail drawer. It has three tabs:

1. **Details** — task metadata, status timeline, failure category, self-review
   outcome, and fix-loop / execution telemetry
2. **Logs** — full streaming output from the agent (live via WebSocket while
   running, full history once terminal)
3. **Changes** — the PR diff: changed files plus PR/commit links

For a FAILED task the drawer also shows action buttons (**Retry**, **Edit**,
**Dismiss**) — see "Retry & Recovery" below for what each does.

### Error Log

The agent's error output is on the **Logs** tab. A typical failure tail
looks like:

```
[2024-01-15 10:05:00] Task claimed by agent-vm-xyz
[2024-01-15 10:05:05] Cloning repo...
[2024-01-15 10:05:15] Clone successful
[2024-01-15 10:05:20] Running Claude Code...
...
[2024-01-15 10:15:45] ERROR: Test suite failed (5 failures)
[2024-01-15 10:15:46] Spawning fix task...
```

---

## Viewing Metrics & Telemetry

### Agent Telemetry

Each task carries execution telemetry:

```json
{
  "context": {
    "_timing": {
      "clone_duration_ms": 2000,
      "claude_duration_ms": 180000,
      "fix_loop_iterations": 3,
      "total_duration_ms": 300000
    },
    "llm_metrics": {
      "input_tokens": 45000,
      "output_tokens": 15000,
      "api_calls": 5
    }
  }
}
```

**View in admin UI:**
1. Click the task to open its drawer
2. Open the **Details** tab
3. Scroll to the telemetry / self-review section

---

## Debugging Specific Task Types

### Fix Tasks (review-fix loop)

A fix task addresses issues found in a review. If it fails:

1. Check the review findings (parent task's artifacts)
2. Verify agent understood the issue
3. Check if fix attempts were made (fix loop telemetry)
4. If max iterations hit, issue is too complex

**Code:** `agent/src/handlers/review-fix.ts`

---

### Research Tasks

A research task analyzes code and produces findings. If it fails:

1. Check logs for analysis errors
2. Verify target files were found
3. Check Claude output quality (was it hallucinatory?)
4. Increase scope or depth if analysis was shallow

**Code:** `agent/src/handlers/research-plan.ts`

---

### Security Audit Tasks

A security audit searches for vulnerabilities. If it fails:

1. Check if audit completed (may have found 0 issues)
2. Review findings for false positives
3. Check if code is actually insecure or if scanner is misconfigured

**Code:** `agent/src/handlers/specialized.ts` → `handleSecurityAudit`

---

## Accessing GCP VM Logs

If you need to see what happened on the VM itself:

1. Go to GCP Console → Compute Engine → VM Instances
2. Find the VM name (listed in task details)
3. Click VM → **Serial port 1 (console)**
4. Scroll to see startup logs, agent execution, errors

**SSH access:** If you need to examine the VM filesystem:

```bash
gcloud compute ssh <vm-name> --zone <zone>
```

---

## Circuit Breaker Trips

If a project keeps failing, the circuit breaker may halt new tasks:

**Symptoms:**
```
New tasks created but remain QUEUED indefinitely
Admin UI shows "Circuit breaker open"
```

**Fix:**
1. Identify and fix the root cause
2. Click the **Autonomy** indicator in the top bar to expand the status popover
3. When the breaker is `OPEN` a **Reset Circuit Breaker** button appears in the
   popover — click it
4. New tasks will start provisioning

**Code:** `backend/app/task_completion.py` → `_check_circuit_breaker()`

---

## Retry & Recovery

### Automatic Retries

Most failures trigger automatic retries:

```
Attempt 1: FAILED (clone_failed)
→ Attempt 2: FAILED (clone_failed)
→ Attempt 3: FAILED (clone_failed)
→ Task terminal FAILED (max retries exhausted)
```

**Retry budget:** Default 3 attempts. Configured in:
- `backend/app/models.py` → `Task.max_attempts`

---

### Manual Retry, Dismiss, Cancel & Archive

Operator actions in the task drawer mutate the **existing** task — they do not
spawn a fresh task row (the only exception is the automatic CI-driven `fix`
tasks above):

| Action | Where | Effect |
|--------|-------|--------|
| **Retry** | FAILED task drawer | Re-queues the **same** task (`status → QUEUED`). It re-provisions and runs again under the same task id — no new task is created. |
| **Dismiss** | FAILED task drawer | Cancels the task (`status → CANCELLED`) to clear it from attention without re-running it. |
| **Cancel Task** | active (IN_PROGRESS/QUEUED) task drawer | Stops the running task (`status → CANCELLED`). |
| **Edit** | FAILED task drawer | Updates the task prompt in place before you Retry it. |
| **Archive** | task card / drawer | Moves a terminal task out of the active board into the archive view; does not change its status. |

**Blast radius:**
- **Retry** touches only the single task you act on. It re-uses the same id, so its history and prior attempts stay attached — check the **Logs** tab after it re-runs to confirm the new attempt actually made progress.
- **Dismiss** (on a FAILED task) cancels only that task.
- **Cancel Task** (on an active task) cascades `CANCELLED` to all active children (status `QUEUED`, `PROVISIONING`, `IN_PROGRESS`, or `EVALUATING`) and closes any orphan PRs they hold. Tasks that were `QUEUED`/`BLOCKED` waiting on a cancelled child become stranded dependents — the UI warns you of this count before you confirm. Use Cancel with care on parent tasks with running subtrees.

---

## Logging & Monitoring

### Backend Logs

View orchestrator + worker logs:

```bash
# In Docker
docker compose logs app

# In Cloud Run (production)
gcloud logging read \
  "resource.type=cloud_run_revision AND resource.labels.service_name=blueprint-api" \
  --limit 100
```

---

### Application Logs

Blueprint logs important events:

```python
logger.error(f"Task {task_id} failed with category: {category}")
logger.info(f"[AGENT_AUTH_REJECT] reason={reason} task_id={task_id}")
logger.warning(f"Circuit breaker open for project {project_id}")
```

**Search for:**
- `ERROR` — Failures
- `[AGENT_AUTH_REJECT]` — Auth problems
- `Circuit breaker` — Halted projects
- `rate_limit` — Rate limiting

---

## Performance Profiling

If a task is slow:

1. Check execution timeline in task details
2. Compare against baseline for same task type
3. Identify which phase is slow:
   - Clone (network)
   - Claude Code (LLM latency)
   - Tests (code complexity)
   - PR creation (GitHub API)

**Code:** `backend/app/digest_metrics.py` → Latency percentiles

---

## Common Patterns & Solutions

| Issue | Root Cause | Solution |
|-------|-----------|----------|
| Clone fails 3x in a row | Invalid GitHub token | Reconnect OAuth in Settings |
| Tests fail, no fix loop | Agent didn't understand error | Check fix loop max iterations |
| Timeout while testing | Tests are slow | Increase `MAX_EXECUTION_TIME` or scope down tests |
| Rate limited | Too many concurrent API calls | Reduce `MAX_CONCURRENT_VMS` |
| Hung on lint fix | Linter tool not available | Add tool to VM image (Packer) |
| PR creation fails | Branch conflict | Manual merge or rebase |

---

## Escalation & Support

If you can't diagnose the issue:

1. Check **Logs** tab for exact error message
2. Search error in [FAQ](#frequently-asked-questions) below
3. Check [TROUBLESHOOTING section in README](../README.md#troubleshooting)
4. Open an issue on GitHub with:
   - Task ID
   - Full error log
   - Steps to reproduce
   - Expected vs. actual behavior

---

## Frequently Asked Questions

**Q: Task is stuck in PROVISIONING for >2 minutes, what do I do?**

A: The orchestrator is still creating the VM. This is normal for the first 1-2 min. If >5 min, check:
- GCP quotas (`gcloud compute quotas`)
- Cloud Run logs for provisioning errors
- Try cancelling and retrying

---

**Q: How do I see what Claude Code did?**

A: View the **Logs** tab in task details. The logs include:
- Claude prompt
- Claude output (streaming)
- Lint/test results
- Final status

---

**Q: Can I manually edit a task and retry it?**

A: Yes. In the FAILED task drawer:
1. Click **Edit** to update the prompt in place
2. Click **Retry** to re-queue the same task (`status → QUEUED`) — it runs
   again under the same task id rather than creating a new one
3. Prefer launching a brand-new task only when you want a clean payload and a
   separate history

---

**Q: Why did the fix loop stop at iteration 3?**

A: Fix loop has a max iteration limit (default 5) to prevent infinite loops. If reached:
- Issue may be too complex
- Claude misunderstood the problem
- Try manual review + targeted fix task

---

**Q: How do I know if my GitHub token is valid?**

A: In admin UI:
1. Settings → GitHub
2. If connected, you'll see your username
3. If disconnected or expired, click **Reconnect**

---

## See Also

- [TASK_LIFECYCLE.md](TASK_LIFECYCLE.md) — Understanding task states
- [BACKEND_MODULES.md](BACKEND_MODULES.md) — Orchestrator, reaper, task lifecycle
- [README.md](../README.md#troubleshooting) — Quick troubleshooting guide
