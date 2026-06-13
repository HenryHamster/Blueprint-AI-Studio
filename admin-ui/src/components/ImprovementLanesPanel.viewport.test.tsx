import { afterEach, beforeEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ImprovementLanesPanel } from './ImprovementLanesPanel';
import {
  expectMinTouchTargetAt,
  expectNoOverflowAt,
  expectNoTextTruncationAt,
} from '../test/viewport';

vi.mock('../api', () => ({
  drainLanePilot: vi.fn(),
  fetchLanePilotPreflight: vi.fn(async () => null),
  fetchImprovementLanes: vi.fn(async () => ({
    total: 2,
    lanes: [
      {
        id: 'lane-1',
        status: 'ACTIVE',
        branch: 'lane/super-long-improvement-branch-name-that-must-wrap-on-mobile',
        direction_id: 'direction-with-a-very-long-identifier-for-mobile-wrap',
        last_checkpoint_sha: 'abc1234def5678901234567890123456789012ab',
        last_checkpoint_phase: 'fix',
        last_checkpoint_seq: 7,
        last_checkpoint_age_seconds: 3600,
        workspace_state: 'WARM_RESERVED',
        workspace_vm_instance_id: 'agent-vm-improvement-lane-mobile-001',
        reserved_vm_instance_id: 'agent-vm-reserved-lane-mobile-001',
        reservation_expires_at: '2026-06-11T12:30:00Z',
        reservation_claimed_by_fix_count: 5,
        reservation_expired_before_fix_count: 2,
        reservation_force_expired_count: 1,
        pilot_arm: 'lane',
        phase_requeues: 1,
        llm_input_tokens_total: 12345,
        llm_output_tokens_total: 2345,
        vm_cost_usd_total: 4.56,
        reserved_idle_cost_usd_total: 0.78,
        checkpoint_loss_count: 0,
        shadow: false,
      },
      {
        id: 'lane-2',
        status: 'PARKED',
        branch: 'lane/shadow-mode-branch-with-long-name',
        direction_id: null,
        last_checkpoint_sha: null,
        last_checkpoint_phase: null,
        last_checkpoint_seq: null,
        last_checkpoint_age_seconds: null,
        workspace_state: 'COLD',
        workspace_vm_instance_id: null,
        reserved_vm_instance_id: null,
        reservation_expires_at: null,
        reservation_claimed_by_fix_count: 0,
        reservation_expired_before_fix_count: 0,
        reservation_force_expired_count: 0,
        pilot_arm: null,
        phase_requeues: 0,
        llm_input_tokens_total: 0,
        llm_output_tokens_total: 0,
        vm_cost_usd_total: 0,
        reserved_idle_cost_usd_total: 0,
        checkpoint_loss_count: 1,
        shadow: true,
      },
    ],
  })),
}));

describe('ImprovementLanesPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    localStorage.setItem('blueprint-admin:improvementLanesPanelCollapsed', 'false');
  });

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
    localStorage.clear();
  });

  async function renderPanel() {
    host = document.createElement('div');
    host.style.padding = '16px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<ImprovementLanesPanel projectId="proj-1" />);
    });
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 50));
    });
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, renderPanel);
  });

  test('operator-facing text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, renderPanel);
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, renderPanel, { selector: 'button, a[href]' });
  });
});
