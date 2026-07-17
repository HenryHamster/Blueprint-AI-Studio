import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  acknowledgeQueuedResidentToolHandoffs,
  dispatchResidentToolHandoff,
  enqueueResidentToolHandoff,
  loadQueuedResidentToolHandoffEntries,
  loadQueuedResidentToolHandoffs,
  RESIDENT_TOOL_HANDOFF_EVENT,
  type ResidentToolHandoffDetail,
} from './residentToolHandoff';

const detail: ResidentToolHandoffDetail = {
  projectId: 'project-a',
  artifactRef: {
    id: 'artifact-a',
    artifact_ref_id: 'artifact-a',
    tool_id: 'notes-a',
    project_id: 'project-a',
    kind: 'notes',
    revision: 7,
    checksum: 'a'.repeat(64),
    author: 'operator-a',
    timestamp: '2026-07-17T00:00:00Z',
    retrieval: {
      capability: 'inline_bounded_text',
      href: '/api/agent/resident-turn/artifact?artifact_ref_id=artifact-a',
    },
  },
  intent: 'ask',
  prompt: 'Review Notes revision 7.',
};

describe('Resident tool handoff transport', () => {
  beforeEach(() => localStorage.clear());

  it('persists the canonical actor/project handoff even without a mounted listener', () => {
    expect(dispatchResidentToolHandoff(detail)).toBe(true);
    expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([detail]);
    expect(loadQueuedResidentToolHandoffs('operator-b', 'project-a')).toEqual([]);
  });

  it('persists before notifying the mounted conversation fast path', () => {
    const observed = vi.fn(() => {
      expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([detail]);
    });
    window.addEventListener(RESIDENT_TOOL_HANDOFF_EVENT, observed);
    try {
      expect(dispatchResidentToolHandoff(detail)).toBe(true);
      expect(observed).toHaveBeenCalledOnce();
    } finally {
      window.removeEventListener(RESIDENT_TOOL_HANDOFF_EVENT, observed);
    }
  });

  it('fails truthfully and emits nothing when durable storage is unavailable', () => {
    const listener = vi.fn();
    window.addEventListener(RESIDENT_TOOL_HANDOFF_EVENT, listener);
    const storage = vi.spyOn(window.localStorage, 'setItem').mockImplementation(() => {
      throw new DOMException('quota exceeded', 'QuotaExceededError');
    });
    try {
      expect(dispatchResidentToolHandoff(detail)).toBe(false);
      expect(listener).not.toHaveBeenCalled();
    } finally {
      storage.mockRestore();
      window.removeEventListener(RESIDENT_TOOL_HANDOFF_EVENT, listener);
    }
  });

  it('rejects a mismatched ArtifactRef project before persistence or notification', () => {
    const listener = vi.fn();
    window.addEventListener(RESIDENT_TOOL_HANDOFF_EVENT, listener);
    try {
      expect(dispatchResidentToolHandoff({
        ...detail,
        artifactRef: { ...detail.artifactRef, project_id: 'project-b' },
      })).toBe(false);
      expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([]);
      expect(listener).not.toHaveBeenCalled();
      expect(dispatchResidentToolHandoff({
        ...detail,
        artifactRef: { ...detail.artifactRef, author: '' },
      })).toBe(false);
    } finally {
      window.removeEventListener(RESIDENT_TOOL_HANDOFF_EVENT, listener);
    }
  });

  it('stores independent refs without a cross-tab read-modify-write loss', () => {
    const second = {
      ...detail,
      artifactRef: {
        ...detail.artifactRef,
        id: 'artifact-b',
        artifact_ref_id: 'artifact-b',
        revision: 8,
      },
    };
    expect(enqueueResidentToolHandoff('operator-a', 'project-a', detail)).toBe(true);
    expect(enqueueResidentToolHandoff('operator-a', 'project-a', second)).toBe(true);
    expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([detail, second]);
  });

  it('acknowledges only the snapshot version and preserves a same-ref replacement', () => {
    expect(enqueueResidentToolHandoff('operator-a', 'project-a', detail)).toBe(true);
    const snapshot = loadQueuedResidentToolHandoffEntries('operator-a', 'project-a');
    const replacement = { ...detail, intent: 'share' as const, prompt: 'Use Notes revision 7.' };
    expect(enqueueResidentToolHandoff('operator-a', 'project-a', replacement)).toBe(true);

    const scopedKeysBeforeAck = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index))
      .filter(key => key?.startsWith('resident_tool_handoff_entry:operator-a:project-a:'));
    expect(scopedKeysBeforeAck).toHaveLength(2);

    acknowledgeQueuedResidentToolHandoffs('operator-a', 'project-a', snapshot);

    expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([replacement]);
  });

  it('acknowledges one immutable snapshot without consuming an interleaved later event', () => {
    expect(enqueueResidentToolHandoff('operator-a', 'project-a', detail)).toBe(true);
    const snapshot = loadQueuedResidentToolHandoffEntries('operator-a', 'project-a');
    const later = {
      ...detail,
      artifactRef: {
        ...detail.artifactRef,
        id: 'artifact-later',
        artifact_ref_id: 'artifact-later',
        revision: 9,
      },
      prompt: 'Review the later board.',
    };
    expect(enqueueResidentToolHandoff('operator-a', 'project-a', later)).toBe(true);

    acknowledgeQueuedResidentToolHandoffs('operator-a', 'project-a', snapshot);

    expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([later]);
  });

  it('removes malformed and expired scoped entries during queue maintenance', () => {
    const prefix = 'resident_tool_handoff_entry:operator-a:project-a:';
    localStorage.setItem(`${prefix}malformed`, '{');
    localStorage.setItem(`${prefix}expired`, JSON.stringify({
      version: 1,
      entry_id: 'expired',
      queued_at: (Date.now() - 8 * 24 * 60 * 60 * 1000) * 1000,
      detail,
    }));

    expect(loadQueuedResidentToolHandoffEntries('operator-a', 'project-a')).toEqual([]);
    expect(localStorage.getItem(`${prefix}malformed`)).toBeNull();
    expect(localStorage.getItem(`${prefix}expired`)).toBeNull();
  });
});
