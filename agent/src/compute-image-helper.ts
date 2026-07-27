import fs from 'fs';
import { agentTokenShellFn } from './token-file.js';

function shellSingleQuoted(value: string): string {
  return `'${value.replace(/'/g, `'\"'\"'`)}'`;
}

export function generateComputeImageScript(projectId: string): string {
  return `#!/bin/bash
# Project compute-image environment resolver. Status is read-only for every
# task; build is a Strategic-only, manifest-declared action. The helper accepts
# no arbitrary image/build arguments.
OP="\${1:?Usage: compute-image status|build. Run status first; CPU-only experiments do not need a compute image.}"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
${agentTokenShellFn()}
_load_agent_token || exit 1
PROJECT=${shellSingleQuoted(projectId)}

case "$OP" in
  status)
    response=$(curl -sS --max-time 30 --connect-timeout 10 \\
      -X GET "$API/api/agent/projects/$PROJECT/compute-environment" \\
      -H "X-Agent-Token: $TOKEN" -w '\\n%{http_code}')
    ;;
  build)
    # The backend enforces the Strategic-only build authority. Ordinary code
    # tasks can still inspect status/schema without being granted build access.
    # Do not poll or retry from this resident turn.
    response=$(curl -sS --max-time 30 --connect-timeout 10 \\
      -X POST "$API/api/agent/projects/$PROJECT/compute-image/build" \\
      -H "X-Agent-Token: $TOKEN" -w '\\n%{http_code}')
    ;;
  *) echo "Usage: compute-image status|build. Run status first; omit accelerator/container_image for CPU-only work." >&2; exit 1 ;;
esac

http_code=$(printf '%s' "$response" | tail -n1)
body=$(printf '%s' "$response" | head -n -1)
if [ "$http_code" -ge 200 ] && [ "$http_code" -lt 300 ]; then
  printf '%s\\n' "$body"
  exit 0
fi
printf '%s\\n' "$body" >&2
echo "[ERROR] compute-image $OP failed (HTTP $http_code)" >&2
echo "[HINT] Inspect the structured error above. CPU-only jobs can omit accelerator and container_image; GPU jobs should use the environment returned by status." >&2
exit 1
`;
}

export function writeComputeImageHelper(
  projectId: string,
  workspace: string,
): void {
  fs.writeFileSync(
    `${workspace}/compute-image`,
    generateComputeImageScript(projectId),
    { mode: 0o755 },
  );
}
