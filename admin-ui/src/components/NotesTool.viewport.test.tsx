import { afterEach, beforeAll, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { registerToolKind } from '../toolRegistry';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    fetchToolContent: vi.fn(),
    saveToolContent: vi.fn(),
  };
});

import { fetchToolContent, saveToolContent, ConflictError } from '../api/projectTools';
import NotesTool from './NotesTool';

const SAMPLE_TEXT = '# Research notes\n\nHypothesis: energy is $E = mc^2$.\n\n- point one\n- point two';

const SAMPLE_TOOL = {
  id: 'notes-1',
  kind: 'notes',
  slug: 'notes',
  title: 'Notes',
  enabled: true,
  placement: 'project_section' as const,
  content_revision: 2,
};

beforeAll(() => {
  registerToolKind('notes', NotesTool);
});

describe('NotesTool — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.resetAllMocks();
  });

  // -----------------------------------------------------------------------
  // Rendered with content
  // -----------------------------------------------------------------------

  test('renders with sample notes without overflow at 375px', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { text: SAMPLE_TEXT },
      content_revision: 2,
    });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 3 });

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 100)));
    });
    if (!host) throw new Error('host not mounted');
    expect(host.querySelector('textarea')).toBeTruthy();
    await expectNoTextTruncationAt(375, async () => {});
  });

  test('renders with sample notes without overflow at 1280px', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { text: SAMPLE_TEXT },
      content_revision: 2,
    });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 3 });

    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.width = '1280px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 100)));
    });
    if (!host) throw new Error('host not mounted');
    expect(host.querySelector('textarea')).toBeTruthy();
    await expectNoTextTruncationAt(1280, async () => {});
  });

  // -----------------------------------------------------------------------
  // Loading state
  // -----------------------------------------------------------------------

  test('shows loading state before content resolves', async () => {
    let resolveContent: (v: { content: { text: string }; content_revision: number }) => void = () => {};
    vi.mocked(fetchToolContent).mockReturnValue(
      new Promise(res => { resolveContent = res; }),
    );
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Loading notes');
    await act(async () => {
      resolveContent({ content: { text: SAMPLE_TEXT }, content_revision: 2 });
    });
  });

  // -----------------------------------------------------------------------
  // Empty state
  // -----------------------------------------------------------------------

  test('renders an empty editable textarea when no content is saved', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { text: '' },
      content_revision: 0,
    });
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 100)));
    if (!host) throw new Error('host not mounted');
    const textarea = host.querySelector('textarea') as HTMLTextAreaElement | null;
    expect(textarea).toBeTruthy();
    expect(textarea!.value).toBe('');
  });

  // -----------------------------------------------------------------------
  // Edit vs preview
  // -----------------------------------------------------------------------

  test('edit mode shows a textarea; preview mode renders markdown', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({
      content: { text: '**bold text**' },
      content_revision: 1,
    });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 2 });
    host = document.createElement('div');
    host.style.width = '1280px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 100)));
    if (!host) throw new Error('host not mounted');

    // Edit mode by default: textarea present, no rendered markdown <strong>.
    expect(host.querySelector('textarea')).toBeTruthy();
    expect(host.querySelector('strong')).toBeNull();

    // Switch to preview: MarkdownContentInner renders **bold** as <strong>.
    const previewBtn = Array.from(host.querySelectorAll('button')).find(
      b => b.getAttribute('aria-label') === 'Preview mode',
    ) as HTMLButtonElement | null;
    expect(previewBtn).not.toBeNull();
    await act(async () => {
      previewBtn!.click();
      await new Promise(resolve => setTimeout(resolve, 100));
    });
    expect(host.querySelector('textarea')).toBeNull();
    const strong = host.querySelector('strong');
    expect(strong).toBeTruthy();
    expect(strong!.textContent).toContain('bold text');
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
    await act(async () => { r.render(<NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 100)));
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Network error');
  });

  // -----------------------------------------------------------------------
  // Conflict recovery
  // -----------------------------------------------------------------------

  test('a 409 save conflict preserves local edits and surfaces a conflict banner', async () => {
    vi.mocked(fetchToolContent)
      .mockResolvedValueOnce({
        content: { text: SAMPLE_TEXT },
        content_revision: 2,
      })
      .mockResolvedValueOnce({
        content: { text: 'server wins' },
        content_revision: 7,
      });

    vi.mocked(saveToolContent).mockRejectedValueOnce(new ConflictError());

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    await act(async () => await new Promise(resolve => setTimeout(resolve, 100)));

    if (!host) throw new Error('host not mounted');
    const textarea = host.querySelector('textarea') as HTMLTextAreaElement | null;
    expect(textarea).toBeTruthy();

    // Make a local edit so autosave has changed content to persist.
    const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!;
    await act(async () => {
      setter.call(textarea, 'my local edit');
      textarea!.dispatchEvent(new Event('input', { bubbles: true }));
    });

    // Wait for auto-save debounce (1500ms) + conflict reload.
    await act(async () => await new Promise(resolve => setTimeout(resolve, 3000)));

    // fetchToolContent: 1st call (initial load) + 2nd call (conflict reload).
    expect(fetchToolContent).toHaveBeenCalledTimes(2);
    // Local edits are NEVER silently discarded — the losing writer keeps its work.
    expect(textarea!.value).toBe('my local edit');
    expect(textarea!.value).not.toBe('server wins');
    // A conflict banner is surfaced for the operator to resolve.
    expect(host.textContent).toContain('Another viewer saved a newer version');
  });
});
