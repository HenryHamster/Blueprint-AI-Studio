import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt } from '../test/viewport';
import { DirectionPortfolioSection, type DirectionProgress } from './DirectionPortfolioSection';

vi.mock('./StrategicAuditPanel', () => ({
  StrategicAuditPanel: () => null,
}));

const { fetchEvictedFindingsMock } = vi.hoisted(() => ({
  fetchEvictedFindingsMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  fetchEvictedFindings: fetchEvictedFindingsMock,
  API_BASE_URL: '',
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
}));

function makeDirection(): DirectionProgress {
  return {
    direction_id: 'dir-1',
    direction_text: 'Improve retry handling',
    priority: 'high',
    cycles_in_window: 2,
    tasks_created: 3,
    prs_merged: 12,
    possibly_saturated: false,
    conversion_rate: 0.65,
    acceptance_rate: 0.85,
    first_attempt_success_rate: 0.4,
    first_attempt_sample_size: 10,
    estimated_cost_usd: 45.23,
    cost_per_merged_pr: 45.23,
    avg_time_to_merge_hours: 1.2,
  };
}

describe('DirectionPortfolioSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) {
      const r = root;
      act(() => r.unmount());
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionPortfolioSection
            directions={[makeDirection()]}
            loading={false}
            productPct={null}
            totalDirPRs={12}
            hasAttribution={false}
            totalAutonomousPRs={null}
            totalAssistedPRs={null}
            selfSufficientCount={1}
          />,
        );
      });
    });
  });

  test('renders the dense evicted-findings drawer without horizontal overflow at 375px', async () => {
    fetchEvictedFindingsMock.mockResolvedValue({
      direction_id: 'dir-1',
      total: 11,
      offset: 0,
      limit: 10,
      evicted_findings: Array.from({ length: 10 }, (_, i) => ({
        title: `Evicted finding ${i + 1}`,
        claims: [`Claim ${i + 1}`],
        source_refs: [`backend/app/example_${i + 1}.py:12`],
        status: 'evicted',
        evicted_at: new Date(Date.now() - (i + 1) * 60_000).toISOString(),
      })),
    });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <DirectionPortfolioSection
            directions={[{
              ...makeDirection(),
              history_aggregate: { evicted_findings: 11 },
            }]}
            loading={false}
            productPct={null}
            totalDirPRs={12}
            hasAttribution={false}
            totalAutonomousPRs={null}
            totalAssistedPRs={null}
            selfSufficientCount={1}
            projectId="proj-1"
          />,
        );
      });
      await act(async () => {
        const btn = host?.querySelector('button[title="View evicted findings for this direction"]') as HTMLButtonElement | null;
        btn?.click();
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
    });
  });
});
