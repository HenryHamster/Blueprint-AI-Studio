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

## 3. Install as a systemd --user service (Linux / WSL)

A systemd `--user` unit runs under your own account without root, starts at login
(or at boot with linger enabled), and restarts the runner automatically if it exits
non-zero.

### 3a. Copy and edit the unit file

```bash
mkdir -p ~/.config/systemd/user
cp scripts/blueprint-runner.service ~/.config/systemd/user/blueprint-runner.service
```

Edit `~/.config/systemd/user/blueprint-runner.service` and update `ExecStart` to
use the **absolute path** to `local_runner.py`:

```ini
ExecStart=/usr/bin/python3 /home/yourname/workspace/scripts/local_runner.py
```

### 3b. Create the environment file

```bash
mkdir -p ~/.blueprint
chmod 0700 ~/.blueprint
cat > ~/.blueprint/runner.env <<'EOF'
BLUEPRINT_ORCHESTRATOR_URL=https://your-orchestrator.example.com
# BLUEPRINT_RUNNER_TOKEN is optional after the first run — the runner reads it
# from ~/.blueprint/runner_token automatically.
EOF
chmod 0600 ~/.blueprint/runner.env
```

### 3c. Enable and start

```bash
systemctl --user daemon-reload
systemctl --user enable --now blueprint-runner.service
```

### 3d. Enable linger (start at boot without a login session)

By default, user services only run while your session is active. Enable linger so
the runner starts at system boot even if you are not logged in:

```bash
loginctl enable-linger "$USER"
```

To verify:

```bash
loginctl show-user "$USER" | grep Linger
# Linger=yes
```

---

## 4. Restart-on-crash

The unit uses `Restart=on-failure`, which means systemd restarts the runner
whenever it exits non-zero. The runner calls `os._exit(1)` (via its internal
`_die()` helper) on fatal conditions such as a revoked token or a thread supervisor
failure. That non-zero exit triggers a restart. A normal shutdown (SIGTERM, Ctrl-C)
exits `0` and is **not** restarted.

Default restart delay is 5 seconds (`RestartSec=5`). Adjust in the unit file if
needed.

---

## 5. View logs

```bash
# Live tail
journalctl --user -u blueprint-runner.service -f

# Last 100 lines
journalctl --user -u blueprint-runner.service -n 100

# Since last boot
journalctl --user -u blueprint-runner.service -b
```

---

## 6. Verify the runner is healthy

```bash
# Run the built-in preflight check (no work claimed)
python3 scripts/local_runner.py --check
```

Exit code `0` means the orchestrator is reachable, the token is valid, and at
least one task class is claimable. Non-zero means something is misconfigured —
read the preflight output for details.

---

## 7. Re-enrollment

If the runner token is revoked or lost and you cannot recover it from
`~/.blueprint/runner_token`, re-enroll via the admin UI (Settings → Runners →
Register runner). This mints a new token and a new runner identity. Run the new
token through the first-launch step above to persist it.
