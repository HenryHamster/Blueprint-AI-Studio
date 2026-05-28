import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MessagesSummaryCard } from './MessagesSummaryCard';
import { expectNoOverflowAt } from '../test/viewport';

describe('MessagesSummaryCard — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders all summary categories without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <MessagesSummaryCard
            newMessageCounts={{
              blockers: 12,
              questions: 7,
              feedback: 3,
              bugReports: 5,
              routine: 24,
              highSignalNotes: 4,
            }}
            onScrollToBlockers={() => { /* no-op */ }}
            onScrollToQuestions={() => { /* no-op */ }}
            onScrollToRoutine={() => { /* no-op */ }}
          />,
        );
      });
    });
  });
});
