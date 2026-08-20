import { afterEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MessageBubble } from './MessageBubble';
import type { MessageBubbleProps, ProjectMessage } from './MessageBubble';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';

function makeMsg(overrides: Partial<ProjectMessage> = {}): ProjectMessage {
  return {
    id: 'msg-1',
    project_id: 'proj-1',
    message_type: 'NOTE',
    from_type: 'system',
    from_task_id: null,
    content: 'Test message content',
    context: {},
    status: 'open',
    resolution: null,
    resolved_at: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function makeProps(overrides: Partial<MessageBubbleProps> = {}): MessageBubbleProps {
  return {
    msg: makeMsg(),
    faded: false,
    readOnly: false,
    replyTarget: null,
    deletingIds: new Set(),
    creatingDirectionIds: new Set(),
    investigatingId: null,
    investigatingIds: new Set(),
    selectMode: false,
    selectedIds: new Set(),
    onToggleSelect: () => {},
    onCreateDirection: () => {},
    onCreateInvestigation: () => {},
    onDelete: () => {},
    onStartInvestigate: () => {},
    onCancelInvestigate: () => {},
    onReply: () => {},
    ...overrides,
  };
}

describe('MessageBubble — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('plain text system message fits 420px without overflow, truncation, or small targets', async () => {
    await expectNoOverflowAt(420, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageBubble
            {...makeProps({
              msg: makeMsg({ from_type: 'system', content: 'System notification message' }),
            })}
          />,
        );
      });
    });
    // Message body renders through MarkdownContent, which is code-split via
    // React.lazy (PR #13146). On the first render in this file the lazy chunk
    // suspends, so the body text commits a tick after the initial paint. Poll
    // until it settles rather than asserting mid-suspense.
    await vi.waitFor(() => {
      expect(host?.textContent).toContain('System notification message');
    });
    await expectNoTextTruncationAt(420, async () => {});
    await expectMinTouchTargetAt(420, async () => {});
  });

  test('markdown agent message fits 420px without overflow, truncation, or small targets', async () => {
    await expectNoOverflowAt(420, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageBubble
            {...makeProps({
              msg: makeMsg({
                from_type: 'agent',
                content: '# Findings\n\n- Item one\n- Item two\n\nMore **bold** text here',
              }),
            })}
          />,
        );
      });
    });
    if (host) expect(host.textContent).toContain('Findings');
    await expectNoTextTruncationAt(420, async () => {});
    await expectMinTouchTargetAt(420, async () => {});
  });

  test('user message (plain text) fits 420px without overflow, truncation, or small targets', async () => {
    await expectNoOverflowAt(420, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageBubble
            {...makeProps({
              msg: makeMsg({
                from_type: 'user',
                message_type: 'QUESTION',
                content: 'What is the status of this component?',
              }),
            })}
          />,
        );
      });
    });
    if (host) expect(host.textContent).toContain('What is the status of this component?');
    await expectNoTextTruncationAt(420, async () => {});
    await expectMinTouchTargetAt(420, async () => {});
  });

  test('operator chat message with Strategic context receipt fits 375px without overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageBubble
            {...makeProps({
              presentation: 'chat',
              actionMode: 'reply-only',
              msg: makeMsg({
                from_type: 'operator',
                content: 'Use the current context.',
                context: {
                  strategic_session: {
                    version: 1,
                    interaction_mode: 'continuous',
                    active_surface: 'pipeline',
                    context_package_id: 'strategic-context:project-123',
                    selected_refs: [
                      {
                        kind: 'knowledge_document',
                        id: 'knowledge:operator-notes',
                        label: 'Operator notes for the local Strategic workday session',
                      },
                    ],
                  },
                },
              }),
            })}
          />,
        );
      });
    });
    if (host) expect(host.textContent).toContain('Refs attached');
    if (host) expect(host.textContent).toContain('Operator notes');
    await expectNoTextTruncationAt(375, async () => {});
    await expectMinTouchTargetAt(375, async () => {});
  });

  test('strategic message with markdown links fits 420px without overflow, truncation, or small targets', async () => {
    await expectNoOverflowAt(420, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageBubble
            {...makeProps({
              msg: makeMsg({
                from_type: 'strategic',
                content: 'Check out [this link](https://example.com) for more info.',
              }),
            })}
          />,
        );
      });
    });
    if (host) expect(host.textContent).toContain('Check out');
    if (host) expect(host.textContent).toContain('for more info');
    await expectNoTextTruncationAt(420, async () => {});
    await expectMinTouchTargetAt(420, async () => {});
  });

  test('chat task metadata fits 375px without overflow', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageBubble
            {...makeProps({
              presentation: 'chat',
              actionMode: 'reply-only',
              onOpenTask: () => {},
              msg: makeMsg({
                from_type: 'strategic',
                from_task_id: 'task-abcdef1234567890',
                content: 'This is a long Strategic task-linked update that should wrap cleanly while the compact task metadata remains usable.',
              }),
            })}
          />,
        );
      });
    });
    if (host) expect(host.textContent).toContain('task-abc');
  });

  test('chat user bubble uses dense developer-tool styling', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <MessageBubble
          {...makeProps({
            presentation: 'chat',
            actionMode: 'reply-only',
            msg: makeMsg({
              from_type: 'user',
              content: 'Tighten this up.',
            }),
          })}
        />,
      );
    });

    const innerBubble = host.querySelector('[data-testid="message-bubble"] > div > div');
    expect(innerBubble?.className).toContain('rounded-md');
    expect(innerBubble?.className).toContain('px-3');
    expect(innerBubble?.className).not.toContain('rounded-lg');
    expect(innerBubble?.className).not.toContain('0_6px_24px');
  });
});
