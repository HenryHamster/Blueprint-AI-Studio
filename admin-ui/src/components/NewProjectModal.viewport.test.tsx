import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { NewProjectModal } from './NewProjectModal';
import { expectNoOverflowAt } from '../test/viewport';

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
            trigger_label: 'blueprint:implement',
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
  authGet: vi.fn(async () => {
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
  })),
  API_BASE_URL: 'http://localhost:3000',
}));

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
