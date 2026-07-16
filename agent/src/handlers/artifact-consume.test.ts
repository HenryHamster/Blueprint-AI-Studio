import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import * as fs from 'fs';
import * as os from 'os';
import * as path from 'path';
import * as zlib from 'zlib';

// ── Module mocks ──────────────────────────────────────────────────────────────
// git is mocked at a narrow surface: extraction + orchestration are the unit
// under test, git is the external helper. The mock answers the two read calls
// (`diff --cached --name-only`, `rev-parse HEAD`) so the commit path is
// exercised, and records every invocation so we can assert the exact
// add/commit arguments (message must cite the artifact_id).
const gitCalls: string[][] = [];
const sendLogMock = vi.fn();
vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '/tmp/workspace',
  sendLog: (...args: unknown[]): void => { sendLogMock(...args); },
  git: (args: string[], _cwd?: string): string => {
    gitCalls.push(args);
    if (args[0] === 'diff') return 'committed/file.txt';
    if (args[0] === 'rev-parse') return 'abc1234def567890';
    return '';
  },
}));

const fetchArtifactDownloadUrlMock = vi.fn();
vi.mock('./prior-knowledge.js', () => ({
  fetchArtifactDownloadUrl: (id: string): Promise<string | null> =>
    fetchArtifactDownloadUrlMock(id) as Promise<string | null>,
}));

// ── Imports under test ────────────────────────────────────────────────────────
import {
  parseTar,
  isUnsafeEntryPath,
  extractTarGzToDir,
  downloadBundle,
  recordConsumedArtifacts,
  consumeAndCommitArtifacts,
  COMMITTABLE_ARTIFACT_TYPES,
} from './artifact-consume.js';

// ── Fixture tar builder (independent of the parser under test) ─────────────────
function tarHeader(name: string, size: number, typeflag = '0'): Buffer {
  const h = Buffer.alloc(512, 0);
  h.write(name, 0, 100, 'utf-8');
  h.write('0000644', 100, 8, 'ascii'); // mode
  h.write('0000000', 108, 8, 'ascii'); // uid
  h.write('0000000', 116, 8, 'ascii'); // gid
  h.write(size.toString(8).padStart(11, '0'), 124, 11, 'ascii'); // size (octal)
  h.write('00000000000', 136, 11, 'ascii'); // mtime
  h.write('        ', 148, 8, 'ascii'); // chksum placeholder (8 spaces)
  h.write(typeflag, 156, 1, 'ascii');
  h.write('ustar\0', 257, 6, 'ascii');
  h.write('00', 263, 2, 'ascii');
  let sum = 0;
  for (let i = 0; i < 512; i++) sum += h[i];
  h.write(sum.toString(8).padStart(6, '0') + '\0 ', 148, 8, 'ascii');
  return h;
}

function tarFile(name: string, content: string): Buffer {
  const data = Buffer.from(content, 'utf-8');
  const header = tarHeader(name, data.length, '0');
  const pad = Buffer.alloc((512 - (data.length % 512)) % 512, 0);
  return Buffer.concat([header, data, pad]);
}

function makeTarGz(entries: { name: string; content: string }[]): Buffer {
  const blocks = entries.map((e) => tarFile(e.name, e.content));
  const end = Buffer.alloc(1024, 0); // two zero blocks terminate the archive
  return zlib.gzipSync(Buffer.concat([...blocks, end]));
}

const FIXTURE_ENTRIES = [
  { name: 'committed/file.txt', content: 'hello artifact' },
  { name: 'nested/dir/data.json', content: '{"k":1}' },
  { name: '../escape.txt', content: 'path traversal payload' },
  { name: '/etc/absolute.txt', content: 'absolute payload' },
];

let tmpRepo: string;

beforeEach(() => {
  gitCalls.length = 0;
  sendLogMock.mockReset();
  fetchArtifactDownloadUrlMock.mockReset();
  tmpRepo = fs.mkdtempSync(path.join(os.tmpdir(), 'artifact-consume-'));
});

afterEach(() => {
  vi.unstubAllGlobals();
  try { fs.rmSync(tmpRepo, { recursive: true, force: true }); } catch { /* best effort */ }
});

describe('isUnsafeEntryPath', () => {
  it('rejects absolute, drive, and parent-traversal paths; accepts normal ones', () => {
    expect(isUnsafeEntryPath('')).toBe(true);
    expect(isUnsafeEntryPath('/etc/passwd')).toBe(true);
    expect(isUnsafeEntryPath('../escape.txt')).toBe(true);
    expect(isUnsafeEntryPath('a/b/../../../escape')).toBe(true);
    expect(isUnsafeEntryPath('C:/windows/x')).toBe(true);
    expect(isUnsafeEntryPath('..\\escape')).toBe(true);
    expect(isUnsafeEntryPath('committed/file.txt')).toBe(false);
    expect(isUnsafeEntryPath('./nested/data.json')).toBe(false);
  });
});

describe('parseTar', () => {
  it('parses ustar file entries with names, sizes, and contents', () => {
    const tar = zlib.gunzipSync(makeTarGz([{ name: 'a/b.txt', content: 'xyz' }]));
    const entries = parseTar(tar);
    const files = entries.filter((e) => e.type === 'file');
    expect(files).toHaveLength(1);
    expect(files[0].name).toBe('a/b.txt');
    expect(files[0].data.toString('utf-8')).toBe('xyz');
  });
});

describe('extractTarGzToDir', () => {
  it('extracts safe files and rejects traversal/absolute entries', () => {
    const gz = makeTarGz(FIXTURE_ENTRIES);
    const res = extractTarGzToDir(gz, tmpRepo);

    expect(res.written).toEqual(['committed/file.txt', 'nested/dir/data.json']);
    expect(res.rejected).toContain('../escape.txt');
    expect(res.rejected).toContain('/etc/absolute.txt');

    expect(fs.readFileSync(path.join(tmpRepo, 'committed/file.txt'), 'utf-8')).toBe('hello artifact');
    expect(fs.readFileSync(path.join(tmpRepo, 'nested/dir/data.json'), 'utf-8')).toBe('{"k":1}');
    // The traversal entry must NOT have been written above the repo root.
    expect(fs.existsSync(path.join(path.dirname(tmpRepo), 'escape.txt'))).toBe(false);
  });

  it('enforces the max-files cap, counting the overflow as skipped', () => {
    const many = Array.from({ length: 5 }, (_v, i) => ({ name: `f${i}.txt`, content: 'x' }));
    const res = extractTarGzToDir(makeTarGz(many), tmpRepo, {
      maxFiles: 2,
      maxTotalBytes: 1024,
      maxEntryBytes: 1024,
    });
    expect(res.written).toHaveLength(2);
    expect(res.skipped).toHaveLength(3);
  });
});

describe('downloadBundle', () => {
  it('returns the body buffer on a 200 response', async () => {
    const payload = Buffer.from('bundle-bytes');
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => null },
      arrayBuffer: () => Promise.resolve(payload.buffer.slice(payload.byteOffset, payload.byteOffset + payload.byteLength)),
    }));
    const buf = await downloadBundle('https://signed.example/bundle.tar.gz');
    expect(buf?.toString('utf-8')).toBe('bundle-bytes');
  });

  it('returns null fail-soft on non-2xx, network error, and empty body', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, headers: { get: () => null }, arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)) }));
    expect(await downloadBundle('https://x')).toBeNull();
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new Error('ECONNRESET')));
    expect(await downloadBundle('https://x')).toBeNull();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, headers: { get: () => null }, arrayBuffer: () => Promise.resolve(new ArrayBuffer(0)) }));
    expect(await downloadBundle('https://x')).toBeNull();
  });

  it('refuses an over-cap content-length without reading the body', async () => {
    const arrayBuffer = vi.fn();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true, headers: { get: () => String(200 * 1024 * 1024) }, arrayBuffer }));
    expect(await downloadBundle('https://x')).toBeNull();
    expect(arrayBuffer).not.toHaveBeenCalled();
  });
});

describe('recordConsumedArtifacts', () => {
  it('unions newly consumed ids with any pre-existing file content', () => {
    const taskId = 'rec-test-1234';
    const file = `/tmp/consumed_artifacts_${taskId}.json`;
    try { fs.unlinkSync(file); } catch { /* fresh */ }
    fs.writeFileSync(file, JSON.stringify(['old-id']));
    recordConsumedArtifacts(taskId, ['new-id', 'old-id']);
    const parsed = JSON.parse(fs.readFileSync(file, 'utf-8')) as string[];
    expect(new Set(parsed)).toEqual(new Set(['old-id', 'new-id']));
    fs.unlinkSync(file);
  });
});

describe('consumeAndCommitArtifacts (fetch → untar → extract → commit)', () => {
  it('downloads the signed URL, extracts the bundle, and git-commits citing the artifact_id', async () => {
    const gz = makeTarGz(FIXTURE_ENTRIES);
    fetchArtifactDownloadUrlMock.mockResolvedValue('https://signed.example/bundle.tar.gz');
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      headers: { get: () => null },
      arrayBuffer: () => Promise.resolve(gz.buffer.slice(gz.byteOffset, gz.byteOffset + gz.byteLength)),
    });
    vi.stubGlobal('fetch', fetchMock);

    const taskId = 'consume-task-abcdef';
    const consumedFile = `/tmp/consumed_artifacts_${taskId}.json`;
    try { fs.unlinkSync(consumedFile); } catch { /* fresh */ }

    const outcome = await consumeAndCommitArtifacts({
      taskId,
      repoRoot: tmpRepo,
      pointers: [
        {
          artifact_id: 'artifact-xyz',
          logical_key: 'repo-files',
          artifact_type: 'GIT_COMMIT',
          source_task_id: 'producer-1',
        },
      ],
    });

    // Signed URL was minted via the existing helper, and the bundle fetched.
    expect(fetchArtifactDownloadUrlMock).toHaveBeenCalledWith('artifact-xyz');
    expect(fetchMock).toHaveBeenCalledWith('https://signed.example/bundle.tar.gz');

    // Safe files extracted into the repo clone; traversal entry rejected.
    expect(fs.readFileSync(path.join(tmpRepo, 'committed/file.txt'), 'utf-8')).toBe('hello artifact');
    expect(outcome.rejectedEntries[0].entries).toContain('../escape.txt');

    // git add staged the extracted files, and the commit message cites the artifact_id.
    const addCall = gitCalls.find((c) => c[0] === 'add');
    expect(addCall).toEqual(['add', '--', 'committed/file.txt', 'nested/dir/data.json']);
    const commitCall = gitCalls.find((c) => c[0] === 'commit');
    expect(commitCall?.[1]).toBe('--no-verify');
    expect(commitCall?.[2]).toBe('-m');
    expect(commitCall?.[3]).toContain('artifact-xyz');
    expect(commitCall?.[3]).toContain('bundle.tar.gz');

    // Outcome + consumed-artifacts crediting.
    expect(outcome.committedArtifactIds).toEqual(['artifact-xyz']);
    expect(outcome.commits[0].sha).toBe('abc1234def567890');
    const credited = JSON.parse(fs.readFileSync(consumedFile, 'utf-8')) as string[];
    expect(credited).toContain('artifact-xyz');
    fs.unlinkSync(consumedFile);
  });

  it('skips non-committable artifact types without minting a URL or committing', async () => {
    expect(COMMITTABLE_ARTIFACT_TYPES.has('GIT_COMMIT')).toBe(true);
    expect(COMMITTABLE_ARTIFACT_TYPES.has('RESEARCH_FINDINGS')).toBe(false);

    const outcome = await consumeAndCommitArtifacts({
      taskId: 'consume-task-skip',
      repoRoot: tmpRepo,
      pointers: [{ artifact_id: 'report-1', artifact_type: 'RESEARCH_FINDINGS' }],
    });
    expect(outcome.committedArtifactIds).toEqual([]);
    expect(fetchArtifactDownloadUrlMock).not.toHaveBeenCalled();
    expect(gitCalls).toHaveLength(0);
  });

  it('is fail-soft when the download fails — no commit, no throw', async () => {
    fetchArtifactDownloadUrlMock.mockResolvedValue('https://signed.example/bundle.tar.gz');
    const download = vi.fn().mockResolvedValue(null);
    const outcome = await consumeAndCommitArtifacts({
      taskId: 'consume-task-faildl',
      repoRoot: tmpRepo,
      download,
      pointers: [{ artifact_id: 'artifact-dl', artifact_type: 'GIT_COMMIT' }],
    });
    expect(download).toHaveBeenCalled();
    expect(outcome.committedArtifactIds).toEqual([]);
    expect(gitCalls.find((c) => c[0] === 'commit')).toBeUndefined();
  });

  it('prefers a pre-resolved signed URL over minting a fresh one', async () => {
    const gz = makeTarGz([{ name: 'committed/file.txt', content: 'hi' }]);
    const download = vi.fn().mockResolvedValue(gz);
    const outcome = await consumeAndCommitArtifacts({
      taskId: 'consume-task-presigned',
      repoRoot: tmpRepo,
      signedUrls: new Map([['artifact-pre', 'https://pre.example/bundle.tar.gz']]),
      download,
      pointers: [{ artifact_id: 'artifact-pre', artifact_type: 'GIT_COMMIT' }],
    });
    expect(download).toHaveBeenCalledWith('https://pre.example/bundle.tar.gz');
    expect(fetchArtifactDownloadUrlMock).not.toHaveBeenCalled();
    expect(outcome.committedArtifactIds).toEqual(['artifact-pre']);
  });
});
