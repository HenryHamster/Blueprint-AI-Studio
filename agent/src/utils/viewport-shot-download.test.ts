import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import { execFileSync } from 'child_process';
import { downloadViewportShots } from './viewport-shot-download.js';

vi.mock('fs');
vi.mock('child_process');

const mockMkdirSync = vi.mocked(fs.mkdirSync);
const mockExecFileSync = vi.mocked(execFileSync);

const BASE_OPTS = {
  orchestratorUrl: 'http://localhost:8080',
  projectId: 'proj-1',
  sourceTaskId: 'task-abc',
  shotDir: '/tmp/shots',
};

beforeEach(() => {
  vi.resetAllMocks();
  mockMkdirSync.mockReturnValue(undefined as unknown as string);
  mockExecFileSync.mockReturnValue(Buffer.from(''));
});

describe('downloadViewportShots', () => {
  it('returns downloaded paths for valid entries', () => {
    const result = downloadViewportShots({
      ...BASE_OPTS,
      entries: [{ name: 'Foo-375-before' }, { name: 'Bar-375-after' }],
    });
    expect(result).toEqual([
      '/tmp/shots/Foo-375-before.png',
      '/tmp/shots/Bar-375-after.png',
    ]);
  });

  it('sanitizes path traversal: ../evil resolves inside shotDir', () => {
    const result = downloadViewportShots({
      ...BASE_OPTS,
      entries: [{ name: '../../etc/evil' }],
    });
    // path.basename('../../etc/evil') === 'evil' — dest stays inside shotDir
    expect(result).toEqual(['/tmp/shots/evil.png']);
    const [, args] = mockExecFileSync.mock.calls[0] as [string, string[]];
    const destArg = args[args.indexOf('-o') + 1];
    expect(destArg.startsWith('/tmp/shots/')).toBe(true);
    expect(destArg).not.toContain('..');
  });

  it('sanitizes leading slash: /etc/passwd resolves inside shotDir', () => {
    const result = downloadViewportShots({
      ...BASE_OPTS,
      entries: [{ name: '/etc/passwd' }],
    });
    expect(result).toEqual(['/tmp/shots/passwd.png']);
  });

  it('skips entries with empty name', () => {
    const result = downloadViewportShots({
      ...BASE_OPTS,
      entries: [{ name: '' }, { name: 'valid' }],
    });
    expect(result).toEqual(['/tmp/shots/valid.png']);
  });

  it('respects limit (default 8)', () => {
    const entries = Array.from({ length: 12 }, (_, i) => ({ name: `shot-${i}` }));
    downloadViewportShots({ ...BASE_OPTS, entries });
    expect(mockExecFileSync).toHaveBeenCalledTimes(8);
  });

  it('respects custom limit', () => {
    const entries = Array.from({ length: 5 }, (_, i) => ({ name: `shot-${i}` }));
    downloadViewportShots({ ...BASE_OPTS, entries, limit: 3 });
    expect(mockExecFileSync).toHaveBeenCalledTimes(3);
  });

  it('calls onError and excludes failed downloads from result', () => {
    mockExecFileSync.mockImplementationOnce(() => { throw new Error('curl failed'); });
    const onError = vi.fn();
    const result = downloadViewportShots({
      ...BASE_OPTS,
      entries: [{ name: 'bad' }, { name: 'good' }],
      onError,
    });
    expect(onError).toHaveBeenCalledWith('bad');
    expect(result).toEqual(['/tmp/shots/good.png']);
  });

  it('passes the original (unsanitized) name in the download URL query param', () => {
    downloadViewportShots({
      ...BASE_OPTS,
      entries: [{ name: 'component-375-before' }],
    });
    const [, args] = mockExecFileSync.mock.calls[0] as [string, string[]];
    const url = args[args.length - 1];
    expect(url).toContain('name=component-375-before');
    expect(url).toContain('project_id=proj-1');
    expect(url).toContain('task_id=task-abc');
  });

  it('returns empty array when entries is empty', () => {
    const result = downloadViewportShots({ ...BASE_OPTS, entries: [] });
    expect(result).toEqual([]);
    expect(mockExecFileSync).not.toHaveBeenCalled();
  });
});
