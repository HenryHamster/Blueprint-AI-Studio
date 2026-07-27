import fs from 'fs';
import path from 'path';
import { git, WORKSPACE } from '../index.js';
import { agentTokenShellFn } from '../token-file.js';

export const COMPUTE_ARTIFACT_GUIDANCE = `## Compute job artifacts
If this task cites a compute job whose artifact was not delivered above, run \`./artifacts fetch <compute-job-id>\`. It downloads the exact artifact archive through Blueprint into \`.blueprint-inputs/compute-jobs/<job-id>/bundle.tar.gz\` without requiring gcloud credentials and without staging it.

`;

export function generateArtifactsScript(): string {
  return `#!/bin/bash
set -euo pipefail
OP="\${1:-}"
JOB_ID="\${2:-}"
[ "$#" -eq 2 ] && [ "$OP" = "fetch" ] && [ -n "$JOB_ID" ] || { echo "Usage: ./artifacts fetch <compute-job-id>" >&2; exit 2; }
case "$JOB_ID" in *[!A-Za-z0-9._-]*|""|"."|"..") echo "[ERROR] invalid compute job id" >&2; exit 2;; esac
DEST=".blueprint-inputs/compute-jobs/$JOB_ID"
API="\${ORCHESTRATOR_URL:?ORCHESTRATOR_URL not set}"
${agentTokenShellFn()}
_load_agent_token || exit 1
ROOT="$(cd "$(dirname "$0")" && pwd -P)"
INPUT_ROOT="$ROOT/.blueprint-inputs/compute-jobs"
FULL="$INPUT_ROOT/$JOB_ID"
[ ! -L "$ROOT/.blueprint-inputs" ] && [ ! -L "$ROOT/.blueprint-inputs/compute-jobs" ] && [ ! -L "$FULL" ] || {
  echo "[ERROR] compute artifact destination contains a symlink" >&2
  exit 2
}
mkdir -p "$FULL"
FULL="$(realpath -m -- "$FULL")"
case "$FULL/" in "$ROOT"/*) ;; *) echo "[ERROR] resolved destination escapes the workspace" >&2; exit 2;; esac
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
URL_ENDPOINT="$API/api/agent/compute-jobs/$JOB_ID/artifact/download-url"
fetch_url() {
  _load_agent_token || return 1
  HTTP_CODE=$(curl -sS --max-time 30 --connect-timeout 10 -o "$TMP/response.json" -w '%{http_code}' -H "X-Agent-Token: $TOKEN" "$URL_ENDPOINT") || return 1
}
fetch_url || { echo "[ERROR] could not resolve compute artifact" >&2; exit 1; }
if [ "$HTTP_CODE" = "401" ] || [ "$HTTP_CODE" = "403" ]; then fetch_url || true; fi
[ "$HTTP_CODE" -ge 200 ] && [ "$HTTP_CODE" -lt 300 ] || { cat "$TMP/response.json" >&2; echo "[ERROR] artifact lookup failed (HTTP $HTTP_CODE)" >&2; exit 1; }
SIGNED_URL=$(node -e 'const fs=require("fs"); const d=JSON.parse(fs.readFileSync(process.argv[1],"utf8")); if(typeof d.url==="string") process.stdout.write(d.url);' "$TMP/response.json")
case "$SIGNED_URL" in https://*) ;; *) echo "[ERROR] artifact lookup returned no HTTPS download URL" >&2; exit 1;; esac
curl -fLsS --max-time 300 --connect-timeout 10 --max-filesize 104857600 -o "$TMP/bundle.tar.gz" "$SIGNED_URL"
mv "$TMP/bundle.tar.gz" "$FULL/bundle.tar.gz"
echo "Downloaded compute job $JOB_ID artifact to \${DEST}/bundle.tar.gz"
echo "The archive is an ignored runtime input; validate and ingest selected contents rather than staging it wholesale."
`;
}

export function writeArtifactsHelper(
  workspace: string = WORKSPACE,
  resolveGitPath: (root: string) => string = (root) =>
    git(['rev-parse', '--git-path', 'info/exclude'], root).trim(),
  verifyIgnored: (root: string, relativePath: string) => boolean = (root, relativePath) => {
    try {
      git(['check-ignore', '-q', '--no-index', '--', relativePath], root);
      return true;
    } catch {
      return false;
    }
  },
): boolean {
  const helperPath = path.join(workspace, 'artifacts');
  if (fs.existsSync(helperPath)) return false;
  try {
    const rawGitPath = resolveGitPath(workspace).trim();
    if (!rawGitPath) return false;
    const excludePath = path.isAbsolute(rawGitPath)
      ? rawGitPath
      : path.resolve(workspace, rawGitPath);
    fs.mkdirSync(path.dirname(excludePath), { recursive: true });
    const existing = fs.existsSync(excludePath) ? fs.readFileSync(excludePath, 'utf-8') : '';
    const lines = new Set(existing.split(/\r?\n/));
    const missing = ['/artifacts', '/.blueprint-inputs/'].filter((entry) => !lines.has(entry));
    if (missing.length > 0) {
      const separator = existing && !existing.endsWith('\n') ? '\n' : '';
      fs.appendFileSync(excludePath, `${separator}${missing.join('\n')}\n`);
    }
    if (
      !verifyIgnored(workspace, 'artifacts')
      || !verifyIgnored(workspace, '.blueprint-inputs/.blueprint-ignore-probe')
    ) {
      return false;
    }
    fs.writeFileSync(helperPath, generateArtifactsScript(), { mode: 0o755 });
    return true;
  } catch {
    // A missing git metadata directory is non-fatal; task setup normally creates it.
    return false;
  }
}
