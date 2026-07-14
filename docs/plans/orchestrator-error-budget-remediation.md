# Orchestrator Error-Budget Remediation Plan

**Status:** DRAFT for operator review — 2026-07-05
**Author:** Claude (root-cause investigation via 4 parallel audit subagents + operator-directed scoping)
**Target base:** land each work item off a **fresh `automain`** (each WI is its own small PR unless noted)
**Serving revision investigated:** `blueprint-orchestrator-00515-kcp`

---

## 0. TL;DR / framing correction

The acute incident ("autonomy stopped spawning research") is **already fixed and confirmed live** — the global-rate-cap raise (50→500) + PR #12801 (per-project session isolation in the research-spawn sweeps). Research is spawning again. **This plan is about the residual orchestrator *error budget*, which is a separate, broader problem.**

An initial diagnosis blamed a single busy-loop for all the errors. Four independent audits **overturned** that:

- The busy-loop is **real but an amplifier (~10×), not the dominant source.** It inflates monitoring-GitHub-read volume and CI-lock DB churn, but it does not cause the 403 storm or the DB "disconnects."
- The error budget is dominated by **≥4 independent wedges** that survive the busy-loop fix untouched.
- Two of the three fixes originally proposed needed correction; one should be dropped entirely.

### Corrections to the originally-proposed 3 fixes
| Original proposal | Corrected verdict |
|---|---|
| Gate fast-loop on "`_cycle_dispatched > 0` **OR** dispatchable work" | Use **`_cycle_dispatched > 0` only** — the "OR dispatchable" clause re-creates the loop (warm-pinned/held tasks *look* dispatchable). → **WI-4** |
| Bound the warm-pin deadline | **Insufficient** — defeated by new-row churn (each fresh fix row gets a new 240s pin). Real fix = anti-re-pin guard + liveness proxy. → **WI-5** |
| Fix `/next-role` handoff | **Drop.** The 204 is correct behavior; the pin points at a VM the orchestrator already deleted. Not a bug. |

---

## 1. Priority-ranked work items

| # | Wedge | Observed volume | Independent of busy-loop? | Risk | Priority |
|---|---|---|---|---|---|
| WI-1 | Sidecar-recovery infinite retry | ~348 log-lines/hr (11 sources every ~2 min) | Yes | Low | **1** |
| WI-2 | `digest_precompute` self-cancellation | ~44/hr (largest DB family) | Yes | Low | **2** |
| WI-3 | GitHub create-PR fail-open idempotency + agent retry storm | ~82/hr POST 500s; dup-PR hazard | Yes | Med | **3** |
| WI-4 | Orchestrator busy-loop (amplifier) | 2 Hz spin during any hold | — (this IS the loop) | Low | **4** |
| WI-5 | Warm-pin liveness proxy + anti-re-pin | (drives WI-4 bursts) | partial | Med | **5** |
| WI-6 | Deadlocks in `_process_post_commit` | ~6/hr | Yes | Med | **6** |
| WI-7 | Strategic `MissingGreenlet` (expired attr after rollback) | ~1–2/hr | Yes | Low | **7** |
| WI-8 | Post-commit orphan backlog > 100/cycle | drain-capped every ~2 min | downstream of WI-3/WI-6 | Low | **8** |

All file:line references are from the 2026-07-05 investigation; the implementing agent should re-confirm against fresh `automain` (lines drift).

---

## WI-1 — Sidecar-recovery infinite retry loop  ⟵ highest volume

**Symptom.** Every ~2 min: `[SIDECAR-RECOVERY] Source <id> has unapplied side-car verdict (lane=review_second_opinion sidecar=<id> status=COMPLETED) — retrying application` for the **same 11 sources**, plus `Retried 11 unapplied side-car verdict(s)`. The apply side logs the same challenge IDs as `already resolved — ignoring replayed terminal status`. ~348 log-lines/hr, self-sustaining.

**Root cause.** `recovery_sidecar.py:191-248` (`_sweep_stranded_sidecar_sources`) selects every source whose `context[_SIDECAR_VERDICT_APPLICATION_PENDING_KEY]` marker is set and whose sidecar is terminal, and calls `trigger_stranded_sidecar_resolution(sidecar, session)` (line 226). When the resolver determines the second-opinion challenge is **already resolved / a replayed terminal status**, it short-circuits without applying — **and the pending marker on the source is never cleared.** So the source is re-selected, re-logged, and re-"retried" (retried += 1, no exception) on every sweep, forever. This is the known "second-opinion fail-closed → review-approval loop" wedge class (prior fix `f47706c2`) resurfacing on the **recovery** side.

**Fix.** In the resolver's already-resolved / replayed-terminal branch, **clear `_SIDECAR_VERDICT_APPLICATION_PENDING_KEY`** from `source.context` (mark it applied/abandoned) and commit, so the recovery sweep stops re-selecting it. Verify the marker is cleared on *all* terminal outcomes of `trigger_stranded_sidecar_resolution`, not only the happy-path apply. Guard the clear against the documented expired-attribute-after-rollback hazard (snapshot ids first — see the inline comment at `recovery_sidecar.py:217-224`).

**Tests.** `testing/test_recovery_sidecar.py` (add: a source whose challenge is already-resolved has its pending marker cleared after one sweep and is NOT re-selected on the next). `testing/test_second_opinion*.py` for the apply path.

**Verify in prod.** `[SIDECAR-RECOVERY] Retried N` lines drop to ~0; the set of "unapplied" source IDs stops repeating.

---

## WI-2 — `digest_precompute` cancels its own live query  ⟵ largest DB family

**Symptom.** ~44/hr `[DB] Invalidated pool connection after CancelledError [disconnect]`, **1:1 timestamp-identical** (confirmed 15/15) with `[DIGEST_PRECOMPUTE] tick exceeded 35s budget — abandoning this tick`, every ~80s. The ejected connection is **healthy** — the app cancelled the query.

**Root cause.** `digest_precompute.py:621`:
```python
async with asyncio.timeout(PRECOMPUTE_TICK_BUDGET_SECONDS):   # =35s, line 77
    refreshed = await run_precompute_tick(AsyncSessionLocal)
```
`run_precompute_tick` chronically overruns 35s. `asyncio.timeout` fires **mid-`session.execute()`**, asyncpg raises `CancelledError`, SQLAlchemy's disconnect handler (`database.py:346-362`) flags `is_disconnect=True` and ejects the pool connection. The `except TimeoutError` at line 628 treats this as "recoverable / bounded, not fatal" — **unaware it sheds a pool connection every tick.**

**Fix.** Make the budget **cooperative** instead of a query-cancelling hard timeout: inside `run_precompute_tick`, check elapsed wall-time **between** digest keys/units of work and return early (partial refresh; next tick continues) when the budget is exceeded — so no in-flight query is ever cancelled. Keep an *outer* safety timeout only as a last resort with a generous margin, or drop it entirely once the inner loop self-bounds. The compute path is already row-capped and has its own `statement_timeout`, so a single key can't run away.

**Tests.** `testing/test_digest_precompute.py` (add: a tick that exceeds budget returns a partial result and does NOT cancel/invalidate the session; assert no `CancelledError` propagates).

**Verify in prod.** `CancelledError [disconnect]` rate collapses to ~0; `tick exceeded budget` may still appear but with no paired DB invalidation.

---

## WI-3 — GitHub create-PR fail-open idempotency + agent retry storm  ⟵ safety hazard

**Symptom.** `POST /api/agent/github/pull-requests` returns 500 from an agent (`curl/7.81.0`) retrying every 6–9s (~71–82/hr); `create pull request (403): rate limit exceeded (short-circuited locally)`. The install token's real **5000/hr primary** limit is genuinely drained (clean monotonic countdown to a real reset — the local limiter at `github.py:256-271` is correct, not mis-tuned).

**Root cause (two parts).**
1. **Fail-open idempotency (data hazard).** `_find_open_pull_request` (`github_pulls.py:206`) does a `GET /pulls?head=…` to dedup before creating. Under rate-limit exhaustion that GET **short-circuits to a synthetic 403 → returns `None`**, so the caller concludes "no existing PR" and **POSTs anyway.** When the quota window resets, this creates **duplicate PRs.**
2. **No agent-side backoff.** `routers/agent_github.py:270-339` → `create_pull_request` raises HTTP 500 on any non-2xx (`github_pulls.py:278`); the agent's `curl` retries the whole endpoint with no backoff, re-burning quota and re-tripping the limiter.

**Fix.**
- Make the idempotency check **fail-closed**: if the open-PR lookup is rate-limited/uncertain (short-circuit or non-200), do **not** proceed to POST — return a *retryable* signal (e.g. 429/503 with `Retry-After`) so no duplicate is created.
- Add **agent-side backoff** on 403/500 from the create-PR endpoint (respect `Retry-After`; exponential with cap). This is in the agent (TypeScript) — coordinate the contract with the backend response.

**Tests.** `testing/test_github_pulls.py` (add: rate-limited open-PR lookup ⇒ create is refused, not fail-open). Agent-side: unit-test the backoff on 429/503.

**Verify in prod.** create-PR 500 rate drops; no duplicate PRs appear after a quota-reset boundary.

> Note: WI-4 (busy-loop) independently reduces the *monitoring-read* slice of GitHub quota burn (~10× fewer `get_pull_request`/branch-ref reads), which lowers how often the limiter trips — but it does **not** fix the create-PR fail-open or the agent retry cadence. WI-3 and WI-4 are complementary.

---

## WI-4 — Orchestrator busy-loop (load amplifier)

**Symptom.** During any period where a task is stuck QUEUED-but-undispatchable, the main loop cycles at **2 Hz** (`sleep(0.5)`), re-running the full pass (dispatch + review/PR processing + monitoring) — e.g. `[ORCHESTRATOR] Warm-pin skip-provision for task <id>` logged ~1.5×/sec.

**Root cause.** `orchestrator.py:9074`:
```python
elif queued_tasks:
    await asyncio.sleep(0.5)
else:
    await asyncio.sleep(5)
```
`queued_tasks` (re-fetched each cycle at `:4563`) is treated as "there's dispatchable work worth spinning for," but it includes tasks that **cannot** be dispatched this cycle and never increment `_cycle_dispatched`. **Five** such hold classes all keep `queued_tasks` non-empty with no `retry_after`:
1. Warm-pin skip-provision — `:4894-4934` (rollback + continue)
2. Strategic-local-runner hold — `_hold_strategic_local_runner_task` (`:4786-4794`, body `:827-888`) — **the most common real-world trigger** when the WSL strategic runner is offline
3. Project-resource/spend defer — `:4795-4820`
4. No-credentials-at-all — `:5478-5504`
5. Backlog-reuse first-refusal hold — `:5226-5258`

`_cycle_dispatched` increments only at `:4793, 4847, 4865, 6194` — every one moves the task **out** of QUEUED, so it's a sound "made real progress" signal.

**Fix.** Gate the fast path on progress, not queue-presence:
```python
elif _cycle_dispatched > 0:      # made real progress → grab next batch fast
    await asyncio.sleep(0.5)
else:                            # nothing dispatchable (holds/defers) → back off
    await asyncio.sleep(5)
```
Do **not** add an "OR dispatchable queued work" clause — computing that correctly means replaying the entire per-task hold chain, which the dispatch loop already did and summarized as `_cycle_dispatched`; a naive version re-creates the loop. Also initialize `_cycle_dispatched = 0` at loop top (`~:3165`) defensively. (Optional belt-and-suspenders: stamp a short per-task cooldown / `retry_after_until` in the warm-pin skip-provision branch so the specific task isn't re-evaluated every cycle.)

**Why safe (no starvation, nothing needs 2 Hz).** If a task is genuinely dispatchable, the loop dispatches it and increments the counter — so `_cycle_dispatched == 0` provably means nothing was dispatchable; 5s costs only ~4.5s extra latency to notice external state changes (slot frees, account recovers, runner registers), all second-to-minute-scale. CI polling is already 30s-gated + webhook-driven; the per-cycle health checks already run at 5s cadence when slots are full (`:3596-3598`→`:3627`). Verified: no dispatch-then-bounce path can latch the counter.

**Tests.** `testing/test_orchestrator*.py` (add: a cycle whose only queued tasks are held ⇒ `_cycle_dispatched == 0` ⇒ 5s sleep path; a cycle that dispatches ⇒ 0.5s path).

**Verify in prod.** `Warm-pin skip-provision` (and other hold) log lines drop from ~1.5/sec to ~1/5s; monitoring-read GitHub call rate and CI-lock churn fall ~10×.

---

## WI-5 — Warm-pin liveness proxy + anti-re-pin guard

**Symptom.** The busy-loop bursts (WI-4) orbit a fix task pinned to a VM that is **already deleted**. Log-confirmed: task's source VM busy until 18:54, `[VM CLEANUP] Deleted VM agent-vm-…` at 18:55:05, yet skip-provision loops against it ~30× until the 90s heartbeat window lapses (`reason=vm_unresponsive`) → cold provision.

**Root cause (three structural gaps).**
1. **Corpse-tolerant liveness.** `is_vm_alive` (`warm_vm_state.py:120-133`) is a pure ≤90s heartbeat check; a VM that just completed a task and is being torn down still reads "alive" off its completion POST's heartbeat. The skip-provision gate (`orchestrator.py:4920-4923`) trusts it.
2. **Pin set to a terminal source.** `review_pipeline.py:3904-3946` (`_create_and_persist_spawn`) sets `warm_handoff_deadline = now + 240s` on **every** new fix row when the source VM is IN_PROGRESS **or** recently terminal (≤`WARM_HANDOFF_WINDOW_SECS`=240s) — with **no guard against an already-pinned-but-unclaimed sibling** for the same PR. This mints a fresh 240s pin per new row (why the deadline "never expires").
3. **Unconditional teardown, no chain-continuity defer.** `agent_status.py:2445-2454` calls `fire_and_forget_vm_delete(old_vm_id, reason="agent_completion")` (which synchronously stamps the doomed marker, `vm_lifecycle.py:339`); only backlog-reuse (`:2392-2444`) defers teardown — chain-continuity follow-ons get none, so a pin to a terminal source is dead-on-arrival.

**Fix.**
- **Liveness:** AND-in `not is_orchestrator_initiated_delete(_warm_target_vm_id)` (`vm_lifecycle.py:67`) to the skip-provision gate at `orchestrator.py:4920-4923`, so a torn-down VM's stale heartbeat no longer keeps a task QUEUED.
- **Anti-re-pin:** at the spawn site (`review_pipeline.py:3904-3946`), skip pinning if a pinned-but-unclaimed review/fix for the same PR / `warm_target_vm_id` already exists (mirror the existing skip-reason funnel: `source_cold`/`stale_source`/…). Stops the new-row pin churn.
- **Optional (efficiency, not storm-critical):** add a chain-continuity teardown-defer in `agent_status.py:2392-2454` mirroring backlog-reuse, so warm-chain handoff can actually deliver its efficiency win instead of always cold-falling-through.

**Note.** WI-4 alone stops the *storm* from any stuck task; WI-5 fixes the *correctness* (don't pin to a corpse) so warm-continuity works and tasks don't idle 90s before cold fallback. Both are wanted; WI-4 is the blast-radius cap, WI-5 is the specific bug.

**Tests.** `testing/test_warm_continuity*.py` / `test_review_pipeline*.py` (add: pin refused when a pinned-unclaimed sibling exists; skip-provision falls through when the target VM is doomed).

---

## WI-6 — Deadlocks in `_process_post_commit`

**Symptom.** ~6/hr `DeadlockDetectedError`, bursty, on concurrent agent-status callbacks. Postgres detail shows classic two-transaction cross-lock (`Process A waits for ShareLock … blocked by B` and vice-versa).

**Root cause.** `/api/agent/status` (`routers/agent_status.py:3739`) → `task_lifecycle.py:_process_post_commit` (frames at `:2367/:2585/:2622/:5547`) → `review_pipeline.py:1908 _spawn_pipeline_task`. When multiple agents post status/completion concurrently, their post-commit transactions acquire **Task and Project row locks in inconsistent order**, violating the canonical **Project-before-Task** ordering (`task_locking.py`). Cap-bound (VM slots), not loop-frequency-bound — WI-4 does not affect it.

**Fix.** Enforce Project-before-Task lock acquisition throughout `_process_post_commit` (and `_spawn_pipeline_task`); where a Task is locked first, acquire the owning Project lock up front. Audit for any `FOR UPDATE`/autoflush that takes locks out of canonical order. Consider a short deadlock-retry wrapper as defense-in-depth (not a substitute for correct ordering).

**Tests.** A concurrency test that fires two post-commit paths on tasks sharing a project and asserts no deadlock (or deterministic lock order).

---

## WI-7 — Strategic `MissingGreenlet` (expired attribute after rollback)

**Symptom.** ~1–2/hr `MissingGreenlet` from the strategic-session compensation path.

**Root cause.** `strategic_resident_session.py`: `start_resident_session` fails (409 "No strategic_review task available", `:1097`), enters `except`, calls `session.rollback()` (`:1148`) — which expires all ORM attributes even under `expire_on_commit=False` — then reads `project.id` at `_release_start_claim(project.id, …)` (`:1156-1158`) → sync expired-load in async context → `MissingGreenlet`. The inline comment (`:1134-1136`) acknowledges the hazard yet still triggers it. This is the known "Strategic-hold MissingGreenlet wedge" (different call-site than the earlier `_hold_strategic_local_runner_task` occurrence).

**Fix.** Snapshot `project.id` (and any other needed attributes) into plain locals **before** `session.rollback()`, and use the snapshots in the compensation call. Grep the file for other post-rollback ORM reads and apply the same pattern.

**Tests.** `testing/test_strategic*.py` (add: the 409 path completes compensation without MissingGreenlet).

---

## WI-8 — Post-commit orphan backlog exceeds per-cycle cap

**Symptom.** `[RECOVERY] Hit per-cycle cap of 100 orphan candidates — remainder deferred` every ~2 min — a persistent backlog draining slower than it accrues.

**Root cause.** Orphaned post-commit work accrues faster than the 100/cycle drain, fed largely by the failures in **WI-3** (GitHub) and **WI-6** (deadlocks failing post-commit steps).

**Fix.** Re-evaluate **after** WI-3/WI-6 land — the inflow should drop substantially. If a backlog persists, raise/parallelize the drain rate or add windowed catch-up. Do **not** raise the cap blindly before reducing inflow.

**Tests.** N/A until re-evaluated.

---

## 2. Recommended sequencing

1. **WI-1, WI-2, WI-3** — the three dominant, independent wedges. Parallelizable (disjoint files: `recovery_sidecar.py`, `digest_precompute.py`, `github_pulls.py`+agent). Biggest error-budget movement.
2. **WI-4** — cheap, low-risk busy-loop gate (one file, `orchestrator.py`). Cuts the ~10× amplification.
3. **WI-5** — warm-pin correctness (`orchestrator.py` skip-provision + `review_pipeline.py` spawn). Sequence *after* WI-4 (shares `orchestrator.py` region — avoid conflicts) or coordinate.
4. **WI-6, WI-7** — lock-ordering + rollback-snapshot. Independent; parallelizable.
5. **WI-8** — re-evaluate after WI-3/WI-6.

Each WI is a self-contained PR against fresh `automain`. WI-4 and WI-5 both touch `orchestrator.py` — land WI-4 first, then rebase WI-5. Everything else is disjoint and can fan out to parallel implementation subagents.

## 3. Global verification (after landing)

Re-run the census on the new revision and confirm each line moves:
- `[SIDECAR-RECOVERY] Retried N` → ~0 (WI-1)
- `Invalidated pool connection after CancelledError` → ~0, no longer paired with digest ticks (WI-2)
- create-PR 500s → down; **no duplicate PRs** post quota-reset (WI-3)
- `Warm-pin skip-provision` → ~1/5s not ~1.5/s; monitoring GitHub reads ~10× lower (WI-4)
- warm pins no longer set against doomed VMs (WI-5)
- `DeadlockDetectedError` → ~0 (WI-6)
- strategic `MissingGreenlet` → 0 (WI-7)
- `[RECOVERY] Hit per-cycle cap` → clears or shrinks (WI-8)

## 4. Out of scope (already done / separate)

- Global-rate-cap **starvation** (50→500 raise + PR #12801 per-project isolation) — **done, autonomy confirmed spawning.**
- Global-cap **redesign + visibility** (category-aware initiative-vs-servicing budgets) — implemented in **PR #12852** (`plan/b8bc4e94` → `automain`), currently being salvaged to mergeable/CI-green for operator approval behind `observe` mode. Orthogonal to this error-budget work.
