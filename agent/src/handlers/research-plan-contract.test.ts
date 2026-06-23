import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { PR_INELIGIBLE_TASK_TYPES } from './research-plan.js';

describe('PR-ineligible task type contract', () => {
  it('keeps the agent stats renderer in sync with the backend denominator contract', () => {
    const backendSource = readFileSync(
      path.resolve(process.cwd(), '..', 'backend/app/direction_outcomes.py'),
      'utf8',
    );
    const match = backendSource.match(
      /PR_INELIGIBLE_TASK_TYPES = frozenset\(\s*\{([\s\S]*?)\}\s*\)/,
    );
    expect(
      match,
      'backend PR_INELIGIBLE_TASK_TYPES literal should be parseable',
    ).not.toBeNull();
    if (!match) {
      throw new Error('backend PR_INELIGIBLE_TASK_TYPES literal should be parseable');
    }
    const backendTypes = Array.from(match[1].matchAll(/"([^"]+)"/g), (m) => m[1]);
    expect([...PR_INELIGIBLE_TASK_TYPES].sort()).toEqual(backendTypes.sort());
  });
});

describe('compute_experiment smoke-test authoring guidance', () => {
  const source = readFileSync(
    path.resolve(process.cwd(), 'src/handlers/research-plan.ts'),
    'utf8',
  );

  it('includes smoke_sample in the authoring prompt', () => {
    expect(source).toContain('smoke_sample');
  });

  it('includes the generic substrate nudge (project-agnostic)', () => {
    // The nudge must tell authors that nothing beyond the repo itself is guaranteed,
    // without naming any specific project, database, or API endpoint.
    expect(source).toMatch(/nothing beyond the repo itself is guaranteed/);
  });

  it('does NOT encode project-specific infrastructure details in smoke guidance', () => {
    // Find the region of the file that contains the smoke guidance so we can
    // check it in isolation without false-positives from unrelated sections.
    const smokeStart = source.indexOf('smoke_sample');
    expect(smokeStart).toBeGreaterThan(-1);
    // Search a window starting from the first smoke_sample mention (generous
    // 3000-char window covers both research and plan guidance sections).
    const smokeRegion = source.slice(smokeStart, smokeStart + 3000);

    // These strings are project-specific and must NOT appear in smoke guidance:
    const forbidden = [
      'admin_api_key',
      'ADMIN_API_KEY',
      'orchestrator_url',
      'ORCHESTRATOR_URL',
      'AsyncSessionLocal',
      'backend.app.db',
    ];
    for (const term of forbidden) {
      expect(smokeRegion, `smoke guidance must not mention '${term}'`).not.toContain(term);
    }
  });
});
