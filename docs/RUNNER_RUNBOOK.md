# Blueprint Self-Hosted Runner Runbook

This guide covers enrolling and operating Blueprint fleet runners on the four
supported device shapes. For each shape it states the enroll/start steps,
the expected `blueprint-runner check` output, and common failure fixes.
A multi-runner registration and observability section follows at the end.

---

## Prerequisites (all shapes)

Every device shape needs the same two host prerequisites before the runner
can execute agent tasks. Install them first:

| Prerequisite | Minimum | Install |
|---|---|---|
| Node.js | v22 | https://nodejs.org |
| Claude Code CLI | latest | `npm install -g @anthropic-ai/claude-code` |
| Codex CLI | 0.144.1 | `npm install -g --include=optional @openai/codex@0.144.1` |

The runner binary itself is part of the repo (no separate install):

```bash
# Clone the repo (or pull latest) on the machine that will run the runner
git clone https://github.com/<your-org>/blueprint.git
cd blueprint

# Symlink blueprint-runner into ~/.local/bin for bare-command use
bash scripts/install-cli.sh

# If ~/.local/bin is not on PATH yet, add to shell rc:
export PATH="$HOME/.local/bin:$PATH"
```

The `blueprint-runner` CLI wraps `scripts/blueprint_runner.py`. On Windows /
PowerShell (where the bash wrapper does not work), call the script directly:
`py -3 scripts/blueprint_runner.py start --token <token>`.

---

## Isolation policy: bwrap-first

Blueprint enforces **fail-closed isolation**: every task runs inside an
isolation boundary or is declined. There is no bare/unsandboxed path.

The preferred boundary is **bubblewrap (bwrap)** — rootless, no daemon, minimal
trust surface. A container backend (docker, podman, apptainer, enroot) is used
only when required:

1. **Image-native compute** — the job supplies a `container_image` (e.g. an NGC
   CUDA image); bwrap cannot fabricate the image's userspace.
2. **bwrap unavailable** — the host kernel has unprivileged userns disabled (a
   hardened or shared HPC kernel). apptainer/enroot become the necessity.
3. **Opt-in prod-parity agent** — `BLUEPRINT_AGENT_BACKEND=docker` (explicit
   operator choice); runs agent tasks in the prod `agent/Dockerfile` image for
   exact Cloud Run parity.

See `scripts/runner_isolation.py` for the full decision tables
(`decide_agent_backend`, `decide_compute_backend`).

### GPU probe image on restricted networks

Before dispatching an image-backed GPU job, the runner proves that its selected
container runtime can expose a GPU by running `nvidia-smi -L`. By default this
uses a public CUDA image. If the runner is allowed to pull project/private
registry images but Docker Hub is blocked, configure a pullable image that
includes `nvidia-smi` before starting the runner:

```bash
export BLUEPRINT_RUNNER_GPU_PROBE_IMAGE='registry.example/project/cuda:stable'
blueprint-runner start --token <token>
```

The runner uses this image on startup and on its bounded retry cadence; ensure
the runner's container runtime is authenticated to that registry.

---

## 1. WSL PC (developer laptop, WSL2)

### Isolation profile

| Capability | Expected state |
|---|---|
| userns / bwrap | ✓ — WSL2 kernel enables unprivileged namespaces by default |
| docker | maybe present (Docker Desktop); treated as opt-in |
| GPU | usually none; one consumer GPU if installed |

**Agent default:** bwrap (host toolchain).  
**Imageless compute:** bwrap.  
**Image compute:** docker/podman (necessity #1).

### Enroll and start

1. **Get a runner token** — In the Blueprint admin UI, open **Fleet → Enroll
   runner** for your project. Click **Enroll runner** (default preset). Copy the
   token displayed once.

2. **Install bubblewrap** (required for bwrap):
   ```bash
   # Ubuntu/Debian on WSL
   sudo apt-get update && sudo apt-get install -y bubblewrap
   ```

3. **Verify setup** (no work claimed):
   ```bash
   blueprint-runner check --token <token>
   ```

4. **Start the runner** (blocks; Ctrl-C for graceful shutdown):
   ```bash
   blueprint-runner start --token <token>
   ```

   Or export the token to keep it off the process argv:
   ```bash
   export BLUEPRINT_RUNNER_TOKEN=<token>
   blueprint-runner start
   ```

5. **Install as a durable service** (survives logout/reboot — recommended once
   `check` is green):
   ```bash
   blueprint-runner install --token <token>
   ```
   This renders the systemd `--user` unit from the shipped template with the real
   paths for *this* checkout, writes it to
   `~/.config/systemd/user/blueprint-runner.service`, runs `daemon-reload` +
   `enable --now`, and turns on `loginctl enable-linger` so the runner starts at
   boot with no active login session. Remove it with `blueprint-runner uninstall`.
   (WSL2 note: on Windows reboot the WSL distro is not auto-started; launch the
   distro once — e.g. open a WSL terminal or `wsl.exe -d <distro>` — and linger
   brings the runner up from there.)

### Expected `--check` output

```
blueprint-runner preflight
  orchestrator : https://blueprint-orchestrator-mmtfi5aysa-uc.a.run.app
  node         : [OK     ] v22.x
  claude       : [OK     ] /usr/local/bin/claude
  docker-agent : [OFF    ] docker unusable -> docker-agent path OFF. Agent tasks run under host bwrap (or are declined if bwrap is unavailable).

Blueprint local fleet runner — preflight
  runner host      : <hostname>
  orchestrator     : https://... [reachable]
  runner token     : <prefix>... [valid]
  python           : 3.x.x
  workspace        : /tmp/blueprint-agent-workspaces [writable]
  concurrency      : agents=2  compute=2  (separate pools; up to 4 tasks total)
  isolation boundaries:
    bwrap        : /usr/bin/bwrap [userns: yes] -> usable
    apptainer    : not installed
    docker       : not installed
    podman       : not installed
    resource cap : none (resource caps best-effort via rlimit only)
  gpus             : 0x [none]
  gpu policy       : allowlist=[none]  per_job=1
  task classes:
    agent        : CLAIMED via bwrap
    compute      : CLAIMED via bwrap (imageless + image-downgrade)
```

Even if Docker Desktop is installed and the daemon is running, the docker-agent
line stays `[OFF]` by default — bwrap is always the default agent backend. To
enable the prod-image docker-agent path, pass `--docker-agent`:
```bash
blueprint-runner start --token <token> --docker-agent
# or: BLUEPRINT_AGENT_BACKEND=docker blueprint-runner start --token <token>
```

### Common failure fixes

**`bwrap: not installed`**
```
bwrap: not installed
agent: DECLINED — no bwrap sandbox (install bubblewrap; kernel userns required)
```
Fix: `sudo apt-get install -y bubblewrap`

**`userns: NO`**
```
bwrap: /usr/bin/bwrap [userns: NO] -> UNUSABLE
```
WSL2 should support userns by default. Check WSL version:
```bash
wsl --version   # should show WSL 2
wsl --update
```
If on WSL1, upgrade: `wsl --set-version <distro> 2`.

**`node: not found` or version too old**
```
node: [MISSING] not found on PATH
 -> Install Node.js >= 22 (https://nodejs.org)
```
Install nvm and Node 22: `curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.0/install.sh | bash && nvm install 22`

**`orchestrator: UNREACHABLE`**
Check internet connectivity and that `BLUEPRINT_ORCHESTRATOR_URL` is not set to
a stale value: `unset BLUEPRINT_ORCHESTRATOR_URL`.

**`runner token: rejected`**
The token was revoked or is invalid. Re-enroll via **Fleet → Enroll runner**.

---

## 2. Second PC (native Linux operator box)

### Isolation profile

| Capability | Expected state |
|---|---|
| userns / bwrap | ✓ — typically enabled on operator-controlled distros |
| docker | often installed and usable |
| GPU | modest or none |

**Agent default:** bwrap.  
**Imageless compute:** bwrap.  
**Image compute:** docker (necessity #1).

### Enroll and start

1. **Get a runner token** — same as WSL PC above.

2. **Install bubblewrap**:
   ```bash
   # Debian/Ubuntu
   sudo apt-get install -y bubblewrap
   # Fedora/RHEL
   sudo dnf install -y bubblewrap
   # Arch
   sudo pacman -S bubblewrap
   ```

3. **Verify**:
   ```bash
   blueprint-runner check --token <token>
   ```

4. **Start**:
   ```bash
   blueprint-runner start --token <token>
   ```

   To keep the runner alive across reboots, install it as a durable service —
   one command renders the unit, enables it, and turns on boot persistence:
   ```bash
   blueprint-runner install --token <token>
   ```
   This is a systemd `--user` unit + `loginctl enable-linger` (starts at boot with
   no login session). Uninstall with `blueprint-runner uninstall`. See §7 of
   `docs/runner-install.md` for details and the token-persistence model.

### Expected `--check` output

```
blueprint-runner preflight
  orchestrator : https://blueprint-orchestrator-mmtfi5aysa-uc.a.run.app
  node         : [OK     ] v22.x
  claude       : [OK     ] /usr/local/bin/claude
  docker-agent : [OFF    ] docker-agent path OFF (bwrap is default; pass --docker-agent or set BLUEPRINT_AGENT_BACKEND=docker to enable the prod-image path).

Blueprint local fleet runner — preflight
  ...
  isolation boundaries:
    bwrap        : /usr/bin/bwrap [userns: yes] -> usable
    docker       : usable
  task classes:
    agent        : CLAIMED via bwrap
    compute      : CLAIMED via docker (image jobs, --nv/--gpus), bwrap (imageless + image-downgrade)
```

The docker-agent line is `[OFF]` by default even when docker is usable — bwrap
is always the default agent backend. To enable the docker-agent path (read-only
agent types in the prod image), pass `--docker-agent`:

```bash
blueprint-runner check --token <token> --docker-agent
# With docker-agent enabled and the image present:
#   docker-agent : [ON     ] image blueprint-agent:local already present -> docker-agent path ON.
#   agent        : CLAIMED via bwrap + docker image blueprint-agent:local
```

Note that `agent: CLAIMED via bwrap` handles write/heavy task types (create,
fix, refactor, etc.) even when docker is opted in. Docker is only used for
read-only types when the docker-agent path is active.

### Common failure fixes

**`userns: NO` on a hardened corporate distro**
```
bwrap: /usr/bin/bwrap [userns: NO] -> UNUSABLE
```
Check the sysctl: `cat /proc/sys/kernel/unprivileged_userns_clone` (0 = off).
Enable (requires root): `sysctl -w kernel.unprivileged_userns_clone=1`
Make persistent: add `kernel.unprivileged_userns_clone=1` to `/etc/sysctl.d/99-userns.conf`.

**Docker present but unusable (socket denied)**
```
    docker       : present but UNUSABLE (socket denied?)
```
Add the user to the docker group: `sudo usermod -aG docker $USER && newgrp docker`

**Multiple runners on one machine**
Run each runner in its own shell/service with a different token and a different
workspace root:
```bash
BLUEPRINT_RUNNER_TOKEN=<token2> \
BLUEPRINT_RUNNER_WORKSPACE_ROOT=/tmp/blueprint-runner-2 \
blueprint-runner start
```
Each runner auto-discovers its own workspace; the paths must not overlap.

---

## 3. DGX (shared, multi-tenant NVIDIA box)

The DGX is the decisive case: docker is on PATH but the socket is denied
(permission denied on `/var/run/docker.sock`). The usability probes correctly
classify it as **docker UNUSABLE**. Whether bwrap works depends on whether the
kernel admin has enabled unprivileged user namespaces.

### Isolation profile

| Capability | Expected state |
|---|---|
| userns / bwrap | depends on kernel config — ask your sysadmin |
| docker | present on PATH but **denied** — `docker info` exits non-zero |
| apptainer / enroot | often installed (HPC convention) |
| GPU | many high-end NVIDIA GPUs |

### Case A: userns enabled (bwrap✓)

**Agent default:** bwrap (bwrap is available, docker is denied).  
**Imageless compute:** bwrap (including GPU via `/dev/nvidia*` device binds).  
**Image compute:** apptainer/enroot if installed; bwrap fallback otherwise.

#### Enroll and start

1. **Get a runner token** from **Fleet → Enroll runner**.  
   For GPU compute, choose **Enroll runner** (default preset).

2. **Install bubblewrap** (if not already present; may need sysadmin):
   ```bash
   # Check first
   which bwrap
   bwrap --version

   # If missing, request sysadmin install or install from source into ~/bin
   # (requires no root once --enable-setuid is skipped and userns is on)
   ```

3. **Verify**:
   ```bash
   BLUEPRINT_RUNNER_TOKEN=<token> blueprint-runner check
   ```

4. **Start with GPU visibility** (assign all detected GPUs, or a subset):
   ```bash
   # All GPUs
   BLUEPRINT_RUNNER_TOKEN=<token> blueprint-runner start

   # Restrict to specific GPU indices (e.g. GPUs 0 and 1 only)
   BLUEPRINT_RUNNER_GPU_INDICES=0,1 BLUEPRINT_RUNNER_TOKEN=<token> blueprint-runner start
   ```

5. **Install as a durable service.** On a shared, multi-tenant box, prefer a
   **system** unit so the runner is boot-persistent and not tied to any one
   operator's login session:
   ```bash
   sudo blueprint-runner install --system --token <token>
   ```
   This writes `/etc/systemd/system/blueprint-runner.service` with
   `WantedBy=multi-user.target` and `User=$USER`, so no `enable-linger` is needed.
   Put `BLUEPRINT_RUNNER_GPU_INDICES` in `~/.blueprint/runner.env` if you restrict
   GPUs. On a single-operator box use the default `--user` install (no `--system`,
   no sudo). Uninstall with `sudo blueprint-runner uninstall --system`.

#### Expected `--check` output (bwrap✓, apptainer present)

```
blueprint-runner preflight
  ...
  docker-agent : [OFF    ] docker unusable -> docker-agent path OFF. Agent tasks run under host bwrap (or are declined if bwrap is unavailable).

Blueprint local fleet runner — preflight
  ...
  isolation boundaries:
    bwrap        : /usr/bin/bwrap [userns: yes] -> usable
    apptainer    : /usr/bin/apptainer
    docker       : present but UNUSABLE (socket denied?)
    podman       : not installed
    resource cap : /usr/bin/systemd-run (resource caps available)
  gpus             : 8x [0,1,2,3,4,5,6,7]
  gpu policy       : allowlist=[0,1,2,3,4,5,6,7]  per_job=1
  task classes:
    agent        : CLAIMED via bwrap
    compute      : CLAIMED via apptainer (image jobs, --nv/--gpus), bwrap (imageless + image-downgrade)
```

Key indicators to verify:
- `docker: present but UNUSABLE (socket denied?)` — expected on a shared DGX
- `bwrap: ... [userns: yes] -> usable` — required for agent work
- `gpus: Nx [0,1,...,N-1]` — all installed GPUs detected
- `agent: CLAIMED via bwrap` — correct; docker is denied

### Case B: userns disabled (bwrap✗)

When the kernel admin has disabled unprivileged userns (`/proc/sys/kernel/unprivileged_userns_clone` = 0), bwrap cannot build a sandbox.

**Agent default:** DECLINE (no boundary available; docker is denied).  
**Imageless compute:** apptainer/enroot (necessity #2 — bwrap unavailable, container is the only path).  
**Image compute:** apptainer/enroot.

#### Expected `--check` output (bwrap✗)

```
  isolation boundaries:
    bwrap        : /usr/bin/bwrap [userns: NO] -> UNUSABLE
    apptainer    : /usr/bin/apptainer
    docker       : present but UNUSABLE (socket denied?)
  task classes:
    agent        : DECLINED — no bwrap sandbox (install bubblewrap; kernel userns required)
    compute      : CLAIMED via apptainer (image jobs, --nv/--gpus)
```

In this case, the runner handles **compute jobs only** (image jobs via
apptainer/enroot). Agent tasks are declined. Route agent work to a different
runner (WSL PC, second PC, lab server) and use the DGX exclusively for compute.

Enrolling a compute-only runner:
```bash
# Enroll with default preset (compute still works without agent)
# Then restrict task_types on the runner row in Fleet panel
# to exclude agent task types if the orchestrator might pin them
BLUEPRINT_RUNNER_TOKEN=<token> blueprint-runner start
```

### Common failure fixes

**`agent: DECLINED` and userns is off**
Ask the DGX sysadmin to enable userns:
```bash
# Sysadmin runs:
sysctl -w kernel.unprivileged_userns_clone=1
# Persist:
echo 'kernel.unprivileged_userns_clone=1' >> /etc/sysctl.d/99-userns.conf
```
If the admin cannot enable it, use the DGX for compute only and route agent tasks elsewhere.

**`gpus: 0x [none]` when GPUs are present**
`nvidia-smi` is not on PATH or the user lacks permissions:
```bash
which nvidia-smi
nvidia-smi -L   # should list GPU 0: ... GPU 1: ...
```
If `nvidia-smi` is in a non-standard path, add it: `export PATH=$PATH:/usr/local/cuda/bin`.

**`compute: DECLINED — no bwrap sandbox and no usable container runtime`**
Neither bwrap nor apptainer/enroot is available. Install one:
- apptainer: https://apptainer.org/docs/admin/main/installation.html
- enroot: https://github.com/NVIDIA/enroot

**GPU jobs not dispatched to this runner**
The runner's capability advertisement is read by the orchestrator. Confirm:
```bash
blueprint-runner check --token <token>
# Verify: gpus: Nx [indices]
# Verify: compute: CLAIMED
```
Also check that the project's compute job specifies an image (`container_image`
field) or is imageless — only imageless jobs use bwrap; image jobs require a
container runtime.

---

## 4. Lab Server (operator-controlled GPU server)

### Isolation profile

| Capability | Expected state |
|---|---|
| userns / bwrap | ✓ — operator owns the box, can enable |
| docker | ✓ usable — operator owns the socket |
| GPU | present; operator owns device access |

This is the shape where **both** bwrap and docker fully work. Policy still
pins to bwrap-first by default; the docker-agent path is available as an
opt-in for prod-parity read-only agent runs.

**Agent default:** bwrap (write/heavy task types); docker image (read-only, opt-in).  
**Imageless compute:** bwrap.  
**Image compute:** docker/podman (necessity #1); GPU scoped via `--gpus device=<idx>`.

### Enroll and start

1. **Get a runner token** from **Fleet → Enroll runner**.

2. **Install bubblewrap** (if not already present):
   ```bash
   sudo apt-get install -y bubblewrap   # Debian/Ubuntu
   sudo dnf install -y bubblewrap       # Fedora/RHEL
   ```

3. **Verify**:
   ```bash
   blueprint-runner check --token <token>
   ```

4. **Start** (bwrap-first default — docker is used for image compute jobs):
   ```bash
   BLUEPRINT_RUNNER_TOKEN=<token> blueprint-runner start
   ```

   **Opt-in: docker-agent path** (read-only agent types run in prod image):
   ```bash
   # Build the prod image from agent/Dockerfile (first run only):
   blueprint-runner start --token <token> --docker-agent
   # The launcher builds blueprint-agent:local from agent/Dockerfile if absent.

   # Or pull a pre-built registry image (--pull implies docker opt-in):
   blueprint-runner start --token <token> --pull registry.example.com/blueprint-agent:latest

   # Or set the env var for a persistent docker-agent configuration:
   BLUEPRINT_AGENT_BACKEND=docker blueprint-runner start --token <token>
   ```

   **Override: force bwrap for all agent tasks** (ignore docker image even if opted in):
   ```bash
   BLUEPRINT_AGENT_BACKEND=bwrap blueprint-runner start --token <token>
   ```

   **GPU subset** (assign GPUs 2 and 3 to this runner, e.g. in a multi-runner setup):
   ```bash
   BLUEPRINT_RUNNER_GPU_INDICES=2,3 blueprint-runner start --token <token>
   ```

5. **Install as a durable service.** For a single-operator lab server, the
   default `--user` install is simplest; for a shared server use `--system`:
   ```bash
   blueprint-runner install --token <token>            # per-operator (systemd --user + linger)
   sudo blueprint-runner install --system --token <token>   # shared server (boot-persistent)
   ```
   Persistent knobs (`BLUEPRINT_RUNNER_GPU_INDICES`, `BLUEPRINT_AGENT_BACKEND`,
   etc.) belong in `~/.blueprint/runner.env`, which the installed unit loads.
   Uninstall with `blueprint-runner uninstall` (add `--system` for the system unit).

### Expected `--check` output (full capabilities, 4 GPUs)

Default (bwrap agent backend):
```
blueprint-runner preflight
  orchestrator : https://blueprint-orchestrator-mmtfi5aysa-uc.a.run.app
  node         : [OK     ] v22.x
  claude       : [OK     ] /usr/local/bin/claude
  docker-agent : [OFF    ] docker-agent path OFF (bwrap is default; pass --docker-agent or set BLUEPRINT_AGENT_BACKEND=docker to enable the prod-image path).

Blueprint local fleet runner — preflight
  runner host      : lab-server-01
  orchestrator     : https://... [reachable]
  runner token     : abcdef12... [valid]
  python           : 3.11.x
  workspace        : /tmp/blueprint-agent-workspaces [writable]
  concurrency      : agents=2  compute=2  (separate pools; up to 4 tasks total)
  isolation boundaries:
    bwrap        : /usr/bin/bwrap [userns: yes] -> usable
    apptainer    : not installed
    docker       : usable
    podman       : not installed
    resource cap : /usr/bin/systemd-run (resource caps available)
  gpus             : 4x [0,1,2,3]
  gpu policy       : allowlist=[0,1,2,3]  per_job=1
  task classes:
    agent        : CLAIMED via bwrap
    compute      : CLAIMED via docker (image jobs, --nv/--gpus), bwrap (imageless + image-downgrade)
```

With `--docker-agent` (prod-image path opted in):
```
  docker-agent : [ON     ] image blueprint-agent:local already present -> docker-agent path ON.
  ...
  task classes:
    agent        : CLAIMED via bwrap + docker image blueprint-agent:local
    compute      : CLAIMED via docker (image jobs, --nv/--gpus), bwrap (imageless + image-downgrade)
```

Key indicators:
- `bwrap: ... [userns: yes] -> usable` — bwrap sandbox is active (default agent backend)
- `docker: usable` — docker daemon reachable (used for image compute; opt-in for agent)
- `agent: CLAIMED via bwrap` — bwrap handles all agent types by default
- `gpus: 4x [0,1,2,3]` — all GPUs detected and available

### Common failure fixes

**GPU indices not showing**
```bash
nvidia-smi -L
# Expected: GPU 0: NVIDIA A100 ...   GPU 1: ...
```
If `nvidia-smi` lists them but `--check` shows `0x [none]`, ensure the runner
user has access to the nvidia devices (`ls -la /dev/nvidia*`).

**`docker: present but UNUSABLE (socket denied?)`**
```bash
docker info   # should succeed
sudo usermod -aG docker $USER && newgrp docker
```

**Agent tasks routed to docker backend unexpectedly (write types in docker)**
Write/heavy types (create, fix, refactor) cannot run in the docker backend
because they need a writable workspace. If they arrive at the docker runner,
the decision table routes them to bwrap. Confirm bwrap is usable:
```
agent: CLAIMED via bwrap + docker image ...
```
The `bwrap` portion handles the write types. If only `docker image` is listed,
bwrap is not available and write types will be declined. Install bubblewrap.

**`docker build` fails when using `--docker-agent`**
When `--docker-agent` is passed, the launcher builds `blueprint-agent:local` from
`agent/Dockerfile` on first `start`. If the build fails:
```bash
cd agent && docker build -t blueprint-agent:local .
```
Or use `--no-build-image` to skip the build (the image must already be present):
```bash
blueprint-runner start --token <token> --docker-agent --no-build-image
```
Omitting `--docker-agent` entirely is the simplest option — bwrap handles all agent tasks.

---

## Multi-Runner Registration and Observability

### Registering multiple runners

Blueprint supports multiple runners per project — for example, three runners
across a lab server, a DGX, and a developer PC. Each is a separate enrollment.

**Via the admin UI (recommended):**
1. Open **Fleet** in the admin sidebar.
2. Click **Enroll runner** (standard) or **Strategic runner** (strategic
   task types only, `max_concurrent=1`).
3. Copy the token and follow the per-shape steps above.
4. Repeat for each machine.

**Via the API** (for automation):
```bash
curl -X POST \
  "https://your-orchestrator/api/admin/projects/<project_id>/runners/register" \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"hostname": "lab-server-01", "preset": "default"}'

# Response (token shown ONCE — store securely):
# {"runner_id": "...", "token": "blpr_..."}
```

Preset options (`preset` field):
- `"default"` — all task types, `max_concurrent=2`
- `"strategic_local"` / `"strategic"` — strategic task types only, `max_concurrent=1`,
  used for the strategist session runner (`strategic_review` and `design_doc_update`)

For custom capability overrides (e.g. GPU-only, specific task types), pass
`capabilities`:
```bash
curl -X POST "https://.../runners/register" \
  -H "Authorization: Bearer $ADMIN_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "hostname": "dgx-compute",
    "capabilities": {"has_gpu": true, "max_concurrent": 4}
  }'
```

**Token security:** The plaintext token is returned exactly once and is never
stored (only its SHA256 hash is persisted). If you lose it, rotate via:
- UI: **Fleet → runner row → Rotate token**
- API: `POST /api/admin/projects/{project_id}/runners/{runner_id}/rotate-token`

### Observability in the admin UI

The **Fleet** panel (admin sidebar) shows each enrolled runner with:

| Column | Meaning |
|---|---|
| Status | `connected` (live, heartbeat fresh) · `failing` (live but jobs failing) · `stale` (heartbeat > 5 min old) · `offline` |
| Last seen | Time of most recent heartbeat |
| Active work | Count of running tasks + compute jobs |
| Available slots | `max_concurrent - active_work` |
| Capabilities | GPU flag, task-type restrictions, concurrency |

**Liveness thresholds** (from source):
- **connected**: heartbeat within 300 s
- **stale**: heartbeat 300–600 s ago
- **offline**: > 600 s or `status != active`

**Degraded state** (`failing` label) — a runner is degraded when:
- It fails ≥ 3 consecutive jobs (`_FAILURE_STREAK_DEGRADED_THRESHOLD`), or
- It has active work, at least one recorded failure, and no successful job
  in the past 30 minutes (`_SUCCESS_STALE_SECONDS = 1800`).

A degraded runner still heartbeats and is counted as live for capacity
purposes, but the `failing` label signals an operator investigation.

### Rotating or revoking runners

```bash
# Rotate (invalidate old token, get new one)
curl -X POST "https://.../runners/<runner_id>/rotate-token" \
  -H "Authorization: Bearer $ADMIN_API_KEY"
# Response: {"token": "blpr_...", ...}

# Revoke (soft-delete; runner stops receiving work)
curl -X DELETE "https://.../runners/<runner_id>" \
  -H "Authorization: Bearer $ADMIN_API_KEY"
```

Via UI: **Fleet → runner row → Rotate token** or **Revoke**.
After revoking, restart any live runner process using the old token — it will
receive 401s and stop polling (by default it exits after 20 consecutive 401s;
see `BLUEPRINT_RUNNER_MAX_AUTH_FAILURES`).

### Partitioning work across multiple runners

To direct specific task types to specific machines:

1. Enroll each runner.
2. In **Fleet → runner row → capabilities**, set `task_types` to the desired
   subset (e.g. `["compute_experiment"]` for a compute-only machine, or
   `["research", "web_research", "design"]` for a research-only runner).

The orchestrator intersects `task_types` with the runner's advertised
capabilities at dispatch time. A runner that reports no GPU will not be
dispatched GPU compute jobs.

### Running multiple runners on one machine

Permissible for higher concurrency. Each process needs a distinct workspace:

```bash
# Runner 1 (port-0 is fine; workers are pull-only)
BLUEPRINT_RUNNER_TOKEN=<token1> \
BLUEPRINT_RUNNER_WORKSPACE_ROOT=/tmp/blueprint-ws-1 \
BLUEPRINT_RUNNER_MAX_CONCURRENT=2 \
blueprint-runner start &

# Runner 2
BLUEPRINT_RUNNER_TOKEN=<token2> \
BLUEPRINT_RUNNER_WORKSPACE_ROOT=/tmp/blueprint-ws-2 \
BLUEPRINT_RUNNER_MAX_CONCURRENT=2 \
BLUEPRINT_RUNNER_GPU_INDICES=2,3 \
blueprint-runner start &
```

Key points:
- Each runner uses separate, non-overlapping `BLUEPRINT_RUNNER_WORKSPACE_ROOT` directories.
- Partition GPU indices via `BLUEPRINT_RUNNER_GPU_INDICES` so jobs on runner 1
  use GPUs 0,1 and jobs on runner 2 use GPUs 2,3.
- Each token maps to a distinct runner row in the database; liveness and telemetry
  are tracked per-row.

### Troubleshooting runner connectivity

**Runner shows `stale` or `offline` in Fleet panel**
```bash
# On the runner machine — first triage step: check the on-box liveness file.
# Its mtime is the last time the runner process wrote anything; its contents
# show the last known state even if the process is dead (OOM, SIGKILL).
cat ~/.blueprint/status.json

# Verify connectivity and token validity with the live check:
blueprint-runner check --token <token>
# Observe orchestrator reachability and token validity lines
```

**Heartbeat rejected (401 in logs)**
```
[runner] WARNING: initial heartbeat returned 401 — token may be stale or the runner row inactive; entering poll loop and retrying
```
This is non-fatal — the runner enters its poll loop and the heartbeat re-arms
the row. If it persists, rotate the token.

**Tasks arrive but immediately decline**
Check `--check` output: `agent: DECLINED` or `compute: DECLINED` means an
isolation boundary is missing. Install bubblewrap (for agent/imageless compute)
or apptainer/enroot (for image compute on a no-userns host).

**Task hangs with no logs**
The runner's reaper terminates subprocesses that exceed the wall-clock deadline
(`BLUEPRINT_RUNNER_AGENT_MAX_SECONDS`, default 4 hours). Check logs on the runner
machine for `[runner] reaping overdue task`.

**Low-disk advisory in Fleet panel**
The runner reports disk usage each heartbeat. A `low_disk` flag appears when the
workspace volume has < 2 GiB free.

Per-task agent workspaces are now **auto-removed when each task finishes** (the
reaper rmtree's the task's workspace directory), so they no longer accumulate
under normal operation. To keep finished workspaces for post-mortem debugging,
set `BLUEPRINT_RUNNER_KEEP_WORKSPACES=1` — but remember to clean them up
manually, since that opts out of auto-clean:
```bash
rm -rf /tmp/blueprint-agent-workspaces/*/
```

As an extra safety net, set `BLUEPRINT_RUNNER_MIN_DISK_BYTES` (bytes) to make the
runner stop claiming new agent tasks while free space on the workspace filesystem
is below that threshold, instead of starting work that dies mid-run on a full disk:
```bash
# Refuse new agent work when < 2 GiB free
BLUEPRINT_RUNNER_MIN_DISK_BYTES=2147483648 blueprint-runner run
```

---

## Crash Recovery and Process Supervision

The runner self-updates when the orchestrator advertises a newer version (while
the runner is idle). After a successful update — or after a crash — the process
must be restarted to resume work. Blueprint handles this through process
supervision. **Running the runner unsupervised means crashes are permanent
outages until an operator notices.**

### Why a supervisor is required

Two events require an external restart:

1. **Crash / unexpected exit** — any unhandled exception, OOM kill, or
   hardware fault exits the runner process. Without a supervisor, the runner
   stays dead.
2. **Self-update** — when the orchestrator pushes a new runner version, the
   runner pulls the update and then exits with code 0 (supervised mode) so the
   supervisor can restart it with the new code. Without a supervisor the runner
   re-execs in place (best-effort), but a crash mid-re-exec has no fallback.

Background threads (the reaper, the heartbeat loop, the thread supervisor) are
individually guarded; if one dies unexpectedly the runner calls `sys.exit`
non-zero so the process supervisor can restart a clean copy. This design
intentionally escalates silent degradations to recoverable restarts.

### Option 1: systemd service (recommended)

`blueprint-runner install` renders and installs a systemd unit with
`Restart=always` for you. For the common shapes:

```bash
# Per-operator unit (WSL PC, second PC, single-operator lab server):
blueprint-runner install --token <token>
# Writes ~/.config/systemd/user/blueprint-runner.service
# Enables linger (boot persistence without active login session)

# System-wide unit (shared DGX / multi-operator server, requires sudo):
sudo blueprint-runner install --system --token <token>
# Writes /etc/systemd/system/blueprint-runner.service
```

The installed unit includes `Restart=always` and `RestartSec=5`. The runner
exits 0 after a successful self-update and systemd restarts the fresh binary;
crashes (non-zero exit) are also restarted. `Restart=always` is required, not
`Restart=on-failure`: the post-update exit is a *clean* exit-0, which
`on-failure` would NOT restart, silently leaving the self-updated runner dead
until a manual restart. A deliberate `systemctl --user stop` still stays stopped
(systemd never applies the restart policy to an operator-initiated stop). A burst
cap (`StartLimitIntervalSec=60` / `StartLimitBurst=3`) prevents a persistent
failure from restarting forever.

To view logs: `journalctl --user -u blueprint-runner -f`  
To restart manually: `systemctl --user restart blueprint-runner`  
To uninstall: `blueprint-runner uninstall` (add `--system` for the system unit)

### Option 2: manual restart loop (tmux / nohup / screen)

When systemd is not available, wrap the runner in a shell restart loop. Set
`BLUEPRINT_RUNNER_SUPERVISED=1` so the runner knows it is supervised and exits
cleanly on self-update (instead of re-exec'ing in place):

```bash
# Inside a tmux pane or screen session:
export BLUEPRINT_RUNNER_TOKEN=<token>
export BLUEPRINT_RUNNER_SUPERVISED=1
while true; do
    python3 scripts/local_runner.py
    sleep 1
done
```

Without `BLUEPRINT_RUNNER_SUPERVISED=1`, the runner detects no supervisor and
falls back to `os.execv` in place on self-update. That works for updates, but
a genuine crash still exits with no restart. Always set the variable in the
loop.

### Supervisor detection (`BLUEPRINT_RUNNER_SUPERVISED`)

At startup and before each self-update, the runner calls `_detect_process_supervisor()`
to determine whether a restart is available. It checks (first match wins):

| Signal | Detected supervisor |
|---|---|
| `INVOCATION_ID` or `JOURNAL_STREAM` env var | `systemd` |
| `SUPERVISOR_ENABLED` env var | `supervisord` |
| `BLUEPRINT_RUNNER_SUPERVISED=1` env var | `declared` (manual restart loop) |
| `/proc/1/comm` is `systemd`, `supervisord`, `runit`, `runsv`, `s6-svscan`, or `tini` | container init supervisor |

When a supervisor is detected, self-update exits with code 0 and lets the
supervisor respawn. When **no** supervisor is detected, self-update falls back
to `os.execv` in place and prints:

```
[runner] auto-update complete — NO process supervisor detected
(manual tmux/nohup/screen launch); re-exec'ing in place so the update
takes effect. Wrap the runner in systemd or a restart loop to recover
from crashes too (see --check RUNNER_RUNBOOK).
```

### `--check` supervisor warning

`blueprint-runner check` runs `_detect_process_supervisor()` and warns when no
supervisor is found:

```
supervisor : [WARN] no process supervisor detected — crashes will not auto-recover.
             Wrap the runner in systemd (blueprint-runner install) or a restart loop
             (while true; do ...; done with BLUEPRINT_RUNNER_SUPERVISED=1).
```

When a supervisor is detected, `--check` reports it (the `Restart=always`
reminder appears for systemd because the detector confirms a service unit but
cannot read its `Restart=` policy — the installed unit sets `always`):

```
supervisor : systemd (auto-restart on crash/update — needs Restart=always (blueprint-runner install sets it)) [systemd service cgroup (blueprint-runner.service)]
```

This warning is advisory — `--check` still exits 0 if isolation and token are
valid. But running long-term without a supervisor is not recommended for
production use.

---

## Quick-reference: `--check` field glossary

| Field | What to look for |
|---|---|
| `orchestrator` | `[reachable]` — network path is open |
| `runner token` | `[valid]` or `[401 — heartbeat re-arms]` — both are OK |
| `bwrap` | `[userns: yes] -> usable` — required for agent + imageless compute |
| `docker` | `usable` (lab server) · `present but UNUSABLE (socket denied?)` (DGX) · `not installed` |
| `apptainer` | path shown (HPC boxes) or `not installed` |
| `gpus` | `Nx [0,...,N-1]` — count and allowlist of detected NVIDIA GPUs |
| `agent: CLAIMED via bwrap` | bwrap sandbox handles agent tasks |
| `agent: CLAIMED via docker image ...` | docker handles read-only agent types |
| `agent: DECLINED` | no isolation boundary — install bubblewrap |
| `compute: CLAIMED via ...` | runtime shown; confirms compute jobs can run |
| `compute: DECLINED` | no sandbox; install bubblewrap or a container runtime |

**Exit codes:** `blueprint-runner check` exits 0 when the orchestrator is
reachable, the token is valid/401, and at least one task class is claimable.
Exit 1 means setup is incomplete.

---

## Troubleshooting: GitHub App installation token errors

These errors appear in agent task logs and indicate a problem with the GitHub
App installation's permissions or token validity — **not** something the runner
itself can fix. Do not attempt runner restarts or re-enrollment; escalate to
whoever manages the GitHub App installation for your organization.

**`Git push failed: credential helper at /home/agent/.blueprint/git-credential-helper provides read-only GitHub App installation token (ghs_ prefix) -- cannot push to origin.`**

The GitHub App installation token (`ghs_…`) is valid but was issued with
read-only repository permissions. The runner's credential helper correctly
fetched this token, but it lacks the `contents: write` scope needed to push.

_Remediation:_ In your GitHub organization's App installation settings, grant
the Blueprint GitHub App **Read and write** access to repository contents, then
re-run the affected task.

**`GitHub App installation token (ghs_***...) returns HTTP 401 Bad credentials for all push attempts and API calls -- the ghs_ token has expired.`**

The installation token has expired. GitHub App installation tokens are
short-lived (≤1 hour); if the token was cached or the runner's clock is skewed,
it can expire mid-task.

_Remediation:_ Verify that the runner host's system clock is accurate (NTP
synchronized). If expiry is happening frequently, check whether the GitHub App
installation itself is still active and that the App has not been suspended or
uninstalled. Do not attempt to have the runner self-heal an expired installation
token — re-running the task will cause the credential helper to fetch a fresh one.
