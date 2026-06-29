import { afterEach, describe, test, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act, createRef } from 'react';
import { MessagesComposer } from './MessagesComposer';
import type { StrategicContextDescriptorModel } from '../utils/strategicContextDescriptor';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

async function renderComposer(
  root: Root,
  newType: string,
  lastArming: 'steering' | 'passive_note' | 'routine_event' | 'not_armed' | null = null,
  strategistBusy = false,
  chrome: 'full' | 'minimal' = 'full',
  strategicContext: StrategicContextDescriptorModel | null = null,
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
        strategicContext={strategicContext}
        onOpenStrategicContext={() => {}}
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
    expect(host!.textContent).toContain('Send');
    expect(host!.textContent).not.toContain('Strategic');
    expect(host!.textContent).not.toContain('reaches the strategist soon');
    expect(host!.textContent).not.toContain('won’t wake the strategist');
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
    expect(host!.textContent).toContain('Send');
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
    expect(host!.textContent).toContain('Send');
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
});
