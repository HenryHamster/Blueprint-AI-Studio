# Scope: Duplicate rollup PRs on a shared research staging branch

**Status:** proposed · **Owner:** TBD · **Origin:** incident on research task
`bdb302f7` (rollup PR #13035 wedged → spurious `FAILED`)

## TL;DR

A research task's staging branch (`research/{prefix}`) can be rolled up to the
base branch by **two different parent tasks** — the research task itself *and*
any of its children that later becomes a rollup-parent (because it spawned its
own review/fix/decomposition children). Each parent's "already rolled up?" guard
consults **only its own** `context["rollup_pr_number"]`, so neither sees the
other's PR. GitHub's fallback dedup only matches **open** PRs, and **squash**
merges defeat the empty-diff finalize path — so a second, redundant rollup PR
gets created for a branch that already landed, then wedges and marks the task
`FAILED`. The work is not lost (the first rollup lands it); the damage is a
duplicate PR, a spuriously-`FAILED` research task, and ~2h of merge-gate churn.

## What happened (incident `bdb302f7`, all verified)

| PR | Created by (parent task) | Head → Base | Title source | Children counted | Outcome |
|----|--------------------------|-------------|--------------|------------------|---------|
| **#13026** | child `2fea759a` "perf: gzip…" (has its own children `b962f19f`, `d2e60c85`) | `research/bdb302f7` → `automain` | its **own** title | 1 | **squash-merged 04:39Z** — landed the whole branch |
| **#13035** | research parent `bdb302f7` | `research/bdb302f7`@`2ec4b98e` (**same commit**) | **parent** title | 8 | created 04:46Z, wedged, task `FAILED` 06:35Z, bot-closed 06:46Z |

Both PRs pointed at the identical branch head. #13026 won by 7 minutes and
carried everything to automain (a trial 3-way merge of the head into current
`automain` is a **byte-identical no-op** — proven). #13035 had nothing to add.

## Root cause — four compounding defects

1. **Branch inheritance makes descendants rollup-eligible onto the parent's branch.**
   `research_completion.py:4700` seeds **every** spawned child's context with
   `"research_branch": research_branch` (the research parent's own staging
   branch) and `"original_base": automain` (`:4701`).
2. **Rollup head/target derive from the *inherited* context.**
   `rollup_management.py:2003-2006`:
   `staging_branch = plan_branch or research_branch`;
   `target_branch = parent_base or original_base`. So when a research child
   itself reaches `EVALUATING` with completed children, it rolls the **shared**
   `research/bdb302f7` up to `automain` — semantically it should never roll that
   branch straight to the base; that's the research parent's job.
3. **The duplicate-create guard is per-task, not per-branch.**
   `rollup_management.py:2387`: `existing_pr = parent_ctx.get("rollup_pr_number")`
   (+ same-task `rollup_pr_creating_at` claim at `:2398`). #13026's marker lives
   in the **child's** context, so the research parent's marker is empty and it
   falls through to create #13035.
4. **Fallback dedup is open-only + squash defeats empty-diff.**
   `github_pulls.py:272` filters `state=open`; #13026 had already merged/closed,
   so the pre-create lookup returns "none". And the `empty_diff` COMPLETED path
   (`rollup_management.py:2661`) keys on GitHub's "no commits between" — which a
   **squash** merge does not produce (branch commits stay distinct from the
   squash commit), so #13035 is treated as a real, non-empty PR and proceeds.

Note: `_classify_merged_rollup_landing` (`rollup_management.py:339`) already
knows how to finalize a parent when its rollup PR is found merged — but it is
only reached via the `existing_pr` marker path (`:2440-2448`), which the
research parent never entered because its marker was empty.

## Blast radius

Triggerable by **any research direction where a child becomes a rollup-parent**
(i.e. spawns review/fix or decomposition children) — a common shape. It only
surfaces as a *visible* failure when both the child-rollup and the parent-rollup
fire against the shared branch; in the squash case the parent-rollup is a
guaranteed duplicate. **Step 0 of implementation is to measure frequency**
(query below) before assuming it's a one-off — it likely explains a class of
spurious research-task `FAILED`s.

## Fix options

### Option A — cross-state "branch already landed" guard *(recommended, immediate)*
Before the POST (`rollup_management.py:2624`), when the per-task marker is empty
(`:2387`), query GitHub for a **merged** PR whose `head == staging_branch` (any
state, filtered to `merged`). If one exists, the branch already landed — route
the parent through the existing landed/no-op finalize (reuse
`_classify_merged_rollup_landing` `:339` or the empty-diff COMPLETED path
`:2661-2682`) instead of creating a new PR.
- **Squash-proof:** keys on "a merged PR from this exact head", not on tree/diff.
- **Small surface:** one function; reuses existing finalize machinery.
- **Must fail *closed*:** if the lookup is rate-limited/uncertain, **defer**
  (like the existing `_OpenPRLookup("uncertain")` contract at
  `github_pulls.py:296-303`) — never assume "not landed" and POST.

### Option B — staging-branch ownership *(root-cause, follow-up)*
Only the task that **owns** a staging branch may roll it up to the base. Gate
head/target selection (`rollup_management.py:2003-2006`) on an ownership
predicate: a research parent owns `research/{its-id[:8]}`; a plan owner owns its
`plan_branch`. A descendant holding an **inherited** `research_branch` must
either target the research branch (not `automain`) or not roll up at all (its
children's PRs already merged into the research branch — nothing to carry up).
Prevents the collision at the source and also fixes the semantic wart of a
`perf:` child squash-landing straight to `automain` and bypassing the research
rollup/review.
- Higher blast radius (changes nested-descendant rollup targeting); needs
  confirmation that descendant children already land in the research branch so
  nothing is stranded.

### Option C — widen the pre-create lookup *(defense-in-depth)*
Extend `_find_open_pull_request` (`github_pulls.py:255`) with a merged-state
sibling so the collapse-to-existing behavior also covers just-merged PRs. This
is essentially Option A implemented one layer down; ship it *with* A, not
instead of it.

## Recommended plan (phased)

- **Phase 0 — size it.** Count `research/*` and `plan/*` heads with >1 `Rollup:`
  PR across states; correlate with research tasks in `FAILED`. Decide priority.
- **Phase 1 — Option A** (stops duplicate PRs, spurious `FAILED`s, and the
  wedge immediately; low risk; keep as permanent defense-in-depth).
- **Phase 2 — Option B** (durable root cause; behind tests).

## Exact edit points

- `backend/app/rollup_management.py:2387-2407` — add the cross-state landed-branch
  check when `existing_pr` is empty; on hit, finalize via `:339` / `:2661`.
- `backend/app/rollup_management.py:2003-2006` *(Option B)* — ownership gate on
  head/target derivation.
- `backend/app/research_completion.py:4700-4701` *(Option B)* — mark the
  inherited `research_branch`/`original_base` as inherited-only so descendants
  don't treat them as rollup sources, or stop inheriting them as rollup inputs.
- `backend/app/github_pulls.py:255-303` *(Option C)* — merged-state lookup helper.

## Test plan

Primary: `backend/testing/test_rollup_management.py`,
`test_rollup_state.py`, `test_task_completion.py`, `test_research_completion.py`.
Add a **regression test**: two `EVALUATING` parents sharing one staging branch →
exactly one rollup PR is created; the second observes the merged PR and finalizes
as landed (`COMPLETED`, **no** new PR). Cover the **squash** case explicitly (the
empty-diff path must NOT be what saves it — the new merged-PR check must). Also
assert fail-closed behavior when the merged-PR lookup is uncertain (defer, don't
create). Run targeted:
`bash scripts/test-backend.sh testing/test_rollup_management.py testing/test_research_completion.py testing/test_task_completion.py`.

## Risks & non-goals

- **Non-goal:** the Alembic dual-head guard's GitHub-403 fetch-storm that #13035
  actually wedged on — separately fixed (#13032 identity, #13042 apply). This
  scope removes the *duplicate PR*; that fix removes the *wedge*. Both are worth
  having.
- **Non-goal:** CI-wait backstop timing (the 1h that turned a stuck rollup into a
  hard `FAILED`).
- **Risk (A):** one extra GitHub call per rollup-create; must fail-closed to
  "defer" on uncertainty or it reintroduces duplicate risk.
- **Risk (B):** re-targeting descendant rollups could strand work if any
  descendant currently relies on rolling straight to `automain`; verify first.

## Appendix — measurement query (Phase 0)

```bash
# heads with more than one "Rollup:" PR (any state) → collision candidates
gh pr list --repo HenryHamster/BlueprintAIStudio --state all --search 'Rollup: in:title' \
  --json number,headRefName,state,title -L 500 \
  | jq -r 'group_by(.headRefName)[] | select(length>1)
           | "\(.[0].headRefName): \(length) PRs — \([.[].number]|join(","))"'
```
