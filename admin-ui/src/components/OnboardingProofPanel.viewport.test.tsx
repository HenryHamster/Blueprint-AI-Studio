import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OnboardingProofPanel } from './OnboardingProofPanel';
import type { Project } from '../types/project';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
} from '../test/viewport';

const BASE_PROJECT: Project = {
  id: 'proj-ext-1',
  name: 'stsbot2',
  status: 'active',
  github_owner: 'HenryHamster',
  github_repo: 'stsbot2',
  autonomous_enabled: true,
  created_at: '2026-06-01T09:00:00Z',
  is_self_repo: false,
};

const PROOF_ALL_DONE = {
  repo_added_at: '2026-06-01T09:00:00Z',
  bootstrap_completed_at: '2026-06-01T09:05:00Z',
  first_task_spawned_at: '2026-06-01T09:15:00Z',
  first_pr_opened_at: '2026-06-01T09:30:00Z',
  first_merge_gate_verdict_at: '2026-06-01T09:45:00Z',
  first_merge_gate_executed_tests: true,
  first_merge_gate_unverified_reason: null,
  first_pr_merged_at: '2026-06-01T09:50:00Z',
};

const PROOF_PENDING = {
  repo_added_at: '2026-06-01T09:00:00Z',
  bootstrap_completed_at: null,
  first_task_spawned_at: null,
  first_pr_opened_at: null,
  first_merge_gate_verdict_at: null,
  first_merge_gate_executed_tests: null,
  first_merge_gate_unverified_reason: null,
  first_pr_merged_at: null,
};

describe('OnboardingProofPanel — viewport regression', () => {
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

  async function renderWith(project: Project) {
    host = document.createElement('div');
    host.style.padding = '16px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<OnboardingProofPanel project={project} />);
    });
  }

  test('renders all milestones completed with tests-executed badge at 1024px', async () => {
    await expectNoOverflowAt(1024, () =>
      renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_ALL_DONE }),
    );
  });

  test('renders pending milestones (em-dash) at 375px without overflow', async () => {
    await expectNoOverflowAt(375, () =>
      renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_PENDING }),
    );
  });

  test('renders fell-open badge with reason at 1024px without overflow', async () => {
    await expectNoOverflowAt(1024, () =>
      renderWith({
        ...BASE_PROJECT,
        onboarding_proof: {
          ...PROOF_ALL_DONE,
          first_merge_gate_executed_tests: false,
          first_merge_gate_unverified_reason: 'no_detectable_test_command',
        },
      }),
    );
  });

  test('operator text is not clipped at 1024px', async () => {
    await expectNoTextTruncationAt(1024, () =>
      renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_ALL_DONE }),
    );
  });

  test('returns null for self-repo — renders nothing', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <OnboardingProofPanel
          project={{ ...BASE_PROJECT, is_self_repo: true, onboarding_proof: PROOF_ALL_DONE }}
        />,
      );
    });
    // Panel must not render any content for self-repo
    if (host.firstChild !== null) {
      throw new Error('OnboardingProofPanel rendered content for is_self_repo=true');
    }
  });

  test('returns null when onboarding_proof is absent', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<OnboardingProofPanel project={{ ...BASE_PROJECT }} />);
    });
    if (host.firstChild !== null) {
      throw new Error('OnboardingProofPanel rendered content when onboarding_proof is absent');
    }
  });
});
