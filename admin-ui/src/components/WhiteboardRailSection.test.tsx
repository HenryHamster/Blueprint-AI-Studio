import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    fetchProjectTools: vi.fn(),
    // The overlay lazy-loads WhiteboardTool, which itself fetches content.
    fetchToolContent: vi.fn().mockResolvedValue({ content: { strokes: [], nodes: [] }, content_revision: 0 }),
    saveToolContent: vi.fn(),
  };
});

import { fetchProjectTools } from '../api/projectTools';
import { WhiteboardRailSection } from './WhiteboardRailSection';

const WHITEBOARD_TOOL = {
  id: 'wb-1',
  kind: 'whiteboard',
  slug: 'whiteboard',
  title: 'Project Whiteboard',
  enabled: true,
  placement: 'sidebar' as const,
  content_revision: 1,
};

const DISABLED_WHITEBOARD = { ...WHITEBOARD_TOOL, id: 'wb-2', enabled: false };

describe('WhiteboardRailSection', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.clearAllMocks();
  });

  async function mount(projectId: string | null) {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<WhiteboardRailSection projectId={projectId} />); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    return host as HTMLDivElement;
  }

  test('renders nothing when there is no selected project', async () => {
    const el = await mount(null);
    expect(el.textContent).toBe('');
    expect(fetchProjectTools).not.toHaveBeenCalled();
  });

  test('surfaces an Open canvas entry point when an enabled whiteboard exists', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue({ tools: [WHITEBOARD_TOOL], total_count: 1, next_offset: null });
    const el = await mount('proj-a');
    expect(el.textContent).toContain('Whiteboard');
    const openBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Open canvas');
    expect(openBtn).toBeTruthy();
  });

  test('opening the canvas mounts the whiteboard tool in an overlay dialog', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue({ tools: [WHITEBOARD_TOOL], total_count: 1, next_offset: null });
    const el = await mount('proj-a');
    const openBtn = Array.from(el.querySelectorAll('button')).find(b => b.textContent === 'Open canvas');
    expect(openBtn).toBeTruthy();
    await act(async () => { openBtn?.dispatchEvent(new MouseEvent('click', { bubbles: true })); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(document.querySelector('[role="dialog"]')).toBeTruthy();
    // The dialog is titled with the whiteboard tool's title.
    expect(document.querySelector('[role="dialog"]')?.textContent).toContain('Project Whiteboard');
  });

  test('points to Project → Tools when no whiteboard tool is present', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue({ tools: [], total_count: 0, next_offset: null });
    const el = await mount('proj-a');
    expect(el.textContent).toContain('Project → Tools');
    expect(Array.from(el.querySelectorAll('button')).some(b => b.textContent === 'Open canvas')).toBe(false);
  });

  test('ignores a disabled whiteboard tool (treated as none present)', async () => {
    vi.mocked(fetchProjectTools).mockResolvedValue({ tools: [DISABLED_WHITEBOARD], total_count: 1, next_offset: null });
    const el = await mount('proj-a');
    expect(el.textContent).toContain('Project → Tools');
  });
});
