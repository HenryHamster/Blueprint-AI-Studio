import { mkdir, writeFile } from 'node:fs/promises';
import { dirname } from 'node:path';

const CLASSNAME = 'blueprint.vitest.execution';

function escapeXml(value) {
  return String(value)
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&apos;');
}

function errorType(error) {
  return typeof error === 'object' && error && 'type' in error ? error.type : 'unhandled';
}

/**
 * Vitest's JUnit reporter omits runner-level errors.  Emit just their
 * structured presence as JUnit nodes so the existing Python attributor can
 * fail closed without inspecting the human-readable reporter output.
 *
 * Two hooks are implemented deliberately.  Vitest 2 (the agent suite)
 * dispatches `onFinished`; Vitest 4 (the admin-ui suite, which resolves the
 * hoisted root install) removed it from the reporter interface and dispatches
 * `onTestRunEnd` instead.  Exactly one fires per major, so the writes are
 * idempotent and never downgrade an already-recorded failure.
 */
export default class BlueprintVitestExecutionReporter {
  #emittedErrorCount = -1;

  async #emit(errors) {
    const reportPath = process.env.BLUEPRINT_VITEST_EXECUTION_JUNIT;
    if (!reportPath) {
      throw new Error('BLUEPRINT_VITEST_EXECUTION_JUNIT is required');
    }
    // Both hooks firing must not turn a recorded runner failure back into a
    // clean report; only escalate.
    if (errors.length <= this.#emittedErrorCount) {
      return;
    }
    const cases = [
      `<testcase classname="${CLASSNAME}" name="runner"/>`,
      ...errors.map(
        (error) =>
          `<testcase classname="${CLASSNAME}" name="unhandled_error"><error type="${escapeXml(errorType(error))}"/></testcase>`,
      ),
    ];
    const report = `<?xml version="1.0" encoding="UTF-8"?>\n<testsuites><testsuite name="blueprint-vitest-execution" tests="${cases.length}" errors="${errors.length}" failures="0">${cases.join('')}</testsuite></testsuites>\n`;
    await mkdir(dirname(reportPath), { recursive: true });
    await writeFile(reportPath, report, 'utf8');
    this.#emittedErrorCount = errors.length;
  }

  /** Vitest 2 reporter hook. */
  async onFinished(_files = [], errors = []) {
    await this.#emit(errors ?? []);
  }

  /** Vitest 3+ reporter hook; `onFinished` is not dispatched there. */
  async onTestRunEnd(_testModules = [], unhandledErrors = [], reason = 'passed') {
    const errors = [...(unhandledErrors ?? [])];
    if (reason === 'interrupted') {
      // An interrupted run produced no trustworthy per-test verdicts.  Record
      // it as a runner-level error so the attributor fails closed.
      errors.push({ type: 'interrupted' });
    }
    await this.#emit(errors);
  }
}
