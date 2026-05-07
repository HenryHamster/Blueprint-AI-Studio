import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TaskLaunchFAB } from './TaskLaunchFAB';
import { expectNoOverflowAt } from '../test/viewport';

describe('TaskLaunchFAB — mobile viewport', () => {
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

  test('FAB renders without overflow at 375px mobile viewport', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<TaskLaunchFAB onClick={() => {}} visible={true} />);
      });
    });
  });

  test('FAB has sufficient touch target size (minimum 56x56)', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    await act(async () => {
      root?.render(<TaskLaunchFAB onClick={() => {}} visible={true} />);
    });

    const fab = document.querySelector('button[title="Launch new task"]') as HTMLButtonElement;
    expect(fab).toBeTruthy();

    const rect = fab.getBoundingClientRect();
    expect(rect.width).toBeGreaterThanOrEqual(56);
    expect(rect.height).toBeGreaterThanOrEqual(56);
  });

  test('FAB is hidden when visible prop is false', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    await act(async () => {
      root?.render(<TaskLaunchFAB onClick={() => {}} visible={false} />);
    });

    const fab = document.querySelector('button[title="Launch new task"]') as HTMLButtonElement | null;
    expect(fab).toBeNull();
  });
});
