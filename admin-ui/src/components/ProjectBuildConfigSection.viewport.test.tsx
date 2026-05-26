import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';
import { ProjectBuildConfigSection } from './ProjectBuildConfigSection';
import type { BuildConfig } from '../types/project';

const buildConfig: BuildConfig = {
  language: 'TypeScript',
  directories: [
    { path: 'admin-ui/', type: 'node', lint_cmd: 'npx eslint .', test_cmd: 'npm test', build_cmd: 'npm run build' },
    { path: 'backend/', type: 'python', lint_cmd: 'ruff check .', test_cmd: 'pytest', build_cmd: '' },
  ],
  git_user_email: 'blueprint-agent@automated.dev',
  git_user_name: 'Blueprint Agent',
  enable_fresh_reviewer: false,
};

describe('ProjectBuildConfigSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { act(() => root.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectBuildConfigSection
            githubOwner="myorg"
            githubRepo="myrepo"
            buildConfig={buildConfig}
            onSave={vi.fn(async () => {})}
            onDetect={vi.fn(async () => {})}
            detectAvailable={true}
            projectId="test-project"
          />,
        );
      });
    });
  });

  test('shows warning for unrecognized lint_cmd', async () => {
    const warnConfig: BuildConfig = {
      ...buildConfig,
      directories: [
        { path: 'backend/', type: 'python', lint_cmd: 'make lint', test_cmd: 'pytest', build_cmd: '' },
      ],
    };
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ProjectBuildConfigSection
          githubOwner="myorg"
          githubRepo="myrepo"
          buildConfig={warnConfig}
          onSave={vi.fn(async () => {})}
          onDetect={vi.fn(async () => {})}
          detectAvailable={true}
          projectId="test-project"
        />,
      );
    });
    const warnings = host.querySelectorAll('[data-testid="lint-cmd-warning"]');
    expect(warnings.length).toBe(1);
    expect(warnings[0].textContent).toContain('Unrecognized');
  });

  test('no warning for recognized lint_cmd', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ProjectBuildConfigSection
          githubOwner="myorg"
          githubRepo="myrepo"
          buildConfig={buildConfig}
          onSave={vi.fn(async () => {})}
          onDetect={vi.fn(async () => {})}
          detectAvailable={true}
          projectId="test-project"
        />,
      );
    });
    const warnings = host.querySelectorAll('[data-testid="lint-cmd-warning"]');
    expect(warnings.length).toBe(0);
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectBuildConfigSection
            githubOwner="myorg"
            githubRepo="myrepo"
            buildConfig={buildConfig}
            onSave={vi.fn(async () => {})}
            onDetect={vi.fn(async () => {})}
            detectAvailable={true}
            projectId="test-project"
          />,
        );
      });
    }, { selector: 'button, a[href]' });
  });
});
