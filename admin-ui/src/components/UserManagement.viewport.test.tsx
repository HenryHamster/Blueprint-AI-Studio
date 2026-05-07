import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { UserManagement } from './UserManagement';
import { expectNoOverflowAt } from '../test/viewport';

// UserManagement is pure prop-driven; no '../api' import to mock.
const sampleUsers = [
  {
    id: 'u1',
    email: 'alice@example.com',
    role: 'admin',
    display_name: 'Alice (Project Admin)',
    last_login_at: '2026-05-06T10:00:00Z',
    created_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'u2',
    email: 'bob.with-a-very-long-email-address-that-could-overflow@example-organization.com',
    role: 'user',
    display_name: null,
    last_login_at: null,
    created_at: '2026-02-15T00:00:00Z',
  },
];

const noop = async () => {};

describe('UserManagement — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('empty user list renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <UserManagement
            users={[]}
            currentUserEmail="alice@example.com"
            onAdd={noop}
            onUpdateRole={noop}
            onRemove={noop}
            onClose={() => {}}
          />,
        );
      });
    });
  });

  test('populated users + long email renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <UserManagement
            users={sampleUsers}
            currentUserEmail="alice@example.com"
            onAdd={noop}
            onUpdateRole={noop}
            onRemove={noop}
            onClose={() => {}}
          />,
        );
      });
    });
  });
});
