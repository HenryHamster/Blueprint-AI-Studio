import { describe, it, expect } from 'vitest';
import {
  fmtScalar,
  buildSystemMaturitySection,
  buildCapabilityVerificationSection,
  buildCycleStartReconciliationSection,
  buildPruneCandidatesSection,
  buildDriftSection,
  buildOutcomeSection,
  buildClockAndRecentWorkSection,
  buildConversationThreadSection,
  buildFeedbackSection,
  buildOpenMessagesText,
  buildDirSummaryText,
  buildDirHealthSummary,
} from './strategic-sections.js';

describe('fmtScalar', () => {
  it('returns fallback for undefined/null', () => {
    expect(fmtScalar(undefined)).toBe('?');
    expect(fmtScalar(null)).toBe('?');
    expect(fmtScalar(undefined, 'N/A')).toBe('N/A');
  });

  it('stringifies primitives', () => {
    expect(fmtScalar(42)).toBe('42');
    expect(fmtScalar('hello')).toBe('hello');
    expect(fmtScalar(true)).toBe('true');
  });

  it('JSON-stringifies objects', () => {
    expect(fmtScalar({ a: 1 })).toBe('{"a":1}');
  });
});

describe('buildSystemMaturitySection', () => {
  it('returns empty for empty input', () => {
    expect(buildSystemMaturitySection([])).toBe('');
  });

  it('renders single system with score and mode', () => {
    const result = buildSystemMaturitySection([{
      system_id: 'auth',
      score: 0.85,
      recommended_mode: 'ship',
      last_audited_at: '2026-05-01',
    }]);
    expect(result).toContain('## System Maturity');
    expect(result).toContain('auth');
    expect(result).toContain('score=0.85');
    expect(result).toContain('recommended_mode=ship');
  });

  it('includes top issues when present', () => {
    const result = buildSystemMaturitySection([{
      system_id: 'db',
      score: 0.5,
      recommended_mode: 'audit',
      last_audited_at: '2026-05-01',
      top_issues: ['slow queries', 'missing indexes'],
    }]);
    expect(result).toContain('slow queries; missing indexes');
  });

  it('skips top_issues when empty array', () => {
    const result = buildSystemMaturitySection([{
      system_id: 'api',
      score: 0.9,
      recommended_mode: 'ship',
      top_issues: [],
    }]);
    expect(result).not.toContain('Top issues');
  });
});

describe('buildDriftSection', () => {
  it('returns empty for null input', () => {
    expect(buildDriftSection(null)).toBe('');
  });

  it('returns empty when no flagged entries', () => {
    const result = buildDriftSection({
      window_days: 7,
      by_direction: { 'd-1': { over_claim_flag: false, stalled_despite_churn_flag: false } },
    });
    expect(result).toBe('');
  });

  it('renders flagged directions', () => {
    const result = buildDriftSection({
      window_days: 7,
      by_direction: {
        'd-1': {
          over_claim_flag: true,
          stalled_despite_churn_flag: false,
          claimed_changes_count: 10,
          actually_merged_prs: 2,
          claim_shipped_consistency: 0.2,
        },
      },
    });
    expect(result).toContain('Drift Signals');
    expect(result).toContain('Direction d-1');
    expect(result).toContain('over_claim');
    expect(result).toContain('0.20');
  });
});

describe('buildCapabilityVerificationSection', () => {
  it('returns empty when problem model is missing or has no capabilities', () => {
    expect(buildCapabilityVerificationSection(undefined)).toBe('');
    expect(buildCapabilityVerificationSection({})).toBe('');
  });

  it('omits the section when capabilities are verified-only', () => {
    const result = buildCapabilityVerificationSection({
      capabilities: [{
        name: 'search',
        status: 'working',
        verification: {
          effective_status: 'working',
          present: ['backend/app/search.py'],
          missing: [],
        },
      }],
    });
    expect(result).toBe('');
  });

  it('renders claimed-but-absent capabilities with advisory framing', () => {
    const result = buildCapabilityVerificationSection({
      capabilities: [{
        name: 'payments',
        status: 'working',
        verification: {
          effective_status: 'absent',
          present: [],
          missing: ['backend/app/payments.py', 'backend/app/ledger.py'],
        },
      }],
    });
    expect(result).toContain('## Capability Verification (advisory)');
    expect(result).toContain('avoid hallucinating current behavior');
    expect(result).toContain('do not treat missing code as a ban on proposing future work');
    expect(result).toContain('**payments** (claimed status: working): CLAIMED-BUT-ABSENT');
    expect(result).toContain('backend/app/payments.py, backend/app/ledger.py');
  });

  it('renders partial capabilities with missing and present paths', () => {
    const result = buildCapabilityVerificationSection({
      capabilities: [{
        name: 'notifications',
        status: 'partial',
        verification: {
          effective_status: 'partial',
          present: ['backend/app/notify.py'],
          missing: ['backend/app/notify_worker.py'],
        },
      }],
    });
    expect(result).toContain('**notifications** (claimed status: partial): PARTIAL');
    expect(result).toContain('missing: backend/app/notify_worker.py; present: backend/app/notify.py');
  });
});

describe('buildCycleStartReconciliationSection', () => {
  it('returns empty when the reconciliation block is absent', () => {
    expect(buildCycleStartReconciliationSection(undefined)).toBe('');
  });

  it('renders the header for an empty (all-consistent) block', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      counts: {},
    });
    expect(result).toContain('## Cycle-start reconciliation (your starting state)');
    // No conflict subsections when every surface is consistent.
    expect(result).not.toContain('Capability status contradicted');
    expect(result).not.toContain('Orphaned open gaps');
    expect(result).not.toContain('Ungrounded active directions');
  });

  it('renders capability contradictions with stored-vs-grounded framing', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [{
        name: 'payments',
        stored_status: 'working',
        effective_status: 'absent',
        missing: ['backend/app/payments.py', 'backend/app/ledger.py'],
      }],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      counts: { capability_contradictions: 1 },
    });
    expect(result).toContain('Capability status contradicted by code (1)');
    expect(result).toContain('**payments**: stored=working but code shows ABSENT');
    expect(result).toContain('missing: backend/app/payments.py, backend/app/ledger.py');
  });

  it('shows how many missing backing paths were truncated', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [{
        name: 'search',
        stored_status: 'working',
        effective_status: 'absent',
        missing: ['backend/app/search_1.py', 'backend/app/search_2.py'],
        missing_total: 5,
      }],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      counts: { capability_contradictions: 1 },
    });
    expect(result).toContain('missing: backend/app/search_1.py, backend/app/search_2.py (+3 more not shown)');
  });

  it('renders orphaned gaps and ungrounded directions with their reasons', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [{ gap_id: 'g-7', name: 'cross-cycle memory', priority: 'high' }],
      ungrounded_directions: [
        { direction_id: 'd-1', text: 'refactor auth', reason: 'no_gap' },
        { direction_id: 'd-2', text: 'old idea', reason: 'gap_missing' },
      ],
      direction_pipeline: [],
      counts: { orphaned_gaps: 1, ungrounded_directions: 2 },
    });
    expect(result).toContain('Orphaned open gaps — unstaffed, not deferred (1)');
    expect(result).toContain('**cross-cycle memory** [high] (gap g-7)');
    expect(result).toContain('Ungrounded active directions — advance no tracked gap (2)');
    expect(result).toContain('refactor auth (id: d-1) — no gap_id linkage');
    expect(result).toContain('old idea (id: d-2) — its gap_id no longer resolves to an active gap');
  });

  it('flags orphaned blocked gaps distinctly from active ones', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [
        { gap_id: 'g-a', name: 'active orphan', priority: 'high', status: 'active' },
        { gap_id: 'g-b', name: 'blocked orphan', priority: 'low', status: 'blocked' },
      ],
      ungrounded_directions: [],
      direction_pipeline: [],
      counts: { orphaned_gaps: 2 },
    });
    expect(result).toContain('**active orphan** [high] (gap g-a)');
    expect(result).not.toContain('active orphan** [high] (gap g-a) — BLOCKED');
    expect(result).toContain('**blocked orphan** [low] (gap g-b) — BLOCKED');
  });

  it('splits the research pipeline into waiting / in-progress / idle', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [
        { direction_id: 'd-w', text: 'waiting dir', state: 'research_waiting_on_action', in_flight: 0, findings: 3 },
        { direction_id: 'd-p', text: 'busy dir', state: 'tasks_in_progress', in_flight: 2, findings: 0 },
        { direction_id: 'd-i', text: 'quiet dir', state: 'idle', in_flight: 0, findings: 0 },
      ],
      counts: { deferred_gaps: 1, active_directions: 3 },
    });
    expect(result).toContain('Research pipeline by direction (1 with tasks in progress, 1 waiting on action, 1 idle; 1 gaps deferred)');
    expect(result).toContain('waiting dir (id: d-w) — 3 recent finding(s), 0 tasks in flight');
    expect(result).toContain('busy dir (id: d-p) — 2 task(s) in flight');
    expect(result).toContain('Idle (no findings, no tasks in flight): quiet dir');
  });

  it('shows a "+N more" tail when a list was capped below its true total', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [{ gap_id: 'g-1', name: 'shown gap', priority: 'medium' }],
      ungrounded_directions: [],
      direction_pipeline: [],
      counts: { orphaned_gaps: 5 },
    });
    expect(result).toContain('Orphaned open gaps — unstaffed, not deferred (5)');
  });

  it('uses true pipeline totals when the per-direction list was capped', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [
        { direction_id: 'd-w', text: 'shown waiting', state: 'research_waiting_on_action', in_flight: 0, findings: 2 },
        { direction_id: 'd-p', text: 'shown progress', state: 'tasks_in_progress', in_flight: 1, findings: 0 },
      ],
      counts: {
        deferred_gaps: 0,
        pipeline_waiting_on_action: 4,
        pipeline_tasks_in_progress: 3,
        pipeline_idle: 5,
        direction_pipeline_overflow: 10,
      },
    });
    expect(result).toContain('Research pipeline by direction (3 with tasks in progress, 4 waiting on action, 5 idle; 0 gaps deferred)');
    expect(result).toContain('Only the first 2 active directions are listed below (+10 more not shown).');
    expect(result).toContain('+3 more waiting direction(s) not shown');
    expect(result).toContain('+2 more in-progress direction(s) not shown');
    expect(result).toContain('Idle (no findings, no tasks in flight): +5 direction(s) not shown');
  });

  it('surfaces merge-accounting drift, flagging only verified-not-landed titles', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      merge_accounting: [{
        direction_id: 'd-drift',
        direction_text: 'shipping dir',
        not_landed_titles: ['ghost feat'],
        not_landed_titles_overflow: 0,
        landed: 2,
        not_landed: 1,
        unverifiable: 1,
      }],
      counts: {
        merge_accounting: 1,
        merge_landed: 2,
        merge_not_landed: 1,
        merge_unverifiable: 1,
      },
    });
    expect(result).toContain('Merge-accounting drift — badge says shipped, ancestry says NOT landed (1 direction(s), 1 title(s); 1 unverifiable)');
    expect(result).toContain('shipping dir (id: d-drift) — 1 NOT landed, 2 landed, 1 unverifiable');
    expect(result).toContain('✗ ghost feat');
  });

  it('does not render merge-accounting when there are no not-landed conflicts', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      merge_accounting: [],
      // Only landed + unverifiable tallies — no conflict to surface.
      counts: { merge_accounting: 0, merge_landed: 3, merge_not_landed: 0, merge_unverifiable: 2 },
    });
    expect(result).not.toContain('Merge-accounting drift');
  });

  it('shows "+N more" tails for capped merge-accounting titles and directions', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      merge_accounting: [{
        direction_id: 'd-1',
        direction_text: 'noisy dir',
        not_landed_titles: ['ghost 0', 'ghost 1'],
        not_landed_titles_overflow: 3,
        landed: 0,
        not_landed: 5,
        unverifiable: 0,
      }],
      counts: {
        merge_accounting: 4,
        merge_landed: 0,
        merge_not_landed: 5,
        merge_unverifiable: 0,
      },
    });
    expect(result).toContain('noisy dir (id: d-1) — 5 NOT landed, 0 landed, 0 unverifiable');
    expect(result).toContain('+3 more not-landed title(s) not shown');
    expect(result).toContain('+3 more direction(s) with drift not shown');
  });
});

describe('buildPruneCandidatesSection', () => {
  it('returns empty string for undefined or empty input', () => {
    expect(buildPruneCandidatesSection(undefined)).toBe('');
    expect(buildPruneCandidatesSection({})).toBe('');
    expect(buildPruneCandidatesSection({
      resolved_gaps: [], absent_capabilities: [], expired_holds: [],
    })).toBe('');
  });

  it('renders resolved gaps with their direction reason', () => {
    const result = buildPruneCandidatesSection({
      model_size_status: 'oversized',
      resolved_gaps: [
        { gap_id: 'g-ret', name: 'retired work', reason: 'direction_retired', directions: ['d-1'] },
        { gap_id: 'g-ship', name: 'shipped work', reason: 'direction_shipped', directions: ['d-2'] },
      ],
      absent_capabilities: [],
      expired_holds: [],
      counts: { resolved_gaps: 2, absent_capabilities: 0, expired_holds: 0 },
    });
    expect(result).toContain('Concrete prune candidates (model is oversized)');
    expect(result).toContain('Resolved-yet-open gaps (2)');
    expect(result).toContain('**retired work** (gap g-ret) — its direction was retired [d-1]');
    expect(result).toContain('**shipped work** (gap g-ship) — its direction shipped merged PRs [d-2]');
  });

  it('renders absent capabilities and expired holds with overflow counts', () => {
    const result = buildPruneCandidatesSection({
      model_size_status: 'large',
      resolved_gaps: [],
      absent_capabilities: [{ name: 'ghost', stored_status: 'working' }],
      expired_holds: [{ gap_id: 'g-exp', name: 'parked', hold_until: '2026-01-01T00:00:00+00:00' }],
      counts: { resolved_gaps: 0, absent_capabilities: 3, expired_holds: 1 },
    });
    expect(result).toContain('Claimed-but-absent capabilities (3)');
    expect(result).toContain('**ghost** (stored status=working) — code is absent');
    expect(result).toContain('+2 more not shown');
    expect(result).toContain('Expired deferral holds (1)');
    expect(result).toContain('**parked** (gap g-exp) — hold expired 2026-01-01T00:00:00+00:00');
  });
});

describe('buildOutcomeSection', () => {
  it('returns empty for undefined', () => {
    expect(buildOutcomeSection(undefined)).toBe('');
  });

  it('renders window and overall metrics', () => {
    const result = buildOutcomeSection({
      window: { vision_revision: 3, from: '2026-01-01', to: '2026-05-01' },
      overall: { cycles: 20, merged: 15, failed: 5, success_rate: 0.75, trend: 'up', product_ratio: 0.6 },
      systems: [],
      directions: [],
    });
    expect(result).toContain('Outcome evaluation');
    expect(result).toContain('vision rev 3');
    expect(result).toContain('20 cycles');
    expect(result).toContain('75% success rate');
    expect(result).toContain('product ratio: 60%');
  });

  it('renders direction outcomes with type distribution', () => {
    const result = buildOutcomeSection({
      directions: [{
        id: 'd-1',
        text: 'improve perf',
        status: 'active',
        cycles: 5,
        merged: 3,
        failed: 2,
        type_distribution: { refactor: 2, fix: 1 },
        product_ratio: 0.4,
        confirmed: ['faster queries'],
      }],
    });
    expect(result).toContain('improve perf');
    expect(result).toContain('Type mix: 2 refactor, 1 fix');
    expect(result).toContain('Confirmed (shipped): faster queries');
  });

  it('truncates failed_approaches to 5', () => {
    const result = buildOutcomeSection({
      directions: [{
        id: 'd-1',
        text: 'test',
        status: 'active',
        failed_approaches: ['a', 'b', 'c', 'd', 'e', 'f'],
      }],
    });
    expect(result).toContain('Failed approaches: a; b; c; d; e');
    expect(result).not.toContain('; f');
  });
});

describe('buildClockAndRecentWorkSection', () => {
  it('returns empty strings for undefined', () => {
    const { clockHeader, recentWorkSection } = buildClockAndRecentWorkSection(undefined);
    expect(clockHeader).toBe('');
    expect(recentWorkSection).toBe('');
  });

  it('builds clock header from hours_since_last_strategic', () => {
    const { clockHeader } = buildClockAndRecentWorkSection({
      hours_since_last_strategic: 6,
      rolling_rates: {
        since_last_strategic: { merged: 3, finished: 5, spawned: 5, merge_rate: 0.6 },
      },
    });
    expect(clockHeader).toContain('6h since your last strategic cycle');
    expect(clockHeader).toContain('3 of 5 finished tasks merged (60%)');
  });

  it('adds in-flight note when active_by_scope present', () => {
    const { clockHeader } = buildClockAndRecentWorkSection({
      hours_since_last_strategic: 2,
      rolling_rates: { since_last_strategic: { merged: 1, finished: 1, merge_rate: 1.0 } },
      active_by_scope: { small: 3, medium: 1 },
    });
    expect(clockHeader).toContain('Currently in flight: small=3, medium=1');
  });

  it('renders rolling rates section', () => {
    const { recentWorkSection } = buildClockAndRecentWorkSection({
      rolling_rates: {
        last_24h: { merged: 2, finished: 4, merge_rate: 0.5 },
      },
    });
    expect(recentWorkSection).toContain('Recent Work & Rolling Outcomes');
    expect(recentWorkSection).toContain('last_24h: 2/4 merged (50%)');
  });
});

describe('buildConversationThreadSection', () => {
  it('returns empty for empty array', () => {
    expect(buildConversationThreadSection([])).toBe('');
  });

  it('renders open messages with id', () => {
    const result = buildConversationThreadSection([{
      id: 'msg-1',
      status: 'open',
      from: 'user',
      type: 'FEEDBACK',
      content: 'please add X',
      created_at: new Date().toISOString(),
    }]);
    expect(result).toContain('Conversation History');
    expect(result).toContain('OPEN');
    expect(result).toContain('id: msg-1');
    expect(result).toContain('please add X');
  });

  it('renders resolved messages with resolution', () => {
    const result = buildConversationThreadSection([{
      id: 'msg-2',
      status: 'resolved',
      from: 'user',
      type: 'NOTE',
      content: 'some note',
      resolution: 'handled it',
      created_at: new Date().toISOString(),
    }]);
    expect(result).toContain('RESOLVED');
    expect(result).toContain('-> handled it');
    expect(result).not.toContain('id: msg-2');
  });
});

describe('buildFeedbackSection', () => {
  it('returns empty when no FEEDBACK messages', () => {
    const result = buildFeedbackSection([
      { id: '1', type: 'NOTE', content: 'hi', from: 'user', created_at: new Date().toISOString() },
    ]);
    expect(result).toBe('');
  });

  it('includes only FEEDBACK messages', () => {
    const result = buildFeedbackSection([
      { id: '1', type: 'FEEDBACK', content: 'add dark mode', from: 'user', created_at: new Date().toISOString() },
      { id: '2', type: 'NOTE', content: 'ignore me', from: 'user', created_at: new Date().toISOString() },
    ]);
    expect(result).toContain('User Feature Requests');
    expect(result).toContain('add dark mode');
    expect(result).not.toContain('ignore me');
  });
});

describe('buildOpenMessagesText', () => {
  it('returns empty string for empty array', () => {
    expect(buildOpenMessagesText([])).toBe('');
  });

  it('formats all messages with type and id', () => {
    const result = buildOpenMessagesText([
      { id: 'x1', type: 'NOTE', content: 'hello', from: 'agent', created_at: new Date().toISOString() },
    ]);
    expect(result).toContain('[NOTE]');
    expect(result).toContain('id: x1');
    expect(result).toContain('hello');
  });
});

describe('buildDirSummaryText', () => {
  it('returns no-directions message for empty array', () => {
    expect(buildDirSummaryText([])).toBe('No directions exist yet.');
  });

  it('formats single active direction with stats', () => {
    const result = buildDirSummaryText([{
      id: 'd-1',
      text: 'improve perf',
      status: 'active',
      priority: 'high',
      total_cycles: 5,
      prs_merged: 3,
      tasks_failed: 1,
      tasks_failed_infra: 0,
    }]);
    expect(result).toContain('[high]');
    expect(result).toContain('improve perf');
    expect(result).toContain('5 cycles');
    expect(result).toContain('3 merged');
    expect(result).toContain('1 failed');
  });

  it('shows SATURATED flag when applicable', () => {
    const result = buildDirSummaryText([{
      id: 'd-2', text: 'sat dir', status: 'active', priority: 'medium',
      total_cycles: 0, possibly_saturated: true,
    }]);
    expect(result).toContain('[SATURATED]');
  });

  it('shows paused status with reason', () => {
    const result = buildDirSummaryText([{
      id: 'd-3', text: 'paused dir', status: 'paused', priority: 'low',
      total_cycles: 0, auto_paused: true, paused_reason: 'waiting for feedback',
    }]);
    expect(result).toContain('[AUTO_PAUSED: waiting for feedback]');
  });

  it('shows user-paused status', () => {
    const result = buildDirSummaryText([{
      id: 'd-4', text: 'user paused', status: 'paused', priority: 'low',
      total_cycles: 0, auto_paused: false, paused_reason: '',
    }]);
    expect(result).toContain('[USER_PAUSED]');
  });

  it('orders multiple directions by index', () => {
    const result = buildDirSummaryText([
      { id: 'd-1', text: 'first', status: 'active', priority: 'high', total_cycles: 0 },
      { id: 'd-2', text: 'second', status: 'active', priority: 'medium', total_cycles: 0 },
    ]);
    expect(result).toContain('1. ');
    expect(result).toContain('2. ');
    const idx1 = result.indexOf('first');
    const idx2 = result.indexOf('second');
    expect(idx1).toBeLessThan(idx2);
  });
});

describe('buildDirHealthSummary', () => {
  it('shows correct slot counts', () => {
    const dirs = [
      { status: 'active', category: 'product', possibly_saturated: false },
      { status: 'active', category: 'structural', possibly_saturated: false },
      { status: 'active', category: 'product', possibly_saturated: false },
    ];
    const result = buildDirHealthSummary(dirs, 10);
    expect(result).toContain('3/10 slots used (7 available)');
    expect(result).toContain('Product directions: 2');
    expect(result).toContain('No critical gaps detected');
  });

  it('detects NO_PRODUCT_DIRECTIONS gap', () => {
    const dirs = [
      { status: 'active', category: 'structural', possibly_saturated: false },
    ];
    const result = buildDirHealthSummary(dirs, 10);
    expect(result).toContain('NO_PRODUCT_DIRECTIONS');
  });

  it('detects ALL_SATURATED gap', () => {
    const dirs = [
      { status: 'active', category: 'structural', possibly_saturated: true },
      { status: 'active', category: 'product', possibly_saturated: true },
    ];
    const result = buildDirHealthSummary(dirs, 10);
    expect(result).toContain('ALL_SATURATED');
  });

  it('detects BELOW_MINIMUM gap', () => {
    const dirs = [
      { status: 'active', category: 'product', possibly_saturated: false },
    ];
    const result = buildDirHealthSummary(dirs, 10);
    expect(result).toContain('BELOW_MINIMUM');
  });

  it('shows paused direction counts', () => {
    const dirs = [
      { status: 'active', category: 'product', possibly_saturated: false },
      { status: 'active', category: 'product', possibly_saturated: false },
      { status: 'active', category: 'product', possibly_saturated: false },
      { status: 'paused', auto_paused: true, category: 'structural' },
      { status: 'paused', auto_paused: false, category: 'product' },
    ];
    const result = buildDirHealthSummary(dirs, 10);
    expect(result).toContain('Paused: 2 (1 auto, 1 user)');
  });
});
