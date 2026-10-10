# Research: Persistent Cross-Task Agent Memory

**Task:** Give agents persistent cross-task memory. Agents currently start cold on every task.

---

## Executive Summary

Agents in Blueprint currently start cold on every task. While a playbook system and per-claim project context exist, they do not solve the deeper problem: agents cannot learn from what failed, adjust strategy based on accumulated project knowledge, or avoid repeating past mistakes across task boundaries.

This document describes the current state, identifies the specific gaps, and proposes a concrete implementation path for three complementary memory mechanisms ordered by value-to-effort ratio.

---

## 1. Current State

### What Agents Know at Startup

When an agent claims a task (`POST /api/agent/claim`), it receives:

| Data | Source | Limitation |
|------|--------|------------|
| Task `payloads` and `context` | `Task` DB row | Scoped to current task only |
| `project_context.recently_completed_tasks` | 15 most recent tasks, computed on-the-fly | Status + PR URL + failure reason — no detail |
| `project_context.lessons` | Up to 8 high-iteration tasks | Only captured for 3+ review-loop tasks; surface feedback only |
| `project_context.active_tasks` + `queued_tasks` | Live query | Coordination only, no historical knowledge |
| `playbook` entries | `Project.design_doc[playbook/*]` | Manually set by past agents; low adoption; 30 KB cap; key-value only |
| Design doc pages | `Project.design_doc[pages/*]` | Architecture docs, not operational memory |

### Code Locations

- **Claim endpoint** — `backend/app/routers/agent.py:238` builds the full claim response
- **Project context assembly** — `agent.py:357–479` (recently_completed_tasks, lessons, open_prs, pipeline_health)
- **Playbook injection** — `agent.py:484–493` (reads `design_doc[playbook/*]`)
- **Agent prompt assembly** — `agent/src/prompt.ts:130–137` (`buildContextPrefix`)
- **Playbook formatting** — `agent/src/prompt.ts:101–107` (`formatPlaybook`)
- **Task model** — `backend/app/models.py:118` (`Task`, `TaskHistory`, `Artifact`)

### What Is Missing

1. **Retry context** — when a task fails and is requeued, the re-spawned agent has no knowledge of what the previous attempt did, what error it hit, or how far it got. `attempt_count` is tracked but never surfaced.
2. **Failure pattern memory** — recurring failure types (e.g., "frontend build always breaks when touching X module") are not captured anywhere accessible to future agents.
3. **Per-task post-mortem** — agents cannot write a structured summary of what they learned during a task for the benefit of future agents tackling related work.
4. **Temporal decay** — playbook entries have no timestamp or confidence weight. Stale entries are as prominent as fresh ones.
5. **Structured knowledge vs. free text** — the playbook is untyped key-value pairs. There is no way to query "lessons about testing" vs. "lessons about deployments."

---

## 2. Gap Analysis: Where Memory Would Help Most

### 2a. Task Retry Loops

**Problem:** A task fails with a CODE_ERROR on attempt 1 (e.g., TypeScript compilation error after an import refactor). On re-queue, attempt 2 starts from a clean checkout with no idea what was tried. It likely makes the same mistake.

**Current code path:**
- `orchestrator.py:204–216` increments `provision_failures` counter and requeues
- `agent.py:238` claim endpoint never includes prior attempt data in the response
- `attempt_count` field in the Task model is never surfaced to agents

**Impact:** High. Short-circuit loops and repeated identical failures are a primary source of wasted compute.

### 2b. Project-Wide Pattern Memory

**Problem:** An agent discovers that a specific test file always needs to be updated when touching a certain module. It saves this to the playbook. But 10 tasks later, a different file pattern needs the same treatment — and no agent has connected these dots.

**Current code path:**
- Playbook is a flat key-value dict (`design_doc[playbook/*]`)
- No aggregation, no tagging, no deduplication

**Impact:** Medium. Each agent reinvents knowledge that prior agents accumulated.

### 2c. Cross-Task Research Synthesis

**Problem:** A research agent runs and produces findings (stored as `RESEARCH_FINDINGS` artifact). A subsequent implementation agent sees only the first 8000 chars of that research in `project_context.recently_completed_tasks`. If the research was long or nuanced, key insights are lost.

**Current code path:**
- `agent.py:367–371` trims research findings to 8000 chars in the claim response
- No structured extraction of actionable conclusions

**Impact:** Medium. Research investments are underutilized.

---

## 3. Proposed Implementation

Three mechanisms, ordered by value-to-effort:

### Mechanism 1: Retry Context Injection (Low Effort, High Value)

**What it does:** When an agent is claiming a retry of a failed task, inject the previous attempt's error and status log directly into the task context.

**Implementation:**

In `backend/app/routers/agent.py`, in the `claim_task` function (around line 238), add:

```python
# Inject retry context if this is a retry attempt
attempt_count = task.attempt_count or 0
if attempt_count > 0 and task.status_log:
    # Find the most recent failure message
    prior_failures = []
    for log_entry in reversed(task.status_log[-10:]):
        msg = log_entry.get("msg", "")
        if msg:
            prior_failures.append(msg)
        if len(prior_failures) >= 3:
            break
    if prior_failures:
        response["retry_context"] = {
            "attempt_number": attempt_count + 1,
            "prior_failure_messages": prior_failures,
        }
```

In `agent/src/prompt.ts`, in `buildContextPrefix` (line 130), add retry context formatting:

```typescript
export function formatRetryContext(retryCtx: Record<string, unknown> | undefined): string {
  if (!retryCtx) return '';
  const attempt = retryCtx.attempt_number as number;
  const failures = retryCtx.prior_failure_messages as string[];
  if (!failures || failures.length === 0) return '';
  return `## Retry Context (Attempt ${attempt})\n\n` +
    `This task has failed ${attempt - 1} time(s) previously. Prior failure messages:\n` +
    failures.map(m => `- ${m}`).join('\n') + '\n\n' +
    `Carefully consider what went wrong before and avoid repeating the same approach.\n\n`;
}
```

**Effort:** ~2 hours. No schema changes needed — `status_log` already exists and is populated.

**Testing:** Add a test in `backend/testing/` that creates a task with `attempt_count > 0` and `status_log` entries, claims it, and asserts `retry_context` is present in the response.

---

### Mechanism 2: Structured Agent Memory Table (Medium Effort, Highest Long-Term Value)

**What it does:** Adds a `AgentMemory` table to store typed, searchable memories at the project level. Agents can write memories during a task; future agents read them at claim time.

**Schema:**

```python
class AgentMemory(SQLModel, table=True):
    __tablename__ = "agentmemory"
    
    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    project_id: uuid.UUID = Field(foreign_key="project.id", index=True)
    
    # Classification
    memory_type: str  # "lesson", "pattern", "warning", "success"
    category: str     # "testing", "build", "deployment", "frontend", "backend", "agent"
    
    # Content
    title: str        # Short searchable summary (max 120 chars)
    content: str      # Detailed description (max 2000 chars)
    
    # Provenance
    source_task_id: Optional[uuid.UUID] = Field(default=None, foreign_key="task.id")
    confirmed_by_task_ids: List[str] = Field(default=[], sa_column=Column(JSONB))
    
    # Quality signals
    confidence: float = Field(default=1.0)  # 0.0–1.0; decreases with age
    confirmation_count: int = Field(default=0)
    contradiction_count: int = Field(default=0)
    
    # Lifecycle
    created_at: datetime = Field(default_factory=_utcnow)
    last_confirmed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None  # None = no expiry
    
    # Indexes
    __table_args__ = (
        Index("ix_agentmemory_project_type", "project_id", "memory_type"),
        Index("ix_agentmemory_project_category", "project_id", "category"),
    )
```

**Backend endpoint:**

```python
# POST /api/agent/memory
# Body: {"task_id": "...", "op": "write|confirm|contradict|list", "memory": {...}}
@router.post("/api/agent/memory")
async def agent_memory(req: AgentMemoryRequest, session: AsyncSession = Depends(get_session)):
    ...
```

**Claim injection:** At claim time, load top-N memories for this project (ordered by `confirmation_count DESC`, `created_at DESC`, limit 20) and inject into the response alongside the playbook:

```python
# In claim_task, after playbook injection
memories = await session.exec(
    select(AgentMemory)
    .where(AgentMemory.project_id == task.project_id)
    .order_by(AgentMemory.confirmation_count.desc(), AgentMemory.created_at.desc())
    .limit(20)
)
response["agent_memory"] = [
    {"type": m.memory_type, "category": m.category, "title": m.title, "content": m.content}
    for m in memories
]
```

**Agent-side helper script:** Similar to `./playbook`, write a `./memory` helper script in `writeMemoryHelper()` (parallel to `writePlaybookHelper` in `prompt.ts:67`):

```bash
./memory write --type lesson --category testing \
  "Backend tests must run via Docker (bash scripts/test-backend.sh), not pytest directly"

./memory confirm <memory-id>   # Reinforce an existing memory
./memory list                  # Show current project memories
```

**Agent prompt injection:** In `prompt.ts`, add `formatAgentMemory()` parallel to `formatPlaybook()`:

```typescript
export function formatAgentMemory(memories: Array<Record<string, string>> | undefined): string {
  if (!memories || memories.length === 0) return '';
  const grouped: Record<string, string[]> = {};
  for (const m of memories) {
    const cat = m.category || 'general';
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(`**${m.title}** — ${m.content}`);
  }
  const sections = Object.entries(grouped)
    .map(([cat, items]) => `### ${cat}\n${items.map(i => `- ${i}`).join('\n')}`)
    .join('\n\n');
  return `## Agent Memory (accumulated project knowledge)\n\n${sections}\n\n`;
}
```

**Effort:** ~1 day. Requires a DB migration (new table, one index), a new API endpoint, agent-side helper script, and prompt injection.

**Key design decisions:**
- Keep memories small (title ≤ 120 chars, content ≤ 2000 chars) to control prompt size
- Cap claim injection at 20 memories; filter to the most-confirmed/relevant
- Confidence decay: a background job (or on-read calculation) reduces confidence of memories not confirmed in 30 days
- Separate from playbook: playbook is for operational commands/shortcuts; memory is for learned patterns and warnings

---

### Mechanism 3: Rich Retry History in Task Context (Low-Medium Effort)

**What it does:** Extends the `retry_context` injection (Mechanism 1) with a structured log of what each prior attempt did — what files it touched, what commands it ran, and why it ended.

**Implementation:**

When an agent completes (success or failure), it writes a `TASK_SUMMARY` entry to task context before posting its final status. The agent does this using a `./summarize` helper or as part of the normal `status` endpoint:

```typescript
// At end of agent run (agent/src/index.ts, near status report)
const summary = {
  files_modified: getModifiedFiles(),  // git diff --name-only
  commands_run: last_10_tool_calls,    // already logged
  outcome: "FAILED" | "COMPLETED",
  failure_reason: errorMessage,
};
// Write to task context via heartbeat or status endpoint
```

On re-queue (in `orchestrator.py`), append this summary to `context.attempt_history[]`:

```python
ctx = task.context or {}
history = ctx.get("attempt_history", [])
history.append({
    "attempt": task.attempt_count,
    "summary": ctx.get("last_attempt_summary", {}),
    "status_message": task.latest_status_message,
    "ts": _utcnow().isoformat(),
})
ctx["attempt_history"] = history[-5:]  # keep last 5 attempts
ctx.pop("last_attempt_summary", None)
task.context = ctx
```

At claim time, `attempt_history` is included in `context` (already passed to agents) so no new endpoint is needed — agents see it automatically via the existing context passthrough.

**Effort:** ~4 hours. No schema changes. Requires agent-side summary writing + orchestrator re-queue logic.

---

## 4. Recommended Implementation Order

| Priority | Mechanism | Effort | Value |
|----------|-----------|--------|-------|
| 1 | **Retry Context Injection** | 2 hours | Immediate reduction in duplicate-failure loops |
| 2 | **Rich Retry History** | 4 hours | Agents understand full prior-attempt trajectory |
| 3 | **Structured Agent Memory Table** | 1 day | Long-term project knowledge accumulation |

Start with 1 and 2 since they require zero schema changes and directly address the most expensive failure mode (wasted retry compute). Mechanism 3 is a larger investment but creates lasting project intelligence.

---

## 5. Files to Modify

**Mechanism 1 (Retry Context Injection):**
- `backend/app/routers/agent.py` — claim endpoint, add retry_context to response
- `agent/src/prompt.ts` — `formatRetryContext()`, inject in `buildContextPrefix()`
- `backend/testing/test_agent.py` — add test for retry_context in claim response

**Mechanism 2 (Agent Memory Table):**
- `backend/app/models.py` — add `AgentMemory` model
- `backend/alembic/` — new migration (auto-generated)
- `backend/app/routers/agent.py` — add `/api/agent/memory` endpoint + claim injection
- `agent/src/prompt.ts` — `writeMemoryHelper()`, `formatAgentMemory()`
- `agent/src/index.ts` — call `writeMemoryHelper()` in agent setup
- `backend/testing/test_agent.py` — add tests for memory write/read

**Mechanism 3 (Rich Retry History):**
- `agent/src/index.ts` — write `last_attempt_summary` before final status
- `backend/app/orchestrator.py` — append to `attempt_history` on re-queue
- `backend/app/routers/agent.py` — pass `attempt_history` through in claim context

---

## 6. Open Questions

1. **Memory garbage collection** — should memories auto-expire? After how long? The simplest approach: expire any memory not confirmed in 60 days.
2. **Size budget** — with 20 memories at 2000 chars each, the prompt injection could grow to ~40KB. Consider a tiered approach: high-confidence memories always included; lower-confidence ones only for matching task types.
3. **Contradiction handling** — if two agents write contradictory memories, surface both with a "conflicting guidance" label rather than silently dropping one.
4. **Playbook migration** — should existing playbook entries be migrated into the new memory table? Recommendation: no, keep them separate. Playbook = how-to commands; Memory = learned patterns.
5. **Research findings persistence** — could long research artifacts be summarized and written to AgentMemory automatically? The research handler could call `./memory write` at the end with key conclusions. This would be high-value but requires agent-level summarization quality.
