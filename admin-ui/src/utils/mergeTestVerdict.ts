// SINGLE SOURCE OF TRUTH (frontend) for parsing a no-CI merge-gate
// MERGE_TEST_RESULT artifact and deriving the top-line gate verdict the
// operator sees in the Merge Test Gate panel (TaskDetailDrawerMeta).
//
// The verdict precedence here MIRRORS the backend gate's actual merge decision
// (`classify_merge_test_verdict` in backend/app/ci_resolution.py). Keeping it
// in one pure, unit-testable module means the panel can never display a clean
// "Passed" / "No suite detected" when the gate actually BLOCKED on a
// documented-command, partial-setup, or acceptance-probe failure.
//
// The artifact shape is owned by the agent (VerifyTestsResult in
// agent/src/handlers/verify-tests.ts). This module only READS it — it adds no
// fields and changes no backend/agent behavior.

// The cheap goal-level acceptance probe ("does the changed code still
// compile/build?"), attached only on a detected=false fall-open.
export interface ProbeBlock {
  ran?: boolean;
  ecosystem?: string;
  outcome?: 'pass' | 'fail' | 'could_not_run';
  passed?: boolean;
  could_not_run?: boolean;
  output?: string;
  label?: string;
  // True when the probe ran the project's OWN declared build command rather
  // than a generic ecosystem compile check.
  declared?: boolean;
  // Set only for a PASSED declared build: the build exited 0 but wrote no new
  // output (a possible no-op build).
  wrote_no_artifact?: boolean;
}

// The diff-aware probe of commands the PR INTRODUCES or DOCUMENTS, run
// regardless of unit-suite detection.
export interface DocumentedCommandsBlock {
  ran?: boolean;
  outcome?: 'pass' | 'fail' | 'could_not_run';
  passed?: boolean;
  could_not_run?: boolean;
  output?: string;
  label?: string;
}

export interface RecoveredDirectory {
  path: string;
  type: string;
  test_cmd: string;
}

// Parsed, typed view of the MERGE_TEST_RESULT artifact JSON.
export interface MergeTestVerdict {
  detected?: boolean;
  passed?: boolean;
  output: string;
  probe: ProbeBlock | null;
  documentedCommands: DocumentedCommandsBlock | null;
  partialSetupFailed: boolean;
  setupFailed: boolean;
  recovered: boolean;
  recovered_directories: RecoveredDirectory[];
  noTestsReason: 'all_skipped' | 'no_real_tests' | 'no_suite' | null;
}

// The top-line panel verdict. `blocked` distinguishes a gate BLOCK that came
// from a documented-command / partial-setup / acceptance-probe failure (not the
// classic detected-suite `fail`) so the panel can frame it accurately.
export type MergeTestOutcome = 'pass' | 'fail' | 'blocked' | 'undetected';

// Which precedence rule produced the outcome — drives panel wording.
export type MergeTestReason =
  | 'documented_command_failed'
  | 'partial_setup_failed'
  | 'declared_build_failed'
  | 'probe_failed'
  | 'suite_failed'
  | 'suite_passed'
  | 'undetected';

export interface MergeTestResult {
  outcome: MergeTestOutcome;
  reason: MergeTestReason;
  detail: string;
}

function asString(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

function asProbe(value: unknown): ProbeBlock | null {
  return value && typeof value === 'object' ? (value as ProbeBlock) : null;
}

function asDocumented(value: unknown): DocumentedCommandsBlock | null {
  return value && typeof value === 'object' ? (value as DocumentedCommandsBlock) : null;
}

function asRecoveredDirectories(value: unknown): RecoveredDirectory[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((d): d is Record<string, unknown> => Boolean(d) && typeof d === 'object')
    .map(d => ({
      path: asString(d.path),
      type: asString(d.type),
      test_cmd: asString(d.test_cmd),
    }));
}

const NO_TESTS_REASONS = new Set(['all_skipped', 'no_real_tests', 'no_suite']);

/**
 * Parse a JSON-decoded MERGE_TEST_RESULT artifact body into the typed verdict
 * shape. Returns null for a non-object (so an old/empty/garbage artifact is
 * treated as "no verdict"). Tolerant of missing fields — an older agent that
 * emits only {detected, passed, output} parses cleanly with empty extras.
 */
export function parseMergeTestVerdict(parsed: unknown): MergeTestVerdict | null {
  if (!parsed || typeof parsed !== 'object') return null;
  const v = parsed as Record<string, unknown>;
  const noTestsReason = asString(v.noTestsReason);
  return {
    detected: typeof v.detected === 'boolean' ? v.detected : undefined,
    passed: typeof v.passed === 'boolean' ? v.passed : undefined,
    output: asString(v.output),
    probe: asProbe(v.probe),
    documentedCommands: asDocumented(v.documentedCommands),
    partialSetupFailed: v.partialSetupFailed === true,
    setupFailed: v.setupFailed === true,
    recovered: v.recovered === true,
    recovered_directories: asRecoveredDirectories(v.recovered_directories),
    noTestsReason: NO_TESTS_REASONS.has(noTestsReason)
      ? (noTestsReason as MergeTestVerdict['noTestsReason'])
      : null,
  };
}

/**
 * Whether an acceptance `probe` block is a CLEAR, blocking failure: it ran, was
 * not a could-not-run degrade, and did not pass. Mirrors `probe_is_clear_failure`
 * in ci_resolution.py.
 */
export function probeIsClearFailure(probe: ProbeBlock | null): boolean {
  if (!probe) return false;
  if (!probe.ran) return false;
  if (probe.could_not_run) return false;
  return probe.passed === false;
}

/**
 * Whether a `documentedCommands` block is a CLEAR, blocking failure. Mirrors
 * `_documented_command_is_clear_failure` in ci_resolution.py.
 */
export function documentedCommandIsClearFailure(
  documented: DocumentedCommandsBlock | null,
): boolean {
  if (!documented) return false;
  if (!documented.ran) return false;
  if (documented.could_not_run) return false;
  return documented.passed === false;
}

/**
 * Derive the panel's top-line verdict from a parsed merge-test verdict, mirroring
 * the precedence in `classify_merge_test_verdict` (backend/app/ci_resolution.py)
 * so the panel agrees with the gate's actual merge decision:
 *
 *   1. A CLEAR documented-command failure BLOCKS — higher precedence than the
 *      unit suite, even when detected && passed.
 *   2. `partialSetupFailed` BLOCKS.
 *   3. When detected===false, a CLEAR probe failure BLOCKS (declared build vs
 *      generic probe framing); otherwise this is the undetected fall-open.
 *   4. Otherwise passed => Passed; else => detected-suite Failed.
 */
export function deriveMergeTestOutcome(verdict: MergeTestVerdict): MergeTestResult {
  // 1. HIGH precedence: a documented / PR-introduced command that CLEARLY failed.
  if (documentedCommandIsClearFailure(verdict.documentedCommands)) {
    const doc = verdict.documentedCommands as DocumentedCommandsBlock;
    const label = asString(doc.label).trim();
    const output = asString(doc.output).trim();
    const prefix = label
      ? `PR-documented command \`${label}\` failed: `
      : 'PR-documented command failed: ';
    return {
      outcome: 'blocked',
      reason: 'documented_command_failed',
      detail: output ? prefix + output : prefix + 'the documented command did not succeed.',
    };
  }
  // 2. Partial setup failure: a suite never executed; never a clean pass / fall-open.
  if (verdict.partialSetupFailed) {
    return {
      outcome: 'blocked',
      reason: 'partial_setup_failed',
      detail:
        verdict.output.trim() ||
        'Partial setup failure: one or more test runners could not run their suite (environment/setup error); the suite is only partially verified.',
    };
  }
  // 3. No unit suite detected.
  if (verdict.detected === false) {
    if (probeIsClearFailure(verdict.probe)) {
      const probe = verdict.probe as ProbeBlock;
      const output = asString(probe.output).trim();
      if (probe.declared) {
        const label = asString(probe.label).trim();
        const prefix = label
          ? `Declared build command \`${label}\` failed: `
          : 'Declared build command failed: ';
        return {
          outcome: 'blocked',
          reason: 'declared_build_failed',
          detail: output ? prefix + output : prefix + 'the declared build did not succeed.',
        };
      }
      return {
        outcome: 'blocked',
        reason: 'probe_failed',
        detail: output || 'Acceptance probe failed: the changed code does not compile/build.',
      };
    }
    return { outcome: 'undetected', reason: 'undetected', detail: '' };
  }
  // 4. A suite was detected.
  if (verdict.passed) {
    return { outcome: 'pass', reason: 'suite_passed', detail: '' };
  }
  return {
    outcome: 'fail',
    reason: 'suite_failed',
    detail: verdict.output.trim() || 'Detected test suite failed (no output captured).',
  };
}
