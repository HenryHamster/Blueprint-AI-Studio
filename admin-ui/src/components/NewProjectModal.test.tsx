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
  authGetMock: vi.fn(async (url: string) => {
    if (url.includes('/api/github/status')) {
      return new Response(JSON.stringify({
        identity_linked: true,
        credential_kind: 'oauth',
        account_name: 'octocat',
        app_configured: true,
        app_installation_linked: true,
        app_installation_reachable: true,
        installation_count: 1,
      }), { status: 200 });
    }
    return new Response(JSON.stringify({ repositories: [] }), { status: 200 });
  }),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: mocks.apiFetchMock,
    authGet: mocks.authGetMock,
    previewOnboardProject: mocks.previewOnboardProjectMock,
    onboardProject: mocks.onboardProjectMock,
    API_BASE_URL: 'http://localhost:3000',
  });
});

beforeEach(() => {
  window.sessionStorage.clear();
  window.history.replaceState({}, '', '/');
  mocks.previewOnboardProjectMock.mockClear();
  mocks.onboardProjectMock.mockClear();
  mocks.apiFetchMock.mockClear();
  mocks.authGetMock.mockClear();
  mocks.authGetMock.mockImplementation(async (url: string) => {
    if (url.includes('/api/github/status')) {
      return new Response(JSON.stringify({
        identity_linked: true,
        credential_kind: 'oauth',
        account_name: 'octocat',
        app_configured: true,
        app_installation_linked: true,
        app_installation_reachable: true,
        installation_count: 1,
      }), { status: 200 });
    }
    return new Response(JSON.stringify({ repositories: [] }), { status: 200 });
  });
});

describe('NewProjectModal GitHub authorization', () => {
  it('shows manual repository entry but blocks preview until GitHub identity is linked', async () => {
    mocks.authGetMock.mockImplementation(async (url: string) => {
      if (url.includes('/api/github/status')) {
        return new Response(JSON.stringify({
          identity_linked: false,
          credential_kind: null,
          account_name: null,
          app_configured: true,
          app_installation_linked: false,
          app_installation_reachable: false,
          installation_count: 0,
        }), { status: 200 });
      }
      return new Response(JSON.stringify({ repositories: [] }), { status: 200 });
    });

    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);

    expect(await screen.findByRole('button', { name: 'Connect GitHub' })).toBeInTheDocument();
    const repoInput = screen.getByPlaceholderText('owner/repo or https://github.com/owner/repo');
    expect(repoInput).toBeInTheDocument();
    expect(screen.getByText(/personally have push or admin access/)).toBeInTheDocument();
    expect(screen.getByText(/GitHub App must be installed with access/)).toBeInTheDocument();
    expect(mocks.authGetMock).toHaveBeenCalledWith('http://localhost:3000/api/github/status');
    expect(mocks.authGetMock).not.toHaveBeenCalledWith('http://localhost:3000/api/admin/github/repos');

    fireEvent.change(repoInput, { target: { value: 'acme/platform' } });
    expect(screen.getByRole('button', { name: 'Continue' })).toBeDisabled();
    fireEvent.keyDown(repoInput, { key: 'Enter' });
    expect(await screen.findByText('Connect your GitHub account before previewing this repository.')).toBeInTheDocument();
    expect(mocks.previewOnboardProjectMock).not.toHaveBeenCalled();
  });

  it('renders the connected account from user-scoped status', async () => {
    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);

    expect(await screen.findByText('Connected as octocat')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Connect GitHub' })).not.toBeInTheDocument();
    await waitFor(() => expect(mocks.authGetMock).toHaveBeenCalledWith('http://localhost:3000/api/admin/github/repos'));
  });

  it('restores the in-progress form after the GitHub OAuth return', async () => {
    window.history.replaceState({}, '', '/?github_connected=octocat');
    window.sessionStorage.setItem('blueprint:new-project:github-oauth-resume:v1', JSON.stringify({
      version: 1,
      repoInput: 'acme/platform',
      projectName: 'Platform',
      goal: 'Ship safely',
      projectType: 'product',
      users: 'internal teams',
      team: 'three engineers',
      deployCadence: 'weekly',
      notes: 'No Friday deploys',
      constraints: ['Do not edit production secrets'],
      promotionTargetInput: 'main',
      useManualRepo: true,
      repoFilter: '',
      previewDefaultBranch: 'main',
    }));

    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} resumeFromGitHub />);

    expect(await screen.findByText('Connected as octocat')).toBeInTheDocument();
    expect(screen.getByDisplayValue('acme/platform')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Platform')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Ship safely')).toBeInTheDocument();
    expect(screen.getByDisplayValue('Do not edit production secrets')).toBeInTheDocument();
    expect(window.sessionStorage.getItem('blueprint:new-project:github-oauth-resume:v1')).toBeNull();
  });

  it('starts current-user OAuth from the existing authorize endpoint and keeps useful errors', async () => {
    mocks.authGetMock.mockImplementation(async (url: string) => {
      if (url.includes('/api/github/status')) {
        return new Response(JSON.stringify({ identity_linked: false, app_configured: true }), { status: 200 });
      }
      if (url.includes('/api/github/oauth/authorize')) {
        return new Response(JSON.stringify({ error: { message: 'GitHub OAuth is not configured' } }), { status: 503 });
      }
      return new Response(JSON.stringify({ repositories: [] }), { status: 200 });
    });

    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Connect GitHub' }));

    expect(await screen.findByText('GitHub OAuth is not configured')).toBeInTheDocument();
    expect(mocks.authGetMock).toHaveBeenCalledWith(
      'http://localhost:3000/api/github/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A3000',
    );
  });

  it('offers to resume App installation for a linked identity and surfaces authorize errors', async () => {
    mocks.authGetMock.mockImplementation(async (url: string) => {
      if (url.includes('/api/github/status')) {
        return new Response(JSON.stringify({
          identity_linked: true,
          credential_kind: 'oauth',
          account_name: 'octocat',
          app_configured: true,
          app_installation_linked: false,
          app_installation_reachable: false,
          installation_count: 0,
        }), { status: 200 });
      }
      if (url.includes('/api/github/oauth/authorize')) {
        return new Response(JSON.stringify({ error: { message: 'Could not resume GitHub App installation' } }), { status: 503 });
      }
      return new Response(JSON.stringify({ repositories: [] }), { status: 200 });
    });

    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);
    fireEvent.click(await screen.findByRole('button', { name: 'Install Blueprint App' }));

    expect(await screen.findByText('Could not resume GitHub App installation')).toBeInTheDocument();
    expect(mocks.authGetMock).toHaveBeenCalledWith(
      'http://localhost:3000/api/github/oauth/authorize?redirect_uri=http%3A%2F%2Flocalhost%3A3000',
    );
  });

  it('shows administrator guidance instead of an installation warning when the App is unconfigured', async () => {
    mocks.authGetMock.mockImplementation(async (url: string) => {
      if (url.includes('/api/github/status')) {
        return new Response(JSON.stringify({
          identity_linked: true,
          credential_kind: 'oauth',
          account_name: 'octocat',
          app_configured: false,
          app_installation_linked: false,
          app_installation_reachable: false,
          installation_count: 0,
        }), { status: 200 });
      }
      return new Response(JSON.stringify({ repositories: [] }), { status: 200 });
    });

    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);

    expect(await screen.findByText(/GitHub App is not configured on this server/)).toBeInTheDocument();
    expect(screen.queryByText(/has no repository installation/)).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Install Blueprint App' })).not.toBeInTheDocument();
  });

  it('keeps existing-project setup mode independent of GitHub connection and repository discovery', async () => {
    render(
      <NewProjectModal
        onClose={vi.fn()}
        onSuccess={vi.fn()}
        setupProject={{ id: 'existing-project', owner: 'acme', repo: 'platform' }}
      />,
    );

    expect(await screen.findByText('Project Setup')).toBeInTheDocument();
    expect(screen.getByText('Project created')).toBeInTheDocument();
    expect(mocks.authGetMock).not.toHaveBeenCalled();
  });

  it('preserves Blueprint repo-discovery errors while falling back to manual entry', async () => {
    mocks.authGetMock.mockImplementation(async (url: string) => {
      if (url.includes('/api/github/status')) {
        return new Response(JSON.stringify({ identity_linked: true, credential_kind: 'oauth', app_configured: true }), { status: 200 });
      }
      return new Response(JSON.stringify({
        error: { message: 'Blueprint GitHub App is not installed for this GitHub identity.' },
      }), { status: 422 });
    });

    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);

    expect(await screen.findByText('Blueprint GitHub App is not installed for this GitHub identity.')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('owner/repo or https://github.com/owner/repo')).toBeInTheDocument();
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

  it('renders actionable message and preserves the current App-install error on preview failure', async () => {
    const user = userEvent.setup();
    mocks.previewOnboardProjectMock.mockRejectedValue(
      new Error('Your GitHub authorization is valid, but the Blueprint GitHub App is not installed for this repository.')
    );
    render(<NewProjectModal onClose={vi.fn()} onSuccess={vi.fn()} />);
    const repoInput = await screen.findByPlaceholderText('owner/repo or https://github.com/owner/repo');
    await user.type(repoInput, 'acme/platform');
    await user.click(screen.getByRole('button', { name: 'Continue' }));

    // Custom actionable message replaces the raw error (includes repo reference)
    await screen.findByText(/Blueprint GitHub App isn't installed on acme\/platform/);
    // Raw backend error is preserved as secondary text (with reduced opacity)
    expect(screen.getByText(/Your GitHub authorization is valid/)).toBeInTheDocument();
  });

  it('renders actionable message and preserves the current App-install error on submit failure', async () => {
    const user = userEvent.setup();
    mocks.onboardProjectMock.mockRejectedValue(
      new Error('Your GitHub authorization is valid, but the Blueprint GitHub App is not installed for this repository.')
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
    expect(screen.getByText(/Your GitHub authorization is valid/)).toBeInTheDocument();
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
