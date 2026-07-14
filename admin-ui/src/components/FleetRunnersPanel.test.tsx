import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FleetRunnersPanel } from './FleetRunnersPanel';
import { runnerSetupSnippets, runnerInstallSnippet, RUNNER_PREREQUISITES, DEFAULT_ORCHESTRATOR_URL } from './FleetRunnersPanel.utils';
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

// Distinct backend API base URL: the enroll snippet must point runners here (the
// orchestrator API), NOT at the admin-UI hosting origin (window.location.origin,
// which happy-dom serves at http://localhost). Firebase Hosting has no /api/** route,
// so heartbeats POSTed to the UI origin silently vanish (SPA HTML 200).
const API_BASE_URL_FIXTURE = 'https://blueprint-orchestrator-xyz.a.run.app';

// React tracks controlled-input values via a native value setter, so assigning
// `input.value` directly is invisible to onChange. Drive it through the prototype
// setter, then dispatch the 'input' event React binds to for text inputs.
function typeInto(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

// Open the per-runner Configure panel (all editable config lives there now,
// reached via the gear toggle in the runner's action strip).
async function openConfig(el: HTMLDivElement) {
  const toggle = el.querySelector('[data-testid="fleet-runner-configure-toggle"]') as HTMLButtonElement;
  if (!toggle) throw new Error('Configure toggle not found');
  await act(async () => { toggle.click(); });
  await act(async () => {});
}

// Click the single "Save changes" button inside an open Configure panel.
async function saveConfig(el: HTMLDivElement) {
  const saveBtn = el.querySelector('[data-testid="fleet-runner-config-save"]') as HTMLButtonElement;
  if (!saveBtn) throw new Error('Config save button not found');
  await act(async () => { saveBtn.click(); });
  await act(async () => {});
}

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    API_BASE_URL: 'https://blueprint-orchestrator-xyz.a.run.app',
    listFleetRunners: vi.fn(async () => SAMPLE_RUNNERS),
    registerFleetRunner: vi.fn(async () => ({ runner_id: 'runner-new', token: 'tok-secret-xyz' })),
    rotateFleetRunnerToken: vi.fn(async () => ({ runner_id: 'runner-abc123', token: 'tok-rotated-xyz' })),
    revokeFleetRunner: vi.fn(async () => {}),
    updateRunnerCapabilities: vi.fn(async () => SAMPLE_RUNNERS[0]),
    // Default allow-list: enrollment project only. Individual tests override.
    listRunnerProjects: vi.fn(async (_p: string, runnerId: string) => ({
      runner_id: runnerId,
      enrollment_project_id: 'proj-test',
      projects: [{ project_id: 'proj-test', is_enrollment: true }],
    })),
    addRunnerProject: vi.fn(async (_p: string, runnerId: string) => ({
      runner_id: runnerId,
      enrollment_project_id: 'proj-test',
      projects: [{ project_id: 'proj-test', is_enrollment: true }],
    })),
    removeRunnerProject: vi.fn(async (_p: string, runnerId: string) => ({
      runner_id: runnerId,
      enrollment_project_id: 'proj-test',
      projects: [{ project_id: 'proj-test', is_enrollment: true }],
    })),
    listOwnedProjects: vi.fn(async () => [
      { id: 'proj-test', name: 'test-project' },
      { id: 'proj-extra', name: 'extra-project' },
    ]),
  });
});

import { listFleetRunners, registerFleetRunner, revokeFleetRunner, rotateFleetRunnerToken, updateRunnerCapabilities, fetchRunnerStats, fetchRunnerTelemetry, listRunnerProjects, addRunnerProject, removeRunnerProject } from '../api';

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

  async function mount(
    projectId = 'proj-test',
    opts: { onNavigateToTask?: (taskId: string) => void } = {},
  ) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <FleetRunnersPanel projectId={projectId} onNavigateToTask={opts.onNavigateToTask} />,
      );
    });
    await act(async () => {});
    return host as HTMLDivElement;
  }

  test('runnerSetupSnippets builds the blueprint-runner command with safe quoting', () => {
    const snippets = runnerSetupSnippets("tok'abc", 'https://blueprint.test/a path');

    expect(Object.keys(snippets)).toEqual(expect.arrayContaining(['windowsPowerShell', 'macosLinux']));
    // Windows invokes the python launcher directly; macOS/Linux invoke the wrapper
    // via `bash blueprint-runner` (works regardless of the tracked file's exec bit).
    expect(snippets.windowsPowerShell).toContain('py -3 scripts/blueprint_runner.py start');
    expect(snippets.macosLinux).toContain('bash blueprint-runner start');
    // The token rides on --token, quoted safely for each shell.
    expect(snippets.windowsPowerShell).toContain("--token 'tok''abc'");
    expect(snippets.macosLinux).toContain("--token 'tok'\\''abc'");
    // A non-default backend surfaces an explicit --url override.
    expect(snippets.windowsPowerShell).toContain("--url 'https://blueprint.test/a path'");
    expect(snippets.macosLinux).toContain("--url 'https://blueprint.test/a path'");
    // The orchestrator URL is baked into blueprint-runner — no env-var plumbing remains.
    expect(snippets.macosLinux).not.toContain('BLUEPRINT_ORCHESTRATOR_URL');
    expect(snippets.windowsPowerShell).not.toContain('local_runner.py');

    // When the project's backend matches the baked-in prod default, --url is omitted.
    const prod = runnerSetupSnippets('tok', DEFAULT_ORCHESTRATOR_URL);
    expect(prod.macosLinux).toBe("bash blueprint-runner start --token 'tok'");
    expect(prod.macosLinux).not.toContain('--url');
  });

  test('runnerInstallSnippet builds durable systemd install commands with safe quoting', () => {
    const install = runnerInstallSnippet("tok'abc", 'https://blueprint.test/a path');
    // Per-user (systemd --user) and system-wide (sudo) durable service installs.
    expect(install.userService).toContain('bash blueprint-runner install');
    expect(install.userService).not.toContain('sudo');
    expect(install.systemService).toContain('sudo bash blueprint-runner install --system');
    // The token rides on --token, single-quote-escaped for bash.
    expect(install.userService).toContain("--token 'tok'\\''abc'");
    expect(install.systemService).toContain("--token 'tok'\\''abc'");
    // A non-default backend surfaces an explicit --url override on both.
    expect(install.userService).toContain("--url 'https://blueprint.test/a path'");
    expect(install.systemService).toContain("--url 'https://blueprint.test/a path'");

    // Prod default backend omits --url (baked into the launcher).
    const prod = runnerInstallSnippet('tok', DEFAULT_ORCHESTRATOR_URL);
    expect(prod.userService).toBe("bash blueprint-runner install --token 'tok'");
    expect(prod.userService).not.toContain('--url');

    // Prerequisites cover the real host requirements the launcher checks.
    const joined = RUNNER_PREREQUISITES.join(' ');
    expect(joined).toContain('Node.js >= 22');
    expect(joined).toContain('@anthropic-ai/claude-code');
    expect(joined).toContain('blueprint-runner check');
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

  // ── Isolation-backend inventory ──────────────────────────────────────────────

  test('renders isolation-backend pills and selected agent/compute backends', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: {
          ...SAMPLE_RUNNERS[0].capabilities,
          isolation_backends: { bwrap_ok: true, docker_usable: true, podman_usable: false },
          agent_backend: 'bwrap',
          compute_backend: 'docker',
        },
      },
    ]);
    const el = await mount();
    const section = el.querySelector('[data-testid="fleet-runner-backends"]');
    expect(section).not.toBeNull();
    // Only the truthy flags surface as pills (podman is false → absent).
    expect(section?.textContent).toContain('bwrap');
    expect(section?.textContent).toContain('docker');
    expect(section?.textContent).not.toContain('podman');
    // Selected backends are called out with a → indicator.
    expect(section?.textContent).toContain('agent→bwrap');
    expect(section?.textContent).toContain('compute→docker');
    // The selected bwrap pill gets the emerald highlight; a non-selected pill would not.
    expect(section?.querySelector('.text-emerald-300')).not.toBeNull();
  });

  test('hides the backends section for an older runner without isolation_backends', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
      },
    ]);
    const el = await mount();
    expect(el.querySelector('[data-testid="fleet-runner-backends"]')).toBeNull();
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

  test('enroll snippet targets the API base URL, not the admin-UI hosting origin', async () => {
    const clipboardWrite = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    const el = await mount();

    const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    // The command must point the runner at the backend API base (via --url), not
    // window.location.origin (the SPA host has no /api/** route → heartbeats vanish).
    const shellCopyBtn = el.querySelector('button[title="Copy macOS/Linux setup snippet"]');
    const psCopyBtn = el.querySelector('button[title="Copy Windows PowerShell setup snippet"]');
    expect(shellCopyBtn).not.toBeNull();
    expect(psCopyBtn).not.toBeNull();

    await act(async () => { (shellCopyBtn as HTMLButtonElement).click(); });
    const shellSnippet = clipboardWrite.mock.calls.at(-1)?.[0] as string;
    expect(shellSnippet).toContain(`--url '${API_BASE_URL_FIXTURE}'`);
    expect(shellSnippet).not.toContain(window.location.origin);

    await act(async () => { (psCopyBtn as HTMLButtonElement).click(); });
    const psSnippet = clipboardWrite.mock.calls.at(-1)?.[0] as string;
    expect(psSnippet).toContain(`--url '${API_BASE_URL_FIXTURE}'`);
    expect(psSnippet).not.toContain(window.location.origin);
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
    // The clean baked-in-URL command replaces the old env-var plumbing; it is the
    // same for every preset (the preset is encoded server-side on the runner row).
    expect(el.textContent).toContain('py -3 scripts/blueprint_runner.py start');
    expect(el.textContent).toContain('bash blueprint-runner start');
    expect(el.textContent).toContain('the first heartbeat connects it below');

    const psCopyBtn = el.querySelector('button[title="Copy Windows PowerShell setup snippet"]');
    const shellCopyBtn = el.querySelector('button[title="Copy macOS/Linux setup snippet"]');
    expect(psCopyBtn).not.toBeNull();
    expect(shellCopyBtn).not.toBeNull();

    await act(async () => { (psCopyBtn as HTMLButtonElement).click(); });
    expect(clipboardWrite).toHaveBeenLastCalledWith(expect.stringContaining('scripts/blueprint_runner.py start'));
    expect((psCopyBtn as HTMLButtonElement).querySelector('.text-emerald-400')).not.toBeNull();
    expect((shellCopyBtn as HTMLButtonElement).querySelector('.text-emerald-400')).toBeNull();

    await act(async () => { (shellCopyBtn as HTMLButtonElement).click(); });
    expect(clipboardWrite).toHaveBeenLastCalledWith(expect.stringContaining('bash blueprint-runner start'));
    expect((psCopyBtn as HTMLButtonElement).querySelector('.text-emerald-400')).not.toBeNull();
    expect((shellCopyBtn as HTMLButtonElement).querySelector('.text-emerald-400')).not.toBeNull();
  });

  test('enroll dialog does not render a phantom token-expiry line', async () => {
    // Runner tokens do not expire; the backend register/rotate responses are
    // exactly {runner_id, token}. The old "Token expires" affordance was dead
    // (driven by phantom fields the server never sent) and has been removed.
    const el = await mount();

    const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(el.textContent).toContain('Runner enrolled');
    expect(el.textContent).not.toContain('Token expires');
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

  test('enroll dialog shows waiting, then connected once the runner heartbeats', async () => {
    const connectedNew: FleetRunner = {
      id: 'runner-new',
      hostname: 'operator-laptop',
      version: null,
      status: 'active',
      capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 1 },
      health: {
        device_status: 'connected',
        device_status_reason: null,
        is_live: true,
        seconds_since_last_seen: 3,
        active_task_count: 0,
        active_compute_job_count: 0,
        active_work_count: 0,
        max_concurrent: 1,
        available_slots: 1,
        stale_reason: null,
        agent_available: true,
        liveness_window_seconds: 300,
      },
      last_seen_at: new Date().toISOString(),
      created_at: '2026-07-03T00:00:00Z',
    };
    const el = await mount();

    const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
    // Post-enroll load still lacks the new runner (it has not heartbeated yet).
    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    const status = el.querySelector('[data-testid="runner-connection-status"]');
    expect(status).not.toBeNull();
    expect(status?.textContent).toContain('Waiting for your runner to connect');

    // Simulate the runner's first heartbeat arriving: the next list poll shows it
    // connected. Drive a poll via the panel refresh control (deterministic; the
    // background fast-poll uses a real 3s interval).
    vi.mocked(listFleetRunners).mockResolvedValue([...SAMPLE_RUNNERS, connectedNew]);
    const refreshBtn = el.querySelector('button[aria-label="Refresh runners"]');
    await act(async () => { (refreshBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    const statusAfter = el.querySelector('[data-testid="runner-connection-status"]');
    expect(statusAfter?.textContent).toContain('Connected on this project');
    expect(statusAfter?.textContent).toContain('operator-laptop');
  });

  test('enroll dialog surfaces prerequisites and the durable install path', async () => {
    const clipboardWrite = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    const el = await mount();

    const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
    await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    // Prerequisites block names the real host requirements (no more bare command).
    const prereq = el.querySelector('[data-testid="runner-prerequisites"]');
    expect(prereq).not.toBeNull();
    expect(prereq?.textContent).toContain('Node.js >= 22');
    expect(prereq?.textContent).toContain('@anthropic-ai/claude-code');
    expect(prereq?.textContent).toContain('bash blueprint-runner check');

    // Durable install path is offered as the recommended option, copyable.
    expect(el.textContent).toContain('Durable install');
    expect(el.textContent).toContain('recommended');
    const installBtn = el.querySelector('button[title="Copy Linux — per-user service install snippet"]');
    expect(installBtn).not.toBeNull();
    await act(async () => { (installBtn as HTMLButtonElement).click(); });
    expect(clipboardWrite).toHaveBeenLastCalledWith(expect.stringContaining('bash blueprint-runner install --token'));

    const sysInstallBtn = el.querySelector('button[title="Copy Linux — system-wide (shared box) install snippet"]');
    expect(sysInstallBtn).not.toBeNull();
    await act(async () => { (sysInstallBtn as HTMLButtonElement).click(); });
    expect(clipboardWrite).toHaveBeenLastCalledWith(expect.stringContaining('sudo bash blueprint-runner install --system'));

    // The quick foreground start remains available as the ephemeral alternative.
    expect(el.textContent).toContain('Quick start (foreground)');
  });

  test('enroll dialog falls back to an honest not-connected state after the connect timeout', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      // The runner never connects: every poll returns only the base list, never
      // the just-enrolled runner-new (a wrong token behaves identically).
      vi.mocked(listFleetRunners).mockResolvedValue(SAMPLE_RUNNERS);
      const el = await mount();

      const enrollBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
      // The runner never heartbeats (mock keeps returning the base list) — a wrong
      // token would behave identically, so the spinner must not spin forever.
      await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
      await act(async () => {});

      let status = el.querySelector('[data-testid="runner-connection-status"]');
      expect(status?.textContent).toContain('Waiting for your runner to connect');

      // Advance past the bounded connect wait with no heartbeat.
      await act(async () => { await vi.advanceTimersByTimeAsync(95_000); });

      status = el.querySelector('[data-testid="runner-connection-status"]');
      expect(status?.textContent).toContain('Not connected yet');
      expect(status?.textContent).toContain('bash blueprint-runner check');
      expect(status?.textContent).not.toContain('Waiting for your runner to connect');

      // "Keep waiting" returns to the optimistic state (timer re-arms).
      const keepBtn = [...el.querySelectorAll('button')].find(b => b.textContent?.trim() === 'Keep waiting');
      expect(keepBtn).not.toBeNull();
      await act(async () => { (keepBtn as HTMLButtonElement).click(); });

      status = el.querySelector('[data-testid="runner-connection-status"]');
      expect(status?.textContent).toContain('Waiting for your runner to connect');
      expect(status?.textContent).not.toContain('Not connected yet');
    } finally {
      vi.useRealTimers();
    }
  });

  // ── Rotate runner token ──────────────────────────────────────────────────────

  test('rotating a runner shows ConfirmDialog, calls rotateFleetRunnerToken on confirm, and shows restart-focused token dialog', async () => {
    const clipboardWrite = vi.fn(async () => {});
    Object.defineProperty(navigator, 'clipboard', {
      configurable: true,
      value: { writeText: clipboardWrite },
    });
    const el = await mount();

    const rotateBtn = el.querySelector('button[title="Rotate runner token"]');
    expect(rotateBtn).not.toBeNull();

    await act(async () => { (rotateBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    // ConfirmDialog should be visible with rotate-specific text
    expect(el.textContent).toContain('current token will stop working immediately');
    expect(rotateFleetRunnerToken).not.toHaveBeenCalled();

    // Confirm the dialog
    const confirmBtn = el.querySelector('[data-testid="confirm-dialog-confirm"]');
    expect(confirmBtn).not.toBeNull();
    await act(async () => { (confirmBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(rotateFleetRunnerToken).toHaveBeenCalledWith('proj-test', SAMPLE_RUNNERS[0].id);
    expect(el.textContent).toContain('Runner token rotated');
    expect(el.textContent).toContain('tok-rotated-xyz');
    expect(el.textContent).toContain('Restart the existing runner process');
    expect(el.textContent).toContain('Restart required');
    expect(el.textContent).not.toContain('Connected on this project');
    expect(el.textContent).toContain('Windows PowerShell');
    expect(el.textContent).toContain('macOS/Linux');
    expect(el.textContent).toContain('py -3 scripts/blueprint_runner.py start');
    expect(el.textContent).toContain('bash blueprint-runner start');

    const tokenCopyBtn = el.querySelector('button[title="Copy token"]');
    const shellCopyBtn = el.querySelector('button[title="Copy macOS/Linux setup snippet"]');
    expect(tokenCopyBtn).not.toBeNull();
    expect(shellCopyBtn).not.toBeNull();

    await act(async () => { (tokenCopyBtn as HTMLButtonElement).click(); });
    expect(clipboardWrite).toHaveBeenLastCalledWith('tok-rotated-xyz');
    await act(async () => { (shellCopyBtn as HTMLButtonElement).click(); });
    expect(clipboardWrite).toHaveBeenLastCalledWith(expect.stringContaining("bash blueprint-runner start --token 'tok-rotated-xyz'"));
  });

  test('cancelling the rotate confirm does not call rotateFleetRunnerToken', async () => {
    const el = await mount();

    const rotateBtn = el.querySelector('button[title="Rotate runner token"]');
    await act(async () => { (rotateBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    // ConfirmDialog should be visible
    expect(el.querySelector('[data-testid="confirm-dialog-confirm"]')).not.toBeNull();

    // Cancel the dialog
    const cancelBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Cancel');
    await act(async () => { (cancelBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(rotateFleetRunnerToken).not.toHaveBeenCalled();
    expect(el.textContent).not.toContain('Runner token rotated');
  });

  test('rotate error is displayed in the panel', async () => {
    vi.mocked(rotateFleetRunnerToken).mockRejectedValueOnce(new Error('Rotate failed'));
    const el = await mount();

    const rotateBtn = el.querySelector('button[title="Rotate runner token"]');
    await act(async () => { (rotateBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    const confirmBtn = el.querySelector('[data-testid="confirm-dialog-confirm"]');
    await act(async () => { (confirmBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(el.textContent).toContain('Rotate failed');
    expect(el.textContent).not.toContain('Runner token rotated');
  });

  // ── Revoke runner ────────────────────────────────────────────────────────────

  test('revoking a runner shows ConfirmDialog before calling revokeFleetRunner', async () => {
    const el = await mount();

    const revokeBtn = el.querySelector('button[title="Revoke runner"]');
    expect(revokeBtn).not.toBeNull();

    await act(async () => { (revokeBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    // ConfirmDialog should be visible
    expect(el.querySelector('[data-testid="confirm-dialog-confirm"]')).not.toBeNull();
    expect(revokeFleetRunner).not.toHaveBeenCalled();

    const confirmBtn = el.querySelector('[data-testid="confirm-dialog-confirm"]');
    await act(async () => { (confirmBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(revokeFleetRunner).toHaveBeenCalledWith('proj-test', SAMPLE_RUNNERS[0].id);
  });

  test('cancelling the revoke confirm does not call revokeFleetRunner', async () => {
    const el = await mount();

    const revokeBtn = el.querySelector('button[title="Revoke runner"]');
    await act(async () => { (revokeBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    // ConfirmDialog should be visible
    expect(el.querySelector('[data-testid="confirm-dialog-confirm"]')).not.toBeNull();

    const cancelBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Cancel');
    await act(async () => { (cancelBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(revokeFleetRunner).not.toHaveBeenCalled();
  });

  test('revoke error is displayed in the panel', async () => {
    vi.mocked(revokeFleetRunner).mockRejectedValueOnce(new Error('Network error'));
    const el = await mount();

    const revokeBtn = el.querySelector('button[title="Revoke runner"]');
    await act(async () => { (revokeBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    const confirmBtn = el.querySelector('[data-testid="confirm-dialog-confirm"]');
    await act(async () => { (confirmBtn as HTMLButtonElement).click(); });
    await act(async () => {});

    expect(el.textContent).toContain('Network error');
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

  test('backend health renders connected state and available slot count', async () => {
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
          device_status: 'connected',
          device_status_reason: null,
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
    expect(el.textContent).toContain('connected');
    expect(el.textContent).toContain('Strategic capable');
    expect(el.querySelector('[data-testid="fleet-runner-strategic-readiness"]')?.textContent).toBe('1 eligible live');
    expect(el.textContent).toContain('1 slots free');
    expect(el.textContent).toContain('design_doc_update, strategic_review');
    expect(el.querySelector('[data-testid="fleet-next-action"]')?.textContent).toContain('Fleet has dispatch capacity');
  });

  test('surfaces blocked agent availability as the fleet next action', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: ['compute_experiment'], has_gpu: true, max_concurrent: 2 },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          seconds_since_last_seen: 8,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: 2,
          stale_reason: null,
          agent_available: false,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    expect(el.querySelector('[data-testid="fleet-next-action"]')?.textContent).toContain('Inspect agent availability');
    expect(el.textContent).toContain('agent unavailable');
    expect(el.textContent).toContain('Next: Restart or inspect the local agent');
    expect(el.textContent).toContain('2 slots free');
  });

  test('renders per-leg agent-unavailable breakdown when the structured legs map is present', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: ['compute_experiment'], has_gpu: true, max_concurrent: 2 },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          seconds_since_last_seen: 8,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: 2,
          stale_reason: null,
          agent_available: false,
          agent_unavailable_reason: 'bwrap sandbox missing; node runtime missing',
          agent_unavailable_legs: {
            bwrap_sandbox: false,
            agent_dist: true,
            node_runtime: false,
            docker: true,
          },
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    const legsRow = el.querySelector('[data-testid="fleet-runner-agent-legs"]');
    expect(legsRow).not.toBeNull();

    // All four legs are shown, each labelled.
    const bwrap = el.querySelector('[data-testid="fleet-runner-agent-leg-bwrap_sandbox"]');
    const dist = el.querySelector('[data-testid="fleet-runner-agent-leg-agent_dist"]');
    const node = el.querySelector('[data-testid="fleet-runner-agent-leg-node_runtime"]');
    const docker = el.querySelector('[data-testid="fleet-runner-agent-leg-docker"]');
    expect(bwrap?.textContent).toContain('bwrap');
    expect(dist?.textContent).toContain('agent dist');
    expect(node?.textContent).toContain('node');
    expect(docker?.textContent).toContain('docker');

    // Failing legs are distinguishable from passing ones (state + red styling).
    expect(bwrap?.getAttribute('data-leg-ok')).toBe('false');
    expect(node?.getAttribute('data-leg-ok')).toBe('false');
    expect(dist?.getAttribute('data-leg-ok')).toBe('true');
    expect(docker?.getAttribute('data-leg-ok')).toBe('true');
    expect(bwrap?.textContent).toContain('✗');
    expect(node?.textContent).toContain('✗');
    expect(dist?.textContent).toContain('✓');
    expect(docker?.textContent).toContain('✓');
    expect(bwrap?.className).toContain('border-red-500/30');
    expect(dist?.className).toContain('border-emerald-500/30');

    // With the structured map present, the pill is the bare 'agent ✗' (the folded
    // string is not repeated), but the full reason stays available as its tooltip.
    const agentPill = Array.from(el.querySelectorAll('span')).find(
      s => s.textContent === 'agent ✗',
    );
    expect(agentPill).toBeTruthy();
    expect(agentPill?.getAttribute('title')).toBe('bwrap sandbox missing; node runtime missing');
    expect(el.textContent).not.toContain('agent ✗: bwrap sandbox missing');
  });

  test('falls back to the folded agent_unavailable_reason string when no legs map is present', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: ['compute_experiment'], has_gpu: true, max_concurrent: 2 },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          seconds_since_last_seen: 8,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: 2,
          stale_reason: null,
          agent_available: false,
          agent_unavailable_reason: 'bwrap sandbox missing; docker unavailable',
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    // Older runner without the structured map: no per-leg row, folded string shown.
    expect(el.querySelector('[data-testid="fleet-runner-agent-legs"]')).toBeNull();
    expect(el.textContent).toContain('agent ✗: bwrap sandbox missing; docker unavailable');
  });

  test('distinguishes unknown runner slots from full capacity', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 2 },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          seconds_since_last_seen: 8,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: null,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    expect(el.querySelector('[data-testid="fleet-next-action"]')?.textContent).toContain('Confirm runner slot telemetry');
    expect(el.querySelector('[data-testid="fleet-next-action"]')?.textContent).not.toContain('Capacity is full');
    expect(el.textContent).toContain('slots unknown');
    expect(el.textContent).toContain('Next: Confirm runner slot telemetry');
    expect(el.textContent).not.toContain('1/1 ready');
    expect(el.textContent).toContain('0+ known');
  });

  test('backend device status renders connected, stale, and offline row labels', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        id: 'runner-connected',
        hostname: 'connected-host',
        capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 2 },
        health: {
          device_status: 'connected',
          device_status_reason: null,
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
      {
        ...SAMPLE_RUNNERS[0],
        id: 'runner-stale',
        hostname: 'stale-host',
        capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 2 },
        health: {
          device_status: 'stale',
          device_status_reason: 'heartbeat_stale',
          is_live: false,
          seconds_since_last_seen: 480,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: 2,
          stale_reason: 'heartbeat_stale',
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
      {
        ...SAMPLE_RUNNERS[0],
        id: 'runner-offline',
        hostname: 'offline-host',
        status: 'inactive',
        capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 2 },
        health: {
          device_status: 'offline',
          device_status_reason: 'inactive',
          is_live: false,
          seconds_since_last_seen: 30,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: 2,
          stale_reason: 'inactive',
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();
    const rows = [...el.querySelectorAll('[data-testid="fleet-runner-row"]')];

    expect(rows).toHaveLength(3);
    expect(el.textContent).toContain('connected');
    expect(el.textContent).toContain('1/3');
    expect(rows[0].textContent).toContain('connected');
    expect(rows[1].textContent).toContain('stale');
    expect(rows[2].textContent).toContain('offline');
    expect(rows[2].textContent).toContain('offline (deactivated)');
  });

  test('backend device status wins over legacy is_live fallback', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        id: 'runner-stale-live',
        hostname: 'stale-live-host',
        capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 2 },
        health: {
          device_status: 'stale',
          device_status_reason: 'heartbeat_stale',
          is_live: true,
          seconds_since_last_seen: 480,
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

    expect(el.textContent).toContain('stale');
    expect(el.textContent).toContain('0/1');
    expect(el.querySelector('[data-testid="fleet-runner-work-capacity"]')?.textContent).toBe('0/-');
  });

  test('connected all-capability runner counts as strategic-capable', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 2 },
        health: {
          device_status: 'connected',
          device_status_reason: null,
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

    expect(el.querySelector('[data-testid="fleet-runner-strategic-readiness"]')?.textContent).toBe('1 eligible live');
    expect(el.textContent).toContain('Strategic capable');
  });

  test('strategic summary distinguishes connected runners from eligible live runners', async () => {
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
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          seconds_since_last_seen: 8,
          active_task_count: 1,
          active_compute_job_count: 0,
          active_work_count: 1,
          max_concurrent: 1,
          available_slots: 0,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    expect(el.querySelector('[data-testid="fleet-runner-strategic-readiness"]')?.textContent).toBe('none eligible');
    expect(el.textContent).toContain('1 connected');
    expect(el.textContent).toContain('full');
  });

  test('strategic summary ignores blocked agents on compute-only runners', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        id: 'strategic-runner',
        capabilities: {
          task_types: ['design_doc_update', 'strategic_review'],
          has_gpu: false,
          max_concurrent: 1,
          runner_preset: 'strategic_local',
          workload: 'strategic',
        },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          seconds_since_last_seen: 8,
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
      {
        ...SAMPLE_RUNNERS[0],
        id: 'compute-runner',
        capabilities: {
          task_types: ['compute_experiment'],
          has_gpu: true,
          max_concurrent: 1,
        },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          seconds_since_last_seen: 10,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 1,
          available_slots: 1,
          stale_reason: null,
          agent_available: false,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    expect(el.querySelector('[data-testid="fleet-runner-strategic-readiness"]')?.textContent).toBe('1 eligible live');
    expect(el.textContent).toContain('1 connected');
    expect(el.textContent).not.toContain('agent blocked');
  });

  test('active work summary includes active compute jobs when aggregate is absent', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: {
          task_types: ['compute_experiment'],
          has_gpu: true,
          max_concurrent: 3,
        },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          seconds_since_last_seen: 10,
          active_task_count: 1,
          active_compute_job_count: 1,
          max_concurrent: 3,
          available_slots: 1,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
        },
      },
    ]);
    const el = await mount();

    expect(el.querySelector('[data-testid="fleet-runner-work-capacity"]')?.textContent).toBe('2/3');
    expect(el.textContent).toContain('2 active');
    expect(el.textContent).toContain('1 slots free');
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
    expect(el.querySelector('[data-testid="fleet-runner-strategic-readiness"]')?.textContent).toBe('none eligible');
    expect(el.textContent).toContain('offline (deactivated)');
    expect(el.textContent).not.toContain('1 active');
    expect(el.textContent).not.toContain('2 slots free');
  });

  test('local-only compute warning appears when no connected compute runner exists', async () => {
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
    expect(host.textContent).toContain('wait until a connected compute-capable runner is available');
  });

  // ── Trust telemetry / "connected but failing" ────────────────────────────────

  test('degraded runner renders "connected but failing" amber state, not ready', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        id: 'runner-failing',
        hostname: 'failing-host',
        capabilities: { task_types: ['compute_experiment'], has_gpu: true, max_concurrent: 2 },
        health: {
          device_status: 'degraded',
          device_status_reason: 'job_failure_streak',
          is_live: true,
          degraded: true,
          degraded_reason: 'job_failure_streak',
          seconds_since_last_seen: 12,
          active_task_count: 1,
          active_compute_job_count: 0,
          active_work_count: 1,
          max_concurrent: 2,
          available_slots: 1,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
          last_successful_job_at: new Date(Date.now() - 3_600_000).toISOString(),
          seconds_since_last_success: 3600,
          consecutive_job_failures: 4,
          disk_free_bytes: 1610612736, // 1.5 GiB
          disk_low: true,
          gpu_utilization_pct: 88,
          failure_streak_threshold: 3,
        },
      },
    ]);
    const el = await mount();

    // Row shows the amber "connected but failing" readiness + "failing" device label.
    expect(el.textContent).toContain('connected but failing');
    expect(el.textContent).toContain('failing');
    // Not counted as ready (degraded is live but failing).
    expect(el.textContent).toContain('0/1 ready');
    // Failing count surfaced in the readiness summary tile.
    expect(el.querySelector('[data-testid="fleet-runner-connected-summary"]')?.textContent).toContain('1 failing');
    // Telemetry line renders GPU util, disk free, and the failure streak.
    const telemetry = el.querySelector('[data-testid="fleet-runner-telemetry"]')?.textContent ?? '';
    expect(telemetry).toContain('GPU 88%');
    expect(telemetry).toContain('1.5 GiB disk');
    expect(telemetry).toContain('4 fail streak');
    // Next action points the operator at the failing host.
    expect(el.querySelector('[data-testid="fleet-next-action"]')?.textContent).toContain('failing its jobs');
    // A degraded runner still counts as live capacity (slots accounting intact).
    expect(el.querySelector('[data-testid="fleet-runner-work-capacity"]')?.textContent).toBe('1/2');
  });

  test('healthy runner shows telemetry and is ready (no degraded state)', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          degraded: false,
          degraded_reason: null,
          seconds_since_last_seen: 10,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: 2,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
          last_successful_job_at: new Date(Date.now() - 90_000).toISOString(),
          seconds_since_last_success: 90,
          consecutive_job_failures: 0,
          disk_free_bytes: 53687091200, // 50 GiB
          disk_low: false,
          gpu_utilization_pct: 12,
          failure_streak_threshold: 3,
        },
      },
    ]);
    const el = await mount();

    expect(el.textContent).not.toContain('connected but failing');
    expect(el.textContent).toContain('1/1 ready');
    const telemetry = el.querySelector('[data-testid="fleet-runner-telemetry"]')?.textContent ?? '';
    expect(telemetry).toContain('last success');
    expect(telemetry).toContain('GPU 12%');
    expect(telemetry).toContain('50 GiB disk');
    expect(telemetry).not.toContain('fail streak');
    expect(el.querySelector('[data-testid="fleet-runner-connected-summary"]')?.textContent).toBe('1 connected');
  });

  test('memory-starved runner surfaces free memory and warns', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 2 },
        health: {
          device_status: 'connected',
          device_status_reason: null,
          is_live: true,
          degraded: false,
          degraded_reason: null,
          seconds_since_last_seen: 10,
          active_task_count: 0,
          active_compute_job_count: 0,
          active_work_count: 0,
          max_concurrent: 2,
          available_slots: 2,
          stale_reason: null,
          agent_available: true,
          liveness_window_seconds: 300,
          last_successful_job_at: new Date(Date.now() - 90_000).toISOString(),
          seconds_since_last_success: 90,
          consecutive_job_failures: 0,
          disk_free_bytes: 53687091200, // 50 GiB — disk healthy
          disk_low: false,
          memory_free_bytes: 268435456, // 256 MiB — memory starved
          memory_low: true,
          cpu_load_avg: 3.5,
          failure_streak_threshold: 3,
        },
      },
    ]);
    const el = await mount();

    const telemetryEl = el.querySelector('[data-testid="fleet-runner-telemetry"]');
    const telemetry = telemetryEl?.textContent ?? '';
    // Memory free is surfaced on the telemetry line (previously dropped entirely).
    expect(telemetry).toContain('256 MiB mem');
    expect(telemetry).toContain('50 GiB disk');
    // memory_low trips the warn styling even though disk is healthy and the
    // runner is not job-failure degraded.
    expect(telemetryEl?.className).toContain('amber');
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

  // ── live prop / 30s poll gating ──────────────────────────────────────────────

  test('test_live_stream_suppresses_blind_30s_poll: when live=true, setInterval is not called with 30000', async () => {
    const intervals: number[] = [];
    const origSetInterval = globalThis.setInterval;
    const spy = vi.spyOn(globalThis, 'setInterval').mockImplementation((fn, delay, ...args) => {
      if (typeof delay === 'number') intervals.push(delay);
      return origSetInterval(fn, delay, ...args);
    });
    try {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FleetRunnersPanel projectId="proj-live" live={true} />);
      });
      await act(async () => {});

      // The 30s blind-poll interval must not be registered when live=true
      expect(intervals).not.toContain(30000);
    } finally {
      spy.mockRestore();
    }
  });

  test('when live=false, setInterval is called with 30000 (poll runs)', async () => {
    const intervals: number[] = [];
    const origSetInterval = globalThis.setInterval;
    const spy = vi.spyOn(globalThis, 'setInterval').mockImplementation((fn, delay, ...args) => {
      if (typeof delay === 'number') intervals.push(delay);
      return origSetInterval(fn, delay, ...args);
    });
    try {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FleetRunnersPanel projectId="proj-poll" live={false} />);
      });
      await act(async () => {});

      expect(intervals).toContain(30000);
    } finally {
      spy.mockRestore();
    }
  });

  test('when live is absent (default), setInterval fires with 30000 (backward-compat)', async () => {
    const intervals: number[] = [];
    const origSetInterval = globalThis.setInterval;
    const spy = vi.spyOn(globalThis, 'setInterval').mockImplementation((fn, delay, ...args) => {
      if (typeof delay === 'number') intervals.push(delay);
      return origSetInterval(fn, delay, ...args);
    });
    try {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FleetRunnersPanel projectId="proj-default" />);
      });
      await act(async () => {});

      expect(intervals).toContain(30000);
    } finally {
      spy.mockRestore();
    }
  });

  test('enrollment fast-poll (3s) still fires when live=true and enroll dialog is open', async () => {
    // Reset mock so runner-new is NOT in the list (a prior test's mockResolvedValue
    // would otherwise include it as 'connected', making enrolledConnected=true which
    // short-circuits the fast-poll useEffect).
    vi.mocked(listFleetRunners).mockResolvedValue(SAMPLE_RUNNERS);

    // Use fake timers only for setInterval/clearInterval so React's scheduler
    // (which uses MessageChannel/setTimeout) keeps working normally.
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    try {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<FleetRunnersPanel projectId="proj-enroll-live" live={true} />);
      });
      await act(async () => {});

      const enrollBtn = [...(host as HTMLDivElement).querySelectorAll('button')].find(b => b.textContent?.includes('Enroll runner'));
      await act(async () => { (enrollBtn as HTMLButtonElement).click(); });
      await act(async () => {});

      // Verify the dialog opened and the runner has not yet connected
      expect((host as HTMLDivElement).textContent).toContain('Waiting for your runner to connect');

      const callsBeforeAdvance = vi.mocked(listFleetRunners).mock.calls.length;

      // Advance 3s: the enrollment fast-poll (gated on pendingToken, NOT on live)
      // must still fire when live=true — assert an extra load() call occurs.
      await act(async () => { vi.advanceTimersByTime(3000); });
      await act(async () => {});

      expect(vi.mocked(listFleetRunners).mock.calls.length).toBeGreaterThan(callsBeforeAdvance);
    } finally {
      vi.useRealTimers();
    }
  });

  // ── Live config: applied-vs-pending badge + GPU allowlist editor ─────────────

  test('renders config-pending badge when config_pending is true', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        config: {
          config_generation: 'gen-new',
          applied_config_generation: 'gen-old',
          config_pending: true,
          gpu_indices_allowed: [0, 1],
        },
      },
    ]);
    const el = await mount();

    const badge = el.querySelector('[data-testid="fleet-runner-config-status"]');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toContain('config pending — applying on next heartbeat');
    // Pending state is amber, distinct from the applied emerald.
    expect(badge?.className).toContain('text-amber-300');
    expect(badge?.textContent).not.toContain('config applied');
  });

  test('stuck config on a live daemon too old to report reads red "too old", not eternal amber', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        last_seen_at: new Date(Date.now() - 30_000).toISOString(), // live
        config: {
          config_generation: 'gen-new',
          applied_config_generation: null,
          config_pending: true,
          // Changed well past the pending window (2 min) → stuck.
          desired_changed_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
          daemon_reports_applied: false,
        },
      },
    ]);
    const el = await mount();

    const badge = el.querySelector('[data-testid="fleet-runner-config-status"]');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toContain('config not applied — runner too old');
    // STUCK is red, distinct from the optimistic amber pending.
    expect(badge?.className).toContain('text-red-300');
    expect(badge?.textContent).not.toContain('applying on next heartbeat');
  });

  test('stuck config on a live daemon that reported a stale generation reads red "rejected/stuck"', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        last_seen_at: new Date(Date.now() - 30_000).toISOString(), // live
        config: {
          config_generation: 'gen-new',
          applied_config_generation: 'gen-old',
          config_pending: true,
          desired_changed_at: new Date(Date.now() - 5 * 60 * 1000).toISOString(),
          daemon_reports_applied: true,
        },
      },
    ]);
    const el = await mount();

    const badge = el.querySelector('[data-testid="fleet-runner-config-status"]');
    expect(badge?.textContent).toContain('rejected it or is stuck');
    expect(badge?.className).toContain('text-red-300');
    // Must NOT mislabel a too-old daemon: it DID report a generation.
    expect(badge?.textContent).not.toContain('runner too old');
  });

  test('pending config on an OFFLINE runner reads neutral "queued", never red', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        last_seen_at: new Date(Date.now() - 15 * 60 * 1000).toISOString(), // offline
        config: {
          config_generation: 'gen-new',
          applied_config_generation: null,
          config_pending: true,
          // Even long-stale desired change must not flag red while offline —
          // pending is expected and applies on reconnect.
          desired_changed_at: new Date(Date.now() - 30 * 60 * 1000).toISOString(),
          daemon_reports_applied: false,
        },
      },
    ]);
    const el = await mount();

    const badge = el.querySelector('[data-testid="fleet-runner-config-status"]');
    expect(badge?.textContent).toContain('config queued — applies when runner reconnects');
    expect(badge?.className).not.toContain('text-red-300');
    expect(badge?.className).toContain('text-neutral-300');
  });

  test('pending config within the window on a live runner stays amber "applying"', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        last_seen_at: new Date(Date.now() - 30_000).toISOString(), // live
        config: {
          config_generation: 'gen-new',
          applied_config_generation: null,
          config_pending: true,
          // Just changed — well within the 2-minute pending window.
          desired_changed_at: new Date(Date.now() - 10_000).toISOString(),
          daemon_reports_applied: false,
        },
      },
    ]);
    const el = await mount();

    const badge = el.querySelector('[data-testid="fleet-runner-config-status"]');
    expect(badge?.textContent).toContain('config pending — applying on next heartbeat');
    expect(badge?.className).toContain('text-amber-300');
    expect(badge?.className).not.toContain('text-red-300');
  });

  test('renders config-applied badge when applied matches desired', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
        },
      },
    ]);
    const el = await mount();

    const badge = el.querySelector('[data-testid="fleet-runner-config-status"]');
    expect(badge).not.toBeNull();
    expect(badge?.textContent).toContain('config applied');
    expect(badge?.className).toContain('text-emerald-300');
    expect(badge?.textContent).not.toContain('config pending');
  });

  test('renders no config badge for a runner without a config block', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([SAMPLE_RUNNERS[0]]);
    const el = await mount();
    expect(el.querySelector('[data-testid="fleet-runner-config-status"]')).toBeNull();
  });

  test('editing the GPU allowlist to an index list calls updateRunnerCapabilities with gpu_indices_allowed', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
        },
      },
    ]);
    const el = await mount();

    // Current value surfaced read-only in the header; editing lives in the panel.
    expect(el.textContent).toContain('GPUs: all');
    await openConfig(el);

    // Uncheck "all" to reveal the index input.
    const allToggle = el.querySelector('input[aria-label="Allow all GPUs"]') as HTMLInputElement;
    expect(allToggle).not.toBeNull();
    expect(allToggle.checked).toBe(true);
    // Native click toggles the checkbox and fires the 'click' event React binds
    // its onChange to for checkboxes (a dispatched 'input' event is unreliable).
    await act(async () => { allToggle.click(); });

    const indexInput = el.querySelector('input[aria-label="GPU indices allowed"]') as HTMLInputElement;
    expect(indexInput).not.toBeNull();
    await act(async () => { typeInto(indexInput, '2, 0, 2'); });

    await saveConfig(el);

    // Only the changed field is sent, parsed to a deduped, sorted array.
    expect(updateRunnerCapabilities).toHaveBeenCalledWith('proj-test', 'runner-abc123', {
      gpu_indices_allowed: [0, 2],
    });
  });

  test('saving the GPU allowlist while "all" is checked sends the "all" sentinel', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: [0, 1],
        },
      },
    ]);
    const el = await mount();

    // Currently restricted — open the panel and re-check "all".
    expect(el.textContent).toContain('GPUs: 0,1');
    await openConfig(el);

    const allToggle = el.querySelector('input[aria-label="Allow all GPUs"]') as HTMLInputElement;
    // Opened from a restricted list → starts unchecked.
    expect(allToggle.checked).toBe(false);
    await act(async () => { allToggle.click(); });

    await saveConfig(el);

    expect(updateRunnerCapabilities).toHaveBeenCalledWith('proj-test', 'runner-abc123', {
      gpu_indices_allowed: 'all',
    });
  });

  test('the GPU allowlist control is absent for non-GPU runners', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 2 },
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
        },
      },
    ]);
    const el = await mount();
    await openConfig(el);
    expect(el.querySelector('input[aria-label="Allow all GPUs"]')).toBeNull();
    expect(el.querySelector('input[aria-label="GPU indices allowed"]')).toBeNull();
  });

  // ── Live config: per-job GPU count + task-type allowlist editors ─────────────

  test('editing gpus_per_job calls updateRunnerCapabilities with the new count', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
          gpus_per_job: 1,
        },
      },
    ]);
    const el = await mount();

    // Current value surfaced read-only in the header; edit in the panel.
    expect(el.textContent).toContain('1 GPUs/job');
    await openConfig(el);

    const input = el.querySelector('input[aria-label="GPUs per job"]') as HTMLInputElement;
    expect(input).not.toBeNull();
    await act(async () => { typeInto(input, '4'); });

    await saveConfig(el);

    expect(updateRunnerCapabilities).toHaveBeenCalledWith('proj-test', 'runner-abc123', {
      gpus_per_job: 4,
    });
  });

  test('the gpus_per_job input clamps out-of-range values to 1–8', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
          gpus_per_job: 2,
        },
      },
    ]);
    const el = await mount();

    await openConfig(el);

    const input = el.querySelector('input[aria-label="GPUs per job"]') as HTMLInputElement;
    // Above the max clamps to 8.
    await act(async () => { typeInto(input, '99'); });
    await saveConfig(el);

    expect(updateRunnerCapabilities).toHaveBeenCalledWith('proj-test', 'runner-abc123', {
      gpus_per_job: 8,
    });
  });

  test('the gpus_per_job control is absent for non-GPU runners', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 2 },
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
          gpus_per_job: 1,
        },
      },
    ]);
    const el = await mount();
    await openConfig(el);
    expect(el.querySelector('input[aria-label="GPUs per job"]')).toBeNull();
  });

  test('editing task_types to a list calls updateRunnerCapabilities with the parsed array', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 2 },
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
          desired: { task_types: 'all' },
        },
      },
    ]);
    const el = await mount();

    expect(el.textContent).toContain('tasks: all');
    await openConfig(el);

    const allToggle = el.querySelector('input[aria-label="Allow all task types"]') as HTMLInputElement;
    expect(allToggle).not.toBeNull();
    expect(allToggle.checked).toBe(true);
    await act(async () => { allToggle.click(); });

    const listInput = el.querySelector('input[aria-label="Task types allowed"]') as HTMLInputElement;
    expect(listInput).not.toBeNull();
    await act(async () => { typeInto(listInput, 'compute_experiment, code_change, compute_experiment'); });

    await saveConfig(el);

    // Deduped, trimmed list preserving input order.
    expect(updateRunnerCapabilities).toHaveBeenCalledWith('proj-test', 'runner-abc123', {
      task_types: ['compute_experiment', 'code_change'],
    });
  });

  test('saving task_types with "all" checked sends the "all" sentinel', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: ['compute_experiment'], has_gpu: false, max_concurrent: 2 },
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
          desired: { task_types: ['compute_experiment'] },
        },
      },
    ]);
    const el = await mount();

    expect(el.textContent).toContain('tasks: compute_experiment');
    await openConfig(el);

    const allToggle = el.querySelector('input[aria-label="Allow all task types"]') as HTMLInputElement;
    // Opened from a restricted list → starts unchecked.
    expect(allToggle.checked).toBe(false);
    await act(async () => { allToggle.click(); });

    await saveConfig(el);

    expect(updateRunnerCapabilities).toHaveBeenCalledWith('proj-test', 'runner-abc123', {
      task_types: 'all',
    });
  });

  test('the task_types control is suppressed for strategic runners', async () => {
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
        config: {
          config_generation: 'gen-abc',
          applied_config_generation: 'gen-abc',
          config_pending: false,
          gpu_indices_allowed: 'all',
          desired: { task_types: ['design_doc_update', 'strategic_review'] },
        },
      },
    ]);
    const el = await mount();
    await openConfig(el);
    // Strategic runners have task types pinned; the editable control is replaced
    // by a read-only value, so the "all" checkbox is absent.
    expect(el.querySelector('input[aria-label="Allow all task types"]')).toBeNull();
    expect(el.textContent).toContain('Pinned to the strategic task set');
  });

  // ── Configure panel: coherent config surface (rebuild) ───────────────────────

  test('config panel is closed by default and opens/closes via the Configure toggle', async () => {
    const el = await mount();
    // Closed initially — no config form on screen.
    expect(el.querySelector('[data-testid="fleet-runner-config-panel"]')).toBeNull();
    await openConfig(el);
    expect(el.querySelector('[data-testid="fleet-runner-config-panel"]')).not.toBeNull();
    // The toggle is a real, discoverable affordance (not a bare pencil icon).
    const toggle = el.querySelector('[data-testid="fleet-runner-configure-toggle"]') as HTMLButtonElement;
    expect(toggle.getAttribute('aria-label')).toContain('Configure runner');
    // Toggling again collapses it.
    await act(async () => { toggle.click(); });
    await act(async () => {});
    expect(el.querySelector('[data-testid="fleet-runner-config-panel"]')).toBeNull();
  });

  test('walkthrough: a new operator configures a GPU runner end-to-end without reading source', async () => {
    const runnerBefore: FleetRunner = {
      ...SAMPLE_RUNNERS[0],
      capabilities: { task_types: 'all', has_gpu: true, max_concurrent: 2 },
      config: {
        config_generation: 'gen-abc',
        applied_config_generation: 'gen-abc',
        config_pending: false,
        gpu_indices_allowed: 'all',
        gpus_per_job: 1,
        desired: { task_types: 'all', max_concurrent: 2 },
      },
    };
    // Initial load shows defaults; the post-save reload reflects the applied
    // config (simulating the backend converging the desired config).
    vi.mocked(listFleetRunners).mockResolvedValueOnce([runnerBefore]);
    vi.mocked(listFleetRunners).mockResolvedValue([
      {
        ...runnerBefore,
        capabilities: { ...runnerBefore.capabilities, max_concurrent: 4 },
        config: {
          ...runnerBefore.config,
          gpu_indices_allowed: [0, 1],
          desired: { task_types: 'all', max_concurrent: 4 },
        },
      },
    ]);
    const el = await mount();

    // 1. Operator opens the Configure panel from the labelled gear toggle.
    await openConfig(el);
    const panel = el.querySelector('[data-testid="fleet-runner-config-panel"]') as HTMLElement;
    expect(panel).not.toBeNull();

    // 2. Every control carries a plain-language label — no source-reading needed.
    expect(panel.textContent).toContain('Max concurrent tasks');
    expect(panel.textContent).toContain('Task types');
    expect(panel.textContent).toContain('GPU allowlist');
    expect(panel.textContent).toContain('GPUs per job');
    expect(panel.textContent).toContain('apply on the runner’s next heartbeat');

    // 3. Nothing changed yet → Save disabled, feedback says "No changes".
    const saveBtn = el.querySelector('[data-testid="fleet-runner-config-save"]') as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);
    expect(el.querySelector('[data-testid="fleet-runner-config-feedback"]')?.textContent).toContain('No changes');

    // 4. Operator raises concurrency and restricts GPUs, then saves once.
    const concInput = el.querySelector('input[aria-label="Max concurrent tasks"]') as HTMLInputElement;
    await act(async () => { typeInto(concInput, '4'); });
    const gpuAll = el.querySelector('input[aria-label="Allow all GPUs"]') as HTMLInputElement;
    await act(async () => { gpuAll.click(); });
    const gpuIdx = el.querySelector('input[aria-label="GPU indices allowed"]') as HTMLInputElement;
    await act(async () => { typeInto(gpuIdx, '0,1'); });

    // Feedback reflects unsaved edits before the write.
    expect(el.querySelector('[data-testid="fleet-runner-config-feedback"]')?.textContent).toContain('Unsaved changes');
    expect(saveBtn.disabled).toBe(false);

    await saveConfig(el);

    // 5. Exactly the two changed fields are sent in ONE request.
    expect(updateRunnerCapabilities).toHaveBeenCalledTimes(1);
    expect(updateRunnerCapabilities).toHaveBeenCalledWith('proj-test', 'runner-abc123', {
      max_concurrent: 4,
      gpu_indices_allowed: [0, 1],
    });
    // 6. The operator gets explicit save confirmation.
    expect(el.querySelector('[data-testid="fleet-runner-config-feedback"]')?.textContent).toContain('Saved');
  });

  test('config panel surfaces a save error instead of failing silently', async () => {
    vi.mocked(listFleetRunners).mockResolvedValue([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: { task_types: 'all', has_gpu: false, max_concurrent: 2 },
        config: { config_pending: false, desired: { max_concurrent: 2 } },
      },
    ]);
    vi.mocked(updateRunnerCapabilities).mockRejectedValueOnce(new Error('HTTP 409 conflict'));
    const el = await mount();

    await openConfig(el);
    const concInput = el.querySelector('input[aria-label="Max concurrent tasks"]') as HTMLInputElement;
    await act(async () => { typeInto(concInput, '8'); });
    await saveConfig(el);

    const feedback = el.querySelector('[data-testid="fleet-runner-config-feedback"]');
    expect(feedback?.textContent).toContain('Save failed');
    expect(feedback?.textContent).toContain('HTTP 409 conflict');
  });

  test('config panel pins concurrency read-only for strategic runners', async () => {
    vi.mocked(listFleetRunners).mockResolvedValue([
      {
        ...SAMPLE_RUNNERS[0],
        capabilities: {
          task_types: ['design_doc_update', 'strategic_review'],
          has_gpu: false,
          max_concurrent: 1,
          runner_preset: 'strategic_local',
          workload: 'strategic',
        },
        config: { config_pending: false, desired: { max_concurrent: 1 } },
      },
    ]);
    const el = await mount();
    await openConfig(el);
    // No editable concurrency input; a read-only value + explanation instead.
    expect(el.querySelector('input[aria-label="Max concurrent tasks"]')).toBeNull();
    expect(el.textContent).toContain('Pinned to 1 for strategic runners');
  });

  // ── Cloud-cost-avoided rollup in the telemetry detail ────────────────────────

  async function openTelemetry(el: HTMLDivElement) {
    const toggle = [...el.querySelectorAll('button')].find(b =>
      b.getAttribute('aria-label')?.startsWith('Toggle telemetry for runner'),
    );
    await act(async () => { (toggle as HTMLButtonElement).click(); });
    await act(async () => {});
  }

  test('telemetry detail renders the cloud-cost-avoided and claim-latency lines when the backend reports them', async () => {
    vi.mocked(fetchRunnerTelemetry).mockResolvedValue({
      runner_id: 'runner-abc123',
      since_seconds: 3600,
      count: 0,
      snapshots: [],
    });
    vi.mocked(fetchRunnerStats).mockResolvedValue({
      runner_id: 'runner-abc123',
      jobs_run_count: 4,
      jobs_terminal_count: 3,
      jobs_succeeded_count: 3,
      jobs_success_rate: 1,
      cloud_cost_avoided_usd: 12.5,
      cloud_cost_avoided_basis: 2,
      claim_latency_ms_p50: 1200,
      claim_latency_ms_p95: 3400,
      claim_latency_ms_p99: 5600,
      claim_latency_ms_max: 6000,
      claim_latency_sample_count: 3,
      last_n_outcomes: [],
    });

    const el = await mount();
    await openTelemetry(el);

    const detail = el.querySelector('[data-testid="fleet-runner-telemetry-detail"]');
    expect(detail).not.toBeNull();
    const text = detail?.textContent ?? '';
    expect(text).toContain('Cloud cost avoided:');
    expect(text).toContain('$12.50');
    expect(text).toContain('Claim latency:');
    expect(text).toContain('1200ms');
    expect(text).toContain('3 samples');
  });

  test('telemetry detail omits the claim-latency line when there is no cold-start sample', async () => {
    vi.mocked(fetchRunnerTelemetry).mockResolvedValue({
      runner_id: 'runner-abc123',
      since_seconds: 3600,
      count: 0,
      snapshots: [],
    });
    vi.mocked(fetchRunnerStats).mockResolvedValue({
      runner_id: 'runner-abc123',
      jobs_run_count: 1,
      jobs_terminal_count: 1,
      jobs_succeeded_count: 1,
      jobs_success_rate: 1,
      cloud_cost_avoided_usd: 0,
      cloud_cost_avoided_basis: 0,
      claim_latency_ms_p50: null,
      claim_latency_ms_p95: null,
      claim_latency_ms_p99: null,
      claim_latency_ms_max: null,
      claim_latency_sample_count: 0,
      last_n_outcomes: [],
    });

    const el = await mount();
    await openTelemetry(el);

    const detail = el.querySelector('[data-testid="fleet-runner-telemetry-detail"]');
    expect(detail).not.toBeNull();
    const text = detail?.textContent ?? '';
    // Cloud-cost line always renders (defaults to $0.00); claim-latency block is gated.
    expect(text).toContain('Cloud cost avoided:');
    expect(text).not.toContain('Claim latency:');
  });

  test('telemetry recent-jobs rows show terminal_reason and exit_code for a failed job', async () => {
    vi.mocked(fetchRunnerTelemetry).mockResolvedValue({
      runner_id: 'runner-abc123',
      since_seconds: 3600,
      count: 0,
      snapshots: [],
    });
    vi.mocked(fetchRunnerStats).mockResolvedValue({
      runner_id: 'runner-abc123',
      jobs_run_count: 2,
      jobs_terminal_count: 2,
      jobs_succeeded_count: 1,
      jobs_success_rate: 0.5,
      cloud_cost_avoided_usd: 0,
      cloud_cost_avoided_basis: 0,
      claim_latency_ms_p50: null,
      claim_latency_ms_p95: null,
      claim_latency_ms_p99: null,
      claim_latency_ms_max: null,
      claim_latency_sample_count: 0,
      last_n_outcomes: [
        {
          job_id: 'job-failed',
          task_id: 'task-1234abcd',
          status: 'FAILED',
          outcome: null,
          created_at: null,
          completed_at: null,
          terminal_reason: 'process exited non-zero',
          exit_code: 137,
        },
        {
          job_id: 'job-ok',
          task_id: null,
          status: 'SUCCEEDED',
          outcome: null,
          created_at: null,
          completed_at: null,
          terminal_reason: null,
          exit_code: null,
        },
      ],
    });

    const el = await mount();
    await openTelemetry(el);

    const detail = el.querySelector('[data-testid="fleet-runner-telemetry-detail"]');
    expect(detail).not.toBeNull();
    const text = detail?.textContent ?? '';
    // The failed row surfaces the reason and exit code inline.
    expect(text).toContain('process exited non-zero');
    expect(text).toContain('(exit 137)');
  });

  test('telemetry recent-jobs rows show command_log_tail for a failed job only', async () => {
    vi.mocked(fetchRunnerTelemetry).mockResolvedValue({
      runner_id: 'runner-abc123',
      since_seconds: 3600,
      count: 0,
      snapshots: [],
    });
    vi.mocked(fetchRunnerStats).mockResolvedValue({
      runner_id: 'runner-abc123',
      jobs_run_count: 2,
      jobs_terminal_count: 2,
      jobs_succeeded_count: 1,
      jobs_success_rate: 0.5,
      cloud_cost_avoided_usd: 0,
      cloud_cost_avoided_basis: 0,
      claim_latency_ms_p50: null,
      claim_latency_ms_p95: null,
      claim_latency_ms_p99: null,
      claim_latency_ms_max: null,
      claim_latency_sample_count: 0,
      last_n_outcomes: [
        {
          job_id: 'job-failed',
          task_id: 'task-1234abcd',
          status: 'FAILED',
          outcome: null,
          created_at: null,
          completed_at: null,
          terminal_reason: 'process exited non-zero',
          exit_code: 137,
          command_log_tail: 'Traceback (most recent call last):\n  RuntimeError: boom',
        },
        {
          job_id: 'job-ok',
          task_id: null,
          status: 'SUCCEEDED',
          outcome: null,
          created_at: null,
          completed_at: null,
          terminal_reason: null,
          exit_code: null,
          command_log_tail: 'this should never render',
        },
      ],
    });

    const el = await mount();
    await openTelemetry(el);

    const detail = el.querySelector('[data-testid="fleet-runner-telemetry-detail"]');
    expect(detail).not.toBeNull();
    const text = detail?.textContent ?? '';
    // The failed row surfaces its command-log tail inline.
    expect(text).toContain('RuntimeError: boom');
    // A success row's tail (defensive) is never rendered.
    expect(text).not.toContain('this should never render');
  });

  test('recent-outcome task id is a keyboard-accessible link that fires onNavigateToTask', async () => {
    vi.mocked(fetchRunnerTelemetry).mockResolvedValue({
      runner_id: 'runner-abc123',
      since_seconds: 3600,
      count: 0,
      snapshots: [],
    });
    vi.mocked(fetchRunnerStats).mockResolvedValue({
      runner_id: 'runner-abc123',
      jobs_run_count: 1,
      jobs_terminal_count: 1,
      jobs_succeeded_count: 0,
      jobs_success_rate: 0,
      cloud_cost_avoided_usd: 0,
      cloud_cost_avoided_basis: 0,
      claim_latency_ms_p50: null,
      claim_latency_ms_p95: null,
      claim_latency_ms_p99: null,
      claim_latency_ms_max: null,
      claim_latency_sample_count: 0,
      last_n_outcomes: [
        {
          job_id: 'job-failed',
          task_id: 'task-1234abcd',
          status: 'FAILED',
          outcome: null,
          created_at: null,
          completed_at: null,
          terminal_reason: 'process exited non-zero',
          exit_code: 137,
        },
      ],
    });

    const onNavigateToTask = vi.fn();
    const el = await mount('proj-test', { onNavigateToTask });
    await openTelemetry(el);

    const link = el.querySelector<HTMLButtonElement>(
      '[data-testid="fleet-outcome-task-link"]',
    );
    expect(link).not.toBeNull();
    // Rendered as a real <button> so it is focusable/keyboard-activatable.
    expect(link?.tagName).toBe('BUTTON');
    expect(link?.textContent).toContain('task task-123');

    await act(async () => { (link as HTMLButtonElement).click(); });
    expect(onNavigateToTask).toHaveBeenCalledTimes(1);
    expect(onNavigateToTask).toHaveBeenCalledWith('task-1234abcd');
  });

  test('recent-outcome task id degrades to plain text when no navigator is provided', async () => {
    vi.mocked(fetchRunnerTelemetry).mockResolvedValue({
      runner_id: 'runner-abc123',
      since_seconds: 3600,
      count: 0,
      snapshots: [],
    });
    vi.mocked(fetchRunnerStats).mockResolvedValue({
      runner_id: 'runner-abc123',
      jobs_run_count: 1,
      jobs_terminal_count: 1,
      jobs_succeeded_count: 0,
      jobs_success_rate: 0,
      cloud_cost_avoided_usd: 0,
      cloud_cost_avoided_basis: 0,
      claim_latency_ms_p50: null,
      claim_latency_ms_p95: null,
      claim_latency_ms_p99: null,
      claim_latency_ms_max: null,
      claim_latency_sample_count: 0,
      last_n_outcomes: [
        {
          job_id: 'job-failed',
          task_id: 'task-1234abcd',
          status: 'FAILED',
          outcome: null,
          created_at: null,
          completed_at: null,
          terminal_reason: 'process exited non-zero',
          exit_code: 137,
        },
      ],
    });

    const el = await mount();
    await openTelemetry(el);

    // No callback → no clickable link, but the id is still shown as plain text.
    expect(el.querySelector('[data-testid="fleet-outcome-task-link"]')).toBeNull();
    const detail = el.querySelector('[data-testid="fleet-runner-telemetry-detail"]');
    expect(detail?.textContent).toContain('task task-123');
  });

  // ── Projects allow-list section ──────────────────────────────────────────────

  // React tracks controlled <select> values via a native setter, same as text
  // inputs — assign through the prototype setter, then dispatch 'change'.
  function selectOption(select: HTMLSelectElement, value: string) {
    const setter = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value')?.set;
    setter?.call(select, value);
    select.dispatchEvent(new Event('change', { bubbles: true }));
  }

  test('renders the enrollment project marked default and non-removable', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([SAMPLE_RUNNERS[0]]);
    // Default listRunnerProjects mock returns the enrollment project only.
    const el = await mount();

    const section = el.querySelector('[data-testid="fleet-runner-projects"]');
    expect(section).not.toBeNull();
    // Enrollment project resolves to its owned-project name and is marked default.
    expect(section?.textContent).toContain('test-project');
    expect(section?.textContent).toContain('(default)');
    // The enrollment anchor has no remove control.
    expect(el.querySelector('button[aria-label^="Remove project"]')).toBeNull();
  });

  test('adding a project calls addRunnerProject then reloads the runner list', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([SAMPLE_RUNNERS[0]]);
    const el = await mount();

    const callsBefore = vi.mocked(listFleetRunners).mock.calls.length;

    const addBtn = el.querySelector('button[aria-label^="Add project to runner"]') as HTMLButtonElement;
    expect(addBtn).not.toBeNull();
    await act(async () => { addBtn.click(); });

    const select = el.querySelector('select[aria-label="Project to add"]') as HTMLSelectElement;
    expect(select).not.toBeNull();
    // proj-test is already on the allow-list, so only proj-extra is offered.
    expect(select.textContent).toContain('extra-project');
    expect(select.textContent).not.toContain('test-project');
    await act(async () => { selectOption(select, 'proj-extra'); });

    const saveBtn = el.querySelector('button[aria-label="Add selected project to runner"]') as HTMLButtonElement;
    await act(async () => { saveBtn.click(); });
    await act(async () => {});

    expect(addRunnerProject).toHaveBeenCalledWith('proj-test', 'runner-abc123', 'proj-extra');
    // load() re-fetches after a successful add.
    expect(vi.mocked(listFleetRunners).mock.calls.length).toBeGreaterThan(callsBefore);
  });

  test('removing a non-default project calls removeRunnerProject with its id', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([SAMPLE_RUNNERS[0]]);
    vi.mocked(listRunnerProjects).mockResolvedValueOnce({
      runner_id: 'runner-abc123',
      enrollment_project_id: 'proj-test',
      projects: [
        { project_id: 'proj-test', is_enrollment: true },
        { project_id: 'proj-extra', is_enrollment: false },
      ],
    });
    const el = await mount();

    const removeBtn = el.querySelector('button[aria-label="Remove project extra-project from runner"]') as HTMLButtonElement;
    expect(removeBtn).not.toBeNull();
    await act(async () => { removeBtn.click(); });
    await act(async () => {});

    expect(removeRunnerProject).toHaveBeenCalledWith('proj-test', 'runner-abc123', 'proj-extra');
  });

  test('surfaces the backend 403 cross-account rejection inline when an add fails', async () => {
    vi.mocked(listFleetRunners).mockResolvedValueOnce([SAMPLE_RUNNERS[0]]);
    vi.mocked(addRunnerProject).mockRejectedValueOnce(
      new Error('cannot attach a project owned by a different account to this runner'),
    );
    const el = await mount();

    const addBtn = el.querySelector('button[aria-label^="Add project to runner"]') as HTMLButtonElement;
    await act(async () => { addBtn.click(); });
    const select = el.querySelector('select[aria-label="Project to add"]') as HTMLSelectElement;
    await act(async () => { selectOption(select, 'proj-extra'); });
    const saveBtn = el.querySelector('button[aria-label="Add selected project to runner"]') as HTMLButtonElement;
    await act(async () => { saveBtn.click(); });
    await act(async () => {});

    expect(el.textContent).toContain('cannot attach a project owned by a different account');
    vi.mocked(listFleetRunners).mockReset();
  });
});
