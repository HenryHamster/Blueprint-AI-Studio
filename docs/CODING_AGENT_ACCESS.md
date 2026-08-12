# Coding-agent access to Blueprint

Blueprint exposes developer-hosted coding-agent context through a small,
read-only MCP server. Use that server for orientation and diagnosis; keep the
operator CLI for human fallback and explicitly authorized mutations.

This first release does not inject reusable service credentials into Blueprint
task workers. Those workers keep their task-scoped runtime token and helpers;
the setup below is for developer-hosted Codex, Claude Code, or another local
MCP host.

This is intentionally not an MCP wrapper around the admin API. A coding agent
authenticates with an expiring `ServiceCredential`, receives ordinary viewer
membership in explicitly named projects, and can call only a fixed set of GET
routes. A shared `ADMIN_API_KEY`, an ambient repository config, and task-scoped
agent JWTs are rejected by the MCP process.

## What an agent can read

The initial `coding-agent:read` scope provides:

- `get_project_snapshot`: the canonical first call, combining bounded operator
  attention, pipeline diagnostics, execution-resource availability, stable open
  message counts, timestamps, and the serving revision. User-relative unread
  state is omitted because a read-only credential cannot advance a cursor.
- `list_projects`: only the credential's effective project memberships.
- `list_tasks` and `get_task`: bounded task lists and one membership-checked,
  payload-redacted task detail.
- `list_messages`: bounded operator-visible messages. Non-operator audiences
  and large raw context blobs stay excluded by the backend projection.
- `list_directions`: bounded, paginated project directions with explicit
  truncation metadata.

Every tool is annotated read-only and returns both structured MCP content and a
JSON text copy. A failed required read is an error, not a successful snapshot
with silently missing fields. There are no create, cancel, requeue, settings,
fleet, deployment, or merge tools in this scope.

## 1. Mint a project-scoped credential

An operator with the normal admin credential performs this once. The plaintext
token is returned once and cannot be retrieved later.

PowerShell:

```powershell
$env:BLUEPRINT_CONFIG = "C:\secure\blueprint\operator.json"
.\blueprint.ps1 agent-access grant codex-blueprint `
  --project-id PROJECT-UUID `
  --description "Local Codex read access" `
  --expires-in-days 30 `
  --yes
```

Bash:

```bash
BLUEPRINT_CONFIG=/secure/blueprint/operator.json ./blueprint agent-access grant codex-blueprint \
  --project-id PROJECT-UUID \
  --description "Local Codex read access" \
  --expires-in-days 30 \
  --yes
```

Store the resulting token in a config file outside the repository. Do not put
it in `.blueprint-config.json`, `.env`, a checked-in MCP file, a prompt, or shell
history. The file format is:

```json
{
  "api_url": "https://YOUR-BLUEPRINT-HOST",
  "api_key": "bpsc_TOKEN_SHOWN_ONCE",
  "project_id": "PROJECT-UUID",
  "read_only": true
}
```

Keep the `read_only` marker: the service credential is already server-enforced,
and this adds a local CLI guard against accidentally attempting a mutation with
the scoped file.

For the checked-in Claude registration, set `BLUEPRINT_AGENT_CONFIG` in the
developer host to that absolute file path. The registration maps it to
`BLUEPRINT_CONFIG` only in the MCP child process, so the operator CLI can keep
separate authorization. The MCP server deliberately refuses the CLI's ambient
repo/home discovery and gcloud admin bootstrap. Another host may instead pass
`BLUEPRINT_CONFIG` directly to the MCP child, or pass child-only
`BLUEPRINT_API_URL` and `BLUEPRINT_API_KEY` values plus `BLUEPRINT_PROJECT_ID`
when the credential has more than one project.

Verify the effective identity in a short-lived shell before connecting an
agent. Clear `BLUEPRINT_CONFIG` afterward; it is only an adapter for these
read-only CLI checks, not the operator mutation config:

```powershell
$env:BLUEPRINT_AGENT_CONFIG = "C:\secure\blueprint\codex-blueprint.json"
$previousBlueprintConfig = $env:BLUEPRINT_CONFIG
try {
  $env:BLUEPRINT_CONFIG = $env:BLUEPRINT_AGENT_CONFIG
  .\blueprint.ps1 agent-access whoami --json
  .\blueprint.ps1 agent-access snapshot --json
} finally {
  $env:BLUEPRINT_CONFIG = $previousBlueprintConfig
}
```

## 2. Connect an MCP host

The stdio entrypoint is `scripts/blueprint_mcp.py`; it needs Python 3 and only
the standard library. Use an absolute interpreter path when the host does not
inherit a reliable `python` command.

Codex CLI can register it without copying the token into Codex configuration:

```powershell
codex mcp add blueprint `
  --env BLUEPRINT_CONFIG=C:\secure\blueprint\codex-blueprint.json `
  -- C:\path\to\python.exe D:\path\to\Blueprint\scripts\blueprint_mcp.py
codex mcp list
```

The equivalent project or user `config.toml` entry is:

```toml
[mcp_servers.blueprint]
command = "C:\\path\\to\\python.exe"
args = ["D:\\path\\to\\Blueprint\\scripts\\blueprint_mcp.py"]
cwd = "D:\\path\\to\\Blueprint"
default_tools_approval_mode = "approve"

[mcp_servers.blueprint.env]
BLUEPRINT_CONFIG = "C:\\secure\\blueprint\\codex-blueprint.json"
```

Codex's local clients support stdio servers, environment forwarding, and a
project-scoped `.codex/config.toml`; see the [official Codex MCP
documentation](https://developers.openai.com/codex/mcp).

Claude Code discovers the checked-in, secret-free `.mcp.json` at the project
root. Before launching a developer-hosted session, set `BLUEPRINT_AGENT_CONFIG`
and, when `python` is not on `PATH`, set `BLUEPRINT_PYTHON` to an absolute
interpreter path. Interactive Claude sessions require one-time approval for a
project-scoped MCP server: approve `blueprint` when prompted, then verify it
with `/mcp`. Headless Claude sessions may load the project registration without
that prompt; Blueprint task workers remain outside this release because they
are not provisioned with `BLUEPRINT_AGENT_CONFIG` or a reusable `bpsc_`
credential. The fail-closed placeholder therefore exposes no Blueprint data.
The registration maps
`BLUEPRINT_AGENT_CONFIG` to child-only `BLUEPRINT_CONFIG` and uses
`${CLAUDE_PROJECT_DIR:-.}` so linked worktrees resolve their own MCP script; see
the [official Claude Code MCP documentation](https://code.claude.com/docs/en/mcp).
For another stdio MCP host, use the same command, argument, working-directory,
and environment fields in that host's configuration format.

After restarting or opening a new host session, confirm that the six Blueprint
tools are visible. Ask for a project snapshot first; do not begin a diagnosis by
manually joining several raw REST calls.

The checked-in `AGENTS.md` carries the same MCP-first, read-only guidance into
Codex sessions. Host registration supplies the tools; repository instructions
tell the coding agent when and how to use them.

## Human/CLI fallback

Windows users can run `.\blueprint.ps1`; it prefers `BLUEPRINT_PYTHON`, then
the current checkout's `backend\.venv\Scripts\python.exe`, the main checkout's
venv when running under `.worktrees` or `.codex-worktrees`, `python`, and finally
`py -3`. Unix users can run the checked-in `./blueprint`; `bash
scripts/install-cli.sh` additionally installs the bare command in
`~/.local/bin`.

For a read-only fallback, select the scoped config only in a short-lived shell:

```powershell
$previousBlueprintConfig = $env:BLUEPRINT_CONFIG
try {
  $env:BLUEPRINT_CONFIG = $env:BLUEPRINT_AGENT_CONFIG
  .\blueprint.ps1 agent-access snapshot --json
  .\blueprint.ps1 tasks list --active --limit 10 --json
  .\blueprint.ps1 tasks list --status FAILED --limit 50 --json
  .\blueprint.ps1 tasks show TASK-UUID --json
  .\blueprint.ps1 messages list --limit 50 --offset 0 --json
} finally {
  $env:BLUEPRINT_CONFIG = $previousBlueprintConfig
}
```

Task creation remains an explicitly authorized CLI mutation. Code-producing
tasks now require declared acceptance evidence, and `fix`/`review` tasks require
PR metadata. Select an operator config that is distinct from
`BLUEPRINT_AGENT_CONFIG`, and always name the target project:

```powershell
$env:BLUEPRINT_CONFIG = "C:\secure\blueprint\operator.json"
.\blueprint.ps1 tasks create "Fix retry accounting" "Correct the retry counter." `
  --project Blueprint `
  --type fix `
  --pr-number 123 `
  --branch fix/retry-accounting `
  --acceptance-test backend/testing/test_retry.py::test_retry_counter `
  --idempotency-key fix-retry-accounting-123 `
  --yes
```

## Rotation and revocation

List and revoke credentials with an admin config:

```powershell
$env:BLUEPRINT_CONFIG = "C:\secure\blueprint\operator.json"
.\blueprint.ps1 agent-access list --json
.\blueprint.ps1 agent-access revoke CREDENTIAL-UUID --yes
```

Minting the same name after revocation creates a fresh service principal. It
does not inherit the old principal's project memberships or role, so shrinking
a grant cannot silently retain old access. Rotate the old shared admin key only
after every intended MCP host has moved to scoped credentials.

## Contract boundaries

- REST identity: `GET /api/agent-access/v1/me`
- REST snapshot: `GET /api/agent-access/v1/projects/{project_id}/snapshot`
- REST bounded task list: `GET /api/agent-access/v1/tasks`
- REST bounded task detail: `GET /api/agent-access/v1/tasks/{task_id}`
- REST bounded message list: `GET /api/agent-access/v1/messages`
- REST bounded direction list: `GET /api/agent-access/v1/directions`
- MCP protocol revision: current `2026-07-28`, with negotiation for
  `2025-11-25`, `2025-06-18`, and `2024-11-05` legacy hosts.
- Snapshot schema: `blueprint.project-snapshot.v1`
- Bounded task-detail schema: `blueprint.agent-task.v1`
- Access schema: `blueprint.agent-access.v1`

The MCP and `coding-agent:read` profile remain deliberately read-only. A
separate `project-operator` scope now provides narrowly typed CLI actions for a
trusted external operator such as Hermes; it does not expand this scope or add
MCP mutations. See [HERMES_OPERATOR_ACCESS.md](HERMES_OPERATOR_ACCESS.md) for
the exact commands, exclusions, rotation, and deployment boundary.
