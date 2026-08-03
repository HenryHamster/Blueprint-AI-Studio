import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProjectMembers } from './ProjectMembers';
import type { ProjectMemberEntry } from '../types/project';

const members: ProjectMemberEntry[] = [
  {
    id: 'owner-member-id',
    user_id: 'real-owner-id',
    email: 'owner@example.com',
    role: 'owner',
    status: 'active',
    created_at: null,
  },
  {
    id: 'pending-member-id',
    user_id: null,
    email: 'invited@example.com',
    role: 'member',
    status: 'pending',
    created_at: null,
  },
];

function renderMembers(
  myRole: 'owner' | 'member' | 'viewer',
  canManageMembers: boolean,
  callbacks: {
    onAdd?: (email: string, role: string) => Promise<void>;
    onUpdateRole?: (memberId: string, role: string) => Promise<void>;
    onRemove?: (memberId: string) => Promise<void>;
  } = {},
) {
  return render(
    <ProjectMembers
      projectId="project-1"
      members={members}
      myRole={myRole}
      canManageMembers={canManageMembers}
      onAdd={callbacks.onAdd ?? vi.fn(async () => {})}
      onUpdateRole={callbacks.onUpdateRole ?? vi.fn(async () => {})}
      onRemove={callbacks.onRemove ?? vi.fn(async () => {})}
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('ProjectMembers access matrix', () => {
  it.each([
    ['member', false],
    ['viewer', false],
    ['owner', false],
  ] as const)('hides mutation controls for %s with canManageMembers=%s', (role, canManageMembers) => {
    renderMembers(role, canManageMembers);

    expect(screen.queryByRole('button', { name: 'Invite' })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Cancel invitation/)).not.toBeInTheDocument();
    expect(screen.queryByRole('combobox')).not.toBeInTheDocument();
  });

  it('shows owner controls only when the explicit access prop permits them', () => {
    renderMembers('owner', true);

    expect(screen.getByRole('button', { name: 'Invite' })).toBeInTheDocument();
    expect(screen.getByLabelText('Cancel invitation for invited@example.com')).toBeInTheDocument();
    expect(screen.getAllByRole('combobox')).toHaveLength(2);
    expect(screen.getAllByRole('combobox')[0]).toHaveValue('viewer');
    expect(screen.getByText(/Viewer is read-only; Member can run tasks/)).toBeInTheDocument();
    expect(screen.getByText(/Blueprint does not send an email/)).toBeInTheDocument();
  });
});

describe('ProjectMembers invitation behavior', () => {
  it('renders additive pending/active state without exposing a pending user id', () => {
    renderMembers('member', false);

    expect(screen.getByText('Pending sign-in')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('invited@example.com')).toBeInTheDocument();
    expect(screen.queryByText('pending-member-id')).not.toBeInTheDocument();
    expect(screen.queryByText('real-owner-id')).not.toBeInTheDocument();
  });

  it('invites by normalized form values through the existing add callback', async () => {
    const onAdd = vi.fn(async () => {});
    renderMembers('owner', true, { onAdd });

    fireEvent.change(screen.getByPlaceholderText('user@example.com'), { target: { value: '  next@example.com  ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Invite' }));

    await waitFor(() => expect(onAdd).toHaveBeenCalledWith('next@example.com', 'viewer'));
  });

  it('PATCHes and DELETEs a pending invitation by its stable member id', async () => {
    const user = userEvent.setup();
    const onUpdateRole = vi.fn(async () => {});
    const onRemove = vi.fn(async () => {});
    vi.stubGlobal('confirm', vi.fn(() => true));
    renderMembers('owner', true, { onUpdateRole, onRemove });

    await user.selectOptions(screen.getAllByRole('combobox')[1], 'viewer');
    await waitFor(() => expect(onUpdateRole).toHaveBeenCalledWith('pending-member-id', 'viewer'));

    await user.click(screen.getByLabelText('Cancel invitation for invited@example.com'));
    await waitFor(() => expect(onRemove).toHaveBeenCalledWith('pending-member-id'));
  });
});
