import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { buildContextPrefix, formatRejectionPatternsSection, formatDomainOverview, REVIEW_GATED_TASK_TYPES } from './context-prefix.js';

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

  it('renders compute_experiment_sweeps section with arm comparison', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        // compute_experiment_sweeps is a dict {sweepId: [_knowledge_index_entry, ...]}
        compute_experiment_sweeps: {
          'sw-abcd1234': [
            { id: 'kd-aaa', title: 'arm baseline', arm_label: 'baseline', criterion_met: false, compute_acceptance_grade: 'measured_only', summary: 'accuracy 0.88' },
            { id: 'kd-bbb', title: 'arm treatment', arm_label: 'treatment', criterion_met: true, compute_acceptance_grade: 'measured_only', summary: 'accuracy 0.93' },
          ],
        },
      }],
    });
    expect(prefix).toContain('## Completed experiment sweeps');
    expect(prefix).toContain('Sweep sw-abcd1234');
    expect(prefix).toContain('**baseline**');
    expect(prefix).toContain('[NOT MET]');
    expect(prefix).toContain('accuracy 0.88');
    expect(prefix).toContain('**treatment**');
    expect(prefix).toContain('[MET]');
    expect(prefix).toContain('[kd-aaa]');
    expect(prefix).toContain('[kd-bbb]');
  });

  it('omits sweep section when compute_experiment_sweeps is absent', () => {
    const prefix = buildContextPrefix({ payloads: [{ type: 'strategic_review' }] });
    expect(prefix).not.toContain('## Completed experiment sweeps');
  });

  it('renders acceptance-grade badge for accepted compute_experiment entry', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        knowledge_index: [
          {
            id: 'kd-compute-1',
            title: 'GPU accuracy benchmark',
            summary: 'Accuracy reached 0.93 >= 0.9 threshold.',
            topic: 'compute',
            source_kind: 'compute_experiment',
            compute_acceptance_grade: 'acceptance',
            compute_acceptance_reason: 'llm_artifact_cited',
          },
        ],
      }],
    });

    expect(prefix).toContain('[kd-compute-1] GPU accuracy benchmark');
    expect(prefix).toContain('[acceptance]');
    expect(prefix).not.toContain('[measured_only');
  });

  it('renders measured-only badge with reason for deterministic compute entry', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        knowledge_index: [
          {
            id: 'kd-compute-2',
            title: 'Deterministic compute result',
            summary: 'Outcome recorded without LLM analysis.',
            topic: 'compute',
            source_kind: 'compute_experiment',
            compute_acceptance_grade: 'measured_only',
            compute_acceptance_reason: 'analysis_mode_not_llm',
          },
        ],
      }],
    });

    expect(prefix).toContain('[kd-compute-2] Deterministic compute result');
    expect(prefix).toContain('[measured_only: analysis_mode_not_llm]');
    expect(prefix).not.toContain('[acceptance]');
  });

  it('renders analysis-unavailable badge when unavailable_cause is present', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        knowledge_index: [
          {
            id: 'kd-compute-3',
            title: 'Compute run with missing grounding',
            summary: 'Analysis was not available for this run.',
            topic: 'compute',
            source_kind: 'compute_experiment',
            compute_acceptance_grade: 'measured_only',
            compute_acceptance_reason: 'missing_grounding_probe',
            unavailable_cause: 'grounding_probe_absent',
          },
        ],
      }],
    });

    expect(prefix).toContain('[kd-compute-3] Compute run with missing grounding');
    expect(prefix).toContain('[analysis_unavailable: grounding_probe_absent]');
    expect(prefix).not.toContain('[measured_only:');
    expect(prefix).not.toContain('[acceptance]');
  });

  it('omits grade badge for non-compute knowledge entries', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        knowledge_index: [
          {
            id: 'kd-web-1',
            title: 'OAuth flow research',
            summary: 'Web research on OAuth device flow.',
            topic: 'auth',
            source_kind: 'web_research',
            compute_acceptance_grade: null,
            compute_acceptance_reason: 'non_compute_forward_quality_gate',
          },
        ],
      }],
    });

    expect(prefix).toContain('[kd-web-1] OAuth flow research');
    expect(prefix).not.toContain('[acceptance]');
    expect(prefix).not.toContain('[measured_only');
    expect(prefix).not.toContain('[analysis_unavailable');
  });

  it('flat knowledge index renders criterion NOT MET for a failing standalone compute_experiment distinct from its trustworthiness grade', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        knowledge_index: [
          {
            id: 'kd-compute-failing',
            title: 'Failed accuracy benchmark',
            summary: 'Accuracy 0.85 < 0.9 threshold — criterion not met.',
            topic: 'compute',
            source_kind: 'compute_experiment',
            criterion_met: false,
            compute_acceptance_grade: 'measured_only',
            compute_acceptance_reason: 'analysis_mode_not_llm',
          },
        ],
      }],
    });

    expect(prefix).toContain('[kd-compute-failing] Failed accuracy benchmark');
    expect(prefix).toContain('[NOT MET]');
    expect(prefix).toContain('[measured_only: analysis_mode_not_llm]');
    expect(prefix).not.toContain('[acceptance]');
    expect(prefix).toContain('Accuracy 0.85 < 0.9 threshold');
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
    // The evidence declaration can originate at plan time or research time;
    // the spawned task sees it as declared "at spawn time".
    expect(prefix).toContain('at spawn time');
    expect(prefix).not.toContain('at plan time');
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

  it('renders a visual acceptance declaration explicitly (component, width, assertion, how-verified)', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      context: {
        // PlanAcceptanceEvidence shape: descriptor = component, selector = width.
        acceptance_evidence: {
          kind: 'visual',
          descriptor: 'MetricsDashboard',
          selector: '375',
          assertion_type: 'overflow',
        },
      },
    });

    expect(prefix).toContain('## Acceptance Evidence');
    expect(prefix).toContain('Kind: visual');
    expect(prefix).toContain('- Component to capture: MetricsDashboard');
    expect(prefix).toContain('- Viewport width: 375px');
    expect(prefix).toContain('- Assertion focus: overflow');
    // It explains how the proof is verified (captured + uploaded + gate-matched).
    expect(prefix).toContain('How it is verified:');
    expect(prefix).toContain('viewport screenshot of MetricsDashboard at 375px');
    expect(prefix).toContain('*.viewport.test.tsx');
    // Visual must NOT fall through to the generic descriptor/selector rendering.
    expect(prefix).not.toContain('- Proof: MetricsDashboard');
    expect(prefix).not.toContain('- Selector: 375');
  });

  it('omits the assertion-focus line for a visual declaration without assertion_type', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      context: {
        acceptance_evidence: { kind: 'visual', descriptor: 'TaskCard', selector: '768' },
      },
    });
    expect(prefix).toContain('- Component to capture: TaskCard');
    expect(prefix).toContain('- Viewport width: 768px');
    expect(prefix).not.toContain('- Assertion focus:');
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

  // qa/docs/deps also ship reviewed code through the same gate, so they receive
  // the same launch context as create/fix/refactor/migrate.
  for (const type of ['qa', 'docs', 'deps']) {
    it(`renders the rejection-pattern section for review-gated type '${type}'`, () => {
      const prefix = buildContextPrefix({
        payloads: [{ type }],
        project_context: { rejection_patterns: { ...rejectionPatterns, task_type: type } },
      });
      expect(prefix).toContain('## Recent rejection patterns (context, not a checklist)');
      expect(prefix).toContain('**regression_risk**');
    });
  }
});

// ── buildContextPrefix — self-review calibration wiring ──────────────────────
describe('buildContextPrefix self-review calibration', () => {
  // Use categories/for_task_type shape — what formatProjectContext reads.
  // by_task_type is only consumed by formatSelfReviewCalibrationSection (called directly).
  const srSource = {
    categories: { 'test-coverage': { miss_rate: 0.7 } },
  };

  // The Self-Review Blind Spots section renders inside formatProjectContext
  // when self_review_catch_rate.categories has entries with miss_rate > 0.
  // Requires at least one array-valued project_context key (hasData) to trigger
  // formatProjectContext at all.
  for (const type of ['qa', 'docs', 'deps']) {
    it(`renders the blind-spots section for review-gated type '${type}'`, () => {
      const prefix = buildContextPrefix({
        payloads: [{ type }],
        project_context: { open_prs: ['Some PR'], self_review_catch_rate: srSource },
      });
      expect(prefix).toContain('### Self-Review Blind Spots');
    });
  }

  it('omits the blind-spots section when categories are absent from self_review_catch_rate', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'research' }],
      project_context: { open_prs: ['Some PR'], self_review_catch_rate: { by_task_type: {} } },
    });
    expect(prefix).not.toContain('### Self-Review Blind Spots');
  });

  // Regression guard: SR catch-rate signal must appear exactly once.
  // Before this refactor, buildContextPrefix rendered it twice for review-gated
  // task types: once via formatProjectContext (### Self-Review Blind Spots) and
  // once via a direct formatSelfReviewCalibrationSection call (## Self-review
  // calibration). The direct call has been removed; only the richer block remains.
  it('test_sr_catch_rate_not_double_rendered', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix' }],
      project_context: {
        open_prs: ['Some PR'],
        self_review_catch_rate: { categories: { correctness: { miss_rate: 0.7 } } },
      },
    });
    // Richer block from formatProjectContext must be present
    expect(prefix).toContain('### Self-Review Blind Spots');
    // Redundant thin block must not appear
    expect(prefix).not.toContain('## Self-review calibration');
    // Confirm the heading appears exactly once (no double-render)
    const occurrences = (prefix.match(/Self-Review Blind Spots/g) ?? []).length;
    expect(occurrences).toBe(1);
  });

  // repo_overview rendering
  it('renders ## Repo Overview with all fields when repo_overview is present', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      repo_overview: {
        tech_stack: 'Python, FastAPI, React',
        directory_layout: 'backend/ agent/ admin-ui/',
        first_steps_summary: 'Run pip install and npm install',
        entry_points: 'backend/app/main.py; agent/src/index.ts',
        test_layout: 'backend/testing/ and agent/src/**/*.test.ts',
        key_conventions: 'Use shared prompt helpers from prompt.ts',
        environment_requirements: 'Python 3.12 and Node 20',
        bootstrap_completed_at: '2026-01-15T10:00:00Z',
        provenance: {
          refreshed_at: '2026-01-15T10:01:00Z',
          source_branch: 'main',
          source_revision: 'abc1234',
        },
      },
    });
    expect(prefix).toContain('## Repo Overview');
    expect(prefix).toContain('**Tech stack:** Python, FastAPI, React');
    expect(prefix).toContain('**Directory layout:** backend/ agent/ admin-ui/');
    expect(prefix).toContain('**First steps:** Run pip install and npm install');
    expect(prefix).toContain('**Entry points:** backend/app/main.py; agent/src/index.ts');
    expect(prefix).toContain('**Test layout:** backend/testing/ and agent/src/**/*.test.ts');
    expect(prefix).toContain('**Key conventions:** Use shared prompt helpers from prompt.ts');
    expect(prefix).toContain('**Environment:** Python 3.12 and Node 20');
    expect(prefix).toContain('**Bootstrapped:** 2026-01-15T10:00:00Z');
    expect(prefix).toContain(
      '**Provenance:** refreshed 2026-01-15T10:01:00Z; branch main; revision abc1234',
    );
  });

  it('omits ## Repo Overview when repo_overview is absent', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
    });
    expect(prefix).not.toContain('## Repo Overview');
  });

  it('omits ## Repo Overview when a required field is missing', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix' }],
      repo_overview: {
        tech_stack: 'Python',
        // directory_layout and first_steps_summary missing
      },
    });
    expect(prefix).not.toContain('## Repo Overview');
  });

  it('renders ## Repo Overview without bootstrapped line when bootstrap_completed_at is absent', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix' }],
      repo_overview: {
        tech_stack: 'TypeScript, Node',
        directory_layout: 'src/ tests/',
        first_steps_summary: 'npm install && npm test',
      },
    });
    expect(prefix).toContain('## Repo Overview');
    expect(prefix).not.toContain('**Bootstrapped:**');
    // optional-field labels must not appear when the fields are absent
    expect(prefix).not.toContain('**Entry points:**');
    expect(prefix).not.toContain('**Test layout:**');
    expect(prefix).not.toContain('**Key conventions:**');
    expect(prefix).not.toContain('**Environment:**');
  });

  it('renders optional repo_overview fields when present', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      repo_overview: {
        tech_stack: 'Python, FastAPI, React',
        directory_layout: 'backend/ agent/ admin-ui/',
        first_steps_summary: 'Run pip install and npm install',
        entry_points: 'backend/app/main.py, admin-ui/src/App.tsx',
        test_layout: 'backend/testing/, agent/src/**/*.test.ts',
        key_conventions: 'async/await everywhere; functional React components',
        environment_requirements: 'Python 3.11, Node 20, Postgres 15',
        bootstrap_completed_at: '2026-01-15T10:00:00Z',
      },
    });
    expect(prefix).toContain('**Entry points:** backend/app/main.py, admin-ui/src/App.tsx');
    expect(prefix).toContain('**Test layout:** backend/testing/, agent/src/**/*.test.ts');
    expect(prefix).toContain('**Key conventions:** async/await everywhere; functional React components');
    expect(prefix).toContain('**Environment:** Python 3.11, Node 20, Postgres 15');
    // optional fields render after required fields and before the bootstrapped timestamp
    expect(prefix.indexOf('**First steps:**')).toBeLessThan(prefix.indexOf('**Entry points:**'));
    expect(prefix.indexOf('**Environment:**')).toBeLessThan(prefix.indexOf('**Bootstrapped:**'));
  });

  it('renders only the present optional repo_overview fields in a mixed payload', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      repo_overview: {
        tech_stack: 'Go',
        directory_layout: 'cmd/ internal/',
        first_steps_summary: 'go build ./...',
        entry_points: 'cmd/server/main.go',
        // test_layout and key_conventions absent
        environment_requirements: 'Go 1.22',
      },
    });
    expect(prefix).toContain('**Entry points:** cmd/server/main.go');
    expect(prefix).toContain('**Environment:** Go 1.22');
    expect(prefix).not.toContain('**Test layout:**');
    expect(prefix).not.toContain('**Key conventions:**');
  });

  it('omits optional repo overview fields and provenance when absent', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'research' }],
      repo_overview: {
        tech_stack: 'TypeScript, Node',
        directory_layout: 'src/ tests/',
        first_steps_summary: 'npm install && npm test',
      },
    });
    expect(prefix).toContain('## Repo Overview');
    expect(prefix).not.toContain('**Entry points:**');
    expect(prefix).not.toContain('**Test layout:**');
    expect(prefix).not.toContain('**Key conventions:**');
    expect(prefix).not.toContain('**Environment requirements:**');
    expect(prefix).not.toContain('**Provenance:**');
  });
});

// ── formatDomainOverview ─────────────────────────────────────────────────────
describe('formatDomainOverview', () => {
  it('renders Domain Knowledge block when summary and body are both present', () => {
    const out = formatDomainOverview({ summary: 'An e-commerce platform.', body: 'Handles orders and inventory.' });
    expect(out).toContain('## Domain Knowledge');
    expect(out).toContain('An e-commerce platform.');
    expect(out).toContain('Handles orders and inventory.');
  });

  it('returns empty string when input is absent', () => {
    expect(formatDomainOverview(undefined)).toBe('');
    expect(formatDomainOverview(null)).toBe('');
  });

  it('returns empty string when input is not an object', () => {
    expect(formatDomainOverview('string')).toBe('');
    expect(formatDomainOverview(42)).toBe('');
  });

  it('returns empty string when summary is missing', () => {
    expect(formatDomainOverview({ body: 'Some body text.' })).toBe('');
  });

  it('returns empty string when body is missing', () => {
    expect(formatDomainOverview({ summary: 'Some summary.' })).toBe('');
  });

  it('returns empty string when both fields are empty strings', () => {
    expect(formatDomainOverview({ summary: '', body: '' })).toBe('');
  });
});

// ── buildContextPrefix — domain_overview wiring ──────────────────────────────
describe('buildContextPrefix — compute experiment sweeps', () => {
  it('renders compute_experiment_sweeps as sweep comparison section', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        compute_experiment_sweeps: {
          'sweep-abc': [
            {
              id: 'kd-arm-1',
              title: 'Arm A: baseline',
              summary: 'Baseline result summary.',
              arm_label: 'baseline',
              criterion_met: true,
              compute_acceptance_grade: 'measured_only',
              source_kind: 'compute_experiment',
            },
            {
              id: 'kd-arm-2',
              title: 'Arm B: optimized',
              summary: 'Optimized result summary.',
              arm_label: 'optimized',
              criterion_met: false,
              compute_acceptance_grade: 'measured_only',
              source_kind: 'compute_experiment',
            },
          ],
        },
      }],
    });

    expect(prefix).toContain('## Completed experiment sweeps');
    expect(prefix).toContain('### Sweep sweep-abc');
    expect(prefix).toContain('**baseline**');
    expect(prefix).toContain('[MET]');
    expect(prefix).toContain('[kd-arm-1]');
    expect(prefix).toContain('Baseline result summary.');
    expect(prefix).toContain('**optimized**');
    expect(prefix).toContain('[NOT MET]');
    expect(prefix).toContain('[kd-arm-2]');
    expect(prefix).toContain('./fetch knowledge <id>');
  });

  it('renders UNMEASURED when criterion_met is null/undefined', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        compute_experiment_sweeps: {
          'sweep-null': [
            {
              id: 'kd-arm-x',
              title: 'Arm X: unmeasured',
              summary: 'Metric not captured.',
              arm_label: 'unmeasured',
              criterion_met: null,
              compute_acceptance_grade: 'measured_only',
              source_kind: 'compute_experiment',
            },
            {
              id: 'kd-arm-y',
              title: 'Arm Y: met',
              arm_label: 'met',
              criterion_met: true,
              compute_acceptance_grade: 'measured_only',
              source_kind: 'compute_experiment',
            },
          ],
        },
      }],
    });
    expect(prefix).toContain('[UNMEASURED]');
    expect(prefix).toContain('[MET]');
    expect(prefix).not.toContain('[NOT MET]');
  });

  it('criterion_met=true renders MET even when grade is measured_only (not acceptance)', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        compute_experiment_sweeps: {
          'sweep-det': [
            {
              id: 'kd-det-1',
              title: 'Deterministic met arm',
              arm_label: 'det',
              criterion_met: true,
              compute_acceptance_grade: 'measured_only',
              source_kind: 'compute_experiment',
            },
            {
              id: 'kd-det-2',
              title: 'Deterministic not-met arm',
              arm_label: 'det2',
              criterion_met: false,
              compute_acceptance_grade: 'measured_only',
              source_kind: 'compute_experiment',
            },
          ],
        },
      }],
    });
    expect(prefix).toContain('[MET]');
    expect(prefix).toContain('[NOT MET]');
  });

  it('shows unavailable verdict when unavailable_cause is set', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        compute_experiment_sweeps: {
          'sweep-xyz': [
            {
              id: 'kd-arm-3',
              title: 'Arm C: failed',
              summary: 'Run failed to capture metric.',
              arm_label: 'failed_arm',
              compute_acceptance_grade: 'measured_only',
              unavailable_cause: 'metric_not_written',
              source_kind: 'compute_experiment',
            },
          ],
        },
      }],
    });

    expect(prefix).toContain('[unavailable (metric_not_written)]');
    expect(prefix).not.toContain('[MET]');
    expect(prefix).not.toContain('[NOT MET]');
  });

  it('omits sweeps that already have a disposition task in flight', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'strategic_review',
        compute_experiment_sweeps: {
          'sweep-disposed': [
            {
              id: 'kd-disposed-1',
              title: 'Disposed arm A',
              arm_label: 'a',
              criterion_met: true,
              sweep_disposition: { task_id: 'task-1', status: 'running' },
            },
            {
              id: 'kd-disposed-2',
              title: 'Disposed arm B',
              arm_label: 'b',
              criterion_met: false,
            },
          ],
        },
      }],
    });
    expect(prefix).not.toContain('## Completed experiment sweeps');
    expect(prefix).not.toContain('sweep-disposed');
    expect(prefix).not.toContain('Disposed arm A');
  });

  it('omits sweep section when compute_experiment_sweeps is absent', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'strategic_review', knowledge_index: [] }],
    });
    expect(prefix).not.toContain('## Completed experiment sweeps');
  });

  it('renders sweep section for research tasks when sweeps are present', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'research',
        compute_experiment_sweeps: {
          'sweep-abc': [
            {
              id: 'kd-arm-1',
              title: 'Some arm',
              arm_label: 'a',
              criterion_met: true,
              compute_acceptance_grade: 'measured_only',
            },
          ],
        },
      }],
    });
    expect(prefix).toContain('## Completed experiment sweeps');
    expect(prefix).toContain('sweep-abc');
  });

  it('omits sweep section when taskType is not research/strategic_review/design_doc_update', () => {
    const prefix = buildContextPrefix({
      payloads: [{
        type: 'create',
        compute_experiment_sweeps: {
          'sweep-abc': [
            {
              id: 'kd-arm-1',
              title: 'Some arm',
              arm_label: 'a',
              compute_acceptance_grade: 'acceptance',
            },
          ],
        },
      }],
    });
    expect(prefix).not.toContain('## Completed experiment sweeps');
  });
});

describe('buildContextPrefix — purpose dedup', () => {
  it('renders only Operator Purpose line when purpose equals user_purpose', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'research' }],
      strategic_intent: {
        user_purpose: 'Make Blueprint handle any project well',
        purpose: 'Make Blueprint handle any project well',
        current_focus: 'Context efficiency',
      },
    });
    expect(prefix).toContain('**Operator Purpose (immutable):** Make Blueprint handle any project well');
    expect(prefix).not.toContain('**Purpose:** Make Blueprint handle any project well');
    expect(prefix).toContain('**Current focus:** Context efficiency');
  });

  it('renders both Purpose lines when purpose differs from user_purpose', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'research' }],
      strategic_intent: {
        user_purpose: 'Original operator purpose',
        purpose: 'Strategist revised purpose',
        current_focus: 'Focus area',
      },
    });
    expect(prefix).toContain('**Operator Purpose (immutable):** Original operator purpose');
    expect(prefix).toContain('**Purpose:** Strategist revised purpose');
  });

  it('renders Purpose line alone when user_purpose is absent', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'research' }],
      strategic_intent: {
        purpose: 'Only purpose set',
      },
    });
    expect(prefix).toContain('**Purpose:** Only purpose set');
    expect(prefix).not.toContain('**Operator Purpose (immutable):**');
  });
});

describe('buildContextPrefix — suppressTaskLists', () => {
  const taskWithLists = {
    payloads: [{ type: 'research' }],
    project_context: {
      active_tasks: [
        { task_type: 'fix', title: 'Fix something', status: 'IN_PROGRESS', prompt: 'Fix the bug' },
      ],
      queued_tasks: [
        { task_type: 'create', title: 'Build feature' },
      ],
      recently_completed_tasks: [
        { task_type: 'refactor', title: 'Refactor module', pr_url: 'https://github.com/example/pull/1' },
      ],
      sibling_tasks: [
        { status: 'QUEUED', title: 'Sibling task', active: false },
      ],
    },
  };

  it('includes task lists by default (no opts)', () => {
    const prefix = buildContextPrefix(taskWithLists);
    expect(prefix).toContain('### Active Tasks');
    expect(prefix).toContain('### Queued Tasks');
    expect(prefix).toContain('### Recently Completed Tasks');
    expect(prefix).toContain('## Sibling Tasks');
  });

  it('suppresses active/queued/recently-completed lists when suppressTaskLists is true', () => {
    const prefix = buildContextPrefix(taskWithLists, { suppressTaskLists: true });
    expect(prefix).not.toContain('### Active Tasks');
    expect(prefix).not.toContain('### Queued Tasks');
    expect(prefix).not.toContain('### Recently Completed Tasks');
    // sibling tasks are NOT task lists — they should still render
    expect(prefix).toContain('## Sibling Tasks');
  });

  it('output is byte-identical to unsuppressed when all task-list inputs are empty', () => {
    const noLists = {
      payloads: [{ type: 'research' }],
      project_context: {
        sibling_tasks: [{ status: 'QUEUED', title: 'Sibling', active: false }],
      },
    };
    const withOpt = buildContextPrefix(noLists, { suppressTaskLists: true });
    const withoutOpt = buildContextPrefix(noLists);
    expect(withOpt).toEqual(withoutOpt);
  });
});

describe('buildContextPrefix — domain overview', () => {
  it('renders Domain Knowledge block when domain_overview is present', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'create' }],
      domain_overview: {
        summary: 'A project management SaaS.',
        body: 'Core concepts: projects, tasks, assignees, sprints.',
      },
    });
    expect(prefix).toContain('## Domain Knowledge');
    expect(prefix).toContain('A project management SaaS.');
    expect(prefix).toContain('Core concepts: projects, tasks, assignees, sprints.');
  });

  it('omits Domain Knowledge block and stays byte-identical when domain_overview is absent', () => {
    const base = { payloads: [{ type: 'create' }] };
    const without = buildContextPrefix(base);
    const withUndefined = buildContextPrefix({ ...base, domain_overview: undefined });
    expect(without).not.toContain('## Domain Knowledge');
    expect(withUndefined).toEqual(without);
  });
});

// ── Direction context condensed rendering ─────────────────────────────────
describe('condensed direction context', () => {
  const sampleDirCtx = {
    title: 'Fix agent prompt overhead',
    priority: 'high',
    implementation_constraints: 'Do not modify prompt.ts exports',
    contract: {
      completion_criteria: 'Direction context section < 200 bytes smaller in batch mode',
      success_evidence: 'Measured in prompt_section_sizes telemetry',
      retirement_condition: 'All three findings produce observable reductions',
    },
    narrative: {
      established_findings: ['Finding A: prompt overhead confirmed', 'Finding B: duplicate rendering'],
      open_questions: ['Question 1: Which sections are redundant?'],
      last_cycle: {
        tasks_created: 5,
        prs_merged: 2,
        prs_rejected: 1,
      },
    },
    failed_approaches: [
      { text: 'Patch rendering inline', failure_type: 'regression' },
      { text: 'Extract to separate file', failure_type: 'circular_import' },
    ],
    recent_outcomes: [
      { tasks_created: 2, prs_merged: 1 },
      { tasks_created: 3, prs_merged: 0 },
    ],
    failure_stage_pattern: { message: 'Fails at review stage', rate: 0.6 },
    scope_success_rates: {
      small: { created: 5, merged: 4, failed: 0 },
      medium: { created: 3, merged: 1, failed: 1 },
    },
    review_iteration_rolling_median: 2.5,
    top_first_review_issues: [
      { description: 'Missing test coverage', count: 4, category: 'test-coverage' },
      { description: 'Error handling gaps', count: 2, category: 'error-handling' },
    ],
    // Fields that should NOT appear in condensed output:
    recent_findings_evidence: [
      { title: 'A finding', claims: ['claim 1'], source_refs: [] },
    ],
    direction_prior_analysis: [
      { title: 'Prior fail', failure_category: 'other' },
    ],
    direction_successful_analysis: [
      { title: 'Success', scope: 'small' },
    ],
    review_effectiveness: { categories: { 'test-coverage': { trend: 'stable' } } },
    review_quality_feedback: { note: 'Missing calibration data' },
    prompt_quality_patterns: ['Pattern 1', 'Pattern 2'],
  };

  it('renders full direction context when co_batch_sibling_count is absent', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix', scope: 'small' }],
      direction_context: { ...sampleDirCtx },
    });
    expect(prefix).toContain('## Direction Context');
    expect(prefix).toContain('**Goal:** Fix agent prompt overhead');
    // Full rendering includes verbose sections
    expect(prefix).toContain('Established findings (what we know)');
    expect(prefix).toContain('Prior analysis from failed attempts');
    expect(prefix).toContain('Approaches that worked');
    expect(prefix).not.toContain('(condensed — batch sibling)');
  });

  it('renders condensed direction context when co_batch_sibling_count >= 3', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix', scope: 'small' }],
      direction_context: { ...sampleDirCtx, co_batch_sibling_count: 3 },
    });
    expect(prefix).toContain('## Direction Context (condensed — batch sibling)');
    expect(prefix).toContain('**Goal:** Fix agent prompt overhead');
    // Condensed still includes structural metadata
    expect(prefix).toContain('**Priority:** high');
    expect(prefix).toContain('**Completion criteria:** Direction context section < 200 bytes smaller');
    expect(prefix).toContain('Scope merge rates: small 4/5 (80%), medium 1/3 (33%, 1 failed)');
    expect(prefix).toContain('Recent PRs on this direction averaged 2.5 review iterations');
    expect(prefix).toContain('Missing test coverage');
    // Condensed omits verbose sections
    expect(prefix).not.toContain('Established findings (what we know)');
    expect(prefix).not.toContain('Prior analysis from failed attempts');
    expect(prefix).not.toContain('Approaches that worked');
    expect(prefix).not.toContain('Recent findings evidence');
  });

  it('renders full direction context when co_batch_sibling_count < 3', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix', scope: 'small' }],
      direction_context: { ...sampleDirCtx, co_batch_sibling_count: 2 },
    });
    expect(prefix).toContain('## Direction Context');
    expect(prefix).not.toContain('(condensed — batch sibling)');
    // Full rendering includes verbose sections
    expect(prefix).toContain('Established findings (what we know)');
  });

  it('condensed context omits recent_findings_evidence entries', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix', scope: 'small' }],
      direction_context: { ...sampleDirCtx, co_batch_sibling_count: 3 },
    });
    // Should not contain the verbose claims+sources block
    expect(prefix).not.toContain('Recent findings evidence');
    expect(prefix).not.toContain('A finding');
    expect(prefix).not.toContain('claim 1');
  });

  it('condensed context shows only top 1 review issue and hints at more', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix', scope: 'small' }],
      direction_context: { ...sampleDirCtx, co_batch_sibling_count: 3 },
    });
    expect(prefix).toContain('Missing test coverage');
    expect(prefix).not.toContain('Error handling gaps');
    expect(prefix).toContain('check full context via');
  });

  it('condensed context omits review_effectiveness and self-review calibration', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix', scope: 'small' }],
      direction_context: { ...sampleDirCtx, co_batch_sibling_count: 3 },
    });
    expect(prefix).not.toContain('Review effectiveness');
    expect(prefix).not.toContain('Self-review calibration');
    expect(prefix).not.toContain('Missing calibration data');
  });

  it('condensed context omits prompt_quality_patterns', () => {
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix', scope: 'small' }],
      direction_context: { ...sampleDirCtx, co_batch_sibling_count: 3 },
    });
    expect(prefix).not.toContain('Prompt quality patterns');
    expect(prefix).not.toContain('Pattern 1');
  });

  it('condensed context includes failure_hotspots when present', () => {
    const dirCtxWithHotspots = {
      ...sampleDirCtx,
      co_batch_sibling_count: 3,
      direction_prior_analysis: [
        { title: 'Fail A', pr_changed_files: ['src/core.py', 'src/utils.py'] },
        { title: 'Fail B', pr_changed_files: ['src/core.py', 'src/helpers.py'] },
      ],
    };
    const prefix = buildContextPrefix({
      payloads: [{ type: 'fix', scope: 'small' }],
      direction_context: dirCtxWithHotspots,
    });
    expect(prefix).toContain('Files appearing in multiple recent failures');
    expect(prefix).toContain('src/core.py');
  });
});
describe('REVIEW_GATED_TASK_TYPES contract', () => {
  it('matches the backend CODE_REVIEW_GATED_TASK_TYPES derived from the registry', () => {
    // REVIEW_GATED_TASK_TYPES is a hand-maintained TS array mirroring the
    // registry-derived CODE_REVIEW_GATED_TASK_TYPES in task_capabilities.py.
    // Parse it via TaskCapability entries with evaluation="code-ci-review".
    const capabilitiesSource = readFileSync(
      path.resolve(process.cwd(), '..', 'backend/app/task_capabilities.py'),
      'utf8',
    );
    // Split on TaskCapability( boundaries to get individual capability blocks,
    // then filter for blocks containing evaluation="code-ci-review".
    const blocks = capabilitiesSource.split(/TaskCapability\(/);
    const backendTypes: string[] = [];
    for (const block of blocks) {
      if (!block.includes('evaluation="code-ci-review"')) continue;
      const m = block.match(/TaskType\.(\w+)/);
      if (m) backendTypes.push(m[1]);
    }
    expect(
      backendTypes.length,
      'backend task_capabilities.py must contain at least one code-ci-review TaskCapability',
    ).toBeGreaterThan(0);
    expect([...REVIEW_GATED_TASK_TYPES].sort()).toEqual(backendTypes.sort());
  });
});
