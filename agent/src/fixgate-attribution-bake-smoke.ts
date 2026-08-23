/**
 * Verify the structured failure comparator from the path used by baked agents.
 *
 * Packer invokes this from /opt/agent after copying agent/src. It guards both
 * the packaged path and a real route/testcase/normalized-detail comparison.
 */
import { execFileSync } from 'child_process';
import fs from 'fs';
import path from 'path';

const comparatorPath = path.join(
  '/opt/agent/src',
  '_agent_scripts',
  'fixgate_attribution.py',
);

function main(): void {
  if (!fs.statSync(comparatorPath).isFile()) {
    throw new Error(`Packaged fixgate comparator is missing: ${comparatorPath}`);
  }

  const record = {
    route: 'backend/testing/test_fixgate.py',
    testcase: 'test_baked_agent_comparator',
    kind: 'failure',
    // Task counters are deliberately normalized by the canonical comparator.
    detail: 'AssertionError: task Task-123 failed',
  };
  const output = execFileSync('python3', [comparatorPath], {
    encoding: 'utf-8',
    input: JSON.stringify({
      current: [record],
      baseline: [{ ...record, detail: 'AssertionError: task Task-456 failed' }],
    }),
    timeout: 30_000,
  });
  const attribution = JSON.parse(output) as {
    blocking?: unknown;
    new_failures?: unknown;
    suppressed_failures?: unknown;
  };
  if (
    attribution.blocking !== false ||
    !isEmptyRecord(attribution.new_failures) ||
    !hasOneRecord(attribution.suppressed_failures)
  ) {
    throw new Error(`Unexpected baked comparator result: ${output}`);
  }
  console.log('PACKER_FIXGATE_ATTRIBUTION_PATH_OK');
}

function isEmptyRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) && Object.keys(value).length === 0;
}

function hasOneRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value) && Object.keys(value).length === 1;
}

main();
