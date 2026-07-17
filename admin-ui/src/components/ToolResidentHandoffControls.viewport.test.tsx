import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ToolResidentHandoffControls } from './ToolResidentHandoffControls';
import { expectNoOverflowAt } from '../test/viewport';

describe('ToolResidentHandoffControls — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  const noop = async () => {};

  test('handoff controls do not overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <ToolResidentHandoffControls toolLabel="Whiteboard" onHandoff={noop} />,
        );
      });
    });
  });

  test('disabled state with a long reason does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <ToolResidentHandoffControls
            toolLabel="Whiteboard"
            disabledReason="Another lane owner is preparing a Resident handoff for this tool right now."
            onHandoff={noop}
          />,
        );
      });
    });
  });
});
