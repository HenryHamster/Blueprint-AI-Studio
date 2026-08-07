import { describe, it, expect, beforeEach, afterEach } from 'vitest';
import { execFileSync, spawnSync } from 'child_process';
import { readFileSync, writeFileSync, mkdtempSync, rmSync } from 'fs';
import path from 'path';
import os from 'os';
import { fileURLToPath } from 'url';

// Capability probes: some tests spawn python3/jq or assert a Unix executable bit.
// Those skip when the capability is absent (e.g. local Windows dev box without
// python3/jq on PATH, or no Unix exec bit) but still run fully in Linux CI.
const hasBin = (b: string): boolean => {
  try {
    return !spawnSync(b, ['--version'], { stdio: 'ignore' }).error;
  } catch {
    return false;
  }
};
const HAS_PYTHON3 = hasBin('python3');
const HAS_JQ = hasBin('jq');
const SUPPORTS_EXEC_BIT = process.platform !== 'win32';

// Regression guard for the committed before/after screenshot tooling (Phase 0).
// scripts/viewport-snapshot.sh and scripts/viewport_manifest.py are invoked by
// the agent at task time; CI must catch a syntax break or a renamed reference
// before it ships. The agent vitest suite runs in CI with bash + python3
// available (same VM that runs the agent), so static syntax + reference checks
// belong here rather than in a manual smoke test.
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const SNAPSHOT_SH = path.join(REPO_ROOT, 'scripts', 'viewport-snapshot.sh');
const MANIFEST_PY = path.join(REPO_ROOT, 'scripts', 'viewport_manifest.py');
const LIVE_SH = path.join(REPO_ROOT, 'scripts', 'viewport-live.sh');
const LIVE_CAPTURE_JS = path.join(REPO_ROOT, 'scripts', 'viewport-live-capture.js');

describe('scripts/viewport-snapshot.sh — committed snapshot tooling', () => {
  it('is valid bash (bash -n)', () => {
    expect(() => execFileSync('bash', ['-n', SNAPSHOT_SH])).not.toThrow();
  });

  it('drives the capture npm script and the manifest helper by reference', () => {
    const src = readFileSync(SNAPSHOT_SH, 'utf-8');
    expect(src).toContain('npm run test:viewport:capture');
    expect(src).toContain('scripts/viewport_manifest.py');
    // Output must live under absolute /tmp (./pr runs git add -A).
    expect(src).toContain('/tmp/viewport_shots/');
    // Dedicated worktree path — must NOT clobber ./check's baseline worktree.
    expect(src).toContain('WT="/tmp/viewport_snap_wt"');
    expect(src).not.toContain('WT="/tmp/check_viewport_baseline_wt"');
    // Infra-skip sentinel reused from check-viewport.sh.
    expect(src).toContain('VIEWPORT_GATE_INFRA_UNAVAILABLE');
  });

  // Requires python3 on PATH (spawns py_compile).
  it('manifest helper is valid python (py_compile)', () => {
    if (!HAS_PYTHON3) throw new Error('python3 is required for this test but was not found on PATH. Install python3 (Linux CI always has it).');
    expect(() => execFileSync('python3', ['-m', 'py_compile', MANIFEST_PY])).not.toThrow();
  });

  it('exits 1 when --upload is passed without --task-id', () => {
    let status = 0;
    let stderr = '';
    try {
      execFileSync('bash', [SNAPSHOT_SH, '--upload'], { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'] });
    } catch (err: unknown) {
      const e = err as { status?: number; stderr?: string };
      status = e.status ?? -1;
      stderr = e.stderr ?? '';
    }
    expect(status).toBe(1);
    expect(stderr).toContain('--upload requires --task-id');
  });

  it('wires the Phase 1 upload to the task-scoped signed-PUT endpoint + known manifest path', () => {
    const src = readFileSync(SNAPSHOT_SH, 'utf-8');
    // Posts to the screenshot upload-url endpoint with Bearer agent auth.
    expect(src).toContain('/api/agent/viewport/screenshot-upload-url');
    expect(src).toContain('X-Agent-Token: ${AGENT_API_TOKEN}');
    // Body matches ViewportScreenshotUploadUrlRequest {task_id, name, width, phase}.
    expect(src).toContain('\\"phase\\":\\"${_phase}\\"');
    // Only changed/new pairs with a known width are uploaded, capped at 8 pairs
    // (8 × before+after+diff = 24 PNGs, matching the backend 24-entry context cap).
    expect(src).toContain('select((.status == "changed" or .status == "new") and .width != null))[0:8]');
    // Publishes the manifest at the path pr-helper.ts reads.
    expect(src).toContain('/tmp/viewport_manifest_${TASK_ID}.json');
  });

  // Requires jq on PATH (runs the script's embedded jq filter).
  it('scales change_pct to a 0-1 magnitude on the "after" row only', () => {
    if (!HAS_JQ) throw new Error('jq is required for this test but was not found on PATH. Install jq (Linux CI always has it).');
    // Run the REAL jq filter embedded in the script against a sample manifest so a
    // regression in the change_pct/100 scaling (or in which rows carry magnitude)
    // is caught here, not in production. The backend rejects magnitude > 1.0, so
    // emitting an unscaled 0-100 change_pct would silently drop every upload.
    const src = readFileSync(SNAPSHOT_SH, 'utf-8');
    const m = src.match(/jq -r '(\.pairs[\s\S]*?)' "\$MANIFEST"/);
    if (!m) throw new Error('jq filter not found in viewport-snapshot.sh');
    const filter = m[1];

    const manifest = {
      pairs: [
        { status: 'changed', test: 'TaskCard', width: 375, before: '/b.png', after: '/a.png', diff: '/d.png', change_pct: 23.0 },
        { status: 'new', test: 'NewComp', width: 375, after: '/n.png', change_pct: 100.0 },
        { status: 'changed', test: 'NoPct', width: 375, before: '/b2.png', after: '/a2.png', diff: '/d2.png' },
      ],
    };
    const manifestPath = path.join(os.tmpdir(), `vp-manifest-test-${process.pid}.json`);
    writeFileSync(manifestPath, JSON.stringify(manifest));
    let out: string;
    try {
      out = execFileSync('jq', ['-r', filter, manifestPath], { encoding: 'utf-8' });
    } finally {
      rmSync(manifestPath, { force: true });
    }
    const rows = out.split('\n').filter((r) => r.length > 0).map((r) => r.split('\t'));

    // 5th column (index 4) is the magnitude. before/diff rows always carry an empty
    // magnitude; the "after" row carries change_pct/100 (or empty when absent).
    const byKey = (phase: string, test: string): string[] => {
      const row = rows.find((r) => r[0] === phase && r[1] === test);
      if (!row) throw new Error(`no ${phase} row for ${test} in jq output`);
      return row;
    };
    expect(byKey('after', 'TaskCard')[4]).toBe('0.23'); // 23.0 / 100
    expect(byKey('after', 'NewComp')[4]).toBe('1'); // 100.0 / 100
    expect(byKey('after', 'NoPct')[4]).toBe(''); // change_pct absent -> empty
    expect(byKey('before', 'TaskCard')[4]).toBe('');
    expect(byKey('diff', 'TaskCard')[4]).toBe('');
  });
});

// Spawns a real `bash -n` on the generated wrapper, so wall-clock is dominated
// by process startup on a loaded VM rather than by the assertions. Vitest's 5s
// default kills it mid-spawn under ./check's cold-cache contention.
// Same convention as handlers/bake-smoke-run.test.ts:63.
describe('writeViewportSnapshotHelper — generated ./viewport-snapshot wrapper', { timeout: 120_000 }, () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(path.join(os.tmpdir(), 'vs-test-'));
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('generates a valid bash wrapper that delegates to viewport-snapshot.sh --upload', async () => {
    // Dynamically import so the module resolves WORKSPACE from the real env;
    // we override by passing tmpDir as the workspace arg directly.
    const { writeViewportSnapshotHelper } = await import('./prompt.js');
    writeViewportSnapshotHelper('test-task-id-1234', tmpDir);

    const wrapperPath = path.join(tmpDir, 'viewport-snapshot');
    const src = readFileSync(wrapperPath, 'utf-8');

    // Must invoke viewport-snapshot.sh with --upload and baked-in task ID.
    expect(src).toContain('viewport-snapshot.sh --upload --task-id');
    expect(src).toContain('test-task-id-1234');
    // Must be valid bash.
    expect(() => execFileSync('bash', ['-n', wrapperPath])).not.toThrow();
  });

  it('bakes the task ID into the script so --task-id is not a runtime variable', async () => {
    const { writeViewportSnapshotHelper } = await import('./prompt.js');
    writeViewportSnapshotHelper('abc-task-xyz', tmpDir);

    const src = readFileSync(path.join(tmpDir, 'viewport-snapshot'), 'utf-8');
    // Task ID must appear as a literal assignment, not just forwarded from env.
    expect(src).toMatch(/TASK_ID="abc-task-xyz"/);
  });

  it('VIEWPORT_SNAPSHOT_HINT references ./viewport-snapshot (not the raw bash invocation)', async () => {
    const { VIEWPORT_SNAPSHOT_HINT } = await import('./prompt.js');
    expect(VIEWPORT_SNAPSHOT_HINT).toContain('./viewport-snapshot');
    expect(VIEWPORT_SNAPSHOT_HINT).not.toContain('bash scripts/viewport-snapshot.sh`');
  });
});

describe('scripts/viewport-live.sh — live capture script', () => {
  it('is valid bash (bash -n)', () => {
    expect(() => execFileSync('bash', ['-n', LIVE_SH])).not.toThrow();
  });

  it('drives the external capture helper and manifest by reference (no inline heredoc)', () => {
    const src = readFileSync(LIVE_SH, 'utf-8');
    // Infra-skip sentinel surfaces a Chromium bootstrap failure as exit 75.
    expect(src).toContain('VIEWPORT_GATE_INFRA_UNAVAILABLE');
    // Delegates to the real .js helper rather than embedding Node in a heredoc.
    expect(src).toContain('scripts/viewport-live-capture.js');
    // PNGs land under the live-capture tmp tree.
    expect(src).toContain('/tmp/viewport_live/');
    // Builds the SPA with the firebase.ts token-bypass env var.
    expect(src).toContain('VITE_DASHBOARD_API_TOKEN');
    // --upload guard requires a task id.
    expect(src).toContain('--upload requires --task-id');
    // Builds and serves the SPA against the live backend.
    expect(src).toContain('npm run build');
    expect(src).toContain('vite preview');
    // Reuses the shared manifest helper.
    expect(src).toContain('viewport_manifest.py');
  });

  it('exits 1 when --upload is passed without --task-id', () => {
    let status = 0;
    let stderr = '';
    try {
      execFileSync('bash', [LIVE_SH, '--upload'], { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'pipe'] });
    } catch (err: unknown) {
      const e = err as { status?: number; stderr?: string };
      status = e.status ?? -1;
      stderr = e.stderr ?? '';
    }
    expect(status).toBe(1);
    expect(stderr).toContain('--upload requires --task-id');
  });
});

describe('scripts/viewport-live-capture.js — Playwright capture helper', () => {
  it('is valid node syntax (node --check)', () => {
    expect(() => execFileSync('node', ['--check', LIVE_CAPTURE_JS])).not.toThrow();
  });

  it('uses playwright and captures full-page screenshots over the given args', () => {
    const src = readFileSync(LIVE_CAPTURE_JS, 'utf-8');
    // Requires the real playwright package, not an inline browser heredoc.
    expect(src).toContain("require('playwright')");
    // Accepts the url/outDir/widths positional args.
    expect(src).toContain('url, outDir, widthsArg');
    // Captures full-page screenshots.
    expect(src).toContain('fullPage: true');
  });

  // Requires python3 on PATH (runs viewport_manifest.py).
  it('writes PNGs with the px suffix required by viewport_manifest.py width regex', () => {
    if (!HAS_PYTHON3) throw new Error('python3 is required for this test but was not found on PATH. Install python3 (Linux CI always has it).');
    // viewport_manifest.py uses _WIDTH_RE = re.compile(r"-(\d+)px\.png$").
    // If the capture helper omits "px", the manifest emits width: null and every
    // upload returns 422 from the backend (width: int = Field(ge=1, le=4096)).
    // This test creates a sample capture directory with a correctly-named file,
    // runs viewport_manifest.py over it, and asserts the width is a non-null integer.
    const tmpDir = mkdtempSync(path.join(os.tmpdir(), 'vl-manifest-test-'));
    const outManifest = path.join(tmpDir, 'manifest.json');
    try {
      // Simulate what viewport-live-capture.js writes: live-<width>px.png
      writeFileSync(path.join(tmpDir, 'live-375px.png'), Buffer.alloc(0));
      // Also write a file with the OLD (broken) naming to confirm manifest yields null width.
      writeFileSync(path.join(tmpDir, 'live-375.png'), Buffer.alloc(0));

      // viewport_manifest.py argv: before_dir after_dir base run_id out_manifest
      // (mirrors how viewport-live.sh calls it: before_dir="" after_dir="$OUT_DIR" …)
      execFileSync(
        'python3',
        [MANIFEST_PY, '', tmpDir, 'live', 'test-run', outManifest],
        { encoding: 'utf-8' },
      );
      const manifest = JSON.parse(readFileSync(outManifest, 'utf-8')) as {
        pairs: Array<{ width: number | null; test: string; test_file: string }>;
      };

      // live-375px.png: _test_name strips "-375px.png" → test="live", width=375.
      const withPx = manifest.pairs.find((p) => p.test_file === 'live-375px.png');
      expect(withPx).toBeDefined();
      if (!withPx) throw new Error('pair for live-375px.png not found in manifest');
      expect(withPx.width).toBe(375);

      // live-375.png (broken name): regex doesn't match → width=null.
      const withoutPx = manifest.pairs.find((p) => p.test_file === 'live-375.png');
      expect(withoutPx).toBeDefined();
      if (!withoutPx) throw new Error('pair for live-375.png not found in manifest');
      expect(withoutPx.width).toBeNull();
    } finally {
      rmSync(tmpDir, { recursive: true, force: true });
    }
  });
});

describe('writeViewportLiveHelper — generated ./viewport-live wrapper', () => {
  let tmpDir: string;

  beforeEach(() => {
    tmpDir = mkdtempSync(path.join(os.tmpdir(), 'vl-test-'));
  });

  afterEach(() => {
    rmSync(tmpDir, { recursive: true, force: true });
  });

  it('generates a valid bash wrapper that delegates to viewport-live.sh --upload', async () => {
    const { writeViewportLiveHelper } = await import('./prompt.js');
    writeViewportLiveHelper('live-task-id-1234', tmpDir);

    const wrapperPath = path.join(tmpDir, 'viewport-live');
    const src = readFileSync(wrapperPath, 'utf-8');

    // Must invoke viewport-live.sh with --upload and the baked-in task ID.
    expect(src).toContain('viewport-live.sh --upload --task-id');
    expect(src).toContain('live-task-id-1234');
    // Must be valid bash.
    expect(() => execFileSync('bash', ['-n', wrapperPath])).not.toThrow();
  });

  it('bakes the task ID into the script as a literal assignment', async () => {
    const { writeViewportLiveHelper } = await import('./prompt.js');
    writeViewportLiveHelper('live-abc-xyz', tmpDir);

    const src = readFileSync(path.join(tmpDir, 'viewport-live'), 'utf-8');
    expect(src).toMatch(/TASK_ID="live-abc-xyz"/);
  });

  // Requires a Unix executable bit (Windows has none; chmod is a no-op there).
  it.skipIf(!SUPPORTS_EXEC_BIT)('writes the wrapper as an executable file', async () => {
    const { writeViewportLiveHelper } = await import('./prompt.js');
    writeViewportLiveHelper('mode-task', tmpDir);

    const { statSync } = await import('fs');
    const mode = statSync(path.join(tmpDir, 'viewport-live')).mode;
    // Owner-execute bit must be set (0o100).
    expect(mode & 0o100).toBe(0o100);
  });

  it('VIEWPORT_LIVE_HINT references ./viewport-live and the infra-skip contract', async () => {
    const { VIEWPORT_LIVE_HINT } = await import('./prompt.js');
    expect(VIEWPORT_LIVE_HINT).toContain('./viewport-live');
    expect(VIEWPORT_LIVE_HINT).toContain('/tmp/viewport_live/');
    expect(VIEWPORT_LIVE_HINT).toContain('ORCHESTRATOR_URL');
    expect(VIEWPORT_LIVE_HINT).toContain('ADMIN_API_KEY');
    expect(VIEWPORT_LIVE_HINT).toContain('75');
  });
});
