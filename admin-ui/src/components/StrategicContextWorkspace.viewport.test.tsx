import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicContextWorkspace } from './StrategicContextWorkspace';
import { expectMinTouchTargetAt, expectNoOverflowAt } from '../test/viewport';
import type { StrategicStateResponse } from '../strategicStateApi';

const STATE = {
  context_package: {
    id: 'strategic-context:proj-viewport',
    title: 'Strategic context package with long operator-facing labels',
    status: 'ready',
    item_count: 18,
    token_count: 3800,
    sources: [
      { kind: 'running_update', count: 1 },
      { kind: 'problem_model', count: 7 },
      { kind: 'tasks', count: 10 },
    ],
    canvas: {
      id: 'canvas:proj-viewport',
      title: 'Strategic context map',
      node_count: 3,
      edge_count: 2,
      nodes: [
        {
          id: 'task-runner-registration',
          kind: 'task',
          label: 'Validate local Strategic runner registration and device liveness',
          summary: 'The operator needs to see whether the connected device can accept Strategic work.',
          status: 'blocked',
          target: { surface: 'task', task_id: 'task-runner-registration' },
        },
        {
          id: 'project-problem-model',
          kind: 'project_section',
          label: 'Problem model and operating assumptions',
          summary: 'Active questions for the workday session.',
          target: { surface: 'project', section: 'problem-model' },
        },
      ],
      edges: [
        { source: 'project-problem-model', target: 'task-runner-registration', label: 'drives' },
      ],
    },
    resources: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:operator-checkin-notes',
        label: 'Operator check-in notes and persistent Strategic context',
        summary: 'Daily notes, decisions, and constraints.',
        target: { surface: 'knowledge', document_id: 'operator-checkin-notes' },
      },
      {
        kind: 'artifact',
        id: 'artifact:runner-registration-report',
        label: 'Runner registration report with diagnostic output',
        summary: 'Self-hosted runner readiness evidence.',
        target: { surface: 'task', task_id: 'task-runner-registration', artifact_id: 'runner-registration-report' },
      },
    ],
    evidence: {
      knowledge: { total: 0, items: [] },
      artifacts: { total: 0, items: [] },
    },
    drilldowns: [
      { id: 'runtime', label: 'Runtime devices', target: { surface: 'runtime', section: 'devices' } },
      { id: 'pipeline', label: 'Pipeline flow', target: { surface: 'pipeline' } },
    ],
  },
  work_session: {
    version: 1,
    id: 'strategic-session:proj-viewport',
    mode: 'continuous',
    active_surface: 'strategic',
    selected_refs: [
      { kind: 'knowledge_document', id: 'knowledge:operator-checkin-notes', label: 'Operator check-in notes and persistent Strategic context' },
    ],
    selected_ref_descriptors: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:operator-checkin-notes',
        label: 'Operator check-in notes and persistent Strategic context',
        summary: 'Daily notes, decisions, and constraints.',
        status: 'active',
        target: { surface: 'knowledge', document_id: 'operator-checkin-notes' },
      },
    ],
    curated_metrics: [],
  },
} as StrategicStateResponse;

describe('StrategicContextWorkspace — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  function mount() {
    if (root) {
      act(() => { root?.unmount(); });
      root = null;
    }
    host?.remove();
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    act(() => {
      currentRoot.render(
        <StrategicContextWorkspace
          open
          state={STATE}
          onClose={vi.fn()}
          onOpenTarget={vi.fn()}
          onUpdateSessionRefs={vi.fn()}
        />,
      );
    });
  }

  test('drawer renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, mount);
  });

  test('drawer controls meet touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, mount);
  });
});
