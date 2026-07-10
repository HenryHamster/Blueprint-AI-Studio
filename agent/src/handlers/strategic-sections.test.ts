import { describe, it, expect } from 'vitest';
import { existsSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  fmtScalar,
  buildSystemMaturitySection,
  buildCapabilityVerificationSection,
  buildCycleStartReconciliationSection,
  buildPruneCandidatesSection,
  buildDriftSection,
  buildHeldDirectionsSection,
  buildOutcomeSection,
  buildClockAndRecentWorkSection,
  buildConversationThreadSection,
  buildFeedbackSection,
  buildQuestionSection,
  buildOpenMessagesText,
  buildDirSummaryText,
  buildDirHealthSummary,
  buildDirectionPortfolioSection,
  buildCiComplianceSection,
  buildSweepComparisonSection,
  buildPayloadStalenessSection,
  buildCompletionDebtSection,
  buildPendingFollowupsSection,
} from './strategic-sections.js';
import { buildThinkerPrompt } from '../prompts/thinker.js';

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

describe('buildDriftSection consecutive_flagged_cycles', () => {
  it('appends flagged_cycles count to direction entry when >1', () => {
    const result = buildDriftSection({
      window_days: 7,
      by_direction: {
        'd-stuck': {
          over_claim_flag: true,
          stalled_despite_churn_flag: false,
          claimed_changes_count: 8,
          actually_merged_prs: 1,
          claim_shipped_consistency: 0.12,
          consecutive_flagged_cycles: 4,
        },
      },
    });
    expect(result).toContain('Drift Signals');
    expect(result).toContain('Direction d-stuck');
    expect(result).toContain('over_claim');
    expect(result).toContain('flagged_cycles=4');
  });

  it('omits flagged_cycles tail when consecutive_flagged_cycles is 1', () => {
    const result = buildDriftSection({
      window_days: 7,
      by_direction: {
        'd-once': {
          over_claim_flag: true,
          stalled_despite_churn_flag: false,
          claimed_changes_count: 5,
          actually_merged_prs: 0,
          claim_shipped_consistency: 0.0,
          consecutive_flagged_cycles: 1,
        },
      },
    });
    expect(result).toContain('Direction d-once');
    expect(result).not.toContain('flagged_cycles=');
  });

  it('omits flagged_cycles tail when consecutive_flagged_cycles is absent', () => {
    const result = buildDriftSection({
      window_days: 7,
      by_direction: {
        'd-no-trend': {
          over_claim_flag: true,
          stalled_despite_churn_flag: false,
          claimed_changes_count: 3,
          actually_merged_prs: 0,
          claim_shipped_consistency: 0.0,
        },
      },
    });
    expect(result).toContain('Direction d-no-trend');
    expect(result).not.toContain('flagged_cycles=');
  });
});

describe('buildHeldDirectionsSection', () => {
  it('returns empty string for undefined or empty input', () => {
    expect(buildHeldDirectionsSection(undefined)).toBe('');
    expect(buildHeldDirectionsSection([])).toBe('');
  });

  it('renders direction text, id, consecutive failures, last class, and fingerprint', () => {
    const result = buildHeldDirectionsSection([{
      direction_id: 'd-held',
      direction_text: 'improve test coverage',
      consecutive_failures: 5,
      last_failure_class: 'agent_stalled',
      dominant_error_fingerprint: 'Force-terminated by timeout',
      recent_failure_messages: ['agent timed out after 600s', 'agent timed out after 600s'],
    }]);
    expect(result).toContain('## Held directions — stuck on consecutive failures');
    expect(result).toContain('improve test coverage (id: d-held)');
    expect(result).toContain('Consecutive failures: 5');
    expect(result).toContain('Last failure class: agent_stalled');
    expect(result).toContain('Dominant fingerprint: Force-terminated by timeout');
    expect(result).toContain('agent timed out after 600s');
  });

  it('renders "(none)" fingerprint when dominant_error_fingerprint is absent', () => {
    const result = buildHeldDirectionsSection([{
      direction_id: 'd-nofp',
      direction_text: 'auth work',
      consecutive_failures: 3,
      last_failure_class: 'ci_not_green',
    }]);
    expect(result).toContain('Dominant fingerprint: (none)');
  });

  it('omits Recent failure messages block when array is empty', () => {
    const result = buildHeldDirectionsSection([{
      direction_id: 'd-empty',
      direction_text: 'some dir',
      consecutive_failures: 2,
      last_failure_class: 'test-coverage',
      dominant_error_fingerprint: 'missing tests',
      recent_failure_messages: [],
    }]);
    expect(result).not.toContain('Recent failure messages:');
  });

  it('renders multiple held directions', () => {
    const result = buildHeldDirectionsSection([
      {
        direction_id: 'd-a',
        direction_text: 'dir alpha',
        consecutive_failures: 3,
        last_failure_class: 'error',
        dominant_error_fingerprint: 'fp-a',
        recent_failure_messages: ['msg a'],
      },
      {
        direction_id: 'd-b',
        direction_text: 'dir beta',
        consecutive_failures: 7,
        last_failure_class: 'stalled',
        dominant_error_fingerprint: 'fp-b',
        recent_failure_messages: ['msg b'],
      },
    ]);
    expect(result).toContain('dir alpha (id: d-a)');
    expect(result).toContain('dir beta (id: d-b)');
    expect(result).toContain('fp-a');
    expect(result).toContain('fp-b');
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

  it('renders qualification_prose_drift entries with header and per-entry bullets', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      qualification_prose_drift: [
        { stale_sha: 'aabbccdd', active_candidate_sha: '11223344', cited_in: 'intent.current_focus' },
        { stale_sha: 'deadbeef', active_candidate_sha: '11223344', cited_in: 'running_update' },
      ],
      counts: { qualification_prose_drift: 2 },
    });

    expect(result).toContain('Qualification prose-drift — stale candidate SHA in prose (2)');
    expect(result).toContain('Active candidate: `11223344`');
    expect(result).toContain('Rewrite the stale narrative to reflect the current candidate');
    expect(result).toContain('`aabbccdd` cited in intent.current_focus (active: `11223344`)');
    expect(result).toContain('`deadbeef` cited in running_update (active: `11223344`)');
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

  it('renders broken criterion links (inactive + dangling) with recovery hints', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      stale_active_moves: [],
      criterion_link_conflicts: [
        { direction_id: 'd-1', direction_text: 'ship valve', criterion_key: 'c-dead', classification: 'inactive', recovery_hint: 'Remap to an active criterion.' },
        { direction_id: 'd-2', direction_text: 'wrong version', criterion_key: 'c-ghost', classification: 'dangling', recovery_hint: 'Remap to a criterion in the open version.' },
      ],
      direction_pipeline: [],
      counts: { criterion_link_conflicts: 2 },
    });

    expect(result).toContain('Broken criterion links — direction maps to a non-active criterion (2)');
    expect(result).toContain('ship valve (id: d-1) → criterion c-dead is retracted or superseded — Remap to an active criterion.');
    expect(result).toContain('wrong version (id: d-2) → criterion c-ghost is not in the open version — Remap to a criterion in the open version.');
  });

  it('omits the criterion-link section when there are no criterion conflicts', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      stale_active_moves: [],
      direction_pipeline: [],
      counts: {},
    });
    expect(result).not.toContain('Broken criterion links');
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

  it('renders pending-in-open-rollup titles outside the conflict list', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      merge_accounting: [],
      pending_in_open_rollup: [{
        direction_id: 'd-rollup',
        direction_text: 'pending rollup dir',
        pending_in_open_rollup_titles: ['Feature A (merged to staging)', 'Feature B (merged to staging)'],
        pending_in_open_rollup_titles_overflow: 0,
        pending_in_open_rollup: 2,
      }],
      counts: {
        merge_accounting: 0,
        merge_landed: 1,
        merge_not_landed: 0,
        merge_unverifiable: 0,
        merge_pending_in_open_rollup: 2,
        merge_pending_in_open_rollup_accounting: 1,
      },
    });
    expect(result).toContain('## Pending rollup merges — work awaiting squash-merge to integration branch');
    expect(result).toContain('pending rollup dir (id: d-rollup) — 2 pending rollup merge');
    expect(result).toContain('Feature A (merged to staging)');
    expect(result).toContain('Feature B (merged to staging)');
    expect(result).not.toContain('Merge-accounting drift');
    expect(result).not.toContain('NOT landed');
  });

  it('renders qualification_surface_conflicts entries with conflict_type and description', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      qualification_surface_conflicts: [
        { conflict_type: 'status_state_mismatch', description: "last_qualification.status='pending' but qualification_state.state='not_started' (states do not align for this status)." },
        { conflict_type: 'criterion_coverage_vs_confidence', description: 'Confidence 85.00% meets threshold 80.00%, but only 2/5 criteria are covered.' },
      ],
      counts: { qualification_surface_conflicts: 2 },
    });
    expect(result).toContain('Qualification surface conflicts — coherence disagreements (2)');
    expect(result).toContain("[status_state_mismatch] last_qualification.status='pending' but qualification_state.state='not_started' (states do not align for this status).");
    expect(result).toContain('[criterion_coverage_vs_confidence] Confidence 85.00% meets threshold 80.00%, but only 2/5 criteria are covered.');
  });

  it('omits the qualification_surface_conflicts section when the array is empty or absent', () => {
    const withEmpty = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      qualification_surface_conflicts: [],
      counts: { qualification_surface_conflicts: 0 },
    });
    expect(withEmpty).not.toContain('Qualification surface conflicts');

    const withAbsent = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      counts: {},
    });
    expect(withAbsent).not.toContain('Qualification surface conflicts');
  });

  it('shows +N more tail when counts.qualification_surface_conflicts exceeds shown entries', () => {
    const result = buildCycleStartReconciliationSection({
      capability_contradictions: [],
      orphaned_gaps: [],
      ungrounded_directions: [],
      direction_pipeline: [],
      qualification_surface_conflicts: [
        { conflict_type: 'stale_candidate_with_promotion_failed_status', description: 'Candidate currency says the pinned SHA is stale/behind HEAD, but last_qualification.status is promotion_failed.' },
      ],
      counts: { qualification_surface_conflicts: 4 },
    });
    expect(result).toContain('Qualification surface conflicts — coherence disagreements (4)');
    expect(result).toContain('[stale_candidate_with_promotion_failed_status]');
    expect(result).toContain('- +3 more not shown');
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

  it('renders outcome_scaffolding_flagged warning with evidence', () => {
    const result = buildOutcomeSection({
      directions: [{
        id: 'd-scaffold',
        text: 'improve throughput',
        status: 'active',
        cycles: 15,
        merged: 10,
        failed: 2,
        outcome_scaffolding_flagged: true,
        outcome_scaffolding_evidence: {
          total_cycles: 15,
          lifetime_merged: 10,
          lifetime_merge_rate: 0.667,
          progress_stagnant_cycles: 5,
        },
      }],
    });
    expect(result).toContain('OUTCOME-SCAFFOLDING-FLAGGED');
    expect(result).toContain('Scaffolding evidence: 15 cycles, 10 lifetime merged (rate=0.667), stagnant for 5 cycles');
  });

  it('omits outcome_scaffolding_flagged warning when flag is false or absent', () => {
    const result = buildOutcomeSection({
      directions: [{
        id: 'd-normal',
        text: 'normal direction',
        status: 'active',
        cycles: 5,
        merged: 3,
        failed: 1,
        outcome_scaffolding_flagged: false,
      }],
    });
    expect(result).not.toContain('OUTCOME-SCAFFOLDING-FLAGGED');
    expect(result).not.toContain('Scaffolding evidence');
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

  it('renders plain merged when verified_prs_merged equals prs_merged (no badge-only noise)', () => {
    const result = buildDirSummaryText([{
      id: 'd-allverified',
      text: 'all verified direction',
      status: 'active',
      priority: 'medium',
      total_cycles: 2,
      prs_merged: 4,
      verified_prs_merged: 4,
    }]);
    expect(result).toContain('4 merged');
    expect(result).not.toContain('badge-only');
    expect(result).not.toContain('verified,');
  });

  it('falls back to plain merged when verified_prs_merged is absent (legacy payload)', () => {
    const result = buildDirSummaryText([{
      id: 'd-legacy',
      text: 'legacy no verified field',
      status: 'active',
      priority: 'medium',
      total_cycles: 1,
      prs_merged: 7,
    }]);
    expect(result).toContain('7 merged');
    expect(result).not.toContain('badge-only');
    expect(result).not.toContain('verified,');
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

  it('renders verified annotation when verified < merged', () => {
    const result = buildDirSummaryText([{
      id: 'd-verified',
      text: 'direction with verified split',
      status: 'active',
      priority: 'high',
      total_cycles: 3,
      prs_merged: 5,
      verified_prs_merged: 3,
    }]);
    expect(result).toContain('5 merged (3 verified)');
  });

  it('omits verified annotation when verified equals merged', () => {
    const result = buildDirSummaryText([{
      id: 'd-all-verified',
      text: 'complete credit direction',
      status: 'active',
      priority: 'medium',
      total_cycles: 2,
      prs_merged: 4,
      verified_prs_merged: 4,
    }]);
    expect(result).toContain('4 merged');
    expect(result).not.toContain('4 merged (');
  });

  it('omits verified annotation when field is absent', () => {
    const result = buildDirSummaryText([{
      id: 'd-legacy-no-verified',
      text: 'legacy direction',
      status: 'active',
      priority: 'medium',
      total_cycles: 1,
      prs_merged: 3,
    }]);
    expect(result).toContain('3 merged');
    expect(result).not.toContain('3 merged (');
  });

  it('renders reachable-vs-badge split when reachable_prs_merged is present', () => {
    const result = buildDirSummaryText([{
      id: 'd-reachable',
      text: 'ancestry tracking direction',
      status: 'active',
      priority: 'high',
      total_cycles: 4,
      prs_merged: 5,
      reachable_prs_merged: 3,
    }]);
    expect(result).toContain('5 merged (3 reachable, 2 badge-only)');
  });

  it('omits reachable split when field is absent (legacy-unknown)', () => {
    const result = buildDirSummaryText([{
      id: 'd-legacy-no-reachable',
      text: 'original direction',
      status: 'active',
      priority: 'medium',
      total_cycles: 2,
      prs_merged: 2,
    }]);
    expect(result).toContain('2 merged');
    expect(result).not.toContain('2 merged (');
  });

  it('renders explicit known-zero reachability distinctly from legacy-unknown', () => {
    // reachable_prs_merged: 0 (present, zero) — must emit the annotation so operators
    // see "merged credit exists but nothing is ancestry-confirmed" rather than the
    // same silent '3 merged' as a legacy-unknown direction.
    const result = buildDirSummaryText([{
      id: 'd-known-zero-reachable',
      text: 'direction with zero reachable',
      status: 'active',
      priority: 'high',
      total_cycles: 3,
      prs_merged: 3,
      reachable_prs_merged: 0,
    }]);
    expect(result).toContain('3 merged (0 reachable, 3 badge-only)');
  });

  it('combines verified and reachable annotations with semicolon separator', () => {
    const result = buildDirSummaryText([{
      id: 'd-both',
      text: 'direction with both verified and reachable',
      status: 'active',
      priority: 'high',
      total_cycles: 5,
      prs_merged: 6,
      verified_prs_merged: 4,
      reachable_prs_merged: 5,
    }]);
    expect(result).toContain('6 merged (4 verified; 5 reachable, 1 badge-only)');
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

  it('renders SCAFFOLDING marker when outcome_scaffolding_flagged is true', () => {
    const result = buildDirSummaryText([{
      id: 'd-scaffolding',
      text: 'direction with scaffolding flag',
      status: 'active',
      priority: 'high',
      total_cycles: 5,
      outcome_scaffolding_flagged: true,
    }]);
    expect(result).toContain('[SCAFFOLDING]');
  });

  it('omits SCAFFOLDING marker when outcome_scaffolding_flagged is false or absent', () => {
    const result = buildDirSummaryText([{
      id: 'd-no-scaffolding',
      text: 'direction without scaffolding flag',
      status: 'active',
      priority: 'medium',
      total_cycles: 3,
      outcome_scaffolding_flagged: false,
    }]);
    expect(result).not.toContain('[SCAFFOLDING]');
  });

  it('renders CHURN-RETIRE-RECOMMENDED marker and evidence when recommended', () => {
    const result = buildDirSummaryText([{
      id: 'd-churn-rec',
      text: 'churn direction recommended for retirement',
      status: 'paused',
      priority: 'medium',
      total_cycles: 6,
      auto_paused: true,
      paused_reason: 'Unproductive: 0 PRs merged',
      churn_retirement_recommended: true,
      churn_retirement_evidence: {
        churn_family_pause_count: 2,
        threshold: 2,
        merge_rate: 0,
        summary: 'Paused 2x for churn with no escape; retirement recommended.',
      },
    }]);
    expect(result).toContain('[CHURN-RETIRE-RECOMMENDED]');
    expect(result).toContain('CHURN-RETIREMENT-RECOMMENDED');
    expect(result).toContain('will NOT auto-retire');
    expect(result).toContain('2x churn');
  });

  it('omits CHURN-RETIRE marker when churn_retirement_recommended is false or absent', () => {
    const result = buildDirSummaryText([{
      id: 'd-no-churn-rec',
      text: 'ordinary paused direction',
      status: 'paused',
      priority: 'medium',
      total_cycles: 3,
      auto_paused: true,
      paused_reason: 'Unproductive: 0 PRs merged',
      churn_retirement_recommended: false,
    }]);
    expect(result).not.toContain('CHURN-RETIRE');
  });

  it('renders retired_direction_grounding when present on an active direction', () => {
    const result = buildDirSummaryText([{
      id: 'd-grounding',
      text: 'Re-staffed direction',
      status: 'active',
      priority: 'medium',
      total_cycles: 1,
      retired_direction_grounding: {
        direction_id: 'd-prior-001',
        classification: 'abandoned',
        metrics: { prs_merged: 0, cycles: 5 },
        reason: 'stalled on flaky infra',
      },
    }]);
    expect(result).toContain('d-prior-001');
    expect(result).toContain('[abandoned]');
    expect(result).toContain('stalled on flaky infra');
  });

  it('omits grounding section when retired_direction_grounding is absent', () => {
    const result = buildDirSummaryText([{
      id: 'd-no-grounding',
      text: 'Novel direction',
      status: 'active',
      priority: 'high',
      total_cycles: 2,
    }]);
    expect(result).not.toContain('Re-staffing of retired work');
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

describe('buildPayloadStalenessSection', () => {
  const STALE_SHA = 'a'.repeat(40);
  const CANDIDATE_SHA = 'b'.repeat(40);

  it('returns empty string for undefined', () => {
    expect(buildPayloadStalenessSection(undefined)).toBe('');
  });

  it('returns empty string for object missing stale boolean and no verdict', () => {
    expect(buildPayloadStalenessSection({ payload_compiled_sha: STALE_SHA })).toBe('');
    expect(buildPayloadStalenessSection({})).toBe('');
  });

  it('unknown verdict — renders explicit UNKNOWN warning, not empty string', () => {
    const result = buildPayloadStalenessSection({ verdict: 'unknown', checked_at: '2026-06-30T12:00:00' });
    expect(result).toContain('## Payload Currency (cycle-start check)');
    expect(result).toContain('⚠ UNKNOWN');
    expect(result).toContain('freshness check could not be completed');
    expect(result).toContain('2026-06-30T12:00:00');
    expect(result).toContain('Treat this payload as potentially stale');
    expect(result).not.toContain('⚠ STALE');
    expect(result).not.toContain('CURRENT');
  });

  it('unknown verdict without checked_at — still renders UNKNOWN section', () => {
    const result = buildPayloadStalenessSection({ verdict: 'unknown' });
    expect(result).toContain('⚠ UNKNOWN');
    expect(result).toContain('freshness check could not be completed');
  });

  it('api_unreachable verdict — renders TEMPORARY reachability warning distinct from UNKNOWN/STALE', () => {
    const result = buildPayloadStalenessSection({
      verdict: 'api_unreachable',
      reason: 'github_http_502',
      checked_at: '2026-06-30T12:00:00',
    });
    expect(result).toContain('## Payload Currency (cycle-start check)');
    expect(result).toContain('⚠ API UNREACHABLE');
    expect(result).toContain('could not reach GitHub');
    expect(result).toContain('github_http_502');
    expect(result).toContain('2026-06-30T12:00:00');
    // Key regression: must NOT push the strategist to re-ground everything as stale.
    expect(result).toContain('TEMPORARY API-reachability problem');
    expect(result).toContain('Do NOT re-ground');
    // Distinct from the other verdicts — no STALE/UNKNOWN/CURRENT framing.
    expect(result).not.toContain('⚠ STALE');
    expect(result).not.toContain('⚠ UNKNOWN');
    expect(result).not.toContain('Candidate is CURRENT');
  });

  it('api_unreachable verdict without reason/checked_at — still renders API UNREACHABLE section', () => {
    const result = buildPayloadStalenessSection({ verdict: 'api_unreachable' });
    expect(result).toContain('⚠ API UNREACHABLE');
    expect(result).toContain('TEMPORARY API-reachability problem');
  });

  it('divergence rendered — stale=true shows ⚠ STALE block with truncated SHAs (legacy payload_compiled_sha fallback)', () => {
    const result = buildPayloadStalenessSection({
      stale: true,
      payload_compiled_sha: STALE_SHA,
      active_candidate_sha: CANDIDATE_SHA,
    });
    expect(result).toContain('⚠ STALE');
    expect(result).toContain(STALE_SHA.slice(0, 12));
    expect(result).toContain(CANDIDATE_SHA.slice(0, 12));
    expect(result).toContain('## Payload Currency (cycle-start check)');
    expect(result).toContain('Do NOT treat unverified audit findings');
    expect(result).toContain('payload-vs-candidate');
  });

  it('buildPayloadStalenessSection renders structured stale reasons for SHA divergence', () => {
    const CANDIDATE_SHA = 'c'.repeat(40);
    const INTEGRATION_SHA = 'd'.repeat(40);
    // payload_sha as primary; differs from both candidate and integration head
    const result = buildPayloadStalenessSection({
      stale: true,
      payload_sha: STALE_SHA,
      payload_compiled_sha: 'e'.repeat(40),  // should be ignored when payload_sha present
      active_candidate_sha: CANDIDATE_SHA,
      integration_head_sha: INTEGRATION_SHA,
    });
    expect(result).toContain('⚠ STALE');
    expect(result).toContain('## Payload Currency (cycle-start check)');
    // Uses payload_sha (STALE_SHA), not payload_compiled_sha
    expect(result).toContain(STALE_SHA.slice(0, 12));
    expect(result).not.toContain('e'.repeat(12));
    // payload-vs-candidate bullet present
    expect(result).toContain('payload-vs-candidate');
    expect(result).toContain(CANDIDATE_SHA.slice(0, 12));
    // payload-vs-integration-head bullet present
    expect(result).toContain('payload-vs-integration-head');
    expect(result).toContain(INTEGRATION_SHA.slice(0, 12));
    expect(result).toContain('Do NOT treat unverified audit findings');
  });

  it('only candidate annotation when integration_head_sha absent', () => {
    const result = buildPayloadStalenessSection({
      stale: true,
      payload_sha: STALE_SHA,
      active_candidate_sha: CANDIDATE_SHA,
    });
    expect(result).toContain('⚠ STALE');
    expect(result).toContain('payload-vs-candidate');
    expect(result).toContain(CANDIDATE_SHA.slice(0, 12));
    expect(result).not.toContain('payload-vs-integration-head');
  });

  it('no stale bullets when payload SHA is missing (indeterminate guard)', () => {
    const result = buildPayloadStalenessSection({
      stale: true,
      active_candidate_sha: CANDIDATE_SHA,
      integration_head_sha: 'c'.repeat(40),
    });
    expect(result).toContain('⚠ STALE');
    // No specific SHA comparison bullets since payload SHA is unknown
    expect(result).not.toContain('payload-vs-candidate');
    expect(result).not.toContain('payload-vs-integration-head');
  });

  it('no false positive — stale=false shows CURRENT line, not ⚠ STALE', () => {
    const result = buildPayloadStalenessSection({
      stale: false,
      payload_sha: STALE_SHA,
      payload_compiled_sha: STALE_SHA,
      active_candidate_sha: STALE_SHA,
    });
    expect(result).not.toContain('⚠ STALE');
    expect(result).toContain('CURRENT');
    expect(result).toContain('## Payload Currency (cycle-start check)');
  });

  it('stale branch with generated_at — appends _Generated at <timestamp>_ line', () => {
    const result = buildPayloadStalenessSection({
      stale: true,
      payload_sha: STALE_SHA,
      active_candidate_sha: CANDIDATE_SHA,
      generated_at: '2026-06-30T12:00:00Z',
    });
    expect(result).toContain('⚠ STALE');
    expect(result).toContain('_Generated at 2026-06-30T12:00:00Z_');
    expect(result).not.toContain('_Checked at');
  });

  it('current branch with generated_at — appends _Checked at <timestamp>_ line', () => {
    const result = buildPayloadStalenessSection({
      stale: false,
      payload_sha: STALE_SHA,
      active_candidate_sha: STALE_SHA,
      generated_at: '2026-06-30T12:00:00Z',
    });
    expect(result).toContain('CURRENT');
    expect(result).toContain('_Checked at 2026-06-30T12:00:00Z_');
    expect(result).not.toContain('_Generated at');
  });

  it('omits generated_at lines when generated_at field is absent', () => {
    const staleResult = buildPayloadStalenessSection({
      stale: true,
      payload_sha: STALE_SHA,
      active_candidate_sha: CANDIDATE_SHA,
    });
    expect(staleResult).not.toContain('_Generated at');
    const currentResult = buildPayloadStalenessSection({
      stale: false,
      payload_sha: STALE_SHA,
      active_candidate_sha: STALE_SHA,
    });
    expect(currentResult).not.toContain('_Checked at');
  });

  it('position in prompt — staleness section appears before problem model', () => {
    const stalenessMarker = 'STALENESS_MARKER_XYZ';
    const problemModelMarker = 'PROBLEM_MODEL_MARKER_XYZ';
    const prompt = buildThinkerPrompt({
      contextPrefix: '',
      projectName: 'test',
      clockHeader: '',
      isSelfRepo: false,
      userStateLine: '',
      packerMetaSection: '',
      payloadStalenessSection: stalenessMarker,
      strategicContext: '',
      problemModelSection: problemModelMarker,
      problemModelRecovery: 'present',
      cycleStartReconciliationSection: '',
      reflectionSection: '',
      selfImprovementBlock: '',
      runningUpdateSection: '',
      designDocJson: '',
      visionJson: '',
      outcomeSection: '',
      previousDecisionOutcomes: '',
      pastRetirementCritiquesSection: '',
      retiredDirectionsSummarySection: '',
      directionPortfolioSection: '',
      userPausedSection: '',
      rejectedProposalsSection: '',
      operatorOverridesSection: '',
      modelHealthSection: '',
      systemMaturitySection: '',
      driftSection: '',
      recentWorkSection: '',
      feedbackSection: '',
      questionSection: '',
      threadSection: '',
      taskId: 'test-task',
      versionPhaseSection: '',
      memoCitationsSection: '',
      efficiencySnapshotSection: '',
    });
    const stalenessPos = prompt.indexOf(stalenessMarker);
    const problemModelPos = prompt.indexOf(problemModelMarker);
    expect(stalenessPos).toBeGreaterThan(-1);
    expect(problemModelPos).toBeGreaterThan(-1);
    expect(stalenessPos).toBeLessThan(problemModelPos);
  });
});

describe('buildCompletionDebtSection', () => {
  it('returns empty string for null, undefined, empty array, and empty dict', () => {
    expect(buildCompletionDebtSection(null)).toBe('');
    expect(buildCompletionDebtSection(undefined)).toBe('');
    expect(buildCompletionDebtSection([])).toBe('');
    expect(buildCompletionDebtSection({ items: [], total_open: 0 })).toBe('');
  });

  it('renders total_open from dict shape, not list length', () => {
    const result = buildCompletionDebtSection({
      items: [
        { kind: 'dormant_knob', title: 'Knob X default-off', description: 'Knob X default-off', age_days: 3 },
      ],
      total_open: 25,
    });
    expect(result).toContain('25 open obligation(s)');
    expect(result).toContain('+24 more not shown');
    expect(result).toContain('[dormant_knob] Knob X default-off');
    expect(result).toContain('(3d old)');
  });

  it('shows no tail note when items length equals total_open', () => {
    const result = buildCompletionDebtSection({
      items: [
        { kind: 'no_ci_gate', description: 'Gate fell open', age_days: 1 },
        { kind: 'capability_trial', description: 'Trial pending', age_days: 0 },
      ],
      total_open: 2,
    });
    expect(result).toContain('2 open obligation(s)');
    expect(result).not.toContain('more not shown');
  });

  it('backward-compat: accepts a plain array and uses its length as total', () => {
    const result = buildCompletionDebtSection([
      { kind: 'seed_inventory', title: 'Legacy item', description: 'Legacy item', age_days: 10 },
    ]);
    expect(result).toContain('1 open obligation(s)');
    expect(result).not.toContain('more not shown');
    expect(result).toContain('[seed_inventory] Legacy item');
  });

  it('includes advisory footer', () => {
    const result = buildCompletionDebtSection({ items: [{ kind: 'dormant_knob', title: 'x', description: 'x', age_days: 0 }], total_open: 1 });
    expect(result).toContain('Address open completion debt');
  });

  it('renders the stable debt id and the disposition/linkage hint', () => {
    const result = buildCompletionDebtSection({
      items: [
        { id: 'debt-abc123', kind: 'dormant_knob', title: 'Knob X default-off', description: 'Knob X default-off', age_days: 4 },
      ],
      total_open: 1,
    });
    // The stable id must be surfaced so the strategist can address it.
    expect(result).toContain('`debt-abc123`');
    expect(result).toContain('[dormant_knob] `debt-abc123` Knob X default-off');
    // The disposition/linkage instruction must be present.
    expect(result).toContain('dispose-completion-debt');
    expect(result).toContain('"disposition":"resolved"');
    expect(result).toContain('"debt_id":"<id>"');
  });

  it('leads with title and does NOT show bare knob value for dormant_knob row', () => {
    // The backend sets description = title for dormant_knob to avoid "off"/"observe"
    // being the primary label. The renderer must not show the bare knob state.
    const result = buildCompletionDebtSection({
      items: [
        {
          id: 'debt-knob1',
          kind: 'dormant_knob',
          title: 'Feature X shipped default-off',
          description: 'Feature X shipped default-off',
          age_days: 5,
        },
      ],
      total_open: 1,
    });
    expect(result).toContain('[dormant_knob] `debt-knob1` Feature X shipped default-off');
    // The bare knob value "off" must NOT appear as a standalone label.
    expect(result).not.toContain('[dormant_knob] `debt-knob1` off');
  });

  it('appends description only when it differs from title', () => {
    const result = buildCompletionDebtSection({
      items: [
        {
          kind: 'no_ci_gate',
          title: 'No-CI gate fell open',
          description: 'Unverified merge',
          age_days: 2,
        },
      ],
      total_open: 1,
    });
    // Title appears as the primary label.
    expect(result).toContain('No-CI gate fell open');
    // Description (distinct from title) appended.
    expect(result).toContain('Unverified merge');
  });

  it('renders linked task statuses inline', () => {
    const result = buildCompletionDebtSection({
      items: [
        {
          kind: 'no_ci_gate',
          title: 'Gate fell open',
          description: 'Unverified merge',
          age_days: 1,
          linked_tasks: [
            { task_id: 'aaaa1111bbbb2222', status: 'IN_PROGRESS' },
          ],
        },
      ],
      total_open: 1,
    });
    expect(result).toContain('Linked task(s):');
    expect(result).toContain('aaaa1111: IN_PROGRESS');
  });

  it('emits dispose nudge when a linked task is COMPLETED', () => {
    const result = buildCompletionDebtSection({
      items: [
        {
          kind: 'no_ci_gate',
          title: 'Gate fell open',
          description: 'Gate fell open',
          age_days: 3,
          linked_tasks: [
            { task_id: 'cccc3333dddd4444', status: 'COMPLETED' },
          ],
        },
      ],
      total_open: 1,
    });
    expect(result).toContain('COMPLETED');
    expect(result).toContain('dispose-completion-debt');
    // Specifically the inline nudge for the completed task.
    expect(result).toContain('work landed; consider dispose-completion-debt');
  });

  it('omits linked_tasks block when list is empty or absent', () => {
    const result = buildCompletionDebtSection({
      items: [
        {
          kind: 'dormant_knob',
          title: 'Knob Y',
          description: 'Knob Y',
          age_days: 0,
          linked_tasks: [],
        },
      ],
      total_open: 1,
    });
    expect(result).not.toContain('Linked task(s)');
  });
});

// ── Reconciliation sections fixture contract (agent render coverage) ──────────

const RECON_FIXTURE_RELATIVE = 'backend/testing/__fixtures__/reconciliation_sections.json';

function loadReconSectionsFixture(): { schema_version: number; list_keys: string[] } {
  const candidates = [
    import.meta.url.startsWith('file:')
      ? resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', '..', RECON_FIXTURE_RELATIVE)
      : null,
    resolve(process.cwd(), '..', RECON_FIXTURE_RELATIVE),
    resolve(process.cwd(), RECON_FIXTURE_RELATIVE),
  ].filter((p): p is string => Boolean(p));
  const fixturePath = candidates.find(c => existsSync(c));
  if (!fixturePath) {
    throw new Error(`Missing reconciliation_sections fixture. Checked: ${candidates.join(', ')}`);
  }
  return JSON.parse(readFileSync(fixturePath, 'utf-8')) as { schema_version: number; list_keys: string[] };
}

// A recognisable marker embedded in each minimal item so we can assert the
// renderer actually outputs it (rather than silently ignoring the key).
const CANARY = 'recon-contract-canary-marker';

// One minimal item per fixture list key, structured so the renderer's
// conditional guard fires and the CANARY string appears in the output.
const MINIMAL_ITEMS: Partial<Record<string, unknown[]>> = {
  capability_contradictions: [
    { name: CANARY, stored_status: 'working', effective_status: 'absent', missing: [] },
  ],
  orphaned_gaps: [
    { gap_id: 'g-1', name: CANARY, priority: 'high' },
  ],
  staffed_but_deferred: [
    { name: CANARY, priority: 'high', gap_id: 'g-1', status: 'active' },
  ],
  ungrounded_directions: [
    { direction_id: 'd-1', text: CANARY, reason: 'no_gap' },
  ],
  stale_active_moves: [
    { gap_name: CANARY, gap_id: 'g-1', stale_direction_ids: ['d-stale'] },
  ],
  direction_pipeline: [
    { direction_id: 'd-1', text: CANARY, state: 'research_waiting_on_action', findings: 1 },
  ],
  merge_accounting: [
    {
      direction_id: 'd-1',
      direction_text: CANARY,
      not_landed: 1,
      landed: 0,
      unverifiable: 0,
      not_landed_titles: ['pr-ghost'],
    },
  ],
  pending_in_open_rollup: [
    {
      direction_id: 'd-1',
      direction_text: CANARY,
      pending_in_open_rollup: 1,
      pending_in_open_rollup_titles: ['pr-pending'],
    },
  ],
  phantom_direction_refs: [
    { direction_id: CANARY, cited_in: 'intent' },
  ],
  qualification_prose_drift: [
    { stale_sha: 'deadbeef', active_candidate_sha: '11223344', cited_in: CANARY },
  ],
  qualification_surface_conflicts: [
    { conflict_type: 'canary_type', description: CANARY },
  ],
};

describe('reconciliation_sections fixture contract (agent render coverage)', () => {
  it('fixture has schema_version 1 and a non-empty list_keys array', () => {
    const fixture = loadReconSectionsFixture();
    expect(fixture.schema_version).toBe(1);
    expect(Array.isArray(fixture.list_keys)).toBe(true);
    expect(fixture.list_keys.length).toBeGreaterThan(0);
  });

  it('MINIMAL_ITEMS covers every fixture list key', () => {
    const fixture = loadReconSectionsFixture();
    const uncovered = fixture.list_keys.filter(k => !(k in MINIMAL_ITEMS));
    expect(
      uncovered,
      `Test gap: add a MINIMAL_ITEMS entry for each new fixture key so the render check can run: ${uncovered.join(', ')}`,
    ).toEqual([]);
  });

  it('buildCycleStartReconciliationSection renders every fixture list key when populated', () => {
    const fixture = loadReconSectionsFixture();
    const missingRenderers: string[] = [];

    for (const key of fixture.list_keys) {
      const items = MINIMAL_ITEMS[key];
      if (!items) continue; // covered by the previous test
      const recon: Record<string, unknown> = { [key]: items, counts: {} };
      const output = buildCycleStartReconciliationSection(recon);
      if (!output.includes(CANARY)) {
        missingRenderers.push(key);
      }
    }

    expect(
      missingRenderers,
      `These list keys are declared in the reconciliation_sections fixture but ` +
      `buildCycleStartReconciliationSection does not render them. ` +
      `Add a renderer in agent/src/handlers/strategic-sections.ts: ` +
      missingRenderers.join(', '),
    ).toEqual([]);
  });
});

describe('buildDirectionPortfolioSection', () => {
  it('produces a consolidated portfolio block with basic direction list', () => {
    const dirs = [
      { id: 'd-1', text: 'improve auth', status: 'active', priority: 'high', total_cycles: 3, category: 'product', possibly_saturated: false },
      { id: 'd-2', text: 'refactor db', status: 'active', priority: 'medium', total_cycles: 1, category: 'structural', possibly_saturated: false },
      { id: 'd-3', text: 'add tests', status: 'active', priority: 'low', total_cycles: 2, category: 'product', possibly_saturated: false },
    ];
    const result = buildDirectionPortfolioSection(dirs, 10, [], undefined);
    expect(result).toContain('## Direction Portfolio');
    expect(result).toContain('Active: 3/10 slots used');
    expect(result).toContain('Product directions: 2');
    expect(result).toContain('No critical gaps detected');
    expect(result).toContain('improve auth');
    expect(result).toContain('refactor db');
    expect(result).toContain('add tests');
  });

  it('detects all three gap types when product=0, all saturated, below minimum', () => {
    const dirs = [
      { id: 'd-1', text: 'auth dir', status: 'active', category: 'structural', possibly_saturated: true, priority: 'medium', total_cycles: 5 },
      { id: 'd-2', text: 'infra dir', status: 'active', category: 'infra', possibly_saturated: true, priority: 'medium', total_cycles: 2 },
    ];
    const result = buildDirectionPortfolioSection(dirs, 10, [], undefined, 2);
    expect(result).toContain('NO_PRODUCT_DIRECTIONS');
    expect(result).toContain('ALL_SATURATED');
    expect(result).toContain('BELOW_MINIMUM');
    expect(result).toContain('GAPS DETECTED: NO_PRODUCT_DIRECTIONS, ALL_SATURATED, BELOW_MINIMUM');
  });

  it('includes trim note when authoritative count is larger than shipped list', () => {
    const dirs = [
      { id: 'd-1', text: 'one dir', status: 'active', category: 'product', possibly_saturated: false, priority: 'high', total_cycles: 2 },
    ];
    const result = buildDirectionPortfolioSection(dirs, 15, [], undefined, 6, 0);
    expect(result).toContain('Showing 1 of 6 active directions below');
    expect(result).toContain('trimmed for budget');
    expect(result).toContain('./fetch direction');
  });

  it('constructs paused-note with auto/user breakdown', () => {
    const dirs = [
      { id: 'd-1', text: 'active dir', status: 'active', category: 'product', possibly_saturated: false, priority: 'high', total_cycles: 3 },
      { id: 'd-p1', text: 'auto paused', status: 'paused', category: 'structural', auto_paused: true, paused_reason: 'waiting', priority: 'low', total_cycles: 0 },
      { id: 'd-p2', text: 'user paused', status: 'paused', category: 'product', auto_paused: false, paused_reason: '', priority: 'low', total_cycles: 0 },
    ];
    const result = buildDirectionPortfolioSection(dirs, 10, [], undefined);
    expect(result).toContain('Paused: 2 (1 auto, 1 user)');
    expect(result).toContain('listed below but not consuming active slots');
  });

  it('handles empty direction list with authoritative counts (suppresses NO_PRODUCT_DIRECTIONS when detail incomplete)', () => {
    // authoritative=1, shipped=0 → shippedActiveCount < activeCount → detailComplete=false
    // so NO_PRODUCT_DIRECTIONS is suppressed. BELOW_MINIMUM fires (1 < 3).
    const result = buildDirectionPortfolioSection([], 10, [], undefined, 1, 0);
    expect(result).toContain('Active: 1/10 slots used');
    expect(result).toContain('BELOW_MINIMUM');
    expect(result).not.toContain('NO_PRODUCT_DIRECTIONS');
    expect(result).not.toContain('ALL_SATURATED');
  });

  it('handles empty systems array gracefully', () => {
    const dirs = [
      { id: 'd-1', text: 'only dir', status: 'active', category: 'product', possibly_saturated: false, priority: 'high', total_cycles: 1 },
    ];
    const result = buildDirectionPortfolioSection(dirs, 10, [], undefined);
    expect(result).toContain('Active: 1/10 slots used');
    expect(result).toContain('BELOW_MINIMUM');
  });

  it('renders single gaps independently — BELOW_MINIMUM alone', () => {
    const dirs = [
      { id: 'd-1', text: 'product dir', status: 'active', category: 'product', possibly_saturated: false, priority: 'high', total_cycles: 1 },
    ];
    const result = buildDirectionPortfolioSection(dirs, 10, [], undefined);
    expect(result).toContain('BELOW_MINIMUM');
    expect(result).not.toContain('NO_PRODUCT_DIRECTIONS');
    expect(result).not.toContain('ALL_SATURATED');
  });

  it('renders single gaps independently — ALL_SATURATED alone', () => {
    const dirs = [
      { id: 'd-1', text: 'sat product', status: 'active', category: 'product', possibly_saturated: true, priority: 'high', total_cycles: 10 },
      { id: 'd-2', text: 'sat structural', status: 'active', category: 'structural', possibly_saturated: true, priority: 'medium', total_cycles: 8 },
      { id: 'd-3', text: 'sat infra', status: 'active', category: 'infra', possibly_saturated: true, priority: 'medium', total_cycles: 6 },
    ];
    const result = buildDirectionPortfolioSection(dirs, 10, [], undefined, 3);
    expect(result).toContain('ALL_SATURATED');
    expect(result).not.toContain('NO_PRODUCT_DIRECTIONS');
    expect(result).not.toContain('BELOW_MINIMUM');
  });

  it('renders single gaps independently — NO_PRODUCT_DIRECTIONS alone (3+ active, not saturated, not below_min)', () => {
    const dirs = [
      { id: 'd-1', text: 'infra dir', status: 'active', category: 'infra', possibly_saturated: false, priority: 'high', total_cycles: 3 },
      { id: 'd-2', text: 'structural dir', status: 'active', category: 'structural', possibly_saturated: false, priority: 'high', total_cycles: 2 },
      { id: 'd-3', text: 'chore dir', status: 'active', category: 'infra', possibly_saturated: false, priority: 'medium', total_cycles: 1 },
    ];
    const result = buildDirectionPortfolioSection(dirs, 10, [], undefined, 3);
    expect(result).toContain('NO_PRODUCT_DIRECTIONS');
    expect(result).not.toContain('ALL_SATURATED');
    expect(result).not.toContain('BELOW_MINIMUM');
  });

  it('skips gap details when detailComplete is false (shipped list incomplete)', () => {
    // authoritative=2, shipped=0 → shippedActiveCount < activeCount → detailComplete=false
    // so NO_PRODUCT_DIRECTIONS and ALL_SATURATED are suppressed.
    // BELOW_MINIMUM still fires because activeCount (2) < 3, regardless of detailComplete.
    const result = buildDirectionPortfolioSection([], 10, [], undefined, 2, 0);
    expect(result).not.toContain('NO_PRODUCT_DIRECTIONS');
    expect(result).not.toContain('ALL_SATURATED');
    expect(result).toContain('BELOW_MINIMUM');
  });
});

describe('buildPendingFollowupsSection', () => {
  it('returns empty string for undefined input', () => {
    expect(buildPendingFollowupsSection(undefined)).toBe('');
  });

  it('returns empty string for null input', () => {
    expect(buildPendingFollowupsSection(null)).toBe('');
  });

  it('returns empty string for empty array', () => {
    expect(buildPendingFollowupsSection([])).toBe('');
  });

  it('returns empty string for non-array (object) input', () => {
    expect(buildPendingFollowupsSection({ id: 'x', title: 'Y' })).toBe('');
  });

  it('renders header and entry count for a single pending doc', () => {
    const followups = [
      {
        id: 'abc-123',
        title: 'Performance Analysis',
        summary: 'Summary of performance findings.',
        source_kind: 'design',
        topic: 'perf',
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('## Pending Knowledge Follow-ups');
    expect(result).toContain('1 design/web_research document(s)');
    expect(result).toContain('abc-123');
    expect(result).toContain('Performance Analysis');
    expect(result).toContain('[design]');
  });

  it('renders topic when present', () => {
    const followups = [
      {
        id: 'doc-1',
        title: 'Caching Study',
        summary: 'Overview.',
        source_kind: 'web_research',
        topic: 'caching-layer',
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('topic: caching-layer');
  });

  it('renders tags when present (up to 5)', () => {
    const followups = [
      {
        id: 'doc-tags',
        title: 'Tagged Doc',
        summary: 'A tagged doc.',
        source_kind: 'design',
        topic: null,
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: ['alpha', 'beta', 'gamma'],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('[alpha, beta, gamma]');
  });

  it('omits topic when null', () => {
    const followups = [
      {
        id: 'doc-notopic',
        title: 'No Topic Doc',
        summary: 'No topic here.',
        source_kind: 'design',
        topic: null,
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).not.toContain('topic:');
  });

  it('renders direction_id when present', () => {
    const followups = [
      {
        id: 'doc-dir',
        title: 'Direction-linked Doc',
        summary: 'Has a direction.',
        source_kind: 'web_research',
        topic: null,
        direction_id: 'd-11223344',
        created_at: new Date().toISOString(),
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('Direction: d-11223344');
  });

  it('omits direction line when direction_id is null', () => {
    const followups = [
      {
        id: 'doc-nodir',
        title: 'No Direction Doc',
        summary: 'Has no direction.',
        source_kind: 'design',
        topic: null,
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).not.toContain('Direction:');
  });

  it('renders summary when non-empty', () => {
    const followups = [
      {
        id: 'doc-sum',
        title: 'Doc with summary',
        summary: 'This is the summary text.',
        source_kind: 'design',
        topic: null,
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('This is the summary text.');
  });

  it('renders age as "(today)" for a same-day timestamp', () => {
    const followups = [
      {
        id: 'doc-today',
        title: 'Today Doc',
        summary: '',
        source_kind: 'design',
        topic: null,
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('(today)');
  });

  it('renders age as "(Nd ago)" for an older timestamp', () => {
    const pastDate = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();
    const followups = [
      {
        id: 'doc-old',
        title: 'Old Doc',
        summary: '',
        source_kind: 'web_research',
        topic: null,
        direction_id: null,
        created_at: pastDate,
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('d ago');
  });

  it('includes consume and dismiss action hints', () => {
    const followups = [
      {
        id: 'doc-action',
        title: 'Action Hints Doc',
        summary: '',
        source_kind: 'design',
        topic: null,
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: [],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('consume-knowledge-followup');
    expect(result).toContain('dismiss-knowledge-followup');
  });

  it('renders all entries when multiple followups are provided', () => {
    const followups = [
      {
        id: 'doc-multi-1',
        title: 'First Doc',
        summary: 'First summary.',
        source_kind: 'design',
        topic: 'auth',
        direction_id: null,
        created_at: new Date().toISOString(),
        tags: ['x'],
      },
      {
        id: 'doc-multi-2',
        title: 'Second Doc',
        summary: 'Second summary.',
        source_kind: 'web_research',
        topic: 'billing',
        direction_id: 'd-abcdef01',
        created_at: new Date().toISOString(),
        tags: ['y', 'z'],
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('2 design/web_research document(s)');
    expect(result).toContain('doc-multi-1');
    expect(result).toContain('First Doc');
    expect(result).toContain('doc-multi-2');
    expect(result).toContain('Second Doc');
    expect(result).toContain('Direction: d-abcdef01');
  });

  it('handles entry with no optional fields gracefully (no topic, no direction, no summary, no tags)', () => {
    const followups = [
      {
        id: 'doc-bare',
        title: 'Bare Doc',
        source_kind: 'design',
        // no summary, topic, direction_id, created_at, tags
      },
    ];
    const result = buildPendingFollowupsSection(followups);
    expect(result).toContain('Bare Doc');
    expect(result).not.toContain('topic:');
    expect(result).not.toContain('Direction:');
  });
});
