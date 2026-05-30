import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MuteWindowMenu } from './MuteWindowMenu';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

describe('MuteWindowMenu — viewport regression', () => {
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

  test('inactive, closed — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MuteWindowMenu muteActive={false} muteWindow={null} onSetMuteWindow={() => {}} />);
      });
    });
  });

  test('inactive, open — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MuteWindowMenu muteActive={false} muteWindow={null} onSetMuteWindow={() => {}} />);
      });
      const button = host.querySelector('button');
      if (button) await act(async () => { button.click(); });
    });
  });

  test('active, open — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<MuteWindowMenu muteActive={true} muteWindow={{ expiresAt: 9_999_999_999_999 }} onSetMuteWindow={() => {}} />);
      });
      const button = host.querySelector('button');
      if (button) await act(async () => { button.click(); });
    });
  });
});
