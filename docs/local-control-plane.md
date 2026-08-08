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

`INITIAL_ADMIN_EMAIL`, `ADMIN_JWT_SECRET`, `ADMIN_JWT_EXPIRE_MINUTES`,
`ADMIN_API_KEY_ALLOWED_IPS`, and `CALL_FOREGROUND_QUALIFICATION` (the typed-Fast
Resident attestation; the backend fail-closes unless it exactly matches the
pinned qualification string) are optional API-only values. The API launcher
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
   state, CMEK, versioned bucket, retention policy, and an explicit recovery
   administrator whose credentials are never installed on hamsterwheel.
2. The dedicated local backup writer can create objects only in that recovery
   bucket. The local server cannot read or delete retained backups.
3. Before cutover, a fresh live-source rehearsal export succeeds and produces both
   `local-postgres-exports/*.sql.gz` and its `.manifest.json` completion marker.
   The manifest SHA-256 and byte count must match the archive.
4. The restore verifier is configured to accept the local archive prefix and
   requires a valid completion manifest; an archive without its manifest is
   incomplete, even if the object exists.
5. The recovery Cloud SQL service identity can read the retained archive, and
   the recovery-only verifier identity has the documented Cloud SQL lifecycle,
   private-network, evidence-write, logging, archive-read, and CMEK permissions.
6. A complete independent restore drill imports the newest rehearsal archive,
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

Keep authoritative PostgreSQL data off the small root filesystem. On
hamsterwheel, prepare a root-owned directory on the large `/srv/data`
filesystem and expose it at the installer-trusted `/srv/blueprint` mountpoint.
Every source and target ancestor must be root-owned and not group/world
writable; do not bind-mount a directory whose parent remains user-writable.
Record the bind mount in `/etc/fstab`, mount it, and prove both the filesystem
and free-space result survive a reboot before import.

Dry-run the complete system profile first. The measured Cloud SQL workload
requires the local database defaults shown below: 4 CPUs, 12 GiB RAM, 200
connections, and at least 20 GiB free on the data filesystem.

```bash
sudo python3 scripts/blueprint_node.py install \
  --profile local-control-plane \
  --system \
  --no-start \
  --dry-run \
  --postgres-image-id sha256:POSTGRES_IMAGE_ID \
  --postgres-data-dir /srv/blueprint/postgres \
  --postgres-secrets-file /etc/blueprint/postgres-password \
  --postgres-cpu-limit 4 \
  --postgres-memory-bytes 12884901888 \
  --postgres-max-connections 200 \
  --postgres-min-free-disk-bytes 21474836480 \
  --postgres-pgdata /var/lib/postgresql/data \
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
`--dry-run` but keep `--no-start`. The installer validates paths and
credentials, creates the PostgreSQL data directory, installs a
content-addressed copy of every root-run helper beneath
`/usr/local/lib/blueprint-node`, renders systemd units, and leaves every unit
disabled and inactive. This is the only safe initial install while Cloud SQL is
authoritative. Do not omit `--no-start` during staging.

After the recovery stack and credentials are proven, start only PostgreSQL:

```bash
sudo python3 scripts/blueprint_node.py start --role database --system
sudo systemctl is-active blueprint-database.service
sudo docker inspect blueprint-local-postgres
```

Do not start the API, worker, edge, or backup timer yet. Their activation order
is part of the cutover below. After activation, verify every role and the
externally visible API:

### Disposable pre-cutover restore rehearsal

Before the production write fence, rehearse the direct PostgreSQL path without
putting production data into `/srv/blueprint/postgres`. Create separate
root-only evidence and disposable-work directories:

On an authenticated operator workstation (not hamsterwheel), capture the
current Cloud SQL instance identity and copy it over Tailscale:

```bash
umask 077
gcloud sql instances describe blueprint-cloudsql-8cee8173 \
  --project=blueprint-orchestrator --format=json \
  > cloud-sql-instance.json
scp cloud-sql-instance.json hamsterwheel:/tmp/blueprint-cloud-sql-instance.json
rm -- cloud-sql-instance.json
```

Then, in one hamsterwheel shell, install that evidence, bind the exact
owner-matched, non-group/world-writable proxy process, and materialize the
existing Cloud SQL password without putting it in argv or shell history:

```bash
sudo install -d -m 0700 -o root -g root \
  /var/lib/blueprint/precutover-evidence \
  /var/lib/blueprint/precutover-rehearsals
sudo install -m 0600 -o root -g root \
  /tmp/blueprint-cloud-sql-instance.json \
  /var/lib/blueprint/precutover-evidence/cloud-sql-instance.json
rm -- /tmp/blueprint-cloud-sql-instance.json
DOCKER_BRIDGE_GATEWAY="$(docker network inspect \
  --format '{{(index .IPAM.Config 0).Gateway}}' \
  blueprint-control-plane)"
PROXY_PID="$(systemctl --user show blueprint-database-proxy.service \
  --property=MainPID --value)"
PROXY_EXE="$(sudo readlink -f "/proc/$PROXY_PID/exe")"
PROXY_SHA256="$(sudo sha256sum "$PROXY_EXE" | awk '{print $1}')"
sudo bash -c 'umask 077; read -r -s -p "Cloud SQL password: " value; printf "\n"; printf "%s" "$value" > /root/blueprint-cloudsql-rehearsal-password'
```

Run the content-addressed rehearsal helper installed with the database service.
The source remains writable. The helper opens one serializable, read-only,
deferrable transaction, exports its snapshot, and uses that exact snapshot for
both the source inventory queries and `pg_dump --snapshot`. The restored target
must exactly match that inventory; the custom archive still does not claim to
be the final cutover artifact:

```bash
sudo python3 /usr/local/lib/blueprint-node/HELPER_DIGEST/local_postgres_rehearsal.py \
  --postgres-image-id postgres@sha256:POSTGRES_REPO_DIGEST \
  --docker-binary /usr/bin/docker \
  --source-network blueprint-control-plane \
  --source-host "$DOCKER_BRIDGE_GATEWAY" --source-port 5433 \
  --source-database postgres --source-user postgres \
  --source-password-file /root/blueprint-cloudsql-rehearsal-password \
  --source-project-id blueprint-orchestrator \
  --source-instance blueprint-cloudsql-8cee8173 \
  --cloud-sql-instance-evidence-file /var/lib/blueprint/precutover-evidence/cloud-sql-instance.json \
  --proxy-pid "$PROXY_PID" \
  --expected-proxy-executable-sha256 "$PROXY_SHA256" \
  --expected-proxy-address "$DOCKER_BRIDGE_GATEWAY" \
  --expected-alembic-revision EXPECTED_SCHEMA_REVISION \
  --work-root /var/lib/blueprint/precutover-rehearsals \
  --permanent-data-dir /srv/blueprint/postgres \
  --artifact-output /var/lib/blueprint/precutover-evidence/live-source-rehearsal.dump \
  --manifest-output /var/lib/blueprint/precutover-evidence/live-source-rehearsal-manifest.json \
  --source-snapshot-output /var/lib/blueprint/precutover-evidence/live-source-snapshot.json \
  --snapshot-output /var/lib/blueprint/precutover-evidence/disposable-target-snapshot.json \
  --result-output /var/lib/blueprint/precutover-evidence/rehearsal-result.json
```

The helper uses two uniquely named, ownership-labelled containers. The source
client has no mounts. The restore target has `--network none`, publishes no
ports, and mounts only a generated session directory beneath `--work-root`.
Success is written only after the target container and session data directory
are gone. It refuses any overlap with `--permanent-data-dir`.

Review the manifest, restored Alembic revision, critical integrity aggregates,
table counts, and isolation evidence. The manifest and result explicitly say
`final_cutover_eligible: false`; never pass this rehearsal artifact to the
fenced `verify-import` command. A failed or incomplete cleanup is a failed
rehearsal and must be investigated before cutover.

Create the separate recovery-shaped archive with the other content-addressed
helper. This is a fresh PostgreSQL 15 plain-SQL dump taken while production
remains writable; it is not the final custom-format cutover artifact. The
dedicated recovery writer can create objects but cannot read or delete them.
The uploader is a pinned Cloud SDK container, so hamsterwheel does not need a
host `gcloud` installation. The mounted, content-addressed upload helper uses
`gcloud` only to mint an access token, then streams directly to GCS with an
`ifGenerationMatch=0` create precondition. It performs no post-upload read or
list operation, so the writer retains create-only IAM:

```bash
sudo install -d -m 0700 -o root -g root /run/blueprint-rehearsal-export

sudo python3 /usr/local/lib/blueprint-node/HELPER_DIGEST/live_source_rehearsal_export.py \
  --postgres-image-id postgres@sha256:POSTGRES_REPO_DIGEST \
  --host "$DOCKER_BRIDGE_GATEWAY" --port 5433 \
  --source-database postgres --user postgres \
  --source-project-id blueprint-orchestrator \
  --source-instance blueprint-cloudsql-8cee8173 \
  --cloud-sql-instance-evidence /var/lib/blueprint/precutover-evidence/cloud-sql-instance.json \
  --proxy-pid "$PROXY_PID" \
  --source-evidence-output /var/lib/blueprint/precutover-evidence/live-source-evidence.json \
  --password-file /root/blueprint-cloudsql-rehearsal-password \
  --archive-output /var/lib/blueprint/precutover-evidence/live-source.sql.gz \
  --bucket gs://RECOVERY_BUCKET \
  --recovery-project-id RECOVERY_PROJECT_ID \
  --writer-credentials-file /etc/blueprint/backup-writer.json \
  --uploader-image-id gcr.io/google.com/cloudsdktool/cloud-sdk@sha256:CLOUD_SDK_REPO_DIGEST \
  --control-plane-network blueprint-control-plane \
  --secret-stage-root /run/blueprint-rehearsal-export

sudo rm -- /root/blueprint-cloudsql-rehearsal-password
```

The exporter validates the raw Cloud SQL identity and the live proxy PID,
owner-matched executable hash, exact argv, connection name, bridge address, and
port before and after the dump. It creates its own pinned, mount-free,
ownership-labelled PG15 client and proves that container is gone before any
archive upload. The source evidence is create-only and hash-binds those facts
to the exact local archive and planned recovery object. Writer-key staging is
allowed only beneath the validated root-owned `/run` tmpfs directory; stale
owned staging files are removed before work begins.

The archive is uploaded with a generation-zero precondition before its exact v1
completion marker. No marker is published after a dump, client cleanup,
identity-evidence, or archive-upload failure. Trigger the independent recovery
verifier unchanged, preserve its evidence, and require that evidence to
identify this archive generation before claiming the off-host gate passed.
After cutover, the normal local backup service replaces this rehearsal producer
and must independently produce and restore its first successful archive before
the timer is enabled.

The installed backup service retries a failed run after 15 minutes, with at
most three starts in a six-hour window (the initial run plus two retries). The
twice-daily persistent timer tries again after that window, so a Wi-Fi or
default-route outage cannot require a manual reset, while a persistent
credential, IAM, or manifest failure cannot create unbounded retained orphan
archives. Successful runs do not restart, and the backup's single-flight lock
plus unique object names prevent overlapping retries or overwrites. Investigate
repeated failures in the journal rather than relying on retries to mask a
credential, capacity, or database problem.

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

1. Prove the fresh live-source rehearsal archive and completion manifest above
   can be restored by the independent recovery identity. Keep retention
   unlocked until that drill succeeds. This pre-cutover rehearsal is the only
   exception to the normal rule that `local-postgres-exports/` comes from the
   authoritative local backup service; the verifier contract is unchanged.
2. Create a root-owned cutover directory, stop and disable the old
   user-scoped worker, and stop every other source writer. Leave the
   authenticated Cloud SQL proxy running because the final dump uses it:

   ```bash
   sudo install -d -m 0700 -o root -g root /var/lib/blueprint/cutover
   systemctl --user stop blueprint-worker.service
   systemctl --user disable blueprint-worker.service
   systemctl --user is-active blueprint-worker.service
   systemctl --user is-enabled blueprint-worker.service
   docker ps --format '{{.Names}}' | grep -E '^blueprint-local-worker$'
   ```

   Both `systemctl` checks must report inactive/disabled and the container check
   must find nothing. Stop any separately installed writer before continuing.
3. Reconstruct the **complete** production Terraform input set and preserve
   every existing non-default value, including CI/WIF, GitHub, billing, budget,
   alerting, region, database, and orchestrator URL settings. Never run this
   root with only `project_id` and the fence variable: empty or false defaults
   can otherwise remove unrelated production resources. Create, inspect, and
   apply one saved plan:

   ```bash
   terraform -chdir=infra/terraform plan \
     -var-file=FULL_PRODUCTION_TFVARS \
     -var=enable_public_orchestrator_access=false \
     -out=/tmp/public-write-fence.tfplan
   terraform -chdir=infra/terraform show \
     /tmp/public-write-fence.tfplan
   sudo install -m 0600 /tmp/public-write-fence.tfplan \
     /var/lib/blueprint/cutover/public-write-fence.tfplan
   terraform -chdir=infra/terraform apply \
     /tmp/public-write-fence.tfplan
   rm -- /tmp/public-write-fence.tfplan
   ```

   The plan must contain no unrelated deletes or replacements, especially of
   Cloud SQL, Cloud Run, Workload Identity, monitoring, budgets, service
   accounts, networks, or storage. Save the reviewed full-variable plan used to
   restore public access as well; do not improvise a partial rollback plan.
4. Capture the resulting service-bound IAM evidence and source identity
   evidence. Use the installed cutover tool so the resource description and
   IAM acquisition are performed together with the exact project, region, and
   service arguments. The IAM JSON must contain neither `allUsers` nor
   `allAuthenticatedUsers` in a `roles/run.invoker` binding. Repeatedly prove
   that both the direct service
   URL and every tagged `run.app` URL return `401` or `403`; DNS movement alone
   is not a write fence:

   ```bash
   # OPERATOR HOST: run from the exact deployed commit.
   python3 /path/to/Blueprint/scripts/local_postgres_cutover.py \
     capture-cloud-run-fence \
     --project-id blueprint-orchestrator --region us-central1 \
     --service blueprint-orchestrator \
     --output /absolute/operator/path/cloud-run-fence.json
   gcloud sql instances describe blueprint-cloudsql-8cee8173 \
     --project=blueprint-orchestrator --format=json \
     > /absolute/operator/path/cloud-sql-instance.json
   scp /absolute/operator/path/cloud-run-fence.json hamsterwheel:/tmp/cloud-run-fence.json
   scp /absolute/operator/path/cloud-sql-instance.json hamsterwheel:/tmp/cloud-sql-instance.json

   # HAMSTERWHEEL:
   ssh -t hamsterwheel \
     'sudo install -m 0600 /tmp/cloud-run-fence.json /var/lib/blueprint/cutover/cloud-run-fence.json'
   ssh -t hamsterwheel \
     'sudo install -m 0600 /tmp/cloud-sql-instance.json /var/lib/blueprint/cutover/cloud-sql-instance.json'
   ssh hamsterwheel 'rm -- /tmp/cloud-run-fence.json /tmp/cloud-sql-instance.json'
   ssh -t hamsterwheel sudo sha256sum \
     /var/lib/blueprint/cutover/cloud-run-fence.json \
     /var/lib/blueprint/cutover/cloud-sql-instance.json

   # OPERATOR HOST:
   curl -sS -o /dev/null -w '%{http_code}\n' CLOUD_RUN_SERVICE_URL
   curl -sS -o /dev/null -w '%{http_code}\n' CLOUD_RUN_TAGGED_URL
   ```

   If `gcloud` is not on the operator host's `PATH`, pass its absolute path with
   `--gcloud-executable`. On Windows, use the SDK's shell-free
   `google-cloud-sdk\lib\gcloud.py` entrypoint; the tool invokes it with its own
   Python interpreter. Delete the operator copy after its SHA-256 matches the
   installed evidence.

   Preserve the endpoint probe output and timestamps beside the fence
   evidence. Abort if any probe returns a status other than `401` or `403`.
5. Rotate the Cloud SQL `postgres` password to a random cutover-only value in a
   root-owned mode-`0600` file on hamsterwheel. Do **not** update Secret Manager,
   Cloud Run, the old worker, or any other old runtime with this value. Generate
   it without shell history or argv exposure. From an interactive, non-recorded
   hamsterwheel SSH session, display it once with `sudo cat`, copy it only to the
   operator's password prompt, then clear the clipboard:

   ```bash
   # HAMSTERWHEEL:
   ssh -t hamsterwheel \
     "sudo sh -c 'umask 077; openssl rand -base64 48 > /var/lib/blueprint/cutover/cloudsql-source-password'"
   ssh -t hamsterwheel sudo stat -c '%a %U:%G %n' \
     /var/lib/blueprint/cutover/cloudsql-source-password
   ssh -t hamsterwheel sudo cat \
     /var/lib/blueprint/cutover/cloudsql-source-password

   # OPERATOR HOST: paste the value at both prompts; do not put it in argv.
   gcloud sql users set-password postgres \
     --instance=blueprint-cloudsql-8cee8173 \
     --project=blueprint-orchestrator \
     --prompt-for-password --async --format='value(name)'
   ```

   On the operator host, record the returned operation name, wait for it, and
   capture the raw operation JSON to an operator-local absolute path. Transfer
   it to hamsterwheel and install it root-only:

   ```bash
   # OPERATOR HOST:
   gcloud sql operations wait PASSWORD_ROTATION_OPERATION \
     --project=blueprint-orchestrator
   gcloud sql operations describe PASSWORD_ROTATION_OPERATION \
     --project=blueprint-orchestrator --format=json \
     > /absolute/operator/path/password-rotation-operation.json
   scp /absolute/operator/path/password-rotation-operation.json \
     hamsterwheel:/tmp/password-rotation-operation.json

   # HAMSTERWHEEL:
   ssh -t hamsterwheel \
     'sudo install -m 0600 /tmp/password-rotation-operation.json /var/lib/blueprint/cutover/password-rotation-operation.json'
   ssh hamsterwheel 'rm -- /tmp/password-rotation-operation.json'
   ```

   The operation must be `DONE`, type `UPDATE_USER`, target this project and
   instance, and contain no error. Never use `--password="$(cat ...)"`: it puts
   the credential in process arguments.
6. Discover and verify the proxy path from the PostgreSQL container. The
   standard local install exposes the authenticated proxy on the control-plane
   bridge gateway port `5433`:

   ```bash
   systemctl --user is-active blueprint-database-proxy.service
   docker network inspect blueprint-control-plane
   docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' \
     blueprint-control-plane
   sudo ss -ltnp | grep ':5433'
   ```

   Substitute the reported gateway for `DOCKER_BRIDGE_GATEWAY` below. Abort if
   the proxy unit is inactive, the listener is not the expected proxy, or a
   connection from `blueprint-local-postgres` cannot authenticate.
7. Through that proxy, terminate every existing source session except the
   cutover operator session, including idle transactions. Use the temporary
   password only through a root process environment (the same no-argv pattern
   enforced by the cutover tool). This command passes only the variable name
   through `docker exec`, not the value in argv:

   ```bash
   sudo sh -c '
     export PGPASSWORD="$(cat /var/lib/blueprint/cutover/cloudsql-source-password)"
     exec docker exec --interactive --env PGPASSWORD blueprint-local-postgres \
       psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 \
       --host "$1" --port 5433 --username postgres --dbname postgres \
       --command "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
   ' sh DOCKER_BRIDGE_GATEWAY
   ```

   Then capture at least three separate, time-ordered observations spanning at
   least 30 seconds. Each adjacent pair must be at least 10 seconds apart; use
   15 seconds below to leave scheduling margin. Each raw JSON file must contain
   exactly `[]`, produced from:

   ```bash
   sudo sh -c '
     export PGPASSWORD="$(cat /var/lib/blueprint/cutover/cloudsql-source-password)"
     docker exec --interactive --env PGPASSWORD blueprint-local-postgres \
       psql --no-psqlrc --quiet --set ON_ERROR_STOP=1 --tuples-only --no-align \
       --host "$1" --port 5433 --username postgres --dbname postgres \
       --command "SELECT COALESCE(json_agg(json_build_object('\''pid'\'',pid,'\''user'\'',usename,'\''application_name'\'',application_name,'\''client_addr'\'',client_addr,'\''state'\'',state,'\''xact_start'\'',xact_start,'\''query_start'\'',query_start) ORDER BY pid),'\''[]'\''::json) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid()" \
       > "$2"
   ' sh DOCKER_BRIDGE_GATEWAY /var/lib/blueprint/cutover/session-observation-1.json
   date -u +%Y-%m-%dT%H:%M:%SZ \
     | sudo tee /var/lib/blueprint/cutover/session-observation-1.captured-at
   sudo sha256sum /var/lib/blueprint/cutover/session-observation-1.json
   sleep 15
   ```

   Repeat that capture command for `session-observation-2.json` and
   `session-observation-3.json`, changing the corresponding `.captured-at`
   filename too. Record each UTC timestamp immediately after its query, and
   sleep at least 15 seconds before the next query. The first-to-last timestamps
   must span at least 30 seconds. Hash each unmodified JSON file. If any old
   session reappears, the fence is not durable: find and stop that writer,
   terminate sessions again, and restart all three observations.
8. Create `/var/lib/blueprint/cutover/source-fence.json`, mode `0600`, with
   `schema_version=1`, `kind=postgres-durable-source-fence`, and:

   - `source`: `project_id`, `instance`, and `database`;
   - `password_rotated=true` and `old_sessions_terminated=true`;
   - `raw_evidence`: hash-bound absolute `path` entries whose unique `kind`
     values are `cloud_run_fence`, `cloud_sql_instance`, and
     `password_rotation_operation`; and
   - `session_observations`: at least three ordered entries containing
     `captured_at`, `old_session_count=0`, absolute `path`, and that raw file's
   SHA-256.

   The resulting document has this exact shape (replace every placeholder with
   the captured value, and use the actual paths for observations two and
   three):

   ```json
   {
     "schema_version": 1,
     "kind": "postgres-durable-source-fence",
     "source": {
       "project_id": "blueprint-orchestrator",
       "instance": "blueprint-cloudsql-8cee8173",
       "database": "postgres",
       "cloud_run_service": "blueprint-orchestrator",
       "cloud_run_region": "us-central1"
     },
     "password_rotated": true,
     "old_sessions_terminated": true,
     "raw_evidence": [
       {
         "kind": "cloud_run_fence",
         "path": "/var/lib/blueprint/cutover/cloud-run-fence.json",
         "sha256": "CLOUD_RUN_FENCE_SHA256"
       },
       {
         "kind": "cloud_sql_instance",
         "path": "/var/lib/blueprint/cutover/cloud-sql-instance.json",
         "sha256": "CLOUD_SQL_INSTANCE_SHA256"
       },
       {
         "kind": "password_rotation_operation",
         "path": "/var/lib/blueprint/cutover/password-rotation-operation.json",
         "sha256": "PASSWORD_ROTATION_OPERATION_SHA256"
       }
     ],
     "session_observations": [
       {
         "captured_at": "UTC_TIMESTAMP_1",
         "old_session_count": 0,
         "path": "/var/lib/blueprint/cutover/session-observation-1.json",
         "sha256": "SESSION_OBSERVATION_1_SHA256"
       },
       {
         "captured_at": "UTC_TIMESTAMP_2",
         "old_session_count": 0,
         "path": "/var/lib/blueprint/cutover/session-observation-2.json",
         "sha256": "SESSION_OBSERVATION_2_SHA256"
       },
       {
         "captured_at": "UTC_TIMESTAMP_3",
         "old_session_count": 0,
         "path": "/var/lib/blueprint/cutover/session-observation-3.json",
         "sha256": "SESSION_OBSERVATION_3_SHA256"
       }
     ]
   }
   ```

   Do not summarize or rewrite the tool-captured Cloud Run envelope or the two
   raw gcloud JSON files. The cutover tool loads and hashes them itself, binds
   the Cloud Run policy to the requested service/project/region, validates
   PostgreSQL 15 and the instance connection name, validates the completed
   password operation, rejects malformed or public invoker IAM, and requires
   every session observation file to parse as exactly `[]`.
9. While the fence remains in force, run `dump-source` directly through the
   authenticated proxy. It validates the durable fence, snapshots exact
   Alembic revision, schema/catalog objects, table counts, sequence maxima, and
   critical integrity aggregates, then streams a PostgreSQL 15 custom-format
   `pg_dump` to the local artifact. It creates and hash-binds both the snapshot
   and manifest; do not hand-author the manifest:

   ```bash
   sudo python3 /usr/local/lib/blueprint-node/CUTOVER_DIGEST/local_postgres_cutover.py dump-source \
     --source-fence /var/lib/blueprint/cutover/source-fence.json \
     --container blueprint-local-postgres \
     --host DOCKER_BRIDGE_GATEWAY --port 5433 \
     --database postgres --user postgres \
     --password-file /var/lib/blueprint/cutover/cloudsql-source-password \
     --instance blueprint-cloudsql-8cee8173 \
     --artifact-output /var/lib/blueprint/cutover/cloudsql-final.dump \
     --source-snapshot-output /var/lib/blueprint/cutover/source-snapshot.json \
     --manifest-output /var/lib/blueprint/cutover/dump-manifest.json \
     --timeout-seconds 7200
   ```

   There is no managed Cloud SQL export, GCS staging generation, CDC, or
   undocumented final delta in this path.
10. With only `blueprint-database.service` active, restore and verify the empty
    local target:

    ```bash
    sudo python3 /usr/local/lib/blueprint-node/CUTOVER_DIGEST/local_postgres_cutover.py verify-import \
      --source-snapshot /var/lib/blueprint/cutover/source-snapshot.json \
      --source-fence /var/lib/blueprint/cutover/source-fence.json \
      --export-manifest /var/lib/blueprint/cutover/dump-manifest.json \
      --target-container blueprint-local-postgres \
      --target-database blueprint --target-user blueprint \
      --target-password-file /etc/blueprint/postgres-password \
      --target-instance hamsterwheel \
      --target-snapshot-output /var/lib/blueprint/cutover/target-snapshot.json \
      --result-output /var/lib/blueprint/cutover/import-result.json \
      --import-timeout-seconds 7200
    ```

    The verifier requires a custom PostgreSQL archive, proves the target public
    namespace is empty, uses `pg_restore --single-transaction`, snapshots the
    target, and refuses success unless every comparison field is exact.
    Preserve the dump, manifest, mode-`0600` result, fence, raw evidence, and
    both snapshots. On failure, keep all application units stopped, reinitialize
    the target deliberately, and investigate; never continue from a partially
    examined target.
11. While the imported database remains quiescent and every application unit
    is still stopped, manually run the local backup service. Verify the
    recovery-bucket archive and manifest, pin that exact generation in the
    independent recovery verifier, and require a successful restore drill with
    cleanup evidence before allowing any application process to connect.
12. Make an explicit go/no-go decision. Starting the API can run mutating
    initialization and background tasks even while it is loopback-only, so
    **API activation is the rollback boundary**. Start only the local API and
    keep the edge and worker stopped:

    ```bash
    sudo systemctl start blueprint-api.service
    sudo systemctl is-active blueprint-api.service
    ```

    Verify the loopback health and read-only API paths. On any failure after
    API activation, stop the API, verify it is inactive, and keep the edge and
    worker stopped to contain further local writes. Keep Cloud Run fenced and
    roll forward locally unless a reviewed reverse synchronization proves
    Cloud SQL contains every local write. Public OAuth/App callbacks, webhooks,
    WebSockets, CORS, and browser-origin behavior cannot be exercised until the
    edge starts.
13. Start the edge to expose Funnel to callbacks, webhooks, and users:

    ```bash
    sudo systemctl start blueprint-edge.service
    sudo systemctl is-active blueprint-edge.service
    ```

    Immediately verify health, authentication, GitHub OAuth/App callbacks and
    webhooks, WebSockets, CORS, artifact signed URLs, and one controlled
    production write. On any failure after edge activation, immediately stop
    the edge and API, verify both are inactive, and keep the worker stopped to
    contain new local writes. Keep Cloud Run fenced and roll forward locally
    unless a reviewed reverse synchronization proves Cloud SQL contains every
    local write.
14. Run another manual backup after the controlled write and independently
    verify its manifest. Then start the worker and confirm exactly one process
    owns advisory leadership and dispatch/reaper heartbeats are current. Enable
    the backup timer only after those checks pass.
15. Keep Cloud Run write-fenced and Cloud SQL frozen with deletion protection,
   PITR, and backups for at least 72 hours and multiple successful local
   backups. Do not delete Cloud SQL during the observation window.

The rollback boundary is API activation, because its initialization and
background tasks can write without public traffic. Before that point, stop and
verify API, edge, and worker are all inactive, rotate
the Cloud SQL `postgres` password back to the original value still held by the
old production secret (again using `--prompt-for-password`, never argv), verify
the resulting `UPDATE_USER` operation is `DONE`, apply the previously reviewed
complete Terraform rollback plan, and only then re-enable/start the old worker.
Recheck the intended Cloud Run IAM binding, health, and exactly one active
writer before reopening traffic. Do not copy the temporary cutover password
into the old secret or runtime.

After either a successful rollback or acceptance of the local cutover, remove
the temporary password file and any temporary access-token material while
retaining the non-secret evidence and dump under the approved retention policy:

```bash
sudo rm -- /var/lib/blueprint/cutover/cloudsql-source-password
```

After API activation, assume a local write may have committed. A direct
rollback to the old Cloud SQL database can discard local-only writes and is
therefore unsafe. Failback then requires
a reviewed reverse migration or restore into Cloud SQL, a new write freeze,
reconciliation evidence, and an explicit operator decision. Do not describe
“restart Cloud Run” as a complete rollback after local writes begin.

For a local service failure that does not require database failback, stop only
the affected role:

```bash
sudo python3 scripts/blueprint_node.py stop --role edge --system
sudo python3 scripts/blueprint_node.py stop --role worker --system
```

Stopping `edge` removes only the exact Blueprint Funnel route. It refuses to
alter foreign Funnel state. Stopping `worker` interrupts active background work;
it is not a drain operation.
