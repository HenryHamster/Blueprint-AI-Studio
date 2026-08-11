# Hamsterwheel application release

<!-- RELEASE_RUNBOOK_CONTRACT:START -->
```json
{
  "entrypoints": {
    "bootstrap": {
      "flags": ["--image", "--rehearse", "--revision"],
      "script": "scripts/bootstrap_local_app_release.py"
    },
    "release": {
      "flags": ["--expected-revision", "--image", "--rehearse"],
      "script": "scripts/local_app_release.py"
    }
  },
  "preconditions": [
    "live systemd privilege boundary",
    "worker secret and static migration inputs",
    "candidate migration reachability from live schema"
  ]
}
```
<!-- RELEASE_RUNBOOK_CONTRACT:END -->

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
the API unit's future starts to the new wrapper, installs the always-on
non-root deploy agent, and installs a socket-activated root release executor.
Because systemd dependencies
cannot be removed by a drop-in, it snapshots the legacy edge base unit and
noninteractive drop-in, renders the complete reviewed edge unit with the live
immutable helper and environment paths, and retires the obsolete drop-in to a
root-only recovery copy. It preserves original file modes for rollback,
performs `daemon-reload`, and does not restart the API, worker, database,
backup timer, or edge. It verifies the effective dependency and hardening
contracts, including systemd's effective `NoNewPrivileges` values, all five
unit markers, and every unrelated role/base file. Receipt-retry state is kept
under `/var/lib/blueprint-deploy-agent/` so a deploy-agent restart cannot cause
the release to run twice.

The deploy agent does not use `sudo`: its hardened non-root unit connects to
`/run/blueprint-release.sock`, whose root-owned socket unit starts
`blueprint-release.service` as root. The socket ACL is limited to the service
account. The agent's live approval read, exact CI manifest binding, superseding
promotion re-read, and activation receipt POST remain the authority checks;
the root executor independently validates the digest and revision before it
starts the immutable release tool. A failed root-release activation phase is
retried at most three times, with 5 seconds and then 10 seconds of backoff.
Accepted or ambiguous socket handoffs are fenced and never replayed. Exhaustion
is reported and persisted as `phase=failed` with the attempt count and
`root_release.failure_reason`; it is not converted into a serving-confirmation
timeout.

## Qualification and integrated pre-stop checks

Do not run either release entrypoint with `--rehearse`. Prequalification
candidate execution on Hamsterwheel is disabled: those compatibility flags
are fail-closed sentinels and exit with `host rehearsal disabled` before image
pull, migration inspection, or service mutation. They are not an operator
release step.

Before promotion, any criterion that requires candidate build/smoke evidence
is satisfied only by the candidate-bound, secrets-free trusted-main GitHub
workflow. That workflow does not claim to prove Hamsterwheel's live systemd,
secret-file, database, or activation state.

The real release command below performs the live pre-stop checks itself,
inside the root-owned activation transaction and immediately before the first
service mutation. It verifies:

- the live systemd-reported privilege boundary: the non-root deploy agent has
  `User=henry_hamster` and `NoNewPrivileges=yes`, while the root release
  executor has `User=root` and `NoNewPrivileges=no`;
- the candidate frontend/revision binding and image inspection;
- worker-secret and other static migration inputs; and
- that the candidate schema is forward-reachable from the live database head.

A precondition failure is recorded in the root release receipt while the
managed units remain running. Fix the reported input or select a corrected
candidate, then submit a new ordinary activation. Never bypass the integrated
check and never run candidate code on the production host before promotion.

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

The tool retains the prior image and compares the candidate's baked schema
revision with the live database head. Matching revisions retain the existing
hot-swap behavior. Before any unit is stopped for a mismatch, the tool checks
the candidate image's bundled Alembic graph and requires the target to be
forward-reachable from the live head. A non-forward or unreachable target is
refused while both app units remain running; select an image matching the
database or fix forward. A forward-reachable mismatch uses bounded downtime:
it stops both managed app units, waits for their exact containers to stop, runs
the canonical local migration transaction with the candidate image, confirms
the resulting schema head, then starts the worker before the API. Worker
startup and sole-leadership readiness must pass before the API is touched.
Local API revision and health must pass before public health. Once a migration
downtime window begins, a failure leaves the app units stopped and requires a
fix-forward release; it never rolls prior code back across a possibly advanced
schema. Other failed activations roll back only their touched services using
the same identity and readiness checks. Root-only receipts are written below
`/var/lib/blueprint/local-app-releases/`. If the run fails after its integrated
pre-stop checks pass, abort and use that receipt to identify the failed phase;
do not blindly retry. Before migration downtime begins, the tool restores its
touched files and services. After downtime begins, leave the units stopped as
directed by the failure evidence, do not restore the prior image across an
advanced schema, and fix forward with a corrected candidate.

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
