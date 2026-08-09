# One-command control-plane deploy

`python scripts/blueprint.py deploy` activates the newest CI-built `main`
release on the hamsterwheel control plane. Activating a release bumps the
orchestrator's advertised revision (`version_hint`), which is what makes every
local runner (`hamsterwheel`, `dgx-1`, `iconlab`, …) self-update its agent
checkout — so this single command is also the rollout vehicle for agent-only
changes.

```bash
python scripts/blueprint.py deploy --dry-run   # show what would ship
python scripts/blueprint.py deploy             # confirm interactively
python scripts/blueprint.py deploy --yes       # scripted
python scripts/blueprint.py deploy --sha 74bf87c  # a specific main build
```

The client finds the newest successful "Deploy Blueprint" run on `main`,
downloads its `local-app-release.json` + `.sha256` artifact, verifies the
checksum, and invokes the host-side shim over SSH.

## Host-side shim and root release service (one-time install)

The local-app bootstrap installs the shim root-owned together with an
ACL-limited socket unit and root service. The deploy agent submits its
approval-bound digest and revision over that socket; no sudoers grant is used
by the hardened agent:

```bash
# from a checkout at the reviewed commit
sudo env -i SUDO_USER=henry_hamster PATH=/usr/sbin:/usr/bin:/sbin:/bin \
  /var/lib/blueprint/bootstrap/local-app-release/bootstrap_local_app_release.py \
  --revision FULL_GIT_SHA
```

## Why the passwordless rule is safe

The trust chain never depends on the caller:

1. **The shim validates everything.** Exactly two arguments, matched against
   anchored regexes (`sha256:` digest, 40-hex revision). The registry,
   project, repository, and image name are hardcoded in the root-owned shim —
   a caller cannot aim the release at a different registry or image.
2. **Only CI can mint deployable artifacts.** The pinned Artifact Registry
   repository is writable only by the GitHub workload-identity service
   account. Nothing an operator account can do creates a new deployable image.
3. **The release tool re-verifies independently.** The shim execs the newest
   root-installed immutable `local_app_release.py`, which checks the image
   digest and revision label, static-tree manifest, service ownership, and
   post-start health — and rolls back a failed activation.
4. **Residual exposure is bounded.** A compromised service account can request
   any image CI has previously built (including an older one — a rollback,
   which the revision receipts record), but cannot replace the root-owned shim
   or deploy non-CI code. The socket ACL is the execution boundary.

Migrations are part of this path. The release tool compares the candidate
image's baked schema revision with the live database head. Matching revisions
use the normal hot-swap. Before stopping either app unit for a mismatch, the
tool verifies that the candidate's bundled Alembic graph reaches the target
from the live head. A non-forward or unreachable target is refused with both
app units still running; use an image matching the database or fix forward.
Only a forward-reachable mismatch enters the bounded downtime window: the tool
stops both managed app units, waits for their containers to stop, runs the
canonical migration transaction with the candidate image, verifies the
resulting schema head, and starts the worker before the API. If the migration
choreography fails after the window begins, the app units remain stopped and
the release must be fixed forward; the tool does not roll older code back
across a possibly advanced schema. Operators do not run migrations separately,
but must treat the deploy as a maintenance window and investigate or fix
forward if it fails during migration.

## Host-side deploy agent (one-time install)

An operator can also activate an approved promotion in-product instead of
hand-running `blueprint deploy`: the backend stamps an approval, and the
durable host-side poller detects the pending binding, submits it to the same
root-owned release socket, and posts the activation receipt. The agent adds
reachability, not privilege: it is bounded by the ACL-limited socket and
`/usr/local/sbin/blueprint-release` execution boundary above, and it refuses
(waits conservatively) on any binding that does not resolve to exactly the
approved version's immutable `promoted_sha`.

The `bootstrap_local_app_release.py` command in the preceding section is the
sole installer for this agent and for
`/etc/systemd/system/blueprint-deploy-agent.service`. It renders the unit from
the reviewed programmatic renderer, installs the immutable bundle that the
unit executes, verifies the units with `systemd-analyze`, and enables the
service. Do not copy a unit template or install a second standalone agent.
To install or refresh the agent, run that bootstrap command again with the
full reviewed revision; its output identifies the installed bundle directory.

After bootstrap, run the preflight from that bundle as `henry_hamster` (replace
`<bundle>` with the directory printed by bootstrap):

```bash
/usr/bin/python3.12 \
  /usr/local/lib/blueprint-node/<bundle>/blueprint.py \
  deploy-agent --preflight
```

### How the agent works

Each poll cycle the agent:

1. Reads the project's latest promoted release version
   (`release-versions?status=promoted&limit=1` — the same endpoint the CLI
   reads).
2. Treats it as a **pending approved activation** only when
   `activation_requested_at` is set and `serving_status` is still
   `unconfirmed` — the same predicate `POST /approve-activation` requires
   before it will stamp an approval. Never-approved, never-armed (`NULL`),
   and already-resolved versions are skipped.
3. Resolves the approved artifact **exactly**: it locates the CI deploy run for
   the version's immutable `promoted_sha`, downloads + checksum-verifies the
   manifest, and **refuses** unless the manifest's built revision equals that
   `promoted_sha`. A descendant or unrelated revision is never activated — an
   approval cannot authorize a different or historical artifact.
4. Re-runs the shared binding check (`_resolve_deploy_binding`) and refuses if
   a newer promotion superseded the approved version since polling.
5. Sends `<digest> <revision>` to `/run/blueprint-release.sock` locally (no SSH
   and no sudo — the agent runs ON the control-plane host), then posts the
   activation receipt.

Serialization is an `flock` on `/run/blueprint-deploy-agent/deploy-agent.lock`
and receipt-retry state is stored at
`/var/lib/blueprint-deploy-agent/deploy-agent.state`; both directories are
owned by systemd for `henry_hamster`. Overlapping agent restarts never run two
deploys at once. Transient control-plane failures are logged and retried at the
poll cadence; the agent never crashes a poll cycle into the service.

### Observability

- **Status / receipts:** `journalctl -u blueprint-deploy-agent.service -f`. Each
  cycle logs `pending approved activation: vN (promoted …)`, `activation
  complete for vN (…)`, or a `REFUSING`/`skipping` line with the reason.
- **Serving confirmation:** the in-product version history reflects the receipt
  (`serving_status=serving_confirmed` + `served_sha`).
- **One-shot cycle (debug):**
  `/usr/local/lib/blueprint-node/<bundle>/blueprint.py deploy-agent --once --project-id <id>`

### Recovery and manual rollback

- **Agent is dead / won't start:** run `deploy-agent --preflight` from the
  installed bundle for the preflight verdict (missing config → create
  `~/.blueprint-config.json`; missing shim → re-run the bootstrap above). Then
  `sudo systemctl reset-failed blueprint-deploy-agent.service` (clears the
  StartLimitBurst cap) and
  `sudo systemctl restart blueprint-deploy-agent.service`.
- **A cycle fails conservatively:** the agent logs the reason and backs off; it
  never posts a receipt for a failed activation. If the release succeeds but
  receipt delivery fails, a host-local marker retries only the receipt and
  never invokes the release executor again for that approval. The approval
  stays pending in the backend, so the next healthy cycle retries it. An
  operator can always hand-run `python scripts/blueprint.py deploy` instead.
- **Manual rollback:** run `python scripts/blueprint.py deploy --sha
  <earlier-main-sha> --force` when an operator must select the earlier image.
  The release tool permits the rollback only when that image has the same
  schema as the live database (a hot swap); an older or unreachable schema is
  refused before either app unit is stopped, and the operator must fix forward
  or choose a schema-compatible image. The receipt records the attempt. The
  bootstrap removes the legacy `/etc/sudoers.d/blueprint-release` grant during
  installation; no manual
  sudoers revocation is needed. The active release executor is the root-owned
  socket service described above.
