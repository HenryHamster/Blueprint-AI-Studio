import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, createRef } from 'react';
import { expectNoOverflowAt } from '../test/viewport';
import { SavedViewsTabStrip } from './SavedViewsTabStrip';
import { type SavedView } from './savedViews';

const DEFAULT_VIEW_ID = '__default__';

const view = (id: string, name: string): SavedView => ({
  id,
  name,
  query: '',
  prefs: {},
  sort: 'importance',
  showResolved: false,
  routineExpanded: false,
  showOnlyNew: false,
  createdAt: 0,
});

describe('SavedViewsTabStrip — viewport regression', () => {
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

  test('saved-views tab strip renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;

      await act(async () => {
        r.render(
          <SavedViewsTabStrip
            savedViews={[view('v1', 'Inbox'), view('v2', 'Triage')]}
            activeViewId="v1"
            defaultViewId={DEFAULT_VIEW_ID}
            activeTabModified
            openKebabId={null}
            setOpenKebabId={() => {}}
            kebabRef={createRef<HTMLDivElement>()}
            onApplyView={() => {}}
            onRenameView={() => {}}
            onDeleteView={() => {}}
          />,
        );
      });
    });
  });
});
