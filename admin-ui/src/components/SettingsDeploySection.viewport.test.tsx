import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DeploySection } from './SettingsDeploySection';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';
import type { DeployState } from '../types/settings';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({ merge_grade_block_threshold: 0.5 }), { status: 200 })),
}));

const noop = async () => {};

const baseDeployState: DeployState = {
  status: 'STABLE',
  current_revision: 'rev-42',
  current_sha: 'abcdef1234567890',
  deployed_at: '2026-05-06T10:00:00Z',
  last_known_good_revision: 'rev-41',
  last_known_good_sha: 'fedcba0987654321',
  rollback_reason: null,
  rollback_count: 0,
  pause_reason: null,
  paused_at: null,
  reason: null,
  pre_pause_max_vms: null,
  failure_rate: 0,
  monitoring_window: null,
  task_results_count: 0,
  gcp_project_id: 'my-gcp-project',
  health_recovery_streak: 0,
  health_recovery_streak_required: 3,
  health_failure_streak: 0,
  health_failure_streak_threshold: 3,
};

const zoneHealthAllHealthy: Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> = {
  'us-central1-a': { status: 'healthy', last_success: '2026-05-06T09:55:00Z', last_failure: null, last_failure_reason: null, consecutive_failures: 0, cooldown_active: false },
  'us-central1-b': { status: 'healthy', last_success: '2026-05-06T09:56:00Z', last_failure: null, last_failure_reason: null, consecutive_failures: 0, cooldown_active: false },
  'us-east1-a': { status: 'healthy', last_success: '2026-05-06T09:54:00Z', last_failure: null, last_failure_reason: null, consecutive_failures: 0, cooldown_active: false },
  'europe-west1-b': { status: 'healthy', last_success: '2026-05-06T09:53:00Z', last_failure: null, last_failure_reason: null, consecutive_failures: 0, cooldown_active: false },
  'asia-east1-a': { status: 'healthy', last_success: '2026-05-06T09:52:00Z', last_failure: null, last_failure_reason: null, consecutive_failures: 0, cooldown_active: false },
};

const zoneHealthWithFailures: Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> = {
  'us-central1-a': { status: 'healthy', last_success: '2026-05-06T09:55:00Z', last_failure: null, last_failure_reason: null, consecutive_failures: 0, cooldown_active: false },
  'us-central1-b': { status: 'exhausted', last_success: '2026-05-06T09:00:00Z', last_failure: '2026-05-06T09:50:00Z', last_failure_reason: 'quota exceeded', consecutive_failures: 5, cooldown_active: true },
  'us-east1-a': { status: 'degraded', last_success: '2026-05-06T09:40:00Z', last_failure: '2026-05-06T09:48:00Z', last_failure_reason: 'connection timeout', consecutive_failures: 2, cooldown_active: false },
};

describe('SettingsDeploySection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px with null deploy state', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <DeploySection
            deployState={null}
            onPausePipeline={noop}
            onResumePipeline={noop}
            onResetHealth={noop}
            onRollback={noop}
            onMarkStable={noop}
            onRepairState={noop}
            zoneHealth={null}
          />
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px with stable deploy state', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'STABLE',
      failure_rate: 0.05,
      external_health: {
        github_api: true,
        db: true,
        dispatch_api: true,
        anthropic_api: true,
        openai_api: false,
        gemini_api: true,
        gcp: true,
        last_check: '2026-05-06T10:00:00Z',
        errors: { openai_api: 'Connection refused: rate limit exceeded for org' },
      },
      health_check_interval_seconds: 60,
      health_failure_streak: 1,
    };

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <DeploySection
            deployState={deployState}
            onPausePipeline={noop}
            onResumePipeline={noop}
            onResetHealth={noop}
            onRollback={noop}
            onMarkStable={noop}
            onRepairState={noop}
            zoneHealth={zoneHealthAllHealthy}
          />
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px with monitoring state', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'MONITORING',
      monitoring_window: { elapsed_seconds: 1200, total_seconds: 3600, percent: 33 },
      task_results_count: 47,
      failure_rate: 0.12,
    };

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <DeploySection
            deployState={deployState}
            onPausePipeline={noop}
            onResumePipeline={noop}
            onResetHealth={noop}
            onRollback={noop}
            onMarkStable={noop}
            onRepairState={noop}
            zoneHealth={zoneHealthWithFailures}
          />
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px with paused state and recovery progress', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'PAUSED',
      pause_reason: 'Automatic pause due to elevated failure rate exceeding threshold of 0.30',
      pre_pause_max_vms: 4,
      health_recovery_streak: 2,
      health_recovery_streak_required: 3,
      health_recovery_history: [true, true, false, false, false],
      health_recovery_history_size: 5,
      failure_rate: 0.45,
    };

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <DeploySection
            deployState={deployState}
            onPausePipeline={noop}
            onResumePipeline={noop}
            onResetHealth={noop}
            onRollback={noop}
            onMarkStable={noop}
            onRepairState={noop}
            zoneHealth={zoneHealthWithFailures}
          />
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px with flap-escalated recovery', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'PAUSED',
      pause_reason: 'Automatic pause due to flapping (repeated auto-pause/resume)',
      pre_pause_max_vms: 4,
      health_recovery_streak: 4,
      health_recovery_streak_required: 5,
      health_recovery_flap_escalated: true,
      health_recovery_history: [true, true, true, true, false],
      health_recovery_history_size: 5,
      failure_rate: 0.45,
    };

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <DeploySection
            deployState={deployState}
            onPausePipeline={noop}
            onResumePipeline={noop}
            onResetHealth={noop}
            onRollback={noop}
            onMarkStable={noop}
            onRepairState={noop}
            zoneHealth={zoneHealthWithFailures}
          />
        );
      });
    });
  });

  test('renders without horizontal overflow at 375px with rolled back state', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'ROLLED_BACK',
      rollback_reason: 'Automatic rollback triggered: failure rate 0.52 exceeded threshold 0.30 after 87 task results',
      rollback_count: 3,
      failure_rate: 0.52,
    };

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <DeploySection
            deployState={deployState}
            onPausePipeline={noop}
            onResumePipeline={noop}
            onResetHealth={noop}
            onRollback={noop}
            onMarkStable={noop}
            onRepairState={noop}
            zoneHealth={null}
          />
        );
      });
    });
  });

  test('rolled-back state offers Resume and Mark Stable, hides Pause, and disables Rollback', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'ROLLED_BACK',
      rollback_reason: 'Automatic rollback triggered: failure rate 0.52 exceeded threshold 0.30 after 87 task results',
      rollback_count: 3,
      failure_rate: 0.52,
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <DeploySection
          deployState={deployState}
          onPausePipeline={noop}
          onResumePipeline={noop}
          onResetHealth={noop}
          onRollback={noop}
          onMarkStable={noop}
          onRepairState={noop}
          zoneHealth={null}
        />
      );
    });
    const buttons = Array.from(host.querySelectorAll('button'));
    const labels = buttons.map((b) => b.textContent?.trim() ?? '');
    // ROLLED_BACK is a first-class operator state: the recovery path (Resume +
    // Mark Stable) is offered and the misleading Pause control is suppressed.
    expect(labels.some((l) => l.includes('Resume'))).toBe(true);
    expect(labels.some((l) => l.includes('Mark Stable'))).toBe(true);
    expect(labels.some((l) => l.includes('Pause'))).toBe(false);
    // Backend rejects a re-rollback (400 "Already rolled back"); the button
    // must not invite an action the backend will refuse.
    const rollbackBtn = buttons.find((b) => b.textContent?.includes('Rollback'));
    expect(rollbackBtn).toBeTruthy();
    expect((rollbackBtn as HTMLButtonElement).disabled).toBe(true);
  });

  test('halted state offers Mark Stable, hides Resume and Pause', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'HALTED',
      rollback_reason: 'Pipeline halted after 5 consecutive rollbacks',
      rollback_count: 5,
      failure_rate: 0.6,
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <DeploySection
          deployState={deployState}
          onPausePipeline={noop}
          onResumePipeline={noop}
          onResetHealth={noop}
          onRollback={noop}
          onMarkStable={noop}
          onRepairState={noop}
          zoneHealth={null}
        />
      );
    });
    const buttons = Array.from(host.querySelectorAll('button'));
    const labels = buttons.map((b) => b.textContent?.trim() ?? '');
    // HALTED is cleared via mark-stable (resets the consecutive-rollback count);
    // the backend resume endpoint rejects a HALTED resume with HTTP 400, so the
    // panel must surface Mark Stable and must NOT offer Resume.
    expect(labels.some((l) => l.includes('Mark Stable'))).toBe(true);
    expect(labels.some((l) => l.includes('Resume'))).toBe(false);
    // Pausing an already-halted pipeline is meaningless — no Pause control.
    expect(labels.some((l) => l.includes('Pause'))).toBe(false);
    // Repair remains available as a generic recovery affordance.
    expect(labels.some((l) => l.includes('Repair'))).toBe(true);
  });

  test('renders without horizontal overflow at 375px with halted state', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'HALTED',
      rollback_reason: 'Pipeline halted after 5 consecutive rollbacks; mark stable to recover',
      rollback_count: 5,
      failure_rate: 0.6,
    };

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <DeploySection
            deployState={deployState}
            onPausePipeline={noop}
            onResumePipeline={noop}
            onResetHealth={noop}
            onRollback={noop}
            onMarkStable={noop}
            onRepairState={noop}
            zoneHealth={null}
          />
        );
      });
    });
  });

  test('surfaces rolled-back-from provenance and live-SHA labeling on rollback', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'ROLLED_BACK',
      current_sha: 'fedcba0987654321',
      rolled_back_from_sha: 'abcdef1234567890',
      rollback_reason: 'Manual rollback',
      rollback_count: 1,
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <DeploySection
          deployState={deployState}
          onPausePipeline={noop}
          onResumePipeline={noop}
          onResetHealth={noop}
          onRollback={noop}
          onMarkStable={noop}
          onRepairState={noop}
          zoneHealth={null}
        />
      );
    });
    const text = host.textContent ?? '';
    // Live SHA is the rolled-back target, explicitly labeled as live.
    expect(text).toContain('Live SHA');
    expect(text).toContain('fedcba09');
    // Provenance of the bad revision rolled back from is shown (short SHA).
    expect(text).toContain('rolled back from abcdef12');
  });

  test('omits rolled-back-from provenance when status is not a rollback', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'STABLE',
      rolled_back_from_sha: 'abcdef1234567890',
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <DeploySection
          deployState={deployState}
          onPausePipeline={noop}
          onResumePipeline={noop}
          onResetHealth={noop}
          onRollback={noop}
          onMarkStable={noop}
          onRepairState={noop}
          zoneHealth={null}
        />
      );
    });
    const text = host.textContent ?? '';
    expect(text).not.toContain('rolled back from');
  });

  test('shows escalated denominator and flapping indicator from deployState only', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'PAUSED',
      pre_pause_max_vms: 4,
      health_recovery_streak: 4,
      health_recovery_streak_required: 5,
      health_recovery_flap_escalated: true,
      health_recovery_history: [true, true, true, true, false],
      health_recovery_history_size: 5,
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <DeploySection
          deployState={deployState}
          onPausePipeline={noop}
          onResumePipeline={noop}
          onResetHealth={noop}
          onRollback={noop}
          onMarkStable={noop}
          onRepairState={noop}
          zoneHealth={null}
        />
      );
    });
    const text = host.textContent ?? '';
    // Denominator reflects the escalated requirement, not a hardcoded 3.
    expect(text).toContain('4/5 healthy checks');
    // Flapping indicator explains the raised bar.
    expect(text.toLowerCase()).toContain('flapping');
    expect(text).toContain('Recovery bar raised to 5');
  });

  test('omits flapping indicator when not escalated', async () => {
    const deployState: DeployState = {
      ...baseDeployState,
      status: 'PAUSED',
      health_recovery_streak: 2,
      health_recovery_streak_required: 3,
      health_recovery_history: [true, true, false, false, false],
      health_recovery_history_size: 5,
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <DeploySection
          deployState={deployState}
          onPausePipeline={noop}
          onResumePipeline={noop}
          onResetHealth={noop}
          onRollback={noop}
          onMarkStable={noop}
          onRepairState={noop}
          zoneHealth={null}
        />
      );
    });
    const text = host.textContent ?? '';
    expect(text).toContain('2/3 healthy checks');
    expect(text.toLowerCase()).not.toContain('flapping');
  });

  test('renders a durable failed-rollback banner when failedRollback is set', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <DeploySection
          deployState={baseDeployState}
          onPausePipeline={noop}
          onResumePipeline={noop}
          onResetHealth={noop}
          onRollback={noop}
          onMarkStable={noop}
          onRepairState={noop}
          failedRollback={{ serving_sha: 'badc0ffee0000000', gcloud_error: 'traffic shift rejected' }}
          zoneHealth={null}
        />
      );
    });
    const banner = host.querySelector('[role="alert"]');
    expect(banner).toBeTruthy();
    const text = banner?.textContent ?? '';
    // States that the rollback failed and traffic still serves the bad revision.
    expect(text).toContain('Last rollback failed');
    expect(text).toContain('badc0ffe');
    // Surfaces the gcloud reason and invites a retry.
    expect(text).toContain('traffic shift rejected');
    expect(text.toLowerCase()).toContain('retry');
  });

  test('omits the failed-rollback banner when failedRollback is null', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <DeploySection
          deployState={baseDeployState}
          onPausePipeline={noop}
          onResumePipeline={noop}
          onResetHealth={noop}
          onRollback={noop}
          onMarkStable={noop}
          onRepairState={noop}
          failedRollback={null}
          zoneHealth={null}
        />
      );
    });
    expect(host.querySelector('[role="alert"]')).toBeNull();
    expect(host.textContent ?? '').not.toContain('Last rollback failed');
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <DeploySection
            deployState={baseDeployState}
            onPausePipeline={noop}
            onResumePipeline={noop}
            onResetHealth={noop}
            onRollback={noop}
            onMarkStable={noop}
            onRepairState={noop}
            zoneHealth={zoneHealthAllHealthy}
          />
        );
      });
    }, { selector: 'button, a[href]' });
  });
});
