import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { ToolResidentHandoffControls } from './ToolResidentHandoffControls';

describe('ToolResidentHandoffControls — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.resetAllMocks();
  });

  // The lane-ownership input + Share/Ask buttons wrap in a flex row that must
  // not push past the 375px mobile viewport, and none of the labels/status text
  // may be silently clipped. The wrapper carries data-viewport-allow-small-target
  // (intentional compact chip styling), so the touch-target check is opted out;
  // overflow and text-truncation are the operator-visible risks this surface
  // must not regress.
  test('renders handoff controls without overflow or clipped text at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ToolResidentHandoffControls
            toolLabel="Whiteboard"
            onHandoff={vi.fn().mockResolvedValue(undefined)}
          />,
        );
      });
    });
    if (!host) throw new Error('host not mounted');
    expect(host.querySelector('button')).toBeTruthy();
    await expectNoTextTruncationAt(375, async () => {});
  });

  test('renders the disabled-reason + preparing states without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ToolResidentHandoffControls
            toolLabel="Whiteboard"
            disabledReason="Wait for the current save to finish before sharing."
            preparing
            onHandoff={vi.fn().mockResolvedValue(undefined)}
          />,
        );
      });
    });
    if (!host) throw new Error('host not mounted');
    await expectNoTextTruncationAt(375, async () => {});
  });
});
