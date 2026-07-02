import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { KeyboardShortcutsOverlay } from './KeyboardShortcutsOverlay';
import { expectFullViewportAt } from '../test/viewport';

describe('KeyboardShortcutsOverlay viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    host?.remove();
    host = null;
  });

  function mount() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    act(() => {
      r.render(<KeyboardShortcutsOverlay open onClose={vi.fn()} />);
    });
  }

  test('renders without clipping controls at 375px', async () => {
    await expectFullViewportAt(375, mount);
  });
});
