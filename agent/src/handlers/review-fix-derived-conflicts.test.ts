/**
 * Tests for the mechanical derived-artifact conflict path.
 *
 * This module decides whether a fix task spends a Claude turn, and — when it
 * does not — whether the task is COMPLETED. Both directions are dangerous in a
 * different way, so both are pinned here:
 *
 *  - Completing too eagerly VOIDS real work. That regression already happened
 *    (2026-08-20, fix task 10384f8e: scoped to three BP- review issue ids, hit
 *    a clean PR, voided with all three findings untouched). So the tests assert
 *    that a task carrying review issue ids never takes the no-Claude exit, even
 *    when the mechanical resolution succeeded.
 *  - Declining too eagerly is merely wasteful, which is why every failure mode
 *    here — resolver missing, non-zero exit, unparseable report, failed commit,
 *    failed push — must decline rather than throw. A thrown error would fail the
 *    fix task instead of falling through to the agent that can still fix it.
 *
 * The push is deliberately part of the success condition. A resolution that
 * stays in the VM leaves the PR unmergeable on GitHub, so the orchestrator
 * respawns the same fix forever; `test_push_failure_declines` pins that.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';

const execFileSyncMock = vi.hoisted(() => vi.fn());
vi.mock('child_process', () => ({ execFileSync: execFileSyncMock }));

import {
  CONFLICT_ONLY_FIX_REASONS,
  DERIVED_RESOLVER_SCRIPT,
  describeResolution,
  isConflictOnlyFixTask,
  parseResolverReport,
  resolveDerivedArtifactConflicts,
} from './review-fix-derived-conflicts.js';

const COMPLETE_REPORT = JSON.stringify({
  complete: true,
  resolved: ['backend/.test_durations', 'test-estate-ranked-queue.md'],
  unhandled: [],
  details: {
    'backend/.test_durations': 'union of insert-only measurement maps',
    'test-estate-ranked-queue.md': 'regenerated from the merged tree',
  },
  error: null,
});

/** Captures what the caller would have streamed to the task log. */
let logged: string[] = [];

function run(push = vi.fn().mockResolvedValue(undefined)) {
  return resolveDerivedArtifactConflicts({
    workspace: '/ws', branch: 'feat/x', log: (line: string) => { logged.push(line); }, push,
  });
}

beforeEach(() => {
  execFileSyncMock.mockReset();
  logged = [];
});

// ── isConflictOnlyFixTask: the guard that decides whether work can be voided ──

describe('isConflictOnlyFixTask', () => {
  it('accepts the structured spawn reasons whose whole job is the rebase', () => {
    expect(CONFLICT_ONLY_FIX_REASONS.has('merge_conflict')).toBe(true);
    expect(isConflictOnlyFixTask('merge_conflict', 'PR #1 has conflicts')).toBe(true);
    expect(isConflictOnlyFixTask('update_branch_failed', 'PR #1 is behind')).toBe(true);
  });

  it('refuses when the prompt carries review issue ids', () => {
    // The 2026-08-20 regression shape: conflict reason AND real findings.
    expect(isConflictOnlyFixTask('merge_conflict', 'fix BP-0123456789ab now')).toBe(false);
  });

  it('refuses when no structured reason was stamped', () => {
    // Absent provenance, assume real work: wasting spend beats voiding a task.
    expect(isConflictOnlyFixTask(undefined, 'PR #1 has merge conflicts')).toBe(false);
    expect(isConflictOnlyFixTask('ci_failure', 'PR #1 has merge conflicts')).toBe(false);
  });
});

// ── parseResolverReport: fail closed on anything unusable ────────────────────

describe('parseResolverReport', () => {
  it('reads a well-formed report', () => {
    const report = parseResolverReport(COMPLETE_REPORT);
    expect(report?.complete).toBe(true);
    expect(report?.resolved).toEqual(['backend/.test_durations', 'test-estate-ranked-queue.md']);
  });

  it.each([
    ['not json', 'boom'],
    ['a non-object', '"a string"'],
    ['a missing complete flag', '{"resolved":[],"unhandled":[]}'],
    ['a non-boolean complete flag', '{"complete":"yes","resolved":[],"unhandled":[]}'],
    ['missing arrays', '{"complete":true}'],
  ])('returns null for %s', (_label, payload) => {
    expect(parseResolverReport(payload)).toBeNull();
  });
});

describe('describeResolution', () => {
  it('names every path and what was done to it', () => {
    const summary = describeResolution(parseResolverReport(COMPLETE_REPORT)!);
    expect(summary).toContain('backend/.test_durations');
    expect(summary).toContain('union of insert-only measurement maps');
    expect(summary).toContain('no agent turn');
  });
});

// ── resolveDerivedArtifactConflicts: the whole sequence ──────────────────────

describe('resolveDerivedArtifactConflicts', () => {
  it('resolves, commits the merge and pushes, then reports complete', async () => {
    execFileSyncMock.mockImplementation((cmd: string) =>
      cmd === 'python3' ? COMPLETE_REPORT : '');
    const push = vi.fn().mockResolvedValue(undefined);

    const outcome = await run(push);

    expect(outcome.complete).toBe(true);
    expect(outcome.resolved).toEqual(['backend/.test_durations', 'test-estate-ranked-queue.md']);
    expect(execFileSyncMock).toHaveBeenCalledWith(
      'python3',
      [DERIVED_RESOLVER_SCRIPT, '--repo', '/ws', '--json'],
      expect.objectContaining({ cwd: '/ws' }),
    );
    // The merge must actually be committed, or the push carries nothing.
    expect(execFileSyncMock).toHaveBeenCalledWith(
      'git', ['-C', '/ws', 'commit', '--no-edit'], expect.anything(),
    );
    expect(push).toHaveBeenCalledWith('feat/x');
    // The task log must show what was resolved and that it was pushed, or a
    // no-Claude completion is unauditable after the fact.
    expect(logged.join('')).toContain('backend/.test_durations');
    expect(logged.join('')).toContain('Pushed the mechanically resolved merge to feat/x');
  });

  it('declines and names the unhandled path when the resolver exits non-zero', async () => {
    // Exit 1 carries the report on stdout; the caller must learn WHY.
    const err = Object.assign(new Error('exit 1'), {
      stdout: JSON.stringify({
        complete: false, resolved: [], unhandled: ['backend/app/models.py'],
        details: {}, error: null,
      }),
    });
    execFileSyncMock.mockImplementation((cmd: string) => {
      if (cmd === 'python3') throw err;
      return '';
    });
    const push = vi.fn();

    const outcome = await run(push);

    expect(outcome.complete).toBe(false);
    expect(outcome.unhandled).toEqual(['backend/app/models.py']);
    expect(outcome.summary).toContain('backend/app/models.py');
    expect(push).not.toHaveBeenCalled();
  });

  it('declines without throwing when the resolver cannot run at all', async () => {
    execFileSyncMock.mockImplementation((cmd: string) => {
      if (cmd === 'python3') throw new Error('python3: not found');
      return '';
    });
    await expect(run()).resolves.toMatchObject({ complete: false });
  });

  it('declines when the resolver exits 0 with an unusable report', async () => {
    execFileSyncMock.mockImplementation((cmd: string) => (cmd === 'python3' ? 'not json' : ''));
    const push = vi.fn();
    expect((await run(push)).complete).toBe(false);
    expect(push).not.toHaveBeenCalled();
  });

  it('declines when the report says the resolution was incomplete', async () => {
    execFileSyncMock.mockImplementation((cmd: string) => (cmd === 'python3'
      ? JSON.stringify({ complete: false, resolved: [], unhandled: ['x'], details: {}, error: null })
      : ''));
    const push = vi.fn();
    expect((await run(push)).complete).toBe(false);
    expect(push).not.toHaveBeenCalled();
  });

  it('declines when the merge commit fails, without pushing', async () => {
    execFileSyncMock.mockImplementation((cmd: string, args: string[]) => {
      if (cmd === 'python3') return COMPLETE_REPORT;
      if (args.includes('commit')) throw new Error('nothing to commit');
      return '';
    });
    const push = vi.fn();

    const outcome = await run(push);

    expect(outcome.complete).toBe(false);
    expect(push).not.toHaveBeenCalled();
  });

  it('declines when the push fails, so the task is never completed unpushed', async () => {
    // A resolution that never leaves the VM leaves the PR unmergeable on
    // GitHub, so the orchestrator would respawn this same fix forever.
    execFileSyncMock.mockImplementation((cmd: string) => (cmd === 'python3' ? COMPLETE_REPORT : ''));
    const push = vi.fn().mockRejectedValue(new Error('protected branch'));

    const outcome = await run(push);

    expect(outcome.complete).toBe(false);
    expect(outcome.summary).toContain('push failed');
  });
});
