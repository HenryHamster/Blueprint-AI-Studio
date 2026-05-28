import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { RecurringPatternsSection } from './RecurringPatternsSection';
import { expectNoOverflowAt } from '../test/viewport';

describe('RecurringPatternsSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders dense list of recurring patterns without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <RecurringPatternsSection
            recurringPatterns={[
              {
                fingerprint: 'fp-aaaaaaaaaaaaaaaaaaaa1',
                occurrence_count: 17,
                severity: 'high',
                example_message: 'AssertionError: expected viewport overflow to be absent at 375px but rendered child exceeded host width by 42px on operator-facing dashboard panel',
                affected_direction_count: 4,
                affected_directions: [
                  { direction_id: 'd-001', direction_text: 'Improve admin UI mobile responsiveness across all operator-facing panels for narrow widths' },
                  { direction_id: 'd-002', direction_text: 'Stabilize the autonomous fix loop diagnosis-to-retry pipeline against churn' },
                  { direction_id: 'd-003', direction_text: 'Reduce reviewer false-positive flag rate in the self-review pipeline' },
                  { direction_id: 'd-004', direction_text: 'Compact horizontal layout for catch-up dashboard summary blocks' },
                  { direction_id: 'd-005', direction_text: 'Additional direction beyond the visible cap to exercise +N more counter' },
                ],
                remediation_status: 'investigating',
              },
              {
                fingerprint: 'fp-bbbbbbbbbbbbbbbbbbbb2',
                occurrence_count: 5,
                example_message: 'Recurring lint regression: unused-import warning re-introduced by the same refactor template across multiple extracted section components',
                affected_direction_count: 1,
                affected_directions: [
                  { direction_id: 'd-006', direction_text: 'Quiet pre-existing eslint warnings on changed files only' },
                ],
                remediation_status: 'fix_shipped',
              },
              {
                fingerprint: 'fp-ccccccccccccccccccc3',
                occurrence_count: 2,
                example_message: 'Short example.',
                affected_direction_count: 0,
                remediation_status: 'fix_verified',
              },
            ]}
            onDirectionClick={() => { /* no-op */ }}
          />,
        );
      });
    });
  });
});
