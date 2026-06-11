import fs from 'fs';
import path from 'path';
import { WORKSPACE } from '../index.js';
import {
  BLUEPRINT_ADMIN_UI_ESLINT_CMD,
  BLUEPRINT_AGENT_ESLINT_CMD,
  BLUEPRINT_AGENT_TSC_CMD,
  BLUEPRINT_BACKEND_RUFF_CHECK_CMD,
  BLUEPRINT_BACKEND_RUFF_FORMAT_CMD,
} from './check-helper.js';

export const DIAGNOSTICS_AFTER_EDIT_HOOK_MATCHER = 'Edit|Write|MultiEdit|NotebookEdit';
export const DIAGNOSTICS_AFTER_EDIT_HOOK_PATH = '.claude/hooks/diagnostics-after-edit.sh';
const SETTINGS_PATH = '.claude/settings.json';

function withoutTrailingRedirect(command: string): string {
  return command.replace(/\s+2>&1$/, '');
}

function excludeClaudeWorkspaceState(workspace: string): void {
  try {
    const excludePath = path.join(workspace, '.git', 'info', 'exclude');
    const existing = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
    if (!existing.split('\n').includes('/.claude/')) {
      fs.appendFileSync(excludePath, '/.claude/\n');
    }
  } catch { /* best effort */ }
}

export function buildDiagnosticsAfterEditHookScript(): string {
  const adminUiEslint = withoutTrailingRedirect(BLUEPRINT_ADMIN_UI_ESLINT_CMD);
  const agentEslint = withoutTrailingRedirect(BLUEPRINT_AGENT_ESLINT_CMD);
  const agentTsc = BLUEPRINT_AGENT_TSC_CMD;
  const backendRuffCheck = withoutTrailingRedirect(BLUEPRINT_BACKEND_RUFF_CHECK_CMD);
  const backendRuffFormat = withoutTrailingRedirect(BLUEPRINT_BACKEND_RUFF_FORMAT_CMD);

  return `#!/bin/bash
set +e

HOOK_DIR="$(cd "$(dirname "\${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="\${CLAUDE_PROJECT_DIR:-$(cd "$HOOK_DIR/../.." && pwd)}"
INPUT_FILE="$(mktemp)"
OUTPUT_FILE="$(mktemp)"
PATHS_FILE="$(mktemp)"
trap 'rm -f "$INPUT_FILE" "$OUTPUT_FILE" "$PATHS_FILE"' EXIT

cat > "$INPUT_FILE" || exit 0

python3 - "$INPUT_FILE" "$PROJECT_DIR" > "$PATHS_FILE" <<'PY' || exit 0
import json
import sys
from pathlib import Path

input_path, project_dir = sys.argv[1], sys.argv[2]
try:
    payload = json.loads(Path(input_path).read_text())
except Exception:
    sys.exit(0)

root = Path(project_dir).resolve()
tool_input = payload.get("tool_input") if isinstance(payload, dict) else {}
tool_response = payload.get("tool_response") if isinstance(payload, dict) else {}
if not isinstance(tool_input, dict):
    tool_input = {}
if not isinstance(tool_response, dict):
    tool_response = {}

candidates = [
    tool_input.get("file_path"),
    tool_input.get("path"),
    tool_input.get("notebook_path"),
    tool_response.get("filePath"),
]

seen = set()
for raw in candidates:
    if not isinstance(raw, str) or not raw:
        continue
    p = Path(raw)
    if not p.is_absolute():
        p = root / p
    try:
        rel = p.resolve().relative_to(root).as_posix()
    except Exception:
        continue
    if rel not in seen:
        seen.add(rel)
        print(rel)
PY

if [ ! -s "$PATHS_FILE" ]; then
  exit 0
fi

run_diag() {
  local label="$1"
  local timeout_s="$2"
  shift 2
  local tmp
  tmp="$(mktemp)"
  if timeout "$timeout_s" "$@" > "$tmp" 2>&1; then
    rm -f "$tmp"
    return 0
  fi
  local code=$?
  {
    echo "[$label] exit_code=$code"
    tail -c 4000 "$tmp"
    echo ""
  } >> "$OUTPUT_FILE"
  rm -f "$tmp"
  return 0
}

agent_tsc=0
while IFS= read -r rel; do
  [ -z "$rel" ] && continue
  [ -f "$PROJECT_DIR/$rel" ] || continue
  case "$rel" in
    admin-ui/*.ts|admin-ui/*.tsx|admin-ui/*.js|admin-ui/*.jsx|admin-ui/*.cjs|admin-ui/*.mjs)
      run_diag "admin-ui eslint $rel" 20 bash -c "cd \\"$PROJECT_DIR/admin-ui\\" && ${adminUiEslint} \\"\\\${1#admin-ui/}\\" 2>&1" _ "$rel"
      ;;
    agent/*.ts|agent/*.tsx|agent/*.js|agent/*.jsx|agent/*.cjs|agent/*.mjs)
      run_diag "agent eslint $rel" 20 bash -c "cd \\"$PROJECT_DIR/agent\\" && ${agentEslint} \\"\\\${1#agent/}\\" 2>&1" _ "$rel"
      agent_tsc=1
      ;;
    backend/*.py)
      run_diag "backend ruff check $rel" 20 bash -c "cd \\"$PROJECT_DIR\\" && ${backendRuffCheck} \\"$rel\\" 2>&1"
      run_diag "backend ruff format $rel" 20 bash -c "cd \\"$PROJECT_DIR\\" && ${backendRuffFormat} \\"$rel\\" 2>&1"
      ;;
  esac
done < "$PATHS_FILE"

if [ "$agent_tsc" = "1" ]; then
  run_diag "agent tsc --noEmit" 45 bash -c "cd \\"$PROJECT_DIR/agent\\" && ${agentTsc}"
fi

if [ ! -s "$OUTPUT_FILE" ]; then
  exit 0
fi

python3 - "$OUTPUT_FILE" <<'PY' || exit 0
import json
import sys
from pathlib import Path

text = Path(sys.argv[1]).read_text(errors="replace")
if len(text.encode("utf-8")) > 12000:
    text = text.encode("utf-8")[-12000:].decode("utf-8", "replace")
message = "Post-edit diagnostics reported issues. Review and fix before continuing if relevant.\\n\\n" + text
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": message,
    },
}))
PY

exit 0
`;
}

function parseExistingSettings(settingsPath: string): Record<string, unknown> {
  if (!fs.existsSync(settingsPath)) return {};
  try {
    const parsed = JSON.parse(fs.readFileSync(settingsPath, 'utf-8')) as unknown;
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? parsed as Record<string, unknown>
      : {};
  } catch {
    return {};
  }
}

export function writeDiagnosticsAfterEditHook(workspace: string = WORKSPACE): boolean {
  try {
    const claudeDir = path.join(workspace, '.claude');
    const hooksDir = path.join(workspace, '.claude', 'hooks');
    fs.mkdirSync(hooksDir, { recursive: true });

    const hookAbsPath = path.join(workspace, DIAGNOSTICS_AFTER_EDIT_HOOK_PATH);
    fs.writeFileSync(hookAbsPath, buildDiagnosticsAfterEditHookScript(), { mode: 0o755 });

    const settingsAbsPath = path.join(workspace, SETTINGS_PATH);
    const settings = parseExistingSettings(settingsAbsPath);
    const hooks = settings.hooks && typeof settings.hooks === 'object' && !Array.isArray(settings.hooks)
      ? settings.hooks as Record<string, unknown>
      : {};
    const postToolUse = Array.isArray(hooks.PostToolUse)
      ? hooks.PostToolUse.filter((entry) => {
          if (!entry || typeof entry !== 'object') return true;
          const inner = (entry as { hooks?: unknown }).hooks;
          if (!Array.isArray(inner)) return true;
          return !inner.some((handler) =>
            handler && typeof handler === 'object'
            && (handler as { command?: unknown }).command === '${CLAUDE_PROJECT_DIR}/.claude/hooks/diagnostics-after-edit.sh'
          );
        })
      : [];

    postToolUse.push({
      matcher: DIAGNOSTICS_AFTER_EDIT_HOOK_MATCHER,
      hooks: [
        {
          type: 'command',
          command: '${CLAUDE_PROJECT_DIR}/.claude/hooks/diagnostics-after-edit.sh',
          timeout: 60,
        },
      ],
    });
    hooks.PostToolUse = postToolUse;
    settings.hooks = hooks;

    fs.mkdirSync(claudeDir, { recursive: true });
    fs.writeFileSync(settingsAbsPath, `${JSON.stringify(settings, null, 2)}\n`, { mode: 0o644 });
    excludeClaudeWorkspaceState(workspace);
    return true;
  } catch {
    return false;
  }
}
