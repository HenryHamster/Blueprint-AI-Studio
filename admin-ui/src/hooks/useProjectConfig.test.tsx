import { act, useEffect } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, describe, expect, test, vi } from 'vitest';
import { useProjectConfig } from './useProjectConfig';
import type { Project } from '../types/project';

const mocks = vi.hoisted(() => ({
  authGet: vi.fn(),
  fetchProjectResourceEnvelope: vi.fn(),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authGet: mocks.authGet,
    fetchProjectResourceEnvelope: mocks.fetchProjectResourceEnvelope,
  });
});

let captured: ReturnType<typeof useProjectConfig> | null = null;

function Harness({ project }: { project: Project }) {
  const result = useProjectConfig(
    vi.fn(),
    project.id,
    [project],
    { current: null },
    { current: null },
  );
  useEffect(() => {
    captured = result;
  }, [result]);
  return null;
}

describe('useProjectConfig access readiness', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
    captured = null;
    vi.clearAllMocks();
  });

  test.each(['viewer', undefined] as const)('skipped %s project config reads are ready, not loading', role => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    const project = {
      id: 'project-1',
      github_owner: 'owner',
      github_repo: 'repo',
      my_role: role,
    } as Project;

    act(() => root?.render(<Harness project={project} />));

    expect(captured?.taskBranchDefaultsReady).toBe(true);
    expect(captured?.taskBranchDefaultsLoading).toBe(false);
    expect(captured?.taskBranchDefaultsError).toBeNull();
    expect(mocks.authGet).not.toHaveBeenCalled();
    expect(mocks.fetchProjectResourceEnvelope).not.toHaveBeenCalled();
  });
});
