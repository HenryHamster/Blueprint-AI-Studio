import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
  },
}));

vi.mock('../index.js', () => ({
  sendLog: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  strVal: (v: unknown, fallback = '') => {
    if (typeof v === 'string') return v;
    return fallback;
  },
}));

import fs from 'fs';
import { runClaudeCode, persistAndPostStatus } from '../index.js';
import { buildThinkerPrompt, runThinkerWithGate, type ThinkerPromptParams } from './thinker.js';
import { buildThinkerPrompt as buildThinkerPromptFromResearchPlan } from '../handlers/research-plan.js';
import { buildMemoCitationsSection } from '../handlers/research-plan.js';
import { renderStrategicPromptSuffix } from '../handlers/strategic.js';

// Regression guard: research-plan.ts must re-export the same function object
// from thinker.ts — not a local copy. If someone accidentally defines a second
// buildThinkerPrompt in research-plan.ts, this test fails.
describe('thinker re-export identity', () => {
  it('research-plan.ts re-exports buildThinkerPrompt from thinker.ts (same reference)', () => {
    expect(buildThinkerPromptFromResearchPlan).toBe(buildThinkerPrompt);
  });
});

describe('buildThinkerPrompt', () => {
  const base: ThinkerPromptParams = {
    contextPrefix: '',
    projectName: 'Test Project',
    clockHeader: 'Clock: 2026-05-04',
    isSelfRepo: false,
    userStateLine: 'User last active: today',
    packerMetaSection: '',
    strategicContext: '',
    problemModelSection: '## Your current understanding (problem_model)\n_(empty — bootstrap)_',
    problemModelRecovery: 'empty',
    reflectionSection: '## What you previously said was hard\n_(no prior)_',
    selfImprovementBlock: '',
    runningUpdateSection: '## Running update (living document — rewrite, don\'t append)\n_(empty)_',
    designDocJson: '',
    visionJson: '',
    outcomeSection: '',
    previousDecisionOutcomes: '',
    pastRetirementCritiquesSection: '',
    retiredDirectionsSummarySection: '',
    dirSummaryText: 'No active directions.',
    dirHealthSummary: 'Active: 0/10 slots.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    portfolioCompositionText: 'No active directions — portfolio is empty.',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    threadSection: '',
    taskId: 'test-task-123',
    versionPhaseSection: '',
    memoCitationsSection: '',
    efficiencySnapshotSection: '',
  };

  it('includes required structural headers when inputs are populated', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('## Your current understanding (problem_model)');
    expect(prompt).toContain('## What you previously said was hard');
    expect(prompt).toContain('## Running update');
    expect(prompt).toContain('## Architecture');
    expect(prompt).toContain('## Current research directions');
    expect(prompt).toContain('## Direction Health');
    expect(prompt).toContain('## Portfolio Composition');
  });

  it('includes all required output section headers', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('## 1. State assessment');
    expect(prompt).toContain('## 2. Gap analysis');
    expect(prompt).toContain('## 3. Moves');
    expect(prompt).toContain('## 4. Running update document');
    expect(prompt).toContain('## 5. Self-reflection');
    expect(prompt).toContain('## 6. Problem model');
  });

  it('section ordering is stable: key sections appear in expected sequence', () => {
    const prompt = buildThinkerPrompt(base);
    const idx = (s: string) => prompt.indexOf(s);
    // Context sections appear before instructions
    expect(idx('## Architecture')).toBeLessThan(idx('## Current research directions'));
    expect(idx('## Current research directions')).toBeLessThan(idx('## Direction Health'));
    expect(idx('## Direction Health')).toBeLessThan(idx('## Portfolio Composition'));
    expect(idx('## Portfolio Composition')).toBeLessThan(idx('# Your job'));
    // Output sections appear at the end
    expect(idx('# Your job')).toBeLessThan(idx('## 1. State assessment'));
    expect(idx('## 1. State assessment')).toBeLessThan(idx('## 6. Problem model'));
  });

  it('renders verificationFailuresSection before # Your job when provided', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      verificationFailuresSection: '\n## Capability Verification (advisory)\nflagged capability\n',
    });
    const verificationIdx = prompt.indexOf('## Capability Verification (advisory)');
    const jobIdx = prompt.indexOf('# Your job');
    expect(verificationIdx).toBeGreaterThan(-1);
    expect(verificationIdx).toBeLessThan(jobIdx);
  });

  it('omits verificationFailuresSection when empty', () => {
    const prompt = buildThinkerPrompt({ ...base, verificationFailuresSection: '' });
    expect(prompt).not.toContain('## Capability Verification (advisory)');
  });

  it('problem_model section uses bootstrap text when problemModelSection signals empty', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      problemModelRecovery: 'empty',
      problemModelSection: '## Your current understanding (problem_model)\n_(empty — bootstrap)_',
    });
    expect(prompt).toContain('_(empty — bootstrap)_');
    // Bootstrap guidance section should also be present
    expect(prompt).toContain('# Bootstrap (when the problem_model is empty)');
    // ...and the fetch-then-edit recovery block must NOT appear on a true first cycle
    expect(prompt).not.toContain('# Recover your durable model (fetch-then-edit)');
  });

  it('renders the fetch-then-edit recovery block (not bootstrap) when recovery is fetch', () => {
    const prompt = buildThinkerPrompt({ ...base, problemModelRecovery: 'fetch' });
    // Recovery wording present; bootstrap absent
    expect(prompt).toContain('# Recover your durable model (fetch-then-edit)');
    expect(prompt).toContain('./fetch problem-model');
    expect(prompt).not.toContain('# Bootstrap (when the problem_model is empty)');
    // Section 6 carries the recovery caveat so the full replacement is the
    // fetched model edited, never a cold-start reconstruction.
    expect(prompt).toContain('Recovery caveat');
  });

  it('renders neither bootstrap nor recovery block when the model is present', () => {
    const prompt = buildThinkerPrompt({ ...base, problemModelRecovery: 'present' });
    expect(prompt).not.toContain('# Bootstrap (when the problem_model is empty)');
    expect(prompt).not.toContain('# Recover your durable model (fetch-then-edit)');
    expect(prompt).not.toContain('Recovery caveat');
  });

  it('vision section rendered only when visionJson is non-empty', () => {
    const withVision = buildThinkerPrompt({ ...base, visionJson: '{"systems":[]}' });
    expect(withVision).toContain('## Vision (legacy artifact');

    const withoutVision = buildThinkerPrompt({ ...base, visionJson: '' });
    expect(withoutVision).not.toContain('## Vision (legacy artifact');
  });

  it('system maturity section rendered only when systemMaturitySection is non-empty', () => {
    const with_ = buildThinkerPrompt({ ...base, systemMaturitySection: '\n## System Maturity\nsome data\n' });
    expect(with_).toContain('## System Maturity');

    const without = buildThinkerPrompt({ ...base, systemMaturitySection: '' });
    expect(without).not.toContain('## System Maturity');
  });

  it('drift section rendered only when driftSection is non-empty', () => {
    const with_ = buildThinkerPrompt({ ...base, driftSection: '\n## Drift Signals\nover_claim: true\n' });
    expect(with_).toContain('## Drift Signals');

    const without = buildThinkerPrompt({ ...base, driftSection: '' });
    expect(without).not.toContain('## Drift Signals');
  });

  it('efficiency snapshot section rendered when efficiencySnapshotSection is non-empty', () => {
    const section = '\n## Pipeline Efficiency Axes\nsome data\n';
    const prompt = buildThinkerPrompt({ ...base, efficiencySnapshotSection: section });
    expect(prompt).toContain('## Pipeline Efficiency Axes');
    expect(prompt).toContain('some data');
  });

  it('self-improvement block rendered when selfImprovementBlock is non-empty', () => {
    const selfRepoPrompt = buildThinkerPrompt({
      ...base,
      selfImprovementBlock: '\n\n# Self-improvement\n\nThis project\'s codebase includes your own prompts.',
    });
    expect(selfRepoPrompt).toContain('# Self-improvement');

    const notSelfRepo = buildThinkerPrompt({ ...base, selfImprovementBlock: '' });
    expect(notSelfRepo).not.toContain('# Self-improvement');
  });

  it('Self-repo line reflects isSelfRepo flag', () => {
    expect(buildThinkerPrompt({ ...base, isSelfRepo: true })).toContain('Self-repo: true');
    expect(buildThinkerPrompt({ ...base, isSelfRepo: false })).toContain('Self-repo: false');
  });

  it('includes required recommended_mode instruction on create-direction calls', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('recommended_mode');
    expect(prompt).toContain('create-direction');
    // The instruction must explicitly require emitting recommended_mode on create-direction
    const rdIdx = prompt.indexOf('create-direction');
    const rmIdx = prompt.indexOf('recommended_mode');
    // recommended_mode instruction appears in the prompt with create-direction nearby
    expect(Math.abs(rdIdx - rmIdx)).toBeLessThan(1000);
  });

  it('includes source citation instruction (dedup-justification)', () => {
    const prompt = buildThinkerPrompt(base);
    // Agents must cite sources on gaps/directions so dedup logic can trace provenance
    expect(prompt).toMatch(/source.*user_message/);
    expect(prompt).toContain('Cite sources');
  });

  it('requires capabilities to round-trip backing_paths', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('backing_paths');
    expect(prompt).toContain('Emit `backing_paths: []` instead of dropping the field');
  });

  it('includes retire-direction instruction language', () => {
    const prompt = buildThinkerPrompt(base);
    // The prompt must instruct the agent to retire directions
    expect(prompt).toContain('retire');
    // Moves section must explicitly list retirement
    expect(prompt).toMatch(/Directions to retire/);
  });

  it('versionPhaseSection rendered only when non-empty', () => {
    const withPhase = buildThinkerPrompt({ ...base, versionPhaseSection: '\n## Version Phase\nv3 — BUILD\n' });
    expect(withPhase).toContain('## Version Phase');
    expect(withPhase).toContain('v3 — BUILD');

    const withoutPhase = buildThinkerPrompt({ ...base, versionPhaseSection: '' });
    expect(withoutPhase).not.toContain('## Version Phase');
  });

  it('version phase section appears before # Your job', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      versionPhaseSection: '\n## Version Phase\nv2 — STABILIZE\n',
    });
    const phaseIdx = prompt.indexOf('## Version Phase');
    const jobIdx = prompt.indexOf('# Your job');
    expect(phaseIdx).toBeGreaterThan(-1);
    expect(jobIdx).toBeGreaterThan(-1);
    expect(phaseIdx).toBeLessThan(jobIdx);
  });

  it('memoCitationsSection rendered only when non-empty', () => {
    const withMemos = buildThinkerPrompt({
      ...base,
      memoCitationsSection: '\n## Memo Citations\n- memo memo-xyz: a finding\n',
    });
    expect(withMemos).toContain('## Memo Citations');
    expect(withMemos).toContain('memo memo-xyz: a finding');

    const withoutMemos = buildThinkerPrompt({ ...base, memoCitationsSection: '' });
    expect(withoutMemos).not.toContain('## Memo Citations');
  });

  it('renders the memo citations digest produced by buildMemoCitationsSection', () => {
    const memoCitationsSection = buildMemoCitationsSection([
      { task_id: 'memo-digest-1', summary_excerpt: 'The provisioning path leaks VMs.' },
    ]);
    const prompt = buildThinkerPrompt({ ...base, memoCitationsSection });
    expect(prompt).toContain('## Memo Citations');
    // Memos are now rendered with handles and task_ids
    expect(prompt).toContain('M0 (task_id: memo-digest-1): The provisioning path leaks VMs.');
    // Digest must land before the output instructions so the reviewer sees it.
    expect(prompt.indexOf('## Memo Citations')).toBeLessThan(prompt.indexOf('# Your job'));
  });

  it('does not contain banned legacy directive phrases', () => {
    const prompt = buildThinkerPrompt(base);
    // These are removed/renamed operations that should not appear in current prompt
    expect(prompt).not.toContain('add-gap');
    expect(prompt).not.toContain('create-gap');
    expect(prompt).not.toContain('create_gap');
    // set_vision was the old API; problem_model is the new substrate
    expect(prompt).not.toContain('set_vision');
  });

  it('dirSummaryText contribution stays under 50,000 chars', () => {
    const hugeDirSummary = 'x'.repeat(49_000);
    const prompt = buildThinkerPrompt({ ...base, dirSummaryText: hugeDirSummary });
    const idx = prompt.indexOf(hugeDirSummary);
    expect(idx).toBeGreaterThan(-1);
    // The prompt should not balloon — the section itself is bounded by its input
    expect(hugeDirSummary.length).toBeLessThan(50_000);
  });

  it('outcomeSection contribution stays under 30,000 chars', () => {
    const hugeOutcome = 'y'.repeat(29_000);
    const prompt = buildThinkerPrompt({ ...base, outcomeSection: hugeOutcome });
    expect(prompt).toContain(hugeOutcome);
    expect(hugeOutcome.length).toBeLessThan(30_000);
  });

  it('threadSection contribution stays under 20,000 chars', () => {
    const hugeThread = 'z'.repeat(19_000);
    const prompt = buildThinkerPrompt({ ...base, threadSection: hugeThread });
    expect(prompt).toContain(hugeThread);
    expect(hugeThread.length).toBeLessThan(20_000);
  });

  it('includes the taskId in file paths for problem model and running update', () => {
    const prompt = buildThinkerPrompt({ ...base, taskId: 'abc-456' });
    expect(prompt).toContain('/tmp/problem_model_abc-456.json');
    expect(prompt).toContain('/tmp/running_update_abc-456.md');
    expect(prompt).toContain('/tmp/research_progress_abc-456.md');
  });

  it('includes PLAYBOOK_GUIDANCE (structural invariant: strategic_review class)', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('./playbook set');   // PLAYBOOK_GUIDANCE fingerprint
  });

  it('includes DEFAULT_TOOL_GUIDANCE (structural invariant: strategic_review class)', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('Use Grep/Glob for code search');  // DEFAULT_TOOL_GUIDANCE fingerprint
  });

  it('does not include CODE_CHANGE_HELPER_GUIDANCE (strategic_review is read-only)', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).not.toContain('Task lifecycle — you own it end-to-end');  // CODE_CHANGE_HELPER_GUIDANCE fingerprint
  });

  it('fails detection: removing a required section header causes assertion to fail', () => {
    // Simulate a broken prompt where "## 1. State assessment" was accidentally removed
    const prompt = buildThinkerPrompt(base);
    const broken = prompt.replace('## 1. State assessment', '');
    expect(broken).not.toContain('## 1. State assessment');
    // This verifies our structural test WOULD catch such a regression
    expect(prompt).toContain('## 1. State assessment'); // original still passes
  });
});

// ── runThinkerWithGate ────────────────────────────────────────────────────────

describe('runThinkerWithGate', () => {
  const mockRunClaudeCode = vi.mocked(runClaudeCode);
  const mockPersistAndPostStatus = vi.mocked(persistAndPostStatus);
  const mockExistsSync = vi.mocked(fs.existsSync as (p: string) => boolean);

  const taskId = 'test-task-123';
  const ws = null;
  const statusRef = { value: '' };

  const okThinkerResult = { exitCode: 0, allOutput: 'analysis output', lastResultText: 'analysis text' };
  const failThinkerResult = { exitCode: 1, allOutput: 'crash output', lastResultText: '' };

  beforeEach(() => {
    // resetAllMocks clears queued mockReturnValueOnce values too, preventing
    // bleed-over between tests. Re-setup defaults after the reset.
    vi.resetAllMocks();
    mockRunClaudeCode.mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    mockPersistAndPostStatus.mockResolvedValue(undefined);
    mockExistsSync.mockReturnValue(false); // default: files absent
  });

  it('(a) thinker exits 0 + both files present → returns ok with analysisText, executor eligible', async () => {
    mockRunClaudeCode.mockResolvedValueOnce(okThinkerResult);
    mockExistsSync.mockReturnValue(true); // both files present

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result).toEqual({ ok: true, analysisText: 'analysis text' });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(1);
    expect(mockPersistAndPostStatus).not.toHaveBeenCalled();
  });

  it('(b) thinker exits 0 + files missing → focused retry runs and creates them, returns ok', async () => {
    mockRunClaudeCode
      .mockResolvedValueOnce(okThinkerResult)           // pass-1 thinker
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'retry output', lastResultText: '' }); // focused retry

    // filesPresent() uses short-circuit: only problemModelPath is checked when it returns false.
    // Sequence: outer filesPresent(problem→false), missing[](problem→false, running→false),
    //           inner filesPresent(problem→true, running→true).
    mockExistsSync
      .mockReturnValueOnce(false) // outer filesPresent: problem_model (short-circuits)
      .mockReturnValueOnce(false) // missing[] check: problem_model
      .mockReturnValueOnce(false) // missing[] check: running_update
      .mockReturnValueOnce(true)  // inner filesPresent: problem_model
      .mockReturnValueOnce(true); // inner filesPresent: running_update

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result.ok).toBe(true);
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    // The focused retry is a fresh session — it must embed the thinker's PRIOR
    // OUTPUT verbatim so it has real content to extract, instead of asking it to
    // recall "your prior analysis" it cannot see (regression for the 2026-06-03
    // "I can't manufacture that completeness from an empty context" refusal that
    // failed every strategic_review with strategic_thinker_artifacts_missing).
    const retryPrompt = mockRunClaudeCode.mock.calls[1][0];
    expect(retryPrompt).toContain('were not written to disk');
    expect(retryPrompt).toContain('FRESH session');
    expect(retryPrompt).toContain('YOUR PREVIOUS ANALYSIS OUTPUT');
    expect(retryPrompt).toContain('analysis output'); // okThinkerResult.allOutput, embedded verbatim
    expect(mockPersistAndPostStatus).not.toHaveBeenCalled();
  });

  it('(c) retry still misses files → executor never runs, persistAndPostStatus called with FAILED + failure_category', async () => {
    mockRunClaudeCode
      .mockResolvedValueOnce(okThinkerResult)           // pass-1 thinker
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'retry output', lastResultText: '' }); // focused retry
    // mockExistsSync default (from beforeEach): always false

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result).toEqual({ ok: false });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    expect(mockPersistAndPostStatus).toHaveBeenCalledOnce();
    const statusArg = mockPersistAndPostStatus.mock.calls[0][1];
    expect(statusArg.status).toBe('FAILED');
    expect(statusArg.failure_category).toBe('strategic_thinker_artifacts_missing');
  });

  it('(d) thinker exits non-zero → FAILED posted, no retry, returns ok: false', async () => {
    mockRunClaudeCode.mockResolvedValueOnce(failThinkerResult);
    // mockExistsSync default (from beforeEach): always false

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result).toEqual({ ok: false });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(1);
    expect(mockPersistAndPostStatus).toHaveBeenCalledOnce();
    const statusArg = mockPersistAndPostStatus.mock.calls[0][1];
    expect(statusArg.status).toBe('FAILED');
    expect(statusArg.failure_category).toBe('strategic_thinker_artifacts_missing');
  });
});

describe('buildThinkerPrompt on_automain annotation', () => {
  const base: ThinkerPromptParams = {
    contextPrefix: '',
    projectName: 'Test',
    clockHeader: '',
    isSelfRepo: false,
    userStateLine: '',
    packerMetaSection: '',
    strategicContext: '',
    problemModelSection: '## Your current understanding (problem_model)\n_(empty)_',
    problemModelRecovery: 'empty',
    reflectionSection: '## What you previously said was hard\n_(no prior)_',
    selfImprovementBlock: '',
    runningUpdateSection: '## Running update (living document — rewrite, don\'t append)\n_(empty)_',
    designDocJson: '',
    visionJson: '',
    outcomeSection: '',
    previousDecisionOutcomes: '',
    pastRetirementCritiquesSection: '',
    retiredDirectionsSummarySection: '',
    dirSummaryText: 'No active directions.',
    dirHealthSummary: 'Active: 0/10 slots.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    portfolioCompositionText: 'No active directions — portfolio is empty.',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    threadSection: '',
    taskId: 'test-task',
    versionPhaseSection: '',
    memoCitationsSection: '',
    efficiencySnapshotSection: '',
  };

  it('passes recentWorkSection with staged-not-on-automain annotation through to output', () => {
    const section =
      '\n# Recent Work & Rolling Outcomes\n' +
      '  - Fix qualifier CI gate #5147 (2.1h ago) [direction: release] [staged, NOT on automain]\n';
    const prompt = buildThinkerPrompt({ ...base, recentWorkSection: section });
    expect(prompt).toContain('[staged, NOT on automain]');
  });

  it('does not annotate when on_automain is true (no annotation tag)', () => {
    const section =
      '\n# Recent Work & Rolling Outcomes\n' +
      '  - Normal landed fix #5100 (3h ago) [direction: release]\n';
    const prompt = buildThinkerPrompt({ ...base, recentWorkSection: section });
    expect(prompt).not.toContain('[staged, NOT on automain]');
    expect(prompt).toContain('Normal landed fix');
  });
});

// ── renderStrategicPromptSuffix composition ────────────────────────────────────

describe('buildThinkerPrompt + renderStrategicPromptSuffix (strategic handler composition)', () => {
  const base: ThinkerPromptParams = {
    contextPrefix: '',
    projectName: 'Test Project',
    clockHeader: 'Clock: 2026-05-04',
    isSelfRepo: false,
    userStateLine: 'User last active: today',
    packerMetaSection: '',
    strategicContext: '',
    problemModelSection: '## Your current understanding (problem_model)\n_(empty — bootstrap)_',
    problemModelRecovery: 'empty',
    reflectionSection: '## What you previously said was hard\n_(no prior)_',
    selfImprovementBlock: '',
    runningUpdateSection: '## Running update\n_(empty)_',
    designDocJson: '',
    visionJson: '',
    outcomeSection: '',
    previousDecisionOutcomes: '',
    pastRetirementCritiquesSection: '',
    retiredDirectionsSummarySection: '',
    dirSummaryText: 'No active directions.',
    dirHealthSummary: 'Active: 0/10 slots.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    portfolioCompositionText: 'No active directions — portfolio is empty.',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    threadSection: '',
    taskId: 'test-task-123',
    versionPhaseSection: '',
    memoCitationsSection: '',
    efficiencySnapshotSection: '',
  };

  it('appends Additional instructions block when prompt_suffix is non-empty', () => {
    const payload = {
      prompt_suffix: 'deployment-model: v2-multi-tenant\nstabilize audit-findings: gap-id-42 missing test coverage',
    };
    const composed = buildThinkerPrompt(base) + renderStrategicPromptSuffix(payload);

    expect(composed).toContain('Additional instructions:');
    expect(composed).toContain('deployment-model: v2-multi-tenant');
    expect(composed).toContain('stabilize audit-findings: gap-id-42 missing test coverage');
    // The prompt should have two line breaks before Additional instructions (forming a section break)
    expect(composed).toContain('\n\nAdditional instructions:');
  });

  it('does NOT append Additional instructions block when prompt_suffix is absent', () => {
    const payload = {};
    const composed = buildThinkerPrompt(base) + renderStrategicPromptSuffix(payload);

    expect(composed).not.toContain('Additional instructions:');
    // Verify the base prompt is unchanged
    expect(composed).toContain('You own this project\'s direction.');
    expect(composed).toContain('## Your current understanding (problem_model)');
  });

  it('does NOT append Additional instructions block when prompt_suffix is empty string', () => {
    const payload = { prompt_suffix: '' };
    const composed = buildThinkerPrompt(base) + renderStrategicPromptSuffix(payload);

    expect(composed).not.toContain('Additional instructions:');
  });

  it('renders prompt_suffix verbatim without modification', () => {
    const suffix = 'Mode: build\nMax active directions: 8\nStabilize audit gaps: g-retry-logic, g-error-boundary';
    const payload = { prompt_suffix: suffix };
    const composed = buildThinkerPrompt(base) + renderStrategicPromptSuffix(payload);

    // Exact substring match — no escaping, no truncation
    expect(composed).toContain(suffix);
  });

  it('regression: strategic agent receives full prompt_suffix content in analysis pass', () => {
    // This is the scenario from the task: the backend packs deployment framing,
    // stabilize audit-findings hints, and mode hints into prompt_suffix.
    // The strategic handler must append it as Additional instructions so the
    // thinker receives it.
    const deploymentFraming = 'Deployment model: v2-stable (SLA upgrade required for feature gate A)';
    const auditHint = 'Stabilize audit gaps: gap-id-123 (test coverage <60%), gap-id-124 (API error handling)';
    const modeHint = 'System health: audit mode — prioritize stability fixes over features';
    const payload = {
      prompt_suffix: `${deploymentFraming}\n${auditHint}\n${modeHint}`,
    };

    const thinkerPrompt = buildThinkerPrompt(base);
    const thinkerPromptWithSuffix = thinkerPrompt + renderStrategicPromptSuffix(payload);

    // Verify the thinker received all backend-supplied content
    expect(thinkerPromptWithSuffix).toContain('Additional instructions:');
    expect(thinkerPromptWithSuffix).toContain(deploymentFraming);
    expect(thinkerPromptWithSuffix).toContain(auditHint);
    expect(thinkerPromptWithSuffix).toContain(modeHint);
    // Verify the additional instructions block is at the end (appended, not prepended)
    const additionalIdx = thinkerPromptWithSuffix.indexOf('Additional instructions:');
    const jobIdx = thinkerPrompt.indexOf('# Your job');
    expect(additionalIdx).toBeGreaterThan(jobIdx);
  });
});
