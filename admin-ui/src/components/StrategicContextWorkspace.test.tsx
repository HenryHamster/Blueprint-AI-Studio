import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import type { ComponentProps } from 'react';
import { StrategicContextWorkspace } from './StrategicContextWorkspace';
import type { StrategicStateResponse, StrategicWorkbenchState } from '../strategicStateApi';

type SessionRefs = StrategicWorkbenchState['selected_refs'];

const STATE = {
  context_package: {
    id: 'strategic-context:proj-1',
    title: 'Workbench context package',
    status: 'ready',
    updated_at: '2026-06-29T01:00:00Z',
    token_count: 2400,
    item_count: 12,
    sources: [
      { kind: 'running_update', count: 1 },
      { kind: 'tasks', count: 6 },
    ],
    canvas: {
      id: 'canvas:proj-1',
      title: 'Strategic map',
      node_count: 3,
      edge_count: 2,
      nodes: [
        {
          id: 'project',
          kind: 'project',
          label: 'Blueprint',
          summary: 'Operator workbench',
          target: { surface: 'project', section: 'running-update' },
        },
        {
          id: 'problem-model',
          kind: 'project_section',
          label: 'Problem model',
          summary: 'Current product questions',
          target: { surface: 'project', section: 'problem-model' },
        },
        {
          id: 'task-context-1',
          kind: 'task',
          label: 'Investigate local runner',
          summary: 'Runner registration needs validation',
          status: 'blocked',
          count: 2,
          target: { surface: 'task', task_id: 'task-context-1' },
        },
      ],
      edges: [
        { source: 'project', target: 'problem-model', label: 'defines' },
        { source: 'problem-model', target: 'task-context-1', label: 'drives' },
      ],
      drilldowns: [
        { id: 'map', label: 'Problem model', target: { surface: 'project', section: 'problem-model' } },
      ],
    },
    resources: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:operator-notes',
        label: 'Operator notes',
        summary: 'Persistent operator context',
        target: { surface: 'knowledge', document_id: 'operator-notes' },
      },
      {
        kind: 'artifact',
        id: 'artifact:deep-artifact',
        label: 'Deep artifact',
        summary: 'Full workspace should render this row',
        target: { surface: 'task', task_id: 'task-context-1', artifact_id: 'deep-artifact' },
      },
    ],
    evidence: {
      knowledge: {
        total: 1,
        items: [
          {
            id: 'operator-context-brief',
            kind: 'knowledge_document',
            label: 'Operator context brief',
            summary: 'Evidence from project notes',
            target: { surface: 'knowledge', document_id: 'operator-context-brief' },
          },
        ],
      },
      artifacts: {
        total: 1,
        items: [
          {
            id: 'latest-pr',
            kind: 'pull_request',
            label: 'Latest pull request',
            artifact_type: 'pull_request',
            task_title: 'Validate workspace',
            target: { surface: 'task', task_id: 'task-context-2', artifact_id: 'latest-pr' },
          },
        ],
      },
    },
    drilldowns: [
      { id: 'runtime', label: 'Runtime compute', target: { surface: 'runtime', section: 'compute' } },
    ],
  },
  workbench: {
    version: 1,
    interaction_mode: 'continuous',
    active_surface: 'strategic',
    selected_refs: [
      { kind: 'knowledge_document', id: 'knowledge:operator-notes', label: 'Operator notes' },
    ],
  },
  work_session: {
    version: 1,
    id: 'strategic-session:proj-1',
    mode: 'continuous',
    active_surface: 'strategic',
    selected_refs: [
      { kind: 'knowledge_document', id: 'knowledge:operator-notes', label: 'Operator notes' },
    ],
    selected_ref_descriptors: [
      {
        kind: 'knowledge_document',
        id: 'knowledge:operator-notes',
        label: 'Operator notes',
        summary: 'Persistent operator context',
        status: 'reviewing',
        target: { surface: 'knowledge', document_id: 'operator-notes' },
      },
    ],
    curated_metrics: [],
  },
} as StrategicStateResponse;

describe('StrategicContextWorkspace', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  function mount(overrides: Partial<ComponentProps<typeof StrategicContextWorkspace>> = {}) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    const props = {
      open: true,
      state: STATE,
      onClose: vi.fn(),
      onOpenTarget: vi.fn(),
      ...overrides,
    };
    act(() => {
      currentRoot.render(<StrategicContextWorkspace {...props} />);
    });
    return { host, props };
  }

  test('does not render while closed', () => {
    const { host } = mount({ open: false });
    expect(host.textContent).toBe('');
  });

  test('shows a neutral empty state when no context package is loaded', () => {
    const { host } = mount({ state: null });

    expect(host.textContent).toContain('empty');
    expect(host.textContent).toContain('No Strategic context package is loaded for this project.');
    expect(host.textContent).not.toContain('ready');
  });

  test('renders the full Strategic context package', () => {
    const { host } = mount();

    expect(host.textContent).toContain('Strategic context');
    expect(host.textContent).toContain('Workbench context package');
    expect(host.textContent).toContain('Operator notes');
    expect(host.textContent).toContain('reviewing');
    expect(host.textContent).toContain('Problem model');
    expect(host.textContent).toContain('Investigate local runner');
    expect(host.textContent).toContain('blocked');
    expect(host.textContent).toContain('Blueprint');
    expect(host.textContent).toContain('defines');
    expect(host.textContent).toContain('drives');
    expect(host.textContent).toContain('Deep artifact');
    expect(host.textContent).toContain('Operator context brief');
    expect(host.textContent).toContain('Latest pull request');
    expect(host.textContent).toContain('running update 1');
  });

  test('keeps and removes Strategic session references through the shared updater', () => {
    const onUpdateSessionRefs = vi.fn();
    const { host } = mount({ onUpdateSessionRefs });
    const buttons = Array.from(host.querySelectorAll('button'));

    buttons.find(button => button.title === 'Keep Deep artifact in session context')?.click();
    expect(onUpdateSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    let updater = onUpdateSessionRefs.mock.calls.at(-1)?.[0] as (refs: SessionRefs) => SessionRefs;
    expect(updater([])).toEqual([
      { kind: 'artifact', id: 'artifact:deep-artifact', label: 'Deep artifact' },
    ]);

    buttons.find(button => button.title === 'Remove Operator notes from session context')?.click();
    expect(onUpdateSessionRefs).toHaveBeenLastCalledWith(expect.any(Function));
    updater = onUpdateSessionRefs.mock.calls.at(-1)?.[0] as (refs: SessionRefs) => SessionRefs;
    expect(updater([{ kind: 'knowledge_document', id: 'knowledge:operator-notes', label: 'Operator notes' }])).toEqual([]);
  });

  test('opens existing workbench targets and closes the workspace', () => {
    const onClose = vi.fn();
    const onOpenTarget = vi.fn();
    const { host } = mount({ onClose, onOpenTarget });
    const openDeepArtifact = Array.from(host.querySelectorAll('button'))
      .find(button => button.title === 'Open Deep artifact');

    openDeepArtifact?.click();

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onOpenTarget).toHaveBeenCalledWith({
      surface: 'task',
      task_id: 'task-context-1',
      artifact_id: 'deep-artifact',
    });
  });
});
