import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AdversarialQualityPanel } from './AdversarialQualityPanel';
import { expectNoOverflowAt } from '../test/viewport';

describe('AdversarialQualityPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<AdversarialQualityPanel projectId={null} />);
      });
    });
  });
});
