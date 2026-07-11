import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EvidenceTierBadge } from './EvidenceTierBadge';
import { expectNoOverflowAt } from '../test/viewport';

describe('EvidenceTierBadge — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  const render = async (tier: string) => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(<EvidenceTierBadge tier={tier} />);
    });
  };

  test('covered badge does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, () => render('covered'));
  });

  test('attested badge does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, () => render('attested'));
  });

  test('uncovered badge does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, () => render('uncovered'));
  });
});
