import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import ProblemModelViewer from './ProblemModelViewer';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { formatAbsolute } from '../utils/time';
import type { ProblemModel, SelfReflectionEntry } from '../types';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => ({ ok: true, json: async () => ({}) })),
  // ProblemModelViewer statically imports dismissPendingConfirmation and
  // acceptPendingConfirmation from '../api' (the dismiss/accept pending-
  // confirmation click paths). In browser-mode ESM, the mock factory must
  // expose every named export the component imports or the module linker
  // throws "does not provide an export named '<name>'" at import time.
  dismissPendingConfirmation: vi.fn(async () => {}),
  acceptPendingConfirmation: vi.fn(async () => {}),
  // ProblemModelViewer statically imports the capability-exemption helpers; the
  // browser-mode ESM linker requires every named export it imports to exist on
  // the mock or module linking throws at import time.
  grantCapabilityProofExemption: vi.fn(async () => ({
    status: 'granted',
    capability_name: 'x',
    exempt_capabilities: ['x'],
  })),
  revokeCapabilityProofExemption: vi.fn(async () => ({
    status: 'revoked',
    capability_name: 'x',
    removed: 1,
    exempt_capabilities: [],
  })),
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

  test('renders proven capability proof evidence and badge text', async () => {
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
            capabilities: [
              {
                name: 'language-aware repo map',
                status: 'working',
                notes: 'Builds a graph of tracked source files and call sites',
                proof_status: 'proven',
                proof_records: [
                  {
                    task_id: 'task-proof-123',
                    proved_at: '2026-06-09T12:34:56Z',
                    proof_kind: 'named_test',
                    proof_ref: 'backend/testing/test_capability_proof.py::test_proof_recorded_on_named_test_proven',
                    commit_sha: 'abc123def4567890fedcba',
                  },
                ],
              },
            ],
          })}
          {...defaultProps}
        />
      );
    });

    expect(host.textContent).toContain('Proven');
    expect(host.textContent).toContain('named_test');
    expect(host.textContent).toContain('test_proof_recorded_on_named_test_proven');
    // The proof timestamp is humanized: the body shows a relative "proved Nd ago"
    // label while the absolute instant is surfaced via the title tooltip
    // (PR #12566, "humanize raw ISO timestamps in evidence panels"). Assert the
    // absolute timestamp is surfaced in the tooltip rather than expecting a raw
    // ISO string in the body text (which is both stale and wall-clock fragile).
    const provedEl = Array.from(host.querySelectorAll('span[title]')).find(
      (el) => el.textContent?.trim().startsWith('proved'),
    );
    expect(provedEl).toBeTruthy();
    expect(provedEl?.getAttribute('title')).toBe(formatAbsolute('2026-06-09T12:34:56Z'));
    expect(host.textContent).toContain('abc123def456');
  });

  test('renders claimed-only capabilities when proof records are absent', async () => {
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
            capabilities: [
              { name: 'unproven capability', status: 'partial', notes: 'No durable proof record yet' },
            ],
          })}
          {...defaultProps}
        />
      );
    });

    expect(host.textContent).toContain('Claimed only');
    expect(host.textContent).not.toContain('named_test');
    expect(host.textContent).not.toContain('downstream_artifact');
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

  test('renders self-reflection entries with all fields', async () => {
    const entries: SelfReflectionEntry[] = [
      {
        ts: '2026-06-07T12:00:00Z',
        cycle_task_id: 'task-aabbccdd-1234-5678-abcd-ef0123456789',
        what_was_hard: 'Determining the correct retry backoff parameters for the async job queue without access to production load data.',
        missing_context: 'Historical p95 task latency and queue depth metrics from the last 30 days would have accelerated this analysis significantly.',
        proposed_self_improvement: 'Add a persistent latency percentile summary to the directions response so the strategist can reference it without launching a separate research task.',
      },
      {
        ts: '2026-06-06T09:30:00Z',
        cycle_task_id: 'task-bbccddee-2345-6789-bcde-f01234567890',
        what_was_hard: 'Understanding which frontend components were already covered by viewport tests vs. which needed new coverage.',
        missing_context: 'A machine-readable test coverage map per component would reduce overhead on coverage-gap analysis.',
        proposed_self_improvement: 'Emit a test-coverage index artifact after each CI run and surface it via the diagnostics endpoint.',
      },
    ];
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.height = '800px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ProblemModelViewer
          problemModel={makeProblemModel()}
          selfReflectionLog={entries}
          {...defaultProps}
        />
      );
    });
    // All three field labels should be present
    expect(host.textContent).toContain('What was hard');
    expect(host.textContent).toContain('Missing context');
    expect(host.textContent).toContain('Proposed improvement');
    // Entry content should render
    expect(host.textContent).toContain('retry backoff parameters');
    expect(host.textContent).toContain('p95 task latency');
    expect(host.textContent).toContain('latency percentile summary');
  });

  test('self-reflection section is absent when selfReflectionLog is empty or omitted', async () => {
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
    // Section heading should not appear when no entries provided
    expect(host.textContent).not.toContain('Self-reflection');
  });

  test('self-reflection section renders without overflow at 375px', async () => {
    const entries: SelfReflectionEntry[] = [
      {
        ts: '2026-06-07T12:00:00Z',
        cycle_task_id: 'task-aabbccdd-1234-5678-abcd-ef0123456789',
        what_was_hard: 'Determining the correct retry backoff parameters for the async job queue without access to production load data.',
        missing_context: 'Historical p95 task latency and queue depth metrics from the last 30 days would have accelerated this analysis significantly.',
        proposed_self_improvement: 'Add a persistent latency percentile summary to the directions response so the strategist can reference it without launching a separate research task.',
      },
    ];
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
            selfReflectionLog={entries}
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

  test('pending confirmations sort blocking types first and cap at 3 with show-more', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.height = '800px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <ProblemModelViewer
          problemModel={makeProblemModel({
            pending_user_confirmation: [
              { id: 'p-1', type: 'major_decision', proposal: 'Decision A' },
              { id: 'p-2', type: 'credentials_needed', proposal: 'Credentials B' },
              { id: 'p-3', type: 'intent_change', proposal: 'Intent C' },
              { id: 'p-4', type: 'access_needed', proposal: 'Access D' },
            ],
          })}
          {...defaultProps}
        />
      );
    });
    // Only 3 visible items initially; credentials and access sort first
    expect(host.textContent).toContain('Credentials B');
    expect(host.textContent).toContain('Access D');
    // Fourth item hidden behind show-more
    expect(host.textContent).not.toContain('Decision A');
    expect(host.textContent).toContain('Show 1 more');
  });

  test('renders user_purpose field when present', async () => {
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
            intent: {
              purpose: 'Agent purpose text',
              current_focus: 'Current focus text',
              user_purpose: 'Operator-written statement of project intent',
            },
          })}
          {...defaultProps}
        />
      );
    });
    expect(host.textContent).toContain('Operator purpose');
    expect(host.textContent).toContain('Operator-written statement of project intent');
  });

  test('renders version goal strip when currentVersion is provided', async () => {
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
          currentVersion={{
            version_number: 5,
            phase: 'open',
            phase_changed_at: null,
            phase_reasoning: null,
            goal: 'Ship container-aware compute execution end-to-end',
            goal_updated_at: '2026-06-01T10:00:00Z',
            goal_editor_info: null,
            live_current_focus: 'Ship container-aware compute execution end-to-end',
            opened_at: '2026-05-30T00:00:00Z',
            qualification_attempts: 0,
            merged_task_count: 3,
            last_qualification: null,
          }}
          {...defaultProps}
        />
      );
    });
    expect(host.textContent).toContain('Version goal');
    expect(host.textContent).toContain('Ship container-aware compute execution end-to-end');
  });

  test('renders no-goal state when currentVersion has null goal', async () => {
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
          currentVersion={{
            version_number: 5,
            phase: 'open',
            phase_changed_at: null,
            phase_reasoning: null,
            goal: null,
            goal_updated_at: null,
            goal_editor_info: null,
            live_current_focus: null,
            opened_at: '2026-05-30T00:00:00Z',
            qualification_attempts: 0,
            merged_task_count: 0,
            last_qualification: null,
          }}
          {...defaultProps}
        />
      );
    });
    expect(host.textContent).toContain('Version goal');
    expect(host.textContent).toContain('No version goal set yet');
  });

  test('renders goal-drift indicator when live focus diverges from goal', async () => {
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
          currentVersion={{
            version_number: 5,
            phase: 'open',
            phase_changed_at: null,
            phase_reasoning: null,
            goal: 'Original version goal text',
            goal_updated_at: '2026-06-01T10:00:00Z',
            goal_editor_info: null,
            live_current_focus: 'Focus has since changed to something different',
            opened_at: '2026-05-30T00:00:00Z',
            qualification_attempts: 0,
            merged_task_count: 5,
            last_qualification: null,
          }}
          {...defaultProps}
        />
      );
    });
    expect(host.textContent).toContain('Goal has drifted from live focus');
    expect(host.textContent).toContain('Focus has since changed to something different');
  });

  test('gap card shows spawn direction button when onSpawnDirection prop is provided', async () => {
    const onSpawnDirection = vi.fn();
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
          onSpawnDirection={onSpawnDirection}
          {...defaultProps}
        />
      );
    });
    expect(host.textContent).toContain('Spawn direction');
    const spawnBtn = Array.from(host.querySelectorAll('button')).find(b => b.textContent?.includes('Spawn direction'));
    expect(spawnBtn).not.toBeNull();
  });

  test('gap card does not show spawn direction button when onSpawnDirection is not provided', async () => {
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
    expect(host.textContent).not.toContain('Spawn direction');
  });

  test('gap card shows link direction dropdown when onLinkDirection and directions are provided', async () => {
    const onLinkDirection = vi.fn();
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
          onLinkDirection={onLinkDirection}
          directions={[{ id: 'd-abc12345', text: 'Fix the contract violation detection flow' }]}
          {...defaultProps}
        />
      );
    });
    expect(host.textContent).toContain('Link direction');
    expect(host.textContent).toContain('Fix the contract violation detection flow');
  });

  test('renders capability proof-exemption controls at 1280px', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.height = '800px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelViewer
            problemModel={makeProblemModel({
              capabilities: [
                {
                  name: 'machine-verifiable capability',
                  status: 'working',
                  notes: 'Has a covering named test',
                  proof_status: 'proven',
                },
                {
                  name: 'frontend-visual-verification-loop',
                  status: 'working',
                  notes: 'Cannot be machine-verified — operator exempt',
                  proof_exempt: true,
                },
                {
                  name: 'unprovable claimed capability',
                  status: 'working',
                  notes: 'No proof yet, not exempt',
                },
                { name: 'a partial capability', status: 'partial', notes: 'In progress' },
              ],
            })}
            {...defaultProps}
          />
        );
      });
    });
    // Working non-exempt caps offer a Grant control; the exempt cap offers Revoke.
    expect(host!.textContent).toContain('Grant exemption');
    expect(host!.textContent).toContain('Revoke exemption');
    expect(host!.textContent).toContain('Exempt');
    // Partial capabilities are not eligible — no exemption control for them.
    const partialToggle = host!.querySelector(
      '[data-testid="capability-exempt-toggle-a partial capability"]',
    );
    expect(partialToggle).toBeNull();
  });

  test('version goal section absent when currentVersion is not provided', async () => {
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
    expect(host.textContent).not.toContain('Version goal');
    expect(host.textContent).not.toContain('No version goal set yet');
  });
});
