import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { WelcomeScreen } from './WelcomeScreen';
import { expectNoOverflowAt } from '../test/viewport';

describe('WelcomeScreen — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('welcome screen with no projects does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<WelcomeScreen onCreateProject={() => {}} onSignOut={() => {}} />);
      });
    });
  });
});
