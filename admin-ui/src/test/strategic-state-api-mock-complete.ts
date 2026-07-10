import { vi } from 'vitest';

export function createStrategicStateApiMock(overrides: Record<string, unknown> = {}) {
  const base: Record<keyof typeof import('../strategicStateApi'), unknown> = {
    fetchStrategicState: vi.fn(),
    fetchStrategicStateWithRevision: vi.fn(),
    fetchStrategicWorkbenchState: vi.fn(),
    updateStrategicWorkbenchState: vi.fn(),
    fetchStrategicSession: vi.fn(),
    fetchStrategicConversation: vi.fn(),
    createStrategicSessionMessage: vi.fn(),
    checkInStrategicSession: vi.fn(),
    executeStrategicWorkSessionCommand: vi.fn(),
    startStrategicSession: vi.fn(),
    stopStrategicSession: vi.fn(),
    getMessageAttachmentUploadUrl: vi.fn(),
  };
  return { ...base, ...overrides };
}
