import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AccountMenu } from './AccountMenu';
import type { StoredAccount } from '../accountManager';
import { expectFullViewportAt } from '../test/viewport';

const accounts: StoredAccount[] = [
  { id: 'a', email: 'operator-with-a-fairly-long-address@example.com', displayName: 'Operator A', uid: 'uid-a' },
  { id: 'b', email: 'second-operator-account@example.com', displayName: 'Operator B', uid: 'uid-b' },
];

describe('AccountMenu — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('mobile account menu renders cleanly at 375px', async () => {
    await expectFullViewportAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <AccountMenu
            variant="mobile"
            accounts={accounts}
            activeAccountId="a"
            currentEmail={accounts[0].email ?? ''}
            onAddAccount={() => {}}
            onSwitchAccount={() => {}}
            onSignOut={() => {}}
            onSignOutAll={() => {}}
          />,
        );
      });
    });
  });
});
