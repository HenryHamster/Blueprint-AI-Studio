import { vi } from 'vitest';

export function createStrategicStateApiMock(overrides: Record<string, unknown> = {}) {
  const NOT_MODIFIED = { notModified: true } as const;
  const CONVERSATION_NOT_MODIFIED = { notModified: true } as const;
  const base: Record<keyof typeof import('../strategicStateApi'), unknown> = {
    STRATEGIC_STATE_NOT_MODIFIED: NOT_MODIFIED,
    isStrategicStateNotModified: (r: unknown) => r === NOT_MODIFIED,
    STRATEGIC_CONVERSATION_NOT_MODIFIED: CONVERSATION_NOT_MODIFIED,
    isStrategicConversationNotModified: (r: unknown) => r === CONVERSATION_NOT_MODIFIED,
    fetchStrategicState: vi.fn(),
    fetchStrategicLiveness: vi.fn(),
    fetchStrategicStateWithRevision: vi.fn(),
    fetchStrategicWorkbenchState: vi.fn(),
    updateStrategicWorkbenchState: vi.fn(),
    fetchStrategicSession: vi.fn(),
    startStrategicSession: vi.fn(),
    stopStrategicSession: vi.fn(),
    startStrategicCall: vi.fn(),
    fetchStrategicCall: vi.fn(),
    endStrategicCall: vi.fn(),
    reportStrategicCallPlayout: vi.fn(),
    fetchStrategicConversation: vi.fn(),
    findStrategicMessageByClientIdentity: vi.fn(),
    createStrategicSessionMessage: vi.fn(),
    StrategicSessionMessageCreateError: class StrategicSessionMessageCreateError extends Error {
      readonly status = 500;
    },
    checkInStrategicSession: vi.fn(),
    executeStrategicWorkSessionCommand: vi.fn(),
    getMessageAttachmentUploadUrl: vi.fn(),
    deleteMessageAttachmentUpload: vi.fn(),
    getMessageAttachmentDownloadUrl: vi.fn(),
    invalidateMessageAttachmentDownloadUrl: vi.fn(),
  };
  return { ...base, ...overrides };
}
