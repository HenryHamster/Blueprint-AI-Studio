import { act, renderHook, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useProjectMembers } from './useProjectMembers';

const mocks = vi.hoisted(() => ({
  authGet: vi.fn(),
  apiFetch: vi.fn(),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    API_BASE_URL: 'http://localhost:3000',
    authGet: mocks.authGet,
    apiFetch: mocks.apiFetch,
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function membersResponse(id: string, email: string) {
  return new Response(JSON.stringify({
    members: [{ id, user_id: null, email, role: 'member', status: 'pending', created_at: null }],
  }), { status: 200 });
}

describe('useProjectMembers project fencing', () => {
  beforeEach(() => {
    mocks.authGet.mockReset();
    mocks.apiFetch.mockReset();
  });

  it('clears on scope change and rejects a late A response after B becomes active', async () => {
    const a = deferred<Response>();
    const b = deferred<Response>();
    mocks.authGet.mockImplementation((url: string) => url.includes('/project-a/') ? a.promise : b.promise);
    const { result } = renderHook(() => useProjectMembers());

    let fetchA!: Promise<void>;
    let fetchB!: Promise<void>;
    act(() => { fetchA = result.current.fetchProjectMembers('project-a'); });
    act(() => { fetchB = result.current.fetchProjectMembers('project-b'); });

    expect(result.current.projectMembersProjectId).toBe('project-b');
    expect(result.current.projectMembers).toEqual([]);

    b.resolve(membersResponse('member-b', 'b@example.com'));
    await act(async () => { await fetchB; });
    expect(result.current.projectMembers.map(member => member.id)).toEqual(['member-b']);

    a.resolve(membersResponse('member-a', 'a@example.com'));
    await act(async () => { await fetchA; });
    expect(result.current.projectMembersProjectId).toBe('project-b');
    expect(result.current.projectMembers.map(member => member.id)).toEqual(['member-b']);
  });

  it('does not let a completed A mutation refetch A or replace B after scope changes', async () => {
    const mutation = deferred<Response>();
    mocks.apiFetch.mockReturnValue(mutation.promise);
    mocks.authGet.mockResolvedValue(membersResponse('member-b', 'b@example.com'));
    const { result } = renderHook(() => useProjectMembers());

    act(() => { result.current.activateProjectMembers('project-a'); });
    let updateA!: Promise<void>;
    act(() => { updateA = result.current.updateProjectMemberRole('project-a', 'member-a', 'viewer'); });
    await waitFor(() => expect(mocks.apiFetch).toHaveBeenCalled());

    await act(async () => { await result.current.fetchProjectMembers('project-b'); });
    mutation.resolve(new Response('{}', { status: 200 }));
    await act(async () => { await updateA; });

    expect(mocks.authGet).toHaveBeenCalledTimes(1);
    expect(mocks.authGet).toHaveBeenCalledWith('http://localhost:3000/api/admin/projects/project-b/members?limit=500');
    expect(result.current.projectMembersProjectId).toBe('project-b');
    expect(result.current.projectMembers.map(member => member.id)).toEqual(['member-b']);
  });

  it('invalidates loaded rows when the panel scope is cleared', async () => {
    mocks.authGet.mockResolvedValue(membersResponse('member-a', 'a@example.com'));
    const { result } = renderHook(() => useProjectMembers());
    await act(async () => { await result.current.fetchProjectMembers('project-a'); });
    expect(result.current.projectMembers).toHaveLength(1);

    act(() => { result.current.activateProjectMembers(null); });
    expect(result.current.projectMembersProjectId).toBeNull();
    expect(result.current.projectMembers).toEqual([]);
  });
});
