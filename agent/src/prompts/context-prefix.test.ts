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
});
