# Local-first infrastructure cost plan

Date: 2026-07-27

## Outcome

Bring Blueprint's steady-state Google Cloud infrastructure spend below
**$200/month**, treating $200 as the desired ceiling. With Cloud SQL
deliberately retained at its current shape, the evidence-backed first milestone
is **$215-245/month**. Crossing below $200 requires a separately demonstrated
$20-45/month database saving (optimization followed by safe resizing, a
commitment, or a later migration); it cannot be promised by query optimization
alone because provisioned Cloud SQL CPU/RAM is billed while idle.

The durable design is:

- local runners are the default execution fleet;
- lack of local capacity queues work instead of silently purchasing a VM;
- cloud execution requires an explicit, budgeted override;
- the existing local-runner installer evolves into a general Blueprint node
  installer for execution, control-plane, and GitHub Actions roles;
- operator-owned hosts form one shared execution fabric for Blueprint tasks,
  compute jobs, and ephemeral GitHub Actions workers;
- the public control plane runs locally behind an authenticated tunnel or proxy,
  with Cloud Run retained only as a scale-to-zero recovery deployment;
- Cloud SQL remains managed and at its current CPU size for this milestone.

This plan addresses GCP infrastructure. Moving GitHub Actions to self-hosted
runners is a parallel savings track and is included below, but its savings do
not count toward the $200 GCP target.

## Measured baseline

Google Cloud Billing, July 1-27:

| Service | Actual net cost |
| --- | ---: |
| Compute Engine | $302.55 |
| Cloud Run | $234.47 |
| Cloud SQL | $174.54 |
| Networking | $58.25 |
| Other | $6.32 |
| **Total** | **$776.13** |

The July 1-31 forecast is **$900.72**.

Important attribution:

- Compute Engine recorded 3,234.5 VM-hours in the last 30 days, about 3,109
  agent VM-hours and 125.5 Packer VM-hours.
- Two taskless `n2-standard-2` warm-pool VMs were live during inspection.
- Two leaked Packer `e2-standard-2` builders had been running since July 24-25.
- The warm pool is enabled with target size 2, and the global cloud limit is
  10 concurrent VMs.
- Cloud Run is live at 2 vCPU / 8 GiB with min 1 and max 12. The checked-in
  Terraform expects 2 vCPU / 4 GiB and max 1, so production has drifted.
- Cloud Run request concurrency is low and request queueing is usually zero.
  Scaling appears to be driven primarily by background work, resource use, and
  revision overlap rather than HTTP demand.
- Cloud SQL is `db-custom-4-15360` (4 vCPU, about 15 GiB RAM).

## Why the existing local-runner option is insufficient

Blueprint already has meaningful local-runner controls:

- runners advertise separate `max_concurrent` and
  `max_concurrent_compute` capacities;
- runners can be bound to multiple projects;
- strict task-to-runner pins never fall back to cloud;
- `require_local_runner=true` queues `compute_experiment` jobs instead of
  provisioning cloud compute.

However, `require_local_runner` is explicitly scoped to compute experiments.
It is not a global placement policy for all agent work. Strategic also has a
separate continuous-local hold that deliberately falls back to cloud after its
timeout. Therefore, enabling the current checkbox alone cannot guarantee zero
new GCE execution cost.

## Target monthly budget

| Service | Target | Design assumption |
| --- | ---: | --- |
| Compute Engine | $0-2 | No warm pool or automatic task VMs |
| Cloud Run | $0-3 | Scale-to-zero recovery deployment only |
| Cloud SQL | $190-205 | Current July run-rate; keep capacity and optimize workload |
| Backup/object storage | $3-7 | Artifacts and database backups |
| Network, observability, other | $5-12 | Tunnel, egress, logs, DNS, storage |
| **Total** | **$198-229** | Excludes uncertain WAN DB traffic; use $215-245 operating envelope |

Cloud SQL is the dominant remaining fixed cost. This milestone drives
everything else as close to zero as practical. The budget must be refreshed
after seven days of measuring local-to-Cloud-SQL bytes, latency, and connection
behavior. Do not claim a sub-$200 total until a database saving has actually
appeared in billing.

## Phase 0: stop avoidable spend

Time box: same day.

1. Set the GCE warm-pool target to 0 and disable warm-pool replenishment.
2. Confirm ownership, then terminate leaked Packer builders and taskless
   warm-pool members.
3. Disable scheduled/automatic Packer builds. Retain a manual image build for
   deliberate releases and emergencies.
4. Set initial daily alerts consistent with the measured DB floor: target
   $7.25/day, warning at $7.50/day, and urgent at $8/day. Alert immediately and
   separately on any task VM creation or unexpected Cloud Run instance.
5. Enable detailed BigQuery billing export so service, SKU, project, and label
   costs can be compared daily rather than reconstructed from the reports UI.
6. Reconcile live Cloud Run configuration with source control. Do not merely
   force max instances to 1 yet: observed per-instance memory has reached 100%,
   so that change needs the process split in Phase 2.

Expected result: GCE's recurring idle floor disappears immediately. This phase
does not yet guarantee zero task VM launches.

Rollback: re-enable a single warm-pool member only if measured local-runner
startup latency blocks urgent work. Do not restore target 2 by default.

### Phase 0 release gate

The application-level `managed_compute_enabled` switch cannot stop external
Packer, Terraform, or GitHub Actions builders. Before it is set to `false`, an
operator must record all of the following in the rollout ticket: the disabled
scheduler/workflow names, the time of the last intentional builder, the warm
pool target of zero, and a post-change `gcloud compute instances list` snapshot
showing no Blueprint Packer or taskless-pool VM. Re-check the Cloud Audit Log
for unexpected Compute Engine instance inserts during the following hour. Do
not call the cost cutover complete from the application toggle alone.

## Phase 1: make cloud placement explicit

Time box: 2-4 engineering days.

### 1. Replace `require_local_runner` with one authoritative placement policy

Do not broaden the meaning of `require_local_runner` in place. Its current
schema, API documentation, UI copy, tests, and runtime behavior all promise a
compute-experiment-only policy. Reusing the same boolean for every kind of task
would make mixed-version deployments ambiguous.

Add a project resource-envelope field:

```text
execution_placement_policy =
  local_only       # queue; never purchase cloud compute
  local_preferred  # bounded local wait, then cloud if within budget
  cloud_allowed    # current mixed behavior
```

Migration and compatibility rules:

- when `execution_placement_policy` is set, it is authoritative for every task
  type;
- when it is absent and `require_local_runner=true`, preserve the old
  compute-experiment-only behavior during one compatibility release;
- when both are absent, preserve today's `cloud_allowed` behavior;
- reject conflicting writes that set both fields;
- have the API return a deprecation warning/source marker when behavior came
  from `require_local_runner`;
- update the admin UI to show the three-state placement policy and label the
  old field as deprecated during the compatibility release;
- migrate active projects deliberately, then remove `require_local_runner`,
  its resolver, old UI control, and compatibility tests in the following
  release.

Set active Blueprint projects to `local_only` only after their runners pass the
capacity acceptance checks below. The data migration must not silently convert
`require_local_runner=true` into global `local_only`, because that would expand
the policy from compute experiments to all task types without an operator
decision.

The policy must cover:

- ordinary authoring/verification agent tasks;
- Strategic/Resident work;
- compute experiments;
- retries, recovery, and stale-pin self-healing paths;
- direct compute-job API launches.

Every cloud provisioning chokepoint must receive the resolved policy. No
subsystem may independently reinterpret “local”.

### 2. Add an independent global managed-compute kill switch

Add `managed_compute_enabled=false` as the emergency cost boundary. It covers
every billable execution backend—GCE task VMs, agent Cloud Run Jobs, and direct
cloud compute-job backends—while separately allowing the dormant Cloud Run web
recovery service. It must:

- reject or queue work before committing a task to PROVISIONING;
- check again inside every side-effecting managed launch primitive;
- leave existing VMs visible and terminable;
- allow a time-limited override with reason, project, backend/machine class,
  maximum launches/runtime, and expiry;
- snapshot the resolved decision and policy source onto the task;
- emit an audit event whenever an override is used.

Do not use `max_concurrent_vms=0` as the long-term semantic control unless its
behavior is made explicit and tested. A capacity value and a placement policy
are different concepts.

The first implementation test mocks every managed launch primitive and proves
zero calls occur under the kill switch across ordinary, Strategic, compute,
retry/recovery, and direct-API routes. Billing alerts are delayed detection,
not enforcement.

### 3. Queue local-only work instead of failing after six hours

For `local_only`, replace the current six-hour terminal failure with a durable
capacity wait:

- task remains queued/blocked with a specific reason;
- alert once, then update rather than duplicate;
- optional per-task deadline can still fail intentionally;
- runner arrival or freed capacity wakes the task immediately.

### 4. Turn each host into a multi-tenant execution node

Evolve the current `blueprint-runner` installation path into a general
`blueprint-node` lifecycle:

```text
blueprint-node install --system \
  --roles executor,github-actions,control-plane \
  --channel stable

blueprint-node status
blueprint-node update
blueprint-node drain
blueprint-node uninstall
```

This should reuse the machinery already present in `blueprint-runner install`:
rendered systemd user/system units, 0600 environment files, persisted
enrollment credentials, restart supervision, prerequisite checks, resource
flags, and prerequisite checks. Keep `blueprint-runner` as a backward-compatible
facade during adoption.

Roles are separately supervised sibling services:

- `blueprint-node-agent.service`: enrollment, host inventory, capacity ledger,
  health, update coordination, and draining;
- `blueprint-executor.service`: Blueprint agent and compute lanes;
- `blueprint-actions.service`: ephemeral GitHub Actions runner lifecycle;
- `blueprint-api.service`: local HTTP API;
- `blueprint-worker.service`: scheduler, dispatch, reapers, and background loops;
- `blueprint-edge.service`: authenticated public tunnel/reverse proxy.

Do not run the API or worker as children of `local_runner.py`. They should share
installation, configuration, telemetry, and upgrades while retaining separate
failure and restart boundaries. A broken execution lane must not restart the
control plane.

Do not extend the current in-place Git self-update across all services.
Multi-service updates require versioned release directories, an atomic active
symlink, a signed manifest, a protocol/schema compatibility matrix,
drain-before-update, ordered restarts, health gates, and automatic rollback.
Every role reports its artifact and protocol version.

Use separate Unix users, directories, credentials, and 0600 environment files
for executor, Actions, API/worker, and edge roles. The node agent coordinates
lifecycle but does not receive every role's secrets.

Installation profiles keep the common paths simple:

```text
blueprint-node install --profile primary
# node-agent + executor + actions + api + worker + edge

blueprint-node install --profile worker
# node-agent + executor + actions

blueprint-node install --profile gpu-worker
# node-agent + executor, GPU-oriented defaults
```

The admin fleet UI should show all roles and versions under one physical node,
permit role enable/disable and drain, and report one combined resource view.
Introduce a first-class physical node identity whose lifecycle is independent
of any enrollment project. Executor registrations and project grants attach to
that node; deleting a project must not delete the host identity or implicitly
change its authorization.

Use the existing per-runner `max_concurrent` and `max_concurrent_compute`
settings, but stop treating a runner daemon as one execution slot. A host
should advertise resources and the control plane should admit work against
actual CPU, RAM, disk, GPU, and current pressure.

Each concurrent lane needs:

- its own workspace/worktree;
- unique container names, ports, and temporary paths;
- CPU, RAM, disk, and GPU limits;
- cleanup and stale-lane recovery;
- accurate heartbeat accounting.

Use a host capacity manager with adapters for both Blueprint and GitHub:

- one resource ledger and admission queue per physical host;
- the node agent is the sole admission authority;
- every Blueprint claim and ephemeral Actions runner launch atomically acquires
  a resource-vector lease with idempotency key, owner, and TTL before starting;
- leases reconcile against live processes after node-agent restart;
- a rejected or expired Blueprint reservation is durably requeued rather than
  left pinned;
- cgroup/container CPU and memory limits;
- GPU assignment by device/index and optional VRAM reservation;
- weighted priorities so production control-plane work cannot be starved by CI;
- ephemeral execution containers with shared read-only caches for git,
  packages, Docker layers, and model artifacts;
- preemption/draining for shared DGX and lab-server availability windows;
- a heartbeat that reports total, reserved, and free resources rather than
  only a manually selected slot count.

Reserve control-plane CPU, RAM, disk, file descriptors, and I/O outside the
allocatable ledger. Run API/worker/edge in protected systemd slices; weighted
job priority alone cannot protect services from disk or memory exhaustion.

Initial capacity should be intentionally useful, then automatically constrained
by resource pressure:

| Host | Workload mix | Starting capacity |
| --- | --- | ---: |
| Personal RTX 5080 Linux server | Control plane, agents, trusted CI, one GPU job | 6 agent/trusted-CI lanes plus 1 GPU lane |
| Lab server | CPU agents, tests, builds, overflow CI | 8 lanes, or `floor((RAM - 16 GiB) / 6 GiB)`, whichever is lower |
| DGX-1 | Parallel GPU experiments and GPU CI | Up to one lane per free GPU, bounded by shared-use policy |

These are starting admission limits, not architectural maxima. After a 24-hour
pressure test, raise CPU lanes until either sustained load exceeds 80%, memory
headroom falls below 15%, or disk/cache contention materially increases task
latency. GPU jobs should schedule from actual free devices/VRAM, not a fixed
one-job-per-host rule.

Shared machines remain opt-in and drainable. The personal Linux server carries
the always-on control plane; loss of the shared hosts reduces throughput but
does not cause cloud spending.

General pull-request Actions must not run beside production API/worker/tunnel
credentials under the current best-effort container isolation. Put untrusted CI
on the lab server or a dedicated worker with VM-grade or rigorously rootless
isolation, no shared Docker socket, and no production environment mounts.
The primary profile enables only trusted Actions by default. Deploy execution
uses a distinct Unix user, credential set, and allowed node class.

Acceptance:

- 72 hours with active work and zero new task-owned GCE instances;
- local runner queue wait p95 under 10 minutes when at least one compatible host
  is online;
- no workspace/container collision across concurrent lanes;
- at least 12 simultaneous CPU lanes across the personal and lab hosts during
  a synthetic mixed Blueprint/Actions load test;
- killing a runner leaves tasks recoverable and does not trigger cloud;
- all placement decisions appear in one operator-visible audit stream.

Rollback: change an individual project to `local_preferred` with a small,
expiring override. Do not globally re-enable automatic cloud fallback.

## Phase 2: move the control plane local; make Cloud Run recovery-only

Time box: 4-7 engineering days.

The current Cloud Run service mixes HTTP handling with continuous scheduler,
dispatch, reaper, and other background loops. Instance-based billing is
therefore doing real work, but it is an expensive shape for a mostly quiet API.

### Process split

Introduce an explicit process role:

```text
BLUEPRINT_PROCESS_ROLE=api | worker | all
```

- `api`: HTTP endpoints and short request-scoped work only.
- `worker`: scheduler, orchestrator, dispatch, reapers, reconciliation, and
  other continuous loops.
- `all`: temporary rollback-compatible current behavior.

Run both roles on the personal Linux server under systemd or a local container
supervisor. Publish only the API through an authenticated tunnel/reverse proxy;
the worker stays private. Use lease/fencing semantics already present in
Blueprint so only one active worker owns a loop.

The first local control-plane activation must set
`BLUEPRINT_DEPLOYMENT_MODE=local-production`, provide a valid
`GITHUB_PAT_ENCRYPTION_KEY`, and omit `ALLOW_PLAINTEXT_CREDENTIALS`. The
runtime rejects plaintext mode in that explicit production scope. Keep
`BLUEPRINT_PROCESS_ROLE` unset (or `all`) until the API/worker ownership split
has an implementation and an activation review; the current role foundation is
deliberately fail-closed for `api` and `worker`.

Keep a request-billed Cloud Run `all` recovery revision at min instances 0, or
paired promotable `api` and `worker` services. An API-only deployment does not
restore scheduler, dispatch, reaper, and reconciliation work. Recovery must
connect to Cloud SQL without depending on the failed local machine and should
not require an always-on VPC connector solely for standby.

### Database connectivity

Cloud SQL is currently private-IP-only, so the local worker cannot simply be
moved off GCP. Before moving either process, write a connectivity ADR based on
seven days of measured database bytes and round trips, comparing monthly cost,
latency, IAM/certificate rotation, and failure behavior. Choose one secure path:

1. Cloud VPN/private routing, preferred if the network can be maintained;
2. Cloud SQL Auth Proxy with IAM and a tightly controlled reachable endpoint;
3. a small authenticated bridge only if the first two are impractical.

Never expose PostgreSQL directly to the public internet with password-only
access. Inventory every lifespan task as API-per-instance, singleton-worker, or
optional cloud-maintenance work; `api|worker` needs an explicit startup table
and test matrix, not just one environment-variable branch.

Acceptance:

- 7 days with exactly one active owner for each background loop;
- no duplicate dispatch, reaping, or scheduler actions during failover;
- Cloud Run request p95 and error rate remain at or better than baseline;
- Cloud Run normally runs zero instances and incurs less than $0.20/day;
- external probes reach the local API through the authenticated edge;
- a documented drill promotes the Cloud Run recovery endpoint within 15
  minutes;
- recovery can be initiated from a second trusted device after total loss of
  the personal server or its internet connection;
- promotion and demotion cannot leave local and Cloud Run workers jointly
  owning a singleton loop;
- worker restart recovery succeeds without manual database repair;
- the local server can be patched/rebooted without database corruption.

Rollback: deploy the `all` role revision and stop the local worker. Keep this
path tested until the split has been stable for 30 days.

## Phase 3: make GitHub Actions another workload on the local fleet

Time box: 1-3 days for the first runner, then migrate incrementally.

July had 940 workflow runs and no self-hosted GitHub runner. Take a two-step
path so savings are not blocked on the unified scheduler.

First, install the official self-hosted Actions runner as a separately
supervised `blueprint-node` role on the lab server and migrate high-volume
trusted workflows through the separately managed workflow path. This repo does
not modify `.github/workflows/`. Validate cancellation cleanup, credential
isolation, and cache boundaries immediately.

Then extend the host capacity manager with a GitHub Actions adapter:

1. accept a GitHub job reservation from a trusted webhook/poller;
2. reserve resources in the same ledger used by Blueprint tasks;
3. create an ephemeral, single-job GitHub runner with the required labels;
4. run it inside the same isolated lane/container machinery;
5. deregister and destroy it after the job;
6. expose queue depth and resource use in the existing runner/fleet UI.

Route high-volume CI, test, validation, and image-build jobs first. Trusted
deploy jobs use a separate runner identity, credential boundary, and allowed
labels, but still share the host scheduler. GitHub-hosted execution remains a
manual workflow choice rather than automatic fallback.

GitHub still assigns jobs using `runs-on` labels and runner/JIT registration;
the adapter cannot independently reserve arbitrary GitHub jobs. Do not place
untrusted pull-request code on a persistent runner with production credentials.
Separate trusted deploy runners from general CI runners.

Acceptance:

- at least 80% of billable Actions minutes are self-hosted;
- failed/cancelled jobs leave no workspace or credential residue;
- one week of successful CI before moving deploy credentials;
- GitHub Actions billed run-rate falls below $50/month, then target $10-25.

This is operationally straightforward; hardening isolation and secrets is the
part that deserves care.

## Phase 4: optimize Cloud SQL without reducing capacity

Cloud SQL is not the first lever. Last-30-day telemetry:

| Metric (hourly max series) | p95 | p99 | Maximum |
| --- | ---: | ---: | ---: |
| CPU | 47.24% | 75.00% | 99.49% |
| Memory | 46.96% | 49.16% | 53.78% |
| Connections | 67 | 92 | 121 |
| Waiting backends | 36 | 51 | 87 |

The last seven days are calmer, but a direct move from 4 to 2 vCPU would make
recent p95 CPU roughly 76% and p99 roughly 94% under a simple scaling estimate.
That is not adequate safety margin.

Keep the current 4-vCPU/15-GiB shape for this milestone. Optimize the workload,
not the safety margin:

1. enable/query `pg_stat_statements` and identify the SQL responsible for CPU,
   connection, and waiting-backend spikes;
2. correlate spikes with scheduler/worker loops and remove redundant polling;
3. inventory every periodic loop and record its query frequency, batch size,
   lock behavior, and whether it runs on every process or only the elected
   leader;
4. remove N+1 queries, add indexes only from measured query plans, and convert
   polling to notifications/event wakes where practical;
5. right-size application connection pools for the local API and worker process
   counts; use a pooler only if the measured connection lifecycle warrants it;
6. add dashboards for top query total time, calls, mean/p99 latency, lock waits,
   deadlocks, connections by application name, buffer-cache hit ratio, and
   temporary bytes;
7. load-test the control-plane split against production-like data and compare
   database load before/after;
8. keep a two-week post-change trace so a later database migration or safe
   sizing decision is based on representative evidence.

Acceptance:

- waiting-backend p99 falls below 10;
- total database CPU time per completed Blueprint task falls by at least 30%;
- no regression in API latency, dispatch latency, or task correctness;
- every continuous loop is leader-fenced and has an explicit polling/event
  budget;
- connection count remains bounded through local node restarts and burst load.

A local PostgreSQL migration belongs in a separate plan after this milestone.
Its prerequisites are dedicated storage, off-site backups, restore/failover
drills, and capacity at least equal to the current managed instance. Do not
downsize Cloud SQL merely to make the bill fit.

## Delivery sequence

1. **Cost stop:** warm pool 0, remove leaks, suspend automatic Packer, billing
   export and alerts.
2. **Hard cost boundary:** global managed-compute kill switch plus exhaustive
   launch-primitive tests.
3. **Placement contract:** task-wide policy, audited decision snapshot, and one
   durable local-capacity wait state.
4. **Current runner throughput:** raise concurrency and repair real isolation
   collisions before designing a new scheduler.
5. **Fast Actions saving:** official self-hosted runner role for trusted,
   high-volume workflows.
6. **Blueprint node:** additive profiles, role-specific supervision/secrets,
   atomic releases, and rollback.
7. **Shared local fabric:** atomic host resource leases plus ephemeral Actions
   adapter; mixed pressure soak at 12+ CPU lanes.
8. **GCE proof:** 72 active hours with zero managed execution launches.
9. **Connectivity and lifecycle inventory:** Cloud SQL ADR, ingress/callback
   proof, and classification/fencing of every background loop.
10. **Local control plane:** worker first, then API/edge; retain complete
    scale-to-zero Cloud Run recovery.
11. **Recovery proof:** seven stable local days plus promotion and demotion
    drills from a second device.
12. **Database optimization:** profile and reduce query/wait load while
    retaining current Cloud SQL capacity.

Do not combine the placement-policy rollout and Cloud Run process split in one
release. They have independent rollback boundaries.

The “engineering day” labels above are human-planning estimates. Agent-assisted
implementation should be scoped as small, independently reviewable PR slices;
most code slices should fit one focused agent run plus review. Host enrollment,
72-hour/seven-day soaks, DNS/network validation, and billing confirmation are
real elapsed-time gates and cannot be compressed by parallel coding.

## Weekly scorecard

Review every seven days until the target holds for a full billing month:

| Metric | Goal |
| --- | --- |
| GCP trailing 7-day daily average | Initially <= $7.25/day; <= $6.60 only after measured DB saving |
| GCE task VM creations | 0 |
| GCE cost | <= $0.33/day |
| Cloud Run cost | <= $0.20/day |
| Cloud SQL CPU p99 hourly max | Report, no premature target |
| Local execution capacity | >= 12 CPU lanes when both primary hosts are available |
| Compatible local queue wait p95 | < 10 minutes |
| Cloud override events | 0 normally; every event reviewed |
| GitHub-hosted billable minutes | Falling weekly; >=80% displaced |

The no-database-change milestone is complete when GCE and Cloud Run are
effectively zero, managed execution cannot launch silently, Cloud SQL remains
healthy, and Blueprint plus GitHub Actions share local capacity without
resource collisions. The sub-$200 milestone additionally requires a measured
database saving; it is not achieved by redefining the budget.
