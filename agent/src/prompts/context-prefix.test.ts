import { describe, expect, it } from 'vitest';
import { buildContextPrefix } from './context-prefix.js';

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
