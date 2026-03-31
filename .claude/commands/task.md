Create a Blueprint task from the user's request and submit it to the deployed orchestrator.

User request: $ARGUMENTS

## How to execute

### 1. Determine task type
Based on the user's request, pick the appropriate type:
- **create** (default): Write code, open a PR
- **plan**: Decompose a large task into subtasks
- **research**: Analyze the codebase and suggest improvements
- **review**: Review an existing PR (requires pr_number and branch)
- **fix**: Fix an existing PR from review feedback (requires pr_number and branch)

### 2. Craft the agent prompt
The Blueprint agent runs Claude Code with full repo access — it can explore the codebase itself. Keep prompts concise: describe WHAT to achieve and key constraints, not step-by-step implementation details.

A good prompt:
- Clear objective in 1-2 sentences
- Key constraints or things to avoid (if any)
- Scope: one task = one PR-worthy change

Don't over-specify. The agent will figure out implementation details.

### 3. Pick a title
Short, descriptive, under 60 chars. Use conventional commit style: "feat:", "fix:", "refactor:", "research:", etc.

### 4. Submit
Use the helper script — it reads auth from `.blueprint-config.json` (never hardcode secrets):

```bash
cd "D:\Henry\VSCode\Codebase\Blueprint" && python3 scripts/blueprint_api.py create_task "TITLE" "PROMPT" "TASK_TYPE"
```

Or for more control (e.g., review/fix tasks with pr_number):
```bash
cd "D:\Henry\VSCode\Codebase\Blueprint" && python3 << 'PYEOF'
from scripts.blueprint_api import create_task
import json
result = create_task(
    title="...",
    prompt="...",
    task_type="research",  # or create, plan, review, fix
    # pr_number=15,        # for review/fix tasks
    # branch="blueprint/task-abc123",  # for review/fix tasks
)
print(json.dumps(result, indent=2))
PYEOF
```

### 5. Report back
Show the user: task ID, title, type, and the prompt you submitted. Keep it brief.
