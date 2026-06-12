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
