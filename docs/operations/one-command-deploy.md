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

Migrations remain deliberately outside this path: a release whose code expects
a newer schema fails closed at worker startup and rolls back. Run migrations
first (see `docs/local-control-plane.md`), then deploy.
