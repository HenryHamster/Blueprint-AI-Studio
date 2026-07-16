import { describe, expect, test } from 'vitest';
import { loadPendingConversationSend, savePendingConversationSend } from './conversationSendState';

describe('pending conversation send persistence', () => {
  test('preserves needs_retry draft, reply, attachments, and source chips across remount', () => {
    const storage = window.sessionStorage;
    const key = 'pending-send:test';
    const pending = {
      actor_user_id: 'actor-1', project_id: 'project-1',
      key: 'send-1', content: 'Please verify this', created_at: '2026-07-15T00:00:00Z',
      message_type: 'FEEDBACK', state: 'needs_retry' as const,
      reply_to_message_id: 'message-4',
      reply_to: { id: 'message-4', content: 'Which run?', from: 'Strategic', message_type: 'QUESTION', status: 'open' },
      context: {
        attachments: [{ type: 'attachment', mime_type: 'text/plain', name: 'run.txt', url: 'https://files/run.txt' }],
        source_refs: [{ kind: 'task', id: 'task-9', label: 'Runner check' }],
      },
    };

    savePendingConversationSend(storage, key, pending);
    expect(loadPendingConversationSend(storage, key)).toMatchObject({
      ...pending,
      context: {
        ...pending.context,
        attachments: [{ type: 'attachment', mime_type: 'text/plain', name: 'run.txt' }],
      },
    });
    storage.removeItem(key);
  });
});
