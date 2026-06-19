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
  first_autonomous_improvement_at: '2026-06-01T10:00:00Z',
};

// Two steps completed (5 min apart) but frontier has been pending for days — stall threshold exceeded.
const PROOF_STALLED = {
  repo_added_at: '2026-06-01T09:00:00Z',
  bootstrap_completed_at: '2026-06-01T09:05:00Z',
  first_task_spawned_at: null,
  first_pr_opened_at: null,
  first_merge_gate_verdict_at: null,
  first_merge_gate_executed_tests: null,
  first_merge_gate_unverified_reason: null,
  first_pr_merged_at: null,
  first_autonomous_improvement_at: null,
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
  first_autonomous_improvement_at: null,
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

  test('renders first_autonomous_improvement_at milestone at 1024px without overflow', async () => {
    await expectNoOverflowAt(1024, () =>
      renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_ALL_DONE }),
    );
  });

  test('renders elapsed duration badges for completed milestones at 375px without overflow', async () => {
    await expectNoOverflowAt(375, () =>
      renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_ALL_DONE }),
    );
    // Verify duration badges are present in the rendered output
    const durationBadges = host!.querySelectorAll('span');
    const texts = Array.from(durationBadges).map((s) => s.textContent ?? '');
    const hasDurationBadge = texts.some((t) => t.startsWith('+'));
    if (!hasDurationBadge) {
      throw new Error('Expected elapsed duration badges (+Xm/+Xh) but none found');
    }
  });

  test('renders DORA headline summary at 1024px without overflow', async () => {
    await expectNoOverflowAt(1024, () =>
      renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_ALL_DONE }),
    );
    const text = host!.textContent ?? '';
    if (!text.includes('Time to first improvement')) {
      throw new Error('Expected DORA headline "Time to first improvement" not found');
    }
    if (!text.includes('Time to first merge')) {
      throw new Error('Expected DORA headline "Time to first merge" not found');
    }
  });

  test('duration badges absent when only repo_added_at is set (PROOF_PENDING)', async () => {
    await renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_PENDING });
    const spans = host!.querySelectorAll('span');
    const texts = Array.from(spans).map((s) => s.textContent ?? '');
    const hasDurationBadge = texts.some((t) => t.startsWith('+'));
    if (hasDurationBadge) {
      throw new Error('Duration badges should not appear when milestones are not yet completed');
    }
  });

  test('DORA headline absent when PROOF_PENDING (no improvements yet)', async () => {
    await renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_PENDING });
    const text = host!.textContent ?? '';
    if (text.includes('Time to first improvement') || text.includes('Time to first merge')) {
      throw new Error('DORA headline should not render when milestones are pending');
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

  test('renders stall warning when frontier has been pending 2× longer than max prior step', async () => {
    await renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_STALLED });
    const text = host!.textContent ?? '';
    if (!text.includes('Journey stalled')) {
      throw new Error('Expected stall warning "Journey stalled" but it was not rendered');
    }
  });

  test('no stall warning when only repo_added_at is set (no prior step durations)', async () => {
    await renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_PENDING });
    const text = host!.textContent ?? '';
    if (text.includes('Journey stalled')) {
      throw new Error('Stall warning must not render when there are no prior step durations to compare');
    }
  });

  test('no stall warning when journey is fully complete', async () => {
    await renderWith({ ...BASE_PROJECT, onboarding_proof: PROOF_ALL_DONE });
    const text = host!.textContent ?? '';
    if (text.includes('Journey stalled')) {
      throw new Error('Stall warning must not render when journey is fully complete');
    }
  });

  test('stall warning includes failed task count when tasks have failed', async () => {
    await renderWith({
      ...BASE_PROJECT,
      onboarding_proof: PROOF_STALLED,
      task_status_counts: { queued: 0, running: 0, evaluating: 0, blocked: 0, failed: 3, completed: 5, total: 8 },
    });
    const text = host!.textContent ?? '';
    if (!text.includes('Journey stalled')) {
      throw new Error('Expected stall warning but it was not rendered');
    }
    if (!text.includes('3 failed tasks')) {
      throw new Error('Expected stall warning to include "3 failed tasks" but it was not found');
    }
  });

  test('stall warning uses singular "task" for single failed task', async () => {
    await renderWith({
      ...BASE_PROJECT,
      onboarding_proof: PROOF_STALLED,
      task_status_counts: { queued: 0, running: 0, evaluating: 0, blocked: 0, failed: 1, completed: 5, total: 6 },
    });
    const text = host!.textContent ?? '';
    if (!text.includes('1 failed task')) {
      throw new Error('Expected stall warning to include "1 failed task" (singular) but it was not found');
    }
  });

  test('stall warning omits failed task count when no tasks have failed', async () => {
    await renderWith({
      ...BASE_PROJECT,
      onboarding_proof: PROOF_STALLED,
      task_status_counts: { queued: 0, running: 0, evaluating: 0, blocked: 0, failed: 0, completed: 5, total: 5 },
    });
    const text = host!.textContent ?? '';
    if (!text.includes('Journey stalled')) {
      throw new Error('Expected stall warning but it was not rendered');
    }
    if (text.includes('failed task')) {
      throw new Error('Stall warning should not include failed task count when failed=0');
    }
  });
});
