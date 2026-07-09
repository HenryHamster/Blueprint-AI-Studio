import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { CAPABILITY_DECLARATION_DOC, ACCEPTANCE_EVIDENCE_RUBRIC } from './research-plan-prompt-blocks.js';

const RESEARCH_PLAN_SRC = readFileSync(
  fileURLToPath(new URL('./research-plan.ts', import.meta.url)),
  'utf-8',
);

describe('CAPABILITY_DECLARATION_DOC', () => {
  it('documents both the provides_capability stamp and the prerequisite ref drawn from the registry', () => {
    expect(CAPABILITY_DECLARATION_DOC).toContain('provides_capability');
    expect(CAPABILITY_DECLARATION_DOC).toContain('prerequisite');
    expect(CAPABILITY_DECLARATION_DOC).toContain('AVAILABLE CAPABILITIES');
    // Applies to every task type, not research special-casing.
    expect(CAPABILITY_DECLARATION_DOC).toContain('every other task type');
    // Off-registry refs are accepted (advisory), never rejected.
    expect(CAPABILITY_DECLARATION_DOC).toMatch(/never rejected/);
  });
});

describe('ACCEPTANCE_EVIDENCE_RUBRIC', () => {
  it('documents exactly the five payload/merge-gate kinds', () => {
    for (const kind of ['named_test', 'observable_behavior', 'downstream_artifact', 'visual', 'coverage_delta']) {
      expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain(`"kind": "${kind}"`);
    }
  });

  it('does NOT document the retired {kind: test, descriptor, selector} vocabulary', () => {
    // The old context-only shape was silently dropped from the merge gate; it
    // must not reappear as a second, contradictory acceptance_evidence rubric.
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).not.toContain('"kind": "test"');
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).not.toContain('"descriptor"');
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).not.toContain('"selector"');
  });

  it('coverage_delta_rubric_describes_total_coverage: describes codebase-wide line coverage, not changed-line-differential', () => {
    // The actual backend implementation (acceptance_gate.py:_evaluate_coverage_delta
    // / orchestrator_ci_monitor.py) measures either total codebase line coverage
    // via pytest-cov/c8 or test pass rate as a fallback — NOT changed-line coverage.
    // The rubric must accurately reflect what the gate checks so agents don't
    // calibrate thresholds against differential coverage they think they declared.
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('total codebase line coverage');
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('not changed-line-differential');
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('test pass rate');
  });

  it('names the payload fields the backend AcceptanceEvidence parser honors', () => {
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('"test_path"');
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('"artifact_id"');
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('"component"');
  });
});

describe('research-plan.ts shared-block references', () => {
  it('references the shared rubric from both the plan and research prompts', () => {
    const refs = RESEARCH_PLAN_SRC.match(/\$\{ACCEPTANCE_EVIDENCE_RUBRIC\}/g) ?? [];
    expect(refs.length).toBe(2);
  });

  it('references CAPABILITY_DECLARATION_DOC from both the plan and research prompts', () => {
    const refs = RESEARCH_PLAN_SRC.match(/\$\{CAPABILITY_DECLARATION_DOC\}/g) ?? [];
    expect(refs.length).toBe(2);
  });

  it('no longer inlines the contradictory {kind: test, descriptor, selector} rubric', () => {
    // The research OUTPUT FORMAT previously documented acceptance_evidence a
    // second time with the incompatible {descriptor, selector} vocabulary, which
    // the merge gate silently dropped. Guard against it being pasted back.
    expect(RESEARCH_PLAN_SRC).not.toContain(
      '{"kind": "test"|"behavior"|"artifact"|"visual", "descriptor"',
    );
  });
});
