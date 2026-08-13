import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ConversationUtilityBar } from './ConversationUtilityBar';
import { expectNoOverflowAt } from '../test/viewport';

describe('ConversationUtilityBar — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  async function renderBar(props: {
    expanded: boolean;
    searchQuery?: string;
  }) {
    host = document.createElement('div');
    host.style.width = '100%';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ConversationUtilityBar
          expanded={props.expanded}
          onExpand={() => {}}
          onCollapse={() => {}}
          searchQuery={props.searchQuery ?? ''}
          onSearchChange={() => {}}
        />,
      );
    });
  }

  test('collapsed search control does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, () => renderBar({ expanded: false }));
  });

  test('expanded search input does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, () =>
      renderBar({ expanded: true, searchQuery: 'deployment rollback investigation notes and more text' }),
    );
  });

  test('collapsed search control does not overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, () => renderBar({ expanded: false }));
  });
});
