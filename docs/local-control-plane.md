# Local control plane

The `local-control-plane` profile moves Blueprint's database, public API, and
leader-only background work onto one trusted Linux server. It installs five
roles:

- an unpublished PostgreSQL 15 container;
- a twice-daily off-host PostgreSQL backup timer;
- an API container published only on host loopback;
- a background worker with no published port; and
- one exact Tailscale Funnel route from public HTTPS to the loopback API.

The API and worker use the same reviewed immutable backend image but have
different process roles, environment allowlists, and Google identities. The
API is categorically ineligible for advisory-lock leadership. The worker owns
dispatch, compute lifecycle, reapers, and maintenance after cutover.

## Trust boundaries

The host contains production database data and production credentials. A
GitHub Actions runner with access to the same host Docker daemon is effectively
root-capable and can inspect containers and mounted credentials. Run Actions
inside a KVM-backed VM with its own kernel and Docker daemon, or on another
physical machine. A second Unix account, a normal container, or an LXD system
container is not an adequate boundary.

The worker and API preflights fail when they detect a host-level Actions runner.
Before copying credentials to the server, confirm this produces no output:

```bash
pgrep -af 'Runner.Listener|Runner.Worker|actions-runner'
```

Use separate owner-only files for every identity and secret class:

| File | Identity/content | Runtime reach |
| --- | --- | --- |
| API Google credential | `blueprint-local-api-runtime@PROJECT.iam.gserviceaccount.com` | API only |
| Worker Google credential | `blueprint-local-worker-runtime@PROJECT.iam.gserviceaccount.com` | worker only |
| Backup Google credential | `blueprint-local-db-backup@RECOVERY_PROJECT.iam.gserviceaccount.com` | backup timer only |
| API JSON bundle | HTTP/auth, GitHub OAuth/App/webhook, metrics/admin, storage, callback URL, and CORS values | API only |
| Worker JSON bundle | background GitHub App secrets and compute/runtime configuration | worker only |
| PostgreSQL password file | raw password for database `blueprint` and user `blueprint` | database, API/worker connection URLs, and backup |

Terraform creates the service accounts and IAM bindings, but deliberately
creates no service-account keys and stores no private key material in Terraform
state. Provision each external key out of band, copy it directly to its final
owner-only path, set mode `0600`, and never reuse the worker key for the API.

The API identity can manage objects in the artifact bucket, manage only
`user-*` Secret Manager values after creation, read operational logs and the
serving revision, and call IAM Credentials `signBlob` only on itself. It cannot
manage Compute Engine, submit Cloud Build or Cloud Run Jobs, attach service
accounts, mutate Cloud Run services, or impersonate the worker.

The worker identity has the compute/build/job and managed-secret permissions
needed by leader-only paths. It cannot sign API URLs or impersonate the API.
The backup writer can create backup objects but cannot read or delete them.

## API and worker bundles

The API JSON file must contain:

```text
DATABASE_URL
AGENT_JWT_SECRET
WS_TICKET_SECRET
GITHUB_APP_ID
GITHUB_CLIENT_ID
GITHUB_CLIENT_SECRET
GITHUB_PRIVATE_KEY
GITHUB_WEBHOOK_SECRET
GITHUB_PAT_ENCRYPTION_KEY
ADMIN_API_KEY
METRICS_TOKEN
GCP_PROJECT_ID
ARTIFACT_BUCKET_NAME
ORCHESTRATOR_URL
ALLOWED_ORIGINS
```

`INITIAL_ADMIN_EMAIL`, `ADMIN_JWT_SECRET`, `ADMIN_JWT_EXPIRE_MINUTES`, and
`ADMIN_API_KEY_ALLOWED_IPS` are optional API-only values. The API launcher
rejects worker-only keys such as `MAX_CONCURRENT_VMS`, `VPC_NETWORK`,
`AGENT_SERVICE_ACCOUNT_EMAIL`, and `CLOUD_RUN_JOB_NAME`.

The worker bundle contains the background GitHub App secrets plus the complete
compute configuration documented by `scripts/local_worker_service.py`. GitHub
OAuth client credentials, webhook/admin/metrics secrets, and CORS configuration
must not be copied into it.

The API/worker bundles and their Google credential files must be absolute,
regular non-symlink files owned by the non-root service user with mode `0600`.
The PostgreSQL password and backup-writer credential are consumed only by the
root-run database/backup units and must instead be root-owned regular
non-symlink files with mode `0600`.

## Funnel, callbacks, and browser origin

Tailscale Funnel is the only public listener. The controller owns exactly one
HTTPS listener on port 443 and one `/` proxy to `127.0.0.1:API_PORT`. It refuses
to start or stop when any foreign Serve/Funnel route exists; it never uses a
node-wide reset. Dedicate this Tailscale node's Funnel state to Blueprint.

The edge unit runs as the same non-root service account that invokes the
installer through `sudo`. Authorize that exact account as Tailscale's operator
before installation (replace `blueprint-control` if using another dedicated
account), then preflight Funnel access as that account:

```bash
sudo tailscale set --operator=blueprint-control
sudo -u blueprint-control tailscale funnel status --json
```

The installer starts the edge unit fail-closed, so a missing operator grant
causes the profile transaction to roll back instead of silently omitting the
public route. Do not run the edge unit as root.

Funnel supplies public TLS and routing, not application authentication, a WAF,
or host isolation. Keep the API container bound to `127.0.0.1`, retain
Blueprint's authentication checks, and do not publish the Docker port on a LAN
address.

Before cutover, obtain the exact Funnel hostname and configure all three
surfaces consistently:

- `ORCHESTRATOR_URL=https://HOSTNAME` in both API and worker bundles;
- GitHub App and OAuth callback URLs using that same HTTPS hostname; and
- `ALLOWED_ORIGINS` containing only the actual browser UI origins, never `*`.

The callback URLs are operator-managed GitHub settings. Starting Funnel does
not update them. Verify OAuth login, GitHub App installation, webhook delivery,
WebSocket upgrade, and browser CORS from the real UI origin before removing
Cloud Run traffic.

## Recovery prerequisites

Do not make local PostgreSQL authoritative until all of these are true:

1. `infra/recovery` is applied in a separate recovery project with its own
   state, CMEK, versioned bucket, retention policy, and independent custodians.
2. The dedicated local backup writer can create objects only in that recovery
   bucket. The local server cannot read or delete retained backups.
3. A manual backup succeeds and produces both
   `local-postgres-exports/*.sql.gz` and its `.manifest.json` completion marker.
   The manifest SHA-256 and byte count must match the archive.
4. The restore verifier is configured to accept the local archive prefix and
   requires a valid completion manifest; an archive without its manifest is
   incomplete, even if the object exists.
5. The recovery Cloud SQL service identity can read the retained archive, and
   the recovery-only verifier identity has the documented Cloud SQL lifecycle,
   private-network, evidence-write, logging, archive-read, and CMEK permissions.
6. A complete independent restore drill imports the newest local archive,
   verifies the expected Alembic revision and aggregate checks, records
   evidence, and deletes the temporary recovery instance.
7. An external TTL janitor exists for abandoned restore-drill instances; the
   verifier's normal `finally` cleanup is not sufficient protection from a
   killed job or platform outage.

Retention locking is irreversible. Perform and review a real restore drill
before setting `lock_retention_policy=true`.

## Install

Build one reviewed backend image and record its immutable local image ID. Also
pin PostgreSQL 15 and the Cloud SDK uploader by immutable local image ID:

```bash
docker build --target app -t blueprint-local-control-plane:candidate backend
docker image inspect --format '{{.Id}}' blueprint-local-control-plane:candidate
docker image inspect --format '{{.Id}}' postgres:15
docker image inspect --format '{{.Id}}' google/cloud-sdk:PINNED
```

Dry-run the complete system profile first:

```bash
sudo python3 scripts/blueprint_node.py install \
  --profile local-control-plane \
  --system \
  --dry-run \
  --postgres-image-id sha256:POSTGRES_IMAGE_ID \
  --postgres-data-dir /var/lib/blueprint/postgres \
  --postgres-secrets-file /etc/blueprint/postgres-password \
  --postgres-backup-project-id RECOVERY_PROJECT \
  --postgres-backup-bucket gs://RECOVERY_BUCKET \
  --postgres-backup-credentials-file /etc/blueprint/backup-writer.json \
  --postgres-backup-image-id sha256:CLOUD_SDK_IMAGE_ID \
  --api-image-id sha256:APP_IMAGE_ID \
  --api-secrets-file /etc/blueprint/api.json \
  --api-google-credentials-file /etc/blueprint/api-runtime.json \
  --api-port 8080 \
  --worker-image-id sha256:APP_IMAGE_ID \
  --worker-secrets-file /etc/blueprint/worker.json \
  --worker-google-credentials-file /etc/blueprint/worker-runtime.json \
  --tailscale-binary /usr/bin/tailscale
```

After reviewing the rendered paths and prerequisites, repeat without
`--dry-run`. The installer validates paths and credentials, creates the private
Docker network and PostgreSQL data directory, renders systemd units, and rolls
back unit changes if a role fails to become active.

Verify every role and the externally visible API:

```bash
sudo python3 scripts/blueprint_node.py status \
  --profile local-control-plane --system
sudo systemctl status \
  blueprint-database.service \
  blueprint-database-backup.timer \
  blueprint-api.service \
  blueprint-worker.service \
  blueprint-edge.service
sudo systemctl start blueprint-database-backup.service
sudo journalctl -u blueprint-database-backup.service --since today
tailscale funnel status --json
curl -fsS https://HOSTNAME/health
```

## Controlled container replacement

The launchers intentionally refuse to stop or adopt a container whose image,
mounts, network, credentials, or resource policy differ from the installed
environment. Therefore do not overwrite the environment first when rotating an
image or changing container configuration: `ExecStop` will compare the running
container with the new values and fail closed.

During a maintenance window, stop the dependent units while the old
environment is still installed:

```bash
sudo systemctl stop \
  blueprint-edge.service \
  blueprint-worker.service \
  blueprint-api.service \
  blueprint-database-backup.timer \
  blueprint-database.service
```

Inspect the stopped container and confirm its Blueprint ownership label and
expected data mount before removing the container object. Never remove the
PostgreSQL data directory:

```bash
sudo docker container inspect blueprint-local-postgres
sudo docker container rm blueprint-local-postgres
```

Then rerun `blueprint_node.py install --profile local-control-plane --system`
with the new immutable image IDs and the complete reviewed argument set. The
installer creates a replacement container around the existing validated data
directory and starts the roles in dependency order. Take and verify an off-host
backup first. A PostgreSQL major-version change is a database migration, not an
image rotation; use a separately reviewed dump/restore or `pg_upgrade`
procedure.

## Cutover and rollback

Stage the services while Cloud Run is still authoritative, but do not permit
both databases to accept independent production writes.

1. Stop writes or enter a maintenance window. Confirm the API, workers, and
   operators can no longer commit production writes to Cloud SQL.
2. While writes remain frozen, trigger a final full Cloud SQL logical export
   and wait for it to complete successfully. Record its object generation,
   timestamp, checksum, and source Alembic revision.
3. Restore that exact final artifact into local PostgreSQL. Before enabling any
   local writer, verify the checksum, Alembic revision, critical table row
   counts, and representative aggregates against the frozen Cloud SQL source.
   Do not use an earlier staged export or an undocumented "final delta": this
   deployment has no CDC/delta mechanism.
4. Start the local API and worker. Confirm exactly one process owns advisory
   leadership and that dispatch/reaper heartbeats are current.
5. Verify Funnel health, authentication, GitHub callbacks/webhooks, WebSockets,
   artifact signed URLs, user-secret storage, and a representative local-runner
   task.
6. Move production DNS/callback/operator traffic to Funnel, then keep Cloud Run
   available but non-authoritative during the observation window.

The rollback boundary is the first production write committed to local
PostgreSQL. Before that point, returning traffic to Cloud Run and Cloud SQL is
straightforward. After that point, a direct rollback to the old Cloud SQL
database discards every local-only write and is therefore not a safe rollback.
Failback then requires a reviewed reverse migration or restore into Cloud SQL,
a new write freeze, reconciliation evidence, and an explicit operator decision.
Do not describe “restart Cloud Run” as a complete rollback after local writes
begin.

For a local service failure that does not require database failback, stop only
the affected role:

```bash
sudo python3 scripts/blueprint_node.py stop --role edge --system
sudo python3 scripts/blueprint_node.py stop --role worker --system
```

Stopping `edge` removes only the exact Blueprint Funnel route. It refuses to
alter foreign Funnel state. Stopping `worker` interrupts active background work;
it is not a drain operation.
