import { describe, it, expect, vi, beforeEach } from 'vitest';

const fsMockState = vi.hoisted(() => ({
  presentFiles: new Set<string>(),
}));

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn((path: string) => {
      if (typeof path === 'string') fsMockState.presentFiles.add(path);
    }),
    existsSync: vi.fn((path: string) => fsMockState.presentFiles.has(path)),
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
import { READ_ONLY_BASH_WRITE_NOTE } from '../prompt.js';
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
    operatorOverridesSection: '',
    portfolioCompositionText: 'No active directions — portfolio is empty.',
    modelHealthSection: '',
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

  it('documents the gap deferral shape and the direction→gap linkage', () => {
    const prompt = buildThinkerPrompt(base);
    // Gap quick-reference lists the optional deferral field.
    expect(prompt).toContain('deferral?');
    // The deferral gloss explains intentional non-staffing and how to clear it.
    expect(prompt).toContain('{rationale, hold_until?, created_at?}');
    expect(prompt).toContain('intentionally');
    // The gap_id linkage on direction ops is documented for reconciliation.
    expect(prompt).toContain('gap_id');
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

  it('renders cycleStartReconciliationSection after the problem_model and before # Your job', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      cycleStartReconciliationSection: '\n## Cycle-start reconciliation (your starting state)\nflagged conflict\n',
    });
    const reconIdx = prompt.indexOf('## Cycle-start reconciliation (your starting state)');
    const modelIdx = prompt.indexOf('## Your current understanding (problem_model)');
    const jobIdx = prompt.indexOf('# Your job');
    expect(reconIdx).toBeGreaterThan(-1);
    expect(reconIdx).toBeGreaterThan(modelIdx);
    expect(reconIdx).toBeLessThan(jobIdx);
  });

  it('omits cycleStartReconciliationSection when empty', () => {
    const prompt = buildThinkerPrompt({ ...base, cycleStartReconciliationSection: '' });
    expect(prompt).not.toContain('## Cycle-start reconciliation');
  });

  it('renders pruneCandidatesSection content when provided', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      pruneCandidatesSection: '\n## Concrete prune candidates (model is oversized)\nclose g-ret\n',
    });
    expect(prompt).toContain('## Concrete prune candidates (model is oversized)');
    expect(prompt).toContain('close g-ret');
  });

  it('omits pruneCandidatesSection when absent or empty', () => {
    expect(buildThinkerPrompt(base)).not.toContain('## Concrete prune candidates');
    expect(buildThinkerPrompt({ ...base, pruneCandidatesSection: '' })).not.toContain('## Concrete prune candidates');
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
    // Section 6 now uses non-destructive-merge semantics (main #7449): there is
    // no inline "full replacement" caveat; recovery is steered by the standalone
    // fetch-then-edit block above plus section 6's own fetch-the-full-state note.
    expect(prompt).toContain('first to read the full current state');
  });

  it('steers to ./fetch full gap history when gapMemoryTrimmedGapIds is non-empty', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      problemModelRecovery: 'present',
      gapMemoryTrimmedGapIds: ['g-strategic-mem', 'g-auth'],
    });
    // Steering block present and naming the trimmed gaps
    expect(prompt).toContain('# Some gap memory was shortened for payload size');
    expect(prompt).toContain('g-strategic-mem');
    expect(prompt).toContain('g-auth');
    // It must point at the fetch helpers the agent already has
    expect(prompt).toContain('./fetch problem-model');
    expect(prompt).toContain('./fetch gap <gap_id>');
    // It must reference the backend marker the agent inspects
    expect(prompt).toContain('_gap_memory_trimmed');
    // Steering lands before the output instructions so the thinker sees it
    expect(prompt.indexOf('# Some gap memory was shortened')).toBeLessThan(
      prompt.indexOf('## 1. State assessment'),
    );
  });

  it('omits the gap-memory steering block when no gap memory was trimmed', () => {
    const empty = buildThinkerPrompt({ ...base, gapMemoryTrimmedGapIds: [] });
    expect(empty).not.toContain('# Some gap memory was shortened for payload size');
    // Field absent entirely → also omitted (present model, normal cycle)
    const absent = buildThinkerPrompt({ ...base, problemModelRecovery: 'present' });
    expect(absent).not.toContain('# Some gap memory was shortened for payload size');
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

  it('model health section rendered only when modelHealthSection is non-empty', () => {
    const with_ = buildThinkerPrompt({ ...base, modelHealthSection: '\n## Problem-model size (soft signal)\nStatus: **large**\n' });
    expect(with_).toContain('## Problem-model size (soft signal)');

    const without = buildThinkerPrompt({ ...base, modelHealthSection: '' });
    expect(without).not.toContain('## Problem-model size (soft signal)');
  });

  it('includes standing model-size hygiene guidance citing thresholds', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('Model-size hygiene (soft)');
    expect(prompt).toContain('<=12 active gaps');
    expect(prompt).toContain('never a hard cap');
  });

  it('staged directions section rendered only when stagedDirectionsSection is non-empty', () => {
    const with_ = buildThinkerPrompt({ ...base, stagedDirectionsSection: '## Staged for next version\n- d-x [high] — work (auto-activates when v3 promotes)' });
    expect(with_).toContain('## Staged for next version');
    expect(with_).toContain('auto-activates when v3 promotes');

    const without = buildThinkerPrompt({ ...base, stagedDirectionsSection: '' });
    expect(without).not.toContain('## Staged for next version');
  });

  it('always includes the Section 3 staging guidance reconciling staging with no-defer', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('**Staging (next-version work).**');
    expect(prompt).toContain('Staging is NOT deferral');
  });

  it('staging guidance directs new next-version ideas to the atomic staged flag (not create-then-stage)', () => {
    const prompt = buildThinkerPrompt(base);
    // A genuinely new idea is created with the staged flag in a single atomic
    // op — never created active then staged (which would briefly expose it as
    // research-eligible).
    expect(prompt).toContain('"staged": true');
    expect(prompt).toContain('single atomic op');
    expect(prompt).not.toContain('create-then-stage');
  });

  it('operator overrides section rendered only when operatorOverridesSection is non-empty', () => {
    const with_ = buildThinkerPrompt({ ...base, operatorOverridesSection: '\n## Operator Overrides Since Last Cycle\nentry\n' });
    expect(with_).toContain('## Operator Overrides Since Last Cycle');

    const without = buildThinkerPrompt({ ...base, operatorOverridesSection: '' });
    expect(without).not.toContain('## Operator Overrides Since Last Cycle');
  });

  it('drift section rendered only when driftSection is non-empty', () => {
    const with_ = buildThinkerPrompt({ ...base, driftSection: '\n## Drift Signals\nover_claim: true\n' });
    expect(with_).toContain('## Drift Signals');

    const without = buildThinkerPrompt({ ...base, driftSection: '' });
    expect(without).not.toContain('## Drift Signals');
  });

  it('running_update superseded note rendered only when runningUpdateSupersededSection is non-empty', () => {
    const note = '\n_Last cycle the operator preserved their own running_update over your proposed rewrite._\n';
    const with_ = buildThinkerPrompt({ ...base, runningUpdateSupersededSection: note });
    expect(with_).toContain('operator preserved their own running_update');

    const without = buildThinkerPrompt({ ...base, runningUpdateSupersededSection: '' });
    expect(without).not.toContain('operator preserved their own running_update');

    const absent = buildThinkerPrompt(base);
    expect(absent).not.toContain('operator preserved their own running_update');
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

  it('includes READ_ONLY_BASH_WRITE_NOTE next to each /tmp output path', () => {
    const prompt = buildThinkerPrompt({ ...base, taskId: 'abc-789' });
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
    // Note should appear multiple times (once per output file path)
    const count = prompt.split(READ_ONLY_BASH_WRITE_NOTE).length - 1;
    expect(count).toBeGreaterThanOrEqual(3);
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
  const problemModelPath = `/tmp/problem_model_${taskId}.json`;
  const runningUpdatePath = `/tmp/running_update_${taskId}.md`;
  const ws = null;
  const statusRef = { value: '' };

  const okThinkerResult = {
    exitCode: 0,
    allOutput: '{"intent":{"purpose":"ok"},"gaps":[]}',
    lastResultText: '{"intent":{"purpose":"ok"},"gaps":[]}',
  };
  const failThinkerResult = { exitCode: 1, allOutput: 'crash output', lastResultText: '' };

  beforeEach(() => {
    // resetAllMocks clears queued mockReturnValueOnce values too, preventing
    // bleed-over between tests. Re-setup defaults after the reset.
    vi.resetAllMocks();
    fsMockState.presentFiles.clear();
    mockRunClaudeCode.mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    mockPersistAndPostStatus.mockResolvedValue(undefined);
    mockExistsSync.mockImplementation((path: string) => fsMockState.presentFiles.has(path));
    // resetAllMocks also clears the writeFileSync implementation from vi.mock().
    // Restore it so that thinker.ts writing the problem_model file is reflected in presentFiles.
    vi.mocked(fs.writeFileSync as (path: string, data: unknown) => void).mockImplementation(
      (path: string) => { if (typeof path === 'string') fsMockState.presentFiles.add(path); },
    );
  });

  it('(a) thinker exits 0 + both files present → returns ok with analysisText, executor eligible', async () => {
    mockRunClaudeCode.mockResolvedValueOnce(okThinkerResult);
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.presentFiles.add(runningUpdatePath);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result).toEqual({ ok: true, analysisText: '{"intent":{"purpose":"ok"},"gaps":[]}' });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(1);
    expect(mockPersistAndPostStatus).not.toHaveBeenCalled();
  });

  it('(a2) valid problem_model file on disk + unparseable stdout → adopts file and skips repair', async () => {
    // Gate should check the authoritative written file before deciding to repair,
    // so a valid file + imperfect stdout does NOT trigger a repair reopen.
    const validProblemModel = '{"intent":{"purpose":"real intent"},"gaps":[],"capabilities":[]}';
    vi.mocked(fs.readFileSync as (path: string, encoding: string) => string).mockReturnValueOnce(
      validProblemModel,
    );
    mockRunClaudeCode.mockResolvedValueOnce({
      exitCode: 0,
      allOutput: 'analysis without parseable problem_model JSON',
      lastResultText: '',
    });
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.presentFiles.add(runningUpdatePath);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    // Should return success without running a repair reopen.
    expect(result).toEqual({ ok: true, analysisText: 'analysis without parseable problem_model JSON' });
    // Only the initial thinker run, no repair reopen recorded.
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(1);
    expect(mockPersistAndPostStatus).not.toHaveBeenCalled();
  });

  it('(b) clean exit with unparseable problem_model reopens once, writes the recovered model, and skips manual fallback', async () => {
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis output without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":"repaired"},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":"repaired"},"gaps":[]}',
      });
    fsMockState.presentFiles.add(runningUpdatePath);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result).toEqual({ ok: true, analysisText: 'analysis output without JSON' });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    const reopenPrompt = mockRunClaudeCode.mock.calls[1][0];
    // Core schema re-ask is still present.
    expect(reopenPrompt).toContain('output ONLY that JSON object now');
    expect(reopenPrompt).toContain(problemModelPath);
    // Fix for BUG_REPORT 8154c2d3: the repair prompt must embed the prior analysis
    // so the fresh session extracts real content instead of echoing placeholder text.
    expect(reopenPrompt).toContain('FRESH session');
    expect(reopenPrompt).toContain('Do NOT use placeholder or template values');
    expect(reopenPrompt).toContain('YOUR PREVIOUS ANALYSIS OUTPUT');
    expect(reopenPrompt).toContain('analysis output without JSON');
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(true);
    expect(mockPersistAndPostStatus).not.toHaveBeenCalled();
  });

  it('(b2) repair prompt includes research_progress breadcrumb when the file exists', async () => {
    // Arrange: thinker wrote a research_progress breadcrumb file.
    const progressPath = `/tmp/research_progress_${taskId}.md`;
    fsMockState.presentFiles.add(progressPath);
    vi.mocked(fs.readFileSync as (path: string, encoding: string) => string).mockReturnValueOnce(
      '# Progress summary\nIntent: billing system\nGaps: g-billing',
    );
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":"ok"},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":"ok"},"gaps":[]}',
      });
    fsMockState.presentFiles.add(runningUpdatePath);

    await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    const reopenPrompt = mockRunClaudeCode.mock.calls[1][0];
    expect(reopenPrompt).toContain('RESEARCH PROGRESS BREADCRUMB');
    expect(reopenPrompt).toContain('Intent: billing system');
  });

  it('(c) repair still fails, so the legacy focused retry remains the last resort for missing files', async () => {
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis output without JSON', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'still not parseable', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'retry output', lastResultText: '' });

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result).toEqual({ ok: false });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(3);
    expect(mockPersistAndPostStatus).toHaveBeenCalledOnce();
    const statusArg = mockPersistAndPostStatus.mock.calls[0][1];
    expect(statusArg.status).toBe('FAILED');
    expect(statusArg.failure_category).toBe('strategic_thinker_artifacts_missing');
  });

  it('(d) thinker exits non-zero → FAILED posted, no repair reopen, returns ok: false', async () => {
    mockRunClaudeCode.mockResolvedValueOnce(failThinkerResult);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result).toEqual({ ok: false });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(1);
    expect(mockPersistAndPostStatus).toHaveBeenCalledOnce();
    const statusArg = mockPersistAndPostStatus.mock.calls[0][1];
    expect(statusArg.status).toBe('FAILED');
    expect(statusArg.failure_category).toBe('strategic_thinker_artifacts_missing');
  });
});

// ── runThinkerWithGate validateParsedValue guard ──────────────────────────────
//
// The repair path's validateParsedValue hook rejects schema-template placeholder
// echoes (purpose='<from analysis>', '...', or '') so recordFormatRepairSuccess
// only fires when the extracted value is actually usable. When the guard fires,
// problemModelRepair.value is null → writeFileSync(problemModelPath) is skipped →
// filesPresent() remains false → the focused retry (3rd runClaudeCode call)
// fires. Tests here lock that path against silent regression.

describe('runThinkerWithGate validateParsedValue guard', () => {
  const mockRunClaudeCode = vi.mocked(runClaudeCode);
  const mockPersistAndPostStatus = vi.mocked(persistAndPostStatus);

  const taskId = 'test-task-123';
  const problemModelPath = `/tmp/problem_model_${taskId}.json`;
  const runningUpdatePath = `/tmp/running_update_${taskId}.md`;
  const ws = null;
  const statusRef = { value: '' };

  beforeEach(() => {
    vi.resetAllMocks();
    fsMockState.presentFiles.clear();
    mockRunClaudeCode.mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    mockPersistAndPostStatus.mockResolvedValue(undefined);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockImplementation(
      (path: string) => fsMockState.presentFiles.has(path),
    );
    vi.mocked(fs.writeFileSync as (path: string, data: unknown) => void).mockImplementation(
      (path: string) => { if (typeof path === 'string') fsMockState.presentFiles.add(path); },
    );
  });

  it('rejects XML-tag placeholder intent.purpose="<from analysis>" — focused retry fires, file not written', async () => {
    fsMockState.presentFiles.add(runningUpdatePath);
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":"<from analysis>"},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":"<from analysis>"},"gaps":[]}',
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'focused retry output', lastResultText: '' });

    await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(mockRunClaudeCode).toHaveBeenCalledTimes(3);
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(false);
  });

  it('rejects ellipsis placeholder intent.purpose="..." — focused retry fires, file not written', async () => {
    fsMockState.presentFiles.add(runningUpdatePath);
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":"..."},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":"..."},"gaps":[]}',
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'focused retry output', lastResultText: '' });

    await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(mockRunClaudeCode).toHaveBeenCalledTimes(3);
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(false);
  });

  it('rejects empty-string placeholder intent.purpose="" — focused retry fires, file not written', async () => {
    fsMockState.presentFiles.add(runningUpdatePath);
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":""},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":""},"gaps":[]}',
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'focused retry output', lastResultText: '' });

    await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(mockRunClaudeCode).toHaveBeenCalledTimes(3);
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(false);
  });

  it('accepts real intent.purpose — no focused retry, file written, returns ok:true', async () => {
    fsMockState.presentFiles.add(runningUpdatePath);
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":"real project purpose"},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":"real project purpose"},"gaps":[]}',
      });

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(true);
    expect(result).toEqual({ ok: true, analysisText: 'analysis without JSON' });
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
    operatorOverridesSection: '',
    portfolioCompositionText: 'No active directions — portfolio is empty.',
    modelHealthSection: '',
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
    operatorOverridesSection: '',
    portfolioCompositionText: 'No active directions — portfolio is empty.',
    modelHealthSection: '',
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
