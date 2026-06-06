import { describe, expect, it } from 'vitest';
import { buildContextPrefix, formatRejectionPatternsSection } from './context-prefix.js';

describe('buildContextPrefix', () => {
  it('preserves strategic intent when project context is present', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix' }],
      strategic_intent: {
        purpose: 'Reduce agent retries',
        current_focus: 'Harden prompt context',
        constraints: ['Do not drop operator steering'],
      },
      project_context: {
        open_prs: ['Fix prompt context ordering'],
      },
    });

    expect(prefix).toContain('## Strategic Intent');
    expect(prefix).toContain('**Purpose:** Reduce agent retries');
    expect(prefix).toContain('## Project Context');
    expect(prefix).toContain('Fix prompt context ordering');
  });

  it('preserves legacy project vision when project context is present', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix' }],
      vision: {
        purpose: 'Keep tactical work aligned',
        priorities: ['Prompt quality'],
      },
      project_context: {
        open_prs: ['Backfill prompt tests'],
      },
    });

    expect(prefix).toContain('## Project Vision');
    expect(prefix).toContain('**Purpose:** Keep tactical work aligned');
    expect(prefix).toContain('## Project Context');
    expect(prefix).toContain('Backfill prompt tests');
  });

  it('renders the existing-knowledge index when knowledge_index is present', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        knowledge_index: [
          {
            id: 'kd-123',
            title: 'OAuth device flow survey',
            summary: 'Summary of device-flow tradeoffs.',
            topic: 'auth',
            direction_id: 'd-abcd1234',
            source_kind: 'web_research',
          },
        ],
      }],
    });

    expect(prefix).toContain('## Existing knowledge documents');
    expect(prefix).toContain('[kd-123] OAuth device flow survey');
    expect(prefix).toContain('topic: auth');
    expect(prefix).toContain('direction: d-abcd1234');
    expect(prefix).toContain('Summary of device-flow tradeoffs.');
    expect(prefix).toContain('./fetch knowledge <id>');
  });

  it('omits the knowledge block entirely when knowledge_index is absent', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'research' }],
    });
    expect(prefix).not.toContain('## Existing knowledge documents');
  });

  it('renders the acceptance-evidence section when context.acceptance_evidence is present', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      context: {
        scope: 'medium',
        acceptance_evidence: {
          kind: 'test',
          descriptor: 'end-to-end test of the propagation seam',
          selector: 'testing/test_propagation.py::test_seam',
        },
      },
    });

    expect(prefix).toContain('## Acceptance Evidence');
    expect(prefix).toContain('end-to-end test of the propagation seam');
    expect(prefix).toContain('test (a named test or selector exercising the seam end-to-end)');
    expect(prefix).toContain('testing/test_propagation.py::test_seam');
    // It frames the proof as something to aim for, not a blocking gate.
    expect(prefix).toContain('Aim to produce it');
  });

  it('omits the selector line when no selector is supplied', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      context: {
        acceptance_evidence: {
          kind: 'behavior',
          descriptor: 'retry count visible in the dashboard',
        },
      },
    });

    expect(prefix).toContain('## Acceptance Evidence');
    expect(prefix).toContain('retry count visible in the dashboard');
    expect(prefix).not.toContain('- Selector:');
  });

  it('omits the acceptance-evidence section and stays byte-identical when absent', () => {
    const base = {
      payloads: [{ type: 'create' }],
      context: { scope: 'medium' },
    };
    const withField = buildContextPrefix({
      ...base,
      context: { ...base.context, acceptance_evidence: undefined },
    });
    const without = buildContextPrefix(base);

    expect(without).not.toContain('## Acceptance Evidence');
    expect(withField).toEqual(without);
  });

  it('ignores a malformed (non-object) acceptance_evidence', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      context: { acceptance_evidence: 'not-an-object' },
    });
    expect(prefix).not.toContain('## Acceptance Evidence');
  });
});

// ── formatRejectionPatternsSection ───────────────────────────────────────────
describe('formatRejectionPatternsSection', () => {
  const sampleSource = {
    task_type: 'create',
    window_days: 7,
    sample_size: 5,
    categories: [
      { category: 'correctness', weight: 3.4, task_count: 4, share: 0.8, guidance: 'Re-check edge cases before declaring done.' },
      { category: 'test-coverage', weight: 2.1, task_count: 3, share: 0.6, guidance: 'Add tests for the behavior you change.' },
    ],
  };

  it('renders categories with share, guidance, and the context-not-canon framing', () => {
    const out = formatRejectionPatternsSection(sampleSource, 'create');
    expect(out).toContain('## Recent rejection patterns (context, not a checklist)');
    expect(out).toContain('Over the last 7 days (5 rejected first attempts)');
    expect(out).toContain('first review most often rejected create tasks');
    expect(out).toContain('- **correctness** (~80% of recent rejections) — Re-check edge cases before declaring done.');
    expect(out).toContain('- **test-coverage** (~60% of recent rejections) — Add tests for the behavior you change.');
    expect(out).toContain('do not over-index');
    expect(out).toContain('not findings about your change');
  });

  it('returns empty string when source is absent or has no categories', () => {
    expect(formatRejectionPatternsSection(undefined)).toBe('');
    expect(formatRejectionPatternsSection(null)).toBe('');
    expect(formatRejectionPatternsSection({})).toBe('');
    expect(formatRejectionPatternsSection({ task_type: 'fix', categories: [] })).toBe('');
  });

  it('skips malformed category entries and caps rendering at 3 rows', () => {
    const out = formatRejectionPatternsSection({
      task_type: 'fix',
      window_days: 7,
      sample_size: 9,
      categories: [
        { category: 'security', share: 0.5, guidance: 'Check auth boundaries.' },
        { share: 0.4 }, // no category — skipped
        { category: 'correctness' },
        { category: 'scope-drift' },
        { category: 'performance' }, // 4th valid — beyond the cap
      ],
    }, 'fix');
    expect(out).toContain('- **security** (~50% of recent rejections) — Check auth boundaries.');
    expect(out).toContain('- **correctness**');
    expect(out).toContain('- **scope-drift**');
    expect(out).not.toContain('performance');
  });

  it('falls back to the caller task type and default window when fields are missing', () => {
    const out = formatRejectionPatternsSection({
      categories: [{ category: 'correctness' }],
    }, 'migrate');
    expect(out).toContain('Over the last 7 days,');
    expect(out).toContain('rejected migrate tasks');
  });
});

// ── buildContextPrefix — rejection patterns wiring ───────────────────────────
describe('buildContextPrefix rejection patterns', () => {
  const rejectionPatterns = {
    task_type: 'fix',
    window_days: 7,
    sample_size: 4,
    categories: [
      { category: 'regression_risk', weight: 2.5, task_count: 3, share: 0.75, guidance: 'Run the affected tests.' },
    ],
  };

  it('renders the section for code-change task types', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix' }],
      project_context: { rejection_patterns: rejectionPatterns },
    });
    expect(prefix).toContain('## Recent rejection patterns (context, not a checklist)');
    expect(prefix).toContain('**regression_risk**');
  });

  it('omits the section for non-code-change task types', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'research' }],
      project_context: { rejection_patterns: rejectionPatterns },
    });
    expect(prefix).not.toContain('## Recent rejection patterns');
  });

  it('omits the section when rejection_patterns is absent', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix' }],
      project_context: { open_prs: ['Some PR'] },
    });
    expect(prefix).not.toContain('## Recent rejection patterns');
  });
});
