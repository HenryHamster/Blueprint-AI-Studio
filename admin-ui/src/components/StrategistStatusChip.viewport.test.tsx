import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategistStatusChip } from './StrategistStatusChip';
import { expectNoOverflowAt } from '../test/viewport';
import type { StrategistStatus } from '../hooks/useStrategistStatus';

const idleStatus: StrategistStatus = {
  phase: 'idle',
  label: 'Strategist idle',
  detail: null,
  activityKind: null,
  source: 'idle',
};

const busyStatus: StrategistStatus = {
  phase: 'busy',
  label: 'Strategist: Planning next cycle…',
  detail: 'Reviewing 12 open directions',
  activityKind: 'autonomous',
  source: 'cycle_progress',
};

describe('StrategistStatusChip — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('idle chip renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '8px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<StrategistStatusChip status={idleStatus} />);
      });
    });
  });

  test('busy chip with long label renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '8px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<StrategistStatusChip status={busyStatus} />);
      });
    });
  });
});
