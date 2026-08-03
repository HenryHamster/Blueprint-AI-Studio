# Hamsterwheel application release

Hamsterwheel is the production API, worker, database, and edge host. A normal
push to `main` builds and publishes a combined backend plus admin-UI image, but
does not automatically wake the recovery-only Cloud Run service or run the
retired Cloud SQL migration job. The build uploads `local-app-release.json`
with an exact Artifact Registry digest and Git revision.

## One-time release bootstrap

The first rollout uses `bootstrap_local_app_release.py` because hamsterwheel's
currently installed immutable bundle predates the release command and deploy
SHA forwarding. Transfer that script and its allowlisted sibling files to one
root-owned `0700` bootstrap directory, verify their reviewed SHA-256 values,
then run:

```bash
sudo env -i \
  SUDO_USER=henry_hamster \
  PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  /var/lib/blueprint/bootstrap/local-app-release/bootstrap_local_app_release.py \
  --revision FULL_GIT_SHA
```

The bootstrap installs a new content-addressed script bundle and redirects only
the API unit's future starts to the new wrapper. Because systemd dependencies
cannot be removed by a drop-in, it snapshots the legacy edge base unit and
noninteractive drop-in, renders the complete reviewed edge unit with the live
immutable helper and environment paths, and retires the obsolete drop-in to a
root-only recovery copy. It preserves original file modes for rollback,
performs `daemon-reload`, and does not restart the API, worker, database,
backup timer, or edge. It verifies the effective dependency and hardening
contracts, all five unit markers, and every unrelated role/base file.

## Release

Run the root-owned immutable `local_app_release.py` with the artifact's exact
`image_ref` and `revision`. Do not use a tag. Launch it as a transient systemd
service so an SSH or Wi-Fi interruption does not terminate the transaction:

```bash
sudo systemd-run \
  --unit=blueprint-local-app-release \
  --collect \
  --wait \
  /usr/bin/env -i \
    SUDO_USER=henry_hamster \
    PATH=/usr/sbin:/usr/bin:/sbin:/bin \
    /usr/local/lib/blueprint-node/REVIEWED_BUNDLE/local_app_release.py \
    --image IMAGE_AT_SHA256 \
    --expected-revision FULL_GIT_SHA
```

The release tool verifies the installed local-control-plane manifest, image
digest and labels, static-tree manifest, service ownership, public origin,
database/backup/edge file hashes, and health endpoints. It authenticates only
to the exact Artifact Registry host using the owner-only worker credential and
an ephemeral root-only Docker configuration below `/run`; that configuration
must be deleted successfully before any role or service mutation begins. The
credential is supplied to Docker over standard input and is never placed in an
argument, environment variable, receipt, or log.

Artifact Registry access is deliberately repository-scoped. Only
`blueprint-local-worker-runtime@blueprint-orchestrator.iam.gserviceaccount.com`
may receive `roles/artifactregistry.reader`, and only on the `blueprint-repo`
repository. Do not grant it at project scope, grant writer/admin, or grant the
API runtime account. Verify the live policy before release:

```bash
gcloud artifacts repositories get-iam-policy blueprint-repo \
  --project=blueprint-orchestrator \
  --location=us-central1 \
  --format=json
```

The output must contain the worker account under
`roles/artifactregistry.reader` and must not add the API runtime account.

The tool retains the prior image, stops only a container whose immutable ID and
runtime contract match the managed API or worker, waits for the unit and that
exact container to stop, and starts the worker before the API. Worker startup
and sole-leadership readiness must pass before the API is touched. Local API
revision and health must pass before public health. Only services touched by a
failed activation are rolled back, using the same identity and readiness
checks. Root-only receipts are written below
`/var/lib/blueprint/local-app-releases/`.

Cloud Run remains a manually activated recovery target. Firebase Hosting
remains a separately deployed rollback UI, and its origin must remain present
in `ALLOWED_ORIGINS` so GitHub OAuth can return to it safely. Before activation,
verify:

- `ORCHESTRATOR_URL` is exactly the Funnel HTTPS origin and `ADMIN_UI_URL`
  resolves to that same origin;
- the Funnel hostname is a Firebase Auth authorized domain;
- both the Funnel and Firebase rollback origins are exact `ALLOWED_ORIGINS`;
- the GitHub OAuth callback is the Funnel origin plus
  `/api/github/oauth/callback`;
- the recovery Cloud Run/Cloud SQL path remains disabled unless its database
  has first been explicitly reconciled from authoritative local PostgreSQL.
