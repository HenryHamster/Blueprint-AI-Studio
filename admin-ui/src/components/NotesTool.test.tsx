import { afterEach, beforeAll, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, StrictMode } from 'react';
import { registerToolKind } from '../toolRegistry';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    createResidentToolArtifactRef: vi.fn(),
    fetchToolContent: vi.fn(),
    saveToolContent: vi.fn(),
  };
});

vi.mock('../utils/residentToolHandoff', async () => {
  const actual = await vi.importActual<typeof import('../utils/residentToolHandoff')>(
    '../utils/residentToolHandoff',
  );
  return {
    ...actual,
    dispatchResidentToolHandoff: vi.fn(),
  };
});

import {
  createResidentToolArtifactRef,
  fetchToolContent,
  saveToolContent,
  ConflictError,
} from '../api/projectTools';
import { dispatchResidentToolHandoff } from '../utils/residentToolHandoff';
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

const NOTES_ARTIFACT_REF = {
  id: 'ref-notes-3',
  artifact_ref_id: 'ref-notes-3',
  tool_id: 'notes-1',
  project_id: 'proj-a',
  kind: 'notes' as const,
  revision: 3,
  checksum: 'notes-sha256',
  author: 'operator-a',
  timestamp: '2026-07-17T00:00:00Z',
  retrieval: {
    capability: 'inline_bounded_text' as const,
    href: '/api/admin/projects/proj-a/tools/notes-1/artifact-refs/ref-notes-3',
  },
};

beforeAll(() => {
  registerToolKind('notes', NotesTool);
});

beforeEach(() => {
  vi.mocked(dispatchResidentToolHandoff).mockReturnValue(true);
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

  test('StrictMode replay and unmount before load do not persist an empty document', async () => {
    vi.mocked(fetchToolContent).mockImplementation(() => new Promise(() => {}));

    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <StrictMode>
          <NotesTool projectId="proj-a" tool={SAMPLE_TOOL} />
        </StrictMode>,
      );
    });
    await act(async () => { root?.unmount(); });
    root = null;

    expect(saveToolContent).not.toHaveBeenCalled();
  });

  test('shows unsaved changes during debounce and Saved only after persistence', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { text: 'loaded' }, content_revision: 2 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 3 });

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'edited'); });
    expect(host!.textContent).toContain('Unsaved changes');
    expect(host!.textContent).not.toContain('Saved');

    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(saveToolContent).toHaveBeenCalledWith('proj-a', 'notes-1', { text: 'edited' }, 2);
    expect(host!.textContent).toContain('Saved');
  });

  test('unmount flushes a debounced edit instead of dropping it', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { text: 'loaded' }, content_revision: 2 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 3 });

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'save before leaving'); });
    await act(async () => { root?.unmount(); await Promise.resolve(); });
    root = null;

    expect(saveToolContent).toHaveBeenCalledWith('proj-a', 'notes-1', { text: 'save before leaving' }, 2);
  });

  test('Share in conversation flushes the exact dirty revision before creating its reference', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { text: 'loaded' }, content_revision: 2 });
    vi.mocked(saveToolContent).mockResolvedValue({ content_revision: 3 });
    vi.mocked(createResidentToolArtifactRef).mockResolvedValue({ state: 'ready', artifact_ref: NOTES_ARTIFACT_REF });

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'exact conversation context'); });
    const share = Array.from(host!.querySelectorAll('button')).find(
      button => button.textContent === 'Share in conversation',
    ) as HTMLButtonElement;

    await act(async () => {
      share.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(saveToolContent).toHaveBeenCalledWith(
      'proj-a',
      'notes-1',
      { text: 'exact conversation context' },
      2,
    );
    expect(createResidentToolArtifactRef).toHaveBeenCalledWith('proj-a', 'notes-1', 3);
    expect(vi.mocked(saveToolContent).mock.invocationCallOrder[0]).toBeLessThan(
      vi.mocked(createResidentToolArtifactRef).mock.invocationCallOrder[0],
    );
    expect(dispatchResidentToolHandoff).toHaveBeenCalledWith(expect.objectContaining({
      projectId: 'proj-a',
      artifactRef: NOTES_ARTIFACT_REF,
      intent: 'share',
    }));
  });

  test('an unresolved conflict refuses conversation handoff', async () => {
    vi.mocked(fetchToolContent)
      .mockResolvedValueOnce({ content: { text: 'loaded' }, content_revision: 2 })
      .mockResolvedValueOnce({ content: { text: 'server version' }, content_revision: 9 });
    vi.mocked(saveToolContent).mockRejectedValue(new ConflictError());

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'local edit'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    const share = Array.from(host!.querySelectorAll('button')).find(
      button => button.textContent === 'Share in conversation',
    ) as HTMLButtonElement;
    expect(share.disabled).toBe(true);
    await act(async () => { share.click(); await Promise.resolve(); });

    expect(createResidentToolArtifactRef).not.toHaveBeenCalled();
    expect(dispatchResidentToolHandoff).not.toHaveBeenCalled();
  });

  test('a failed flush never creates a conversation reference', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { text: 'loaded' }, content_revision: 2 });
    vi.mocked(saveToolContent).mockRejectedValue(new Error('storage unavailable'));

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'still unsaved'); });
    const share = Array.from(host!.querySelectorAll('button')).find(
      button => button.textContent === 'Share in conversation',
    ) as HTMLButtonElement;
    await act(async () => {
      share.click();
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(saveToolContent).toHaveBeenCalledTimes(1);
    expect(createResidentToolArtifactRef).not.toHaveBeenCalled();
    expect(dispatchResidentToolHandoff).not.toHaveBeenCalled();
    expect(host!.querySelector('[role="alert"]')?.textContent).toContain('Notes changed while saving');
  });

  test('queues an edit made during a slow save against its returned revision', async () => {
    vi.mocked(fetchToolContent).mockResolvedValue({ content: { text: 'loaded' }, content_revision: 2 });
    let finishFirst!: (value: { content_revision: number }) => void;
    vi.mocked(saveToolContent)
      .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }))
      .mockResolvedValueOnce({ content_revision: 4 });

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'first edit'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);

    await act(async () => { typeInto(textarea, 'second edit'); });
    await act(async () => { finishFirst({ content_revision: 3 }); await Promise.resolve(); await Promise.resolve(); });

    expect(saveToolContent).toHaveBeenCalledTimes(2);
    expect(vi.mocked(saveToolContent).mock.calls[1]).toEqual([
      'proj-a', 'notes-1', { text: 'second edit' }, 3,
    ]);
  });

  test('a slow save and queued edit stay bound to their original tool after an identity switch', async () => {
    vi.mocked(fetchToolContent)
      .mockResolvedValueOnce({ content: { text: 'tool A' }, content_revision: 2 })
      .mockResolvedValueOnce({ content: { text: 'tool B' }, content_revision: 2 });
    let finishFirst!: (value: { content_revision: number }) => void;
    vi.mocked(saveToolContent)
      .mockImplementationOnce(() => new Promise(resolve => { finishFirst = resolve; }))
      .mockResolvedValueOnce({ content_revision: 4 });

    const textareaA = await mount();
    await act(async () => { typeInto(textareaA, 'A first edit'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { typeInto(textareaA, 'A queued edit'); });

    const toolB = { ...SAMPLE_TOOL, id: 'notes-2', slug: 'notes-2' };
    await act(async () => { root?.render(<NotesTool projectId="proj-a" tool={toolB} />); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect((host!.querySelector('textarea') as HTMLTextAreaElement).value).toBe('tool B');

    await act(async () => {
      finishFirst({ content_revision: 3 });
      await Promise.resolve();
      await Promise.resolve();
    });

    expect(vi.mocked(saveToolContent).mock.calls).toEqual([
      ['proj-a', 'notes-1', { text: 'A first edit' }, 2],
      ['proj-a', 'notes-1', { text: 'A queued edit' }, 3],
    ]);
    expect((host!.querySelector('textarea') as HTMLTextAreaElement).value).toBe('tool B');
    expect(host!.textContent).toContain('Saved');
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

  test('a failed conflict reload releases the gate so Retry can reconcile and save', async () => {
    vi.mocked(fetchToolContent)
      .mockResolvedValueOnce({ content: { text: 'loaded' }, content_revision: 2 })
      .mockRejectedValueOnce(new Error('reload failed'))
      .mockResolvedValueOnce({ content: { text: 'server version' }, content_revision: 9 });
    vi.mocked(saveToolContent)
      .mockRejectedValueOnce(new ConflictError())
      .mockRejectedValueOnce(new ConflictError())
      .mockResolvedValueOnce({ content_revision: 10 });

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'keep this local edit'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(textarea.value).toBe('keep this local edit');
    expect(host!.textContent).not.toContain('Another viewer saved a newer version');
    const retryBtn = Array.from(host!.querySelectorAll('button')).find(
      b => /^retry$/i.test(b.textContent || ''),
    ) as HTMLButtonElement;
    expect(retryBtn).toBeTruthy();

    await act(async () => { retryBtn.click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(saveToolContent).toHaveBeenCalledTimes(2);
    expect(host!.textContent).toContain('Another viewer saved a newer version');

    const keepBtn = Array.from(host!.querySelectorAll('button')).find(
      b => /keep mine/i.test(b.textContent || ''),
    ) as HTMLButtonElement;
    await act(async () => { keepBtn.click(); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    expect(saveToolContent).toHaveBeenCalledTimes(3);
    expect(vi.mocked(saveToolContent).mock.calls[2]).toEqual([
      'proj-a', 'notes-1', { text: 'keep this local edit' }, 9,
    ]);
    expect(host!.textContent).toContain('Saved');
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

  test.each([
    ['non-empty', 'server version'],
    ['empty', ''],
  ])('"Discard mine" adopts the %s server baseline and clears dirty state', async (_label, serverText) => {
    vi.mocked(fetchToolContent)
      .mockResolvedValueOnce({ content: { text: 'loaded' }, content_revision: 2 })
      .mockResolvedValueOnce({ content: { text: serverText }, content_revision: 9 });
    vi.mocked(saveToolContent).mockRejectedValueOnce(new ConflictError());

    const textarea = await mount();
    await act(async () => { typeInto(textarea, 'discard this'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1500); });
    await act(async () => { await Promise.resolve(); await Promise.resolve(); });

    const discardBtn = Array.from(host!.querySelectorAll('button')).find(
      b => /discard mine/i.test(b.textContent || ''),
    ) as HTMLButtonElement;
    await act(async () => { discardBtn.click(); });

    expect((host!.querySelector('textarea') as HTMLTextAreaElement).value).toBe(serverText);
    expect(host!.textContent).toContain('Saved');
    expect(host!.textContent).not.toContain('Unsaved changes');
    await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
    expect(saveToolContent).toHaveBeenCalledTimes(1);
  });
});
