import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, createRef } from 'react';
import { MessagesComposer } from './MessagesComposer';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

async function renderComposer(
  root: Root,
  newType: string,
  lastArming: 'steering' | 'passive_note' | 'routine_event' | null = null,
  strategistBusy = false,
  chrome: 'full' | 'minimal' = 'full',
) {
  const ref = createRef<HTMLTextAreaElement>();
  await act(async () => {
    root.render(
      <MessagesComposer
        textareaRef={ref}
        newType={newType}
        newContent=""
        submitting={false}
        replyTo={null}
        lastArming={lastArming}
        strategistBusy={strategistBusy}
        onChangeType={() => {}}
        onChangeContent={() => {}}
        onCancelReply={() => {}}
        onSubmit={() => {}}
        chrome={chrome}
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

  test('minimal chat composer hides type selector while preserving Strategic steering hint', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await renderComposer(r, 'FEEDBACK', null, false, 'minimal');
    });
    expect(host!.textContent).toContain('Strategic');
    expect(host!.textContent).toContain('Send');
    expect(host!.textContent).toContain('reaches the strategist soon');
    expect(host!.textContent).not.toContain('won’t wake the strategist');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });
});
