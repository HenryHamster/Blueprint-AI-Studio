# Engine Big Levers — strategy notes (2026-07-06)

Consolidated from a strategy session reviewing what to build next, grounded in
web research on the mid-2026 agent landscape, autonomous-research systems, and
cheap-model economics. Items ordered roughly by decision-readiness, not size.

---

## 1. Model/provider economics (immediate; unresolved tension at bottom)

### Current state (verified against prod `/api/admin/settings`)
- Routing is already tiered: ops→Haiku, build/fix/debug→Sonnet 4.6 (medium
  effort), review-category→Opus 4.8 (high), strategic→Fable 5 (max, Opus
  fallback). qa/docs/deps/review pinned to Sonnet.
- Capacity = 6 Claude Max seats, managed by `max_plan_pool.py`. Sonnet and
  Haiku share the **Opus session bucket** per seat — mid-tier burn displaces
  Opus/Fable headroom.

### Research findings (first-party-verified where possible)
Quality (agentic benchmarks): GLM-5.2 ≈ Sonnet-class (SWE-V ~80, SWE-Pro 62.1
vs Opus 4.8's 69.2). DeepSeek V4-Pro a notch below (SWE-V 80.6, SWE-Pro 55.4).
Kimi K2.6 between them; redundant with GLM.

Effective cost, agent-shaped workload (~90% prompt-cache hit):

| Option | eff. input /Mtok | output /Mtok | vs Sonnet API |
|---|---|---|---|
| Sonnet 4.6 API-equiv | ~$0.57 | $15.00 | — |
| GLM-5.2 (z.ai metered) | ~$0.37 | $4.40 | ~3.5× cheaper |
| DeepSeek V4-Pro metered | ~$0.05 | $0.87 | ~15× cheaper |

Subscriptions (all vendors ≈ 10–30× face value in API tokens **if maxed**):
- GLM Coding Max $160 ($112 promo): ~8,000 prompts/wk, BUT ~1 in-flight/account
  (credible report), 2–3× quota burn during China peak (= our overnight), ToS
  bans account pooling (3-strike). Would need a second max_plan_pool.
- Kimi Vivace $199: 30 concurrent documented; weaker model.
- DeepSeek: no subscription exists; metered only (500–2,500 concurrency).
- Claude Max 20x $200 ≈ $2–6k/mo API-equivalent when maxed (independent
  testing; Anthropic publishes no numbers).

Risks/timing:
- **2026-07-13: Claude +50% weekly boost expires** — pool capacity drops ~1/3.
- Anthropic Agent SDK credit split (metered repricing of headless usage):
  announced, paused 2026-06-15, revision pending. Standing existential risk to
  the Max-pool architecture. Multi-provider support is the hedge.
- Third-party GLM serving (OpenRouter/DeepInfra) is almost all FP4/FP8
  quantized — use z.ai first-party only.
- Data exposure: routing mid-tier to z.ai/DeepSeek sends private repo content
  to those providers; outputs run in sandboxes with push-capable creds.
  Conscious decision + scope canary tokens.

### Decisions so far
1. Buy metered API keys only: z.ai (GLM-5.2 → medium tier), DeepSeek
   (→ low tier: ops category, inline_llm calls, future trials). No
   subscriptions until metered spend >$150–200/mo sustained AND concurrency
   proves workable.
2. Cap Anthropic seats at 6. After offload, read pool utilization 2–4 wks;
   likely shed to 3–4 seats (Opus/Fable-tier throughput is the sizing bind).
3. Verify before scale: z.ai `api.z.ai/api/anthropic` and DeepSeek
   `/anthropic` endpoints with metered keys (tool-use, streaming, cache-hit
   billing). Sonnet stays as `fallback_model`.

### Implementation update (2026-07-07)
- PR #13234 merged to `main` (`3038602c`) with z.ai + DeepSeek as first-class
  providers, including provider enum/migration 0411, registry entries, admin UI
  surfacing, deploy health, inline/direct LLM support, agent runner env/auth
  plumbing, redaction, and cost attribution.
- Live smoke tests passed before merge:
  - DeepSeek Anthropic-compatible endpoint:
    `https://api.deepseek.com/anthropic/v1/messages`,
    model `deepseek-v4-flash`.
  - z.ai Anthropic-compatible endpoint:
    `https://api.z.ai/api/anthropic/v1/messages`, model `glm-5.2`.
  - Direct helper smoke passed for both after response-block parsing was fixed.
- Shipped routing defaults:
  - GLM/z.ai is confined to the true strategic lane:
    `strategic_review` + `design_doc_update` route
    `zai -> anthropic -> deepseek -> openai -> gemini`, max effort.
  - `research` + `plan` moved into an `initiative` lane:
    `deepseek -> anthropic -> openai -> gemini`, medium effort.
  - `_default`, `build`, and `ops` route DeepSeek-first; `review` remains
    Anthropic-first with DeepSeek as the next fallback. Claude accounts still
    exist and remain the fallback/quality tier.
  - Provider selection mode is `preferred`: try the first provider with
    capacity, fall through on unavailable/cooldown/no-capacity.
- Provider effort defaults:
  - z.ai: all efforts use `glm-5.2`.
  - DeepSeek: low uses `deepseek-v4-flash`; medium/high/max use
    `deepseek-v4-pro`.
- Guardrails shipped:
  - Generic `ANTHROPIC_API_KEY` no longer silently satisfies z.ai/DeepSeek
    unless a compatible base URL is explicitly present; provider-specific base
    URLs win over generic Anthropic base URL.
  - Rate-limit/auth/usage-exhaustion patterns are wired through the agent
    runner and backend classification paths for the new providers.
  - Runtime-config backfill is additive for existing persisted configs, so old
    deployments gain z.ai/DeepSeek defaults without wiping operator overrides.
  - Non-Anthropic cache-token subcounts are priced at base input rate until
    provider-specific cache pricing is modeled; Anthropic cache pricing remains
    separate.
- Verification: four review subagents ran; local focused/broad backend suites,
  agent unit/typecheck, admin-ui typecheck passed. CI rerun passed all frontend,
  agent, backend shards, then auto-merged.

Remaining provider follow-up:
- Run explicit streaming/tool-use/cache-billing probes. Current live smoke
  proves basic Anthropic-compatible message calls, not full billing semantics.
- After a day of real traffic, compare task success/cost by provider/model and
  decide whether Z.ai should stay first for build/default or be narrowed.
- Add the golden-task suite before moving strategic/review tiers off
  Anthropic-first.

### RESOLUTION — the capacity ladder
A truly maxed subscription seat is ~10–30× cheaper per token than metered
API. Full analysis: consumer-sub arbitrage does NOT scale — the subsidy
prices for idle humans, fleets are the detection target (Anthropic credit
split = this happening), and the failure is correlated (fingerprint → ban →
capacity class zeroed). Capacity you can't openly buy more of is
anti-scalable. But the discount survives at scale under legitimate names.
Ladder (route work down the cheapest rung meeting tier + latency):

1. **Existing consumer seats** — harvest what we have (babysitter is sunk:
   max_plan_pool), size to trough, DON'T grow this rung.
2. **Metered + cache** — elastic default (GLM medium, DeepSeek low).
3. **Batch APIs (−50%) + off-peak windows** — the system is queue-shaped;
   judges/audits/digests/inline calls are batchable; deferrable-backlog
   scheduler time-shifts into discount windows (verify DeepSeek V4 off-peak
   pricing still exists).
4. **Committed-use / enterprise contracts** — the grown-up subscription:
   30–60% off list at negotiated volume, vendor-sanctioned. Trigger: when
   sustained monthly metered spend justifies the phone call.
5. **Self-hosted open weights on spot GPUs** — the permissionless floor
   (~$0.10–0.30/Mtok all-in for 30–80B-class at high utilization); no vendor
   can reprice/throttle/ban. Pays only at high sustained volume — the low
   tier + trials workload is what gets there. Frontier work never moves here
   and doesn't need to (smallest token share).

Operator verdicts (2026-07-06): batch likely too slow for agent loops (keep
for judges/digests only); off-peak unreliable; rungs 4–5 parked until
product-scale volume — self-host break-even ~15–50M tok/mo AND assumes
60–90% utilization ops burden; commodity open-weight APIs (DeepInfra et al.)
already capture most of that margin. Near-term reality = rungs 1–2.

The engine piece that makes the ladder work = the **quota/price-aware
dispatcher**: each capacity class carries (price, concurrency, latency,
deferability, risk); dispatch + a standing deferrable-work backlog
(fill-the-window, fill-the-discount-window) does the arbitrage.
NEXT: design this concretely against max_plan_pool + dispatch.

---

## 2. Direction state + trials + optional objective
(Generalization of "evolve mode"; three primitives, each useful alone.)

1. **Durable direction state**: directions already accumulate *code* via
   staging branches; they don't accumulate *data*. Add a direction-scoped
   store (structured rows + artifacts) all child tasks read/write:
   measurements, repro scripts, candidate diffs, findings, dead ends.
2. **Trial primitive**: execution without task ceremony. Materialize
   base+patch (or command) in a sandbox, run, write a structured result row
   to direction state. No PR/review/judge — output is evidence, not code.
   ~70% exists: compute substrate + BLUEPRINT_RESULT_FILE + results contract.
   New: cheap, frequent, direction-scoped, without destabilizing scheduler.
3. **Optional declared objective**: direction MAY declare "success = this
   command's scalar." When present → selection: rank trials, cull, iterate
   (evolve-mode emerges = objective + trials + state + a sort). When absent,
   trials still gather evidence; nothing degrades.

Evolve-mode notes: campaign pins one base commit; variants are patches to a
bounded region (file/function/config block), stored as archive rows, only
materialized in sandboxes; output = ONE ordinary PR (winner + search report).
Precondition is strict (runnable metric + boundable target) — DEN qualifies,
most engineering directions don't, and that's fine. Evidence base:
AlphaEvolve (prod at Google; cheap-model breadth + executable selection).
Key constraint from research: best-of-N/selection ONLY pays where the
verifier is executable — LLM-judge selection measurably fails (widening gap
with K). Aligns with cutting back challenger/sidecar redundancy.

---

## 3. Compute substrate v2 (agreed: "fundamental improvement")
- **Long-running jobs**: engine currently can't own anything longer than a
  task sandbox. Needed: start/checkpoint/heartbeat/resume/harvest for
  multi-day runs (training, soaks, sweeps). DEN hits this wall next.
- **Pre-flight smoke** before billing long runs (known gap: "unmeasured =
  runtime command failure").
- **Results-contract enforcement** (built, under-consumed) + artifact/lineage
  capture (checkpoints, datasets, plots) into direction state.
- Container-aware exec (NGC etc.) folds in here — generic, no hardcoding.

---

## 4. Signal ingestion (agreed "sure")
The missing *input* half of autonomy. Projects declare signal sources (prod
errors, CI flake rates, dependency CVEs, TODO rot, user feedback); ingested
signals become evidence the strategist weighs when opening/feeding directions.
Broadly applicable by construction. Subsumes the "immune system" idea: point
it at the orchestrator's own error telemetry — self-diagnosis becomes just
another signal source, not a bespoke framework. Market validation: Factory
2.0's entire pitch is signals→triage→ship.

---

## 5. Self-extension: tools → prompts → gates (graduated trust)
One capability: **the engine may extend its own operating surface through its
own landing pipeline.** No new infrastructure — self-extension artifacts are
code, landed via the existing PR/review/rollup path.
- **Tools/skills**: tasks distill reusable helpers (test-runner wrappers, log
  parsers, repro harnesses, MCP tools) into a per-project skills/tools dir.
  Executable knowledge: self-validating, no retrieval system, consumer built
  in (next task invokes it). (Voyager pattern.)
- **Prompts**: move from frozen config to versioned prompt-pack artifacts in
  the repo; engine proposes diffs; canary metric gates rollout. Also: do a
  one-time manual prompt-rot audit pass regardless.
- **Custom version gates** (operator idea): executable checks in `gates/`
  run at qualification — e.g. boot the app's real deployment against a
  scratch DB, push a synthetic task through. Four anti-deadlock rules
  (v22 acceptance-criteria hardlock must not recur):
  1. Self-contained satisfiability — gate consumes only what the candidate
     build produces in isolation; never evidence from live prod running the
     thing under promotion.
  2. Fail toward escalation, not wedge — distinguish "gate says no" from
     "gate broken"; N failures → operator surface with logged one-click waiver.
  3. Advisory-first — a gate introduced in version N observes, enforces from
     N+1; gate diffs flagged for operator review (reward-hack defense).
  4. Budgeted — per-gate timeout + total suite budget.

---

## 6. Engine evals (golden-task suite)
The engine has no capability measurement — qualification checks code, not
end-to-end competence. Minimal build: 10–20 replayable task specs from real
past tasks, machine-checkable outcomes, on-demand runner. Four immediate
consumers: model-routing decisions (GLM/DeepSeek canary), prompt-pack
changes, version candidates, regression hunts. Consumer-first, no new
emitters. Build once for the canary, keep forever.

---

## 7. Witness breadth (operator already planning)
2–3 genuinely different external projects (different language/domain, one
non-ML). Every generality bug so far was found by DEN existing. Cheapest
generator of ground truth; only direct test of "handles any project well."

## 8. Rejected / deferred (with reasons — don't re-propose)
- Prose memory/lesson frameworks, metric/credit-assignment systems, immune-
  system agent as bespoke build: emitters-without-consumers pattern; minimal
  consumer-first versions only (one derived landed-surviving number for
  kill/fund; project brief doc; staleness query). Signal ingestion supersedes
  the immune-system framing.
- Orchestrator flight simulator: overkill for now.
- K-cheap-attempts best-of-N for engineering tasks: NOT cheaper (K× gen);
  only a cost-parity swap where selection is executable; revisit per-task-type
  only if GLM canary shows high failure on a test-verifiable type.
- Challenger/sidecar (judge-side redundancy): weak evidence; operator already
  cutting back. Generation-side redundancy w/ executable selection is the
  only redundancy with evidence.
- More Anthropic seats: capped at 6 pending offload data.

## Near-term checklist
- [x] z.ai + DeepSeek metered keys; basic Anthropic-compatible endpoint smoke
- [x] Provider entries + routing: GLM→strategic-only tier,
      DeepSeek→default/build/initiative/ops tier, Claude fallback
- [x] PR #13234 merged to `main`
- [ ] Explicit streaming/tool-use/cache-billing probes; scoped canary GitHub
      token
- [ ] Brace for 07-13 weekly-boost expiry (do NOT buy seat #7)
- [ ] Golden-task suite (engine evals) as the comparison harness
- [ ] Design: quota-aware base-load/overflow dispatch (§1 clean solution)
- [ ] Design doc: compute substrate v2 (§3)
- [ ] Design doc: direction state + trials + objective (§2)
- [ ] Prompt-rot audit pass (cheap, anytime)
