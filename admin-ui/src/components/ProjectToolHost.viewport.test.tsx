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
    fetchProjectToolManifest: vi.fn(),
    fetchAssetToken: vi.fn(),
  };
});

import { fetchProjectTools, fetchProjectToolManifest, fetchAssetToken } from '../api/projectTools';
import type { FetchProjectToolsResult } from '../api/projectTools';

const TOOLS_RESULT: FetchProjectToolsResult = {
  tools: [
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
  ],
  total_count: 3,
  next_offset: null,
};

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

  beforeEach(() => {
    // Default mocks: empty manifest + resolved asset token so existing tests
    // that don't exercise the manifest path still behave predictably.
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({ tools: [], error: null });
    vi.mocked(fetchAssetToken).mockResolvedValue({ token: 'mock-token', expires_at: '2026-07-11T12:00:00Z' });
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.resetAllMocks();
  });

  test('renders tool grid without overflow at 375px', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue(TOOLS_RESULT);
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
    vi.mocked(fetchProjectTools).mockResolvedValue(TOOLS_RESULT);
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
    vi.mocked(fetchProjectTools).mockResolvedValue({
      tools: [],
      total_count: 0,
      next_offset: null,
    });
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
    vi.mocked(fetchProjectTools).mockResolvedValue({
      tools: [
        {
          id: 'tool-unknown',
          kind: 'unregistered-kind',
          slug: 'unregistered-kind',
          title: 'Unknown Tool',
          enabled: true,
          placement: 'project_section',
          content_revision: 0,
        },
      ],
      total_count: 1,
      next_offset: null,
    });
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
    vi.mocked(fetchProjectTools).mockResolvedValue({
      tools: [
        { ...TOOLS_RESULT.tools[0], id: 'tool-1', kind: 'mock-widget', enabled: false },
        { ...TOOLS_RESULT.tools[1], id: 'tool-2', kind: 'mock-chart', enabled: true },
      ],
      total_count: 2,
      next_offset: null,
    });
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
    let resolveTools: (v: FetchProjectToolsResult) => void = () => {};
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
    resolveTools(TOOLS_RESULT);
  });

const SAMPLE_TOOLS: FetchProjectToolsResult = TOOLS_RESULT;

  test('renders manifest-declared tool in iframe alongside registered tools', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue(SAMPLE_TOOLS);
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({
      tools: [{
        slug: 'custom-viewer',
        title: 'Custom Viewer',
        description: 'A project-declared viewer tool',
        placement: 'project_section',
        entry: 'index.html',
        kind: 'project',
        source: 'project',
      }],
      error: null,
    });
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolHost projectId="proj-a" />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    if (!host) throw new Error('host not mounted');
    // Registered tool renders
    expect(host.textContent).toContain('Mock Widget');
    // Manifest tool renders
    expect(host.textContent).toContain('Custom Viewer');
    // Project-declared badge is visible
    expect(host.textContent).toContain('Project-declared');
  });

  test('shows manifest warning without crashing registered tools', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue(SAMPLE_TOOLS);
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({
      tools: [],
      error: 'Manifest validation errors: tools[0]: title must be a non-empty string',
    });
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolHost projectId="proj-a" />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    if (!host) throw new Error('host not mounted');
    // Warning text is shown
    expect(host.textContent).toContain('Manifest warning');
    // Registered tools still render
    expect(host.textContent).toContain('Mock Widget');
  });

  test('renders manifest tools when no registered tools exist', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue({ tools: [], total_count: 0, next_offset: null });
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({
      tools: [{
        slug: 'standalone-tool',
        title: 'Standalone Tool',
        description: 'Only manifest tool',
        placement: 'project_section',
        entry: 'dist/app.html',
        kind: 'project',
        source: 'project',
      }],
      error: null,
    });
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolHost projectId="proj-standalone" />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Standalone Tool');
    expect(host.textContent).toContain('Project-declared');
    expect(host.textContent).not.toContain('No tools registered');
  });

  test('renders manifest and registered tools without overflow at 1440px', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue(SAMPLE_TOOLS);
    vi.mocked(fetchProjectToolManifest).mockResolvedValue({
      tools: [{
        slug: 'big-viewer',
        title: 'Big Data Viewer with Long Title That Tests Layout',
        description: 'A project-declared data viewer',
        placement: 'project_section',
        entry: 'index.html',
        kind: 'project',
        source: 'project',
      }],
      error: null,
    });
    await expectNoOverflowAt(1440, async () => {
      host = document.createElement('div');
      host.style.width = '1440px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<ProjectToolHost projectId="proj-a" />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Mock Widget');
    expect(host.textContent).toContain('Big Data Viewer');
    expect(host.textContent).toContain('Project-declared');
  });
});
