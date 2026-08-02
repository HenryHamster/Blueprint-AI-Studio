import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';
import { ProjectBuildConfigSection } from './ProjectBuildConfigSection';
import type { BuildConfig, MergeVerificationVerdict } from '../types/project';

const buildConfig: BuildConfig = {
  language: 'TypeScript',
  directories: [
    { path: 'admin-ui/', type: 'node', lint_cmd: 'npx eslint .', test_cmd: 'npm test', build_cmd: 'npm run build' },
    { path: 'backend/', type: 'python', lint_cmd: 'ruff check .', test_cmd: 'pytest', build_cmd: '' },
  ],
  git_user_email: 'blueprint-agent@automated.dev',
  git_user_name: 'Blueprint Agent',
};

describe('ProjectBuildConfigSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
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

  test('unverified merge-verdict warning banner renders without overflow at 375px', async () => {
    const verdict: MergeVerificationVerdict = { state: 'unverified', test_commands: [] };
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
            mergeVerificationVerdict={verdict}
          />,
        );
      });
    });
    const banner = host!.querySelector('[data-testid="merge-verification-banner"]');
    expect(banner?.getAttribute('data-verdict-state')).toBe('unverified');
    expect(banner?.textContent).toContain('UNVERIFIED');
  });

  test('tests_gated merge-verdict banner lists commands without overflow at 375px', async () => {
    const verdict: MergeVerificationVerdict = {
      state: 'tests_gated',
      test_commands: [
        { path: 'backend/', test_cmd: 'python3 -m pytest backend/testing --maxfail=1 --disable-warnings -q' },
        { path: 'admin-ui/', test_cmd: 'npm test --workspace admin-ui -- --run' },
      ],
    };
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
            mergeVerificationVerdict={verdict}
          />,
        );
      });
    });
    const banner = host!.querySelector('[data-testid="merge-verification-banner"]');
    expect(banner?.getAttribute('data-verdict-state')).toBe('tests_gated');
    expect(banner?.textContent).toContain('python3 -m pytest');
    expect(banner?.textContent).toContain('backend/');
  });

  test('tests_gated merge-verdict banner shows runtime fallback details without overflow at 375px', async () => {
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
            mergeVerificationVerdict={{
              state: 'tests_gated',
              test_commands: [{ path: 'backend/', test_cmd: 'pytest' }],
            }}
            mergeGateRuntime={{
              last_run_at: '2026-04-24T11:30:00Z',
              executed_real_tests: true,
              last_unverified_at: '2026-04-24T11:50:00Z',
              last_unverified_reason: 'no_detectable_test_command',
            }}
          />,
        );
      });
    });

    const banner = host!.querySelector('[data-testid="merge-verification-banner"]');
    expect(banner?.getAttribute('data-verdict-state')).toBe('tests_gated');
    expect(banner?.getAttribute('data-runtime-state')).toBe('unverified-fallback');
    expect(banner?.textContent).toContain('Last live merge fell back to UNVERIFIED');
    expect(banner?.textContent).toContain('Last verified real-test run');
  });

  async function renderWith(config: BuildConfig): Promise<string | null> {
    const el = document.createElement('div');
    host = el;
    document.body.appendChild(el);
    const r = createRoot(el);
    root = r;
    await act(async () => {
      r.render(
        <ProjectBuildConfigSection
          githubOwner="myorg"
          githubRepo="myrepo"
          buildConfig={config}
          onSave={vi.fn(async () => {})}
          onDetect={vi.fn(async () => {})}
          detectAvailable={true}
          projectId="test-project"
        />,
      );
    });
    const banner = el.querySelector('[data-testid="ci-status-banner"]');
    return banner ? banner.textContent : null;
  }

  test('has_ci=true renders the CI-gated banner', async () => {
    const text = await renderWith({ ...buildConfig, has_ci: true });
    expect(text).toContain('CI detected');
  });

  test('has_ci=false with no test_cmd warns about UNVERIFIED merges', async () => {
    const text = await renderWith({
      ...buildConfig,
      has_ci: false,
      directories: [{ path: 'backend/', type: 'python', lint_cmd: 'ruff check .', test_cmd: '', build_cmd: '' }],
    });
    expect(text).toContain('No CI detected');
    expect(text).toContain('UNVERIFIED');
  });

  test('has_ci=false with a test_cmd reports the gate will run tests', async () => {
    const text = await renderWith({ ...buildConfig, has_ci: false });
    expect(text).toContain('No CI detected');
    expect(text).toContain('the no-CI gate will run tests');
  });

  test('has_ci undefined renders no CI banner', async () => {
    const text = await renderWith({ ...buildConfig });
    expect(text).toBeNull();
  });

  test('renders without horizontal overflow at 375px with no-CI banner', async () => {
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
            buildConfig={{ ...buildConfig, has_ci: false, directories: [{ path: 'backend/', type: 'python', test_cmd: '' }] }}
            onSave={vi.fn(async () => {})}
            onDetect={vi.fn(async () => {})}
            detectAvailable={true}
            projectId="test-project"
          />,
        );
      });
    });
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
