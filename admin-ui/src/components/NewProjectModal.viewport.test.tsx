import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import userEvent from '@testing-library/user-event';
import { NewProjectModal } from './NewProjectModal';
import { onboardProject } from '../api';
import type { MergeVerificationVerdict } from '../types/project';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const onboardProjectMock = vi.mocked(onboardProject);

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/auto-remediation')) {
      return new Response(
        JSON.stringify({
          config: {
            enabled: true,
            trigger_label: 'blueprint:fix',
            max_per_hour: 5,
            max_concurrent: 2,
            base_branch: 'main',
            default_effort: 'medium',
          },
        }),
        { status: 200 },
      );
    }
    if (url.includes('/issue-implementation')) {
      return new Response(
        JSON.stringify({
          config: {
            enabled: true,
            trigger_label: 'auto:implement',
            max_per_hour: 10,
            max_concurrent: 3,
            base_branch: 'main',
            default_effort: 'medium',
          },
        }),
        { status: 200 },
      );
    }
    return new Response('{}', { status: 200 });
  }),
  authGet: vi.fn(async (url: string) => {
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
    return new Response(
      JSON.stringify({
        repositories: [
          { full_name: 'example/repo-one', default_branch: 'main' },
          { full_name: 'example/repo-two', default_branch: 'develop' },
          { full_name: 'example/very-long-repository-name-with-many-characters', default_branch: 'master' },
        ],
      }),
      { status: 200 },
    );
  }),
  previewOnboardProject: vi.fn(async () => ({
    build_config: {
      language: 'typescript',
      has_ci: false,
      requires_ci_checks: true,
      directories: [
        {
          path: '.',
          type: 'node',
          install_cmd: 'npm install',
          build_cmd: 'npm run build',
          test_cmd: '',
          lint_cmd: 'npm run lint',
        },
      ],
    },
    default_branch: 'main',
    merge_verification_verdict: { state: 'ci_checks', test_commands: [] },
  })),
  onboardProject: vi.fn(async () => ({
    project_id: 'project-12345',
    build_config: {
      language: 'typescript',
      directories: [
        {
          path: '.',
          install_cmd: 'npm install',
          build_cmd: 'npm run build',
          test_cmd: 'npm test',
          lint_cmd: 'npm run lint',
        },
      ],
    },
    default_branch: 'main',
    merge_verification_verdict: { state: 'tests_gated', test_commands: [{ path: '.', test_cmd: 'npm test' }] },
  })),
  API_BASE_URL: 'http://localhost:3000',
}));

/**
 * Drive the wizard from the repo-list step through to the success step, where the
 * merge-verification banner renders. Mirrors a real operator: pick a repo, confirm,
 * create. The onboardProject mock resolves with whatever verdict the test set.
 */
async function driveToSuccess(host: HTMLElement): Promise<void> {
  const user = userEvent.setup();
  // Identity status now resolves before repository discovery, so wait for both
  // requests rather than assuming the old single-fetch timing.
  let repoBtn: HTMLButtonElement | undefined;
  for (let attempt = 0; attempt < 10 && !repoBtn; attempt += 1) {
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    repoBtn = Array.from(host.querySelectorAll('button')).find((button) =>
      button.textContent?.includes('example/repo-one'),
    );
  }
  expect(repoBtn).toBeDefined();
  await act(async () => { await user.click(repoBtn!); });
  const continueBtn = Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.trim() === 'Continue');
  await act(async () => { if (continueBtn) await user.click(continueBtn); });
  await act(async () => { await new Promise((resolve) => setTimeout(resolve, 120)); });
  const createBtn = Array.from(host.querySelectorAll('button')).find((b) => b.textContent?.includes('Create Project'));
  // handleCreate awaits onboardProject + feature-config fetches before settling.
  await act(async () => {
    if (createBtn) await user.click(createBtn);
    await new Promise((resolve) => setTimeout(resolve, 120));
  });
}

describe('NewProjectModal — viewport regression', () => {
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

  test('initial mode (repo selection) renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewProjectModal
            onClose={() => {}}
            onSuccess={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
    });
  });

  test('input focus does not trigger zoom at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewProjectModal
            onClose={() => {}}
            onSuccess={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
      // Find an input and focus it to simulate mobile browser behavior
      const input = document.querySelector('input[type="text"]') as HTMLInputElement;
      if (input) {
        await act(async () => {
          input.focus();
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        });
      }
    });
  });

  test('primary action buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewProjectModal
            onClose={() => {}}
            onSuccess={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
    });
  });

  test('labels and repo names are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewProjectModal
            onClose={() => {}}
            onSuccess={() => {}}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
    });
  });

  test('success step shows unverified merge-verdict warning without overflow at 375px', async () => {
    const verdict: MergeVerificationVerdict = { state: 'unverified', test_commands: [] };
    onboardProjectMock.mockResolvedValueOnce({
      project_id: 'project-12345',
      build_config: { language: 'typescript', directories: [] },
      default_branch: 'main',
      merge_verification_verdict: verdict,
    });
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<NewProjectModal onClose={() => {}} onSuccess={() => {}} />);
      });
      await driveToSuccess(host);
    });
    const banner = host!.querySelector('[data-testid="merge-verification-banner"]');
    expect(banner?.getAttribute('data-verdict-state')).toBe('unverified');
    expect(banner?.textContent).toContain('UNVERIFIED');
  });

  test('success step shows tests_gated merge-verdict with commands without overflow at 375px', async () => {
    const verdict: MergeVerificationVerdict = {
      state: 'tests_gated',
      test_commands: [
        { path: 'backend/', test_cmd: 'python3 -m pytest backend/testing --maxfail=1 --disable-warnings -q' },
      ],
    };
    onboardProjectMock.mockResolvedValueOnce({
      project_id: 'project-12345',
      build_config: { language: 'python', directories: [{ path: 'backend/', type: 'python', test_cmd: 'pytest' }] },
      default_branch: 'main',
      merge_verification_verdict: verdict,
    });
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<NewProjectModal onClose={() => {}} onSuccess={() => {}} />);
      });
      await driveToSuccess(host);
    });
    const banner = host!.querySelector('[data-testid="merge-verification-banner"]');
    expect(banner?.getAttribute('data-verdict-state')).toBe('tests_gated');
    expect(banner?.textContent).toContain('python3 -m pytest');
  });

  test('setup-guide mode renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <NewProjectModal
            onClose={() => {}}
            onSuccess={() => {}}
            setupProject={{
              id: 'project-12345',
              owner: 'example',
              repo: 'very-long-repository-name-with-many-characters-for-testing',
            }}
          />,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
    });
  });
});
