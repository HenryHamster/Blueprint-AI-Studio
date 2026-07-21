import { render, screen } from '@testing-library/react';
import { describe, expect, test } from 'vitest';
import type { ResidentEngineHealth } from '../strategicStateApi';
import { ResidentEngineHealthChip } from './ResidentEngineHealthChip';

function health(overrides: Partial<ResidentEngineHealth> = {}): ResidentEngineHealth {
  return {
    project_id: 'project-1',
    observed_at: '2026-07-18T00:00:00Z',
    resident_initialized: true,
    engine_owner: 'strategic_resident',
    engine_version: 'v2',
    background_policy: 'active',
    authority_generation: 4,
    health_status: 'healthy',
    turns: { active: null, interactive_successor: null, background_successor: null },
    operations: { unsettled_receipt_count: 0 },
    soak: { cutover_completed_at: null, observed_seconds: 86_400, required_seconds: 604_800, rollback_rehearsed_at: null },
    cutover_ready: true,
    rollback_ready: true,
    legacy_retirement_ready: false,
    blockers: [],
    ...overrides,
  };
}

describe('ResidentEngineHealthChip', () => {
  test('shows authoritative active-turn and coalesced successor state', () => {
    render(<ResidentEngineHealthChip health={health({
      turns: {
        active: { task_id: 'turn-1', status: 'IN_PROGRESS', updated_at: null },
        interactive_successor: { task_id: 'turn-2', status: 'QUEUED', updated_at: null },
        background_successor: null,
      },
    })} />);
    expect(screen.getByTestId('resident-engine-health')).toHaveTextContent('Resident working');
    expect(screen.getByTestId('resident-engine-health')).toHaveTextContent('1 queued');
  });

  test('distinguishes paused and shadow rollout states', () => {
    const { rerender } = render(<ResidentEngineHealthChip health={health({ background_policy: 'paused' })} />);
    expect(screen.getByTestId('resident-engine-health')).toHaveTextContent('Background autonomy paused');
    rerender(<ResidentEngineHealthChip health={health({ engine_owner: 'legacy' })} />);
    expect(screen.getByTestId('resident-engine-health')).toHaveTextContent('Resident shadowing');
  });
});
