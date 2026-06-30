import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { StrategicContextMap } from './StrategicContextMap';
import type { StrategicContextPackageDescriptor } from '../strategicStateApi';

type StrategicContextCanvas = NonNullable<StrategicContextPackageDescriptor['canvas']>;

const CANVAS: StrategicContextCanvas = {
  id: 'canvas:test',
  title: 'Strategic context map',
  node_count: 3,
  edge_count: 2,
  nodes: [
    {
      id: 'project',
      kind: 'project',
      label: 'Project foundation',
      target: { surface: 'project', section: 'running-update' },
    },
    {
      id: 'knowledge',
      kind: 'knowledge_document',
      label: 'Operator decision record with a long readable label',
      count: 4,
      status: 'ready',
      target: { surface: 'knowledge', document_id: 'operator-decision-record' },
    },
    {
      id: 'task',
      kind: 'task',
      label: 'Release blocker investigation',
      status: 'blocked',
      target: { surface: 'task', task_id: 'task-release-blocker' },
    },
  ],
  edges: [
    { source: 'project', target: 'knowledge', label: 'informs' },
    { source: 'knowledge', target: 'task', label: 'drives' },
  ],
};

describe('StrategicContextMap', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) act(() => { r.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  function mount(canvas: StrategicContextCanvas | null = CANVAS, onOpenTarget = vi.fn()) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const currentRoot = createRoot(host);
    root = currentRoot;
    act(() => {
      currentRoot.render(<StrategicContextMap canvas={canvas} onOpenTarget={onOpenTarget} />);
    });
    return { host, onOpenTarget };
  }

  test('renders canvas nodes and links as a visual map', () => {
    const { host } = mount();

    expect(host.querySelector('[data-testid="strategic-context-map"]')).not.toBeNull();
    expect(host.querySelector('[role="group"]')?.getAttribute('aria-label')).toContain('3 nodes');
    expect(host.textContent).toContain('Project foundation');
    expect(host.textContent).toContain('Operator decision record with a long readable label');
    expect(host.textContent).toContain('Release blocker investigation');
    expect(host.textContent).toContain('ready');
    expect(host.textContent).toContain('blocked');
    expect(host.textContent).toContain('3 nodes');
    expect(host.textContent).toContain('2 links');
    expect(host.textContent).toContain('informs');
    expect(host.textContent).toContain('drives');
  });

  test('opens node targets through the supplied handler', () => {
    const onOpenTarget = vi.fn();
    const { host } = mount(CANVAS, onOpenTarget);

    Array.from(host.querySelectorAll('button'))
      .find(button => button.title === 'Open Release blocker investigation')
      ?.click();

    expect(onOpenTarget).toHaveBeenCalledTimes(1);
    expect(onOpenTarget).toHaveBeenCalledWith({ surface: 'task', task_id: 'task-release-blocker' });
  });

  test('renders empty and sparse canvases without requiring links', () => {
    const { host } = mount({
      id: 'canvas:sparse',
      title: 'Sparse map',
      nodes: [
        {
          id: 'single',
          kind: 'project_section',
          label: 'A single context node with an intentionally long label that should remain contained',
        },
      ],
      edges: [],
    });

    expect(host.textContent).toContain('A single context node');
    expect(host.textContent).toContain('1 node');
    expect(host.textContent).toContain('0 links');
    expect(host.textContent).toContain('No links in this map yet.');
    expect(host.querySelector('button[title^="Open A single context node"]')).toBeNull();
  });

  test('renders parallel edges with the same label without duplicate key warnings', () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const { host } = mount({
      ...CANVAS,
      edges: [
        { source: 'project', target: 'knowledge', label: 'informs' },
        { source: 'project', target: 'knowledge', label: 'informs' },
      ],
    });

    expect(host.textContent).toContain('2 links');
    expect(consoleError).not.toHaveBeenCalledWith(expect.stringContaining('Encountered two children with the same key'));
    consoleError.mockRestore();
  });

  test('renders an empty state when no canvas nodes are available', () => {
    const { host } = mount({
      id: 'canvas:empty',
      title: 'Empty map',
      nodes: [],
      edges: [],
    });

    expect(host.textContent).toContain('No map nodes are available yet.');
    expect(host.textContent).toContain('Resources and evidence below can still carry the discussion context.');
  });
});
