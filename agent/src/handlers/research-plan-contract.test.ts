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

// ── setup_step placement: the prompt must not invert the platform ────────────
//
// The authoring contract told plan authors that `setup_step` runs as a bare
// `bash -lc` on the HOST, before and outside the container. On the self-hosted
// local-runner fleet — the backend that claims most compute jobs — that is
// INVERTED: setup_step is folded inside the isolation sandbox and shares the
// shell and environment of `command`.
//
// A prose-only assertion would be self-certifying (any words would satisfy it),
// so each claim is pinned against the implementation it describes, read from
// source. If either backend changes where it runs setup_step, these fail and the
// prompt must be revisited instead of quietly rotting back into a lie.
describe('compute_experiment setup_step placement contract', () => {
  const repoFile = (rel: string) =>
    readFileSync(path.resolve(process.cwd(), '..', rel), 'utf8');
  const agentFile = (rel: string) =>
    readFileSync(path.resolve(process.cwd(), rel), 'utf8');

  const researchPlanSource = agentFile('src/handlers/research-plan.ts');
  const strategicSource = agentFile('src/handlers/strategic.ts');
  const runnerSource = repoFile('scripts/local_runner.py');
  const computeBackendsSource = repoFile('backend/app/compute_backends.py');

  it('ground truth: the runner-subprocess backend folds setup_step INSIDE the sandbox', () => {
    // scripts/local_runner.py joins the setup step and the command into ONE
    // shell string before handing it to the isolation wrapper, so both run
    // inside the boundary and share an environment.
    expect(runnerSource).toContain(
      'effective_command = "{\\n" + setup_step + "\\n} && {\\n" + command + "\\n}"',
    );
  });

  it('ground truth: the cloud/docker backends run setup_step OUTSIDE the container', () => {
    // backend/app/compute_backends.py: the local-docker wrapper emits the setup
    // step as a host `bash -lc` ahead of `docker run`, and the GCP startup
    // script does the same on the VM before the container starts.
    expect(computeBackendsSource).toContain('bash -lc {setup_step_q}');
    expect(computeBackendsSource).toContain('if ! bash -lc {_setup_step_q}');
  });

  it('the research prompt no longer claims setup_step always runs on the host', () => {
    // The exact inverted sentences that shipped, pinned as literals so the
    // regression cannot return under a reworded diff.
    expect(researchPlanSource).not.toContain(
      'on the HOST, BEFORE and OUTSIDE the container',
    );
    expect(researchPlanSource).not.toContain(
      'host-level prep run BEFORE/OUTSIDE the container',
    );
  });

  it('the research prompt tells authors the placement is backend-dependent', () => {
    expect(researchPlanSource).toContain(
      '**WHERE it runs is BACKEND-DEPENDENT and you cannot choose the backend**',
    );
    // Both halves of the truth must be present — describing only the runner
    // would be the same bug with its sign flipped.
    expect(researchPlanSource).toContain(
      'folded INSIDE the isolation sandbox, same shell as',
    );
    expect(researchPlanSource).toContain('OUTSIDE the container, a bare host');
    // ...and the conclusion the author can act on whichever backend claims it.
    expect(researchPlanSource).toContain('NEVER in \\`setup_step\\`');
  });

  it('the compact compute_experiment schema line agrees with the prose', () => {
    const schemaLine = researchPlanSource
      .split('\n')
      .find((line) => line.includes('"setup_step": "optional'));
    expect(schemaLine, 'compact compute_experiment schema line not found').toBeDefined();
    expect(schemaLine).toContain('backend-dependent');
    expect(schemaLine).not.toContain('host-level prep');
  });

  it('the strategic spawn-task guidance carries the same correction', () => {
    // Third copy of the same claim, in another handler's prompt. Left alone it
    // would keep teaching the false model to every strategist cycle that emits
    // a compute_experiment.
    expect(strategicSource).not.toContain(
      'setup_step is host-level prep run OUTSIDE the container',
    );
    expect(strategicSource).toContain('setup_step placement is BACKEND-DEPENDENT');
  });

  it('no copy of the guidance claims setup_step runs outside the container, however worded', () => {
    // The first fix pinned the ONE sentence it removed, as a literal. A SECOND
    // copy of the same claim lived in strategic.ts under different wording
    // ('setup_step (host-level prep run as bare bash -lc OUTSIDE the
    // container...'), so that literal stayed green while the defect stayed
    // live -- an assertion fitted to its own fix. Match structurally instead:
    // wherever a prompt mentions setup_step and 'outside' in the same breath,
    // it must also say the placement is backend-dependent. Any rewording of
    // the inverted claim trips this; the corrected wording does not.
    const sources: Array<[string, string]> = [
      ['strategic.ts', strategicSource],
      ['research-plan.ts', researchPlanSource],
    ];
    const offenders: string[] = [];
    for (const [label, source] of sources) {
      for (
        let i = source.indexOf('setup_step');
        i !== -1;
        i = source.indexOf('setup_step', i + 1)
      ) {
        const claim = source.slice(i, i + 400);
        if (!/outside/i.test(claim)) continue;
        // Exculpated only by naming the backend dependence nearby -- the whole
        // point of the correction. Window spans both directions because the
        // qualifier may lead or trail the mention.
        const context = source.slice(Math.max(0, i - 600), i + 600);
        if (/backend[-\s]dependent/i.test(context)) continue;
        offenders.push(`${label}: ${claim.slice(0, 160).replace(/\s+/g, ' ')}`);
      }
    }
    expect(
      offenders,
      'unqualified "setup_step runs outside the container" claim(s) found',
    ).toEqual([]);
  });
});
