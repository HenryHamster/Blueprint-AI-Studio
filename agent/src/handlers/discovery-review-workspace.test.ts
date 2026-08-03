import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';

import { afterEach, describe, expect, it } from 'vitest';

import {
  cleanupPacketOnlyWorkspace,
  preparePacketOnlyWorkspace,
} from './discovery-review-workspace.js';

const scratchRoot = path.resolve(
  path.join(os.tmpdir(), 'blueprint-discovery-review'),
);

describe('packet-only discovery workspace', () => {
  const created: string[] = [];

  afterEach(() => {
    for (const workspace of created.splice(0)) {
      fs.rmSync(workspace, { recursive: true, force: true });
    }
  });

  it('creates and cleans only a task-scoped scratch directory', () => {
    const workspace = preparePacketOnlyWorkspace('task/../../unsafe');
    created.push(workspace);

    expect(workspace.startsWith(`${scratchRoot}${path.sep}`)).toBe(true);
    expect(fs.statSync(workspace).isDirectory()).toBe(true);
    fs.writeFileSync(path.join(workspace, 'receipt.json'), '{}');

    cleanupPacketOnlyWorkspace(workspace);
    expect(fs.existsSync(workspace)).toBe(false);
  });

  it('refuses to clean a path outside its owned scratch root', () => {
    expect(() => {
      cleanupPacketOnlyWorkspace(os.tmpdir());
    }).toThrow(
      'refusing to clear an unsafe packet-only workspace path',
    );
  });
});
