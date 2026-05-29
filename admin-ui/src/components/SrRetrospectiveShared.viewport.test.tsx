import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt } from '../test/viewport';
import {
  RetrospectiveDayWindowButtons,
  RetrospectivePagination,
  RetrospectiveRefreshButton,
  SrCatchRateFreshnessPill,
  SrCatchRateHealthBanner,
} from './SrRetrospectiveShared';

describe('SrRetrospectiveShared — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) {
      act(() => {
        root?.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('shared retrospective controls render without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      await act(async () => {
        r.render(
          <div className="space-y-3 text-xs text-neutral-200">
            <div className="flex flex-wrap items-center gap-2">
              <RetrospectiveDayWindowButtons days={30} onChange={() => {}} />
              <SrCatchRateFreshnessPill
                health={{
                  status: 'stale',
                  data_sufficient: true,
                  last_sufficient_computed_at: '2026-05-29T08:00:00Z',
                  last_sufficient_age_hours: 27.2,
                  signal_last_computed_at: '2026-05-29T09:00:00Z',
                  signal_age_hours: 26.1,
                  category_count: 12,
                  categories_with_hit_rate: 8,
                  overall_hit_rate: 0.71,
                  previous_overall_hit_rate: 0.68,
                  bucket_mode: 'cycle',
                  prompt_version_changed: false,
                  mixed_prompt_versions: false,
                }}
              />
            </div>

            <SrCatchRateHealthBanner
              health={{
                status: 'frozen',
                last_sufficient_age_hours: 36,
                signal_age_hours: 6,
              }}
              onDismiss={() => {}}
            />

            <div className="flex flex-wrap items-start justify-between gap-2">
              <RetrospectivePagination
                page={7}
                total={127}
                totalPages={13}
                pageSize={10}
                setPage={() => {}}
              />
              <RetrospectiveRefreshButton
                isStale
                loading={false}
                onRefresh={() => {}}
              />
            </div>
          </div>,
        );
      });
    });
  });
});
