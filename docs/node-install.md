# Blueprint Node (additive lifecycle facade)

`blueprint-node` is the opt-in successor surface for hosts that will eventually
run several Blueprint roles. It does not replace or modify `blueprint-runner`:
existing runner installs remain supported and retain their original unit,
credential directory, and update behavior.

Today the facade installs the existing local runner as a separately supervised
`blueprint-executor.service`. It also records the future node roles in an
on-host manifest, but does not start placeholders for anything not implemented.
In particular, this release does **not** move the API, worker, edge proxy, or
GitHub Actions runner off their current deployment paths.

## Install an execution host

Use one of the three profiles:

```bash
blueprint-node install --profile primary
blueprint-node install --profile worker
blueprint-node install --profile gpu-worker
```

The installer never accepts an enrollment token on its command line. It uses
the existing persisted runner token, or (for a fresh enrollment) a
`BLUEPRINT_RUNNER_TOKEN` inherited environment value.

Use `--system` for a shared Linux host such as a DGX. It renders a system unit;
without it, the facade renders a `systemd --user` unit. Preview every path
without writing state:

```bash
blueprint-node install --profile gpu-worker --dry-run
```

System installs deliberately refuse to run the executor as `root`. Invoke
`sudo` from the intended non-root service account (or create a dedicated local
account first); the systemd unit and its writable credential directory use that
account.

The installed executor receives a distinct environment file under
`~/.blueprint/node/roles/executor/role.env` and a `blueprint-executor.service`
unit. Its runner credentials still use the existing `~/.blueprint` credential
location, so a previously enrolled runner can be adopted without re-enrollment.
Before adopting an existing host, explicitly stop and disable its old unit so
there is exactly one claimant using those credentials:

```bash
systemctl --user disable --now blueprint-runner.service
# On a system install: sudo systemctl disable --now blueprint-runner.service
```

`blueprint-node install` refuses to continue while that legacy unit is active.
When moving the same service account from a user unit to `--system`, uninstall
the old user unit after it is stopped (`blueprint-runner uninstall`); the system
installer also refuses a leftover user-unit definition because it cannot safely
inspect that user's session bus from a sudo invocation.
`gpu-worker` supplies conservative defaults of one concurrent compute job and
one GPU per job; explicit install flags override those defaults.

## Honest role status

```bash
blueprint-node status
```

Profiles intentionally display node-agent, Actions, API, worker, and edge as
`planned` until each has a real binary, credential boundary, and health contract.
They are not rendered, enabled, or reported as healthy by this initial facade.

## Draining the executor

```bash
blueprint-node stop --role executor
blueprint-node start --role executor
```

`stop` stops **and disables** the executor before writing the stop marker, so
it stays stopped across reboot and no new work can be claimed. It is currently
an immediate stop rather than a claim lease: use it during maintenance only
after allowing active work to finish, and do not present it as graceful
preemption. `start` enables and starts the unit and only then clears the marker.

The facade’s manifest and role environment files contain configuration and may
contain enrollment material; they are written atomically with owner-only file
permissions on POSIX hosts. Do not copy them between hosts or commit them.
