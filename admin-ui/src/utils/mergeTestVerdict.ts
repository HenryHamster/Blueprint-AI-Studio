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

// One newly-introduced test-skip / disable marker surfaced by the agent's
// diff-aware probe (mirrors DisabledTestMarker in test-runners.ts).
export interface DisabledTestMarker {
  file: string;
  marker: string;
  line: string;
}

// VISIBILITY-ONLY: the count + bounded list of test-skip/disable markers the
// PR's OWN diff newly introduced. Present only when the agent found at least one
// newly-added skip; never affects the merge outcome.
export interface DisabledTestsBlock {
  count: number;
  markers: DisabledTestMarker[];
}

// Best-effort coverage summary attached when a detected suite RAN: how many of
// its tests actually executed vs. were skipped/deselected/ignored/filtered.
// Purely informational — never changes the merge outcome.
export interface CoverageBlock {
  passed: number;
  skipped: number;
  total: number;
}

// A passed suite is "mostly skipped" when at least this fraction of its tests
// were skipped (and the total is non-zero). Visibility threshold only — it does
// NOT block or alter the merge outcome.
export const MOSTLY_SKIPPED_RATIO = 0.8;

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
  coverage: CoverageBlock | null;
  // VISIBILITY-ONLY signal: the PR's own diff newly disabled N test(s). null
  // when the agent emitted no (or a zero-count / malformed) block — older
  // artifacts render exactly as before.
  disabledTests: DisabledTestsBlock | null;
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
  | 'suite_passed_mostly_skipped'
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

function isFiniteNumber(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value);
}

// Parse the optional best-effort coverage block. Returns null unless ALL three
// counts are finite numbers — a missing/partial/malformed block is treated as
// absent so older artifacts and garbage values render exactly as before.
function asCoverage(value: unknown): CoverageBlock | null {
  if (!value || typeof value !== 'object') return null;
  const c = value as Record<string, unknown>;
  if (!isFiniteNumber(c.passed) || !isFiniteNumber(c.skipped) || !isFiniteNumber(c.total)) {
    return null;
  }
  return { passed: c.passed, skipped: c.skipped, total: c.total };
}

// Parse the optional newly-disabled-tests block. Returns null unless count is a
// finite, positive number — a missing / zero-count / malformed block is treated
// as absent so older artifacts render exactly as before. Markers coerce to
// safe strings; a non-array markers field yields an empty list.
function asDisabledTests(value: unknown): DisabledTestsBlock | null {
  if (!value || typeof value !== 'object') return null;
  const d = value as Record<string, unknown>;
  if (!isFiniteNumber(d.count) || d.count <= 0) return null;
  const markers = Array.isArray(d.markers)
    ? d.markers
        .filter((m): m is Record<string, unknown> => Boolean(m) && typeof m === 'object')
        .map(m => ({ file: asString(m.file), marker: asString(m.marker), line: asString(m.line) }))
    : [];
  return { count: d.count, markers };
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
    coverage: asCoverage(v.coverage),
    disabledTests: asDisabledTests(v.disabledTests),
  };
}

/**
 * Whether a passed suite's coverage indicates that MOST of its tests were
 * skipped (skip ratio >= MOSTLY_SKIPPED_RATIO, total > 0). Purely a visibility
 * signal — never blocks or alters the merge outcome.
 */
export function coverageIsMostlySkipped(coverage: CoverageBlock | null): boolean {
  if (!coverage) return false;
  if (coverage.total <= 0) return false;
  return coverage.skipped / coverage.total >= MOSTLY_SKIPPED_RATIO;
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
    // Visibility only: a passing suite whose tests were mostly skipped stays a
    // PASS, but the reason distinguishes it so the panel can flag it.
    if (coverageIsMostlySkipped(verdict.coverage)) {
      return { outcome: 'pass', reason: 'suite_passed_mostly_skipped', detail: '' };
    }
    return { outcome: 'pass', reason: 'suite_passed', detail: '' };
  }
  return {
    outcome: 'fail',
    reason: 'suite_failed',
    detail: verdict.output.trim() || 'Detected test suite failed (no output captured).',
  };
}
