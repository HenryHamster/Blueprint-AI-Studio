Check Blueprint task state using the scoped, read-only Blueprint MCP tools.

Arguments (optional): $ARGUMENTS
- No arguments: show the 10 most recent tasks.
- A task UUID: show that exact task, including failure/history evidence.
- `active`: show QUEUED, PROVISIONING, IN_PROGRESS, and EVALUATING tasks.

## How to execute

1. Use `get_project_snapshot` first to resolve the project and establish current
   attention, causal pipeline state, and execution capacity.
2. For a UUID, call `get_task`. Otherwise call `list_tasks` with `limit: 10`;
   for `active`, pass all four active statuses listed above.
3. Present status, exact task ID, title, timing, latest causal status, and PR URL
   when present. If a task failed, include the bounded failure/history evidence
   returned by `get_task`.

Do not read `.blueprint-config.json`, expose a bearer token, or issue raw `curl`
requests. If the MCP server is unavailable, report that clearly and map
`BLUEPRINT_AGENT_CONFIG` to `BLUEPRINT_CONFIG` only for the read-only CLI
invocation. Use `./blueprint tasks list --limit 10 --json` for no arguments,
`./blueprint tasks list --active --limit 10 --json` for `active`, or
`./blueprint tasks show TASK-UUID --json` for a UUID (and `.\blueprint.ps1` in
place of `./blueprint` on Windows). Do not leave the scoped config selected for
an operator mutation.
