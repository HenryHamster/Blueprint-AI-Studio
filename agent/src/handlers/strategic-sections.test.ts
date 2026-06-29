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
  buildQuestionSection,
  buildOpenMessagesText,
  buildDirSummaryText,
  buildDirHealthSummary,
  buildCiComplianceSection,
  buildSweepComparisonSection,
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

  it('buildCycleStartReconciliationSection renders stale active moves staffed deferrals and phantom refs', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      staffed_but_deferred: [{ gap_id: 'g-defer', name: 'deferred but staffed', priority: 'high', status: 'active' }],
      ungrounded_directions: [],
      stale_active_moves: [{ gap_id: 'g-stale', gap_name: 'stale staffed gap', stale_direction_ids: ['d-retired'] }],
      phantom_direction_refs: [{ direction_id: 'd-4890df9e', cited_in: 'intent.current_focus' }, { direction_id: 'd-aaaabbbb', cited_in: 'running_update' }],
      direction_pipeline: [],
      counts: { staffed_but_deferred: 1, stale_active_moves: 1, phantom_direction_refs: 2 },
    });

    expect(result).toContain('Staffed-but-deferred gaps — contradictory staffing state (1)');
    expect(result).toContain('**deferred but staffed** [high] (gap g-defer, status=active)');
    expect(result).toContain('Stale gap active_moves — listed direction is no longer active (1)');
    expect(result).toContain('**stale staffed gap** (gap g-stale) — stale active_moves: d-retired');
    expect(result).toContain('Phantom direction refs — prose cites missing directions (2)');
    expect(result).toContain('d-4890df9e cited in intent.current_focus');
    expect(result).toContain('d-aaaabbbb cited in running_update');
  });

  it('splits the research pipeline into held / recent / waiting / in-progress / idle', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [
        { direction_id: 'd-w', text: 'waiting dir', state: 'research_waiting_on_action', in_flight: 0, findings: 3 },
        { direction_id: 'd-p', text: 'busy dir', state: 'tasks_in_progress', in_flight: 2, findings: 0 },
        { direction_id: 'd-h', text: 'held dir', state: 'held_on_dependency', in_flight: 0, held: 1, findings: 1 },
        { direction_id: 'd-r', text: 'recent dir', state: 'recently_converted', in_flight: 0, recently_completed: 2, findings: 0 },
        { direction_id: 'd-i', text: 'quiet dir', state: 'idle', in_flight: 0, findings: 0 },
      ],
      counts: {
        deferred_gaps: 1,
        active_directions: 5,
        pipeline_held_on_dependency: 1,
        pipeline_recently_converted: 1,
      },
    });
    expect(result).toContain('Research pipeline by direction (1 with tasks in progress, 1 live-held, 1 recently converted, 1 waiting on action, 1 idle; 1 gaps deferred)');
    expect(result).toContain('waiting dir (id: d-w) — 3 recent finding(s), 0 tasks in flight');
    expect(result).toContain('busy dir (id: d-p) — 2 task(s) in flight');
    expect(result).toContain('held dir (id: d-h) — 1 held task(s), 1 recent finding(s)');
    expect(result).toContain('recent dir (id: d-r) — 2 recently completed task(s), 0 recent finding(s)');
    expect(result).toContain('Idle (no findings, no tasks in flight): quiet dir');
  });

  it('does not fold held or recent completion into waiting-on-action', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [
        { direction_id: 'd-h', text: 'held lane', state: 'held_on_dependency', in_flight: 0, held: 1, findings: 2 },
        { direction_id: 'd-r', text: 'recent lane', state: 'recently_converted', in_flight: 0, recently_completed: 1, findings: 2 },
      ],
      counts: {
        deferred_gaps: 0,
        pipeline_waiting_on_action: 0,
        pipeline_tasks_in_progress: 0,
        pipeline_held_on_dependency: 1,
        pipeline_recently_converted: 1,
        pipeline_idle: 0,
      },
    });
    expect(result).toContain('1 live-held, 1 recently converted, 0 waiting on action');
    expect(result).toContain('Live-held on dependency (blocked or parked work is not waiting-on-action):');
    expect(result).toContain('Recently converted/completed (fresh work before the cycle start — not stalled):');
    expect(result).not.toContain('held lane (id: d-h) — 2 recent finding(s), 0 tasks in flight');
    expect(result).not.toContain('recent lane (id: d-r) — 2 recent finding(s), 0 tasks in flight');
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
    expect(result).toContain('Research pipeline by direction (3 with tasks in progress, 0 live-held, 0 recently converted, 4 waiting on action, 5 idle; 0 gaps deferred)');
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

  it('renders a STALE warning naming broken and ungrounded refs for an absent-citation bug report', () => {
    const result = buildOpenMessagesText([
      {
        id: 'b1',
        type: 'BUG_REPORT',
        content: 'something is broken',
        from: 'agent',
        created_at: new Date().toISOString(),
        citation_state: 'absent',
        broken_refs: ['backend/app/x.py'],
        ungrounded_refs: ['backend/app/y.py:foo'],
      },
    ]);
    expect(result).toContain('⚠ STALE');
    expect(result).toContain('files backend/app/x.py');
    expect(result).toContain('symbols backend/app/y.py:foo');
    expect(result).toContain('do NOT route into a fix/investigation');
    // base line still rendered unchanged
    expect(result).toContain('[BUG_REPORT]');
    expect(result).toContain('id: b1');
  });

  it('does NOT render a STALE warning for a message with no citation_state', () => {
    const result = buildOpenMessagesText([
      { id: 'x2', type: 'BUG_REPORT', content: 'still broken', from: 'agent', created_at: new Date().toISOString() },
    ]);
    expect(result).not.toContain('STALE');
    expect(result).toContain('[BUG_REPORT]');
  });

  it('does NOT render a STALE warning for a present citation_state', () => {
    const result = buildOpenMessagesText([
      { id: 'x3', type: 'BUG_REPORT', content: 'present', from: 'agent', created_at: new Date().toISOString(), citation_state: 'present' },
    ]);
    expect(result).not.toContain('STALE');
  });

  it('renders a PARTIALLY GROUNDED advisory (not STALE) for a mixed present-citation report with refs', () => {
    const result = buildOpenMessagesText([
      {
        id: 'm1',
        type: 'BUG_REPORT',
        content: 'partially grounded',
        from: 'agent',
        created_at: new Date().toISOString(),
        citation_state: 'present',
        broken_refs: ['backend/app/x.py'],
        ungrounded_refs: ['backend/app/y.py:foo'],
      },
    ]);
    expect(result).toContain('⚠ PARTIALLY GROUNDED');
    expect(result).toContain('files backend/app/x.py');
    expect(result).toContain('symbols backend/app/y.py:foo');
    // Mixed reports stay actionable — no stale-discount language.
    expect(result).not.toContain('STALE');
    expect(result).not.toContain('do NOT route');
    expect(result).toContain('routing is allowed');
  });

  it('renders a PARTIALLY GROUNDED advisory for an indeterminate report with refs', () => {
    const result = buildOpenMessagesText([
      {
        id: 'm2',
        type: 'BLOCKER',
        content: 'could not verify some refs',
        from: 'agent',
        created_at: new Date().toISOString(),
        citation_state: 'indeterminate',
        broken_refs: ['backend/app/z.py'],
      },
    ]);
    expect(result).toContain('⚠ PARTIALLY GROUNDED');
    expect(result).toContain('files backend/app/z.py');
    expect(result).not.toContain('STALE');
  });

  it('renders no advisory for a present citation_state with no refs', () => {
    const result = buildOpenMessagesText([
      {
        id: 'm3',
        type: 'BUG_REPORT',
        content: 'fully grounded',
        from: 'agent',
        created_at: new Date().toISOString(),
        citation_state: 'present',
        broken_refs: [],
        ungrounded_refs: [],
      },
    ]);
    expect(result).not.toContain('STALE');
    expect(result).not.toContain('PARTIALLY GROUNDED');
  });

  it('renders the STALE marker without a ref list when ref arrays are empty', () => {
    const result = buildOpenMessagesText([
      {
        id: 'b2',
        type: 'BLOCKER',
        content: 'gone',
        from: 'agent',
        created_at: new Date().toISOString(),
        citation_state: 'absent',
        broken_refs: [],
        ungrounded_refs: [],
      },
    ]);
    expect(result).toContain('⚠ STALE — cited code gone on HEAD.');
    expect(result).not.toContain('files ');
    expect(result).not.toContain('symbols ');
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
    expect(result).not.toContain('proven');
    expect(result).not.toContain('consumed downstream');
  });

  it('renders proven merges with consumed downstream subset when present', () => {
    const result = buildDirSummaryText([{
      id: 'd-proof',
      text: 'close proof gap',
      status: 'active',
      priority: 'high',
      total_cycles: 4,
      prs_merged: 5,
      proven_prs_merged: 2,
      consumed_prs_merged: 1,
    }]);
    expect(result).toContain('5 merged');
    expect(result).toContain('2 proven (1 consumed downstream)');
  });

  it('omits zero-only proof fields from legacy summaries', () => {
    const result = buildDirSummaryText([{
      id: 'd-legacy-proof',
      text: 'legacy direction',
      status: 'active',
      priority: 'medium',
      total_cycles: 1,
      prs_merged: 2,
      proven_prs_merged: 0,
      consumed_prs_merged: 0,
    }]);
    expect(result).toContain('2 merged');
    expect(result).not.toContain('0 proven');
    expect(result).not.toContain('consumed downstream');
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

  it('renders research spawn-gate breakdown, sample sizes, and reasons when spawn_gate_metrics is present', () => {
    const result = buildDirSummaryText([{
      id: 'd-sg',
      text: 'direction with gate',
      status: 'active',
      priority: 'high',
      total_cycles: 3,
      spawn_gate_metrics: {
        challenged: 5,
        refuted: 2,
        survived: 3,
        explicit_pass_count: 2,
        fail_open_count: 1,
        unresolved_count: 1,
        manifest_excluded_fail_closed_count: 1,
        manifest_excluded_fail_closed_samples: [
          {
            hash: 'excluded-hash',
            title: 'Budget-excluded finding',
            reason: 'manifest cap',
          },
        ],
        challenged_spawned: 3,
        challenged_merged: 2,
        unchallenged_spawned: 4,
        unchallenged_merged: 3,
        challenged_merge_rate: 0.667,
        unchallenged_merge_rate: 0.75,
        refuted_reasons: ['stale: defect absent on HEAD', 'over-claimed impact'],
      },
    }]);
    expect(result).toContain('Research spawn-gate');
    expect(result).toContain('5 challenged');
    expect(result).toContain('3 survived');
    expect(result).toContain('2 refuted');
    // Decision-quality breakdown distinguishes explicit pass from fail-open / unresolved.
    expect(result).toContain('2 explicit-pass');
    expect(result).toContain('1 fail-open');
    expect(result).toContain('1 unresolved');
    expect(result).toContain('1 manifest-excluded fail-closed');
    expect(result).toContain('Budget-excluded finding');
    // Merge-rate percentages travel with their merged/spawned denominators.
    expect(result).toContain('67% challenged (2/3)');
    expect(result).toContain('75% unchallenged (3/4)');
    // Compact inline sample of refuted/unresolved reasons.
    expect(result).toContain('refuted/unresolved reasons:');
    expect(result).toContain('stale: defect absent on HEAD');
    expect(result).toContain('over-claimed impact');
  });

  it('omits research spawn-gate line when spawn_gate_metrics is absent', () => {
    const result = buildDirSummaryText([{
      id: 'd-nosg',
      text: 'direction without gate',
      status: 'active',
      priority: 'medium',
      total_cycles: 2,
    }]);
    expect(result).not.toContain('Research spawn-gate');
    expect(result).not.toContain('challenged');
  });

  it('renders n/a merge rates (with zero denominators) when merge rates are null', () => {
    const result = buildDirSummaryText([{
      id: 'd-sg-null',
      text: 'direction with partial gate data',
      status: 'active',
      priority: 'medium',
      total_cycles: 1,
      spawn_gate_metrics: {
        challenged: 3,
        refuted: 3,
        survived: 0,
        challenged_merge_rate: null,
        unchallenged_merge_rate: null,
      },
    }]);
    expect(result).toContain('Research spawn-gate');
    expect(result).toContain('n/a challenged (0/0)');
    expect(result).toContain('n/a unchallenged (0/0)');
  });

  it('omits the reasons sample when no refuted_reasons are present', () => {
    const result = buildDirSummaryText([{
      id: 'd-sg-noreasons',
      text: 'direction with gate, no reasons',
      status: 'active',
      priority: 'low',
      total_cycles: 1,
      spawn_gate_metrics: {
        challenged: 2,
        refuted: 0,
        survived: 2,
        explicit_pass_count: 2,
        fail_open_count: 0,
        unresolved_count: 0,
        challenged_merge_rate: 0.5,
        unchallenged_merge_rate: null,
      },
    }]);
    expect(result).toContain('Research spawn-gate');
    expect(result).not.toContain('refuted/unresolved reasons:');
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

  it('anchors headline count and slots on authoritative count when list is dropped', () => {
    // directions_summary fully dropped under budget, but the authoritative
    // count says 12 active against a real ceiling of 15.
    const result = buildDirHealthSummary([], 15, { active: 12, paused: 1 });
    expect(result).toContain('Active: 12/15 slots used (3 available)');
    expect(result).not.toContain('BELOW_MINIMUM');
    // Cannot prove product absence / saturation from an empty list — suppressed.
    expect(result).not.toContain('NO_PRODUCT_DIRECTIONS');
    expect(result).not.toContain('ALL_SATURATED');
    expect(result).toContain('Showing 0 of 12 active directions');
    expect(result).toContain('Paused: 1');
  });

  it('does not emit BELOW_MINIMUM when authoritative count >= 3 but list trimmed below it', () => {
    // Only 2 detail rows shipped, but the true active count is 8.
    const dirs = [
      { status: 'active', category: 'product', possibly_saturated: false },
      { status: 'active', category: 'structural', possibly_saturated: false },
    ];
    const result = buildDirHealthSummary(dirs, 15, { active: 8, paused: 0 });
    expect(result).toContain('Active: 8/15 slots used (7 available)');
    expect(result).not.toContain('BELOW_MINIMUM');
    expect(result).toContain('Showing 2 of 8 active directions');
  });

  it('still flags BELOW_MINIMUM when authoritative count is genuinely low', () => {
    const result = buildDirHealthSummary([], 15, { active: 1, paused: 0 });
    expect(result).toContain('Active: 1/15');
    expect(result).toContain('BELOW_MINIMUM');
  });

  it('falls back to shipped list when authoritative count is absent', () => {
    const dirs = [
      { status: 'active', category: 'product', possibly_saturated: false },
      { status: 'active', category: 'structural', possibly_saturated: false },
      { status: 'active', category: 'product', possibly_saturated: false },
    ];
    const result = buildDirHealthSummary(dirs, 10);
    expect(result).toContain('3/10 slots used (7 available)');
    expect(result).not.toContain('Showing');
  });
});

describe('buildDirSummaryText authoritative count', () => {
  it('does not render "No directions exist yet." when count>0 but list dropped', () => {
    const result = buildDirSummaryText([], 9);
    expect(result).not.toContain('No directions exist yet.');
    expect(result).toContain('9 active direction(s) exist');
    expect(result).toContain('./fetch direction');
  });

  it('renders "No directions exist yet." when count is genuinely zero', () => {
    expect(buildDirSummaryText([], 0)).toBe('No directions exist yet.');
    expect(buildDirSummaryText([])).toBe('No directions exist yet.');
  });

  it('shows trimmed "M of N" header when list shorter than authoritative count', () => {
    const dirs = [
      { id: 'd1', status: 'active', text: 'Dir one', total_cycles: 1 },
      { id: 'd2', status: 'active', text: 'Dir two', total_cycles: 1 },
    ];
    const result = buildDirSummaryText(dirs, 9);
    expect(result).toContain('Showing 2 of 9 active directions');
    expect(result).toContain('Dir one');
  });
});

describe('buildQuestionSection', () => {
  it('returns empty string when no QUESTION messages', () => {
    expect(buildQuestionSection([])).toBe('');
    expect(buildQuestionSection([{ type: 'FEEDBACK', id: '1', from: 'op', content: 'x' }])).toBe('');
  });

  it('renders QUESTION messages under the heading with id, from, age, content', () => {
    const msg = { type: 'QUESTION', id: 'abc123', from: 'operator', content: 'What is the plan?', created_at: new Date().toISOString() };
    const result = buildQuestionSection([msg]);
    expect(result).toContain('## Operator Questions Awaiting Answers');
    expect(result).toContain('id: abc123');
    expect(result).toContain('from: operator');
    expect(result).toContain('What is the plan?');
    expect(result).toContain('./messages resolve');
  });
});

describe('buildCiComplianceSection', () => {
  it('returns empty for nullish and non-object input', () => {
    expect(buildCiComplianceSection(null)).toBe('');
    expect(buildCiComplianceSection(undefined)).toBe('');
    expect(buildCiComplianceSection('bad')).toBe('');
  });

  it('returns empty when compliant is true', () => {
    expect(buildCiComplianceSection({ compliant: true, violations: [{ section: 'backend', rel: 'app/foo.py', count: 200, cap: 100 }] })).toBe('');
  });

  it('renders capped and uncapped module-size violations', () => {
    const result = buildCiComplianceSection({
      compliant: false,
      head_sha: 'deadbeefcafe1234',
      violations: [
        { section: 'backend', rel: 'app/canary.py', count: 150, cap: 100 },
        { section: 'agent', rel: 'handlers/big.ts', count: 1700, cap: null },
      ],
      branch_literal_violations: [],
    });
    expect(result).toContain('## CI Compliance (automain HEAD)');
    expect(result).toContain('Compliant: **no**  (SHA: deadbeef)');
    expect(result).toContain('### Module-size violations');
    expect(result).toContain('python3 backend/testing/check_size_limits.py --fix');
    expect(result).toContain('  - [backend] app/canary.py: 150 lines (limit: 100)');
    expect(result).toContain('  - [agent] handlers/big.ts: 1700 lines (limit: uncapped)');
  });

  it('renders without sha when head_sha is absent', () => {
    const result = buildCiComplianceSection({
      compliant: false,
      violations: [{ section: 'backend', rel: 'app/foo.py', count: 200, cap: 100 }],
      branch_literal_violations: [],
    });
    expect(result).toContain('Compliant: **no**');
    expect(result).not.toContain('SHA:');
  });

  it('renders branch-literal violations', () => {
    const result = buildCiComplianceSection({
      compliant: false,
      violations: [],
      branch_literal_violations: [
        { section: 'agent', rel: 'src/handlers/review-fix.ts', line: 12, text: 'if (baseBranch === "main") {' },
        { section: 'agent', rel: 'src/prompt.ts', line: 9, text: 'if (branch !== "automain") {' },
      ],
    });
    expect(result).toContain('### Branch-literal violations');
    expect(result).toContain('resolveBaseBranch()');
    expect(result).toContain('  - src/handlers/review-fix.ts:12: if (baseBranch === "main") {');
    expect(result).toContain('  - src/prompt.ts:9: if (branch !== "automain") {');
  });
});

describe('buildSweepComparisonSection', () => {
  it('returns empty string for undefined or empty input', () => {
    expect(buildSweepComparisonSection(undefined)).toBe('');
    expect(buildSweepComparisonSection({})).toBe('');
  });

  it('returns empty string when no sweep has all arms verdicted', () => {
    // One arm is UNMEASURED (criterion_met === null)
    const result = buildSweepComparisonSection({
      'sweep-1': [
        { id: 'k-1', title: 'arm A', arm_label: 'arm-a', criterion_met: true },
        { id: 'k-2', title: 'arm B', arm_label: 'arm-b', criterion_met: null },
      ],
    });
    expect(result).toBe('');
  });

  it('returns empty string for single-arm sweeps', () => {
    const result = buildSweepComparisonSection({
      'sweep-single': [
        { id: 'k-r', title: 'arm R', arm_label: 'r', criterion_met: true },
      ],
    });
    expect(result).toBe('');
  });

  it('returns empty string when an arm is unavailable', () => {
    const result = buildSweepComparisonSection({
      'sweep-2': [
        { id: 'k-3', title: 'arm A', arm_label: 'arm-a', criterion_met: true },
        { id: 'k-4', title: 'arm B', arm_label: 'arm-b', criterion_met: false, unavailable_cause: 'compute_failed' },
      ],
    });
    expect(result).toBe('');
  });

  it('returns empty string when arms have duplicate labels', () => {
    expect(buildSweepComparisonSection({
      'sweep-duplicate-label': [
        { id: 'k-1', title: 'arm A', arm_label: 'same', criterion_met: true },
        { id: 'k-2', title: 'arm B', arm_label: 'same', criterion_met: false },
      ],
    })).toBe('');
  });

  it('renders single-document arms-block sweeps (duplicate doc id, distinct arm_label)', () => {
    // build_compute_sweep_groups emits one row per per_arm_verdict entry, all sharing
    // the same knowledge document id from base_entry. Arms are identified by arm_label.
    const result = buildSweepComparisonSection({
      'sweep-single-doc': [
        {
          id: 'k-shared',
          title: 'Sweep result doc',
          arm_label: 'approach-a',
          criterion_met: true,
          criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
          per_arm_verdicts: {
            'approach-a': { value: 0.95, met: true },
            'approach-b': { value: 0.87, met: false },
          },
        },
        {
          id: 'k-shared',
          title: 'Sweep result doc',
          arm_label: 'approach-b',
          criterion_met: false,
        },
      ],
    });
    expect(result).toContain('Sweep sweep-single-doc');
    expect(result).toContain('Winner: **approach-a** (accuracy=0.95; margin=0.08 over approach-b=0.87)');
    expect(result).toContain('margin: +0.08 accuracy over approach-b');
    expect(result).toContain('**approach-a** [MET] [k-shared] (accuracy=0.95)');
    expect(result).toContain('**approach-b** [NOT MET] [k-shared] (accuracy=0.87)');
  });

  it('returns empty string when criterion_met is not boolean', () => {
    const result = buildSweepComparisonSection({
      'sweep-string-verdict': [
        { id: 'k-1', title: 'arm A', arm_label: 'a', criterion_met: true },
        { id: 'k-2', title: 'arm B', arm_label: 'b', criterion_met: 'false' },
      ],
    });
    expect(result).toBe('');
  });

  it('renders metric values and best-arm margin for completed sweeps', () => {
    const result = buildSweepComparisonSection({
      'sweep-abc': [
        {
          id: 'k-met',
          title: 'Approach A: fast tokenizer',
          arm_label: 'fast-tokenizer',
          criterion_met: true,
          criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
          per_arm_verdicts: {
            'fast-tokenizer': { value: 0.93, met: true },
            'slow-tokenizer': { value: 0.88, met: false },
          },
          summary: 'Achieved 98% accuracy at 2x throughput',
        },
        {
          id: 'k-not',
          title: 'Approach B: slow tokenizer',
          arm_label: 'slow-tokenizer',
          criterion_met: false,
          summary: 'Only reached 85% accuracy',
        },
      ],
    });
    expect(result).toContain('## Completed experiment sweeps — synthesis required');
    expect(result).toContain('Sweep sweep-abc');
    expect(result).toContain('Winner: **fast-tokenizer** (accuracy=0.93; margin=0.05 over slow-tokenizer=0.88)');
    expect(result).toContain('margin: +0.05 accuracy over slow-tokenizer');
    expect(result).toContain('**fast-tokenizer** [MET] [k-met] (accuracy=0.93)');
    expect(result).toContain('**slow-tokenizer** [NOT MET] [k-not] (accuracy=0.88)');
    expect(result).toContain('exactly ONE follow-on move');
    expect(result).toContain('./fetch knowledge');
  });

  it('names the winning arm by measured value and renders the numeric margin', () => {
    const result = buildSweepComparisonSection({
      'sweep-abc': [
        {
          id: 'k-alpha',
          title: 'Approach A: alpha',
          arm_label: 'alpha',
          criterion_met: false,
          criterion: { metric: 'loss', operator: '<=', threshold: 0.85 },
          per_arm_verdicts: {
            alpha: { value: 0.94, met: false },
            beta: { value: 0.81, met: true },
          },
          summary: 'Higher loss and slower convergence',
        },
        {
          id: 'k-beta',
          title: 'Approach B: beta',
          arm_label: 'beta',
          criterion_met: true,
          criterion: { metric: 'loss', operator: '<=', threshold: 0.85 },
          per_arm_verdicts: {
            alpha: { value: 0.94, met: false },
            beta: { value: 0.81, met: true },
          },
          summary: 'Lower loss and better convergence',
        },
      ],
    });
    expect(result).toContain('Winner: **beta** (loss=0.81; margin=0.13 over alpha=0.94)');
    expect(result).toContain('margin: +0.13 loss over alpha');
    expect(result).toContain('**alpha** [NOT MET]');
    expect(result).toContain('**beta** [MET]');
  });

  it('shows "none (all NOT MET)" as winner when no arm met the criterion', () => {
    const result = buildSweepComparisonSection({
      'sweep-all-fail': [
        {
          id: 'k-a',
          title: 'arm A',
          arm_label: 'a',
          criterion_met: false,
          criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
          per_arm_verdicts: {
            a: { value: 0.82, met: false },
            b: { value: 0.79, met: false },
          },
        },
        {
          id: 'k-b',
          title: 'arm B',
          arm_label: 'b',
          criterion_met: false,
        },
      ],
    });
    expect(result).toContain('Winner: **none (all NOT MET)**');
    expect(result).toContain('Best measured arm: **a** (accuracy=0.82; margin=0.03 over b=0.79)');
    expect(result).toContain('Winning: 0/2 arm(s) MET, 2/2 NOT MET');
  });

  it('falls back to the current label-only winner rendering when measured values are unavailable', () => {
    const result = buildSweepComparisonSection({
      'sweep-fallback': [
        { id: 'k-fast', title: 'arm A', arm_label: 'fast-tokenizer', criterion_met: true, summary: 'Fast path' },
        { id: 'k-slow', title: 'arm B', arm_label: 'slow-tokenizer', criterion_met: false, summary: 'Slow path' },
      ],
    });
    expect(result).toContain('Winner: **fast-tokenizer**');
    expect(result).not.toContain('(accuracy=');
    expect(result).not.toContain('margin:');
  });

  it('does not dereference a missing measured arm when all label-only arms are NOT MET', () => {
    const result = buildSweepComparisonSection({
      'sweep-label-only-all-fail': [
        { id: 'k-a', title: 'arm A', arm_label: 'a', criterion_met: false, summary: 'No pass' },
        { id: 'k-b', title: 'arm B', arm_label: 'b', criterion_met: false, summary: 'No pass' },
      ],
    });
    expect(result).toContain('Winner: **none (all NOT MET)**');
    expect(result).toContain('Best measured arm: none (no numeric metric values available)');
    expect(result).toContain('**a** [NOT MET] [k-a]');
    expect(result).toContain('**b** [NOT MET] [k-b]');
  });

  it('omits the margin when there is only one numeric arm', () => {
    const result = buildSweepComparisonSection({
      'sweep-one-numeric': [
        {
          id: 'k-solo',
          title: 'Solo arm',
          arm_label: 'solo',
          criterion_met: true,
          criterion: { metric: 'accuracy', operator: '>=', threshold: 0.9 },
          per_arm_verdicts: { solo: { value: 0.97, met: true } },
          summary: 'Single-arm completion',
        },
        {
          id: 'k-unmeasured',
          title: 'Unmeasured arm',
          arm_label: 'unmeasured',
          criterion_met: false,
          summary: 'No metric value',
        },
      ],
    });
    expect(result).toContain('Winner: **solo** (accuracy=0.97)');
    expect(result).not.toContain('margin:');
  });

  it('renders multiple completed sweeps independently', () => {
    const result = buildSweepComparisonSection({
      'sweep-x': [
        { id: 'k-x1', title: 'arm X1', arm_label: 'x1', criterion_met: true },
        { id: 'k-x2', title: 'arm X2', arm_label: 'x2', criterion_met: false },
      ],
      'sweep-y': [
        { id: 'k-y1', title: 'arm Y1', arm_label: 'y1', criterion_met: false },
        { id: 'k-y2', title: 'arm Y2', arm_label: 'y2', criterion_met: true },
      ],
    });
    expect(result).toContain('Sweep sweep-x');
    expect(result).toContain('Sweep sweep-y');
    expect(result).toContain('Winner: **x1**');
    expect(result).toContain('Winner: **y2**');
  });

  it('skips sweeps with fewer than two arms', () => {
    const result = buildSweepComparisonSection({
      'sweep-empty': [],
      'sweep-real': [
        { id: 'k-r1', title: 'arm R1', arm_label: 'r1', criterion_met: true },
        { id: 'k-r2', title: 'arm R2', arm_label: 'r2', criterion_met: false },
      ],
    });
    // Only the real comparative sweep should appear.
    expect(result).not.toContain('Sweep sweep-empty');
    expect(result).toContain('Sweep sweep-real');
  });

  it('renders disposed-sweep block and omits synthesis mandate for sweeps with sweep_disposition', () => {
    const result = buildSweepComparisonSection({
      'sweep-disposed': [
        {
          id: 'k-d1',
          title: 'arm A',
          arm_label: 'arm-a',
          criterion_met: true,
          sweep_disposition: { task_id: 'task-adopt-789', status: 'in_progress' },
        },
        {
          id: 'k-d2',
          title: 'arm B',
          arm_label: 'arm-b',
          criterion_met: false,
        },
      ],
    });
    // Disposition block is rendered with heading, sweep id, task id, and status.
    expect(result).toContain('## Sweep dispositions in flight — do not re-synthesize');
    expect(result).toContain('**sweep-disposed**');
    expect(result).toContain('task-adopt-789');
    expect(result).toContain('in_progress');
    // Normal synthesis mandate must NOT appear — disposed sweeps must not be re-synthesized.
    expect(result).not.toContain('## Completed experiment sweeps — synthesis required');
    expect(result).not.toContain('exactly ONE follow-on move');
  });

  it('renders disposed sweeps and completed sweeps independently in the same output', () => {
    const result = buildSweepComparisonSection({
      'sweep-disposed': [
        {
          id: 'k-d1',
          title: 'arm A',
          arm_label: 'arm-a',
          criterion_met: true,
          sweep_disposition: { task_id: 'task-xyz', status: 'pending' },
        },
        {
          id: 'k-d2',
          title: 'arm B',
          arm_label: 'arm-b',
          criterion_met: false,
        },
      ],
      'sweep-completed': [
        { id: 'k-c1', title: 'arm C1', arm_label: 'c1', criterion_met: true },
        { id: 'k-c2', title: 'arm C2', arm_label: 'c2', criterion_met: false },
      ],
    });
    // Disposition block rendered for disposed sweep.
    expect(result).toContain('## Sweep dispositions in flight — do not re-synthesize');
    expect(result).toContain('**sweep-disposed**: disposition task `task-xyz` (pending)');
    // Completed sweep rendered with synthesis mandate.
    expect(result).toContain('## Completed experiment sweeps — synthesis required');
    expect(result).toContain('Sweep sweep-completed');
    // Disposed sweep id must NOT appear under the completed sweeps section.
    expect(result).not.toContain('Sweep sweep-disposed');
  });
});
