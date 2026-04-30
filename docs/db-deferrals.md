# Database deferrals

This document catalogs database and infrastructure items that the comprehensive
database audit (2026-04-29) identified as worth doing eventually but **deliberately
not done now**. Each entry records why we chose to defer, the concrete event that
should reopen the decision, what mitigation is currently in place, and the
estimated cost when the trigger fires.

This is the "what we knowingly didn't do, and why" reference. Treat it as a
backlog with explicit re-evaluation triggers rather than as a wishlist.

## Per-secret IAM bindings (drop project-wide `secretmanager.admin`)

**Why deferred.** The orchestrator dynamically creates per-agent credential
secrets at runtime (see `backend/app/compute.py` and `backend/app/secrets_manager.py`).
That runtime-provisioning pattern requires `roles/secretmanager.admin` at the
project level, because it both creates new secret resources and grants access to
them. Tightening to per-secret bindings is infeasible without redesigning the
secret-provisioning system itself. Audit finding H-O2 was downgraded once this
was understood (see `infra/terraform/iam.tf:16-21,28-31`).

**Trigger to revisit.** Either (a) the runtime per-agent secret-provisioning
flow is redesigned to use predeclared scopes / a fixed catalogue of secret IDs,
or (b) a compliance requirement (SOC 2 Type II, ISO 27001, HIPAA, etc.) makes
project-wide secret admin unacceptable.

**What's in place today.** The trust boundary is at the Cloud Run authentication
layer: admin tokens, JWT validation, and Firebase auth gate every endpoint that
can reach the secret-manager helpers. The compute service-account is not
exposed to user input, only to orchestrator-internal callers.

**Estimated cost when triggered.** ~3 dev-days. Requires reworking
`backend/app/compute.py` and `backend/app/secrets_manager.py` to use
predeclared secret IDs minted via Terraform with per-secret IAM, and migrating
existing dynamic secrets to the new naming scheme.

## CMEK encryption (customer-managed encryption keys)

**Why deferred.** Google-managed encryption is the default for both Cloud SQL
and AlloyDB, and meets the bar for everything short of regulated data. We have
no PII, no payment data, and no customer with a contractual CMEK requirement.
Adding CMEK now would buy nothing operationally and add KMS-key lifecycle
management to the on-call surface.

**Trigger to revisit.** First customer with a compliance requirement that
mandates customer-managed keys (typically appears in enterprise security
questionnaires under "encryption at rest with customer-controlled keys").

**What's in place today.** Google-managed encryption at rest on both Cloud SQL
and AlloyDB; TLS-in-transit enforced via `ssl_mode = ENCRYPTED_ONLY` (Cloud SQL)
and the migration cutover (AlloyDB).

**Estimated cost when triggered.** ~1 dev-day. Cloud SQL CMEK requires a
fresh instance — you cannot retrofit. Procedure: stand up a parallel
CMEK-enabled instance, dump-and-restore, cutover. Plus ongoing KMS key
rotation policy and IAM on the keyring.

## IAM auth migration (drop database password)

**Why deferred.** `cloudsql.iam_authentication = on` is enabled in Terraform
(`infra/terraform/cloud_sql.tf:91-94`) but the application still uses the
password path (`backend/app/database.py:5-27`). Switching the app over needs
short-lived-token plumbing in `database.py` and a careful staged rollout. The
password rotation cadence today is low enough that the operational pain doesn't
yet justify the engineering work.

**Trigger to revisit.** Quarterly password rotations become an operational
burden (e.g. >2 incidents/year traceable to rotation, or compliance demands
short-lived credentials).

**What's in place today.** Terraform-generated `random_password` with strong
complexity policy (`infra/terraform/cloud_sql.tf:96-102`); password stored in
Secret Manager and never logged; URL-encoded into `DATABASE_URL`; TLS
enforced server-side so credentials never traverse plaintext.

**Estimated cost when triggered.** ~2 dev-days. Need to: switch `database.py`
to fetch a short-lived IAM token via `google.auth`, refresh on connection
churn, handle token-expiry mid-pool, update `setup.sh` to grant the Cloud Run
service-account `cloudsql.instanceUser`, and run a staged rollout (canary +
revert plan).

## DATA_READ pgaudit

**Why deferred.** `pgaudit.log = "ddl,role,write"` covers schema changes, role
changes, and writes. DATA_READ adds row-level read auditing — useful for
proving non-access during an incident, but extremely log-volume heavy
(~10-100x current pgaudit output). No paying user, no audit obligation, no
incident has needed it.

**Trigger to revisit.** First paying user contract that requires read-audit
trail (typical in fintech, healthcare, or any vertical with "who looked at
what" requirements), OR an internal incident where we wished we had read
logs to disprove access.

**What's in place today.** `pgaudit.log = "ddl,role,write"` on both Cloud SQL
and AlloyDB (`cloud_sql.tf:71`, `alloydb.tf:69`); explicit decision documented
in `audit_logs.tf:8`.

**Estimated cost when triggered.** <1 dev-day. Single Terraform flag flip
(`pgaudit.log = "all"` or `"ddl,role,write,read"`). Operational cost: log
ingestion ~$5-50/month depending on traffic, plus longer log-export retention.

## ZONAL → REGIONAL Cloud SQL availability

**Why deferred.** `availability_type = ZONAL` runs a single instance in one
zone. A zone outage requires manual failover (restore-from-backup or
PITR — RTO ~30 min). REGIONAL adds a synchronous standby in another zone with
automatic failover (RTO ~1 min) but doubles the compute bill. With no SLA
committed and no paying user, the ~$30/month savings beat the marginal
availability gain.

**Trigger to revisit.** First paying user OR any uptime SLA committed in
writing (formal contract, marketing-page promise, or board-level metric).

**What's in place today.** Daily backups with 7-day retention; PITR enabled;
documented restore procedure (`docs/db-migration-plan.md` "Operational
continuity" section, RTO ~30 min from latest backup, ~2 min from PITR).

**Estimated cost when triggered.** Console flip or Terraform one-line change
(`availability_type = "REGIONAL"`); takes ~10 min downtime to switch on
existing instance. Ongoing: ~$30/month additional compute for the standby
(scales with the primary tier; multiply if you upgrade tier).

## LB-fronted Cloud Run (drop `INGRESS_TRAFFIC_ALL`)

**Why deferred.** Cloud Run is currently configured with
`ingress = INGRESS_TRAFFIC_ALL` and `member = "allUsers"` for the invoker
binding (`infra/terraform/cloud_run.tf:5,88-94`), making the orchestrator
reachable directly from the public internet. The proper fix is fronting it
with a Cloud Load Balancer + Cloud Armor and tightening ingress to
`INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER`. Doable, but adds LB cost, certs,
and routing complexity for a service whose only client today is our
own deployments + admin tooling.

**Trigger to revisit.** First production incident traceable to public ingress
(scraping, abuse, DDoS), OR introducing a second environment (staging vs
prod) where shared-internet ingress becomes a multi-tenant risk, OR any
paying user.

**What's in place today.** Application-layer authentication on every endpoint
(JWT/Firebase/admin token). No unauthenticated mutation paths. Cloud Run's
own DDoS protection and per-region quotas. The `allUsers` IAM binding only
permits *invoking* — actual access still requires a valid token.

**Estimated cost when triggered.** ~1 dev-day. Add `google_compute_global_address`,
`google_compute_managed_ssl_certificate`, `google_compute_url_map`, backend
NEG pointing at Cloud Run, swap ingress flag, optionally Cloud Armor policy.
Ongoing cost: ~$18/month forwarding rule + per-GB egress at LB rates.

## Server certificate pinning (asyncpg `ssl=True`)

**Why deferred.** `connect_args.ssl=True` (asyncpg) verifies the server's
certificate chain against the system trust store but does not pin a specific
server certificate. Pinning would catch a sophisticated MITM that compromised
a public CA — a threat model that is not on our radar for an internal-network
DB connection.

**Trigger to revisit.** Compliance requirement (some regulated environments
require explicit certificate pinning for data-in-transit), OR a credible
internal threat model that includes CA compromise.

**What's in place today.** TLS 1.2+ enforced server-side via
`ssl_mode = ENCRYPTED_ONLY` (Cloud SQL); CA-validated certs via the system
trust store; `?ssl=require` in the DATABASE_URL is the single source of
truth for the client. Both Cloud SQL and Cloud Run live in the same project
network with private IP eligibility — there's no public-internet hop for the
DB connection.

**Estimated cost when triggered.** ~1 dev-day. Pin the Google-managed
server CA fingerprint (or the Cloud SQL instance-specific cert) in
`connect_args["ssl"]` as an `ssl.SSLContext` with explicit
`load_verify_locations` and `check_hostname=True`. Need to also bake cert
rotation handling (Cloud SQL rotates server certs; we'd need to track this
or risk hard-fail on the rotation date).

## How to use this doc

- **Before opening Blueprint to external users**, walk through each section
  above and verify that the trigger has not silently fired. "First paying
  user" being plural across multiple deferrals means several of these need a
  decision in the same week. Don't wait for the incident.
- **When closing one item**, move its section into the **Done** section
  below with the date and the commit / PR that addressed it. Keep the
  original "Why deferred" text as historical context — future readers will
  want to know why we held off as well as what eventually changed.
- **When a new deferral is created**, add it here with the same five-field
  structure (Why / Trigger / In place today / Cost). A deferral that lives
  only in a code comment or a Slack message will not be re-examined.
- **Quarterly review.** Skim the trigger list once a quarter. If any trigger
  has fired and the corresponding item is still open, escalate to the next
  planning cycle.

## Bulk-action orphan sweep

**Why deferred.** `admin.bulk_action_tasks` runs in two phases: Phase 1
validates task IDs and resolves project membership in a short shared-session
transaction; Phase 2 iterates tasks one at a time, each in its own scoped
session (for retry) or with per-task commits (for approve/cancel). If Cloud
Run mid-iterates and the instance is killed (deploy, OOM, scale-down) between
Phase 1's claim and Phase 2's mutation for some task, that task stays in its
pre-bulk state with no sweep to retry it. The operator notices because the
admin UI still shows the task in its old status and can re-issue the bulk
action. For a solo-dev deployment with Cloud Run rolling deploys roughly
weekly and OOMs ~zero, the operational cost of the orphan-sweep machinery
(state table + reconciliation loop + idempotency guards) outweighs the
recovery cost.

**Trigger to revisit.** Cloud Run mid-iteration crashes are observed at
**>1 per month**, OR a paying user is impacted by a stuck bulk-action
(visible as: bulk-approve clicked, only some tasks moved to COMPLETED, the
rest still stuck in EVALUATING). Either signal warrants an explicit sweep.

**What's in place today.** Per-task commits in Phase 2 ensure partial
progress is durable; `_terminated_task_ids` cache invalidation runs on every
mutated task; the operator can re-issue the same bulk action and it
short-circuits on already-mutated tasks (idempotent for approve/cancel,
re-runs for retry which generates a fresh `vm_instance_id`). The
`bulk_action_tasks` docstring + comment block (added in Wave F4) document
this contract for future maintainers.

**Estimated cost when triggered.** ~2 dev-days. Add a `bulk_action_session`
table (id, action, task_ids JSONB, created_at, completed_at) keyed by a
fresh UUID at Phase 1 entry, mark each task complete as Phase 2 mutates it,
and add a reconciliation loop in `services.py` that re-fires the action for
sessions older than ~5 minutes with un-mutated tasks. Test plan: kill
Cloud Run mid-iteration via SIGKILL on a `pytest` simulation, assert sweep
re-fires the missed tasks within one reconciliation tick.

## Done

(Empty — items move here when addressed, with date and PR/commit reference.)
