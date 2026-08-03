import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProjectBuildConfigSection } from './ProjectBuildConfigSection';
import type { BuildConfig } from '../types/project';

const mocks = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  getBlockDegradedMock: vi.fn(async () => ({ block_degraded_unverified_merge: false })),
  getBlockNoTestMock: vi.fn(async () => ({ block_no_test_unverified_merge: false })),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: mocks.apiFetchMock,
    getBlockDegradedUnverifiedMerge: mocks.getBlockDegradedMock,
    setBlockDegradedUnverifiedMerge: vi.fn(async () => ({ block_degraded_unverified_merge: false })),
    getBlockNoTestUnverifiedMerge: mocks.getBlockNoTestMock,
    setBlockNoTestUnverifiedMerge: vi.fn(async () => ({ block_no_test_unverified_merge: false })),
  });
});

const buildConfig: BuildConfig = {
  language: 'TypeScript',
  directories: [
    { path: 'backend/', type: 'python', lint_cmd: 'ruff check .', test_cmd: 'pytest', build_cmd: '' },
  ],
  git_user_email: 'blueprint-agent@automated.dev',
  git_user_name: 'Blueprint Agent',
};

describe('ProjectBuildConfigSection — staging prefix error handling', () => {
  beforeEach(() => {
    mocks.apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/staging-branch-prefixes')) {
        return new Response(
          JSON.stringify({ staging_branch_prefixes: ['research/', 'plan/'], is_default: true }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify({ items: [] }), { status: 200 });
    });
  });

  it('surfaces error and does not corrupt state when save PATCH returns non-ok', async () => {
    const user = userEvent.setup();
    render(
      <ProjectBuildConfigSection
        githubOwner="myorg"
        githubRepo="myrepo"
        buildConfig={buildConfig}
        onSave={vi.fn(async () => {})}
        onDetect={vi.fn(async () => {})}
        detectAvailable={true}
        projectId="test-project"
        canSave={true}
      />,
    );

    // Wait for staging prefixes to load
    await waitFor(() => expect(mocks.apiFetchMock).toHaveBeenCalledWith(
      expect.stringContaining('/staging-branch-prefixes'),
    ));

    // Make the PATCH return a 403
    mocks.apiFetchMock.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url.includes('/staging-branch-prefixes') && opts?.method === 'PATCH') {
        return new Response(JSON.stringify({ detail: 'Forbidden' }), { status: 403 });
      }
      if (url.includes('/staging-branch-prefixes')) {
        return new Response(
          JSON.stringify({ staging_branch_prefixes: ['research/', 'plan/'], is_default: true }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify({ items: [] }), { status: 200 });
    });

    const saveBtn = screen.getByRole('button', { name: 'Save Staging Prefixes' });
    await user.click(saveBtn);

    await waitFor(() => expect(screen.getByTestId('staging-prefixes-error')).toBeInTheDocument());
    expect(screen.getByTestId('staging-prefixes-error').textContent).toContain('Failed to save');
  });

  it('surfaces error and does not corrupt state when revert PATCH returns non-ok', async () => {
    const user = userEvent.setup();

    // Serve a non-default state so "Revert to default" button appears
    mocks.apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/staging-branch-prefixes')) {
        return new Response(
          JSON.stringify({ staging_branch_prefixes: ['custom/'], is_default: false }),
          { status: 200 },
        );
      }
      return new Response(JSON.stringify({ items: [] }), { status: 200 });
    });

    render(
      <ProjectBuildConfigSection
        githubOwner="myorg"
        githubRepo="myrepo"
        buildConfig={buildConfig}
        onSave={vi.fn(async () => {})}
        onDetect={vi.fn(async () => {})}
        detectAvailable={true}
        projectId="test-project"
        canSave={true}
      />,
    );

    const revertBtn = await screen.findByRole('button', { name: 'Revert to default' });

    // Make the revert PATCH return 403
    mocks.apiFetchMock.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url.includes('/staging-branch-prefixes') && opts?.method === 'PATCH') {
        return new Response(JSON.stringify({ detail: 'Forbidden' }), { status: 403 });
      }
      return new Response(JSON.stringify({ items: [] }), { status: 200 });
    });

    await user.click(revertBtn);

    await waitFor(() => expect(screen.getByTestId('staging-prefixes-error')).toBeInTheDocument());
    expect(screen.getByTestId('staging-prefixes-error').textContent).toContain('Failed to revert');
  });
});
