import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ACCEPTANCE_EVIDENCE_RUBRIC } from './research-plan-prompt-blocks.js';

const RESEARCH_PLAN_SRC = readFileSync(
  fileURLToPath(new URL('./research-plan.ts', import.meta.url)),
  'utf-8',
);

describe('ACCEPTANCE_EVIDENCE_RUBRIC', () => {
  it('documents exactly the four payload/merge-gate kinds', () => {
    for (const kind of ['named_test', 'observable_behavior', 'downstream_artifact', 'visual']) {
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

  it('names the payload fields the backend AcceptanceEvidence parser honors', () => {
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('"test_path"');
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('"artifact_id"');
    expect(ACCEPTANCE_EVIDENCE_RUBRIC).toContain('"component"');
  });
});

describe('research-plan.ts acceptance_evidence documentation', () => {
  it('references the shared rubric from both the plan and research prompts', () => {
    const refs = RESEARCH_PLAN_SRC.match(/\$\{ACCEPTANCE_EVIDENCE_RUBRIC\}/g) ?? [];
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
