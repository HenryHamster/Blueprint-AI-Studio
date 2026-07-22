import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';

import { PR_INELIGIBLE_TASK_TYPES } from './research-plan.js';
import { AGENT_TASK_TYPE_IDS } from '../taskCapabilities.js';

describe('PR-ineligible task type contract', () => {
  it('keeps the agent stats renderer in sync with the backend denominator contract', () => {
    // PR_INELIGIBLE_TASK_TYPES is now READ_ONLY_TASK_TYPES derived from the
    // registry in task_capabilities.py. Parse it via TaskCapability entries
    // with "read_only" as the third (agent_mode) argument.
    const capabilitiesSource = readFileSync(
      path.resolve(process.cwd(), '..', 'backend/app/task_capabilities.py'),
      'utf8',
    );
    // Match: TaskCapability(\n    TaskType.<id>,\n    "<Name>",\n    "read_only",
    const readOnlyPattern = /TaskCapability\(\s+TaskType\.(\w+),\s+"[^"]+",\s+"read_only"/g;
    const backendTypes: string[] = [];
    let m: RegExpExecArray | null;
    while ((m = readOnlyPattern.exec(capabilitiesSource)) !== null) {
      backendTypes.push(m[1]);
    }
    // Compare only the read_only types the agent still dispatches. The backend
    // may list read_only capabilities the agent runtime no longer knows about
    // (retired handlers whose backend entries are removed in their own cleanup
    // task); those are excluded here so the agent-side contract stays in sync
    // with what the agent can actually run.
    const agentKnown = new Set<string>(AGENT_TASK_TYPE_IDS);
    const comparableBackendTypes = backendTypes.filter(t => agentKnown.has(t));
    expect(
      comparableBackendTypes.length,
      'backend task_capabilities.py must contain at least one read_only TaskCapability',
    ).toBeGreaterThan(0);
    expect([...PR_INELIGIBLE_TASK_TYPES].sort()).toEqual(comparableBackendTypes.sort());
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
