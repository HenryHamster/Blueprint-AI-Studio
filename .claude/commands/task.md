Create a Blueprint task from the user's request and submit it to the deployed orchestrator.

User request: $ARGUMENTS

## How to execute

### 1. Load config
Read `.blueprint-config.json` from the project root. It contains:
```json
{
  "api_url": "https://...",
  "api_key": "...",
  "default_repo": "owner/repo",
  "default_base_branch": "automain"
}
```
If the file is missing, tell the user to create it with their Blueprint Cloud Run URL and ADMIN_API_KEY, then stop.

### 2. Craft the agent prompt
The Blueprint agent runs Claude Code with full repo access — it can explore the codebase itself. Keep prompts concise: describe WHAT to achieve and key constraints, not step-by-step implementation details.

A good prompt:
- Clear objective in 1-2 sentences
- Key constraints or things to avoid (if any)
- Scope: one task = one PR-worthy change

Don't over-specify. No line numbers, no CSS values, no copy-pasting current file contents. The agent will figure out implementation details. If the user's request is too broad, suggest splitting into multiple tasks.

### 3. Pick a title
Short, descriptive, under 60 chars. Use conventional commit style prefixes: "feat:", "fix:", "refactor:", "docs:", etc.

### 4. Submit
Use inline Python via Bash — do NOT create temporary script files. The JSON payload often contains newlines and special characters that break curl/shell quoting, so Python's `json.dumps` is the reliable path.

```bash
python -c "
import json, urllib.request
config = json.load(open('.blueprint-config.json'))
payload = {'github_repo': config['default_repo'], 'title': '...', 'prompt': '...', 'base_branch': config['default_base_branch']}
data = json.dumps(payload).encode()
req = urllib.request.Request(f\"{config['api_url']}/api/admin/tasks\", data=data, headers={'Authorization': f\"Bearer {config['api_key']}\", 'Content-Type': 'application/json'})
print(json.loads(urllib.request.urlopen(req).read()))
"
```

Notes:
- Use `config['default_base_branch']` unless the user explicitly specifies a different branch
- The prompt goes in the `prompt` field, NOT `description`
- The API returns `{id, project_id, title, status, vm_instance_id}`

### 5. Report back
Show the user: task ID, title, and the prompt you submitted. Keep it brief.
