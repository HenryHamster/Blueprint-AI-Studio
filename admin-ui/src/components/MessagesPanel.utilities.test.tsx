import { describe, test, expect } from 'vitest';
import { classifySignalLevel, groupRoutineMessages, outcomeBreakdown, isFailingGroup } from './MessagesPanel.utils';
import { armingToastCopy, armingHintCopy, parseArmingOutcome } from './messageArming';

describe('groupRoutineMessages', () => {
  function makeRoutineItem(id: string, event_type: string) {
    return { id, context: { event_type } };
  }

  test('25 pr_merged messages collapse to one group chip', () => {
    const msgs = Array.from({ length: 25 }, (_, i) => makeRoutineItem(`m${i}`, 'pr_merged'));
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('group');
    if (rows[0].kind === 'group') {
      expect(rows[0].msgs).toHaveLength(25);
      expect(rows[0].event_type).toBe('pr_merged');
      expect(rows[0].label).toBe('PR merges');
    }
  });

  test('mixed event_types stay separated into distinct groups', () => {
    const msgs = [
      ...Array.from({ length: 3 }, (_, i) => makeRoutineItem(`p${i}`, 'pr_merged')),
      ...Array.from({ length: 3 }, (_, i) => makeRoutineItem(`a${i}`, 'auto_fix_result')),
    ];
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(2);
    expect(rows[0].kind).toBe('group');
    expect(rows[1].kind).toBe('group');
    if (rows[0].kind === 'group') expect(rows[0].event_type).toBe('pr_merged');
    if (rows[1].kind === 'group') expect(rows[1].event_type).toBe('auto_fix_result');
  });

  test('single-occurrence event_types render as individual rows', () => {
    const msgs = [makeRoutineItem('x', 'pr_merged'), makeRoutineItem('y', 'auto_fix_result')];
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(2);
    expect(rows.every(r => r.kind === 'single')).toBe(true);
  });

  test('exactly 2 of the same type does not group', () => {
    const msgs = [makeRoutineItem('a', 'pr_merged'), makeRoutineItem('b', 'pr_merged')];
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(2);
    expect(rows.every(r => r.kind === 'single')).toBe(true);
  });

  test('exactly 3 of the same type groups', () => {
    const msgs = Array.from({ length: 3 }, (_, i) => makeRoutineItem(`m${i}`, 'pr_merged'));
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(1);
    expect(rows[0].kind).toBe('group');
  });

  test('interleaved different types do not merge into one group', () => {
    const msgs = [
      makeRoutineItem('a', 'pr_merged'),
      makeRoutineItem('b', 'auto_fix_result'),
      makeRoutineItem('c', 'pr_merged'),
      makeRoutineItem('d', 'pr_merged'),
      makeRoutineItem('e', 'pr_merged'),
    ];
    const rows = groupRoutineMessages(msgs);
    // Time-window grouping merges all matching event_types that share a date
    // window (intentional: pr_merged bursts are coalesced even if briefly
    // interrupted), but DIFFERENT event_types stay separate — auto_fix_result
    // never merges into the pr_merged group.
    expect(rows).toHaveLength(2);
    expect(rows[0].kind).toBe('group');
    if (rows[0].kind === 'group') {
      expect(rows[0].event_type).toBe('pr_merged');
      expect(rows[0].msgs).toHaveLength(4);
    }
    expect(rows[1].kind).toBe('single');
  });

  test('uses msg id as group key (stable across renders)', () => {
    const msgs = Array.from({ length: 5 }, (_, i) => makeRoutineItem(`id-${i}`, 'pr_merged'));
    const rows = groupRoutineMessages(msgs);
    expect(rows).toHaveLength(1);
    if (rows[0].kind === 'group') expect(rows[0].key).toBe('id-0');
  });
});

// ── classifySignalLevel unit tests ───────────────────────────────────────────

describe('classifySignalLevel', () => {
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

  test('NOTE with failure_alert event_type is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'failure_alert' } })).toBe('high');
  });

  test('NOTE with rollback_recommended event_type is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'rollback_recommended' } })).toBe('high');
  });

  test('NOTE with fix_loop_divergence_regressed event_type is high-signal', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'fix_loop_divergence_regressed' } })).toBe('high');
  });

  test('NOTE with pr_merged event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'pr_merged' } })).toBe('routine');
  });

  test('NOTE with direction_milestone event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'direction_milestone' } })).toBe('routine');
  });

  test('NOTE with cycle_summary event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'cycle_summary' } })).toBe('routine');
  });

  test('NOTE with account_model_regression_quarantined is routine (single event)', () => {
    // Single quarantine is routine; ≥3 quarantines with ≥50% failed rate auto-promote to high-signal via isFailingGroup
    expect(classifySignalLevel({ ...base, context: { event_type: 'account_model_regression_quarantined' } })).toBe('routine');
  });

  test('NOTE with account_model_regression_recovered is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'account_model_regression_recovered' } })).toBe('routine');
  });

  test('NOTE with auto_fix_spawned is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'auto_fix_spawned' } })).toBe('routine');
  });

  test('NOTE with auto_fix_result is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'auto_fix_result' } })).toBe('routine');
  });

  test('NOTE with residual_fix_spawned is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'residual_fix_spawned' } })).toBe('routine');
  });

  test('NOTE with residual_fix_result is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'residual_fix_result' } })).toBe('routine');
  });

  test('NOTE with residual_fix_unsuspended is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'residual_fix_unsuspended' } })).toBe('routine');
  });

  test('NOTE with residual_case_auto_resolved is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'residual_case_auto_resolved' } })).toBe('routine');
  });

  test('NOTE with issue_implementation_spawned is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'issue_implementation_spawned' } })).toBe('routine');
  });

  test('NOTE with review_feedback is routine', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'review_feedback' } })).toBe('routine');
  });

  test('NOTE with feedback_acknowledgment is routine', () => {
    // Demoted from high-signal — flood of system acks was crowding the panel; reply context
    // is still rendered when the routine ops group is expanded.
    expect(classifySignalLevel({ ...base, context: { event_type: 'feedback_acknowledgment' } })).toBe('routine');
  });

  test('NOTE with unknown event_type is routine (fail-safe)', () => {
    expect(classifySignalLevel({ ...base, context: { event_type: 'some_future_event' } })).toBe('routine');
  });

  test('NOTE with no event_type is routine', () => {
    expect(classifySignalLevel({ ...base, context: {} })).toBe('routine');
  });

  test('QUESTION is high-signal (awaiting operator reply)', () => {
    expect(classifySignalLevel({ ...base, message_type: 'QUESTION' })).toBe('high');
  });

  test('failing group of ≥3 account_model_regression_quarantined (≥50% failed) would be promoted via isFailingGroup', () => {
    // Single quarantine is routine, but a cluster of quarantines with high failure rate
    // should be promoted to high-signal. Verify isFailingGroup contract is available for that behavior.
    const failingGroup = [
      { context: { result: 'failed', event_type: 'account_model_regression_quarantined' } },
      { context: { result: 'failed', event_type: 'account_model_regression_quarantined' } },
      { context: { result: 'failed', event_type: 'account_model_regression_quarantined' } },
      { context: { result: 'merged', event_type: 'account_model_regression_quarantined' } },
    ];
    // Verify isFailingGroup detects this cluster as failing (3 failed out of 4 = 75%)
    expect(isFailingGroup(failingGroup)).toBe(true);
    // Individual message would be routine
    expect(classifySignalLevel({ ...base, context: { event_type: 'account_model_regression_quarantined' } })).toBe('routine');
  });
});

// ── outcomeBreakdown unit tests ───────────────────────────────────────────────

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

// ── isFailingGroup unit tests ─────────────────────────────────────────────────

describe('isFailingGroup', () => {
  function m(result?: string) { return { context: result ? { result } : {} }; }

  test('returns false when fewer than 3 failures', () => {
    expect(isFailingGroup([m('failed'), m('failed'), m('merged')])).toBe(false);
  });

  test('returns false when 3+ failures but rate < 50%', () => {
    // 3 failed out of 7 = 43% < 50%
    expect(isFailingGroup([m('failed'), m('failed'), m('failed'), m('merged'), m('merged'), m('merged'), m('merged')])).toBe(false);
  });

  test('returns true when 3+ failures AND rate >= 50%', () => {
    // 3 failed out of 4 = 75%
    expect(isFailingGroup([m('failed'), m('failed'), m('failed'), m('merged')])).toBe(true);
  });

  test('returns true when all 3 are failed (100%)', () => {
    expect(isFailingGroup([m('failed'), m('failed'), m('failed')])).toBe(true);
  });

  test('returns false for empty array', () => {
    expect(isFailingGroup([])).toBe(false);
  });
});

// ── armingToastCopy / parseArmingOutcome unit tests ──────────────────────────

describe('arming outcomes', () => {
  describe('parseArmingOutcome', () => {
    test('recognizes steering', () => {
      expect(parseArmingOutcome('steering')).toBe('steering');
    });

    test('recognizes passive_note', () => {
      expect(parseArmingOutcome('passive_note')).toBe('passive_note');
    });

    test('recognizes routine_event', () => {
      expect(parseArmingOutcome('routine_event')).toBe('routine_event');
    });

    test('recognizes not_armed', () => {
      expect(parseArmingOutcome('not_armed')).toBe('not_armed');
    });

    test('recognizes duplicate', () => {
      expect(parseArmingOutcome('duplicate')).toBe('duplicate');
    });

    test('returns null for unknown outcomes', () => {
      expect(parseArmingOutcome('unknown')).toBeNull();
      expect(parseArmingOutcome(123)).toBeNull();
      expect(parseArmingOutcome(null)).toBeNull();
      expect(parseArmingOutcome(undefined)).toBeNull();
    });
  });

  describe('armingToastCopy', () => {
    test('returns truthful toast for duplicate resend', () => {
      expect(armingToastCopy('duplicate')).toBe('Message already sent — steer already armed');
    });

    test('returns generic copy for null outcome', () => {
      expect(armingToastCopy(null)).toBe('Message sent');
    });
  });

  describe('armingHintCopy', () => {
    test('returns truthful hint for duplicate resend', () => {
      expect(armingHintCopy('duplicate')).toBe('Message already sent — steer already armed.');
    });
  });
});

