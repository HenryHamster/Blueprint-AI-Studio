import { afterEach, beforeAll, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { registerToolKind } from '../toolRegistry';

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

// Drive a React onChange on the textarea via the native value setter.
function typeInto(textarea: HTMLTextAreaElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value')!.set!;
  setter.call(textarea, value);
  textarea.dispatchEvent(new Event('input', { bubbles: true }));
}

describe('NotesTool — autosave loop + conflict data-loss', () => {
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
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
    vi.resetAllMocks();
  });

  async function mount() {
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />); });
    // Flush the mount-time fetch microtasks.
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    if (!host) throw new Error('host not mounted');
    return host.querySelector('textarea') as HTMLTextAreaElement;
  }

  test('no autosave fires when content is unchanged after a save round-trip', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { text: 'loaded' }, content_revision: 2 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 3 });

    const textarea = await mount();

    // A single edit → exactly one debounced save.
    await act(async () => { typeInto(textarea, 'loaded + edit'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);

    // The save refreshes the revision (new state object) but the CONTENT is
    // unchanged — a reference-identity dependency would loop here forever.
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);
  });

  test('loaded-but-never-edited content never autosaves', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { text: 'loaded' }, content_revision: 2 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 3 });

    await mount();
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(saveToolContent).not.toHaveBeenCalled();
  });

  test('a 409 response does not drop local unsaved content', async () => {
    vi.mocked(fetchToolContent)
      .mockResolvedValueOnce({ content: { text: 'loaded' }, content_revision: 2 })
      .mockResolvedValueOnce({ content: { text: 'server version' }, content_revision: 9 });
    vi.mocked(saveToolContent).mockRejectedValueOnce(new ConflictError());

    const textarea = await mount();

    await act(async () => { typeInto(textarea, 'my important local edit'); });
    // Debounce → save → 409 → conflict reload.
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // Local edit is preserved — never replaced by the server's version.
    expect(textarea.value).toBe('my important local edit');
    expect(textarea.value).not.toBe('server version');
    // Conflict is surfaced, not silent.
    expect(host!.textContent).toContain('Another viewer saved a newer version');
  });

  test('"Keep mine" resolves the conflict by persisting local edits against the new base revision', async () => {
    vi.mocked(fetchToolContent)
      .mockResolvedValueOnce({ content: { text: 'loaded' }, content_revision: 2 })
      .mockResolvedValueOnce({ content: { text: 'server version' }, content_revision: 9 });
    vi.mocked(saveToolContent)
      .mockRejectedValueOnce(new ConflictError())
      .mockResolvedValueOnce({ content_revision: 10 });

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'keep this'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    const keepBtn = Array.from(host!.querySelectorAll('button')).find(
      b => /keep mine/i.test(b.textContent || ''),
    ) as HTMLButtonElement;
    expect(keepBtn).toBeTruthy();

    await act(async () => { keepBtn.click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    // Second save uses the server-adopted base revision (9) and the local text.
    expect(saveToolContent).toHaveBeenCalledTimes(2);
    const secondCall = vi.mocked(saveToolContent).mock.calls[1];
    expect(secondCall[2]).toEqual({ text: 'keep this' });
    expect(secondCall[3]).toBe(9);
    // Banner cleared after resolution.
    expect(host!.textContent).not.toContain('Another viewer saved a newer version');
  });
});
