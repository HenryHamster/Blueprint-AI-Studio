# Research: Activate Agent Persistent Memory via the Playbook System

**Task:** Activate agent persistent memory via the playbook system. Infrastructure is fully built.

**Context:** This document is a follow-up to `research-persistent-agent-memory.md` (committed in PR #788). That document described the architecture and proposed three mechanisms. PRs #788, #810, and #811 have since been merged, fully implementing the playbook read/write/injection pipeline. This document assesses the current state and provides a concrete activation plan for what remains.

---

## Executive Summary

The playbook infrastructure is fully operational. Agents can read and write persistent knowledge; the playbook is injected into every claim response. However, three activation gaps remain that prevent the system from delivering its intended value:

1. **No `confirm` operation** — the `confirmed_count` quality signal on playbook entries exists in the database schema but cannot be incremented by agents. The reinforcement loop is broken.
2. **No retry context injection** — agents have no knowledge of prior failed attempts when retrying a task, despite `attempt_count` and `status_log` being available server-side.
3. **No attempt history on requeue** — the orchestrator does not write a prior-attempt summary to `context` before requeuing, so agents retry blind.

These three gaps are independent, require minimal code changes (no DB migrations needed for gaps 1 and 3), and directly address the most expensive failure modes in agent operation.

---

## 1. Current State After PRs #788, #810, #811

### What Is Fully Operational

| Component | Location | Status |
|-----------|----------|--------|
| Playbook `set`/`get`/`delete`/`list` endpoint | `main.py:2871` | ✅ Done |
| Claim-time playbook injection | `routers/agent.py:484–493` | ✅ Done |
| Agent-side `./playbook` bash helper | `prompt.ts:67–99` | ✅ Done |
| `formatPlaybook()` — playbook in prompt | `prompt.ts:137–143` | ✅ Done |
| Post-task reflection triggers in prompt guidance | `prompt.ts:145–166` | ✅ Done |
| Admin CRUD endpoints for playbook | `admin.py:4312–4430` | ✅ Done |
| `confirmed_count` field in storage schema | `main.py:2942` | ✅ Done (stored, never incremented) |

### What Is NOT Built

| Gap | Impact |
|-----|--------|
| No `confirm` op in `/api/agent/playbook` | Quality signal is frozen at 0; high-value entries look identical to untested ones |
| No `retry_context` in claim response | Agents retry blind — most common source of duplicate failure loops |
| No `attempt_history` written on requeue | Orchestrator discards prior-attempt data when requeueing |
| No `AgentMemory` DB table | Longer-term structured memory (from original research doc); still valid but lower priority than the three above |

---

## 2. Gap 1: Playbook Confirmation (`confirm` op)

### Problem

The `/api/agent/playbook` endpoint accepts ops: `get`, `set`, `delete`, `list`. The database schema stores `confirmed_count` on each entry (line 2942), but there is no `confirm` op that increments it. Agents cannot signal "this entry is still true — I used it successfully."

Without confirmation, all entries have `confirmed_count = 0`. The claim-time injection returns entries sorted by key (dict iteration order), not by quality. A stale entry and a battle-tested entry look identical in the prompt.

### Fix: Add `confirm` op to the playbook endpoint

**File:** `backend/app/main.py`

In the playbook endpoint (`update_playbook`, line 2888), add `"confirm"` to the accepted ops:

```python
if op not in ("get", "set", "delete", "list", "confirm"):
    raise HTTPException(status_code=400, detail=f"Unknown op: {op}")
```

Add a `confirm` handler after the `delete` branch (around line 2951):

```python
elif op == "confirm":
    # confirm requires a valid key
    if not payload.key:
        raise HTTPException(status_code=400, detail="key is required for confirm")
    key_full = f"playbook/{payload.key.strip()}"
    # Lock project for atomic increment
    result = await session.execute(
        select(Project).where(Project.id == task.project_id).with_for_update()
    )
    project = result.scalars().first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    doc = dict(project.design_doc or {})
    entry = doc.get(key_full)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Playbook key '{payload.key}' not found")
    entry["confirmed_count"] = entry.get("confirmed_count", 0) + 1
    entry["last_confirmed_at"] = _utcnow().isoformat()
    entry["last_confirmed_by"] = str(task.id)
    doc[key_full] = entry
    meta = doc.get("_meta", {"revision": 0})
    meta["revision"] = meta.get("revision", 0) + 1
    meta["last_updated"] = _utcnow().isoformat()
    meta["updated_by_task"] = str(task.id)
    doc["_meta"] = meta
    project.design_doc = doc
    flag_modified(project, "design_doc")
    session.add(project)
    await session.commit()
    return {
        "status": "ok",
        "op": op,
        "key": payload.key,
        "confirmed_count": entry["confirmed_count"],
        "revision": meta["revision"],
    }
```

**File:** `agent/src/prompt.ts` — add `confirm` to `writePlaybookHelper()` (line 67)

In the heredoc that writes the `./playbook` script, add a `confirm` arm:

```bash
elif [ "$OP" = "confirm" ]; then
  [ -z "$KEY" ] && { echo "Usage: playbook confirm <key>" >&2; exit 1; }
  printf '{"task_id":"%s","op":"confirm","key":"%s"}' "$TASK" "$KEY" | \
    curl $CURL_OPTS -X POST "$API/api/agent/playbook" \
      -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      -d @- 2>&1
```

**File:** `agent/src/prompt.ts` — update `PLAYBOOK_GUIDANCE` to document `confirm`:

```
./playbook confirm <key>   # Signal: "I used this and it still worked"
```

**Claim-time injection improvement:** Sort injected entries by `confirmed_count` descending so the most-validated knowledge appears first in the prompt. In `routers/agent.py:488–492`:

```python
# Sort by confirmed_count descending so best-validated entries appear first
sorted_entries = sorted(
    ((k, v) for k, v in (project_for_playbook.design_doc or {}).items()
     if k.startswith("playbook/") and isinstance(v, dict)),
    key=lambda kv: kv[1].get("confirmed_count", 0),
    reverse=True
)
playbook_entries = {k[len("playbook/"):]: v.get("content", "") for k, v in sorted_entries}
```

**Effort:** ~2 hours. No schema changes. Backend only (endpoint + agent script update).

---

## 3. Gap 2: Retry Context Injection

### Problem

When a task fails and is requeued, `task.attempt_count` is incremented and `task.status_log` contains up to 90 entries of the failure history. At claim time, none of this is surfaced to the agent. The agent starts its retry with zero knowledge of what went wrong.

`status_log` is already stripped from `safe_context` (line 277 removes no keys with `max_plan_` prefix — but `status_log` is a top-level model field, not a context key, so it's never included in `response["context"]`).

### Fix: Inject `retry_context` in the claim response

**File:** `backend/app/routers/agent.py`

After line 283 (building `safe_context`), add retry context injection before the `task_type` check:

```python
# Inject retry context for retried tasks
if task.attempt_count and task.attempt_count > 0 and task.status_log:
    # Extract the last failure message(s) from status_log
    # status_log entries: {"msg": str, "ts": str, "status": str}
    prior_failures = []
    for entry in reversed((task.status_log or [])[-10:]):
        msg = entry.get("msg", "").strip()
        if msg and entry.get("status") in ("FAILED", "AGENT_TIMEOUT", "CODE_ERROR"):
            prior_failures.append(msg)
        if len(prior_failures) >= 3:
            break
    # Fall back to any non-empty message if no typed failures found
    if not prior_failures:
        for entry in reversed((task.status_log or [])[-5:]):
            msg = entry.get("msg", "").strip()
            if msg:
                prior_failures.append(msg)
                break
    if prior_failures:
        response["retry_context"] = {
            "attempt_number": task.attempt_count + 1,
            "prior_failure_messages": prior_failures,
        }
```

**File:** `agent/src/prompt.ts`

Add a `formatRetryContext()` function after `formatPlaybook()`:

```typescript
export function formatRetryContext(
  retryCtx: { attempt_number: number; prior_failure_messages: string[] } | undefined
): string {
  if (!retryCtx || !retryCtx.prior_failure_messages?.length) return '';
  const n = retryCtx.attempt_number;
  const msgs = retryCtx.prior_failure_messages;
  return (
    `## Retry Context (Attempt ${n})\n\n` +
    `This task has failed ${n - 1} time(s). Prior failure messages:\n` +
    msgs.map(m => `- ${m}`).join('\n') + '\n\n' +
    `Read these carefully and avoid repeating the same approach.\n\n`
  );
}
```

Wire it into `buildContextPrefix()` in each handler (`standard.ts`, `review-fix.ts`, `research-plan.ts`, `specialized.ts`, `design-doc.ts`). In each handler where the claim response is received, add:

```typescript
import { formatRetryContext } from '../prompt.js';
// ...
const retrySection = formatRetryContext(claimData.retry_context);
// prepend retrySection to the prompt (before task description, after playbook)
```

**Effort:** ~3 hours. No schema changes. Backend claim endpoint + prompt.ts + all 5 handlers.

**Testing:** Add to `backend/testing/test_agent.py`:

```python
async def test_claim_includes_retry_context_on_retry(session, ...):
    # Create task with attempt_count=1 and status_log entries
    task = Task(attempt_count=1, status_log=[
        {"msg": "TypeScript compilation failed", "status": "FAILED", "ts": "..."}
    ], ...)
    # Claim it and assert response has retry_context
    resp = await client.post("/claim", ...)
    assert "retry_context" in resp.json()
    assert resp.json()["retry_context"]["attempt_number"] == 2
    assert len(resp.json()["retry_context"]["prior_failure_messages"]) > 0
```

---

## 4. Gap 3: Attempt History on Requeue

### Problem

The retry requeue logic (main.py:3620–3655) increments `attempt_count` and clears `started_at`, but does NOT save any summary of what the prior attempt did. The `context` dict has `rate_limit_counts` and `blacklisted_accounts` for infrastructure retries, but nothing about what code the agent wrote or why it failed.

This means gap 2 (retry context injection) only gives recent log messages. A richer history — what files were touched, what command failed — would let agents make materially different decisions on retry.

### Fix: Write attempt summary to `context` on requeue

**File:** `backend/app/main.py`

In the retry requeue block (around line 3621), after `task.attempt_count += 1`:

```python
# Save prior attempt summary into context for next agent
ctx = dict(task.context or {})
attempt_history = list(ctx.get("attempt_history", []))
# Capture the most recent status log entry as the attempt summary
last_log = (task.status_log or [{}])[-1]
attempt_history.append({
    "attempt": task.attempt_count - 1,  # just-completed attempt number
    "failure_class": failure_class,
    "status_message": task.latest_status_message or "",
    "last_log_msg": last_log.get("msg", ""),
    "ts": _utcnow().isoformat(),
})
ctx["attempt_history"] = attempt_history[-5:]  # keep last 5 attempts
task.context = ctx
```

`attempt_history` is then automatically included in the claim response via `safe_context` (line 277) — no further changes needed.

**Effort:** ~30 minutes. Single-file change. No schema changes.

---

## 5. Implementation Order

| Priority | Gap | Effort | Value |
|----------|-----|--------|-------|
| 1 | **Attempt history on requeue** (Gap 3) | 30 min | Immediate; single change; feeds Gap 2 |
| 2 | **Retry context injection** (Gap 2) | 3 hours | Reduces duplicate-failure retry waste |
| 3 | **Playbook `confirm` op** (Gap 1) | 2 hours | Enables quality ranking; closes the reinforcement loop |

Do Gap 3 first — it feeds directly into Gap 2 (more history at claim time = richer retry context). Do Gap 1 last because its value compounds over time as the playbook grows.

---

## 6. Files to Modify

**Gap 3 (attempt history on requeue):**
- `backend/app/main.py` — retry requeue block (~line 3621), write `attempt_history` to `ctx`

**Gap 2 (retry context injection):**
- `backend/app/routers/agent.py` — `claim_task`, add `retry_context` to response
- `agent/src/prompt.ts` — add `formatRetryContext()`
- `agent/src/handlers/standard.ts`, `review-fix.ts`, `research-plan.ts`, `specialized.ts`, `design-doc.ts` — import and call `formatRetryContext()`
- `backend/testing/test_agent.py` — add `test_claim_includes_retry_context_on_retry`

**Gap 1 (playbook confirm op):**
- `backend/app/main.py` — add `"confirm"` to accepted ops, add confirm handler after `delete`
- `backend/app/routers/agent.py` — sort injected playbook entries by `confirmed_count`
- `agent/src/prompt.ts` — add `confirm` arm to `writePlaybookHelper()`, update `PLAYBOOK_GUIDANCE`
- `backend/testing/test_agent.py` or new `backend/testing/test_playbook.py` — add confirm test

---

## 7. What Is NOT Needed Now

**`AgentMemory` DB table** — the original research doc (PR #788) proposed this as a structured alternative to the flat playbook. Given the playbook system is now operational and the `confirmed_count` reinforcement mechanism is being fixed, defer the `AgentMemory` table. The playbook + confirmation loop addresses the same problem (persistent cross-task knowledge) with zero schema changes. Revisit if the playbook's 30 KB cap or flat key-value structure becomes a constraint in practice.

**Post-task reflection automation** — PR #810 added explicit trigger conditions to `PLAYBOOK_GUIDANCE`. Agents are already instructed to write to the playbook before completing. Forcing a mandatory post-task write would require modifying the agent shutdown sequence and adds brittleness for marginal gain. The prompt guidance is sufficient; monitor playbook adoption rate via the admin UI instead.

---

## 8. Verification Checklist

After implementing the three gaps:

- [ ] Create a task, fail it once, requeue — verify `context.attempt_history` is set in the DB
- [ ] Claim the requeued task — verify `retry_context.attempt_number == 2` in the claim response
- [ ] Claim response — verify `retry_context.prior_failure_messages` is non-empty
- [ ] Set a playbook entry, then `./playbook confirm <key>` — verify `confirmed_count` increments
- [ ] Set 3 playbook entries with different `confirmed_count` values — verify claim response returns them sorted by `confirmed_count` descending
- [ ] Run `bash scripts/test-backend.sh` — verify no regressions
