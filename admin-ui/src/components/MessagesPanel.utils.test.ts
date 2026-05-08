import { describe, it, expect, beforeEach, afterEach, test } from 'vitest';
import {
  loadEventPrefs,
  saveEventPrefs,
  type EventPref,
  eventPrefsKey,
  groupRoutineMessages,
  collapseStaleRoutineRows,
  STALE_ROUTINE_COLLAPSE_AGE_MS,
  isFailingGroup,
  ESCALATION_EVENT_TYPES,
  ESCALATION_BADGE_LABELS,
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
} from './MessagesPanel.utils';

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
    expect(classifySignalLevel({ ...base, context: { event_type: 'pr_merged' } })).toBe('routine');
  });

  test('feedback_acknowledgment classifies as routine (not high-signal)', () => {
    const base = { from_type: 'system', message_type: 'NOTE' as const };
    expect(classifySignalLevel({ ...base, context: { event_type: 'feedback_acknowledgment' } })).toBe('routine');
  });
});

describe('MuteWindow persistence', () => {
  beforeEach(() => { localStorage.clear(); });
  afterEach(() => { localStorage.clear(); });

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
    const expiresAt = Date.now() + 47 * 60000 + 500; // +500ms so ceil gives exactly 47
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
    const groups = rows.filter(r => r.kind === 'group');
    const singles = rows.filter(r => r.kind === 'single');

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

    const groups = rows.filter(r => r.kind === 'group');
    const singles = rows.filter(r => r.kind === 'single');

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

    const groups = rows.filter(r => r.kind === 'group');
    const singles = rows.filter(r => r.kind === 'single');

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
    const groups = rows.filter(r => r.kind === 'group');
    const singles = rows.filter(r => r.kind === 'single');

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

    const groups = rows.filter(r => r.kind === 'group');
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
    const ids = rows.map(r => (r.kind === 'group' ? `group(${r.msgs.map((m: TestMessage) => m.id).join(',')})` : r.msg.id));
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

    const singles = rows.filter(r => r.kind === 'single');
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
    const singles = rows.filter(r => r.kind === 'single');
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
      query: 'blocker',
      prefs: { pr_merged: 'mute' } as Record<string, EventPref>,
      sort: 'importance',
      showResolved: false,
      routineExpanded: false,
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
    expect(collapsed[0].kind).toBe('daily_digest');
    expect(collapsed[0].count).toBe(1);
    expect(collapsed[0].breakdown['pr_merged']).toBe(1);
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
    expect(collapsed[0].kind).toBe('group');
    expect(collapsed[0].msgs).toHaveLength(3);
  });

  it('(3) high-signal event type older than threshold renders as single, not collapsed', () => {
    const msgTs = Date.now() - 4 * STALE_ROUTINE_COLLAPSE_AGE_MS;
    const rows = groupRoutineMessages([makeMsg('m1', 'failure_alert', msgTs)]);
    const collapsed = collapseStaleRoutineRows(rows);
    expect(collapsed).toHaveLength(1);
    expect(collapsed[0].kind).toBe('single');
    expect(collapsed[0].msg.id).toBe('m1');
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
