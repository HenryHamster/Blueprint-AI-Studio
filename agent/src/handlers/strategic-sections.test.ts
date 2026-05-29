import { describe, it, expect } from 'vitest';
import {
  fmtScalar,
  buildSystemMaturitySection,
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
