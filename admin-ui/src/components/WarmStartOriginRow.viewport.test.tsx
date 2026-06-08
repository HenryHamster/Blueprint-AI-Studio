import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { WarmStartOriginRow } from './WarmStartOriginRow';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

const breakdown = {
  warm_pool: 128,
  backlog_reuse: 64,
  chain_continuity: 96,
  warm_total: 288,
  pool_origin_share: 0.44,
};

describe('WarmStartOriginRow — viewport regression', () => {
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

  async function mountAt() {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<WarmStartOriginRow breakdown={breakdown} />);
    });
  }

  test('renders origin breakdown without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, mountAt);
  });

  test('renders origin breakdown without text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, mountAt);
  });
});
