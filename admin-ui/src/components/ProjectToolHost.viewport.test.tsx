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
    kind: 'mock-widget',
    slug: 'mock-widget',
    title: 'Mock Widget',
    enabled: true,
    placement: 'project_section',
    content_revision: 0,
  },
  {
    id: 'tool-2',
    kind: 'mock-chart',
    slug: 'mock-chart',
    title: 'Mock Chart with a Longer Title That Tests Truncation Behavior',
    enabled: true,
    placement: 'project_section',
    content_revision: 0,
  },
  {
    id: 'tool-3',
    kind: 'mock-table',
    slug: 'mock-table',
    title: 'Mock Table',
    enabled: true,
    placement: 'project_section',
    content_revision: 0,
  },
];

function MockWidget({ tool }: ToolComponentProps) {
  return <div className="rounded bg-neutral-800 p-2 text-xs text-neutral-300">{tool.title} content</div>;
}

function MockChart({ tool }: ToolComponentProps) {
  return <div className="rounded bg-neutral-800 p-2 text-xs text-neutral-300">{tool.title} chart</div>;
}

function MockTable({ tool }: ToolComponentProps) {
  return <div className="rounded bg-neutral-800 p-2 text-xs text-neutral-300">{tool.title} table</div>;
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
        kind: 'unregistered-kind',
        slug: 'unregistered-kind',
        title: 'Unknown Tool',
        enabled: true,
        placement: 'project_section',
        content_revision: 0,
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
