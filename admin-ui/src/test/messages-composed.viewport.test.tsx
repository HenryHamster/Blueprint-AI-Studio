import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { OrchestratorHealthBar } from '../components/OrchestratorHealthBar';
import { MessagesPanel as MessagesPanelComponent, type MessagesPanelProps } from '../components/MessagesPanel';

import { expectNoOverflowAt } from './viewport';

type MessagesPanelTestProps = Omit<MessagesPanelProps, 'canOperateProject'> & { canOperateProject?: boolean };
function MessagesPanel(props: MessagesPanelTestProps) {
  return <MessagesPanelComponent canOperateProject={true} {...props} />;
}

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

vi.mock('../api', () => ({
  apiFetch: vi.fn(async (url: string) => {
    if (url.includes('/api/admin/messages')) {
      return new Response(JSON.stringify({ messages: [] }), { status: 200 });
    }
    return new Response('{}', { status: 200 });
  }),
  API_BASE_URL: 'http://localhost',
  MESSAGE_CONTENT_MAX: 20000,
  authHeaders: vi.fn(async () => ({})),
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  // MessagesPanel (rendered here) statically imports the pending-confirmation
  // actions; browser-mode ESM requires the mock to expose every named export the
  // subtree imports or the linker fails at import time.
  acceptPendingConfirmation: vi.fn(async () => ({ status: 'accepted' })),
  dismissPendingConfirmation: vi.fn(async () => ({ status: 'dismissed' })),
}));

const healthBarProps = {
  health: {
    last_cycle_seconds_ago: 90,
    queued_tasks: 3,
    active_tasks: 1,
    pool_info: {},
    deploy_state: {
      status: 'OK',
      rollback_count: 0,
      rollback_reason: null,
      pause_reason: null,
      monitoring_since: null,
      recent_outcomes: [],
    },
    deploy_monitoring: undefined,
  },
  detailedHealth: null,
  apiHealthy: true,
  wsConnected: true,
  lastFetchTime: 1746576000000,
  tasks: [],
  pipelineThrottle: null,
};

describe('MessagesPanel composed with shell panels — viewport regression', () => {
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

  test('no horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <div className="flex flex-col bg-neutral-950 text-neutral-200 p-3 min-h-screen">
            <OrchestratorHealthBar {...healthBarProps} />
            <div className="h-10 flex items-center border-b border-neutral-800 shrink-0" aria-label="navigation" />
            <MessagesPanel
              selectedProjectId="project-123"
              onClose={() => {}}
              addToast={() => {}}
              onMessageCountChange={() => {}}
            />
          </div>,
        );
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 75));
      });
    });

    expect(host!.textContent).toContain('Messages');
  });
});
