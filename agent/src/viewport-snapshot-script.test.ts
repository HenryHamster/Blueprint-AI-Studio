import { describe, it, expect } from 'vitest';
import { execFileSync } from 'child_process';
import { readFileSync } from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

// Regression guard for the committed before/after screenshot tooling (Phase 0).
// scripts/viewport-snapshot.sh and scripts/viewport_manifest.py are invoked by
// the agent at task time; CI must catch a syntax break or a renamed reference
// before it ships. The agent vitest suite runs in CI with bash + python3
// available (same VM that runs the agent), so static syntax + reference checks
// belong here rather than in a manual smoke test.
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const SNAPSHOT_SH = path.join(REPO_ROOT, 'scripts', 'viewport-snapshot.sh');
const MANIFEST_PY = path.join(REPO_ROOT, 'scripts', 'viewport_manifest.py');

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

  it('manifest helper is valid python (py_compile)', () => {
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
    // Only changed/new pairs with a known width are uploaded, capped at 12 pairs.
    expect(src).toContain('select((.status == "changed" or .status == "new") and .width != null))[0:12]');
    // Publishes the manifest at the path pr-helper.ts reads.
    expect(src).toContain('/tmp/viewport_manifest_${TASK_ID}.json');
  });
});
