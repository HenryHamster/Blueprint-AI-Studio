import fs from 'fs';
import path from 'path';

/**
 * Prepare an intentionally repository-free workspace for packet-only reviews.
 * Keep the directory itself because it may be a sandbox bind mount; only its
 * children are removed.
 */
export function preparePacketOnlyWorkspace(workspace: string): void {
  const resolved = path.resolve(workspace);
  const filesystemRoot = path.parse(resolved).root;
  if (!workspace.trim() || resolved === filesystemRoot) {
    throw new Error('refusing to clear an unsafe packet-only workspace path');
  }

  fs.mkdirSync(resolved, { recursive: true });
  for (const entry of fs.readdirSync(resolved)) {
    fs.rmSync(path.join(resolved, entry), { recursive: true, force: true });
  }
}
