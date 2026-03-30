Check the status of Blueprint tasks on the deployed orchestrator.

Arguments (optional): $ARGUMENTS
- No arguments: list the 10 most recent tasks
- A task ID: show details for that specific task
- "active": show only running/queued tasks

## How to execute

### 1. Load config
Read `.blueprint-config.json` from the project root for `api_url` and `api_key`.
If missing, tell the user to create it.

### 2. Fetch tasks
```bash
# List recent tasks
curl -s "${api_url}/api/admin/tasks?limit=10" \
  -H "Authorization: Bearer ${api_key}"

# Or with status filter
curl -s "${api_url}/api/admin/tasks?limit=10&status=IN_PROGRESS" \
  -H "Authorization: Bearer ${api_key}"
```

### 3. Display
Show a clean summary table with: status icon, title, status, time, and PR URL if available.

Status icons: QUEUED=waiting, PROVISIONING=starting, IN_PROGRESS=running, COMPLETED=done, FAILED=error, CANCELLED=stopped.

If a specific task ID was requested and it has error logs, show them.
