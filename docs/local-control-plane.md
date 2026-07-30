# Local background control plane

The `control-plane` node profile runs Blueprint's leader-only scheduler,
dispatch, reaper, and maintenance loops on a trusted Linux host. It does not
publish an HTTP port. The public API remains on Cloud Run.

This profile can run beside the existing `blueprint-runner.service` only while
that executor keeps task code inside its supported isolation backends. It must
**not** share a host with a GitHub Actions runner. PostgreSQL advisory-lock
leadership prevents the local worker and the legacy Cloud Run `all` process
from running background loops simultaneously during rollout.

## Security boundary

Use three separate owner-only files:

- a Cloud SQL Auth Proxy key for
  `blueprint-local-worker@<project>.iam.gserviceaccount.com`; this identity has
  only `roles/cloudsql.client`;
- a worker key for
  `blueprint-local-worker-runtime@<project>.iam.gserviceaccount.com`;
- a worker JSON bundle containing the required application secrets and the
  non-secret production compute configuration.

The worker identity cannot sign public API URLs, administer project IAM, mutate
the Cloud Run service, or access arbitrary Secret Manager secrets. Dynamic
secret access is limited to `agent-creds-*` and `user-*`. The worker container:

- uses an immutable local `sha256:` image ID with pulls disabled;
- publishes no ports;
- runs as the invoking non-root user with all capabilities dropped;
- has a read-only root filesystem, a `noexec` temporary filesystem, resource
  limits, and `no-new-privileges`;
- receives only an explicit environment allowlist;
- bind-mounts only the dedicated worker credential, read-only.

The Auth Proxy binds to the private gateway of an unpublished dedicated Docker
bridge, not `0.0.0.0`, loopback, or a LAN address. Its executable digest and
dedicated credential identity are verified before every start.

GitHub Actions is a separate trust zone. An Actions job that can use the host
Docker daemon is effectively root-capable, regardless of Unix account or file
modes, and can inspect other containers and host credentials. Run Actions in a
real VM (or on another physical machine), with its own Docker daemon. A normal
container, a second host user, or an LXD system container is not a sufficient
boundary. The worker preflight fails if it detects a host-level Actions runner.

Before installing credentials, verify that the host has no Actions runner
processes:

```bash
pgrep -af 'Runner.Listener|Runner.Worker|actions-runner'
```

If any are present, migrate and drain them first. Do not copy production
credentials onto the host while they remain active.

## Install

Build the backend `app` image from the reviewed candidate and record its local
image ID:

```bash
docker build --target app -t blueprint-local-worker:candidate backend
docker image inspect --format '{{.Id}}' blueprint-local-worker:candidate
sha256sum ~/.local/bin/cloud-sql-proxy
```

Then install the two services from the trusted host account. A dedicated
service account is recommended as defense in depth, but it does not replace
the Actions VM boundary: any host-level Actions runner with Docker access can
cross Unix-account boundaries. Values below are identifiers and paths, not
secret values:

```bash
python3 scripts/blueprint_node.py install \
  --profile control-plane \
  --cloudsql-instance PROJECT:REGION:INSTANCE \
  --cloudsql-proxy-binary "$HOME/.local/bin/cloud-sql-proxy" \
  --cloudsql-proxy-sha256 PROXY_SHA256 \
  --cloudsql-credentials-file "$HOME/.blueprint/credentials/cloudsql.json" \
  --worker-image-id sha256:IMAGE_ID \
  --worker-secrets-file "$HOME/.blueprint/credentials/blueprint-local-worker-secrets.json" \
  --worker-google-credentials-file "$HOME/.blueprint/credentials/blueprint-local-worker-runtime.json"
```

Both units have mandatory environment files and `ExecStartPre` security
preflights. The worker requires and starts after the proxy. Its preflight also
opens a credential-free socket from the actual private Docker network, so a
host firewall cannot produce a false-positive host-only health check.

If UFW is active, allow only that bridge subnet to reach the proxy's bridge
gateway after the installer has created `blueprint-control-plane`:

```bash
gateway="$(docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' blueprint-control-plane)"
subnet="$(docker network inspect --format '{{(index .IPAM.Config 0).Subnet}}' blueprint-control-plane)"
sudo ufw allow from "$subnet" to "$gateway" port 5433 proto tcp comment 'Blueprint private Cloud SQL proxy'
systemctl --user restart blueprint-worker.service
```

This does not publish the port or allow LAN access. If the rule is absent, the
worker fails closed before its production secrets enter a container.

## Rollout and rollback

1. Start the local services while Cloud Run still has its legacy `all` role.
2. Confirm the proxy is healthy and exactly one process holds background
   leadership. The local worker may remain a standby at this point.
3. Deploy the reviewed process-role code to Cloud Run.
4. Set Cloud Run to `BLUEPRINT_PROCESS_ROLE=api`.
5. Confirm the local worker acquires leadership and that dispatch/reaper
   heartbeats remain current.

Rollback is deliberately small. First create a warm Cloud Run `all` revision
and keep production traffic on `LATEST`:

```bash
gcloud run services update blueprint-orchestrator \
  --project blueprint-orchestrator \
  --region us-central1 \
  --update-env-vars BLUEPRINT_PROCESS_ROLE=all \
  --min 1 \
  --max 1 \
  --concurrency 120
gcloud run services update-traffic blueprint-orchestrator \
  --project blueprint-orchestrator \
  --region us-central1 \
  --to-latest
```

The new revision initially remains a follower because the local worker still
owns the advisory lock. Stop the local worker, then wait for Cloud Run's
non-leader watchdog to acquire the released lock and restart through the leader
startup path:

```bash
python3 scripts/blueprint_node.py stop --role worker
until curl -fsS https://blueprint-orchestrator-mmtfi5aysa-uc.a.run.app/health \
  | jq -e '.status == "healthy" and .is_leader == true' >/dev/null; do
  sleep 5
done
gcloud run services describe blueprint-orchestrator \
  --project blueprint-orchestrator \
  --region us-central1 \
  --format='yaml(status.latestReadyRevisionName,status.traffic)'
```

Verify a current orchestrator cycle and reaper heartbeat before disabling the
database proxy. If `call-staging` must remain tagged, also verify it is present
in `status.traffic`; `gcloud run services update-traffic --to-latest` can remove
tags and they must be restored explicitly with `--update-tags`.
