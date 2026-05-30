import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EventVisibilityMenu } from './EventVisibilityMenu';
import { type ProjectMessage } from './MessageBubble';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

function msg(id: string, eventType: string): ProjectMessage {
  return {
    id,
    project_id: 'p1',
    message_type: 'NOTE',
    from_type: 'agent',
    from_task_id: null,
    content: 'c',
    context: { event_type: eventType },
    status: 'open',
    resolution: null,
    resolved_at: null,
    created_at: new Date().toISOString(),
  };
}

describe('EventVisibilityMenu — viewport regression', () => {
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

  test('closed — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<EventVisibilityMenu open={false} onOpenChange={() => {}} messages={[]} eventPrefs={{}} onUpdatePref={() => {}} onResetAll={() => {}} />);
      });
    });
  });

  test('open with event rows — no overflow at 375px', async () => {
    const messages = [msg('1', 'AGENT_TIMEOUT_recovery_attempt'), msg('2', 'pipeline_regression'), msg('3', 'pipeline_regression')];
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<EventVisibilityMenu open={true} onOpenChange={() => {}} messages={messages} eventPrefs={{ pipeline_regression: 'mute' }} onUpdatePref={() => {}} onResetAll={() => {}} />);
      });
    });
  });
});
