import { describe, it, expect, vi, beforeEach } from 'vitest';
import fs from 'fs';
import { execFileSync } from 'child_process';
import { downloadViewportShots } from './viewport-shot-download.js';

vi.mock('fs');
vi.mock('child_process');

const mockMkdirSync = vi.mocked(fs.mkdirSync);
const mockExecFileSync = vi.mocked(execFileSync);

const SIGNED_URL = 'https://signed.example.com/shot.png';
const JSON_RESPONSE = Buffer.from(JSON.stringify({ url: SIGNED_URL }));

const BASE_OPTS = {
  orchestratorUrl: 'http://localhost:8080',
  projectId: 'proj-1',
  sourceTaskId: 'task-abc',
  shotDir: '/tmp/shots',
};

beforeEach(() => {
  vi.resetAllMocks();
  mockMkdirSync.mockReturnValue(undefined as unknown as string);
  // Two-step flow: first call fetches JSON with signed URL, second downloads the file.
  // Return JSON_RESPONSE for both calls; the download return value is unused.
  mockExecFileSync.mockReturnValue(JSON_RESPONSE);
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
    // Second call (index 1) is the signed-URL download that writes -o dest
    const [, args] = mockExecFileSync.mock.calls[1] as [string, string[]];
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
    // Two curl calls per entry (fetch JSON + download signed URL)
    expect(mockExecFileSync).toHaveBeenCalledTimes(16);
  });

  it('respects custom limit', () => {
    const entries = Array.from({ length: 5 }, (_, i) => ({ name: `shot-${i}` }));
    downloadViewportShots({ ...BASE_OPTS, entries, limit: 3 });
    // Two curl calls per entry
    expect(mockExecFileSync).toHaveBeenCalledTimes(6);
  });

  it('calls onError and excludes failed downloads from result', () => {
    // Throw on the first JSON-fetch call for the bad entry
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
    // First call fetches the JSON download-url endpoint
    const [, args] = mockExecFileSync.mock.calls[0] as [string, string[]];
    const url = args[args.length - 1];
    expect(url).toContain('name=component-375-before');
    expect(url).toContain('project_id=proj-1');
    expect(url).toContain('task_id=task-abc');
  });

  it('includes x-agent-token header when agentToken is provided', () => {
    downloadViewportShots({
      ...BASE_OPTS,
      entries: [{ name: 'shot-1' }],
      agentToken: 'my-token',
    });
    const [, firstArgs] = mockExecFileSync.mock.calls[0] as [string, string[]];
    const headerIdx = firstArgs.indexOf('-H');
    expect(headerIdx).toBeGreaterThan(-1);
    expect(firstArgs[headerIdx + 1]).toBe('x-agent-token: my-token');
  });

  it('uses signed URL from JSON response as second curl target', () => {
    downloadViewportShots({
      ...BASE_OPTS,
      entries: [{ name: 'shot-1' }],
    });
    // Second call downloads the signed URL to the destination file
    const [, secondArgs] = mockExecFileSync.mock.calls[1] as [string, string[]];
    expect(secondArgs).toContain(SIGNED_URL);
    expect(secondArgs).toContain('-o');
  });

  it('returns empty array when entries is empty', () => {
    const result = downloadViewportShots({ ...BASE_OPTS, entries: [] });
    expect(result).toEqual([]);
    expect(mockExecFileSync).not.toHaveBeenCalled();
  });
});
