# Blueprint Operator Experience — Operating Model

This document defines the operator information architecture (IA) and decision loops for Blueprint. It is scoped around **operator jobs** — what a real person managing a Blueprint deployment needs to understand, decide, and act on — not around existing UI components.

**Audience:** Product and engineering contributors implementing or refining operator-facing surfaces. The doc is canonical: when this conflicts with an existing component, the component is the thing to fix.

---

## 1. Core Objects and Relationships

### Project
The top-level unit of operator concern. A project is a tracked repository (GitHub). Everything else belongs to exactly one project. Projects have a **deploy state** (onboarded, paused, error), a resource envelope (cost ceiling, concurrency cap), and a policy profile (autonomous mode on/off, auto-review gates, promotion target branch).

### Task
A unit of agent work. Tasks are typed (create, review, fix, research, compute_experiment, etc.) and belong to a project. A task is the thing that costs money, consumes a runner, and produces a PR or a finding.

Key task fields an operator cares about:
- **Status**: QUEUED → PROVISIONING → IN_PROGRESS → EVALUATING → COMPLETED/FAILED/CANCELLED
- **Type**: determines the decision loop the operator joins (see §3)
- **Attempt count**: retries after transient failure up to a per-type cap
- **Cost**: cumulative LLM spend for this task
- **PR**: the GitHub PR this task produced or is attached to
- **Direction**: the strategic direction this task was spawned to serve (if any)

### Wave (parent / child tasks)
Some task types spawn child tasks automatically (a `fix` loop spawns a new `fix` after a review rejects; a `strategic_review` spawns `research` children). The parent holds aggregate state; children are the runnable units. A wave is a parent plus all its descendants at any depth.

Operator implication: a task in FAILED state may not need intervention if its parent wave is still healthy and will retry or re-route. Show wave status, not just leaf status.

### PR (Pull Request)
A GitHub PR created or attached to a task. The PR is evidence the task produced something. PRs have a lifecycle independent of the task: open → merged/closed. An operator may need to act on a PR even after its originating task is COMPLETED.

### Direction
A strategic direction is an operator-approved or autonomously generated work item at the goal level. Directions drive which research tasks Blueprint picks next. A direction has:
- **Priority**: high / normal / low
- **Status**: active / paused / completed / expired
- **Mode**: ship (direct create/fix work) or research (gather evidence before acting)
- **Hypothesis**: what Blueprint believes and intends to do

Directions are policy objects: the operator decides whether to approve, pause, or redirect them. They are not tasks — they are instructions that generate tasks.

### Finding
A finding is a structured output from a research or review task — a proposed improvement, bug report, or pattern observed. Findings feed strategic reviews and eventually become new directions. A finding that gets promoted to a direction is operator-visible; an unfiled finding is internal agent state.

### Message
An async message from Blueprint to the operator (or vice versa). Types: NOTE, QUESTION, FEEDBACK, BUG_REPORT, BLOCKER. Messages are the escalation path when a task can't proceed autonomously. A QUESTION message needs a human answer to unblock work.

### Version
A snapshot of the project at a promotion boundary. Blueprint tracks which commit SHA is the current candidate for promotion to the main/LTS branch. A version has qualification state (CI evidence, acceptance grade, any blockers) and is either passed, held, or failed.

### Runner
A compute resource that executes tasks. Runners are either cloud-provisioned (GCP VMs) or pull-based local runners registered by the operator. Runner state: active / inactive / deleted. Local runners self-report liveness via heartbeat.

### Account
An LLM provider account (API key pool) that Blueprint draws from for tasks. Accounts have spend caps, health status, and a provider association.

### Provider
An LLM API endpoint (Anthropic, OpenAI, etc.). Blueprint dispatches tasks to providers via accounts. Providers can be healthy, degraded, or over-quota.

### Deploy State
The current live status of the Blueprint orchestrator itself (not the project it is watching). Relevant to operators who self-host: is the orchestrator running, is the schema migrated, is the GCP job active?

---

## 2. Operator Spaces

Five spaces organize operator work. Each has a distinct job and decision rhythm.

### 2.1 Operate
**Job:** See what's happening right now and act on anything blocked.

The operator opens the console and wants to know: is Blueprint working? Are tasks progressing? Is anything stuck or expensive?

Primary objects in this space: **tasks** (live, filtered to active), **messages** (unread), **alerts** (anomalies, failures above threshold), **orchestrator health**.

Decision rhythm: glance several times per day; act when alerted.

### 2.2 Work
**Job:** Manage the task queue — what's in progress, what needs review, what failed.

The work space is a case-management view over tasks. The operator uses it to:
- Review PRs that need human merge approval
- Retry or cancel stuck tasks
- Diagnose failure patterns
- Respond to QUESTION messages that block a task

Primary objects: **tasks** (all states, filterable), **PRs** (open, needing action), **messages** (QUESTION type), **fix loops** (tasks in retry).

Decision rhythm: daily, after async notification or on a cadence.

### 2.3 Strategy
**Job:** Shape what Blueprint works on next.

The strategy space is where the operator steers autonomous research. It shows active directions, queued research tasks, findings that have surfaced, and the running update (Blueprint's current written understanding of the project state).

Primary objects: **directions** (portfolio), **research tasks**, **findings**, **running update**, **problem model**, **knowledge documents**.

Decision rhythm: weekly review; ad hoc when Blueprint surfaces a strategic question or a direction finishes.

### 2.4 Fleet
**Job:** Manage compute capacity — runners, VM pools, and cost.

The fleet space covers local runners and cloud compute. The operator uses it to register runners, monitor liveness, review per-runner cost, and diagnose dispatch failures (e.g., a task stuck in QUEUED because no eligible runner exists).

Primary objects: **runners** (status, capacity), **compute jobs** (active experiments), **warm VM pools**, **cost metrics by runner type**.

Decision rhythm: on-demand when adding capacity or investigating dispatch failures.

### 2.5 Policy
**Job:** Set rules that govern Blueprint's autonomous behavior.

Policy objects are project-scoped settings that alter risk, cost, or trust boundaries. They include: autonomous mode enable/disable, auto-review gates, per-direction pause, resource envelope (spend cap, concurrency), provider account assignment, and promotion target configuration.

Primary objects: **project settings**, **resource envelope**, **provider config**, **direction auto-pause rules**.

Decision rhythm: infrequent; prompted by a policy change or a trust event (e.g., runaway cost).

---

## 3. Decision Loops by Task Type

### Review / Fix loop
1. `review` task runs → either approves PR or produces rejection with reasons
2. On rejection: `fix` task spawns, attempts to address the findings
3. If `fix` produces a revised PR: another `review` runs
4. After N attempts without approval: escalates to FAILED, operator notified
5. Operator action: merge PR, cancel wave, or provide a direction override

### Research loop
1. Direction selected by autonomous research scheduler
2. `research` task runs, produces knowledge document(s) and finding(s)
3. On completion: `strategic_review` may spawn to update the problem model
4. Operator action: review running update, accept or reject new directions

### Compute experiment loop
1. `compute_experiment` task created (from a direction or operator request)
2. Arms dispatched to eligible runner(s), results aggregated
3. Per-arm criterion verdict computed (met / not met / unmeasured)
4. Knowledge document produced with verdict; feeds into strategy
5. Operator action: review verdict and decide whether to promote the approach

### PR merge loop
1. Task produces a PR
2. Auto-review runs (if policy enabled): review→fix cycles until approved or exhausted
3. Approved PR waits for operator merge (or auto-merge if policy set)
4. After merge: version qualification may advance
5. Operator action: merge or close PR; optionally trigger qualification

---

## 4. Useful Queues

These are the views that matter for day-to-day operation. Each is a filter over the object graph.

### Needs Decision
Tasks or directions that can't progress without a human choice. Criteria:
- Task has an unread QUESTION message
- Direction is in `requires_approval` state
- Review cycle exhausted without convergence (operator must decide next action)

This is the highest-priority queue. Items here block work and cost money every hour they sit.

### Needs Intervention
Tasks that have failed and are not auto-retrying, or are stuck in PROVISIONING/QUEUED longer than the expected window. Criteria:
- Task in FAILED with no remaining attempts
- Task in QUEUED/PROVISIONING for >15 minutes
- Task in IN_PROGRESS with no heartbeat for >10 minutes
- Any BLOCKER-type message unread

This queue surfaces system failures, not business decisions. Intervention usually means diagnosing infrastructure (runner offline, provider quota, GCP dispatch failure).

### Needs Review
PRs that are approved by the review loop but not yet merged. Criteria:
- Task is COMPLETED
- PR is open and has no blocking CI failures
- PR is not in a merge-freeze window

This is the "inbox" for operator merge actions. Items are ready; they just need a human to click merge (or to be auto-merged if policy allows).

### Safe to Ignore (Recently Active)
Tasks that completed normally in the last 24 hours. Criteria:
- Task is COMPLETED
- No QUESTION or BLOCKER messages attached
- PR merged or closed

These are logged for audit but require no action. Showing them alongside needs-attention items dilutes signal.

### Recently Resolved
Tasks that moved from FAILED or BLOCKED to COMPLETED in the last 48 hours. Criteria:
- Terminal status is COMPLETED
- A previous attempt was FAILED or a BLOCKER was cleared

Useful for confirming that an intervention worked, without surfacing these items as ongoing concerns.

---

## 5. Design Rules for Future Implementation

These are binding constraints for any UI task in this product.

### No page-scoped truth without labeling
If a panel shows a count, status, or metric, it must either reflect the full current state of that object or display a label indicating the scope (e.g., "last 24h", "active only", "this project"). Unlabeled counts that are silently partial mislead operators into thinking they have full visibility.

### Dangerous action confirmations must show blast radius
Any action that is hard to reverse (cancel a wave of tasks, delete a direction, pause autonomous mode, drain a runner pool) must present a confirmation dialog that names the specific objects affected and the impact:
- How many tasks will be cancelled?
- Will any in-flight PRs be abandoned?
- How long until work resumes?

"Are you sure? Yes / No" is not sufficient.

### Settings that alter risk or cost are policy changes
Anything that changes the resource envelope, provider assignment, autonomous mode, or auto-merge gate is a **policy change**, not a setting update. It must:
1. Show the current value and the new value before applying
2. Log the change with timestamp and actor
3. Be reversible from the same surface

These are not UI-tier concerns — they belong in the Policy space and must not be buried in a general settings form.

### Task views are case-management views
A task list is not a log. The operator's job is to act on tasks, not to read a history. Task list views should default to showing items in an actionable state (needs decision, needs intervention, needs review), with completed/resolved items collapsed or paginated below the fold. The primary action on each row should be the most likely next step for that task's current state.

### Staleness must be surfaced, not hidden
If a panel's data is from a poll that last ran >60 seconds ago, or if the WebSocket connection is broken, that must be visible. Don't show cached numbers silently. Stale data is worse than no data because it creates false confidence.

### Aggregate metrics need denominator context
A number like "3 failed tasks" or "87% review approval" is only meaningful with denominator and time window. Show: "3 failed / 18 total in the last 7 days" or "87% (72/83 tasks) approved in last 30 days". Without the denominator, an operator cannot tell whether 3 failures is normal noise or a signal to act.

---

## 6. Object Relationship Map

```
Project
├── Deploy State (orchestrator health, schema revision)
├── Resource Envelope (spend cap, concurrency)
├── Policy (autonomous mode, auto-review gates, promotion target)
├── Directions (portfolio of active work goals)
│   └── Tasks (spawned to serve a direction)
│       ├── Messages (NOTE / QUESTION / FEEDBACK / BLOCKER)
│       ├── PR (GitHub PR produced or referenced)
│       └── Wave (parent + child tasks)
├── Versions (candidate promotion SHAs with qualification state)
├── Runners (local fleet members)
│   └── Compute Jobs (hardware-bound experiment arms)
├── Knowledge Documents (findings from research + compute experiments)
└── Accounts → Providers (LLM API keys, spend, health)
```

---

## 7. Anti-patterns This Document Prohibits

These patterns have appeared in the product and should not recur.

**Dashboard as marketing copy.** A panel full of green checkmarks and upward-trending graphs that says everything is fine when a task wave just burned $40 and produced zero merged PRs is worse than no dashboard. Surfaces must show what is true, not what is reassuring.

**Settings scattered across multiple locations.** If a policy can be set from two places (e.g., "autonomous mode" in both a project header and a settings panel), operators will be confused about which is authoritative and changes will conflict. One setting, one home.

**Task detail as log dump.** Showing raw log lines as the primary task view makes the task detail drawer a debugging artifact, not a case-management surface. Diagnosis information (logs, failure reason, self-review output) belongs in a secondary tab or expandable section. The primary view should show: what did this task produce, what is the current status, and what is the next action.

**Direction portfolio without time context.** A list of directions with no indication of when each was created, last acted on, or when it expires creates a pile of stale intentions. Every direction must show its age and last-activity timestamp.

**Omitting criterion verdict from knowledge views.** A compute experiment that ran a declared success criterion must show whether it was MET, NOT MET, or UNMEASURED — not just the trustworthiness grade. Grade = can we trust this measurement. Criterion = did the thing we tried actually work. Both must be visible.

---

*Last updated: 2026-06-27*
