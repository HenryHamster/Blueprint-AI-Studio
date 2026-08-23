#!/usr/bin/env node
/**
 * vitest-test-telemetry.mjs — Generate ranked per-file timing artifacts for
 * the agent/ and admin-ui/ vitest suites.
 *
 * Usage:
 *   node scripts/vitest-test-telemetry.mjs [--suite agent|admin-ui|all] [--flake-runs N]
 *
 * Flags:
 *   --suite <name>     Which suite(s) to run. Default: all (runs both).
 *   --flake-runs <N>   Number of vitest runs per suite (default: 1).
 *                      Set to 2 to enable flake detection: a file is marked
 *                      flaky=true if its pass/fail outcome differs across runs.
 *                      NOTE: flake detection doubles wall-clock time (~10 min total
 *                      for both suites). Use only when you specifically need it.
 *                      The runtime ranking signal is immediately useful without it.
 *
 * Output (committed artifacts):
 *   agent/.test_durations.json    — per-file timings ranked slowest-first for agent/
 *   admin-ui/.test_durations.json — per-file timings ranked slowest-first for admin-ui/
 *
 * Artifact schema:
 *   {
 *     "_meta": {
 *       "suite": "agent" | "admin-ui",
 *       "generated": "<ISO-8601>",
 *       "runs": <number>,
 *       "flake_detection": <boolean>
 *     },
 *     "files": [
 *       {
 *         "file": "<relative-path-from-suite-root>",
 *         "duration_ms": <number>,
 *         "tests": <number>,
 *         "flaky": <boolean>
 *       }
 *     ]
 *   }
 *   Files sorted descending by duration_ms (slowest first = highest cut priority).
 *
 * Requirements:
 *   - Run from the workspace root (the directory containing agent/ and admin-ui/)
 *   - node_modules must be installed:
 *       agent/: cd agent && npm ci
 *       admin-ui/: npm ci   (root; npm workspaces layout)
 *   - No external test infra required — vitest runs standalone
 *
 * Performance notes:
 *   - agent/ suite: several minutes (the committed artifact currently covers 121 files)
 *   - admin-ui/ suite: about a minute (the committed artifact currently covers 250 files)
 *   - Total with --flake-runs 1: ~5-6 min
 *   - Total with --flake-runs 2: ~10-12 min
 *
 * Coverage note:
 *   - Each artifact is seeded from the same on-disk test-file population as its
 *     Vitest config, so the freshness guard can detect missing or stale entries.
 *   - A file in that population with no reporter result is emitted as
 *     `{duration_ms: null, tests: null, measured: false}` and the generator
 *     exits non-zero naming those files. An aborted or partial run therefore
 *     cannot produce a committable artifact: the freshness guard rejects any
 *     unmeasured entry.
 */

import { spawnSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const WORKSPACE_ROOT = path.resolve(__dirname, '..');

// Parse CLI args
const args = process.argv.slice(2);
let suiteArg = 'all';
let flakeRuns = 1;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--suite' && args[i + 1]) suiteArg = args[++i];
  if (args[i] === '--flake-runs' && args[i + 1]) flakeRuns = parseInt(args[++i], 10);
}
if (isNaN(flakeRuns) || flakeRuns < 1) flakeRuns = 1;

const SUITES = {
  agent: {
    // vitest runs from agent/ directory (contains vitest.config.ts)
    cwd: path.join(WORKSPACE_ROOT, 'agent'),
    // relative paths in the artifact are relative to agent/
    relativeBase: path.join(WORKSPACE_ROOT, 'agent'),
    output: path.join(WORKSPACE_ROOT, 'agent', '.test_durations.json'),
    vitestBin: findBin([
      path.join(WORKSPACE_ROOT, 'agent', 'node_modules', '.bin', 'vitest'),
      path.join(WORKSPACE_ROOT, 'node_modules', '.bin', 'vitest'),
    ]),
    extraArgs: [],
    testFileMatcher: relativePath =>
      relativePath.startsWith('src/') && relativePath.endsWith('.test.ts'),
  },
  'admin-ui': {
    // admin-ui uses npm workspaces — vitest binary lives in root node_modules,
    // but the config's include paths are relative to admin-ui/.
    cwd: path.join(WORKSPACE_ROOT, 'admin-ui'),
    // relative paths in the artifact are relative to admin-ui/
    relativeBase: path.join(WORKSPACE_ROOT, 'admin-ui'),
    output: path.join(WORKSPACE_ROOT, 'admin-ui', '.test_durations.json'),
    vitestBin: findBin([
      path.join(WORKSPACE_ROOT, 'node_modules', '.bin', 'vitest'),
      path.join(WORKSPACE_ROOT, 'admin-ui', 'node_modules', '.bin', 'vitest'),
    ]),
    extraArgs: ['--config', path.join(WORKSPACE_ROOT, 'admin-ui', 'vitest.config.ts')],
    testFileMatcher: relativePath => {
      const included =
        (relativePath.startsWith('src/') &&
          (relativePath.endsWith('.test.ts') || relativePath.endsWith('.test.tsx'))) ||
        (relativePath.startsWith('eslint-rules/') &&
          (relativePath.endsWith('.test.js') || relativePath.endsWith('.test.ts')));
      return included &&
        !(relativePath.startsWith('src/') && relativePath.includes('.viewport.test.'));
    },
  },
};

// npm materialises three shims per binary on Windows: an extensionless shell
// script (for Git Bash/WSL), a .cmd and a .ps1. A bare existsSync finds the
// shell script first, but a Windows process cannot execute it -- spawnSync
// fails ENOENT, the generator aborts, and the developer is left with a stale
// artifact that CI then rejects at the promotion PR. Prefer the .cmd there.
function findBin(candidates) {
  const extensions = process.platform === 'win32' ? ['.cmd', '.exe', ''] : [''];
  for (const candidate of candidates) {
    for (const extension of extensions) {
      if (fs.existsSync(candidate + extension)) return candidate + extension;
    }
  }
  return null; // falls back to npx vitest
}

export function collectTestFiles(relativeBase, matcher) {
  const files = [];

  function visit(directory) {
    for (const entry of fs.readdirSync(directory, { withFileTypes: true })) {
      const absolutePath = path.join(directory, entry.name);
      if (entry.isDirectory()) {
        if (entry.name !== 'node_modules') visit(absolutePath);
        continue;
      }
      if (!entry.isFile()) continue;
      const relativePath = path
        .relative(relativeBase, absolutePath)
        .split(path.sep)
        .join('/');
      if (matcher(relativePath)) files.push(relativePath);
    }
  }

  visit(relativeBase);
  return files.sort();
}

function runVitest(suite, suiteConfig, runIndex) {
  const tmpOut = path.join(
    os.tmpdir(),
    `vitest-${suite}-run${runIndex}-${process.pid}.json`,
  );
  const vitestArgs = [
    'run',
    '--reporter=json',
    `--outputFile=${tmpOut}`,
    ...suiteConfig.extraArgs,
  ];

  const [cmd, cmdArgs] = suiteConfig.vitestBin
    ? [suiteConfig.vitestBin, vitestArgs]
    : ['npx', ['vitest', ...vitestArgs]];

  console.log(`[vitest-telemetry] ${suite} run #${runIndex}: ${[cmd, ...cmdArgs].join(' ')}`);

  // Node refuses to execute a .cmd shim without a shell (CVE-2024-27980), so
  // that one case opts in -- and quotes its own arguments, because a shell
  // re-splits them on whitespace.
  const useShell = process.platform === 'win32' && cmd.endsWith('.cmd');
  const spawnArgs = useShell
    ? cmdArgs.map(arg => (/[\s"]/.test(arg) ? `"${arg}"` : arg))
    : cmdArgs;

  const result = spawnSync(cmd, spawnArgs, {
    cwd: suiteConfig.cwd,
    encoding: 'utf-8',
    stdio: ['ignore', 'pipe', 'inherit'],
    env: { ...process.env },
    shell: useShell,
    // 10-minute per-run cap (agent suite takes ~4-5 min; 10 min gives headroom)
    timeout: 10 * 60 * 1000,
  });

  if (result.error) {
    throw new Error(`Failed to spawn vitest for ${suite}: ${result.error.message}`);
  }

  // vitest exits non-zero when tests fail — that's expected, we still get timing data
  if (!fs.existsSync(tmpOut)) {
    const out = (result.stdout ?? '').slice(-500);
    throw new Error(
      `vitest did not write output file for ${suite} (exit ${result.status}).\n${out}`,
    );
  }

  let raw;
  try {
    raw = JSON.parse(fs.readFileSync(tmpOut, 'utf-8'));
  } finally {
    try { fs.unlinkSync(tmpOut); } catch { /* ignore */ }
  }
  return raw;
}

/**
 * Extract per-file stats from a vitest JSON reporter result.
 * Vitest 2.x emits: {testResults: [{name, startTime, endTime, status, assertionResults}]}
 * The `name` field is the absolute file path.
 */
function parseVitestJson(raw, relativeBase) {
  const fileMap = {};
  const testResults = raw.testResults ?? [];

  for (const r of testResults) {
    // vitest 2.x uses `name`; vitest 4.x / jest compat may use `testFilePath`
    const absPath = r.testFilePath || r.name || '';
    const relPath = path.relative(relativeBase, absPath).split(path.sep).join('/');

    const durationMs =
      r.endTime != null && r.startTime != null ? r.endTime - r.startTime : 0;
    const testCount = (r.assertionResults ?? r.tests ?? []).length;
    const passed = (r.status ?? 'passed') !== 'failed';

    if (!fileMap[relPath]) {
      fileMap[relPath] = { duration_ms_sum: 0, tests: 0, passed_runs: 0, total_runs: 0 };
    }
    fileMap[relPath].duration_ms_sum += durationMs;
    fileMap[relPath].tests = Math.max(fileMap[relPath].tests, testCount);
    fileMap[relPath].total_runs += 1;
    if (passed) fileMap[relPath].passed_runs += 1;
  }

  return fileMap;
}

function mergeRuns(accumulated, runMap) {
  for (const [relPath, stats] of Object.entries(runMap)) {
    if (!accumulated[relPath]) {
      accumulated[relPath] = { duration_ms_sum: 0, tests: 0, passed_runs: 0, total_runs: 0 };
    }
    accumulated[relPath].duration_ms_sum += stats.duration_ms_sum;
    accumulated[relPath].tests = Math.max(accumulated[relPath].tests, stats.tests);
    accumulated[relPath].passed_runs += stats.passed_runs;
    accumulated[relPath].total_runs += stats.total_runs;
  }
}

/**
 * Build the artifact's `files` list from accumulated per-file stats plus the
 * on-disk inventory the suite config collects.
 *
 * An inventory file with no reporter result was NOT measured — a run that
 * aborted partway (OOM, crash, interrupt) leaves the rest of the inventory
 * unexecuted. Such entries are emitted explicitly as unmeasured
 * (`duration_ms: null`, `tests: null`, `measured: false`) rather than as a
 * fabricated 0ms/0-test entry, which would be indistinguishable from a real
 * measurement and would silently rank real files as free in the cost queue.
 */
export function buildArtifactFiles(accumulated, inventoryFiles, runs) {
  const measured = Object.entries(accumulated)
    .filter(([file]) => file && !file.startsWith('..'))
    .map(([file, stats]) => ({
      file,
      duration_ms: Math.round(stats.duration_ms_sum / Math.max(stats.total_runs, 1)),
      tests: stats.tests,
      measured: true,
      flaky: runs > 1
        ? stats.passed_runs > 0 && stats.passed_runs < stats.total_runs
        : false,
    }))
    .sort((a, b) => b.duration_ms - a.duration_ms);

  const measuredFiles = new Set(measured.map(f => f.file));
  const unmeasured = inventoryFiles
    .filter(file => file && !file.startsWith('..') && !measuredFiles.has(file))
    .sort()
    .map(file => ({
      file,
      duration_ms: null,
      tests: null,
      measured: false,
      flaky: false,
    }));

  // Unmeasured entries sort last: they carry no cost signal, and a null
  // duration must never masquerade as the cheapest file in the ranking.
  return { files: [...measured, ...unmeasured], unmeasured };
}

async function generateArtifact(suite, suiteConfig) {
  console.log(`\n[vitest-telemetry] Suite: ${suite}`);
  console.log(`  runs: ${flakeRuns}, flake_detection: ${flakeRuns > 1}`);

  const accumulated = {};
  for (let i = 1; i <= flakeRuns; i++) {
    const raw = runVitest(suite, suiteConfig, i);
    const runMap = parseVitestJson(raw, suiteConfig.relativeBase);
    mergeRuns(accumulated, runMap);
  }

  // Keep the committed artifact's population tied to the same on-disk test
  // files that the suite config collects, so the freshness guard can detect
  // both missing and stale entries.
  const inventory = collectTestFiles(suiteConfig.relativeBase, suiteConfig.testFileMatcher);
  const { files, unmeasured } = buildArtifactFiles(accumulated, inventory, flakeRuns);

  const artifact = {
    _meta: {
      suite,
      generated: new Date().toISOString(),
      runs: flakeRuns,
      flake_detection: flakeRuns > 1,
    },
    files,
  };

  fs.writeFileSync(suiteConfig.output, JSON.stringify(artifact, null, 2) + '\n');
  console.log(
    `[vitest-telemetry] Wrote ${files.length} file entries → ${suiteConfig.output}`,
  );
  const ranked = files.filter(f => f.measured);
  if (ranked.length > 0) {
    const top5 = ranked.slice(0, 5);
    console.log('  Top 5 slowest files (cut candidates):');
    top5.forEach((f, i) =>
      console.log(`    ${i + 1}. ${f.file} (${f.duration_ms}ms, ${f.tests} tests)`),
    );
    if (flakeRuns > 1) {
      const flaky = ranked.filter(f => f.flaky);
      console.log(`  Flaky files detected: ${flaky.length}`);
      flaky.slice(0, 5).forEach(f => console.log(`    - ${f.file}`));
    }
  }

  if (unmeasured.length > 0) {
    console.error(
      `[vitest-telemetry] INCOMPLETE run for ${suite}: ${unmeasured.length} of ` +
        `${files.length} test file(s) produced no reporter result and are recorded ` +
        'as unmeasured. The artifact is NOT publishable — rerun the suite until ' +
        'every file is measured.',
    );
    unmeasured.slice(0, 20).forEach(f => console.error(`    - ${f.file}`));
    if (unmeasured.length > 20) {
      console.error(`    ... and ${unmeasured.length - 20} more`);
    }
  }
  return unmeasured;
}

async function main() {
  const suitesToRun =
    suiteArg === 'all'
      ? Object.keys(SUITES)
      : suiteArg.split(',').map(s => s.trim());

  let incomplete = 0;
  for (const suite of suitesToRun) {
    if (!SUITES[suite]) {
      console.error(`Unknown suite: ${suite}. Valid: ${Object.keys(SUITES).join(', ')}`);
      process.exit(1);
    }
    incomplete += (await generateArtifact(suite, SUITES[suite])).length;
  }

  if (incomplete > 0) {
    console.error(
      `\n[vitest-telemetry] FAILED: ${incomplete} test file(s) were never executed. ` +
        'Do not commit the artifacts produced by this run.',
    );
    process.exit(1);
  }

  console.log(
    '\n[vitest-telemetry] Done. Commit the updated .test_durations.json files to the repo.',
  );
}

// Run only when invoked directly; importing the module (e.g. from tests) must
// not spawn vitest. Both sides are realpath-resolved so a symlinked invocation
// still matches.
function realpathOrSelf(candidate) {
  try {
    return fs.realpathSync(candidate);
  } catch {
    return candidate;
  }
}

const invokedPath = process.argv[1] ? realpathOrSelf(path.resolve(process.argv[1])) : '';
if (invokedPath === realpathOrSelf(fileURLToPath(import.meta.url))) {
  main().catch(err => {
    console.error('[vitest-telemetry] Fatal:', err.message);
    process.exit(1);
  });
}
