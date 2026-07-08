# Token Cost Reduction Plan - 2026-07-08

Working plan from the July 2026 provider/cost discussion. This is separate from
`engine-big-levers-20260706.md`: the goal here is specifically to reduce LLM
token burn per task without hiding real quality regressions.

## Current baseline

The immediate provider hedge has shipped:

- PR #13472 merged to `main` as `6d8d50c7a`.
- Backend deploy and agent VM image build both completed successfully.
- Provider usage normalization now covers Anthropic, OpenAI, DeepSeek, and Z.AI
  usage shapes.
- Cost attribution is cache-aware for provider/model pricing and trial-arm
  analytics.
- DeepSeek/Z.AI/OpenAI API-key balance, quota, and capacity failures should no
  longer be misclassified as non-recoverable auth failures.
- GLM/Z.AI is narrowed to strategic-tier work. DeepSeek is the cheaper
  low/medium/default lane with Claude fallback.

Operational caveat: already-running warm VMs can still carry older agent code
until recycled. Already-disabled API-key accounts may need manual re-enable.

## Success metrics

Track every proposal against these metrics:

- Median and p90 input tokens per terminal task.
- Median and p90 uncached input tokens per terminal task.
- Cache hit rate: fraction of input tokens billed at cached rates, by
  provider/model/task type. A cache-hit-rate gain can beat a same-sized prompt
  trim, so treat it as a first-class metric, not an accounting detail.
- Output tokens per task.
- Agent turns (tool-call round-trips) per task. The token integral grows with
  turn count even at fixed context size, so turns are a leading indicator of
  cost — a task that takes 40 exploratory calls costs several times one that
  takes 12 with an identical final diff.
- Dollars per terminal task and dollars per merged PR.
- Retry rate, fail rate, and verified merge rate by provider/model/task type.
- Human/operator intervention rate.
- Time to task completion.

The goal is not just lower spend. A change is only a win if it reduces spend at
roughly equal or better success rate, or if the quality tradeoff is explicit and
reserved for low-risk task classes.

## P0 - Prove token and cost tracking is accurate

Do this before making aggressive cuts, because bad telemetry can make the wrong
optimization look correct.

1. Persist raw provider usage payloads per attempt.
   - Keep a sanitized raw usage blob next to normalized counts.
   - Include provider, model, auth mode, request id if available, and API shape.
   - This lets us debug DeepSeek/Z.AI/OpenAI cache semantics instead of guessing.

2. Reconcile against provider billing portals.
   - Daily compare dashboard totals with provider portal usage.
   - Track expected vs observed spend by provider/model.
   - Alert if drift exceeds a threshold, e.g. 10 to 15 percent.

3. Add anomaly detection.
   - Very high uncached input ratio.
   - Missing cache counts on providers that should report them.
   - Input/output ratio spikes.
   - Task type suddenly using a higher tier model.
   - Provider/model with unknown pricing but nonzero tokens.

4. Attribute cost to prompt sections.
   - Record approximate token contribution by prompt section or context block.
   - At minimum: system/instructions, task payload, repo/context package, logs,
     diffs, direction/strategic state, prior attempts, tool output.
   - Without this, we only know tasks are expensive, not why.

## P1 - Reduce prompt and context size

This is the highest-confidence savings area. The observed pattern was very high
input tokens compared with much smaller output tokens, especially when large
cached and uncached context is sent repeatedly.

1. Audit prompt assembly.
   - Identify largest sections for create/fix/review/strategic/research tasks.
   - Produce a ranked table: section name, median tokens, p90 tokens, inclusion
     rate, and likely owner.

2. Replace broad state dumps with ranked context packages.
   - Include the most relevant files, directions, prior findings, and logs.
   - Prefer stable IDs and summaries for older state.
   - Fetch full artifacts only when the agent asks for them.

3. Cap repeated histories.
   - Logs, status history, prior attempts, review loops, direction history, and
     strategic summaries should all have bounded per-task budgets.
   - Preserve the latest failure, root-cause hypothesis, and operator decisions.
   - Summarize older material with explicit "omitted count" and references.

4. Improve compaction quality.
   - Strategic/resident context should keep decisions, constraints, open risks,
     current branch/PR, and exact next actions.
   - It should drop repeated narration, stale file listings, old command output,
     and superseded hypotheses.

5. Prefer pointers over blobs.
   - Store artifacts and long logs as addressable resources.
   - Embed short snippets plus fetch instructions, not the whole artifact.

Risk: over-trimming can cause agents to miss constraints and repeat mistakes.
Mitigation: canary on golden tasks and include explicit "available but omitted"
references.

## P1 - Cache economics as a design constraint

Cached input is ~10x cheaper on Anthropic and near-free on DeepSeek context-
cache hits. At burst scale, cache hit rate can matter more than prompt size:
a burst of parallel tasks on one project shares an enormous common context,
and whether that context bills at cached or uncached rates is a design choice
made in prompt assembly, not an accident.

1. Structure prompts for a maximal stable prefix.
   - Order prompt assembly: static instructions first, then slow-changing
     project/repo context, then per-task payload last.
   - The boundary between "stable" and "per-task" content is the cache
     boundary; everything after the first varying byte bills uncached.

2. Eliminate cache-busters.
   - No timestamps, task IDs, attempt counters, or random/nondeterministic
     ordering anywhere in the stable prefix.
   - Audit prompt assembly for accidental variance (dict ordering, "generated
     at" headers, per-request nonces).

3. Exploit shared context across parallel tasks.
   - Tasks fanned out on the same project should share an identical context
     prefix so the Nth concurrent task reads the first task's cache.
   - This makes wide bursts cheaper per task than trickled dispatch — the
     opposite of the naive intuition.

4. Measure hit rate before trimming.
   - A trim that reorders or perturbs the stable prefix can lower total tokens
     while raising uncached tokens, i.e. cost goes up. Every P1 trimming change
     should report cache hit rate alongside token counts.

Risk: cache TTLs (e.g. 5 min on Anthropic) mean the benefit depends on
dispatch cadence; a prefix that ages out between tasks buys nothing.
Mitigation: prefer batching same-project dispatches close together.

## P1 - Worker residency / warm-context amortization

The biggest structural lever, and the hardest. Every task today pays the same
cold-start tax: fresh sandbox, fresh agent, re-read the repo, re-derive the
conventions — then does its actual (small-output) work. That fixed exploration
cost, paid N times for N tasks, is likely the dominant share of input tokens.

The structural fix is a warm, long-lived worker per project doing task after
task in one context: task N+1 costs marginal tokens (cached prefix, repo
already understood) instead of full price. If cold-start context is ~60% of
task input, this is a 2-3x structural cut that compounds with the cache lever
rather than competing with it.

This is an architecture decision, not a spend optimization — it belongs to
the resident-workflow-v2 design (the resident strategist already proved the
pattern, and local runners are the natural substrate: long-lived processes
are easy on owned hardware, awkward on ephemeral GCE VMs). This plan tracks
it as a dependency and measures its effect, but does not own the design.

Nearer-term partial versions worth taking even before full residency:

- Richer design docs / task context packages so the worker starts with the
  mental model instead of rebuilding it (cuts exploration turns even in a
  cold-start world).
- Warm-context reuse within a task lineage: follow-on/fix tasks resume the
  parent's session or context package instead of starting cold.

Status: acknowledged hard; design lives with resident-workflow-v2.

## P1 - Repo navigation and retrieval efficiency

We discussed LSP/repo-map style tooling because agents often spend tokens
discovering what a language server or symbol index already knows.

1. Evaluate LSP/symbol index retrieval.
   - Symbols, definitions, references, call sites, imports, and diagnostics.
   - Use it to answer "what files matter?" before reading large files.

2. Add a repo-map or codegraph context source.
   - Compact file/function/class map.
   - Recent changed files and dependency edges.
   - Test ownership and likely impacted tests.

3. A/B against current `rg` plus file-read workflow.
   - Same golden tasks.
   - Compare tokens/task, success rate, retries, wall time, and missed-context
     failures.
   - Also compare TURNS per task, not just tokens: cutting exploratory
     round-trips cuts the token integral superlinearly, so a retrieval tool
     that halves turns is worth more than its per-call token cost suggests.

Risk: LSP setup can be brittle across languages and repos. Start with
TypeScript/Python in this repo, then generalize only if the data is strong.

## P1 - Route by price, quality, and risk

The provider work enables this, but the dispatcher still needs to become more
economics-aware.

Decision (2026-07-08): two-lane world. DeepSeek carries the bulk lane —
create/fix/default work AND standard review. Claude is reserved for
strategic/research work, the version/qualification gate, and fallback.
GLM/Z.AI stays strategic-only.

1. Keep Claude as the high-confidence fallback and quality tier; it holds the
   version/qualification gate.
2. Use DeepSeek for low/medium/default work and standard review, where
   failures are cheap and the merge gate catches misses.
3. Keep GLM/Z.AI strategic-only unless real task data shows it is economical.
4. Track per-provider success and cost by task type.
5. Dispatcher sophistication is deliberately deferred: a full quota/price-aware
   dispatcher (price, cache price, quota, latency, failure rate, risk) is
   over-engineered for a two-lane routing table. Revisit only if a third
   economically distinct lane appears.

Risk: cheaper models can increase retries or bad PRs. Do not judge on token
price alone; judge on cost per verified success.

## P2 - Reduce duplicate agent work

Subagents and reviewers are useful, but they can multiply prompt cost when each
one receives full context.

1. Make reviewer fanout conditional.
   - Always use it for high-risk/shared/core changes.
   - Use cheaper or fewer reviewers for narrow low-risk changes.
   - Candidate cut (2026-07-08): drop model review for non-rollup single
     tasks landing directly into automain — CI plus the promotion gate
     already backstop those, and the expensive review belongs at the rollup/
     version gate where changes aggregate. (A broader "machine-checkable
     acceptance criteria replace review" idea was considered and rejected as
     too non-general.)

2. Send reviewers scoped context.
   - Diff, relevant files, invariants, and test results.
   - Avoid full parent transcript unless needed.

3. Share compact findings between agents.
   - Agents should exchange structured findings and evidence links, not full
     logs or transcripts.

4. Use independent smaller-context reviewers where possible.
   - The benefit is not just cheaper model choice; it is starting from a lower
     context count.

Risk: fewer reviewers can miss bugs. Use this only after there is a risk model
and a golden-task or post-merge regression signal.

## P2 - Skills, plugins, and reusable tools

Skills/plugins are not automatic token savers. They help when they replace
repeated exploration with deterministic, compact workflows.

Good candidates:

- Cost-audit skill: pull raw usage, normalized counts, and pricing reconciliation.
- Prompt-section profiler: report token contribution by prompt block.
- Repo-navigation skill: symbol lookup, references, impacted files, impacted tests.
- Log parser skill: extract failing tests, stack traces, provider errors, and
  retry hints from large logs.
- Provider-billing probes: verify streaming, tool use, cache billing, and auth
  failure shape for each provider.

Weak candidates:

- Generic "be concise" prompt wrappers.
- Broad memory frameworks without a clear consumer.
- Skills that merely restate repo conventions without executable checks.

## P2 - Batch and deferrable work

Batch/off-peak work is not a good fit for tight interactive agent loops, but it
can reduce cost for non-urgent workloads.

Good candidates:

- Judges and audits.
- Digest generation.
- Metrics and retrospective analysis.
- Research sweeps.
- Golden-task evaluation runs.

DeepSeek publishes an off-peak discount window (roughly 50-75% off during
their UTC discount hours). Deferrable work on the DeepSeek lane should be
scheduled into that window by default — this is real money for zero
engineering beyond a dispatch-time preference.

Note on the Anthropic Batch API (50% off): NOT applicable today — the Claude
lane runs on subscription (max-plan) accounts, not metered API billing, so
there is no per-token bill to halve, and batch latency is a poor fit for the
interactive loops anyway. Revisit only if a metered Claude API lane appears.

Poor candidates:

- Interactive create/fix/debug loops.
- Anything blocking a live PR merge.

## P2 - Golden-task suite

Every significant cost reduction should be tested against replayable work.

Build 10 to 20 task specs from real historical tasks:

- create/fix/debug/review/strategic/research mix.
- Machine-checkable outcomes.
- Known expected tests or artifacts.
- Model/provider/prompt variants can be compared apples-to-apples.

Use this for:

- DeepSeek vs Claude routing decisions.
- GLM strategic value checks.
- Prompt trimming canaries.
- LSP/repo-map A/B.
- Skills/tooling ROI.

## P3 - Larger economic levers

These are real but not immediate.

Strategic posture: token prices are deflating roughly 5-10x per year
(DeepSeek V3 is that curve showing up). Bias engineering hours toward what
does NOT get cheaper on its own — wedge-hardening, verification, task
authoring, residency, multi-project supply — and deliberately under-invest
in token micro-optimization. The durable asset is an engine that can safely
run wide; the tokens to feed it are a depreciating cost that can partially
be waited out.

1. Committed-use or enterprise discounts.
   - Trigger when sustained monthly metered spend makes negotiation worth it.

2. Self-hosted open-weight models.
   - Only attractive at high sustained utilization.
   - More operational burden.
   - Frontier/high-risk work remains vendor-hosted.

3. Commodity open-weight APIs.
   - May capture much of self-hosting's margin without operational load.
   - Need quality and privacy review.

## Suggested execution order

1. P0 telemetry correctness and raw usage capture.
2. Prompt-section profiler.
3. Cache-buster audit of prompt assembly + cache hit rate as a reported metric.
4. Prompt/context size audit and top three trimming fixes (each reported with
   cache hit rate, per the P1 cache section).
5. Golden-task suite skeleton.
6. LSP/repo-map A/B on Python/TypeScript.
7. Conditional reviewer fanout and scoped reviewer context.
8. Off-peak scheduling preference for deferrable DeepSeek-lane work.
9. Provider billing probes for streaming, tool use, and cache billing.

## Open questions

- Are provider usage fields accurate enough for billing reconciliation, or do we
  need provider-specific raw payload retention first?
- How much of the high input count is stable cacheable prompt vs uncached task
  payload?
- Which prompt sections dominate create/fix tasks specifically?
- Does DeepSeek reduce cost per verified success, or only raw token price?
- Does LSP/repo-map retrieval reduce tokens without causing missed-context bugs?
- Which review fanout paths are actually preventing production regressions?

