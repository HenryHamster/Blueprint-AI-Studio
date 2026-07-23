import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProblemModelEditor } from './ProblemModelEditor';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';
import type { ProblemModel } from '../types';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({ problem_model: {} }), { status: 200 })),
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
    capabilities: [],
    gaps: [],
    friction: [],
    pending_user_confirmation: [],
    archived_gaps: [],
    revision: 5,
    last_updated: '2026-05-08T15:45:00Z',
    last_updated_by: 'agent',
    ...overrides,
  };
}

const noop = () => {};

describe('ProblemModelEditor — viewport regression', () => {
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

  test('editor modal renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelEditor
            projectId="proj-test-1"
            problemModel={makeProblemModel()}
            onClose={noop}
            onSaved={noop}
            addToast={noop}
          />
        );
      });
    });
  });

  test('long intent and context text do not silently clip at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelEditor
            projectId="proj-test-1"
            problemModel={makeProblemModel({
              intent: {
                purpose: 'Build an autonomous coding studio that enables agents to ship merged PRs without human intervention across multiple project types and infrastructure configurations with comprehensive observability.',
                current_focus: 'Stabilize the tactical→review→merge loop to achieve 70%+ first-attempt merge rate and reduce review iteration overhead while maintaining code quality standards.',
              },
              context: {
                project_type: 'startup / research hybrid with high-risk infrastructure experimentation',
                users: 'Engineering teams building with Blueprint across 3 different cloud providers and on-premise deployments',
                team: '5 backend engineers + 3 frontend engineers + 2 product managers + 1 DevOps specialist',
                deploy_cadence: 'continuous with blue-green deployments and automated rollback on telemetry alerts',
                notes: 'Risk tolerance is medium — reliability and debuggability matter more than absolute speed. Compliance requirements around audit logging may affect agent instrumentation decisions.',
              },
            })}
            onClose={noop}
            onSaved={noop}
            addToast={noop}
          />
        );
      });
    });
  });

  test('editor with many constraints renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelEditor
            projectId="proj-test-1"
            problemModel={makeProblemModel({
              constraints: Array.from({ length: 6 }, (_, i) => `Constraint ${i + 1}: Do not modify or remove this important system component or configuration setting without explicit approval`),
            })}
            onClose={noop}
            onSaved={noop}
            addToast={noop}
          />
        );
      });
    });
  });

  test('close and save buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelEditor
            projectId="proj-test-1"
            problemModel={makeProblemModel()}
            onClose={noop}
            onSaved={noop}
            addToast={noop}
          />
        );
      });
    }, { selector: 'button' });
  });

  test('constraint input fields and remove buttons are accessible at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProblemModelEditor
            projectId="proj-test-1"
            problemModel={makeProblemModel({
              constraints: Array.from({ length: 4 }, (_, i) => `Constraint ${i + 1}`),
            })}
            onClose={noop}
            onSaved={noop}
            addToast={noop}
          />
        );
      });
    }, { selector: 'button' });
  });
});
