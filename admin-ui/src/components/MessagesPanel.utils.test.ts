import { describe, it, expect, beforeEach, afterEach, test, vi } from 'vitest';
import {
  loadEventPrefs,
  saveEventPrefs,
  type EventPref,
  eventPrefsKey,
  groupRoutineMessages,
  outcomeBreakdown,
  insertTimeDividers,
  collapseStaleRoutineRows,
  STALE_ROUTINE_COLLAPSE_AGE_MS,
  isFailingGroup,
  ESCALATION_EVENT_TYPES,
  ESCALATION_BADGE_LABELS,
  HIGH_SIGNAL_NOTE_BADGE_LABELS,
  HIGH_SIGNAL_NOTE_EVENTS,
  CRITICAL_NOTE_EVENTS,
  classifySignalLevel,
  getToastSeverity,
  loadSavedViews,
  saveSavedViews,
  savedViewsKey,
  SAVED_VIEWS_CAP,
  type SavedView,
  loadMuteWindow,
  saveMuteWindow,
  clearMuteWindow,
  isMuteWindowActive,
  formatMuteCountdown,
  muteWindowKey,
  loadLastSeen,
  saveLastSeen,
  resolveLastSeen,
  resolveNoisePrefs,
  fetchServerNoisePrefs,
  saveServerNoisePrefs,
  fetchServerSavedViews,
  saveServerSavedViews,
  resolveServerSavedViews,
  fetchServerTempMutes,
  saveServerTempMutes,
  resolveServerTempMutes,
  fetchServerMuteWindow,
  saveServerMuteWindow,
  resolveServerMuteWindow,
  type TemporaryMutes,
  type MuteWindow,
  countHighSignalMessages,
  countNewSinceLastVisit,
  fetchBackendLastSeen,
  type RoutineRow,
} from './MessagesPanel.utils';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock();
});

import { apiFetch } from '../api';

describe('MessagesPanel.utils - Event Preferences', () => {
  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    localStorage.clear();
  });

  it('load with no key returns empty object', () => {
    const prefs = loadEventPrefs('user1', 'project1');
    expect(prefs).toEqual({});
  });

  it('save then load round-trips correctly', () => {
    const userId = 'user1';
    const projectId = 'project1';
    const testPrefs: Record<string, EventPref> = {
      pr_merged: 'mute',
      failure_alert: 'surfaced',
      cycle_summary: 'routine',
    };

    saveEventPrefs(userId, projectId, testPrefs);
    const loaded = loadEventPrefs(userId, projectId);

    expect(loaded).toEqual(testPrefs);
  });

  it('two project IDs hold independent prefs', () => {
    const userId = 'user1';
    const project1Prefs: Record<string, EventPref> = { pr_merged: 'mute' };
    const project2Prefs: Record<string, EventPref> = { pr_merged: 'surfaced' };

    saveEventPrefs(userId, 'project1', project1Prefs);
    saveEventPrefs(userId, 'project2', project2Prefs);

    const loaded1 = loadEventPrefs(userId, 'project1');
    const loaded2 = loadEventPrefs(userId, 'project2');

    expect(loaded1).toEqual(project1Prefs);
    expect(loaded2).toEqual(project2Prefs);
  });

  it('different users have independent prefs for same project', () => {
    const projectId = 'project1';
    const user1Prefs: Record<string, EventPref> = { pr_merged: 'mute' };
    const user2Prefs: Record<string, EventPref> = { pr_merged: 'surfaced' };

    saveEventPrefs('user1', projectId, user1Prefs);
    saveEventPrefs('user2', projectId, user2Prefs);

    const loaded1 = loadEventPrefs('user1', projectId);
    const loaded2 = loadEventPrefs('user2', projectId);

    expect(loaded1).toEqual(user1Prefs);
    expect(loaded2).toEqual(user2Prefs);
  });

  it('migration: loads from legacy global key if project-scoped key absent', () => {
    const legacyKey = 'messages_panel_event_prefs';
    const legacyPrefs: Record<string, EventPref> = { pr_merged: 'mute' };

    localStorage.setItem(legacyKey, JSON.stringify(legacyPrefs));

    const loaded = loadEventPrefs('user1', 'project1');
    expect(loaded).toEqual(legacyPrefs);

    // Verify legacy key is still there (not deleted during migration)
    expect(localStorage.getItem(legacyKey)).toBe(JSON.stringify(legacyPrefs));
  });

  it('migration: prefers project-scoped key over legacy key', () => {
    const legacyKey = 'messages_panel_event_prefs';
    const legacyPrefs: Record<string, EventPref> = { pr_merged: 'mute' };
    const projectPrefs: Record<string, EventPref> = { pr_merged: 'surfaced' };

    localStorage.setItem(legacyKey, JSON.stringify(legacyPrefs));
    saveEventPrefs('user1', 'project1', projectPrefs);

    const loaded = loadEventPrefs('user1', 'project1');
    expect(loaded).toEqual(projectPrefs);
  });

  it('save with empty prefs removes the key', () => {
    const userId = 'user1';
    const projectId = 'project1';

    saveEventPrefs(userId, projectId, { pr_merged: 'mute' });
    expect(localStorage.getItem(eventPrefsKey(userId, projectId))).not.toBeNull();

    saveEventPrefs(userId, projectId, {});
    expect(localStorage.getItem(eventPrefsKey(userId, projectId))).toBeNull();
  });

  it('handles undefined userId and projectId gracefully', () => {
    const testPrefs: Record<string, EventPref> = { pr_merged: 'mute' };

    saveEventPrefs(undefined, undefined, testPrefs);
    const loaded = loadEventPrefs(undefined, undefined);

    expect(loaded).toEqual(testPrefs);
  });

  it('handles null userId and projectId gracefully', () => {
    const testPrefs: Record<string, EventPref> = { pr_merged: 'mute' };

    saveEventPrefs(null, null, testPrefs);
    const loaded = loadEventPrefs(null, null);

    expect(loaded).toEqual(testPrefs);
  });

  it('handles invalid JSON gracefully', () => {
    const key = eventPrefsKey('user1', 'project1');
    localStorage.setItem(key, 'invalid json {');

    const loaded = loadEventPrefs('user1', 'project1');
    expect(loaded).toEqual({});
  });
});

// ── Escalation event invariants ────────────────────────────────────────────
//
// Backend escalation emitters in notification_dispatch.py post these event_types
// to surface that the autonomous loop's automatic remediation has been escalated
// for operator attention. The frontend wires high-signal classification, error
// toast severity, and a red ContextBadge through ONE source of truth
// (ESCALATION_EVENT_TYPES) so a new escalation type added there can't drift
// out of any one of those three behaviors.

describe('escalation event invariants', () => {
  const KNOWN_ESCALATIONS = [
    'auto_fix_escalated',
    'residual_fix_escalated',
    'residual_fix_audit_deferred',
    'remediation_escalated',
  ] as const;

  test('all four canonical escalation types are present in ESCALATION_EVENT_TYPES', () => {
    for (const et of KNOWN_ESCALATIONS) {
      expect(ESCALATION_EVENT_TYPES.has(et)).toBe(true);
    }
  });

  test('every escalation type is high-signal (HIGH_SIGNAL_NOTE_EVENTS)', () => {
    for (const et of ESCALATION_EVENT_TYPES) {
      expect(HIGH_SIGNAL_NOTE_EVENTS.has(et)).toBe(true);
    }
  });

  test('every escalation type fires error toasts (CRITICAL_NOTE_EVENTS)', () => {
    for (const et of ESCALATION_EVENT_TYPES) {
      expect(CRITICAL_NOTE_EVENTS.has(et)).toBe(true);
    }
  });

  test('every escalation type has a badge label entry', () => {
    // Guards future drift: a new escalation type added to ESCALATION_EVENT_TYPES
    // without a corresponding ContextBadges label fails this test loudly.
    for (const et of ESCALATION_EVENT_TYPES) {
      expect(ESCALATION_BADGE_LABELS[et]).toBeTruthy();
    }
  });

  test('badge labels do not include unknown event types', () => {
    for (const et of Object.keys(ESCALATION_BADGE_LABELS)) {
      expect(ESCALATION_EVENT_TYPES.has(et)).toBe(true);
    }
  });

  describe('classifySignalLevel covers escalation events', () => {
    const base = { from_type: 'system', context: {} };

    for (const et of KNOWN_ESCALATIONS) {
      test(`${et} classifies as high under BLOCKER`, () => {
        expect(classifySignalLevel({ ...base, message_type: 'BLOCKER', context: { event_type: et } })).toBe('high');
      });

      test(`${et} classifies as high under NOTE`, () => {
        // Future-proof: if backend ever changes message_type to NOTE, classification still escalates.
        expect(classifySignalLevel({ ...base, message_type: 'NOTE', context: { event_type: et } })).toBe('high');
      });
    }
  });

  describe('getToastSeverity covers escalation events', () => {
    const base = { from_type: 'system', context: {} };

    for (const et of KNOWN_ESCALATIONS) {
      test(`${et} fires error toast under NOTE`, () => {
        expect(getToastSeverity({ ...base, message_type: 'NOTE', context: { event_type: et } })).toBe('error');
      });

      test(`${et} fires error toast under BLOCKER`, () => {
        expect(getToastSeverity({ ...base, message_type: 'BLOCKER', context: { event_type: et } })).toBe('error');
      });
    }
  });

  test('feedback_acknowledgment fires info toast (not error)', () => {
    const base = { from_type: 'system', message_type: 'NOTE' as const, context: { event_type: 'feedback_acknowledgment' } };
    expect(getToastSeverity(base)).toBe('info');
  });

  test('regression: existing high-signal NOTE events still classify correctly', () => {
    const base = { from_type: 'system', message_type: 'NOTE' as const };
    expect(classifySignalLevel({ ...base, context: { event_type: 'failure_alert' } })).toBe('high');
    expect(classifySignalLevel({ ...base, context: { event_type: 'rollback_recommended' } })).toBe('high');
    expect(classifySignalLevel({ ...base, context: { event_type: 'fix_loop_divergence_regressed' } })).toBe('high');
    expect(classifySignalLevel({ ...base, context: { event_type: 'pr_merged' } })).toBe('routine');
  });

  test('feedback_acknowledgment classifies as routine (not high-signal)', () => {
    const base = { from_type: 'system', message_type: 'NOTE' as const };
    expect(classifySignalLevel({ ...base, context: { event_type: 'feedback_acknowledgment' } })).toBe('routine');
  });

  test('strategic_steering_deferred is high-signal, info toast, and badge-labeled', () => {
    const base = { from_type: 'system', message_type: 'NOTE' as const, context: { event_type: 'strategic_steering_deferred' } };
    expect(classifySignalLevel(base)).toBe('high');
    expect(getToastSeverity(base)).toBe('info');
    expect(HIGH_SIGNAL_NOTE_BADGE_LABELS.strategic_steering_deferred).toBe('Steering Queued');
  });
});

describe('HIGH_SIGNAL_NOTE_BADGE_LABELS invariants', () => {
  test('every non-escalation HIGH_SIGNAL_NOTE_EVENTS entry has a badge label', () => {
    for (const et of HIGH_SIGNAL_NOTE_EVENTS) {
      if (ESCALATION_EVENT_TYPES.has(et)) continue;
      expect(HIGH_SIGNAL_NOTE_BADGE_LABELS[et]).toBeTruthy();
    }
  });

  test('HIGH_SIGNAL_NOTE_BADGE_LABELS has no entry for escalation event types', () => {
    for (const et of Object.keys(HIGH_SIGNAL_NOTE_BADGE_LABELS)) {
      expect(ESCALATION_EVENT_TYPES.has(et)).toBe(false);
    }
  });

  test('HIGH_SIGNAL_NOTE_BADGE_LABELS has no entry for routine event types', () => {
    const KNOWN_ROUTINE = ['pr_merged', 'direction_milestone', 'cycle_summary', 'auto_fix_spawned', 'auto_fix_result'];
    for (const et of KNOWN_ROUTINE) {
      expect(HIGH_SIGNAL_NOTE_BADGE_LABELS[et]).toBeUndefined();
    }
  });
});

describe('MuteWindow persistence', () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-15T12:00:00Z'));
  });
  afterEach(() => {
    localStorage.clear();
    vi.useRealTimers();
  });

  test('loadMuteWindow returns null when no key', () => {
    expect(loadMuteWindow('user1', 'proj1')).toBeNull();
  });

  test('saveMuteWindow then loadMuteWindow round-trips', () => {
    const expiresAt = Date.now() + 3600000;
    saveMuteWindow('user1', 'proj1', { expiresAt });
    const loaded = loadMuteWindow('user1', 'proj1');
    expect(loaded).toEqual({ expiresAt });
  });

  test('loadMuteWindow returns null for expired window', () => {
    saveMuteWindow('user1', 'proj1', { expiresAt: Date.now() - 1 });
    expect(loadMuteWindow('user1', 'proj1')).toBeNull();
  });

  test('loadMuteWindow clears expired key from storage', () => {
    const key = muteWindowKey('user1', 'proj1');
    saveMuteWindow('user1', 'proj1', { expiresAt: Date.now() - 1 });
    loadMuteWindow('user1', 'proj1');
    expect(localStorage.getItem(key)).toBeNull();
  });

  test('clearMuteWindow removes the key', () => {
    const key = muteWindowKey('user1', 'proj1');
    saveMuteWindow('user1', 'proj1', { expiresAt: Date.now() + 3600000 });
    expect(localStorage.getItem(key)).not.toBeNull();
    clearMuteWindow('user1', 'proj1');
    expect(localStorage.getItem(key)).toBeNull();
  });

  test('isMuteWindowActive returns true for future expiry', () => {
    expect(isMuteWindowActive({ expiresAt: Date.now() + 3600000 })).toBe(true);
  });

  test('isMuteWindowActive returns false for past expiry', () => {
    expect(isMuteWindowActive({ expiresAt: Date.now() - 1 })).toBe(false);
  });

  test('isMuteWindowActive returns false for null', () => {
    expect(isMuteWindowActive(null)).toBe(false);
  });

  test('formatMuteCountdown renders Xm left', () => {
    const expiresAt = Date.now() + 47 * 60000; // floor(47 * 60000 / 60000) = 47
    expect(formatMuteCountdown(expiresAt)).toBe('47m left');
  });

  test('formatMuteCountdown renders <1m left when less than a minute remains', () => {
    expect(formatMuteCountdown(Date.now() + 30000)).toBe('<1m left');
  });

  test('two project IDs hold independent mute windows', () => {
    const exp1 = Date.now() + 3600000;
    const exp2 = Date.now() + 7200000;
    saveMuteWindow('user1', 'proj1', { expiresAt: exp1 });
    saveMuteWindow('user1', 'proj2', { expiresAt: exp2 });
    expect(loadMuteWindow('user1', 'proj1')?.expiresAt).toBe(exp1);
    expect(loadMuteWindow('user1', 'proj2')?.expiresAt).toBe(exp2);
  });
});

describe('MessagesPanel.utils - Time-window grouping', () => {
  // Helper to create a test message
  interface TestMessage {
    id: string;
    context: { event_type: string | undefined };
    created_at: string;
  }
  const createMsg = (id: string, eventType: string | undefined, createdAt: string): TestMessage => ({
    id,
    context: { event_type: eventType },
    created_at: createdAt,
  });

  // Helper to create timestamps within the same day (for divider tests)
  const createToday = (hour: number, minute: number = 0) => {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate(), hour, minute, 0);
    return today.toISOString().split('T')[0] + `T${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}:00`;
  };

  it('(a) interleaved arrival groups correctly within time window', () => {
    const msgs = [
      createMsg('m1', 'pr_merged', createToday(10, 0)),
      createMsg('m2', 'cycle_summary', createToday(10, 30)),
      createMsg('m3', 'pr_merged', createToday(10, 45)),
    ];

    const rows = groupRoutineMessages(msgs);

    // m1 and m3 should be in the same group (even though m2 is between them)
    const groups = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'group' }> => r.kind === 'group');
    const singles = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'single' }> => r.kind === 'single');

    // Should have 0 groups (only 2 msgs of same type, below threshold) and 3 singles
    expect(groups.length).toBe(0);
    expect(singles.length).toBe(3);
    expect(singles.map(s => s.msg.id)).toEqual(['m1', 'm2', 'm3']);
  });

  it('(b) consecutive arrivals still group as before', () => {
    const msgs = [
      createMsg('a1', 'auto_fix_result', createToday(10, 0)),
      createMsg('a2', 'auto_fix_result', createToday(10, 15)),
      createMsg('a3', 'auto_fix_result', createToday(10, 30)),
    ];

    const rows = groupRoutineMessages(msgs);

    const groups = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'group' }> => r.kind === 'group');
    const singles = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'single' }> => r.kind === 'single');

    // Should have 1 group with all 3 consecutive msgs
    expect(groups.length).toBe(1);
    expect(singles.length).toBe(0);
    expect(groups[0].msgs.map((m: TestMessage) => m.id)).toEqual(['a1', 'a2', 'a3']);
  });

  it('(c) single event-type with 2 occurrences in window renders individually (below threshold)', () => {
    const msgs = [
      createMsg('x1', 'issue_implementation_spawned', createToday(10, 0)),
      createMsg('x2', 'issue_implementation_spawned', createToday(10, 30)),
    ];

    const rows = groupRoutineMessages(msgs);

    const groups = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'group' }> => r.kind === 'group');
    const singles = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'single' }> => r.kind === 'single');

    // 2 msgs is below the 3-msg threshold, so rendered as singles
    expect(groups.length).toBe(0);
    expect(singles.length).toBe(2);
    expect(singles.map(s => s.msg.id)).toEqual(['x1', 'x2']);
  });

  it('(d) groups do not span time dividers (different days)', () => {
    const now = new Date();
    const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    const yesterday = new Date(today);
    yesterday.setDate(yesterday.getDate() - 1);

    const todayStr = today.toISOString().split('T')[0] + 'T10:00:00';
    const yesterdayStr = yesterday.toISOString().split('T')[0] + 'T10:00:00';

    const msgs = [
      createMsg('m1', 'direction_milestone', yesterdayStr),
      createMsg('m2', 'direction_milestone', todayStr),
      createMsg('m3', 'direction_milestone', todayStr.replace('10:', '11:')),
    ];

    const rows = groupRoutineMessages(msgs);

    // m2 and m3 are on the same day and within 1 hour, should group
    // m1 is on a different day, should not group with m2/m3
    const groups = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'group' }> => r.kind === 'group');
    const singles = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'single' }> => r.kind === 'single');

    // m1 rendered as single, m2+m3 as single or group depending on other criteria
    // Since m2+m3 are only 2 msgs, they render as singles too
    expect(groups.length).toBe(0);
    expect(singles.length).toBe(3);
  });

  it('groups messages only within the 1-hour time window', () => {
    const msgs = [
      createMsg('early', 'auto_fix_result', createToday(10, 0)),
      createMsg('inwindow', 'auto_fix_result', createToday(10, 30)),
      createMsg('outofwindow', 'auto_fix_result', createToday(11, 5)), // 65 min after early
      createMsg('alsooutside', 'auto_fix_result', createToday(11, 10)), // Within window of outofwindow, but not early
    ];

    const rows = groupRoutineMessages(msgs);

    // early + inwindow = 30min apart, within 1hr
    // inwindow + outofwindow = 35min apart, within 1hr (rolling window)
    // So all should be in one group? Let me reconsider...
    // Actually, with rolling window: early (10:00), inwindow (10:30 within 1hr of early), outofwindow (11:05 within 1hr of inwindow at 10:30)
    // So yes, all 4 should be in same group since each is within 1hr of the previous

    const groups = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'group' }> => r.kind === 'group');
    expect(groups.length).toBe(1);
    expect(groups[0].msgs.length).toBe(4);
  });

  it('maintains chronological order when rendering groups', () => {
    const msgs = [
      createMsg('a1', 'auto_fix_result', createToday(10, 0)),
      createMsg('b1', 'cycle_summary', createToday(10, 15)),
      createMsg('a2', 'auto_fix_result', createToday(10, 30)),
      createMsg('c1', 'direction_milestone', createToday(10, 45)),
      createMsg('a3', 'auto_fix_result', createToday(11, 0)),
    ];

    const rows = groupRoutineMessages(msgs);

    // Expected order: a1,a2,a3 in group (or as singles), then b1 as single, then c1 as single
    // But since a1,a2,a3 are interleaved with b1 and c1, rendering at first occurrence:
    // Position of a1: render group [a1,a2,a3]
    // Position of b1: render single [b1]
    // Position of c1: render single [c1]
    // But output should maintain the message order, so between group and singles...

    // Let's verify the IDs are in a reasonable order
    const ids = rows.map(r => {
      if (r.kind === 'group') return `group(${r.msgs.map((m: TestMessage) => m.id).join(',')})`;
      if (r.kind === 'single') return r.msg.id;
      return `digest(${r.day})`;
    });
    expect(ids[0]).toBe('group(a1,a2,a3)'); // a1 group rendered first
    expect(ids[1]).toBe('b1');
    expect(ids[2]).toBe('c1');
  });

  it('handles messages without event_type as singles', () => {
    const msgs = [
      createMsg('m1', undefined, createToday(10, 0)),
      createMsg('m2', 'auto_fix_result', createToday(10, 15)),
      createMsg('m3', undefined, createToday(10, 30)),
    ];

    const rows = groupRoutineMessages(msgs);

    const singles = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'single' }> => r.kind === 'single');
    expect(singles.length).toBe(3);
    expect(singles.map(s => s.msg.id)).toEqual(['m1', 'm2', 'm3']);
  });

  it('creates multiple groups for same event_type if they are separated in time', () => {
    const msgs = [
      createMsg('m1', 'pr_merged', createToday(10, 0)),
      createMsg('m2', 'pr_merged', createToday(10, 30)),
      createMsg('m3', 'pr_merged', createToday(12, 0)), // 90 minutes after m1, outside window
      createMsg('m4', 'pr_merged', createToday(12, 15)),
    ];

    const rows = groupRoutineMessages(msgs);

    // m1, m2 are within 1 hour -> group 1 (but <3 so singles)
    // m3, m4 are within 1 hour -> group 2 (but <3 so singles)
    // All rendered as singles due to <3 threshold
    const singles = rows.filter((r): r is Extract<RoutineRow<TestMessage>, { kind: 'single' }> => r.kind === 'single');
    expect(singles.length).toBe(4);
  });
});

// ── Saved Views ─────────────────────────────────────────────────────────────

describe('Saved Views storage', () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { localStorage.clear(); });

  function makeView(overrides: Partial<SavedView> = {}): SavedView {
    return {
      id: 'view_1',
      name: 'Unresolved blockers',
      description: undefined,
      query: 'blocker',
      prefs: { pr_merged: 'mute' } as Record<string, EventPref>,
      sort: 'importance',
      showResolved: false,
      routineExpanded: false,
      showOnlyNew: false,
      createdAt: 1700000000000,
      ...overrides,
    };
  }

  it('savedViewsKey scopes per user and project', () => {
    expect(savedViewsKey('alice', 'proj-A')).toBe('messages_saved_views:alice:proj-A');
    expect(savedViewsKey(undefined, undefined)).toBe('messages_saved_views:anonymous:default');
    expect(savedViewsKey(null, null)).toBe('messages_saved_views:anonymous:default');
  });

  it('round-trips a saved view', () => {
    const v = makeView({ description: 'top priority' });
    saveSavedViews('u1', 'p1', [v]);
    const loaded = loadSavedViews('u1', 'p1');
    expect(loaded).toEqual([v]);
  });

  it('loads empty array when key is missing', () => {
    expect(loadSavedViews('u1', 'p1')).toEqual([]);
  });

  it('returns empty array for corrupt JSON', () => {
    localStorage.setItem(savedViewsKey('u1', 'p1'), 'not json {');
    expect(loadSavedViews('u1', 'p1')).toEqual([]);
  });

  it('saving an empty array removes the key', () => {
    saveSavedViews('u1', 'p1', [makeView()]);
    expect(localStorage.getItem(savedViewsKey('u1', 'p1'))).not.toBeNull();
    saveSavedViews('u1', 'p1', []);
    expect(localStorage.getItem(savedViewsKey('u1', 'p1'))).toBeNull();
  });

  it('tolerates missing optional description on load', () => {
    saveSavedViews('u1', 'p1', [makeView({ description: undefined })]);
    const loaded = loadSavedViews('u1', 'p1');
    expect(loaded).toHaveLength(1);
    expect(loaded[0].description).toBeUndefined();
    expect(loaded[0].name).toBe('Unresolved blockers');
  });

  it('coerces missing/invalid fields to defaults on load', () => {
    // Write a manually-corrupted file with missing/invalid fields
    const file = {
      version: 1,
      views: [
        { id: 'v1', name: 'A' }, // missing query/prefs/sort/etc — should default
        { id: 'v2', name: 'B', sort: 'nonsense', prefs: { x: 'mute', y: 'invalid' } },
        { name: 'no-id' }, // dropped
        { id: 'no-name' }, // dropped
      ],
    };
    localStorage.setItem(savedViewsKey('u1', 'p1'), JSON.stringify(file));
    const loaded = loadSavedViews('u1', 'p1');
    expect(loaded).toHaveLength(2);
    expect(loaded[0].id).toBe('v1');
    expect(loaded[0].query).toBe('');
    expect(loaded[0].prefs).toEqual({});
    expect(loaded[0].sort).toBe('importance');
    expect(loaded[0].showResolved).toBe(false);
    expect(loaded[0].routineExpanded).toBe(false);
    expect(loaded[1].id).toBe('v2');
    expect(loaded[1].sort).toBe('importance'); // 'nonsense' -> default
    expect(loaded[1].prefs).toEqual({ x: 'mute' }); // 'invalid' EventPref dropped
  });

  it('ignores unknown fields on load (forward-compatible)', () => {
    const file = {
      version: 1,
      views: [
        {
          id: 'v1', name: 'A', query: '', prefs: {}, sort: 'importance',
          showResolved: false, routineExpanded: false, createdAt: 1,
          // future fields the current code does not know about:
          dateRange: { from: '2026-01-01', to: '2026-01-02' },
          muteUntil: 1234567890,
        },
      ],
    };
    localStorage.setItem(savedViewsKey('u1', 'p1'), JSON.stringify(file));
    const loaded = loadSavedViews('u1', 'p1');
    expect(loaded).toHaveLength(1);
    expect(loaded[0].id).toBe('v1');
    expect((loaded[0] as unknown as { dateRange?: unknown }).dateRange).toBeUndefined();
  });

  it('persists views as a {version, views} envelope', () => {
    saveSavedViews('u1', 'p1', [makeView()]);
    const raw = localStorage.getItem(savedViewsKey('u1', 'p1'));
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw!);
    expect(parsed.version).toBe(1);
    expect(Array.isArray(parsed.views)).toBe(true);
  });

  it('per-user-per-project isolation', () => {
    const a = makeView({ id: 'a', name: 'A' });
    const b = makeView({ id: 'b', name: 'B' });
    saveSavedViews('alice', 'proj-A', [a]);
    saveSavedViews('alice', 'proj-B', [b]);
    saveSavedViews('bob', 'proj-A', []);
    expect(loadSavedViews('alice', 'proj-A')).toEqual([a]);
    expect(loadSavedViews('alice', 'proj-B')).toEqual([b]);
    expect(loadSavedViews('bob', 'proj-A')).toEqual([]);
  });

  it('cap constant is 10', () => {
    expect(SAVED_VIEWS_CAP).toBe(10);
  });
});

describe('Age-based routine collapse (collapseStaleRoutineRows)', () => {
  // Freeze the clock to a fixed mid-day UTC time so the now-relative fixtures
  // (e.g. `Date.now() - 20min`) never straddle a UTC date boundary. Without
  // this, test (2)'s 3-message burst lands on two calendar dates when CI runs
  // in the 00:00–00:20 UTC window; groupRoutineMessages buckets by calendar
  // date, so it splits the burst into sub-3 groups that render 'single' → a
  // daily flake that blocked any PR whose frontend CI ran just after midnight.
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-15T12:00:00Z'));
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  const makeMsg = (id: string, et: string, msSinceEpoch: number) => ({
    id,
    context: { event_type: et },
    created_at: new Date(msSinceEpoch).toISOString(),
  });

  it('(1) single stale routine message collapses into a daily_digest row', () => {
    const msgTs = Date.now() - 4 * STALE_ROUTINE_COLLAPSE_AGE_MS;
    const rows = groupRoutineMessages([makeMsg('m1', 'pr_merged', msgTs)]);
    const collapsed = collapseStaleRoutineRows(rows);
    expect(collapsed).toHaveLength(1);
    const digest = collapsed[0];
    expect(digest.kind).toBe('daily_digest');
    if (digest.kind === 'daily_digest') {
      expect(digest.count).toBe(1);
      expect(digest.breakdown['pr_merged']).toBe(1);
    }
  });

  it('(2) fresh burst of 3 routine messages within 30min stays as a 1h-window group', () => {
    const base = Date.now() - 20 * 60000; // 20 min ago — well within 1h threshold
    const msgs = [
      makeMsg('m1', 'pr_merged', base),
      makeMsg('m2', 'pr_merged', base + 10 * 60000),
      makeMsg('m3', 'pr_merged', base + 20 * 60000),
    ];
    const rows = groupRoutineMessages(msgs);
    expect(rows[0].kind).toBe('group'); // sanity: 1h-window grouping fired
    const collapsed = collapseStaleRoutineRows(rows);
    expect(collapsed).toHaveLength(1);
    const groupRow = collapsed[0];
    expect(groupRow.kind).toBe('group');
    if (groupRow.kind === 'group') {
      expect(groupRow.msgs).toHaveLength(3);
    }
  });

  it('(3) high-signal event type older than threshold renders as single, not collapsed', () => {
    const msgTs = Date.now() - 4 * STALE_ROUTINE_COLLAPSE_AGE_MS;
    const rows = groupRoutineMessages([makeMsg('m1', 'failure_alert', msgTs)]);
    const collapsed = collapseStaleRoutineRows(rows);
    expect(collapsed).toHaveLength(1);
    const singleRow = collapsed[0];
    expect(singleRow.kind).toBe('single');
    if (singleRow.kind === 'single') {
      expect(singleRow.msg.id).toBe('m1');
    }
  });
});

describe('Timezone correctness of day labels (naive backend UTC strings)', () => {
  // The backend emits naive UTC ISO strings with NO trailing 'Z'. A negative-offset
  // operator timezone is where a raw `new Date(naiveString)` (local parse) shifts a
  // near-midnight-UTC message into the wrong calendar day. parseTimestamp must append
  // 'Z' so the message lands on the correct local day in every timezone.
  const originalTz = process.env.TZ;
  beforeEach(() => {
    process.env.TZ = 'America/New_York';
    // Freeze "now" well away from the fixture day so day labels render as the
    // absolute 'Thu Jun 4' heading rather than the relative 'Today'/'Yesterday'.
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-08-01T12:00:00Z'));
  });
  afterEach(() => {
    vi.useRealTimers();
    process.env.TZ = originalTz;
  });

  const mk = (id: string, createdAt: string) => ({ id, created_at: createdAt });

  it('naive UTC string near a UTC day boundary labels onto the correct local day', () => {
    // 2026-06-05T02:00:00Z is 2026-06-04 22:00 in America/New_York (UTC-4).
    // The correct local-day divider label is therefore Thu Jun 4, not Fri Jun 5.
    const dividers = insertTimeDividers([mk('m1', '2026-06-05T02:00:00')]);
    const labels = dividers.filter(d => d.kind === 'divider').map(d => d.label);
    expect(labels).toEqual(['Thu Jun 4']);
  });

  it('a naive string and its explicit-UTC equivalent produce one shared divider', () => {
    // Same instant expressed naively and with a 'Z'. After routing through the shared
    // guard both parse identically, so they bucket onto the same local day (one divider).
    const dividers = insertTimeDividers([
      mk('m1', '2026-06-05T02:00:00'),
      mk('m2', '2026-06-05T02:00:00Z'),
    ]);
    const labels = dividers.filter(d => d.kind === 'divider').map(d => d.label);
    expect(labels).toEqual(['Thu Jun 4']);
  });

  it('groupRoutineMessages buckets a near-boundary naive burst onto one local day', () => {
    // Three routine messages just after 02:00Z (late-evening local on Jun 4) must all
    // share a day bucket so the 1h-window grouping fires instead of splitting on a
    // spurious calendar-day boundary.
    const msgs = [
      { id: 'm1', context: { event_type: 'pr_merged' }, created_at: '2026-06-05T02:00:00' },
      { id: 'm2', context: { event_type: 'pr_merged' }, created_at: '2026-06-05T02:10:00' },
      { id: 'm3', context: { event_type: 'pr_merged' }, created_at: '2026-06-05T02:20:00' },
    ];
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('group');
    if (rows[0].kind === 'group') {
      expect(rows[0].msgs).toHaveLength(3);
    }
  });
});

describe('Failing group detection (mute escape hatch)', () => {
  test('group of 5 routine messages with 3 failed (60% failure rate) is identified as failing', () => {
    const msgs = [
      { id: 'msg1', context: { result: 'failed' } },
      { id: 'msg2', context: { result: 'failed' } },
      { id: 'msg3', context: { result: 'failed' } },
      { id: 'msg4', context: { result: 'merged' } },
      { id: 'msg5', context: { result: 'merged' } },
    ];
    expect(isFailingGroup(msgs)).toBe(true);
  });

  test('group of 5 with exactly 3 failed still qualifies as failing', () => {
    const msgs = [
      { id: 'm1', context: { result: 'failed' } },
      { id: 'm2', context: { result: 'failed' } },
      { id: 'm3', context: { result: 'failed' } },
      { id: 'm4', context: { result: 'review_needed' } },
      { id: 'm5', context: { result: 'review_needed' } },
    ];
    expect(isFailingGroup(msgs)).toBe(true);
  });

  test('group below 50% failure rate is not failing', () => {
    const msgs = [
      { id: 'msg1', context: { result: 'failed' } },
      { id: 'msg2', context: { result: 'failed' } },
      { id: 'msg3', context: { result: 'merged' } },
      { id: 'msg4', context: { result: 'merged' } },
      { id: 'msg5', context: { result: 'merged' } },
    ];
    expect(isFailingGroup(msgs)).toBe(false);
  });

  test('group with less than 3 failed does not qualify even if >=50%', () => {
    const msgs = [
      { id: 'msg1', context: { result: 'failed' } },
      { id: 'msg2', context: { result: 'merged' } },
    ];
    expect(isFailingGroup(msgs)).toBe(false);
  });
});

// ── Last-seen storage utilities ───────────────────────────────────────────────

describe('loadLastSeen / saveLastSeen', () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { localStorage.clear(); });

  test('round-trips a timestamp correctly', () => {
    const ts = Date.now() - 5000;
    saveLastSeen('user1', 'proj1', ts);
    expect(loadLastSeen('user1', 'proj1')).toBe(ts);
  });

  test('returns null when no entry exists', () => {
    expect(loadLastSeen('user1', 'proj1')).toBeNull();
  });

  test('two users have independent last-seen values for the same project', () => {
    saveLastSeen('user1', 'proj1', 1000);
    saveLastSeen('user2', 'proj1', 2000);
    expect(loadLastSeen('user1', 'proj1')).toBe(1000);
    expect(loadLastSeen('user2', 'proj1')).toBe(2000);
  });
});

describe('resolveLastSeen (backend + localStorage merge)', () => {
  test('prefers backend when it is more recent', () => {
    expect(resolveLastSeen(100, 200)).toBe(200);
  });

  test('prefers localStorage when it is more recent', () => {
    expect(resolveLastSeen(300, 200)).toBe(300);
  });

  test('falls back to localStorage when backend is null (offline fallback)', () => {
    expect(resolveLastSeen(100, null)).toBe(100);
  });

  test('returns backend when localStorage is null', () => {
    expect(resolveLastSeen(null, 200)).toBe(200);
  });

  test('returns null when both sources are null', () => {
    expect(resolveLastSeen(null, null)).toBeNull();
  });
});

// ── Server-side noise prefs ───────────────────────────────────────────────────

describe('resolveNoisePrefs', () => {
  test('returns server prefs when server result is present', () => {
    const local: Record<string, EventPref> = { pr_merged: 'mute' };
    const server: Record<string, EventPref> = { pr_merged: 'surfaced', cycle_summary: 'routine' };
    expect(resolveNoisePrefs(local, server)).toEqual(server);
  });

  test('falls back to local when server is null (offline)', () => {
    const local: Record<string, EventPref> = { pr_merged: 'mute' };
    expect(resolveNoisePrefs(local, null)).toEqual(local);
  });

  test('returns empty server object when server returns empty prefs', () => {
    const local: Record<string, EventPref> = { pr_merged: 'mute' };
    expect(resolveNoisePrefs(local, {})).toEqual({});
  });
});

describe('fetchServerNoisePrefs', () => {

  beforeEach(() => { vi.resetAllMocks(); });

  test('returns sanitized prefs on successful response', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ prefs: { pr_merged: 'mute', cycle_summary: 'invalid', failure_alert: 'surfaced' } }),
    } as Response);
    const result = await fetchServerNoisePrefs('project-uuid');
    expect(result).toEqual({ pr_merged: 'mute', failure_alert: 'surfaced' }); // 'invalid' dropped
  });

  test('returns null on non-ok response', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: false } as Response);
    expect(await fetchServerNoisePrefs('project-uuid')).toBeNull();
  });

  test('returns null on network error', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    expect(await fetchServerNoisePrefs('project-uuid')).toBeNull();
  });
});

describe('saveServerNoisePrefs', () => {

  beforeEach(() => { vi.resetAllMocks(); });

  test('calls PUT with the correct project_id and prefs', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: true } as Response);
    const prefs: Record<string, EventPref> = { pr_merged: 'mute' };
    await saveServerNoisePrefs('proj-123', prefs);
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/messages/noise-prefs'),
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ project_id: 'proj-123', prefs }) }),
    );
  });

  test('silently swallows network errors', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    await expect(saveServerNoisePrefs('proj-123', {})).resolves.toBeUndefined();
  });
});

// ── Server-side saved views ───────────────────────────────────────────────────

describe('resolveServerSavedViews', () => {
  const view: SavedView = {
    id: 'v1', name: 'Blockers', query: 'blocker', prefs: {},
    sort: 'importance', showResolved: false, routineExpanded: false,
    showOnlyNew: false, createdAt: 1000,
  };

  test('returns server views when server result is present', () => {
    const local = [{ ...view, id: 'local' }];
    const server = [view];
    expect(resolveServerSavedViews(local, server)).toEqual(server);
  });

  test('falls back to local when server is null (offline)', () => {
    const local = [view];
    expect(resolveServerSavedViews(local, null)).toEqual(local);
  });

  test('returns empty array when server returns empty views', () => {
    expect(resolveServerSavedViews([view], [])).toEqual([]);
  });
});

describe('fetchServerSavedViews', () => {
  beforeEach(() => { vi.resetAllMocks(); });

  test('returns sanitized views on successful response', async () => {
    const raw = { id: 'v1', name: 'All', query: '', prefs: { pr_merged: 'mute' }, sort: 'importance', showResolved: false, routineExpanded: false, showOnlyNew: false, createdAt: 1000 };
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ saved_views: [raw, { bad: true }] }),
    } as Response);
    const result = await fetchServerSavedViews('project-uuid');
    expect(result).toHaveLength(1);
    expect(result![0].id).toBe('v1');
    expect(result![0].prefs).toEqual({ pr_merged: 'mute' });
  });

  test('returns null on non-ok response', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: false } as Response);
    expect(await fetchServerSavedViews('project-uuid')).toBeNull();
  });

  test('returns null on network error', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    expect(await fetchServerSavedViews('project-uuid')).toBeNull();
  });

  test('returns empty array when server returns no views', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ saved_views: [] }),
    } as Response);
    expect(await fetchServerSavedViews('project-uuid')).toEqual([]);
  });
});

describe('saveServerSavedViews', () => {
  beforeEach(() => { vi.resetAllMocks(); });

  test('calls PUT with the correct project_id and saved_views', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: true } as Response);
    const views: SavedView[] = [{
      id: 'v1', name: 'Blockers', query: 'blocker', prefs: {},
      sort: 'importance', showResolved: false, routineExpanded: false,
      showOnlyNew: false, createdAt: 1000,
    }];
    await saveServerSavedViews('proj-123', views);
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/messages/saved-views'),
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ project_id: 'proj-123', saved_views: views }) }),
    );
  });

  test('silently swallows network errors', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    const views: SavedView[] = [];
    await expect(saveServerSavedViews('proj-123', views)).resolves.toBeUndefined();
  });
});

// ── Server-side temp-mutes ───────────────────────────────────────────────────

describe('resolveServerTempMutes', () => {
  test('returns server mutes when server result is present', () => {
    const local: TemporaryMutes = { pr_merged: Date.now() + 3600000 };
    const server: TemporaryMutes = { cycle_summary: Date.now() + 7200000 };
    expect(resolveServerTempMutes(local, server)).toEqual(server);
  });

  test('falls back to local when server is null (offline)', () => {
    const local: TemporaryMutes = { pr_merged: Date.now() + 3600000 };
    expect(resolveServerTempMutes(local, null)).toEqual(local);
  });

  test('returns empty object when server returns no active mutes', () => {
    const local: TemporaryMutes = { pr_merged: Date.now() + 3600000 };
    expect(resolveServerTempMutes(local, {})).toEqual({});
  });
});

describe('fetchServerTempMutes', () => {
  beforeEach(() => { vi.resetAllMocks(); });

  test('returns active mutes on successful response, drops expired', async () => {
    const future = Date.now() + 3600000;
    const past = Date.now() - 1000;
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ temp_mutes: { pr_merged: future, cycle_summary: past } }),
    } as Response);
    const result = await fetchServerTempMutes('project-uuid');
    expect(result).toEqual({ pr_merged: future });
  });

  test('returns null on non-ok response', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: false } as Response);
    expect(await fetchServerTempMutes('project-uuid')).toBeNull();
  });

  test('returns null on network error', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    expect(await fetchServerTempMutes('project-uuid')).toBeNull();
  });

  test('returns empty object when server has no active mutes', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ temp_mutes: {} }),
    } as Response);
    expect(await fetchServerTempMutes('project-uuid')).toEqual({});
  });
});

describe('saveServerTempMutes', () => {
  beforeEach(() => { vi.resetAllMocks(); });

  test('calls PUT with correct project_id and temp_mutes', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: true } as Response);
    const mutes: TemporaryMutes = { pr_merged: Date.now() + 3600000 };
    await saveServerTempMutes('proj-123', mutes);
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/messages/temp-mutes'),
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ project_id: 'proj-123', temp_mutes: mutes }) }),
    );
  });

  test('silently swallows network errors', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    await expect(saveServerTempMutes('proj-123', {})).resolves.toBeUndefined();
  });
});

// ── Server-side mute-window ──────────────────────────────────────────────────

describe('resolveServerMuteWindow', () => {
  const win: MuteWindow = { expiresAt: Date.now() + 3600000 };

  test('returns server window when server has an active mute', () => {
    const local: MuteWindow = { expiresAt: Date.now() + 1000 };
    expect(resolveServerMuteWindow(local, win)).toEqual(win);
  });

  test('returns null when server says mute is cleared (not offline)', () => {
    const local: MuteWindow = { expiresAt: Date.now() + 3600000 };
    expect(resolveServerMuteWindow(local, null)).toBeNull();
  });

  test('falls back to local when fetch failed (undefined)', () => {
    const local: MuteWindow = { expiresAt: Date.now() + 3600000 };
    expect(resolveServerMuteWindow(local, undefined)).toEqual(local);
  });
});

describe('fetchServerMuteWindow', () => {
  beforeEach(() => { vi.resetAllMocks(); });

  test('returns MuteWindow with expiresAt when server has active window', async () => {
    const future = Date.now() + 3600000;
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ mute_window: { expires_at: future } }),
    } as Response);
    const result = await fetchServerMuteWindow('project-uuid');
    expect(result).toEqual({ expiresAt: future });
  });

  test('returns null when server mute_window is null', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ mute_window: null }),
    } as Response);
    expect(await fetchServerMuteWindow('project-uuid')).toBeNull();
  });

  test('returns null when server window is expired', async () => {
    const past = Date.now() - 1000;
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ mute_window: { expires_at: past } }),
    } as Response);
    expect(await fetchServerMuteWindow('project-uuid')).toBeNull();
  });

  test('returns undefined on non-ok response (offline fallback)', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: false } as Response);
    expect(await fetchServerMuteWindow('project-uuid')).toBeUndefined();
  });

  test('returns undefined on network error (offline fallback)', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    expect(await fetchServerMuteWindow('project-uuid')).toBeUndefined();
  });
});

describe('saveServerMuteWindow', () => {
  beforeEach(() => { vi.resetAllMocks(); });

  test('calls PUT with expires_at when mute window is active', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: true } as Response);
    const win: MuteWindow = { expiresAt: 9999999999 };
    await saveServerMuteWindow('proj-123', win);
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/messages/mute-window'),
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ project_id: 'proj-123', expires_at: 9999999999 }) }),
    );
  });

  test('calls PUT with expires_at=null to clear the mute window', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: true } as Response);
    await saveServerMuteWindow('proj-123', null);
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
      expect.stringContaining('/api/admin/messages/mute-window'),
      expect.objectContaining({ method: 'PUT', body: JSON.stringify({ project_id: 'proj-123', expires_at: null }) }),
    );
  });

  test('silently swallows network errors', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    await expect(saveServerMuteWindow('proj-123', null)).resolves.toBeUndefined();
  });
});

// ── High-signal NOTE flood survival ──────────────────────────────────────────
// Regression guard: countHighSignalMessages must count each of the three
// high-signal system NOTE event types (failure_alert, rollback_recommended,
// fix_loop_divergence_regressed) correctly even when buried under a routine
// flood of messages that would exceed a typical page limit.

describe('countHighSignalMessages — high-signal NOTE flood survival', () => {
  const makeRoutineFlood = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      id: `routine-${i}`,
      message_type: 'NOTE' as const,
      from_type: 'system',
      status: 'open',
      context: { event_type: 'pr_merged' },
    }));

  const makeHighSignalNote = (et: string, status = 'open') => ({
    id: `hs-${et}`,
    message_type: 'NOTE' as const,
    from_type: 'system',
    status,
    context: { event_type: et },
  });

  test('failure_alert is counted amid routine flood exceeding page limit', () => {
    const messages = [...makeRoutineFlood(100), makeHighSignalNote('failure_alert')];
    expect(countHighSignalMessages(messages)).toBe(1);
  });

  test('rollback_recommended is counted amid routine flood exceeding page limit', () => {
    const messages = [...makeRoutineFlood(100), makeHighSignalNote('rollback_recommended')];
    expect(countHighSignalMessages(messages)).toBe(1);
  });

  test('fix_loop_divergence_regressed is counted amid routine flood exceeding page limit', () => {
    const messages = [...makeRoutineFlood(100), makeHighSignalNote('fix_loop_divergence_regressed')];
    expect(countHighSignalMessages(messages)).toBe(1);
  });

  test('all three high-signal NOTE types are counted independently amid routine flood', () => {
    const messages = [
      ...makeRoutineFlood(100),
      makeHighSignalNote('failure_alert'),
      makeHighSignalNote('rollback_recommended'),
      makeHighSignalNote('fix_loop_divergence_regressed'),
    ];
    expect(countHighSignalMessages(messages)).toBe(3);
  });

  test('resolved high-signal NOTEs are excluded from count', () => {
    const messages = [
      ...makeRoutineFlood(50),
      makeHighSignalNote('failure_alert', 'resolved'),
      makeHighSignalNote('rollback_recommended'),
    ];
    expect(countHighSignalMessages(messages)).toBe(1);
  });

  test('routine flood alone counts zero', () => {
    expect(countHighSignalMessages(makeRoutineFlood(100))).toBe(0);
  });
});

// ── countHighSignalMessages — pref-aware ─────────────────────────────────────

describe('countHighSignalMessages — pref-aware counting', () => {
  const makeNote = (et: string, status = 'open') => ({
    message_type: 'NOTE' as const,
    from_type: 'system',
    status,
    context: { event_type: et },
  });

  test('muted event type is excluded from count when prefs provided', () => {
    const messages = [makeNote('failure_alert'), makeNote('rollback_recommended')];
    const prefs: Record<string, import('./MessagesPanel.utils').EventPref> = { failure_alert: 'mute' };
    expect(countHighSignalMessages(messages, prefs)).toBe(1);
  });

  test('surfaced routine event type is included when prefs provided', () => {
    const messages = [makeNote('pr_merged')];
    const prefs: Record<string, import('./MessagesPanel.utils').EventPref> = { pr_merged: 'surfaced' };
    expect(countHighSignalMessages(messages, prefs)).toBe(1);
  });

  test('active temp mute excludes event type from count', () => {
    const messages = [makeNote('failure_alert')];
    const tempMutes: import('./MessagesPanel.utils').TemporaryMutes = { failure_alert: Date.now() + 60000 };
    expect(countHighSignalMessages(messages, {}, tempMutes)).toBe(0);
  });

  test('expired temp mute does not exclude event type', () => {
    const messages = [makeNote('failure_alert')];
    const tempMutes: import('./MessagesPanel.utils').TemporaryMutes = { failure_alert: Date.now() - 1 };
    expect(countHighSignalMessages(messages, {}, tempMutes)).toBe(1);
  });

  test('zero-arg call (no prefs) is unchanged — existing callers unbroken', () => {
    const messages = [makeNote('failure_alert'), makeNote('pr_merged')];
    expect(countHighSignalMessages(messages)).toBe(1);
  });
});

// ── countNewSinceLastVisit ────────────────────────────────────────────────────

describe('countNewSinceLastVisit', () => {
  const base = Date.now();
  const tsOld = new Date(base - 10000).toISOString();
  const tsNew = new Date(base + 10000).toISOString();

  const makeMsg = (opts: {
    event_type?: string;
    message_type?: string;
    from_type?: string;
    status?: string;
    created_at?: string | null;
  }) => ({
    message_type: opts.message_type ?? 'NOTE',
    from_type: opts.from_type ?? 'system',
    status: opts.status ?? 'open',
    context: { event_type: opts.event_type ?? 'failure_alert' },
    created_at: opts.created_at !== undefined ? opts.created_at : tsNew,
  });

  test('returns 0 when lastSeenTs is null (no baseline)', () => {
    const msgs = [makeMsg({ event_type: 'failure_alert' })];
    expect(countNewSinceLastVisit(msgs, null)).toBe(0);
  });

  test('returns 0 when all high-signal messages predate lastSeenTs', () => {
    const msgs = [makeMsg({ event_type: 'failure_alert', created_at: tsOld })];
    expect(countNewSinceLastVisit(msgs, base)).toBe(0);
  });

  test('counts high-signal messages newer than lastSeenTs', () => {
    const msgs = [makeMsg({ event_type: 'failure_alert', created_at: tsNew })];
    expect(countNewSinceLastVisit(msgs, base)).toBe(1);
  });

  test('excludes routine-signal messages even if newer than lastSeenTs', () => {
    const msgs = [makeMsg({ event_type: 'pr_merged', created_at: tsNew })];
    expect(countNewSinceLastVisit(msgs, base)).toBe(0);
  });

  test('excludes resolved messages even if high-signal and newer', () => {
    const msgs = [makeMsg({ event_type: 'failure_alert', status: 'resolved', created_at: tsNew })];
    expect(countNewSinceLastVisit(msgs, base)).toBe(0);
  });

  test('excludes messages with no created_at', () => {
    const msgs = [makeMsg({ event_type: 'failure_alert', created_at: null })];
    expect(countNewSinceLastVisit(msgs, base)).toBe(0);
  });

  test('counts user messages as high-signal when newer than lastSeenTs', () => {
    const msgs = [makeMsg({ message_type: 'FEEDBACK', from_type: 'user', created_at: tsNew })];
    expect(countNewSinceLastVisit(msgs, base)).toBe(1);
  });

  test('mixed messages: only counts open high-signal newer items', () => {
    const msgs = [
      makeMsg({ event_type: 'failure_alert', created_at: tsNew }),   // counts
      makeMsg({ event_type: 'failure_alert', created_at: tsOld }),   // too old
      makeMsg({ event_type: 'pr_merged', created_at: tsNew }),        // routine
      makeMsg({ event_type: 'failure_alert', status: 'resolved', created_at: tsNew }), // resolved
    ];
    expect(countNewSinceLastVisit(msgs, base)).toBe(1);
  });
});

// ── countNewSinceLastVisit — pref-aware ───────────────────────────────────────

describe('countNewSinceLastVisit — pref-aware counting', () => {
  const base = Date.now();
  const tsNew = new Date(base + 10000).toISOString();

  const makeMsg = (et: string, messageType = 'NOTE', fromType = 'system') => ({
    message_type: messageType,
    from_type: fromType,
    status: 'open',
    context: { event_type: et },
    created_at: tsNew,
  });

  test('muted event type is excluded from new count when prefs provided', () => {
    const msgs = [makeMsg('failure_alert')];
    const prefs: Record<string, import('./MessagesPanel.utils').EventPref> = { failure_alert: 'mute' };
    expect(countNewSinceLastVisit(msgs, base, prefs)).toBe(0);
  });

  test('surfaced routine event type is included in new count when prefs provided', () => {
    const msgs = [makeMsg('pr_merged')];
    const prefs: Record<string, import('./MessagesPanel.utils').EventPref> = { pr_merged: 'surfaced' };
    expect(countNewSinceLastVisit(msgs, base, prefs)).toBe(1);
  });

  test('active temp mute excludes event type from new count', () => {
    const msgs = [makeMsg('failure_alert')];
    const tempMutes: import('./MessagesPanel.utils').TemporaryMutes = { failure_alert: Date.now() + 60000 };
    expect(countNewSinceLastVisit(msgs, base, {}, tempMutes)).toBe(0);
  });

  test('zero-arg prefs call is unchanged — existing callers unbroken', () => {
    const msgs = [makeMsg('failure_alert')];
    expect(countNewSinceLastVisit(msgs, base)).toBe(1);
  });
});

// ── outcomeBreakdown ──────────────────────────────────────────────────────────

describe('outcomeBreakdown', () => {
  function m(result?: string) { return { context: result ? { result } : {} }; }

  test('returns empty string when no result fields present', () => {
    expect(outcomeBreakdown([m(), m()])).toBe('');
  });

  test('shows failed count when only failures present', () => {
    expect(outcomeBreakdown([m('failed'), m('failed'), m('failed')])).toBe('· 3 failed');
  });

  test('shows merged count when only merges present', () => {
    expect(outcomeBreakdown([m('merged'), m('merged')])).toBe('· 2 merged');
  });

  test('shows mixed breakdown', () => {
    const msgs = [m('failed'), m('failed'), m('merged'), m('review_needed')];
    expect(outcomeBreakdown(msgs)).toBe('· 2 failed, 1 merged, 1 review');
  });

  test('silently degrades when result is absent on all messages', () => {
    expect(outcomeBreakdown([{ context: { event_type: 'pr_merged' } }])).toBe('');
  });
});

// ── groupRoutineMessages — group chip properties ──────────────────────────────

describe('groupRoutineMessages — group chip properties', () => {
  function makeItem(id: string, event_type: string) {
    return { id, context: { event_type } };
  }

  test('group row carries event_type, label, and key from first message', () => {
    const msgs = Array.from({ length: 5 }, (_, i) => makeItem(`id-${i}`, 'pr_merged'));
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('group');
    if (rows[0].kind === 'group') {
      expect(rows[0].event_type).toBe('pr_merged');
      expect(rows[0].label).toBe('PR merges');
      expect(rows[0].key).toBe('id-0');
    }
  });
});

// ── classifySignalLevel — explicit message-type coverage ─────────────────────

describe('classifySignalLevel — message-type coverage', () => {
  const base = { message_type: 'NOTE', from_type: 'system', context: {} };

  test('user messages are always high-signal', () => {
    expect(classifySignalLevel({ ...base, from_type: 'user' })).toBe('high');
    expect(classifySignalLevel({ ...base, from_type: 'USER' })).toBe('high');
  });

  test('FEEDBACK is high-signal regardless of from_type', () => {
    expect(classifySignalLevel({ ...base, message_type: 'FEEDBACK', from_type: 'agent' })).toBe('high');
  });

  test('BLOCKER is high-signal', () => {
    expect(classifySignalLevel({ ...base, message_type: 'BLOCKER' })).toBe('high');
  });

  test('BUG_REPORT is high-signal', () => {
    expect(classifySignalLevel({ ...base, message_type: 'BUG_REPORT' })).toBe('high');
  });

  test('QUESTION is high-signal (awaiting operator reply)', () => {
    expect(classifySignalLevel({ ...base, message_type: 'QUESTION' })).toBe('high');
  });

  test('NOTE with failure_alert is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'failure_alert' } })).toBe('high');
  });

  test('NOTE with rollback_recommended is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'rollback_recommended' } })).toBe('high');
  });

  test('NOTE with fix_loop_divergence_regressed is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'fix_loop_divergence_regressed' } })).toBe('high');
  });

  test('NOTE with pr_merged is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'pr_merged' } })).toBe('routine');
  });

  test('NOTE with feedback_acknowledgment is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'feedback_acknowledgment' } })).toBe('routine');
  });

  test('NOTE with unknown event_type is routine (fail-safe)', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'some_future_event' } })).toBe('routine');
  });

  test('NOTE with no event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: {} })).toBe('routine');
  });
});

// ── getToastSeverity — explicit message-type coverage ────────────────────────

describe('getToastSeverity', () => {
  const base = { message_type: 'NOTE', from_type: 'system', context: {} };

  test('BLOCKER returns error', () => {
    expect(getToastSeverity({ ...base, message_type: 'BLOCKER' })).toBe('error');
  });

  test('BUG_REPORT returns error', () => {
    expect(getToastSeverity({ ...base, message_type: 'BUG_REPORT' })).toBe('error');
  });

  test('FEEDBACK returns info', () => {
    expect(getToastSeverity({ ...base, message_type: 'FEEDBACK' })).toBe('info');
  });

  test('NOTE with failure_alert returns error', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'failure_alert' } })).toBe('error');
  });

  test('NOTE with rollback_recommended returns error', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'rollback_recommended' } })).toBe('error');
  });

  test('NOTE with fix_loop_divergence_regressed returns error', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'fix_loop_divergence_regressed' } })).toBe('error');
  });

  test('NOTE with account_model_regression_quarantined returns info', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'account_model_regression_quarantined' } })).toBe('info');
  });

  test('NOTE with feedback_acknowledgment returns info', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'feedback_acknowledgment' } })).toBe('info');
  });

  test('NOTE with routine event_type returns info', () => {
    expect(getToastSeverity({ ...base, context: { event_type: 'pr_merged' } })).toBe('info');
  });

  test('NOTE with no event_type returns info', () => {
    expect(getToastSeverity({ ...base, context: {} })).toBe('info');
  });
});

// ── fetchBackendLastSeen ──────────────────────────────────────────────────────

describe('fetchBackendLastSeen', () => {
  beforeEach(() => { vi.resetAllMocks(); });

  test('returns parsed timestamp on successful response', async () => {
    const ts = 1700000000000;
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ last_seen_at: ts }),
    } as Response);
    const result = await fetchBackendLastSeen('project-uuid');
    expect(result).toBe(ts);
  });

  test('returns null when last_seen_at is zero', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ last_seen_at: 0 }),
    } as Response);
    expect(await fetchBackendLastSeen('project-uuid')).toBeNull();
  });

  test('returns null when last_seen_at is absent', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({}),
    } as Response);
    expect(await fetchBackendLastSeen('project-uuid')).toBeNull();
  });

  test('returns null on non-ok response', async () => {
    vi.mocked(apiFetch).mockResolvedValue({ ok: false } as Response);
    expect(await fetchBackendLastSeen('project-uuid')).toBeNull();
  });

  test('returns null on network error', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('network'));
    expect(await fetchBackendLastSeen('project-uuid')).toBeNull();
  });

  test('URL includes encoded project_id', async () => {
    vi.mocked(apiFetch).mockResolvedValue({
      ok: true,
      json: async () => ({ last_seen_at: 1 }),
    } as Response);
    await fetchBackendLastSeen('proj/with spaces');
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
      expect.stringContaining('proj%2Fwith%20spaces'),
    );
  });
});
