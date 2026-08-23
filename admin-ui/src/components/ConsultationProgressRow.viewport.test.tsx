import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ConsultationProgressRow } from './ConsultationProgressRow';
import { expectNoOverflowAt } from '../test/viewport';
import type { ConsultationIndexRow } from '../consultationDetailApi';

describe('ConsultationProgressRow — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  const mount = async (row: ConsultationIndexRow) => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <ConsultationProgressRow projectId="p" consultationId="c-1" row={row} />,
        );
      });
    });
  };

  test('the longest state label and a long question do not overflow at 375px', async () => {
    // `stalled` carries the longest label in the set, and the question preview
    // is operator-authored text with no natural bound.
    await mount({
      consultation_id: 'c-1',
      state: 'stalled',
      outcome: null,
      requested_at: new Date(Date.now() - 900_000).toISOString(),
      question_preview: {
        text: 'why did the promotion qualifier strand every discovery direction '
          + 'after the snapshot truncation landed on automain last night',
        truncated: true,
        chars: 240,
      },
      step_count: 12,
      steps_truncated: true,
      claim_expired: true,
      delivered_at: null,
    });
  });

  test('a delivered row does not overflow at 375px', async () => {
    await mount({
      consultation_id: 'c-2',
      state: 'delivered',
      outcome: 'completed',
      requested_at: new Date(Date.now() - 3_600_000).toISOString(),
      question_preview: { text: 'why is CI red', truncated: false, chars: 13 },
      step_count: 1,
      steps_truncated: false,
      claim_expired: false,
      delivered_at: new Date().toISOString(),
    });
  });
});
