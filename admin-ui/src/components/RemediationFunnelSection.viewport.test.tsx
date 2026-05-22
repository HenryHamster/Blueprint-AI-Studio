import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { RemediationFunnelSection } from './RemediationFunnelSection';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/residual-fingerprints')) {
      return new Response(
        JSON.stringify({
          window_hours: 24,
          total_code_error_count: 142,
          residual_count: 7,
          top_residuals: [
            {
              fingerprint: 'a1b2c3d4e5f6',
              count: 12,
              most_recent_at: '2026-05-08T10:00:00Z',
              sample_message:
                'sqlalchemy.exc.IntegrityError: duplicate key value violates unique constraint "task_pkey"',
              example_task_ids: ['task-aaa', 'task-bbb', 'task-ccc'],
              case_status: 'REMEDIATING',
              last_spawned_pr_number: 4523,
              consecutive_failure_count: 2,
              audit_defer_count: 1,
              audit_deferred_system_id: 'orchestrator-task-lifecycle',
            },
          ],
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
}));

const funnel = {
  investigations_spawned: 28,
  investigations_completed: 24,
  investigations_with_diagnosis: 18,
  remediations_spawned: 14,
  remediations_merged: 9,
  remediations_failed: 3,
  fixes_held: 4,
  avg_time_to_remediation_hours: 1.7,
  active_investigations: 4,
  investigations_by_confidence: { high: 8, medium: 12, low: 6, unknown: 2 },
  failed_remediations_by_class: { CODE_ERROR: 2, AGENT_TIMEOUT: 1 },
  merged_remediations_by_trigger_class: { CODE_ERROR: 5, AGENT_TIMEOUT: 2, PROVISION_FAILURE: 2 },
  cumulative_merged_by_trigger_class: { CODE_ERROR: 47, AGENT_TIMEOUT: 14, PROVISION_FAILURE: 6, RATE_LIMIT: 2 },
  investigations_spawned_by_class: { CODE_ERROR: 14, AGENT_TIMEOUT: 6, PROVISION_FAILURE: 5, RATE_LIMIT: 2, EXTERNAL_API: 1 },
  investigations_completed_by_class: { CODE_ERROR: 12, AGENT_TIMEOUT: 5, PROVISION_FAILURE: 4, RATE_LIMIT: 2, EXTERNAL_API: 1 },
  remediations_spawned_by_class: { CODE_ERROR: 7, AGENT_TIMEOUT: 3, PROVISION_FAILURE: 3, RATE_LIMIT: 1 },
  remediation_success_by_confidence: {
    high: { merged: 6, failed: 1 },
    medium: { merged: 2, failed: 1 },
    low: { merged: 1, failed: 1 },
  },
  diagnosis_drop_off_by_reason: {
    structural: 3,
    no_recommended_fix: 2,
    low_confidence: 1,
  },
  ci_auto_remediation: {
    spawned: 9,
    merged: 5,
    failed: 2,
    by_ci_failure_type: {
      typescript_compilation_failure: { spawned: 4, merged: 3, failed: 1 },
      eslint_violation: { spawned: 3, merged: 2, failed: 0 },
      pytest_collection_error: { spawned: 2, merged: 0, failed: 1 },
    },
  },
  residual_remediation_funnel: {
    spawned: 5,
    merged: 2,
    failed: 1,
    remediating: 2,
    open_cases_by_error_class: [],
  },
  prior_investigation_reuse: {
    total_with_prior: 6,
    merged_with_prior: 4,
    failed_with_prior: 1,
    merge_rate: 0.67,
  },
  recent_investigations: [
    {
      task_id: 'task-investigation-1',
      direction_id: 'dir-aaa',
      summary:
        'Diagnosis: residual sqlalchemy.exc.IntegrityError on retry — root cause is missing ON CONFLICT clause when re-provisioning a task whose row already exists',
      confidence: 'high',
      severity: 'high',
      completed_at: '2026-05-08T11:00:00Z',
    },
  ],
  active_investigations_detail: [
    { task_id: 'task-active-1', direction_id: 'dir-bbb', elapsed_hours: 3.4 },
    { task_id: 'task-active-2', direction_id: 'dir-ccc', elapsed_hours: 0.5 },
  ],
};

const conversion = {
  total_completed: 24,
  parseable: 22,
  unparseable: 2,
  structural: 3,
  low_confidence: 4,
  actionable: 15,
  remediation_spawned: 14,
};

describe('RemediationFunnelSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders funnel + class breakdown without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <RemediationFunnelSection
            funnel={funnel}
            conversion={conversion}
            loading={false}
            projectId="project-canary"
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
      // Verify the "By failure class" section and its cards are rendered.
      // At 375px the stacked-card layout (md:hidden) is active; the desktop
      // nine-column grid (hidden md:block) is hidden by Tailwind breakpoint.
      const text = host!.textContent ?? '';
      if (!text.includes('By failure class')) {
        throw new Error('"By failure class" section not found at 375px');
      }
      if (!text.includes('CODE_ERROR')) {
        throw new Error('CODE_ERROR class card not rendered at 375px');
      }
      if (!text.includes('AGENT_TIMEOUT')) {
        throw new Error('AGENT_TIMEOUT class card not rendered at 375px');
      }
      // The class breakdown now uses responsive card layout at mobile widths —
      // it no longer carries data-viewport-allow-overflow. Only the horizontal
      // stage-flow arrows section should retain the opt-out (exactly one).
      const escapeHatches = host!.querySelectorAll('[data-viewport-allow-overflow]');
      if (escapeHatches.length > 1) {
        throw new Error(
          `data-viewport-allow-overflow: expected 1 (funnel stage flow), found ${escapeHatches.length} — class breakdown should use responsive layout at 375px instead`,
        );
      }
    });
  });

  test('renders funnel + class breakdown without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <RemediationFunnelSection
            funnel={funnel}
            conversion={conversion}
            loading={false}
            projectId="project-canary"
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
    });
  });

  // Pre-existing dense chips (overdue, expand/collapse rows, ignore/undo
  // controls) carry `data-viewport-allow-small-target` and are skipped — a
  // newly-introduced small button without that opt-out attribute is flagged.
  test('has no undocumented small touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <RemediationFunnelSection
            funnel={funnel}
            conversion={conversion}
            loading={false}
            projectId="project-canary"
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 80));
      });
    });
  });

  test('loading skeleton renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <RemediationFunnelSection
            funnel={null}
            conversion={null}
            loading={true}
            projectId="project-canary"
          />,
        );
      });
    });
  });
});
