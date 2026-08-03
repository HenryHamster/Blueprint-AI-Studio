import fs from 'fs';
import os from 'os';
import path from 'path';

/**
 * Prepare a task-scoped scratch directory without touching the warm checkout.
 */
export function preparePacketOnlyWorkspace(taskId: string): string {
  const safeTaskId = taskId.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 100);
  if (!safeTaskId) throw new Error('packet-only workspace requires a task id');
  const resolved = path.join(os.tmpdir(), 'blueprint-discovery-review', safeTaskId);
  fs.rmSync(resolved, { recursive: true, force: true });
  fs.mkdirSync(resolved, { recursive: true, mode: 0o700 });
  return resolved;
}

export function cleanupPacketOnlyWorkspace(workspace: string): void {
  const root = path.resolve(path.join(os.tmpdir(), 'blueprint-discovery-review'));
  const resolved = path.resolve(workspace);
  if (!resolved.startsWith(`${root}${path.sep}`)) {
    throw new Error('refusing to clear an unsafe packet-only workspace path');
  }
  fs.rmSync(resolved, { recursive: true, force: true });
}
