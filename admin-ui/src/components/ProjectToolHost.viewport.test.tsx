import { afterEach, beforeAll, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectToolHost } from './ProjectToolHost';
import { registerToolKind } from '../toolRegistry';
import type { ToolComponentProps } from '../toolRegistry';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    fetchProjectTools: vi.fn(),
  };
});

import { fetchProjectTools } from '../api/projectTools';

const SAMPLE_TOOLS = [
  {
    id: 'tool-1',
    project_id: 'proj-a',
    kind: 'mock-widget',
    label: 'Mock Widget',
    config: {},
    enabled: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'tool-2',
    project_id: 'proj-a',
    kind: 'mock-chart',
    label: 'Mock Chart with a Longer Label That Tests Truncation Behavior',
    config: {},
    enabled: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
  {
    id: 'tool-3',
    project_id: 'proj-a',
    kind: 'mock-table',
    label: 'Mock Table',
    config: {},
    enabled: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
  },
];

function MockWidget({ tool }: ToolComponentProps) {
  return <div className="rounded bg-neutral-800 p-2 text-xs text-neutral-300">{tool.label} content</div>;
}

function MockChart({ tool }: ToolComponentProps) {
  return <div className="rounded bg-neutral-800 p-2 text-xs text-neutral-300">{tool.label} chart</div>;
}

function MockTable({ tool }: ToolComponentProps) {
  return <div className="rounded bg-neutral-800 p-2 text-xs text-neutral-300">{tool.label} table</div>;
}

beforeAll(() => {
  registerToolKind('mock-widget', MockWidget);
  registerToolKind('mock-chart', MockChart);
  registerToolKind('mock-table', MockTable);
});

describe('ProjectToolHost — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.resetAllMocks();
  });

  test('renders tool grid without overflow at 375px', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue(SAMPLE_TOOLS);
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<ProjectToolHost projectId="proj-a" />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Mock Widget');
    expect(host.textContent).toContain('Mock Chart');
    expect(host.textContent).toContain('Mock Table');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('renders tool grid without overflow at 1280px', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue(SAMPLE_TOOLS);
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.width = '1280px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<ProjectToolHost projectId="proj-a" />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    });
    await expectNoTextTruncationAt(1280, async () => {});
  });

  test('shows empty state when project has no tools', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue([]);
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolHost projectId="proj-empty" />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('No tools registered');
  });

  test('shows empty state when no returned tools match the registry', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue([
      {
        id: 'tool-unknown',
        project_id: 'proj-a',
        kind: 'unregistered-kind',
        label: 'Unknown Tool',
        config: {},
        enabled: true,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z',
      },
    ]);
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolHost projectId="proj-a" />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('No tools registered');
  });

  test('skips disabled tools without crashing', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue([
      { ...SAMPLE_TOOLS[0], id: 'tool-1', kind: 'mock-widget', enabled: false },
      { ...SAMPLE_TOOLS[1], id: 'tool-2', kind: 'mock-chart', enabled: true },
    ]);
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolHost projectId="proj-a" />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).not.toContain('Mock Widget content');
    expect(host.textContent).toContain('Mock Chart');
  });

  test('shows loading state before fetch resolves', async () => {
    let resolveTools: (v: typeof SAMPLE_TOOLS) => void = () => {};
    vi.mocked(fetchProjectTools).mockReturnValue(
      new Promise(res => { resolveTools = res; }),
    );
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolHost projectId="proj-a" />); });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Loading tools');
    resolveTools(SAMPLE_TOOLS);
  });
});
