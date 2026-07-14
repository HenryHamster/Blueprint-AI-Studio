# Local Fleet Runner — Install and Service Setup

The local runner (`scripts/local_runner.py`) lets you attach your own machine to
Blueprint as a task executor. This guide covers enrolling a runner, persisting its
credentials, and wiring it into systemd so it survives reboots and restarts
automatically on crash.

---

## 1. Enroll a runner

In the admin UI go to **Settings → Runners → Register runner**. Choose a preset
(`default`, `strategic`, etc.) and click **Register**. Copy the one-time token
shown — it is never displayed again.

---

## 2. First launch (credential persistence)

Set the token and orchestrator URL for the first run:

```bash
export BLUEPRINT_ORCHESTRATOR_URL=https://your-orchestrator.example.com
export BLUEPRINT_RUNNER_TOKEN=<paste token here>
python3 scripts/local_runner.py
```

On startup the runner automatically saves the token to `~/.blueprint/runner_token`
(permissions `0600`). After this file exists you can omit `BLUEPRINT_RUNNER_TOKEN`
from your environment — the runner reads it from the credential file on every
subsequent start, so a reboot or crash-restart does **not** require re-enrolling
(which would create a new runner identity and lose history).

To check the saved credential location:

```bash
cat ~/.blueprint/runner_token
```

> **Security note:** `~/.blueprint/runner_token` is created with mode `0600` (owner
> read/write only). Do not store it in a world-readable location or commit it to git.

---

## 3. Install as a durable service (one command)

`blueprint-runner install` replaces the old copy-edit-reload dance. It **renders**
the systemd unit from the shipped template (`scripts/blueprint-runner.service`)
with the real absolute paths resolved on *this* host — the current Python
interpreter, this checkout's `local_runner.py`, and the `~/.blueprint/runner.env`
credential file — writes it to the right location, enables it, and turns on boot
persistence. There is nothing to hand-edit and no per-device special-casing.

### 3a. Default: systemd `--user` install

Runs under your own account without root; boot-persistent via linger.

```bash
blueprint-runner install --token <token>
```

This does all of the following for you:

- renders `~/.config/systemd/user/blueprint-runner.service` from the template
  (real `ExecStart=<python3> <abs path>/local_runner.py`, no placeholders);
- seeds `~/.blueprint/runner.env` (mode `0600`) with the orchestrator URL and, if
  you passed `--token`, the token — so the very first boot connects with no manual
  step. After that first start the token is persisted to `~/.blueprint/runner_token`
  and read from there, so it need not stay in the env file;
- runs `systemctl --user daemon-reload` and `systemctl --user enable --now
  blueprint-runner.service`;
- runs `loginctl enable-linger "$USER"` so the runner starts at boot even with no
  active login session;
- **bootstraps the agent's Node.js dependencies** (`npm ci` in `agent/`) as a
  best-effort step — so the runner can claim agent tasks immediately without
  a manual `cd agent && npm install` step. Pass `--no-agent-deps` to skip this
  (e.g. if you manage deps on your own cycle or have a compute-only runner).
  If `npm` is not on `PATH`, a clear warning is printed (the install still
  succeeds);
- verifies the unit is **active** and **enabled** and prints the `journalctl`
  follow command — it returns non-zero (and does not claim success) if any step
  fails.

Omit `--token` if you have already started the runner once and it persisted a
token to `~/.blueprint/runner_token`; the service will read it from there.
Override the URL with `--url` when you point at a non-production orchestrator.

### 3b. Shared boxes: `--system` install

On a multi-tenant machine (e.g. a shared DGX) prefer a **system** unit so the
runner is boot-persistent and not tied to any one operator's login session:

```bash
sudo blueprint-runner install --system --token <token>
```

This writes `/etc/systemd/system/blueprint-runner.service` with
`WantedBy=multi-user.target` and `User=$USER` (no linger needed — the system
target provides boot persistence).

### 3c. Preview or uninstall

```bash
blueprint-runner install --dry-run --token <token>   # print the rendered unit + commands, touch nothing
blueprint-runner uninstall                           # stop, disable, and remove the --user unit
sudo blueprint-runner uninstall --system             # remove the system unit
```

`uninstall` is idempotent (uninstalling when nothing is installed reports cleanly)
and never deletes your credential files under `~/.blueprint`.

> **No systemd?** On a host with no systemd manager (a bare container, a
> non-systemd init), `install` fails with an actionable message and prints the
> equivalent `nohup`/`tmux` command to run the runner under your own supervisor
> instead of crashing.

---

## 4. Restart-on-crash

The unit uses `Restart=always`, which restarts the runner whenever it exits, for
any reason. Two exit paths depend on this:

- **Crash / fatal condition** — the runner calls `os._exit(1)` (via its internal
  `_die()` helper) on a revoked token or a thread-supervisor failure; systemd
  relaunches it.
- **Self-update** — after pulling a newer version the runner exits `0` and trusts
  its supervisor to relaunch the fresh binary. `Restart=on-failure` would NOT
  restart that clean exit-0, silently leaving the self-updated runner dead until
  a manual restart — which is why the unit ships `Restart=always`.

A deliberate `systemctl stop` (or `Ctrl-C` in the foreground) still stays stopped:
systemd never applies the restart policy to an operator-initiated stop. A burst
cap (`StartLimitIntervalSec=60` / `StartLimitBurst=3`) stops a persistent failure
from restarting forever; run `systemctl reset-failed blueprint-runner.service`
after fixing the root cause.

Default restart delay is 5 seconds (`RestartSec=5`). Adjust in the unit file if
needed.

---

## 5. Self-update

When a new agent version is released, update the local checkout efficiently:

```bash
blueprint-runner update
```

This does all of the following:

- Fetches the latest refs from `origin` without merging yet;
- Checks whether the local branch is behind its upstream tracking branch;
- If already up to date, reports so and exits 0;
- If the local branch is **ahead** of upstream (unpushed local commits), **aborts**
  with a warning — push first to avoid losing work;
- If the local branch has **diverged** from upstream, **aborts** with a warning
  — rebase or reset first;
- If the local branch is cleanly **behind**, runs `git merge --ff-only` to
  fast-forward to the latest upstream commits;
- After the merge, checks every changed file: if a `package-lock.json` was
  modified, runs `npm ci` in **only** that project directory (e.g. `agent/` or
  `admin-ui/`), skipping the full `npm ci` for projects whose lockfile did not
  change — this makes repeated updates far faster than a naive `npm ci` on
  every update.

### No-token, no-teardown

Unlike `install`, `update` requires **no** orchestrator token — it's purely a
local git operation. The runner service **does not** need to be stopped first;
you can open a second terminal and run `update` while the runner keeps working.

### Failure modes

- :no_entry: **Not a git repository** — the working tree is a plain checkout
  with no `.git` or was installed from an archive (use a git clone instead).
- :no_entry: **No upstream tracking branch** — the current branch was never
  pushed (push once, then `update` works from then on).
- :no_entry: **Merge conflict** — a fast-forward was not possible (the merge
  was rejected without applying changes; none of your local files were touched).
- :warning: **`npm ci` failure** — logged as a warning (not fatal). The repo is
  updated; you may need to install deps manually.

---

## 6. View logs

```bash
# Live tail
journalctl --user -u blueprint-runner.service -f

# Last 100 lines
journalctl --user -u blueprint-runner.service -n 100

# Since last boot
journalctl --user -u blueprint-runner.service -b
```

---

## 7. Verify the runner is healthy

```bash
# Run the built-in preflight check (no work claimed)
python3 scripts/local_runner.py --check
```

Exit code `0` means the orchestrator is reachable, the token is valid, and at
least one task class is claimable. Non-zero means something is misconfigured —
read the preflight output for details.

`--check` reports **all** detected problems in one pass. If both
`BLUEPRINT_RUNNER_TOKEN` and `BLUEPRINT_ORCHESTRATOR_URL` are missing (and any
diagnostic probe fails), it prints the full list together and exits non-zero,
rather than stopping at the first missing variable — so you can fix everything
at once instead of one-fix-rerun at a time. (Normal boot without `--check`
still fails fast on the first missing prerequisite.)

The preflight now also checks for missing agent Node.js dependencies. If the
agent source (`agent/src/index.ts`) exists on this host but `npm install` was
never run, you will see:

```
    agent deps  : MISSING — run `cd agent && npm install` (or re-run
                  `blueprint-runner install`, which now bootstraps them).
```

This is an **actionable** warning — it prints only when agent source is detected
on the host but the `tsx` development-server entry point is unavailable. A
compute-only runner (no agent source) stays quiet.

---

## 8. Re-enrollment

If the runner token is revoked or lost and you cannot recover it from
`~/.blueprint/runner_token`, re-enroll via the admin UI (Settings → Runners →
Register runner). This mints a new token and a new runner identity. Run the new
token through the first-launch step above to persist it.
