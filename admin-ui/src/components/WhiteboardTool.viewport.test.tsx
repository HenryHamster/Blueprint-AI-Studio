import { afterEach, beforeAll, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { registerToolKind } from '../toolRegistry';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    fetchToolContent: vi.fn(),
    saveToolContent: vi.fn(),
  };
});

import { fetchToolContent, saveToolContent } from '../api/projectTools';
import WhiteboardTool, { DEBOUNCE_MS } from './WhiteboardTool';

const SAMPLE_STROKES = [
  { color: '#e2e8f0', width: 4, points: [{ x: 50, y: 50 }, { x: 100, y: 60 }, { x: 150, y: 45 }] },
  { color: '#f87171', width: 2, points: [{ x: 200, y: 80 }, { x: 220, y: 120 }, { x: 260, y: 110 }] },
];

const SAMPLE_TOOL = {
  id: 'wb-1',
  kind: 'whiteboard',
  slug: 'whiteboard',
  title: 'Whiteboard',
  enabled: true,
  placement: 'project_section' as const,
  content_revision: 3,
};

beforeAll(() => {
  registerToolKind('whiteboard', WhiteboardTool);
});

describe('WhiteboardTool — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.resetAllMocks();
    vi.useRealTimers();
  });

  // -----------------------------------------------------------------------
  // Rendered with strokes
  // -----------------------------------------------------------------------

  test('renders with sample strokes without overflow at 375px', async () => {
    // Viewport helpers settle layout via requestAnimationFrame (and page.viewport),
    // which the global fake clock freezes — use real timers for helper-driven tests.
    vi.useRealTimers();
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { strokes: SAMPLE_STROKES },
      content_revision: 3,
    });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 100)); });
    });
    if (!host) throw new Error('host not mounted');
    expect(host.querySelector('canvas')).toBeTruthy();
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('renders with sample strokes without overflow at 1280px', async () => {
    vi.useRealTimers();
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { strokes: SAMPLE_STROKES },
      content_revision: 3,
    });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.width = '1280px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 100)); });
    });
    if (!host) throw new Error('host not mounted');
    expect(host.querySelector('canvas')).toBeTruthy();
    await expectNoTextTruncationAt(1280, async () => {});
  });

  // -----------------------------------------------------------------------
  // Loading state
  // -----------------------------------------------------------------------

  test('shows loading state before content resolves', async () => {
    let resolveContent: (v: { content: { strokes: typeof SAMPLE_STROKES }; content_revision: number }) => void = () => {};
    vi.mocked(fetchToolContent).mockReturnValue(
      new Promise(res => { resolveContent = res; }),
    );
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Loading whiteboard');
    await act(async () => {
      resolveContent({ content: { strokes: SAMPLE_STROKES }, content_revision: 3 });
    });
  });

  // -----------------------------------------------------------------------
  // Empty state
  // -----------------------------------------------------------------------

  test('renders empty whiteboard when content has no strokes', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { strokes: [] },
      content_revision: 0,
    });
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => { await vi.runAllTimersAsync(); });
    if (!host) throw new Error('host not mounted');
    // Should still have a canvas and controls.
    expect(host.querySelector('canvas')).toBeTruthy();
    expect(host.textContent).toContain('Clear');
  });

  // -----------------------------------------------------------------------
  // Error state
  // -----------------------------------------------------------------------

  test('shows error state when fetch fails', async () => {
    vi.mocked(fetchToolContent).mockRejectedValue(new Error('Network error'));
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => { await vi.runAllTimersAsync(); });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Network error');
  });

  // -----------------------------------------------------------------------
  // Controls
  // -----------------------------------------------------------------------

  test('renders color palette and stroke-width controls', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { strokes: SAMPLE_STROKES },
      content_revision: 3,
    });
    host = document.createElement('div');
    host.style.width = '1280px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => { await vi.runAllTimersAsync(); });
    if (!host) throw new Error('host not mounted');
    // Color buttons
    const colorButtons = host.querySelectorAll('[role="radio"]');
    expect(colorButtons.length).toBeGreaterThanOrEqual(5);
    // Clear button
    expect(host.textContent).toContain('Clear');
  });

  // -----------------------------------------------------------------------
  // Touch target at 1280px (primary acceptance width)
  // -----------------------------------------------------------------------

  // -----------------------------------------------------------------------
  // Autosave loop guard: unchanged content must not autosave
  // -----------------------------------------------------------------------

  test('does not autosave when loaded content is never edited', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { strokes: SAMPLE_STROKES },
      content_revision: 3,
    });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => { await vi.runAllTimersAsync(); });

    if (!host) throw new Error('host not mounted');
    expect(host.querySelector('canvas')).toBeTruthy();

    // Well beyond several debounce cycles — a reference-identity dependency
    // would have fired a continuous save loop here.
    await act(async () => { await vi.advanceTimersByTimeAsync(DEBOUNCE_MS * 3); });

    expect(saveToolContent).not.toHaveBeenCalled();
  });

  test('controls meet minimum touch target at 1280px', async () => {
    vi.useRealTimers();
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { strokes: SAMPLE_STROKES },
      content_revision: 3,
    });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 4 });

    await expectMinTouchTargetAt(1280, async () => {
      host = document.createElement('div');
      host.style.width = '1280px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<WhiteboardTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
      await act(async () => { await new Promise(resolve => setTimeout(resolve, 100)); });
    });
  });
});
