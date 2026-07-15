import { createHash } from 'crypto';
import fs from 'fs';
import { PROVIDER, AUTH_MODE, type ProviderName } from './providers/runners.js';

// ── OAuth credential rotation (agent → backend) ──────────────────────────────
//
// Some CLIs (currently Codex) refresh their OAuth tokens in-place during a
// run by rewriting `auth.json`. The refreshed token lives only on the
// ephemeral VM unless we propagate it back to the backend — on the next
// task, the backend would re-push the pre-refresh token and the upstream
// provider may reject it (invalid_grant) if it rotated the refresh token
// on the last use.
//
// hashFileOrNull + the module-level `initialOAuthAuthHash` let us detect
// the in-run mutation cheaply and skip the POST when nothing changed.

const OAUTH_AUTH_PATH_BY_PROVIDER: Partial<Record<ProviderName, string>> = {
  openai: '/opt/agent/openai/auth.json',
};

function hashFileOrNull(path: string): string | null {
  try {
    const raw = fs.readFileSync(path);
    return createHash('sha256').update(raw).digest('hex');
  } catch {
    return null;
  }
}

let initialOAuthAuthHash: string | null = null;

export function recordInitialOAuthAuthHash(): void {
  const path = OAUTH_AUTH_PATH_BY_PROVIDER[PROVIDER];
  if (!path || AUTH_MODE !== 'oauth') {
    initialOAuthAuthHash = null;
    return;
  }
  initialOAuthAuthHash = hashFileOrNull(path);
}

export async function rotateOAuthCredentialsIfChanged(
  taskId: string,
  apiPostFn: (path: string, body: Record<string, unknown>) => Promise<unknown>,
): Promise<void> {
  const path = OAUTH_AUTH_PATH_BY_PROVIDER[PROVIDER];
  if (!path || AUTH_MODE !== 'oauth') return;
  if (!fs.existsSync(path)) return;
  const currentHash = hashFileOrNull(path);
  if (currentHash === null) return;
  if (initialOAuthAuthHash !== null && currentHash === initialOAuthAuthHash) {
    // Unchanged — nothing to rotate.
    return;
  }
  let contents: string;
  try {
    contents = fs.readFileSync(path, 'utf-8');
  } catch (e) {
    throw new Error(`read ${path} failed: ${e instanceof Error ? e.message : String(e)}`);
  }
  const result = await apiPostFn('/api/agent/credentials/rotate', {
    secret: contents,
    task_id: taskId,
    base_hash: initialOAuthAuthHash,
  });
  if (
    result
    && typeof result === 'object'
    && !Array.isArray(result)
    && (result as { reason?: unknown }).reason === 'stale_credential'
  ) {
    const replacement = (result as { secret?: unknown }).secret;
    if (typeof replacement !== 'string' || !replacement) {
      throw new Error('backend rejected stale OAuth rotation without a replacement credential');
    }
    // Another VM won the compare-and-swap. Adopt the authoritative credential
    // instead of failing the task or overwriting the newer token.
    fs.writeFileSync(path, replacement, { encoding: 'utf-8', mode: 0o600 });
    initialOAuthAuthHash = hashFileOrNull(path);
    if (initialOAuthAuthHash === null) {
      throw new Error(`adopt refreshed OAuth credential at ${path} failed`);
    }
    return;
  }
  if (!result || typeof result !== 'object' || Array.isArray(result) || (result as { rotated?: unknown }).rotated !== true) {
    let reason = 'unexpected response';
    if (result && typeof result === 'object' && !Array.isArray(result)) {
      const rawReason = (result as { reason?: unknown }).reason;
      reason = typeof rawReason === 'string'
        ? rawReason
        : rawReason === undefined
          ? 'unknown reason'
          : JSON.stringify(rawReason);
    }
    throw new Error(`backend did not persist rotated OAuth credentials: ${reason}`);
  }
  // Update the initial hash so a follow-up error path doesn't re-POST the
  // same blob twice. Defensive; the backend is idempotent regardless.
  initialOAuthAuthHash = currentHash;
}

// Thrown when the 45-minute kill timer fires. Carries a distinct failure
// category so the backend can map it to AGENT_TIMEOUT (scope-downgrade path)
// instead of CODE_ERROR (fix-tests/lint path).
export class ClaudeTimeoutError extends Error {
  failureCategory = 'timeout' as const;
  constructor(message: string) {
    super(message);
    this.name = 'ClaudeTimeoutError';
  }
}
