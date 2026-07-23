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
      href: '/api/admin/projects/project-a/artifact-refs/artifact-a',
    },
  },
  intent: 'ask',
  prompt: 'Review Notes revision 7.',
};

describe('conversation tool handoff transport', () => {
  beforeEach(() => localStorage.clear());

  it('persists the canonical actor/project handoff without a mounted listener', () => {
    expect(dispatchResidentToolHandoff(detail)).toBe(true);
    expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([detail]);
    expect(loadQueuedResidentToolHandoffs('operator-b', 'project-a')).toEqual([]);
  });

  it('keeps actor and project queues isolated in durable storage', () => {
    expect(enqueueResidentToolHandoff('operator-a', 'project-a', detail)).toBe(true);
    const otherProject = {
      ...detail,
      projectId: 'project-b',
      artifactRef: {
        ...detail.artifactRef,
        project_id: 'project-b',
      },
    };
    expect(enqueueResidentToolHandoff('operator-a', 'project-b', otherProject)).toBe(true);
    expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([detail]);
    expect(loadQueuedResidentToolHandoffs('operator-a', 'project-b')).toEqual([otherProject]);
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

  it('rejects a mismatched project or missing author before persistence', () => {
    expect(dispatchResidentToolHandoff({
      ...detail,
      artifactRef: { ...detail.artifactRef, project_id: 'project-b' },
    })).toBe(false);
    expect(dispatchResidentToolHandoff({
      ...detail,
      artifactRef: { ...detail.artifactRef, author: '' },
    })).toBe(false);
    expect(loadQueuedResidentToolHandoffs('operator-a', 'project-a')).toEqual([]);
  });

  it('acknowledges only the snapshot and preserves an interleaved later event', () => {
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

  it('removes malformed and expired scoped entries during maintenance', () => {
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
