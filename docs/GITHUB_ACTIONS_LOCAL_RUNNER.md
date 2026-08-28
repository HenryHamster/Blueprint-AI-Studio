# Local GitHub Actions runner role

GitHub workflow code is not a safe peer of the Blueprint worker or host Docker
daemon. A workflow with Docker access is effectively root on the Docker host.
Blueprint therefore runs Actions only inside the KVM boundary managed by
`scripts/install-github-actions-vm.sh`.

`scripts/github_actions_runner.py` is the guest-side role planner. It only
creates a reviewable plan; it does not download a runner, contact GitHub,
accept a token, or change workflow routing. Its rendered units contain a
`ConditionPathExists` gate for the KVM guest bootstrap marker, so they cannot be
started directly on an ordinary Blueprint node.

Generate a trusted internal-CI plan inside the guest:

```bash
python3 scripts/github_actions_runner.py plan \
  --name ci --url https://github.com/example-org --safety trusted
```

The output contains the role-specific Unix user, state path, environment path,
systemd unit, and install/status/drain/uninstall instructions. Its install plan
first verifies the guest marker, then creates or verifies the role-scoped Unix
user and group; do not substitute an existing shared CI account. Registration
tokens are read from a root-owned file only while `config.sh` is run; they are
never emitted in the plan, environment file, or unit.

## Safety profiles

`trusted` is for repositories and workflows allowed to use the guest's
explicitly granted CI credentials. It can be persistent or ephemeral. Docker is
disabled by default. If a workflow needs Docker, configure a dedicated socket
inside the guest; the tool rejects `/var/run/docker.sock` and
`/run/docker.sock`. The production VM profile provides four isolated guest
Docker daemons so concurrent jobs do not share names, networks, caches, or
cleanup.

`untrusted` is for fork/public work and is ephemeral-only. It gets a separate
Unix account, state root, env file, and systemd unit. The rendered unit denies
both conventional Docker sockets and removes access to homes, devices, kernel
controls, capabilities, production-like credentials, and common cloud
metadata-service addresses. Do not put production secrets in its role
environment. This is defense in depth inside the VM. Fork PRs remain excluded
from the persistent internal runner pool unless the repository explicitly
adopts ephemeral disposable guests.

## KVM host lifecycle

The host runtime uses two containers:

- a small network namespace sidecar with only `NET_ADMIN`, no host mounts or
  devices, and deny rules for host, LAN, link-local, metadata, multicast, and
  IPv6 destinations;
- a capability-free QEMU process with access only to `/dev/kvm` and the
  role-scoped VM disk directory.

The guest SSH port is published only on host loopback. The Ubuntu cloud image
and host container base are digest-pinned, and the built host image identity is
recorded before start.

```bash
export BLUEPRINT_ACTIONS_VM_STATE="$HOME/.blueprint/github-actions-vm"
./scripts/install-github-actions-vm.sh plan
./scripts/install-github-actions-vm.sh prepare
./scripts/install-github-actions-vm.sh start
./scripts/install-github-actions-vm.sh status
./scripts/install-github-actions-vm.sh guest-status
```

### Durable host control

An existing prepared guest can be bound to a user systemd service without
rebuilding or replacing its overlay. Select the canonical state directory and
the dedicated guest key, then install the control profile from the durable
Blueprint checkout:

```bash
export BLUEPRINT_ACTIONS_VM_STATE=/srv/data/containers/blueprint/github-actions-vm
export BLUEPRINT_ACTIONS_VM_SSH_KEY="$HOME/.ssh/blueprint-actions-vm.pub"
export BLUEPRINT_ACTIONS_VM_SSH_PRIVATE_KEY="$HOME/.ssh/blueprint-actions-vm"
./scripts/install-github-actions-vm.sh install-control
```

`install-control` verifies the existing state, host image identity, KVM device,
and non-symlinked key files. It writes only paths and resource settings to the
mode-0600 `~/.blueprint/github-actions-vm-control.env`, installs and enables the
user unit, and deliberately does not start or restart the guest. The invoking
checkout path is recorded in the profile, so run it from the durable checkout,
not an expendable worktree.

After proving that no Actions job is active, the installed control surface is:

```bash
systemctl --user start blueprint-actions-vm.service
systemctl --user status blueprint-actions-vm.service
systemctl --user stop blueprint-actions-vm.service
```

The service uses the launcher's `recover` action. Recovery is idempotent when
both containers are running. It can remove and recreate only stopped known
containers; a running guest without its firewall sidecar, or any paused,
restarting, or removing state, fails closed for operator diagnosis. The
overlay, seed, and base image remain regular-file-only inputs under the
canonical state root.

The guest checks storage pressure every 15 minutes. Below 70% filesystem usage
and with at least 30 GiB free it makes no changes. Under pressure it handles
only the four allowlisted runner daemons. For each slot it freezes the runner
listener, proves that its exact systemd cgroup has no `Runner.Worker`, observes
zero running containers twice, and then asks Docker to prune anonymous,
unreferenced volumes. It always thaws the listener, including through a systemd
cleanup fallback if the helper is terminated. Named volumes, images, build
cache, containers, networks, the host Docker daemon, and Blueprint application
or database data are never pruned. Ambiguous state fails closed and is logged.

Install or refresh this maintenance policy on an existing running guest without
stopping runners or touching active jobs:

```bash
export BLUEPRINT_ACTIONS_VM_STATE="$HOME/.blueprint/github-actions-vm"
./scripts/install-github-actions-vm.sh install-maintenance
./scripts/install-github-actions-vm.sh maintenance-status
```

Runner registration is a separate cutover step using a short-lived GitHub
registration token in a mode-0600 file:

```bash
export BLUEPRINT_GITHUB_URL=https://github.com/example-org/example-repo
export BLUEPRINT_ACTIONS_REGISTRATION_TOKEN_FILE=/secure/path/registration-token
./scripts/install-github-actions-vm.sh register
```

The installer pins the official runner release and SHA-256. The registration
token is streamed into a root-only guest file and both copies are shredded
after successful enrollment. Keep the existing host runners online until all guest
runners are registered, online, and have completed representative CI. Drain
host runners before stopping them; never cancel active jobs as part of the
migration.

## Workflow routing and credentials

Repository CI targets the complete label set
`[self-hosted, Linux, X64, hamsterwheel, blueprint, kvm]`. The `kvm` label is a
hard routing fence: an old host registration must never receive repository
code even if it is accidentally re-enabled. Browser tests run in the
version-pinned Playwright container through the runner's isolated Docker
daemon, so the persistent guest account does not need sudo or browser packages
installed on the guest.

Only credential-free deploy detection and Packer validation move to this
persistent pool. Terraform plans, image baking, migrations, and production
deployments continue on fresh GitHub-hosted runners because they receive GCP
credentials. Do not route credential-bearing workflows to a persistent runner
that also executes pull-request code until a disposable or cryptographically
reset trusted-runner boundary exists.

## Draining

First disable the runner or remove its routing label in GitHub so it receives no
new jobs. Wait for the active job to finish, then stop the systemd unit. Do not
use `systemctl stop` as a job-cancellation mechanism. The generated plan lists
the exact unit and paths for that role.
