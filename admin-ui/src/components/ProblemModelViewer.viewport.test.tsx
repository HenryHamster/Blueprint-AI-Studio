import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import ProblemModelViewer from './ProblemModelViewer';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import type { ProblemModel } from '../types';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
}));

function makeProblemModel(overrides: Partial<ProblemModel> = {}): ProblemModel {
  return {
    intent: {
      purpose: 'Build an autonomous coding studio that enables agents to ship merged PRs without human intervention across multiple project types and infrastructure configurations.',
      current_focus: 'Stabilize the tactical→review→merge loop to achieve 70%+ first-attempt merge rate and reduce review iteration overhead.',
    },
    context: {
      project_type: 'startup',
      users: 'Engineering teams building with Blueprint',
      team: '5 engineers + 2 PMs',
      deploy_cadence: 'continuous',
      notes: 'Risk tolerance is medium — reliability and debuggability matter more than absolute speed. Compliance requirements around audit logging may affect agent instrumentation.',
    },
    constraints: [
      'Do not modify .github/workflows/ — CI workflows managed separately',
      'Maintain backward compatibility for projects on stable branches',
      'All new features must include viewport regression tests at 375px width',
    ],
    capabilities: [
      { name: 'code analysis and transformation', status: 'working', notes: 'AST-based refactoring for Python and TypeScript' },
      { name: 'test execution and validation', status: 'working', notes: 'Docker-based hermetic test environments' },
      { name: 'git operations (clone, branch, commit, push)', status: 'working', notes: 'Full read/write access with SSH key management' },
      { name: 'cross-system coordination with GitHub', status: 'partial', notes: 'PR creation and status updates working; webhook integration pending' },
      { name: 'viewport regression detection at 375px', status: 'working', notes: 'Browser-mode vitest with image baselines' },
      { name: 'structured problem analysis and gap prioritization', status: 'planned', notes: 'Requires integration with vector search for semantic similarity' },
    ],
    gaps: [
      {
        id: 'gap-1',
        name: 'Reduce agent iteration count on cross-system contract failures',
        description: 'When a generated URL or API shape mismatches between frontend and backend, the agent retries from scratch instead of narrowly fixing the contract violation. Requires structured contract extraction and violation classification.',
        priority: 'high',
        size: 'large',
        evidence: 'Observed in PR #4705: self-review correctly flagged symptomLayers shape mismatch, but agent couldn\'t repair in-place.',
      },
      {
        id: 'gap-2',
        name: 'Scope-drift detection in viewport test creation',
        description: 'Creating viewport tests for new components occasionally introduces unrelated refactoring or style changes. Recommend a pre-commit lint check for scope-drift in test-only PRs.',
        priority: 'medium',
        size: 'medium',
      },
      {
        id: 'gap-3',
        name: 'Performance profiling for agent VM provisioning',
        description: 'VM cold-start latency is variable; need structured observability to distinguish image build time, boot time, and first-request time.',
        priority: 'low',
        size: 'medium',
      },
    ],
    friction: [
      {
        id: 'friction-1',
        name: 'Docker image size affecting agent startup',
        status: 'mitigated',
        description: 'Reduced from 8GB to 3.2GB by removing development dependencies. Further reductions require careful dep analysis.',
        seen_in: ['sprint-10', 'sprint-11'],
      },
      {
        id: 'friction-2',
        name: 'Playwright browser mode test flakiness on shallow RAM VMs',
        status: 'resolved',
        description: 'Switched to headless mode for non-visual tests; visual tests now run in dedicated high-memory runners.',
        seen_in: ['sprint-9'],
      },
    ],
    pending_user_confirmation: [
      {
        id: 'pending-1',
        type: 'major_decision',
        proposal: 'Adopt a 30-day retention window for agent execution logs and structured telemetry beyond the current 7-day window.',
        rationale: 'Longer retention enables better diagnosis of intermittent failures and compliance audits, but increases storage costs by ~2x. Cost is acceptable given regulatory pressure.',
        created_at: '2026-05-08T14:30:00Z',
      },
    ],
    archived_gaps: [
      {
        id: 'gap-archived-1',
        name: 'Rate-limit coordination across async tasks',
        status: 'closed',
        closed: '2026-04-20T10:00:00Z',
      },
    ],
    revision: 12,
    last_updated: '2026-05-08T15:45:00Z',
    last_updated_by: 'blueprint-agent',
    ...overrides,
  };
}

const noop = () => {};
const defaultProps = {
  projectId: 'proj-test-1',
  addToast: noop,
  onUpdated: noop,
};

describe('ProblemModelViewer — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.height = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelViewer
            problemModel={makeProblemModel()}
            {...defaultProps}
          />
        );
      });
    });
  });

  test('long capability descriptions and context notes do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.height = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelViewer
            problemModel={makeProblemModel()}
            {...defaultProps}
          />
        );
      });
    });
  });

  test('renders with multiple gaps, capabilities, and friction without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.height = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelViewer
            problemModel={makeProblemModel({
              gaps: Array.from({ length: 8 }, (_, i) => ({
                id: `gap-${i}`,
                name: `Gap ${i + 1}: Long description of a specific capability gap that needs addressing in this system`,
                description: 'This is a detailed explanation of why this gap matters and what the impact would be if unresolved.',
                priority: ['high', 'medium', 'low'][i % 3] as 'high' | 'medium' | 'low',
                size: ['small', 'medium', 'large'][i % 3] as 'small' | 'medium' | 'large',
                tag: i % 2 === 0 ? 'urgent' : undefined,
              })),
              capabilities: Array.from({ length: 6 }, (_, i) => ({
                name: `Capability ${i + 1}: Very detailed capability name with context about what it does`,
                status: (['working', 'partial', 'planned'] as const)[i % 3],
                notes: 'Implementation note explaining the current state and any limitations.',
              })),
            })}
            {...defaultProps}
          />
        );
      });
    });
  });

  test('pending confirmations render without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.height = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelViewer
            problemModel={makeProblemModel({
              pending_user_confirmation: [
                {
                  id: 'pending-1',
                  type: 'major_decision',
                  proposal: 'Adopt a 30-day retention window for agent execution logs and structured telemetry to improve debugging and compliance auditing capabilities.',
                  rationale: 'Longer retention enables root-cause diagnosis of intermittent failures across multiple task runs and supports regulatory audit requirements.',
                  created_at: '2026-05-08T14:30:00Z',
                },
                {
                  id: 'pending-2',
                  type: 'credentials_needed',
                  proposal: 'Grant OAuth token access to private GitHub repositories for dependency scanning.',
                  created_at: '2026-05-07T09:15:00Z',
                },
              ],
            })}
            {...defaultProps}
          />
        );
      });
    });
  });
});
