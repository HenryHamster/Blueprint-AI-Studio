import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, createRef } from 'react';
import { MessagesComposer } from './MessagesComposer';
import type { StrategicContextDescriptorModel } from '../utils/strategicContextDescriptor';
import type { PendingAttachment } from './MessagesComposer';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';
import { MESSAGE_CONTENT_MAX } from '../api';

async function renderComposer(
  root: Root,
  newType: string,
  lastArming: 'steering' | 'passive_note' | 'routine_event' | 'not_armed' | null = null,
  strategistBusy = false,
  chrome: 'full' | 'minimal' = 'full',
  strategicContext: StrategicContextDescriptorModel | null = null,
  onOpenCommandPalette?: () => void,
  newContent = '',
  pendingAttachments: PendingAttachment[] = [],
  onAddAttachment?: (file: File) => void,
  onRemoveAttachment?: (id: string) => void,
) {
  const ref = createRef<HTMLTextAreaElement>();
  await act(async () => {
    root.render(
      <MessagesComposer
        textareaRef={ref}
        newType={newType}
        newContent={newContent}
        submitting={false}
        replyTo={null}
        lastArming={lastArming}
        strategistBusy={strategistBusy}
        onChangeType={() => {}}
        onChangeContent={() => {}}
        onCancelReply={() => {}}
        onSubmit={() => {}}
        chrome={chrome}
        strategicContext={strategicContext}
        onOpenStrategicContext={() => {}}
        onOpenCommandPalette={onOpenCommandPalette}
        pendingAttachments={pendingAttachments}
        onAddAttachment={onAddAttachment}
        onRemoveAttachment={onRemoveAttachment}
      />,
    );
  });
}

describe('MessagesComposer — steering hint viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('NOTE (passive) hint fits 375px without overflow, truncation, or small targets', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'NOTE');
    });
    expect(host!.textContent).toContain('won’t wake the strategist');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('FEEDBACK (steering) hint fits 375px without overflow, truncation, or small targets', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK');
    });
    expect(host!.textContent).toContain('reaches the strategist soon');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('busy-strategist steering hint (queued) fits 375px without overflow, truncation, or small targets', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      // FEEDBACK while a cycle is running → pre-send hint says the steer queues.
      await renderComposer(r, 'FEEDBACK', null, true);
    });
    expect(host!.textContent).toContain('queues for the next cycle');
    expect(host!.textContent).not.toContain('reaches the strategist soon');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('last-send routine_event outcome overrides the type-based prediction', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      // FEEDBACK would predict steering, but the server reported routine_event
      // for the last send — the real outcome must win.
      await renderComposer(r, 'FEEDBACK', 'routine_event');
    });
    expect(host!.textContent).toContain('Routine event');
    expect(host!.textContent).toContain('won’t wake the strategist');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('minimal chat composer keeps send affordance without persistent steering chrome', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', null, false, 'minimal');
    });
    expect(host!.querySelector('button[aria-label="Send message"]')).not.toBeNull();
    expect(host!.textContent).not.toContain('Strategic');
    expect(host!.textContent).not.toContain('reaches the strategist soon');
    expect(host!.textContent).not.toContain('won’t wake the strategist');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('minimal chat composer exposes command palette beside the input', async () => {
    const onOpenCommandPalette = vi.fn();
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, onOpenCommandPalette);
    });
    if (!host) throw new Error('Expected composer viewport test host to be mounted');
    const commandButton = host.querySelector<HTMLButtonElement>('button[title="Open command palette (Ctrl+K)"]');
    expect(commandButton).not.toBeNull();
    expect(commandButton?.className).toContain('border-neutral-800');
    expect(commandButton?.className).not.toContain('bg-[#080c11]');
    expect(host.querySelector('[data-testid="strategic-composer-box"]')?.className).toContain('gap-1');
    await act(async () => { commandButton?.click(); });
    expect(onOpenCommandPalette).toHaveBeenCalledOnce();
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('minimal chat composer still shows queued state when the strategist is busy', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', null, true, 'minimal');
    });
    expect(host!.querySelector('button[aria-label="Send message"]')).not.toBeNull();
    expect(host!.textContent).toContain('queues for the next cycle');
    expect(host!.textContent).not.toContain('Strategic');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('minimal chat composer suppresses routine post-send hint chrome', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', 'routine_event', false, 'minimal');
    });
    expect(host!.querySelector('button[aria-label="Send message"]')).not.toBeNull();
    expect(host!.textContent).not.toContain('Routine event');
    expect(host!.textContent).not.toContain('won’t wake the strategist');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('minimal chat composer prefers busy queued state over routine post-send hint', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', 'routine_event', true, 'minimal');
    });
    expect(host!.textContent).toContain('queues for the next cycle');
    expect(host!.textContent).not.toContain('Routine event');
    expect(host!.textContent).not.toContain('won’t wake the strategist');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('minimal chat composer keeps not-armed warning visible', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', 'not_armed', false, 'minimal');
    });
    expect(host!.textContent).toContain('Couldn’t wake the strategist');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('char counter hidden below 80% of MESSAGE_CONTENT_MAX', async () => {
    const content = 'x'.repeat(Math.floor(MESSAGE_CONTENT_MAX * 0.79));
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'NOTE', null, false, 'full', null, undefined, content);
    expect(host!.textContent).not.toContain(`/ ${MESSAGE_CONTENT_MAX.toLocaleString()}`);
  });

  test('char counter appears at >80% of MESSAGE_CONTENT_MAX', async () => {
    const content = 'x'.repeat(Math.floor(MESSAGE_CONTENT_MAX * 0.85));
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'NOTE', null, false, 'full', null, undefined, content);
    expect(host!.textContent).toContain(`/ ${MESSAGE_CONTENT_MAX.toLocaleString()}`);
  });

  test('send button disabled and counter shown in red when content exceeds MESSAGE_CONTENT_MAX', async () => {
    const content = 'x'.repeat(MESSAGE_CONTENT_MAX + 1);
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'NOTE', null, false, 'full', null, undefined, content);
    const sendBtn = host!.querySelector<HTMLButtonElement>('button[aria-label="Send message"]');
    expect(sendBtn?.disabled).toBe(true);
    const counterEl = Array.from(host!.querySelectorAll('p')).find(p =>
      p.textContent?.includes(`/ ${MESSAGE_CONTENT_MAX.toLocaleString()}`)
    );
    expect(counterEl).not.toBeNull();
    expect(counterEl?.className).toContain('text-red-400');
  });

  test('minimal chat composer context descriptor fits 375px without overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', null, false, 'minimal', {
        modeLabel: 'Workday',
        surfaceLabel: 'Pipeline',
        contextLabel: 'Context ready',
        contextTone: 'ready',
        refCount: 1,
        refs: [{
          id: 'knowledge_document:operator-notes',
          kind: 'knowledge_document',
          label: 'Operator notes for the local Strategic workday session',
          status: 'active',
        }],
      });
    });
    if (!host) throw new Error('Expected host to be mounted');
    expect(host.textContent).toContain('Context');
    expect(host.textContent).toContain('Workday');
    expect(host.textContent).toContain('Pipeline');
    expect(host.textContent).toContain('Operator notes');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('minimal chat composer short context descriptor keeps a mobile tap target', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', null, false, 'minimal', {
        modeLabel: 'Chat',
        surfaceLabel: 'Strategic',
        contextLabel: 'Context captured',
        contextTone: 'ready',
        refCount: 0,
        refs: [],
      });
    });
    const descriptor = host?.querySelector<HTMLElement>('[data-testid="strategic-context-descriptor"]');
    expect(descriptor).not.toBeNull();
    expect(descriptor!.getBoundingClientRect().height).toBeGreaterThanOrEqual(44);
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('minimal chat composer with pending attachment preview fits 1280px without overflow', async () => {
    await expectNoOverflowAt(1280, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, undefined, '', [
        { id: '1', file: new File([], 'screenshot.png', { type: 'image/png' }), previewUrl: null, mime_type: 'image/png', name: 'screenshot.png', size_bytes: 123456 },
        { id: '2', file: new File([], 'report.pdf', { type: 'application/pdf' }), previewUrl: null, mime_type: 'application/pdf', name: 'report.pdf', size_bytes: 789012 },
      ]);
    });
    await expectMinTouchTargetAt(1280, async () => {});
  });

  test('minimal chat composer with pending attachment preview fits 375px without overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, undefined, '', [
        { id: 'a', file: new File([], 'screenshot.png', { type: 'image/png' }), previewUrl: null, mime_type: 'image/png', name: 'screenshot.png', size_bytes: 123456 },
        { id: 'b', file: new File([], 'report.pdf', { type: 'application/pdf' }), previewUrl: null, mime_type: 'application/pdf', name: 'report.pdf', size_bytes: 789012 },
        { id: 'c', file: new File([], 'notes.txt', { type: 'text/plain' }), previewUrl: null, mime_type: 'text/plain', name: 'notes.txt', size_bytes: 1024 },
      ]);
    });
    await expectMinTouchTargetAt(375, async () => {});
  });
});

describe('MessagesComposer — attachment interaction', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('remove attachment button calls onRemoveAttachment with the correct attachment id', async () => {
    const onRemoveAttachment = vi.fn();
    host = document.createElement('div');
    host.style.width = '375px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, undefined, '', [
      { id: 'first-id', file: new File([], 'first.png', { type: 'image/png' }), previewUrl: null, mime_type: 'image/png', name: 'first.png', size_bytes: 1000 },
      { id: 'second-id', file: new File([], 'second.pdf', { type: 'application/pdf' }), previewUrl: null, mime_type: 'application/pdf', name: 'second.pdf', size_bytes: 2000 },
    ], undefined, onRemoveAttachment);

    // Find and click the remove button on the second attachment
    const removeButtons = host.querySelectorAll<HTMLButtonElement>('button[aria-label^="Remove"]');
    expect(removeButtons.length).toBe(2);

    await act(async () => {
      removeButtons[1].click();
    });

    expect(onRemoveAttachment).toHaveBeenCalledOnce();
    expect(onRemoveAttachment).toHaveBeenCalledWith('second-id');
  });

  test('remove button aria-label reflects the file name', async () => {
    host = document.createElement('div');
    host.style.width = '375px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, undefined, '', [
      { id: '3', file: new File([], 'my-notes.txt', { type: 'text/plain' }), previewUrl: null, mime_type: 'text/plain', name: 'my-notes.txt', size_bytes: 500 },
    ]);

    const removeBtn = host.querySelector<HTMLButtonElement>('button[aria-label="Remove my-notes.txt"]');
    expect(removeBtn).toBeTruthy();
  });

  test('handlePaste calls onAddAttachment when files are pasted and prevents default', async () => {
    const onAddAttachment = vi.fn();
    host = document.createElement('div');
    host.style.width = '375px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, undefined, '', [], onAddAttachment);

    const textarea = host?.querySelector('textarea');
    expect(textarea).toBeTruthy();

    const file = new File(['pasted content'], 'pasted.txt', { type: 'text/plain' });
    const dt = new DataTransfer();
    dt.items.add(file);
    const pasteEvent = new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true });

    await act(async () => {
      textarea!.dispatchEvent(pasteEvent);
    });

    expect(onAddAttachment).toHaveBeenCalledOnce();
    expect(onAddAttachment).toHaveBeenCalledWith(file);
    expect(pasteEvent.defaultPrevented).toBe(true);
  });

  test('handlePaste does not prevent default or call onAddAttachment when no files are pasted', async () => {
    const onAddAttachment = vi.fn();
    host = document.createElement('div');
    host.style.width = '375px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, undefined, '', [], onAddAttachment);

    const textarea = host?.querySelector('textarea');
    expect(textarea).toBeTruthy();

    // Paste with no clipboardData (no files) — handler should return early.
    const pasteEvent = new ClipboardEvent('paste', { bubbles: true, cancelable: true });

    await act(async () => {
      textarea!.dispatchEvent(pasteEvent);
    });

    expect(onAddAttachment).not.toHaveBeenCalled();
    expect(pasteEvent.defaultPrevented).toBe(false);
  });

  test('handleDrop calls onAddAttachment with dropped files', async () => {
    const onAddAttachment = vi.fn();
    host = document.createElement('div');
    host.style.width = '375px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, undefined, '', [], onAddAttachment);

    const composerBox = host?.querySelector('[data-testid="strategic-composer-box"]');
    expect(composerBox).toBeTruthy();

    const file = new File(['dropped content'], 'dropped.txt', { type: 'text/plain' });
    const dt = new DataTransfer();
    dt.items.add(file);
    const dropEvent = new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true });

    await act(async () => {
      composerBox!.dispatchEvent(dropEvent);
    });

    expect(onAddAttachment).toHaveBeenCalledOnce();
    expect(onAddAttachment).toHaveBeenCalledWith(file);
  });

  test('dragOver and dragLeave toggle the drag-over border class', async () => {
    host = document.createElement('div');
    host.style.width = '375px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'FEEDBACK', null, false, 'minimal');

    const composerBox = host!.querySelector('[data-testid="strategic-composer-box"]')!;
    expect(composerBox).toBeTruthy();

    // Initially no drag-over style
    expect(composerBox.className).not.toContain('border-emerald-500/30');

    // Dispatch dragover
    await act(async () => {
      composerBox.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true }));
    });
    expect(composerBox.className).toContain('border-emerald-500/30');

    // Dispatch dragleave
    await act(async () => {
      composerBox.dispatchEvent(new DragEvent('dragleave', { bubbles: true, cancelable: true }));
    });
    expect(composerBox.className).not.toContain('border-emerald-500/30');
  });

  test('handleDrop clears drag-over state', async () => {
    const onAddAttachment = vi.fn();
    host = document.createElement('div');
    host.style.width = '375px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await renderComposer(r, 'FEEDBACK', null, false, 'minimal', null, undefined, '', [], onAddAttachment);

    const composerBox = host!.querySelector('[data-testid="strategic-composer-box"]')!;

    // First enter drag-over state
    await act(async () => {
      composerBox.dispatchEvent(new DragEvent('dragover', { bubbles: true, cancelable: true }));
    });
    expect(composerBox.className).toContain('border-emerald-500/30');

    // Drop with files
    const file = new File(['content'], 'file.txt', { type: 'text/plain' });
    const dt = new DataTransfer();
    dt.items.add(file);
    await act(async () => {
      composerBox.dispatchEvent(new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    });

    // State cleared after drop
    expect(composerBox.className).not.toContain('border-emerald-500/30');
  });
});
