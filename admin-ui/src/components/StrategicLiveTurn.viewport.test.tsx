import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  StrategicActivityStrip,
  StrategicResidentActivityWidget,
  StrategicStreamingBubble,
} from './StrategicLiveTurn';
import { expectNoOverflowAt } from '../test/viewport';

describe('StrategicLiveTurn — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('streaming reply and reasoning remain contained at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <StrategicStreamingBubble segments={[
            { kind: 'reasoning', text: 'Checking whether the active resident can continue safely.' },
            { kind: 'reply', text: 'Workday is active and ready for another direction.' },
          ]} />,
        );
      });
    });
  });

  test('live activity and resident status remain contained at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <div>
            <StrategicActivityStrip
              latestLifecycle="working"
              items={[{
                id: 1,
                kind: 'command',
                text: 'Inspecting resident session continuity and queued messages',
                inFlight: false,
              }]}
            />
            <StrategicResidentActivityWidget
              state={{ phase: 'idle', label: 'Waiting for direction' }}
              onOpenActivity={() => {}}
            />
          </div>,
        );
      });
    });
  });
});
