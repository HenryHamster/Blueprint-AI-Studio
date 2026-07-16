import type { ProjectMessage } from '../types/messages';

export function messageHasClientIdentity(message: ProjectMessage, identity: string): boolean {
  return message.idempotency_key === identity
    || message.client_idempotency_key === identity
    || message.context?.idempotency_key === identity;
}

export function mergeCanonicalConversationMessage(
  messages: ProjectMessage[],
  message: ProjectMessage,
): ProjectMessage[] {
  const canonicalIdentity = message.client_idempotency_key ?? message.idempotency_key ?? null;
  const withoutDuplicate = messages.filter(existing => (
    existing.id !== message.id
    && !(canonicalIdentity && messageHasClientIdentity(existing, canonicalIdentity))
  ));
  return [...withoutDuplicate, message];
}
