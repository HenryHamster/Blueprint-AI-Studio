# Local GitHub Actions runner role

`scripts/github_actions_runner.py` is the configuration layer for a future
optional GitHub Actions role on a Blueprint node. It only creates a reviewable
plan; it does not download a runner, contact GitHub, accept a token, or change
workflow routing. The current `blueprint-node` facade records the Actions role
as planned and does not install it yet.

Generate a trusted internal-CI plan:

```bash
python3 scripts/github_actions_runner.py plan \
  --name ci --url https://github.com/example-org --safety trusted
```

The output contains the role-specific Unix user, state path, environment path,
systemd unit, and install/status/drain/uninstall instructions. Its install plan
first creates or verifies that role-scoped Unix user and group; do not substitute
an existing shared CI account. Registration
tokens are read from a root-owned file only while `config.sh` is run; they are
never emitted in the plan, environment file, unit, or command-line arguments
produced by this tool.

## Safety profiles

`trusted` is for repositories and workflows that are allowed to use the
machine's explicitly granted CI credentials. It can be persistent or ephemeral.
Docker is disabled by default. If a workflow genuinely needs Docker, configure a
dedicated socket for this role; the tool rejects `/var/run/docker.sock` and
`/run/docker.sock`.

`untrusted` is for fork/public work and is ephemeral-only. It gets a separate
Unix account, state root, env file, and systemd unit. The rendered unit denies
both conventional Docker sockets and removes access to homes, devices, kernel
controls, capabilities, production-like credentials, and common cloud
metadata-service addresses. Do not put production
secrets in its role environment. This is a local hardening profile, not a claim
that arbitrary public code is equivalent to a disposable VM; keep fork PRs on
GitHub-hosted runners until the node has a disposable-worker isolation backend.

## Draining

First disable the runner or remove its routing label in GitHub so it receives no
new jobs. Wait for the active job to finish, then stop the systemd unit. Do not
use `systemctl stop` as a job-cancellation mechanism. The generated plan lists
the exact unit and paths for that role.
