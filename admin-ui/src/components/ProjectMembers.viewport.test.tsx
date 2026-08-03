import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectMembers } from './ProjectMembers';
import { expectFullViewportAt } from '../test/viewport';
import type { ProjectMemberEntry } from '../types/project';

const noop = async () => {};

const sampleMembers: ProjectMemberEntry[] = [
  { id: 'm1', user_id: 'user1', email: 'alice@example.com', role: 'member', created_at: null },
  {
    id: 'm2',
    user_id: 'user2',
    email: 'bob.with-a-very-long-email-address-that-could-overflow@example-organization.com',
    role: 'viewer',
    created_at: null,
  },
  { id: 'm3', user_id: 'user3', email: 'charlie@example.com', role: 'member', created_at: null },
];

describe('ProjectMembers — viewport regression', () => {
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

  test('owner view with invite form and empty member list renders without overflow at 375px', async () => {
    await expectFullViewportAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectMembers
            projectId="proj-1"
            members={[]}
            myRole="owner"
            canManageMembers
            onAdd={noop}
            onUpdateRole={noop}
            onRemove={noop}
          />,
        );
      });
    }, { touchSelector: 'button, select' });
  });

  test('owner view with invite form and multiple members with long emails renders without overflow at 375px', async () => {
    await expectFullViewportAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectMembers
            projectId="proj-1"
            members={sampleMembers}
            myRole="owner"
            canManageMembers
            onAdd={noop}
            onUpdateRole={noop}
            onRemove={noop}
          />,
        );
      });
    }, { touchSelector: 'button, select' });
  });

  test('member view (no invite form) with members list renders without overflow at 375px', async () => {
    await expectFullViewportAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectMembers
            projectId="proj-1"
            members={sampleMembers}
            myRole="member"
            canManageMembers={false}
            onAdd={noop}
            onUpdateRole={noop}
            onRemove={noop}
          />,
        );
      });
    });
  });
});
