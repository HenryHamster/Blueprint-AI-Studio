import fs from 'fs';
import os from 'os';
import path from 'path';
import { afterEach, describe, expect, it } from 'vitest';

import { preparePacketOnlyWorkspace } from './discovery-review-workspace.js';

const workspaces: string[] = [];

afterEach(() => {
  for (const workspace of workspaces.splice(0)) {
    fs.rmSync(workspace, { recursive: true, force: true });
  }
});

describe('preparePacketOnlyWorkspace', () => {
  it('removes a stale checkout while preserving the workspace mountpoint', () => {
    const workspace = fs.mkdtempSync(
      path.join(os.tmpdir(), 'discovery-review-packet-'),
    );
    workspaces.push(workspace);
    fs.mkdirSync(path.join(workspace, '.git'));
    fs.writeFileSync(path.join(workspace, 'stale-source.ts'), 'stale');

    preparePacketOnlyWorkspace(workspace);

    expect(fs.existsSync(workspace)).toBe(true);
    expect(fs.readdirSync(workspace)).toEqual([]);
  });

  it('refuses to clear a filesystem root', () => {
    expect(() => {
      preparePacketOnlyWorkspace(path.parse(process.cwd()).root);
    }).toThrow(/unsafe packet-only workspace path/);
  });
});
