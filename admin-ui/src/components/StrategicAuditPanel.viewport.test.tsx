import { afterEach, beforeEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicAuditPanel } from './StrategicAuditPanel';
import type { ActionabilityResponse, RetirementCritiquesResponse } from '../api';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  fetchRetirementCritiques: vi.fn(),
  fetchResearchActionability: vi.fn(),
}));

import * as api from '../api';

const sampleActionability: ActionabilityResponse = {
  cycles_scanned: 4,
  total_findings: 18,
  grade_distribution: { HIGH: 6, MEDIUM: 8, LOW: 4 },
  grade_tasked: { HIGH: 5, MEDIUM: 5, LOW: 2 },
  grade_merged: { HIGH: 4, MEDIUM: 3, LOW: 1 },
  grade_merge_rate: { HIGH: 0.8, MEDIUM: 0.6, LOW: 0.5 },
  calibration_score: 0.3,
  cycle_history: [
    {
      cycle_id: 'cycle-aaa',
      cycle_created_at: '2026-04-01T00:00:00Z',
      total_findings: 5,
      grade_distribution: { HIGH: 2, MEDIUM: 2, LOW: 1 },
      grade_merged: { HIGH: 1, MEDIUM: 1, LOW: 0 },
      grade_merge_rate: { HIGH: 0.5, MEDIUM: 0.5, LOW: null },
      calibration_score: 0.2,
    },
    {
      cycle_id: 'cycle-bbb',
      cycle_created_at: '2026-04-15T00:00:00Z',
      total_findings: 6,
      grade_distribution: { HIGH: 2, MEDIUM: 3, LOW: 1 },
      grade_merged: { HIGH: 2, MEDIUM: 1, LOW: 0 },
      grade_merge_rate: { HIGH: 1.0, MEDIUM: 0.33, LOW: null },
      calibration_score: 0.28,
    },
    {
      cycle_id: 'cycle-ccc',
      cycle_created_at: '2026-05-01T00:00:00Z',
      total_findings: 7,
      grade_distribution: { HIGH: 2, MEDIUM: 3, LOW: 2 },
      grade_merged: { HIGH: 1, MEDIUM: 1, LOW: 1 },
      grade_merge_rate: { HIGH: 0.5, MEDIUM: 0.33, LOW: 0.5 },
      calibration_score: 0.3,
    },
  ],
};

const sampleCritiquesResponse: RetirementCritiquesResponse = {
  total: 2,
  critiques: [
    {
      direction_id: 'dir-aaa111',
      what_it_addressed: 'Reduce p99 latency on the /api/admin/digest endpoint under load',
      pattern_that_produced_it: 'Over-broad scope that conflated caching, DB indexes, and response shape',
      lesson: 'Focus directions on a single measurable metric; split infra and schema changes into separate tracks',
      cycle_id: 'cycle-aaa',
      cycle_created_at: '2026-04-01T00:00:00Z',
    },
    {
      direction_id: 'dir-bbb222',
      what_it_addressed: 'database_connection_timeout_handling across Cloud SQL proxy restarts',
      pattern_that_produced_it: 'Assumed retry logic existed; found it was missing in prod path only',
      lesson: 'Always verify prod vs test divergence — test coverage does not imply prod coverage',
      cycle_id: 'cycle-bbb',
      cycle_created_at: '2026-04-15T00:00:00Z',
    },
  ],
};

describe('StrategicAuditPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.mocked(api.fetchRetirementCritiques).mockResolvedValue(sampleCritiquesResponse);
    vi.mocked(api.fetchResearchActionability).mockResolvedValue(sampleActionability);
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
  });

  test('grade-distribution rows and recent-cycle rows fit at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<StrategicAuditPanel projectId="project-test" />);
      });
      // Let the mocked API promises resolve and state settle.
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });
});
