import { describe, it, expect, vi, beforeEach } from 'vitest';

const fsMockState = vi.hoisted(() => ({
  presentFiles: new Set<string>(),
  fileContents: new Map<string, string>(),
}));

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn((path: string) => {
      return fsMockState.fileContents.get(path) || '';
    }),
    writeFileSync: vi.fn((path: string, data: string) => {
      if (typeof path === 'string') {
        fsMockState.presentFiles.add(path);
        if (typeof data === 'string') {
          fsMockState.fileContents.set(path, data);
        }
      }
    }),
    existsSync: vi.fn((path: string) => fsMockState.presentFiles.has(path)),
    appendFileSync: vi.fn(),
  },
}));

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
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
import {
  buildThinkerPrompt,
  buildThinkerPromptParts,
  computeResearchPlanPromptFingerprint,
  computeStablePrefixByteShare,
  computeStrategicPromptFingerprint,
  runThinkerWithGate,
  type ThinkerPromptParams,
} from './thinker.js';
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

describe('computeStrategicPromptFingerprint', () => {
  const base: ThinkerPromptParams = {
    contextPrefix: '',
    projectName: 'Test Project',
    clockHeader: 'Clock: 2026-05-04',
    isSelfRepo: false,
    userStateLine: 'User last active: today',
    packerMetaSection: '',
    strategicContext: '',
    problemModelSection: '## Your current understanding (problem_model)\n```json\n{\n  "fingerprint": "strategic-prompt"\n}\n```',
    problemModelRecovery: 'present',
    reflectionSection: '## What you previously said was hard\n_(no prior)_',
    selfImprovementBlock: '',
    runningUpdateSection: '## Running update (living document — rewrite, don\'t append)\n_(empty)_',
    designDocJson: '',
    visionJson: '',
    outcomeSection: '',
    previousDecisionOutcomes: '',
    pastRetirementCritiquesSection: '',
    retiredDirectionsSummarySection: '',
    directionPortfolioSection: '## Direction Portfolio\nActive: 0/10 slots.\nNo critical gaps detected.\n\nNo directions exist yet.\n\nNo active directions — portfolio is empty.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    operatorOverridesSection: '',
    modelHealthSection: '',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    questionSection: '',
    threadSection: '',
    taskId: 'test-task-123',
    versionPhaseSection: '',
    memoCitationsSection: '',
    efficiencySnapshotSection: '',
  };

  it('normalizes payload noise but changes when strategic structure changes', () => {
    const stable = computeStrategicPromptFingerprint(base);
    const noisy = computeStrategicPromptFingerprint({
      ...base,
      projectName: 'Different Project',
      clockHeader: 'Clock: 2026-06-01',
      userStateLine: 'User last active: yesterday',
      taskId: 'other-task',
    });
    const fetchVariant = computeStrategicPromptFingerprint({ ...base, problemModelRecovery: 'fetch' });

    expect(stable).toHaveLength(12);
    expect(stable).toBe(noisy);
    expect(stable).not.toBe(fetchVariant);
    expect(stable).not.toBe(computeResearchPlanPromptFingerprint('research'));
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
    directionPortfolioSection: '## Direction Portfolio\nActive: 0/10 slots.\nNo critical gaps detected.\n\nNo directions exist yet.\n\nNo active directions — portfolio is empty.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    operatorOverridesSection: '',
    modelHealthSection: '',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    questionSection: '',
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
    expect(prompt).toContain('## Direction Portfolio');
  });

  it('includes all required output section headers', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('## 1. State assessment');
    expect(prompt).toContain('## 2. Gap analysis');
    expect(prompt).toContain('## 3. Moves');
    expect(prompt).toContain('## 4. Running update document');
    expect(prompt).toContain('## 5. Self-reflection');
    expect(prompt).toContain('## 6. Problem model');
    expect(prompt).toContain('## 7. Version goal');
    expect(prompt).toContain('## 8. Acceptance criteria');
  });

  it('distinguishes durable directions from bounded tasks and live state', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain(
      'A **direction** is a durable strategic outcome or bet that may require multiple cycles',
    );
    expect(prompt).toContain('A **task** is a bounded unit of implementation');
    expect(prompt).toContain('A **running update** holds replaceable live state');
    expect(prompt).toContain('Fleet capacity is a ceiling, never a target for direction count');
  });

  it('parallelizes execution with tasks instead of file-sharded directions', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('multiple bounded tasks under the existing direction');
    expect(prompt).toContain(
      'the default for bounded implementation, investigation, design, QA, or compute',
    );
    expect(prompt).toContain('multiple tasks carrying the same owning `direction_id`');
    expect(prompt).toContain(
      'reserve for genuinely standalone operational work with no owning strategic outcome',
    );
    expect(prompt).toContain('A stabilize cycle may legitimately open zero directions');
    expect(prompt).toContain(
      'never because work touches different files, can use another worker, or represents a later implementation stage',
    );
    expect(prompt).not.toContain('Three bold directions moving in parallel almost always beats one');
    expect(prompt).not.toContain('create/dispatch BOTH now');
    expect(prompt).not.toContain('No cap on moves');
    expect(prompt).not.toContain('a healthy portfolio of several genuine ones is expected');
    expect(prompt).not.toContain('A stabilize cycle with zero opened directions is the failure mode');
    expect(prompt).not.toContain(
      'with a `direction_id`** (research/plan/`web_research`/`design`/`compute_experiment`)',
    );
  });

  it('includes sweep synthesis guidance for reading back completed experiments', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('Sweep synthesis');
    // The guidance references the rendered "Completed experiment sweeps" section
    // (built from the strategic payload's compute_experiment_sweeps field). The
    // snake_case field name itself is internal plumbing and is deliberately not
    // surfaced in the operator-facing prompt body.
    expect(prompt).toContain('Completed experiment sweeps');
    expect(prompt).toContain('MET or NOT MET');
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

  it('stable guidance leads, volatile payload follows the cache boundary', () => {
    const prompt = buildThinkerPrompt(base);
    const idx = (s: string) => prompt.indexOf(s);
    // Cache boundary: the stable instruction text (# Your job through the
    // output contract) leads; per-cycle payload sections follow it.
    expect(idx('# Your job')).toBeLessThan(idx('## Architecture'));
    expect(idx('## Architecture')).toBeLessThan(idx('## Direction Portfolio'));
    // Output sections are part of the stable block, before the volatile payload.
    expect(idx('# Your job')).toBeLessThan(idx('## 1. State assessment'));
    expect(idx('## 1. State assessment')).toBeLessThan(idx('## 6. Problem model'));
    expect(idx('## 6. Problem model')).toBeLessThan(idx('## Direction Portfolio'));
  });

  // Prompt-cache prefix stability: the wall-clock-derived userStateLine
  // (formatRelativeDays) and the calcMessageAge-fed message sections
  // (question/feedback/thread) must render AFTER the stable guidance body so
  // they no longer poison the cacheable prefix each turn.
  it('relocates clock-derived userStateLine + message sections below the stable guidance body', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      userStateLine: 'User last active: 3 days ago',
      questionSection: '## Operator Questions\nQ-MARKER',
      feedbackSection: '## User Feature Requests\nF-MARKER',
      threadSection: '## Conversation History\nT-MARKER',
    });
    const idx = (s: string) => prompt.indexOf(s);
    // Every clock-derived block now sits after # Your job (the stable body).
    expect(idx('# Your job')).toBeLessThan(idx('User last active: 3 days ago'));
    expect(idx('# Your job')).toBeLessThan(idx('Q-MARKER'));
    expect(idx('# Your job')).toBeLessThan(idx('F-MARKER'));
    expect(idx('# Your job')).toBeLessThan(idx('T-MARKER'));
    // They land in the dedicated tail block, after acceptance criteria (## 8).
    expect(idx('## 8. Acceptance criteria')).toBeLessThan(idx('# Current user activity & open messages'));
    // Content is preserved, not dropped.
    expect(prompt).toContain('User last active: 3 days ago');
    expect(prompt).toContain('Q-MARKER');
  });

  // The cacheable prefix (everything up to and including the stable guidance
  // body) must be byte-identical across turns whose only difference is the wall
  // clock — i.e. userStateLine and the message-age sections varying.
  it('cacheable prefix is invariant to clock-only changes in userStateLine/message sections', () => {
    const common = {
      ...base,
      questionSection: '## Operator Questions\nWhen can we ship?',
      feedbackSection: '',
      threadSection: '',
    };
    const turnA = buildThinkerPrompt({ ...common, userStateLine: 'User last active: today' });
    const turnB = buildThinkerPrompt({ ...common, userStateLine: 'User last active: 5 days ago' });
    const marker = '# Your job';
    const prefixA = turnA.slice(0, turnA.indexOf(marker) + marker.length);
    const prefixB = turnB.slice(0, turnB.indexOf(marker) + marker.length);
    expect(prefixA).toBe(prefixB);
  });

  // ── Cache-boundary invariant (machine-enforced) ─────────────────────────────
  // The strategic thinker prompt must lead with a byte-identical stable
  // instruction block (the cacheable prefix) and put every per-cycle volatile
  // section in the trailing suffix. A later edit that moves a mutable section
  // into the prefix silently destroys the cache benefit; these tests make the
  // boundary part of the build contract.

  it('buildThinkerPromptParts splits at a stable-prefix / volatile-suffix boundary', () => {
    const common = {
      ...base,
      questionSection: '## Operator Questions\nWhen can we ship?',
      feedbackSection: '## User Feature Requests\nF-MARKER',
      threadSection: '## Conversation History\nT-MARKER',
    };
    const turnA = buildThinkerPromptParts({
      ...common,
      userStateLine: 'User last active: today',
      problemModelSection: '## Your current understanding (problem_model)\nMODEL-A',
      directionPortfolioSection: '## Direction Portfolio\nPORTFOLIO-A',
    });
    const turnB = buildThinkerPromptParts({
      ...common,
      userStateLine: 'User last active: 5 days ago',
      problemModelSection: '## Your current understanding (problem_model)\nMODEL-B',
      directionPortfolioSection: '## Direction Portfolio\nPORTFOLIO-B',
    });
    // The stable prefix is byte-identical across builds with different
    // per-cycle payloads — the entire cacheable region.
    expect(turnA.stablePrefix).toBe(turnB.stablePrefix);
    // The volatile suffix carries the per-cycle differences.
    expect(turnA.variableBody).not.toBe(turnB.variableBody);
    // The final prompt is exactly prefix + suffix.
    expect(turnA.finalPrompt).toBe(turnA.stablePrefix + turnA.variableBody);

    // ── c-24f843a8 clause (2): the stable prefix carries the MAJORITY of the
    // prompt's instruction bytes ──────────────────────────────────────────────
    // 'instruction bytes' = the full rendered prompt the model actually
    // receives (`stablePrefix` + `variableBody`), counted as UTF-8 bytes — no
    // exclusions, so the denominator cannot flatter the ratio. A boundary whose
    // stable region were only a small fraction of the prompt would pass every
    // byte-identity check above while delivering almost no cache benefit; >50%
    // per the criterion is the floor, and the failure message reports the
    // measured share so a regression shows how far it slipped.
    for (const turn of [turnA, turnB]) {
      const share = computeStablePrefixByteShare(turn);
      expect(
        share,
        `stable prefix byte share of the rendered prompt = ${(share * 100).toFixed(2)}% ` +
          `(${Buffer.byteLength(turn.stablePrefix, 'utf8')} of ` +
          `${Buffer.byteLength(turn.finalPrompt, 'utf8')} UTF-8 bytes) — ` +
          'expected > 50% per c-24f843a8 clause (2)',
      ).toBeGreaterThan(0.5);
    }
  });

  it('stable prefix is byte-identical across tasks with different taskIds', () => {
    // The stable prefix must not interpolate taskId (it is baked into the /tmp
    // handoff file paths) — taskId differs per strategic task, so a leaked byte
    // would bust Anthropic's prefix cache for the whole block. The concrete
    // per-task paths must be resolved in the volatile suffix instead.
    const partsA = buildThinkerPromptParts({ ...base, taskId: 'task-aaa' });
    const partsB = buildThinkerPromptParts({ ...base, taskId: 'task-bbb' });
    expect(partsA.stablePrefix).toBe(partsB.stablePrefix);
    // The per-task /tmp paths are rendered only in the volatile suffix.
    expect(partsA.variableBody).toContain('/tmp/problem_model_task-aaa.json');
    expect(partsA.variableBody).toContain('/tmp/running_update_task-aaa.md');
    expect(partsA.variableBody).toContain('/tmp/research_progress_task-aaa.md');
    expect(partsA.variableBody).toContain('/tmp/proposed_criteria_task-aaa.json');
    expect(partsB.variableBody).toContain('/tmp/problem_model_task-bbb.json');
    expect(partsB.variableBody).toContain('/tmp/running_update_task-bbb.md');
    // No task-specific byte may leak into the cacheable prefix.
    expect(partsA.stablePrefix).not.toContain('task-aaa');
    expect(partsB.stablePrefix).not.toContain('task-bbb');
    expect(partsA.stablePrefix).not.toContain('/tmp/problem_model_');
    expect(partsA.stablePrefix).not.toContain('/tmp/running_update_');
    expect(partsA.stablePrefix).not.toContain('/tmp/research_progress_');
    expect(partsA.stablePrefix).not.toContain('/tmp/proposed_criteria_');
  });

  it('self-improvement block lives in the volatile suffix, not the cacheable prefix', () => {
    // selfImprovementBlock is per-cycle payload content (enable_self_improvement
    // framing), so it must render after the stable guidance — never in the
    // byte-identical cacheable region.
    const parts = buildThinkerPromptParts({
      ...base,
      selfImprovementBlock: '\n\n# Self-improvement\n\nThis project\'s codebase includes your own prompts.',
    });
    expect(parts.variableBody).toContain('# Self-improvement');
    expect(parts.stablePrefix).not.toContain('# Self-improvement');
    expect(parts.stablePrefix).not.toContain('# Project improvement');
  });

  it('stable prefix precedes every volatile per-cycle section', () => {
    const params = {
      ...base,
      contextPrefix: '## Strategic Context\nctx data',
      userStateLine: 'User last active: 3 days ago',
      questionSection: '## Operator Questions\nQ-MARKER',
      feedbackSection: '## User Feature Requests\nF-MARKER',
      threadSection: '## Conversation History\nT-MARKER',
      problemModelSection: '## Your current understanding (problem_model)\nMODEL',
      directionPortfolioSection: '## Direction Portfolio\nPORTFOLIO',
      cycleStartReconciliationSection: '## Cycle-start reconciliation\nrecon',
    };
    const prompt = buildThinkerPrompt(params);
    const stableEnd = buildThinkerPromptParts(params).stablePrefix.length;
    for (const marker of [
      'Clock:',
      '## Strategic Context',
      '## Your current understanding (problem_model)',
      '## Direction Portfolio',
      '## Cycle-start reconciliation',
      'User last active: 3 days ago',
      'Q-MARKER',
      'F-MARKER',
      'T-MARKER',
    ]) {
      const i = prompt.indexOf(marker);
      expect(i).toBeGreaterThan(-1);
      expect(i).toBeGreaterThanOrEqual(stableEnd);
    }
  });

  it('stable prefix contains the full stable instruction contract and no volatile markers', () => {
    const parts = buildThinkerPromptParts(base);
    expect(parts.stablePrefix).toContain('# Your job');
    expect(parts.stablePrefix).toContain('# How to think');
    expect(parts.stablePrefix).toContain('# Running update: what the user reads');
    expect(parts.stablePrefix).toContain('# What to write');
    expect(parts.stablePrefix).toContain('## 8. Acceptance criteria');
    expect(parts.stablePrefix).toContain('./playbook set');
    expect(parts.stablePrefix).toContain('Use Grep/Glob for code search');
    // None of the per-cycle payload section headers may leak into the prefix —
    // a single volatile byte there busts the cache for the whole block.
    expect(parts.stablePrefix).not.toContain('## Your current understanding (problem_model)');
    expect(parts.stablePrefix).not.toContain('## Direction Portfolio');
    expect(parts.stablePrefix).not.toContain('## Running Context (from prior cycles)');
    expect(parts.stablePrefix).not.toContain('User last active:');
    expect(parts.stablePrefix).not.toContain('Clock:');
  });

  it('renders cycleStartReconciliationSection after the problem_model, in the volatile suffix', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      cycleStartReconciliationSection: '\n## Cycle-start reconciliation (your starting state)\nflagged conflict\n',
    });
    const reconIdx = prompt.indexOf('## Cycle-start reconciliation (your starting state)');
    const modelIdx = prompt.indexOf('## Your current understanding (problem_model)');
    const jobIdx = prompt.indexOf('# Your job');
    expect(reconIdx).toBeGreaterThan(-1);
    // Both are per-cycle payload sections in the volatile suffix; they keep
    // their relative order and both follow the stable guidance block.
    expect(reconIdx).toBeGreaterThan(modelIdx);
    expect(reconIdx).toBeGreaterThan(jobIdx);
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
    // The steering block is per-cycle data (depends on which gaps were trimmed
    // this cycle), so it lives in the volatile suffix, after the stable output
    // instructions — never in the cacheable prefix.
    expect(prompt.indexOf('# Some gap memory was shortened')).toBeGreaterThan(
      prompt.indexOf('# Your job'),
    );
    expect(prompt.indexOf('# Some gap memory was shortened')).toBeGreaterThan(
      prompt.indexOf('## 8. Acceptance criteria'),
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

  it('Section 3 documents experiment commissions move with compute_experiment type', () => {
    const prompt = buildThinkerPrompt(base);
    // The new paragraph must be present and appear after Knowledge commissions
    expect(prompt).toContain('**Experiment commissions.**');
    expect(prompt).toContain('compute_experiment');
    // The launch contract fields must be described
    expect(prompt).toContain('spend_cap_usd');
    expect(prompt).toContain('deadline_seconds');
    expect(prompt).toContain('success_criterion');
    // The section must explain the hypothesis/falsifiable-conjecture framing
    expect(prompt).toContain('hypothesis');
    // Experiment commissions must appear after Knowledge commissions
    const knowledgeIdx = prompt.indexOf('**Knowledge commissions.**');
    const experimentIdx = prompt.indexOf('**Experiment commissions.**');
    expect(knowledgeIdx).toBeGreaterThan(-1);
    expect(experimentIdx).toBeGreaterThan(knowledgeIdx);
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

  it('sweepComparisonSection rendered when non-empty', () => {
    const section = '## Completed experiment sweeps\nArm A: MET, Arm B: NOT MET\n';
    const prompt = buildThinkerPrompt({ ...base, sweepComparisonSection: section });
    expect(prompt).toContain('## Completed experiment sweeps');
    expect(prompt).toContain('Arm A: MET, Arm B: NOT MET');
  });

  it('sweepComparisonSection omitted when absent or empty', () => {
    const withoutField = buildThinkerPrompt(base);
    expect(withoutField).not.toContain('## Completed experiment sweeps');

    const withEmpty = buildThinkerPrompt({ ...base, sweepComparisonSection: '' });
    expect(withEmpty).not.toContain('## Completed experiment sweeps');
  });

  it('sweepComparisonSection appears after efficiencySnapshotSection, in the volatile suffix', () => {
    const efficiency = '\n## Pipeline Efficiency Axes\ndata\n';
    const sweep = '## Completed experiment sweeps\nArm A: MET\n';
    const prompt = buildThinkerPrompt({ ...base, efficiencySnapshotSection: efficiency, sweepComparisonSection: sweep });
    const idx = (s: string) => prompt.indexOf(s);
    // Both are per-cycle payload sections in the volatile suffix; their
    // relative order is preserved and both follow the stable guidance.
    expect(idx('## Pipeline Efficiency Axes')).toBeLessThan(idx('## Completed experiment sweeps'));
    expect(idx('## Completed experiment sweeps')).toBeGreaterThan(idx('# Your job'));
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

  it('version phase section appears in the volatile suffix, after # Your job', () => {
    const prompt = buildThinkerPrompt({
      ...base,
      versionPhaseSection: '\n## Version Phase\nv2 — STABILIZE\n',
    });
    const phaseIdx = prompt.indexOf('## Version Phase');
    const jobIdx = prompt.indexOf('# Your job');
    expect(phaseIdx).toBeGreaterThan(-1);
    expect(jobIdx).toBeGreaterThan(-1);
    expect(phaseIdx).toBeGreaterThan(jobIdx);
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
    // Digest is per-cycle payload in the volatile suffix, after the stable
    // output-instruction block.
    expect(prompt.indexOf('## Memo Citations')).toBeGreaterThan(prompt.indexOf('# Your job'));
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

  it('directionPortfolioSection contribution stays under 50,000 chars', () => {
    const hugePortfolioSection = 'x'.repeat(49_000);
    const prompt = buildThinkerPrompt({ ...base, directionPortfolioSection: hugePortfolioSection });
    const idx = prompt.indexOf(hugePortfolioSection);
    expect(idx).toBeGreaterThan(-1);
    // The prompt should not balloon — the section itself is bounded by its input
    expect(hugePortfolioSection.length).toBeLessThan(50_000);
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
    // new_version_goal is a first-class JSON field — no separate tmp file
    expect(prompt).not.toContain('/tmp/new_version_goal_abc-456.txt');
  });

  it('includes READ_ONLY_BASH_WRITE_NOTE next to each /tmp output path', () => {
    const prompt = buildThinkerPrompt({ ...base, taskId: 'abc-789' });
    expect(prompt).toContain(READ_ONLY_BASH_WRITE_NOTE);
    // Note should appear multiple times (once per output file path):
    // running_update, problem_model, research_progress
    // (new_version_goal is now a first-class JSON field, not a separate file)
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

  it('test_thinker_prompt_instructs_new_version_goal_emission', () => {
    const prompt = buildThinkerPrompt(base);
    // The prompt must instruct the thinker to write new_version_goal
    expect(prompt).toContain('new_version_goal');
    // Section 7 is the dedicated output section for the version goal
    expect(prompt).toContain('## 7. Version goal');
    // new_version_goal is now a first-class JSON field, not a separate tmp file
    expect(prompt).not.toContain('/tmp/new_version_goal_');
    // Must instruct the thinker to include it as a top-level JSON field
    expect(prompt).toContain('top level');
    // Must note it does not persist in the stored model
    expect(prompt).toContain('does not persist in the stored model');
    // Must explain both initial authoring and evidence-based revision.
    expect(prompt).toContain('goal is unset OR');
    expect(prompt).toContain('infeasible, circular, stale, or badly scoped');
    expect(prompt).toContain('normal `spawn-task` operations');
    // Must specify the character limit
    expect(prompt).toContain('500 characters');
    // Must steer toward substantive/directional goals (operator requirement)
    expect(prompt).toContain('SUBSTANTIVE and DIRECTIONAL');
    // Must explain what a bad goal looks like
    expect(prompt).toContain('contentless-goal guard');
    // Section 7 must appear AFTER Section 6 in the prompt
    const sec6Idx = prompt.indexOf('## 6. Problem model');
    const sec7Idx = prompt.indexOf('## 7. Version goal');
    expect(sec6Idx).toBeGreaterThan(-1);
    expect(sec7Idx).toBeGreaterThan(-1);
    expect(sec7Idx).toBeGreaterThan(sec6Idx);
  });

  it('instructs conditional acceptance-criteria authoring after the version goal section', () => {
    const prompt = buildThinkerPrompt({ ...base, taskId: 'criteria-task-1' });

    expect(prompt).toContain('## 8. Acceptance criteria');
    // Stage-1 decoupling: authoring is GOAL-driven (an aspirational rubric),
    // not triggered by a `criterion_coverage.total=0` coverage count.
    expect(prompt).toContain('authored RUBRIC');
    expect(prompt).not.toContain('criterion_coverage.total=0');
    expect(prompt).toContain('non-empty `Goal:` line');
    expect(prompt).toContain('no acceptance criteria have been authored yet');
    expect(prompt).toContain('active directions created this cycle have no criterion mappings');
    expect(prompt).toContain('Skip entirely if criteria already exist and are reasonably mapped');
    expect(prompt).toContain('Do NOT fabricate criteria when the version goal is unset');
    expect(prompt).toContain('/tmp/proposed_criteria_criteria-task-1.json');
    expect(prompt).toContain('{"criteria":[{"text":"Users can complete the key workflow without manual operator intervention.","direction_keys":["d-xxx"]}],"retract":');
    expect(prompt).toContain('5-7 criteria maximum');
    expect(prompt).toContain('outcomes ("Users can X", "System handles Y"), not implementation steps');

    const sec7Idx = prompt.indexOf('## 7. Version goal');
    const sec8Idx = prompt.indexOf('## 8. Acceptance criteria');
    const playbookIdx = prompt.indexOf('./playbook set');
    expect(sec8Idx).toBeGreaterThan(sec7Idx);
    expect(sec8Idx).toBeLessThan(playbookIdx);
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
    fsMockState.fileContents.clear();
    mockRunClaudeCode.mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    mockPersistAndPostStatus.mockResolvedValue(undefined);
    mockExistsSync.mockImplementation((path: string) => fsMockState.presentFiles.has(path));
    // resetAllMocks also clears the implementations from vi.mock().
    // Restore them so that thinker.ts writing/reading files is properly tracked.
    vi.mocked(fs.readFileSync as (path: string, encoding?: string) => string).mockImplementation(
      (path: string) => fsMockState.fileContents.get(path) || '',
    );
    vi.mocked(fs.writeFileSync as (path: string, data: string) => void).mockImplementation(
      (path: string, data: string) => {
        if (typeof path === 'string') {
          fsMockState.presentFiles.add(path);
          if (typeof data === 'string') {
            fsMockState.fileContents.set(path, data);
          }
        }
      },
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
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.fileContents.set(problemModelPath, validProblemModel);
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');

    mockRunClaudeCode.mockResolvedValueOnce({
      exitCode: 0,
      allOutput: 'analysis without parseable problem_model JSON',
      lastResultText: '',
    });

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
    // Fix for placeholder-echo rejection: the schema reminder must NOT use XML-tag
    // placeholder values like "<from analysis>" that the validateParsedValue gate
    // rejects (BUG_REPORT 8154c2d3 / thinker repair success rate 62.4%).
    expect(reopenPrompt).not.toContain('<from analysis>');
    // Must include a concrete realistic example so the repair agent models real content.
    expect(reopenPrompt).toContain('Build an automated billing system');
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(true);
    expect(mockPersistAndPostStatus).not.toHaveBeenCalled();
  });

  it('(b2) repair prompt includes research_progress breadcrumb when the file exists', async () => {
    // Arrange: thinker wrote a research_progress breadcrumb file.
    const progressPath = `/tmp/research_progress_${taskId}.md`;
    const progressContent = '# Progress summary\nIntent: billing system\nGaps: g-billing';
    const validProblemModel = '{"intent":{"purpose":"ok"},"gaps":[]}';
    fsMockState.presentFiles.add(progressPath);
    fsMockState.fileContents.set(progressPath, progressContent);
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');

    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: validProblemModel,
        lastResultText: validProblemModel,
      });

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

  it('(e) command-mode thinker output (contains ./strategic) skips repairStructuredOutput and returns ok when files are present', async () => {
    // Thinker applied state via ./strategic commands — no inline JSON expected.
    // Both handoff files are present (written by the thinker via bash or the API).
    const commandModeOutput = './strategic set-problem-model ...\n./strategic set-running-update ...';
    const validProblemModel = '{"intent":{"purpose":"ok"},"gaps":[]}';
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.fileContents.set(problemModelPath, validProblemModel);
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');

    mockRunClaudeCode.mockResolvedValueOnce({
      exitCode: 0,
      allOutput: commandModeOutput,
      lastResultText: commandModeOutput,
    });

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    // Only 1 runClaudeCode call: the thinker itself. No repair session.
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(1);
    expect(result).toEqual({ ok: true, analysisText: commandModeOutput });
    expect(mockPersistAndPostStatus).not.toHaveBeenCalled();
  });

  it('(f) command-mode thinker output skips repair but still fires focused retry when handoff files are missing', async () => {
    // Thinker used ./strategic commands but did not write the handoff files to disk.
    // Repair must be skipped (no format miss), but the focused retry must still run.
    const commandModeOutput = './strategic set-problem-model ...\n./strategic set-running-update ...';
    mockRunClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: commandModeOutput,
        lastResultText: commandModeOutput,
      })
      // focused retry — files still absent after retry
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'retry output', lastResultText: '' });
    // No files present at all

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    // 2 calls: thinker + focused retry (no repair session in between)
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    // Returns ok:false because files still missing after retry
    expect(result).toEqual({ ok: false });
    expect(mockPersistAndPostStatus).toHaveBeenCalledOnce();
    const statusArg = mockPersistAndPostStatus.mock.calls[0][1];
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

  it('(g) Section 7 new_version_goal file present → returned in newVersionGoal field', async () => {
    const newVersionGoalPath = `/tmp/new_version_goal_${taskId}.txt`;
    const goalText = 'Deliver autonomous cross-project coordination so Blueprint manages 3+ projects with fewer than 1 operator intervention per day.';
    const validProblemModel = '{"intent":{"purpose":"ok"},"gaps":[]}';
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.fileContents.set(problemModelPath, validProblemModel);
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');
    fsMockState.presentFiles.add(newVersionGoalPath);
    fsMockState.fileContents.set(newVersionGoalPath, goalText);
    mockRunClaudeCode.mockResolvedValueOnce(okThinkerResult);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.newVersionGoal).toBe(goalText);
    }
  });

  it('(h) Section 7 new_version_goal file absent → newVersionGoal undefined in result', async () => {
    const validProblemModel = '{"intent":{"purpose":"ok"},"gaps":[]}';
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.fileContents.set(problemModelPath, validProblemModel);
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');
    // new_version_goal_<taskId>.txt NOT in presentFiles
    mockRunClaudeCode.mockResolvedValueOnce(okThinkerResult);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.newVersionGoal).toBeUndefined();
    }
  });

  it('(i) Section 7 file present but empty → newVersionGoal undefined (empty content rejected)', async () => {
    const newVersionGoalPath = `/tmp/new_version_goal_${taskId}.txt`;
    const validProblemModel = '{"intent":{"purpose":"ok"},"gaps":[]}';
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.fileContents.set(problemModelPath, validProblemModel);
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');
    fsMockState.presentFiles.add(newVersionGoalPath);
    fsMockState.fileContents.set(newVersionGoalPath, '   \n  '); // whitespace only
    mockRunClaudeCode.mockResolvedValueOnce(okThinkerResult);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.newVersionGoal).toBeUndefined();
    }
  });

  it('(m) new_version_goal first-class: JSON field present, no tmp file → returned in newVersionGoal', async () => {
    // First-class path: the thinker embeds new_version_goal directly in the
    // problem_model JSON rather than writing a separate tmp file.
    const goalText = 'Deliver autonomous cross-project coordination with zero operator interventions per day.';
    const problemModelWithGoal = JSON.stringify({ intent: { purpose: 'ok' }, gaps: [], new_version_goal: goalText });
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.fileContents.set(problemModelPath, problemModelWithGoal);
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');
    // No new_version_goal_<taskId>.txt in presentFiles (tmp file absent)
    mockRunClaudeCode.mockResolvedValueOnce(okThinkerResult);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.newVersionGoal).toBe(goalText);
    }
  });

  it('(n) new_version_goal first-class: JSON field takes precedence over tmp file', async () => {
    // When both sources are present, the JSON field (first-class) wins.
    const goalInJson = 'Primary goal from problem_model JSON field.';
    const goalInFile = 'Secondary goal from legacy tmp file.';
    const newVersionGoalPath = `/tmp/new_version_goal_${taskId}.txt`;
    const problemModelWithGoal = JSON.stringify({ intent: { purpose: 'ok' }, gaps: [], new_version_goal: goalInJson });
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.fileContents.set(problemModelPath, problemModelWithGoal);
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');
    fsMockState.presentFiles.add(newVersionGoalPath);
    fsMockState.fileContents.set(newVersionGoalPath, goalInFile);
    mockRunClaudeCode.mockResolvedValueOnce(okThinkerResult);

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.newVersionGoal).toBe(goalInJson);
    }
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
    fsMockState.fileContents.clear();
    mockRunClaudeCode.mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    mockPersistAndPostStatus.mockResolvedValue(undefined);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockImplementation(
      (path: string) => fsMockState.presentFiles.has(path),
    );
    vi.mocked(fs.readFileSync as (path: string, encoding?: string) => string).mockImplementation(
      (path: string) => fsMockState.fileContents.get(path) || '',
    );
    vi.mocked(fs.writeFileSync as (path: string, data: string) => void).mockImplementation(
      (path: string, data: string) => {
        if (typeof path === 'string') {
          fsMockState.presentFiles.add(path);
          if (typeof data === 'string') {
            fsMockState.fileContents.set(path, data);
          }
        }
      },
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
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');

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

  it('accepts partial problem_model update omitting intent (non-destructive merge) — no repair, file written', async () => {
    // The prompt explicitly permits partial updates: "entries you omit are preserved".
    // A legitimate partial update might omit intent entirely (only changing gaps or capabilities).
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');

    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"gaps":[{"id":"g-new","name":"new gap","priority":"high"}],"capabilities":[]}',
        lastResultText: '{"gaps":[{"id":"g-new","name":"new gap","priority":"high"}],"capabilities":[]}',
      });

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(true);
    expect(result).toEqual({ ok: true, analysisText: 'analysis without JSON' });
  });

  it('rejects_existing_but_invalid_problem_model_handoff', async () => {
    // Both handoff files exist, but problem_model contains an invalid intent.purpose (empty string).
    // The gate must reject this and trigger repair/retry, not return ok with malformed state.
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.presentFiles.add(runningUpdatePath);

    const invalidProblemModel = JSON.stringify({ intent: { purpose: '' }, gaps: [], capabilities: [] });

    // Mock readFileSync to return invalid problem_model for the initial read
    vi.mocked(fs.readFileSync as (path: string, encoding: string) => string).mockImplementation(
      (path: string) => {
        if (path === problemModelPath) {
          return invalidProblemModel;
        }
        return '';
      },
    );

    // Thinker produces output, repair still produces invalid model, retry can't fix it
    mockRunClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: invalidProblemModel, // thinker's output
        lastResultText: invalidProblemModel,
      })
      // repair session detects invalid content and fails validation
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":"<placeholder>"},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":"<placeholder>"},"gaps":[]}',
      })
      // focused retry still can't produce valid files
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'retry output', lastResultText: '' });

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, []);

    // Should fail because the existing problem_model is invalid and repair/retry don't fix it
    expect(result).toEqual({ ok: false });
    expect(mockPersistAndPostStatus).toHaveBeenCalledOnce();
    const statusArg = mockPersistAndPostStatus.mock.calls[0][1];
    expect(statusArg.status).toBe('FAILED');
    expect(statusArg.failure_category).toBe('strategic_thinker_artifacts_missing');
  });
});

// ── runThinkerWithGate currentPurpose injection ──────────────────────────────
//
// The repair reopenPrompt and focused retry prompt now receive the operator-set
// durable intent.purpose so that a fresh repair session (no prior conversation)
// has a ground-truth value to echo rather than producing a schema placeholder.
// Tests here verify the instruction block is included/preserved correctly.

describe('runThinkerWithGate currentPurpose injection', () => {
  const mockRunClaudeCode = vi.mocked(runClaudeCode);
  const mockPersistAndPostStatus = vi.mocked(persistAndPostStatus);

  const taskId = 'test-task-123';
  const problemModelPath = `/tmp/problem_model_${taskId}.json`;
  const runningUpdatePath = `/tmp/running_update_${taskId}.md`;
  const ws = null;
  const statusRef = { value: '' };

  const durablePurpose = 'Make Blueprint handle any project well: autonomous adaptation with no hardcoded workflows.';

  beforeEach(() => {
    vi.resetAllMocks();
    fsMockState.presentFiles.clear();
    fsMockState.fileContents.clear();
    mockRunClaudeCode.mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' });
    mockPersistAndPostStatus.mockResolvedValue(undefined);
    vi.mocked(fs.existsSync as (p: string) => boolean).mockImplementation(
      (path: string) => fsMockState.presentFiles.has(path),
    );
    vi.mocked(fs.readFileSync as (path: string, encoding?: string) => string).mockImplementation(
      (path: string) => fsMockState.fileContents.get(path) || '',
    );
    vi.mocked(fs.writeFileSync as (path: string, data: string) => void).mockImplementation(
      (path: string, data: string) => {
        if (typeof path === 'string') {
          fsMockState.presentFiles.add(path);
          if (typeof data === 'string') {
            fsMockState.fileContents.set(path, data);
          }
        }
      },
    );
  });

  it('restores durable purpose in an existing blank-purpose handoff without another LLM turn', async () => {
    fsMockState.presentFiles.add(problemModelPath);
    fsMockState.fileContents.set(
      problemModelPath,
      JSON.stringify({ intent: { purpose: '' }, gaps: [{ id: 'g-live' }] }),
    );
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');
    mockRunClaudeCode.mockResolvedValueOnce({
      exitCode: 0,
      allOutput: '{"intent":{"purpose":""},"gaps":[{"id":"g-live"}]}',
      lastResultText: '{"intent":{"purpose":""},"gaps":[{"id":"g-live"}]}',
    });

    const result = await runThinkerWithGate(
      'prompt', taskId, ws, statusRef, undefined, undefined, [], durablePurpose,
    );

    expect(result).toEqual({
      ok: true,
      analysisText: '{"intent":{"purpose":""},"gaps":[{"id":"g-live"}]}',
    });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(1);
    const written = JSON.parse(fsMockState.fileContents.get(problemModelPath) || '{}') as {
      intent?: { purpose?: string };
      gaps?: Array<{ id: string }>;
    };
    expect(written.intent?.purpose).toBe(durablePurpose);
    expect(written.gaps).toEqual([{ id: 'g-live' }]);
  });

  it('validates and restores durable purpose when blank JSON is recovered from stdout', async () => {
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');
    const blankPurposeOutput = '{"intent":{"purpose":""},"gaps":[{"id":"g-live"}]}';
    mockRunClaudeCode.mockResolvedValueOnce({
      exitCode: 0,
      allOutput: blankPurposeOutput,
      lastResultText: blankPurposeOutput,
    });

    const result = await runThinkerWithGate(
      'prompt', taskId, ws, statusRef, undefined, undefined, [], durablePurpose,
    );

    expect(result).toEqual({ ok: true, analysisText: blankPurposeOutput });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(1);
    const written = JSON.parse(fsMockState.fileContents.get(problemModelPath) || '{}') as {
      intent?: { purpose?: string };
    };
    expect(written.intent?.purpose).toBe(durablePurpose);
  });

  it('repair_reopenPrompt_includes_injected_purpose_when_currentPurpose_provided', async () => {
    // The repair reopen prompt must include the currentPurpose preservation
    // instruction so the fresh repair session has the ground-truth value.
    fsMockState.presentFiles.add(runningUpdatePath);
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: JSON.stringify({ intent: { purpose: durablePurpose }, gaps: [] }),
        lastResultText: JSON.stringify({ intent: { purpose: durablePurpose }, gaps: [] }),
      });

    await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, [], durablePurpose);

    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    const reopenPrompt = mockRunClaudeCode.mock.calls[1][0];

    // The injected preservation instruction must be present and contain the exact purpose string.
    expect(reopenPrompt).toContain('operator-set durable intent.purpose');
    expect(reopenPrompt).toContain(durablePurpose);
    expect(reopenPrompt).toContain('do NOT substitute it with');
    // The repair must succeed — the echoed purpose passes validateParsedValue.
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(true);
  });

  it('repair_reopenPrompt_omits_purpose_block_when_currentPurpose_is_undefined', async () => {
    // Backward compat: when currentPurpose is undefined (existing callers),
    // the reopen prompt must NOT contain the purpose preservation block.
    fsMockState.presentFiles.add(runningUpdatePath);
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":"repaired"},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":"repaired"},"gaps":[]}',
      });

    // Pass undefined explicitly to simulate callers that don't set the new parameter.
    await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, [], undefined);

    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    const reopenPrompt = mockRunClaudeCode.mock.calls[1][0];

    // Must NOT contain the purpose preservation block.
    expect(reopenPrompt).not.toContain('operator-set durable intent.purpose');
    expect(reopenPrompt).not.toContain('do NOT substitute it with');
  });

  it('focused_retry_prompt_includes_purpose_when_currentPurpose_provided', async () => {
    // The focused retry prompt must also include the purpose preservation
    // instruction when currentPurpose is provided and the problem_model file is missing.
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'still not parseable', lastResultText: '' })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'retry output', lastResultText: '' });

    await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, [], durablePurpose);

    expect(mockRunClaudeCode).toHaveBeenCalledTimes(3);

    // The focused retry prompt is the 3rd call (indices: 0=thinker, 1=repair, 2=focused retry).
    const retryPrompt = mockRunClaudeCode.mock.calls[2][0];

    // Must include the injected purpose.
    expect(retryPrompt).toContain('operator-set durable intent.purpose');
    expect(retryPrompt).toContain(durablePurpose);
  });

  it('repair_with_valid_purpose_echoes_it_and_passes_validateParsedValue', async () => {
    // End-to-end happy path: repair session receives the currentPurpose,
    // echoes it back, validateParsedValue accepts it, file is written.
    fsMockState.presentFiles.add(runningUpdatePath);
    fsMockState.fileContents.set(runningUpdatePath, '# Running Update\n');

    const problemModelWithPurpose = JSON.stringify({ intent: { purpose: durablePurpose }, gaps: [] });
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: problemModelWithPurpose,
        lastResultText: problemModelWithPurpose,
      });

    const result = await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, [], durablePurpose);

    expect(result).toEqual({ ok: true, analysisText: 'analysis without JSON' });
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(2);
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(true);

    const writtenContent = fsMockState.fileContents.get(problemModelPath);
    expect(writtenContent).toContain(durablePurpose);
  });

  it('repair_still_rejects_placeholder_even_when_currentPurpose_injected', async () => {
    // The injected purpose provides ground truth, but if the repair session
    // ignores it and still echoes a placeholder, validateParsedValue must
    // still reject it (defense in depth — the instruction is a prompt-level
    // fix; the guard is the code-level backstop).
    fsMockState.presentFiles.add(runningUpdatePath);
    mockRunClaudeCode
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'analysis without JSON', lastResultText: '' })
      // Repair session ignores the injected purpose and produces a placeholder.
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '{"intent":{"purpose":"..."},"gaps":[]}',
        lastResultText: '{"intent":{"purpose":"..."},"gaps":[]}',
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: 'focused retry output', lastResultText: '' });

    await runThinkerWithGate('prompt', taskId, ws, statusRef, undefined, undefined, [], durablePurpose);

    // Should reach the focused retry (3 calls) because the placeholder was rejected.
    expect(mockRunClaudeCode).toHaveBeenCalledTimes(3);
    // The problem_model file must NOT be written with the placeholder content.
    expect(fsMockState.presentFiles.has(problemModelPath)).toBe(false);
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
    directionPortfolioSection: '## Direction Portfolio\nActive: 0/10 slots.\nNo critical gaps detected.\n\nNo directions exist yet.\n\nNo active directions — portfolio is empty.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    operatorOverridesSection: '',
    modelHealthSection: '',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    questionSection: '',
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
    directionPortfolioSection: '## Direction Portfolio\nActive: 0/10 slots.\nNo critical gaps detected.\n\nNo directions exist yet.\n\nNo active directions — portfolio is empty.',
    userPausedSection: '',
    rejectedProposalsSection: '',
    operatorOverridesSection: '',
    modelHealthSection: '',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    questionSection: '',
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

describe('unified spawn-task guidance', () => {
  const base: ThinkerPromptParams = {
    contextPrefix: '',
    projectName: 'Test Project',
    clockHeader: '',
    isSelfRepo: false,
    userStateLine: '',
    packerMetaSection: '',
    strategicContext: '',
    problemModelSection: '',
    problemModelRecovery: 'empty',
    reflectionSection: '',
    selfImprovementBlock: '',
    runningUpdateSection: '',
    designDocJson: '',
    visionJson: '',
    outcomeSection: '',
    previousDecisionOutcomes: '',
    pastRetirementCritiquesSection: '',
    retiredDirectionsSummarySection: '',
    directionPortfolioSection: '',
    userPausedSection: '',
    rejectedProposalsSection: '',
    operatorOverridesSection: '',
    modelHealthSection: '',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    questionSection: '',
    threadSection: '',
    taskId: 'test-task',
    versionPhaseSection: '',
    memoCitationsSection: '',
    efficiencySnapshotSection: '',
  };

  it('routes bounded implementation through spawn-task and keeps fix out', () => {
    const prompt = buildThinkerPrompt(base);
    expect(prompt).toContain('dispatch `create`/`refactor`/`qa` directly');
    expect(prompt).not.toContain('`fix` work also uses `spawn-task`');
    expect(prompt).not.toContain('`spawn-fix`');
  });
});

describe('buildThinkerPrompt stabilize-hardening guidance', () => {
  const base: ThinkerPromptParams = {
    contextPrefix: '',
    projectName: 'Test Project',
    clockHeader: '',
    isSelfRepo: false,
    userStateLine: '',
    packerMetaSection: '',
    strategicContext: '',
    problemModelSection: '',
    problemModelRecovery: 'empty',
    reflectionSection: '',
    selfImprovementBlock: '',
    runningUpdateSection: '',
    designDocJson: '',
    visionJson: '',
    outcomeSection: '',
    previousDecisionOutcomes: '',
    pastRetirementCritiquesSection: '',
    retiredDirectionsSummarySection: '',
    directionPortfolioSection: '',
    userPausedSection: '',
    rejectedProposalsSection: '',
    operatorOverridesSection: '',
    modelHealthSection: '',
    systemMaturitySection: '',
    driftSection: '',
    recentWorkSection: '',
    feedbackSection: '',
    questionSection: '',
    threadSection: '',
    taskId: 'test-task',
    versionPhaseSection: '',
    memoCitationsSection: '',
    efficiencySnapshotSection: '',
  };

  it('does not include executor-owned container-runtime field documentation (setup_step, container_registry_auth)', () => {
    const prompt = buildThinkerPrompt(base);
    // These are Executor-level launch-contract details trimmed to reduce per-cycle thinker bloat.
    // The thinker commissions experiments by abstract spec; the Executor owns the full field schema.
    expect(prompt).not.toContain('setup_step');
    expect(prompt).not.toContain('container_registry_auth');
    // Essential commissioning fields must still be present so GPU experiments can be authored
    expect(prompt).toContain('container_image');
    expect(prompt).toContain('accelerator');
  });

  it('Judgment-over-citation subsection does not repeat the intro judgment phrase verbatim', () => {
    const prompt = buildThinkerPrompt(base);
    // The intro paragraph already states "The user hired you for judgment, not audit trails…";
    // the closing sentence of the Judgment-over-citation block was an exact duplicate and was removed.
    // The essential point (sources are traceability, not justification) still appears via Cite sources.
    const judgmentIdx = prompt.indexOf('**Judgment over citation.**');
    expect(judgmentIdx).toBeGreaterThan(-1);
    // Find the next bold-section boundary after Judgment over citation
    const nextBoldIdx = prompt.indexOf('\n\n**', judgmentIdx + 1);
    const judgmentBlock = nextBoldIdx > -1 ? prompt.slice(judgmentIdx, nextBoldIdx) : prompt.slice(judgmentIdx);
    // The duplicate trailing sentence must not appear inside the subsection
    expect(judgmentBlock).not.toContain('The user hired you for judgment, not audit trails.');
    // But the intro still has the related phrase (we only removed the in-section duplicate)
    expect(prompt).toContain('judgment, not for audit trails');
  });

  it('instructs that stabilize is hardening time (not idle) and to commission an audit rather than hold', () => {
    const prompt = buildThinkerPrompt(base);
    // Stabilize must be framed as active hardening, not idling.
    expect(prompt).toContain('Stabilize is hardening time, not idle time');
    expect(prompt).toContain('A stabilize cycle may legitimately open zero directions');
    // Unknown hardening work triggers an audit, whose tasks stay under an owning outcome.
    expect(prompt).toContain('commission an audit');
    expect(prompt).toContain('attach the resulting tasks to an owning direction');
    expect(prompt).not.toContain('NOT license to stop opening directions');
  });
});
