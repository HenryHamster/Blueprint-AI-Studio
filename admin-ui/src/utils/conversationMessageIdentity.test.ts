import { describe, expect, test } from 'vitest';
import type { ProjectMessage } from '../types/messages';
import { mergeCanonicalConversationMessage } from './conversationMessageIdentity';

function message(id: string, identity: string, content = 'same text'): ProjectMessage {
  return {
    id, project_id: 'project-1', message_type: 'FEEDBACK', from_type: 'user',
    from_task_id: null, content, context: {}, status: 'open', resolution: null,
    resolved_at: null, created_at: null, idempotency_key: identity,
  };
}

describe('canonical conversation identity', () => {
  test('preserves two intentional messages with identical text and different identities', () => {
    expect(mergeCanonicalConversationMessage([message('one', 'send-1')], message('two', 'send-2')))
      .toHaveLength(2);
  });

  test('reconciles one optimistic retry exactly once by identity', () => {
    const optimistic = message('optimistic:send-1', 'send-1');
    const canonical = message('canonical-1', 'send-1');
    expect(mergeCanonicalConversationMessage([optimistic], canonical)).toEqual([canonical]);
  });
});
