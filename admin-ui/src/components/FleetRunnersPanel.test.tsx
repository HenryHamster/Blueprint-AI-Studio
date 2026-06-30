import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FleetRunnersPanel } from './FleetRunnersPanel';
import { runnerSetupSnippets } from './FleetRunnersPanel.utils';
import type { FleetRunner } from '../api';

const SAMPLE_RUNNERS: FleetRunner[] = [
  {
    id: 'runner-abc123',
    hostname: 'gpu-worker-01',
    version: '1.2.3',
    status: 'active',
    capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
    last_seen_at: new Date(Date.now() - 30_000).toISOString(),
    created_at: '2026-06-01T00:00:00Z',
  },
  {
    id: 'runner-def456',
    hostname: null,
    version: null,
    status: 'active',
    capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 1 },
    last_seen_at: null,
    created_at: '2026-06-10T00:00:00Z',
  },
];

vi.mock('../api', () => ({
  listFleetRunners: vi.fn(async () => SAMPLE_RUNNERS),
  registerFleetRunner: vi.fn(async () => ({ runner_id: 'runner-new', token: 'tok-secret-xyz' })),
  revokeFleetRunner: vi.fn(async () => {}),
}));

import { listFleetRunners, registerFleetRunner, revokeFleetRunner } from '../api';

describe('FleetRunnersPanel — behavioral', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function mount(projectId = 'proj-test') {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<FleetRunnersPanel projectId={projectId} />);
    });
    await act(async () => {});
    return host as HTMLDivElement;
  }

  test('runnerSetupSnippets safely quotes setup commands', () => {
    const snippets = runnerSetupSnippets("tok'abc", 'https://blueprint.test/a path', 'strategic_local');

    expect(Object.keys(snippets)).toEqual(expect.arrayContaining(['windowsPowerShell', 'macosLinux']));
    expect(snippets.windowsPowerShell).toContain("$env:BLUEPRINT_RUNNER_TOKEN = 'tok''abc'");
    expect(snippets.windowsPowerShell).toContain("$env:BLUEPRINT_ORCHESTRATOR_URL = 'https://blueprint.test/a path'");
    expect(snippets.windowsPowerShell).toContain("$env:BLUEPRINT_RUNNER_MAX_CONCURRENT = '1'");
    expect(snippets.windowsPowerShell).toContain('py -3 scripts/local_runner.py');
    expect(snippets.macosLinux).toContain("BLUEPRINT_RUNNER_TOKEN='tok'\\''abc'");
    expect(snippets.macosLinux).toContain("BLUEPRINT_ORCHESTRATOR_URL='https://blueprint.test/a path'");
    expect(snippets.macosLinux).toContain('BLUEPRINT_RUNNER_MAX_CONCURRENT=1');
    expect(snippets.macosLinux).toContain('python3 scripts/local_runner.py');

    expect(runnerSetupSnippets('tok', 'https://blueprint.test').macosLinux).not.toContain('BLUEPRINT_RUNNER_MAX_CONCURRENT');
  });

  // ── livenessLabel ────────────────────────────────────────────────────────────

  test('livenessLabel shows "live" for runner seen within threshold', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        last_seen_at: new Date(Date.now() - 30_000).toISOString(), // 30s ago — live
      },
    ]);
    const el = await mount();
    expect(el.textContent).toContain('live');
    expect(el.querySelector('.text-emerald-400')).not.toBeNull();
  });

  test('livenessLabel shows "stale" for runner seen 7 minutes ago', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        // Inside the stale window: past the 300s liveness cutoff (mirrors the
        // backend dispatch threshold) but before the 600s offline cutoff.
        last_seen_at: new Date(Date.now() - 7 * 60 * 1000).toISOString(), // 7 min ago — stale
      },
    ]);
    const el = await mount();
    expect(el.textContent).toContain('stale');
    expect(el.querySelector('.text-amber-400')).not.toBeNull();
  });

  test('livenessLabel shows "offline" for runner seen more than 10 minutes ago', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        last_seen_at: new Date(Date.now() - 15 * 60 * 1000).toISOString(), // 15 min ago — offline
      },
    ]);
    const el = await mount();
    expect(el.textContent).toContain('offline');
  });

  test('livenessLabel shows "never seen" for runner with null last_seen_at', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      { ...SAMPLE_RUNNERS[0], last_seen_at: null },
    ]);
    const el = await mount();
    expect(el.textContent).toContain('never seen');
  });

  // ── Enroll runner ────────────────────────────────────────────────────────────

  test('enrolling a runner calls registerFleetRunner and shows the token dialog', async () => {
    const el = await mount();

    const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
    expect(enrollBtn).not.toBeNull();

    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(registerFleetRunner).toHaveBeenCalledWith('proj-test');

    // Token dialog appears with the returned token
    expect(el.textContent).toContain('Runner enrolled');
    expect(el.textContent).toContain('tok-secret-xyz');
    expect(el.textContent).toContain('runner-new');
    // Security warning about one-time display
    expect(el.textContent).toContain('shown only once');
  });

  test('enrolling a strategic runner sends the strategic preset', async () => {
    const clipboardWrite = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    const el = await mount();

    const strategicBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Strategic runner'));
    expect(strategicBtn).not.toBeNull();

    await act(async () => { (strategicBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(registerFleetRunner).toHaveBeenCalledWith('proj-test', { preset: 'strategic_local' });
    expect(el.textContent).toContain('Strategic runner enrolled');
    expect(el.textContent).toContain('Windows PowerShell');
    expect(el.textContent).toContain('macOS/Linux');
    expect(el.textContent).toContain("$env:BLUEPRINT_RUNNER_MAX_CONCURRENT = '1'");
    expect(el.textContent).toContain('BLUEPRINT_RUNNER_MAX_CONCURRENT=1');
    expect(el.textContent).toContain('Next: start the runner; first heartbeat marks it live.');

    const psCopyBtn = el.querySelector('button[title="Copy Windows PowerShell setup snippet"]');
    const shellCopyBtn = el.querySelector('button[title="Copy macOS/Linux setup snippet"]');
    expect(psCopyBtn).not.toBeNull();
    expect(shellCopyBtn).not.toBeNull();

    await act(async () => { (psCopyBtn as HTMLButtonElement).click(); });
    expect(clipboardWrite).toHaveBeenLastCalledWith(expect.stringContaining("$env:BLUEPRINT_RUNNER_MAX_CONCURRENT = '1'"));
    expect((psCopyBtn as HTMLButtonElement).querySelector('.text-emerald-400')).not.toBeNull();
    expect((shellCopyBtn as HTMLButtonElement).querySelector('.text-emerald-400')).toBeNull();

    await act(async () => { (shellCopyBtn as HTMLButtonElement).click(); });
    expect(clipboardWrite).toHaveBeenLastCalledWith(expect.stringContaining('BLUEPRINT_RUNNER_MAX_CONCURRENT=1'));
    expect((psCopyBtn as HTMLButtonElement).querySelector('.text-emerald-400')).not.toBeNull();
    expect((shellCopyBtn as HTMLButtonElement).querySelector('.text-emerald-400')).not.toBeNull();
  });

  test('token dialog shows token expiry when registration returns it', async () => {
    vi.mocked(registerFleetRunner).mockResolvedValueOnce({
      runner_id: 'runner-new',
      token: 'tok-secret-xyz',
      token_expires_at: '2026-07-01T12:00:00Z',
    });
    const el = await mount();

    const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(el.textContent).toContain('Runner enrolled');
    expect(el.textContent).toContain('Token expires');
  });

  test('dismissing the token dialog removes it', async () => {
    const el = await mount();

    const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(el.textContent).toContain('tok-secret-xyz');

    const dismissBtn = [...el.querySelectorAll('button')].find(b =>
      b.textContent?.includes("I've copied the token")
    );
    expect(dismissBtn).not.toBeNull();
    await act(async () => { (dismissBtn as HTMLButtonElement).click(); });

    expect(el.textContent).not.toContain('tok-secret-xyz');
    expect(el.textContent).not.toContain('Runner enrolled');
  });

  test('enroll error is shown and no token dialog appears', async () => {
    vi.mocked(registerFleetRunner).mockRejectedValueOnce(new Error('HTTP 500'));
    const el = await mount();

    const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(el.textContent).toContain('HTTP 500');
    expect(el.textContent).not.toContain('Runner enrolled');
  });

  // ── Revoke runner ────────────────────────────────────────────────────────────

  test('revoking a runner prompts window.confirm before calling revokeFleetRunner', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const el = await mount();

    const revokeBtn = el.querySelector('button[title="Revoke runner"]');
    expect(revokeBtn).not.toBeNull();

    await act(async () => { (revokeBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(confirmSpy).toHaveBeenCalled();
    expect(revokeFleetRunner).toHaveBeenCalledWith('proj-test', SAMPLE_RUNNERS[0].id);

    confirmSpy.mockRestore();
  });

  test('cancelling the revoke confirm does not call revokeFleetRunner', async () => {
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const el = await mount();

    const revokeBtn = el.querySelector('button[title="Revoke runner"]');
    await act(async () => { (revokeBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(confirmSpy).toHaveBeenCalled();
    expect(revokeFleetRunner).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  test('revoke error is displayed in the panel', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    vi.mocked(revokeFleetRunner).mockRejectedValueOnce(new Error('Network error'));
    const el = await mount();

    const revokeBtn = el.querySelector('button[title="Revoke runner"]');
    await act(async () => { (revokeBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(el.textContent).toContain('Network error');
    vi.restoreAllMocks();
  });

  // ── capsSummary ──────────────────────────────────────────────────────────────

  test('capsSummary renders "all task types" for task_types="all"', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      { ...SAMPLE_RUNNERS[0], capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 4 } },
    ]);
    const el = await mount();
    expect(el.textContent).toContain('all task types');
    expect(el.textContent).toContain('max 4 concurrent');
  });

  test('GPU badge appears for runners with has_gpu=true', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      { ...SAMPLE_RUNNERS[0], capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 1 } },
    ]);
    const el = await mount();
    expect(el.textContent).toContain('GPU');
  });

  test('backend health renders live state and available slot count', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: {
          task_types: ['design_doc_update', 'strategic_review'],
          has_gpu: false,
          max_concurrent: 1,
          runner_preset: 'strategic_local',
          workload: 'strategic',
        },
        health: {
          is_live: true,
          seconds_since_last_seen: 12,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 1,
          available_slots: 1,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();
    expect(el.textContent).toContain('live');
    expect(el.textContent).toContain('Strategic capable');
    expect(el.querySelector('[data-testid="fleet-runner-strategic-readiness"]')?.textContent).toBe('1 live');
    expect(el.textContent).toContain('1 slots free');
    expect(el.textContent).toContain('design_doc_update, strategic_review');
  });

  test('live all-capability runner counts as strategic-capable', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 2 },
        health: {
          is_live: true,
          seconds_since_last_seen: 12,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: 2,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    expect(el.querySelector('[data-testid="fleet-runner-strategic-readiness"]')?.textContent).toBe('1 live');
    expect(el.textContent).toContain('Strategic capable');
  });

  test('summary capacity ignores inactive runners even with recent health', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        status: 'inactive',
        capabilities: {
          task_types: ['design_doc_update', 'strategic_review'],
          has_gpu: false,
          max_concurrent: 3,
          runner_preset: 'strategic_local',
          workload: 'strategic',
        },
        health: {
          is_live: true,
          seconds_since_last_seen: 5,
          active_task_count: 1,
          active_compute_job_count: 0,
          active_work_count: 1,
          max_concurrent: 3,
          available_slots: 2,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    expect(el.querySelector('[data-testid="fleet-runner-work-capacity"]')?.textContent).toBe('0/-');
    expect(el.querySelector('[data-testid="fleet-runner-strategic-readiness"]')?.textContent).toBe('none live');
    expect(el.textContent).toContain('offline (deactivated)');
    expect(el.textContent).not.toContain('1 active');
    expect(el.textContent).not.toContain('2 slots free');
  });

  test('local-only compute warning appears when no live compute runner exists', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: {
          task_types: ['design_doc_update', 'strategic_review'],
          has_gpu: false,
          max_concurrent: 1,
          runner_preset: 'strategic_local',
        },
        health: {
          is_live: true,
          seconds_since_last_seen: 12,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 1,
          available_slots: 1,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
    ]);

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<FleetRunnersPanel projectId="proj-test" requireLocalRunner />);
    });
    await act(async () => {});

    expect(host.textContent).toContain('Local-only compute is enabled');
    expect(host.textContent).toContain('wait until a live compute-capable runner connects');
  });

  test('empty state renders when no runners enrolled', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([]);
    const el = await mount();
    expect(el.textContent).toContain('No runners enrolled');
  });

  test('load error is displayed in the panel', async () => {
    vi.mocked(listFleetRunners).mockRejectedValueOnce(new Error('Unauthorized'));
    const el = await mount();
    expect(el.textContent).toContain('Unauthorized');
  });
});
