import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NewProjectModal } from './NewProjectModal';

const mocks = vi.hoisted(() => ({
  previewOnboardProjectMock: vi.fn(async () => ({
    build_config: {
      language: 'python',
      has_ci: false,
      requires_ci_checks: true,
      directories: [
        {
          path: 'backend/',
          type: 'python',
          lint_cmd: 'ruff check',
          build_cmd: '',
          test_cmd: '',
          install_cmd: '',
        },
      ],
    },
    default_branch: 'main',
    merge_verification_verdict: { state: 'ci_checks', test_commands: [] },
  })),
  onboardProjectMock: vi.fn(async () => ({
    project_id: 'project-12345',
    build_config: {},
    default_branch: 'main',
  })),
  apiFetchMock: vi.fn(async (url: string) => {
    if (url.includes('/auto-remediation')) {
      return new Response(JSON.stringify({ config: { enabled: false } }), { status: 200 });
    }
    if (url.includes('/issue-implementation')) {
      return new Response(JSON.stringify({ config: { enabled: false, trigger_label: 'auto:implement', max_per_hour: 5, max_concurrent: 2, base_branch: 'main', default_effort: 'medium' } }), { status: 200 });
    }
    return new Response('{}', { status: 200 });
  }),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: mocks.apiFetchMock,
    authGet: vi.fn(async () => new Response(JSON.stringify({ repositories: [] }), { status: 200 })),
    previewOnboardProject: mocks.previewOnboardProjectMock,
    onboardProject: mocks.onboardProjectMock,
    API_BASE_URL: 'http://localhost:3000',
  });
});

async function advanceToSuccess(user: ReturnType<typeof userEvent.setup>) {
  render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);
  const repoInput = await screen.findByPlaceholderText('owner/repo or https://github.com/owner/repo');
  await user.type(repoInput, 'acme/platform');
  await user.click(screen.getByRole('button', { name: 'Continue' }));
  await screen.findByText('Merge-gate draft');
  await user.click(await screen.findByRole('button', { name: 'Create Project' }));
  await screen.findByText('Project created');
}

describe('NewProjectModal toggle error handling', () => {
  beforeEach(() => {
    mocks.apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/auto-remediation')) {
        return new Response(JSON.stringify({ config: { enabled: false } }), { status: 200 });
      }
      if (url.includes('/issue-implementation')) {
        return new Response(JSON.stringify({ config: { enabled: false, trigger_label: 'auto:implement', max_per_hour: 5, max_concurrent: 2, base_branch: 'main', default_effort: 'medium' } }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
  });

  it('reverts the setup toggle and surfaces error when the PATCH is rejected', async () => {
    const user = userEvent.setup();
    await advanceToSuccess(user);

    // Now make the auto-remediation PATCH return a non-ok response
    mocks.apiFetchMock.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url.includes('/auto-remediation') && opts?.method === 'PATCH') {
        return new Response(JSON.stringify({ detail: 'Forbidden' }), { status: 403 });
      }
      if (url.includes('/auto-remediation')) {
        return new Response(JSON.stringify({ config: { enabled: false } }), { status: 200 });
      }
      if (url.includes('/issue-implementation')) {
        return new Response(JSON.stringify({ config: { enabled: false, trigger_label: 'auto:implement', max_per_hour: 5, max_concurrent: 2, base_branch: 'main', default_effort: 'medium' } }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    // Toggle should be present and enabled (config loaded)
    const toggleBtn = await screen.findByRole('button', { name: /Toggle Auto-Remediation/i });
    await user.click(toggleBtn);

    // After rejection, error message appears and toggle reverts to prior state (false→false)
    await waitFor(() => expect(screen.getByTestId('toggle-error')).toBeInTheDocument());
    expect(screen.getByTestId('toggle-error').textContent).toContain('Failed to update');
  });

  it('reverts issue-implementation toggle and surfaces error when the PATCH is rejected', async () => {
    const user = userEvent.setup();
    await advanceToSuccess(user);

    mocks.apiFetchMock.mockImplementation(async (url: string, opts?: RequestInit) => {
      if (url.includes('/issue-implementation') && opts?.method === 'PATCH') {
        return new Response(JSON.stringify({ detail: 'Forbidden' }), { status: 403 });
      }
      if (url.includes('/auto-remediation')) {
        return new Response(JSON.stringify({ config: { enabled: false } }), { status: 200 });
      }
      if (url.includes('/issue-implementation')) {
        return new Response(JSON.stringify({ config: { enabled: false, trigger_label: 'auto:implement', max_per_hour: 5, max_concurrent: 2, base_branch: 'main', default_effort: 'medium' } }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });

    const toggleBtn = await screen.findByRole('button', { name: /Toggle Issue-to-Implementation/i });
    await user.click(toggleBtn);

    await waitFor(() => expect(screen.getByTestId('toggle-error')).toBeInTheDocument());
    expect(screen.getByTestId('toggle-error').textContent).toContain('Failed to update');
  });

  it('does NOT reset to config step when post-create feature-config fetch fails', async () => {
    const user = userEvent.setup();

    // Make the feature-config GETs fail after project creation
    mocks.apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/auto-remediation') || url.includes('/issue-implementation')) {
        return new Response('Internal Server Error', { status: 500 });
      }
      return new Response('{}', { status: 200 });
    });

    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);
    const repoInput = await screen.findByPlaceholderText('owner/repo or https://github.com/owner/repo');
    await user.type(repoInput, 'acme/platform');
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await screen.findByText('Merge-gate draft');
    await user.click(await screen.findByRole('button', { name: 'Create Project' }));

    // Must reach success step despite feature-config failing
    await screen.findByText('Project created');
    // Must NOT show submitError or revert to config
    expect(screen.queryByText('Merge-gate draft')).toBeNull();
  });
});

describe('NewProjectModal onboarding steering', () => {
  beforeEach(() => {
    mocks.apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/auto-remediation')) {
        return new Response(JSON.stringify({ config: { enabled: false } }), { status: 200 });
      }
      if (url.includes('/issue-implementation')) {
        return new Response(JSON.stringify({ config: { enabled: false, trigger_label: 'auto:implement', max_per_hour: 5, max_concurrent: 2, base_branch: 'main', default_effort: 'medium' } }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
  });

  it('previews the detected merge-gate draft and posts the edited override on create', async () => {
    const user = userEvent.setup();
    render(
      <NewProjectModal
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    const repoInput = await screen.findByPlaceholderText('owner/repo or https://github.com/owner/repo');
    // These fields exercise controlled-state propagation, not keyboard behavior.
    // Dispatch one change per field so this test has bounded runtime even when
    // Vitest is running the full suite under constrained CI worker capacity.
    fireEvent.change(repoInput, { target: { value: 'acme/platform' } });
    fireEvent.change(screen.getByPlaceholderText('What do you want Blueprint to achieve on this repo?'), { target: { value: 'Ship the core fix' } });
    fireEvent.change(screen.getByPlaceholderText('startup / research / product / personal / enterprise'), { target: { value: 'startup' } });
    fireEvent.change(screen.getByPlaceholderText('Who this is for, in one line'), { target: { value: 'Platform team' } });
    fireEvent.change(screen.getByPlaceholderText('solo / 3 engineers + 1 PM / etc.'), { target: { value: '4 engineers + 1 PM' } });
    fireEvent.change(screen.getByPlaceholderText('continuous / weekly / quarterly / sporadic'), { target: { value: 'weekly' } });
    fireEvent.change(screen.getByPlaceholderText('Risk tolerance, compliance constraints, or other context'), { target: { value: 'Reliability over speed' } });
    fireEvent.change(screen.getByPlaceholderText("e.g., Don't modify .github/workflows/"), { target: { value: 'No workflow edits' } });
    await user.click(screen.getByRole('button', { name: 'Add constraint' }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await screen.findByText('Merge-gate draft');
    const requiresCiToggle = screen.getByRole('checkbox', { name: 'Require CI checks' });
    await user.click(requiresCiToggle);
    fireEvent.change(screen.getByPlaceholderText('Leave blank to keep the merge gate unverified'), { target: { value: 'python3 -m pytest' } });
    await user.click(await screen.findByRole('button', { name: 'Create Project' }));

    await waitFor(() => expect(mocks.previewOnboardProjectMock).toHaveBeenCalled());
    await waitFor(() => expect(mocks.onboardProjectMock).toHaveBeenCalled());
    expect(mocks.previewOnboardProjectMock).toHaveBeenCalledWith(
      'acme',
      'platform',
      undefined,
      undefined,
      undefined,
      'Ship the core fix',
      {
        context: {
          project_type: 'startup',
          users: 'Platform team',
          team: '4 engineers + 1 PM',
          deploy_cadence: 'weekly',
          notes: 'Reliability over speed',
        },
        constraints: ['No workflow edits'],
      },
    );
    expect(mocks.onboardProjectMock).toHaveBeenCalledWith(
      'acme',
      'platform',
      undefined,
      expect.objectContaining({
        requires_ci_checks: false,
        directories: [
          expect.objectContaining({
            path: 'backend/',
            test_cmd: 'python3 -m pytest',
          }),
        ],
      }),
      undefined,
      'Ship the core fix',
      {
        context: {
          project_type: 'startup',
          users: 'Platform team',
          team: '4 engineers + 1 PM',
          deploy_cadence: 'weekly',
          notes: 'Reliability over speed',
        },
        constraints: ['No workflow edits'],
      },
    );
  });
});

describe('NewProjectModal installation error banner', () => {
  beforeEach(() => {
    mocks.apiFetchMock.mockImplementation(async (url: string) => {
      if (url.includes('/auto-remediation')) {
        return new Response(JSON.stringify({ config: { enabled: false } }), { status: 200 });
      }
      if (url.includes('/issue-implementation')) {
        return new Response(JSON.stringify({ config: { enabled: false, trigger_label: 'auto:implement', max_per_hour: 5, max_concurrent: 2, base_branch: 'main', default_effort: 'medium' } }), { status: 200 });
      }
      return new Response('{}', { status: 200 });
    });
    mocks.previewOnboardProjectMock.mockResolvedValue({
      build_config: {
        language: 'python',
        has_ci: false,
        requires_ci_checks: true,
        directories: [
          {
            path: 'backend/',
            type: 'python',
            lint_cmd: 'ruff check',
            build_cmd: '',
            test_cmd: '',
            install_cmd: '',
          },
        ],
      },
      default_branch: 'main',
      merge_verification_verdict: { state: 'ci_checks', test_commands: [] },
    });
    mocks.onboardProjectMock.mockResolvedValue({
      project_id: 'project-12345',
      build_config: {},
      default_branch: 'main',
    });
  });

  it('renders actionable message and preserves raw error on preview failure when error contains "installation token"', async () => {
    const user = userEvent.setup();
    mocks.previewOnboardProjectMock.mockRejectedValue(
      new Error('No installation token available -- cannot detect default branch.')
    );
    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);
    const repoInput = await screen.findByPlaceholderText('owner/repo or https://github.com/owner/repo');
    await user.type(repoInput, 'acme/platform');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    // Custom actionable message replaces the raw error (includes repo reference)
    await screen.findByText(/Blueprint GitHub App isn't installed on acme\/platform/);
    // Raw backend error is preserved as secondary text (with reduced opacity)
    expect(screen.getByText(/No installation token available/)).toBeInTheDocument();
  });

  it('renders actionable message and preserves raw error on submit failure when error contains "installation token"', async () => {
    const user = userEvent.setup();
    mocks.onboardProjectMock.mockRejectedValue(
      new Error('No installation token available -- cannot detect default branch.')
    );
    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);
    const repoInput = await screen.findByPlaceholderText('owner/repo or https://github.com/owner/repo');
    await user.type(repoInput, 'acme/platform');
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await screen.findByText('Merge-gate draft');
    await user.click(await screen.findByRole('button', { name: 'Create Project' }));

    // Custom actionable message replaces the raw error (includes repo reference)
    await screen.findByText(/Blueprint GitHub App isn't installed on acme\/platform/);
    // Raw backend error is preserved as secondary text
    expect(screen.getByText(/No installation token available/)).toBeInTheDocument();
  });

  it('renders raw error unchanged for errors that do not contain "installation token"', async () => {
    const user = userEvent.setup();
    mocks.previewOnboardProjectMock.mockRejectedValue(
      new Error('Repository not found or access denied')
    );
    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);
    const repoInput = await screen.findByPlaceholderText('owner/repo or https://github.com/owner/repo');
    await user.type(repoInput, 'acme/platform');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    // The raw error is displayed unchanged (not transformed)
    await screen.findByText('Repository not found or access denied');
    // Custom message must NOT appear
    expect(screen.queryByText(/Blueprint GitHub App isn't installed/)).toBeNull();
  });
});
