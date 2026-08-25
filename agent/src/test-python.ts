// Shared test-only helper for suites that shell out to a CPython 3 interpreter.
//
// Why this exists: Linux/macOS CI always exposes CPython 3 as `python3`, and the
// agent's PRODUCTION code hardcodes `python3` because agents only ever run on
// Linux. A Windows developer host is different in two ways that make a bare
// `python3` unreachable there:
//
//   1. The Windows CPython installer ships `python.exe` / `py.exe` — never a
//      `python3.exe`.
//   2. Node's `child_process.execFileSync`/`spawnSync` (without `shell: true`)
//      resolve an executable by exact filename, NOT through `PATHEXT`, so even a
//      hand-rolled `python3.cmd` / `python3.bat` shim on PATH stays invisible.
//
// The result on a stock Windows dev host is a wave of `spawnSync python3 ENOENT`
// failures that say nothing about the code under test. Two different questions
// need two different answers, so this module exports both:
//
//   TEST_PYTHON            — "give me an interpreter I can run a script with".
//                            Prefers `python3`; on win32 falls back to `python`.
//                            Suites that merely USE Python as a tool should run
//                            this way so Windows keeps full coverage.
//
//   HAS_PYTHON3_EXECUTABLE — "is a literal `python3` on PATH?". This is the
//                            right question when the SUT itself spawns
//                            `python3` (e.g. compareStructuredFailures), because
//                            no test-side interpreter choice can change what the
//                            production code execs.
//
// Both probes stay fail-loud on Linux: `TEST_PYTHON` is non-null on any CI box,
// and callers that gate on `HAS_PYTHON3_EXECUTABLE` must combine it with a
// win32 check so a python3 that goes missing in Linux CI still reds the build
// instead of silently skipping.
import { createRequire } from 'node:module';

// child_process is resolved through createRequire, NOT a static import, on
// purpose. Several suites in this package call vi.mock('child_process', ...) at
// module scope; a mocked spawnSync reaching this probe would make it report "no
// interpreter" on a host that plainly has one, and the skip conditions below
// would then silently disable coverage for the wrong reason. createRequire hands
// back the genuine Node builtin, outside the test module graph.
const { spawnSync } = createRequire(import.meta.url)('child_process') as typeof import('node:child_process');

/** True when `bin` runs and reports itself as CPython 3.x. */
function isPython3(bin: string): boolean {
  try {
    const probe = spawnSync(
      bin,
      ['-c', 'import sys; sys.exit(0 if sys.version_info[0] == 3 else 1)'],
      { stdio: 'ignore' },
    );
    return !probe.error && probe.status === 0;
  } catch {
    return false;
  }
}

/**
 * A runnable CPython 3 interpreter name, or null when the host has none.
 *
 * `python3` is tried first on every platform so Linux CI behaviour is
 * bit-identical to a bare `python3` call. The `python` fallback is win32-only:
 * on Linux a bare `python` may still be CPython 2 on older images, and silently
 * preferring it would change what CI actually exercises.
 */
export const TEST_PYTHON: string | null = (() => {
  if (isPython3('python3')) return 'python3';
  if (process.platform === 'win32' && isPython3('python')) return 'python';
  return null;
})();

/** True when a literal `python3` executable is spawnable on this host. */
export const HAS_PYTHON3_EXECUTABLE: boolean = TEST_PYTHON === 'python3';

/**
 * Skip condition for suites whose PRODUCTION code spawns `python3` directly.
 *
 * Deliberately narrow: it is true ONLY on Windows AND only when no `python3`
 * exists. On Linux CI it is always false, so a vanished `python3` still fails
 * loudly there rather than quietly skipping the coverage.
 */
export const SKIP_NEEDS_PYTHON3_BINARY: boolean =
  process.platform === 'win32' && !HAS_PYTHON3_EXECUTABLE;

/**
 * Return a usable interpreter or throw with actionable guidance.
 *
 * For suites that shell out to Python themselves and must never silently lose
 * coverage. On Linux CI this throws only if the image genuinely lost python3.
 */
export function requireTestPython(context: string): string {
  if (TEST_PYTHON) return TEST_PYTHON;
  throw new Error(
    `A CPython 3 interpreter is required for ${context} but none was found on PATH.\n` +
    'Tried "python3" (all platforms) and "python" (Windows only).\n' +
    'Install Python 3 — Linux CI always has it. A missing interpreter silently drops critical coverage.',
  );
}

/**
 * False on Windows: a sandbox built out of `#!/bin/sh` shim scripts cannot work.
 *
 * Several suites here build a fake toolchain — `git`, `python3`, `npm`,
 * `pg_config`, `docker` — as extensionless `#!/bin/sh` files in a temp `bin/`
 * placed first on PATH, then run a generated bash script against it. bash itself
 * resolves and runs those shims perfectly well on a Windows dev host, so the
 * harness is not the problem. The break is one layer deeper.
 *
 * When the script under test invokes a Python guard (e.g. scripts/
 * static_release_guard.py, scripts/backend_verification_guard.py) and that guard
 * does `subprocess.run(["git", ...])`, a native Windows CPython goes through
 * CreateProcess — which resolves an executable by exact filename plus ".exe" and
 * NEVER consults PATHEXT. The extensionless `bin/git` shim is therefore invisible
 * to it, and so is a `git.bat`/`git.cmd` twin (verified on this host, not
 * assumed). Resolution falls through to the real `git.exe`, which runs against a
 * sandbox temp dir that is not a git repository, and the guard dies with an error
 * about an unresolvable base ref that has nothing to do with the code under test.
 *
 * This is the same platform fact as the `python3` problem documented above:
 * shim SCRIPTS are invisible to any non-shell process spawn on Windows, whether
 * the spawner is Node's execFileSync or Python's subprocess.run. Nothing on the
 * test side can intercept a spawn made by production Python, and the code under
 * test in these suites is a bash helper that only ever runs on Linux agent
 * runners, so there is no Windows behaviour being given up.
 *
 * Gate BOTH the pass-expecting and the fail-expecting cases on this. On Windows
 * a `not.toBe(0)` assertion still passes — but only because the sandbox died for
 * an unrelated reason, which is a vacuous green that would mask a real
 * regression. Every POSIX platform (Linux runners, Linux/macOS CI) runs them all.
 */
export const POSIX_SHIM_SANDBOX_SUPPORTED: boolean = process.platform !== 'win32';
