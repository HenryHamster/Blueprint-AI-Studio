Create a Blueprint task from the user's request through the operator CLI.

User request: $ARGUMENTS

## How to execute

1. Choose the server-supported task type. Common choices are `create`, `plan`,
   `research`, `review`, and `fix`; `review` and `fix` require both a PR number
   and branch.
2. Write a short title and a concise outcome-oriented prompt. One task should be
   one PR-worthy change.
3. For a code-producing task, declare exactly one acceptance contract:
   - `--acceptance-test PATH::TEST` when a named test can prove the change;
   - `--acceptance-behavior "..."` when the proof is an observable behavior;
   - `--acceptance-evidence-file FILE.json` for advanced evidence kinds.
4. Supply a stable `--idempotency-key` when retrying the same intended create.
5. Require an operator-supplied `BLUEPRINT_CONFIG` path and an exact `--project`
   or `--project-id`. The operator config is distinct from the read-only
   `BLUEPRINT_AGENT_CONFIG`; do not infer either path or read either file. If the
   operator config or project selector is unavailable, stop and ask for it.
6. After authorization, run
   `BLUEPRINT_CONFIG="/absolute/operator.json" ./blueprint tasks create TITLE PROMPT --project PROJECT ... --yes`
   on Unix. On Windows, set `$env:BLUEPRINT_CONFIG` to the operator-provided path
   and run `.\blueprint.ps1 tasks create TITLE PROMPT --project PROJECT ... --yes`.
7. Report the exact task ID, title, type, prompt, acceptance declaration, and PR
   metadata when applicable.

Do not call `scripts/blueprint_api.py`, read `.blueprint-config.json`, embed an
API key, or use an inline Python/bash heredoc. Task creation is a mutation and
must use an operator-authorized config; the coding-agent MCP is read-only by
design. Never use `BLUEPRINT_AGENT_CONFIG` to create a task.
