import { describe, it, expect, vi } from 'vitest';

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  strVal: (v: unknown, def = '') => (typeof v === 'string' ? v : def),
}));

vi.mock('./research-plan.js', () => {
  return {
    buildResearchScopeGuidance: vi.fn(),
    aggregateDirectionStats: (history: Array<Record<string, unknown>>, agg: Record<string, unknown>) => {
      const total = history.length + ((agg.total_cycles as number) || 0);
      if (total === 0) return '';
      return `Stats: ${total} cycles`;
    },
    buildScopeCalibrationSignal: (history: Array<Record<string, unknown>>) => {
      if (history.length === 0) return '';
      const last = history[history.length - 1];
      const details = Array.isArray(last.failed_task_details) ? last.failed_task_details as Array<{category: string}> : [];
      const codeErrors = details.filter(d => ['build_failed', 'claude_code_failed'].includes(d.category));
      return codeErrors.length > 0 ? '\n  Last cycle failures: CODE_ERROR' : '';
    },
    parseResearchContext: (raw: string) => {
      const established: string[] = [];
      const failed: string[] = [];
      const open: string[] = [];
      let eviction = '';
      type Section = 'established' | 'failed' | 'open' | 'eviction' | '';
      let current: Section = '';
      for (const line of raw.split('\n')) {
        const t = line.trim();
        if (t.startsWith('## Established')) { current = 'established'; continue; }
        if (t.startsWith('## Failed')) { current = 'failed'; continue; }
        if (t.startsWith('## Open')) { current = 'open'; continue; }
        if (t.startsWith('## Eviction')) { current = 'eviction'; continue; }
        if (current === 'eviction') { if (t) eviction = t; }
        else if (t.startsWith('- ') && current === 'established') established.push(t);
        else if (t.startsWith('- ') && current === 'failed') failed.push(t);
        else if (t.startsWith('- ') && current === 'open') open.push(t);
      }
      return { established, failed, open, eviction };
    },
    buildFilteredFindingsSection: (history: Array<Record<string, unknown>>) => {
      const recent = history.slice(-1);
      const items: string[] = [];
      for (const h of recent) {
        const ff = Array.isArray(h.filtered_findings) ? h.filtered_findings as Array<{title: string}> : [];
        for (const f of ff) items.push(f.title);
      }
      return items.length > 0 ? `\n  Recently filtered (avoid): ${items.join(', ')}` : '';
    },
  };
});

import { renderDirection } from './direction-renderer.js';

describe('renderDirection', () => {
  const noTasks: Array<Record<string, unknown>> = [];

  it('renders basic direction with index, priority, text, and id', () => {
    const d = { id: 'd-1', text: 'Improve caching layer', priority: 'high' };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('1. [high] Improve caching layer (id: d-1)');
  });

  it('uses 1-based index', () => {
    const d = { id: 'd-2', text: 'Second direction', priority: 'medium' };
    const result = renderDirection(d, 1, undefined, noTasks, noTasks);
    expect(result).toContain('2. [medium]');
  });

  it('defaults priority to "medium" when missing', () => {
    const d = { id: 'd-3', text: 'No priority direction' };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('[medium]');
  });

  it('includes path scope when d.paths is non-empty', () => {
    const d = { id: 'd-4', text: 'Scoped direction', priority: 'low', paths: ['backend/', 'agent/'] };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('[scope: backend/, agent/]');
  });

  it('omits path scope when d.paths is empty', () => {
    const d = { id: 'd-5', text: 'No paths', priority: 'medium', paths: [] };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).not.toContain('[scope:');
  });

  it('includes stats line when history is non-empty', () => {
    const d = {
      id: 'd-6', text: 'Dir with history', priority: 'medium',
      research_history: [{ tasks_created: 2, prs_merged: 1 }],
    };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('Stats: 1 cycles');
  });

  it('includes history dedup titles from last 5 cycles', () => {
    const d = {
      id: 'd-7', text: 'Dir', priority: 'medium',
      research_history: [{ finding_titles: ['feat: add cache', 'fix: retry'] }],
    };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('Already completed');
    expect(result).toContain('feat: add cache');
    expect(result).toContain('fix: retry');
  });

  it('omits history dedup when no finding_titles in history', () => {
    const d = {
      id: 'd-8', text: 'Dir', priority: 'medium',
      research_history: [{ tasks_created: 1 }],
    };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).not.toContain('Already completed');
  });

  it('renders notes section when d.notes is non-empty', () => {
    const d = {
      id: 'd-9', text: 'Dir with notes', priority: 'medium',
      notes: [{ text: 'Focus on the hot path', added_at: '2026-01-01' }],
    };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('Notes:');
    expect(result).toContain('Focus on the hot path');
  });

  it('omits notes section when d.notes is empty', () => {
    const d = { id: 'd-10', text: 'Dir', priority: 'medium', notes: [] };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).not.toContain('Notes:');
  });

  it('shows saturation warning when possibly_saturated is true', () => {
    const d = { id: 'd-11', text: 'Saturated dir', priority: 'medium', possibly_saturated: true };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('SATURATED');
    expect(result).toContain('direction_update or direction_note');
  });

  it('omits saturation warning when possibly_saturated is false', () => {
    const d = { id: 'd-12', text: 'Not saturated', priority: 'medium', possibly_saturated: false };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).not.toContain('SATURATED');
  });

  it('shows in-flight tasks when direction matches currentDirectionId', () => {
    const d = { id: 'd-current', text: 'Current dir', priority: 'high' };
    const activeTasks = [{ direction_id: 'd-current', title: 'feat: active task' }];
    const queuedTasks = [{ direction_id: 'd-current', title: 'feat: queued task' }];
    const result = renderDirection(d, 0, 'd-current', activeTasks, queuedTasks);
    expect(result).toContain('In-flight:');
    expect(result).toContain('feat: active task');
    expect(result).toContain('feat: queued task');
  });

  it('omits in-flight note when direction does not match currentDirectionId', () => {
    const d = { id: 'd-other', text: 'Other dir', priority: 'medium' };
    const activeTasks = [{ direction_id: 'd-current', title: 'feat: someone else task' }];
    const result = renderDirection(d, 0, 'd-current', activeTasks, noTasks);
    expect(result).not.toContain('In-flight');
  });

  it('omits in-flight note when currentDirectionId is undefined', () => {
    const d = { id: 'd-1', text: 'Dir', priority: 'medium' };
    const activeTasks = [{ direction_id: 'd-1', title: 'feat: task' }];
    const result = renderDirection(d, 0, undefined, activeTasks, noTasks);
    expect(result).not.toContain('In-flight');
  });

  it('includes filtered findings from buildFilteredFindingsSection', () => {
    const d = {
      id: 'd-ff', text: 'Dir', priority: 'medium',
      research_history: [{ filtered_findings: [{ title: 'Duplicate finding A' }] }],
    };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('Recently filtered');
    expect(result).toContain('Duplicate finding A');
  });

  it('renders research context with established count when research_context is provided', () => {
    const d = {
      id: 'd-ctx', text: 'Dir', priority: 'medium',
      research_context: '## Established Findings\n- finding 1 (merged)\n- finding 2\n',
    };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('findings already completed');
  });

  it('omits researchCtx prose (established/failed/open) for the current direction when currentDirectionId matches', () => {
    const d = {
      id: 'd-current',
      text: 'Current dir',
      priority: 'medium',
      research_context: [
        '## Established Findings',
        '- finding 1 (merged)',
        '## Failed Approaches',
        '- approach-alpha-unique',
        '## Open Questions',
        '- question-beta-unique',
      ].join('\n'),
    };
    const result = renderDirection(d, 0, 'd-current', noTasks, noTasks);
    expect(result).not.toContain('findings already completed');
    expect(result).not.toContain('Failed approaches');
    expect(result).not.toContain('Open questions');
    expect(result).not.toContain('approach-alpha-unique');
    expect(result).not.toContain('question-beta-unique');
    // Header line still present
    expect(result).toContain('d-current');
  });

  it('still emits researchCtx prose for non-current directions even when currentDirectionId is set', () => {
    const d = {
      id: 'd-other',
      text: 'Other dir',
      priority: 'medium',
      research_context: [
        '## Established Findings',
        '- finding 1 (merged)',
        '## Failed Approaches',
        '- approach-gamma-unique',
        '## Open Questions',
        '- question-delta-unique',
      ].join('\n'),
    };
    const result = renderDirection(d, 0, 'd-current', noTasks, noTasks);
    expect(result).toContain('findings already completed');
    expect(result).toContain('Failed approaches');
    expect(result).toContain('Open questions');
    expect(result).toContain('approach-gamma-unique');
    expect(result).toContain('question-delta-unique');
  });

  it('renders calibration signal when history has code errors', () => {
    const d = {
      id: 'd-cal', text: 'Dir', priority: 'medium',
      research_history: [{
        failed_task_details: [{ title: 'feat: bad', reason: '', category: 'build_failed' }],
      }],
    };
    const result = renderDirection(d, 0, undefined, noTasks, noTasks);
    expect(result).toContain('CODE_ERROR');
  });
});
