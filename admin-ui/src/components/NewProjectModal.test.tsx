import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NewProjectModal } from './NewProjectModal';

const mocks = vi.hoisted(() => ({
  onboardProjectMock: vi.fn(async () => ({
    project_id: 'project-12345',
    build_config: {},
    default_branch: 'main',
  })),
}));

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/auto-remediation')) {
      return new Response(JSON.stringify({ config: { enabled: false } }), { status: 200 });
    }
    if (url.includes('/issue-implementation')) {
      return new Response(JSON.stringify({ config: { enabled: false } }), { status: 200 });
    }
    return new Response('{}', { status: 200 });
  }),
  authGet: vi.fn(async () => new Response(JSON.stringify({ repositories: [] }), { status: 200 })),
  onboardProject: mocks.onboardProjectMock,
  API_BASE_URL: 'http://localhost:3000',
}));

describe('NewProjectModal onboarding steering', () => {
  it('builds a structured initial_steering payload from the form fields', async () => {
    const user = userEvent.setup();
    render(
      <NewProjectModal
        onClose={vi.fn()}
        onSuccess={vi.fn()}
      />,
    );

    const repoInput = await screen.findByPlaceholderText('owner/repo or https://github.com/owner/repo');
    await user.type(repoInput, 'acme/platform');
    await user.type(screen.getByPlaceholderText('What do you want Blueprint to achieve on this repo?'), 'Ship the core fix');
    await user.type(screen.getByPlaceholderText('startup / research / product / personal / enterprise'), 'startup');
    await user.type(screen.getByPlaceholderText('Who this is for, in one line'), 'Platform team');
    await user.type(screen.getByPlaceholderText('solo / 3 engineers + 1 PM / etc.'), '4 engineers + 1 PM');
    await user.type(screen.getByPlaceholderText('continuous / weekly / quarterly / sporadic'), 'weekly');
    await user.type(screen.getByPlaceholderText('Risk tolerance, compliance constraints, or other context'), 'Reliability over speed');
    await user.type(screen.getByPlaceholderText("e.g., Don't modify .github/workflows/"), 'No workflow edits');
    await user.click(screen.getByRole('button', { name: 'Add constraint' }));
    await user.click(screen.getByRole('button', { name: 'Continue' }));
    await user.click(await screen.findByRole('button', { name: 'Create Project' }));

    await waitFor(() => expect(mocks.onboardProjectMock).toHaveBeenCalled());
    expect(mocks.onboardProjectMock).toHaveBeenCalledWith(
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
  });
});
