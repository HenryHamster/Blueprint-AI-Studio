import fs from 'fs';
import path from 'path';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';

// This file deliberately leaves test-fix-loop unmocked. It exercises the same
// base checkout → report capture → restore-head → current report path that the
// merge gate uses, rather than reimplementing its attribution result in a mock.
vi.mock('../index.js', async (importOriginal) => {
  // Keep all exports live so the integration test executes production
  // testFixLoop; individual external-IO exports are spied below.
  return importOriginal<typeof import('../index.js')>();
});

vi.mock('../workspacePrep.js', async (importOriginal) => ({
  ...await importOriginal<typeof import('../workspacePrep.js')>(),
  prepareWorkspaceForRole: vi.fn().mockResolvedValue(undefined),
}));

vi.mock('../build-config.js', async (importOriginal) => ({
  ...await importOriginal<typeof import('../build-config.js')>(),
  resolveBuildConfig: vi.fn(() => ({
    directories: [{ path: 'agent', type: 'node', test_cmd: 'npm test' }],
  })),
}));

vi.mock('../utils/test-runners.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../utils/test-runners.js')>();
  return {
    ...actual,
    detectAcceptanceProbes: vi.fn(() => []),
    discoverDocumentedCommands: vi.fn(() => []),
    discoverNewlyDisabledTests: vi.fn(() => []),
  };
});

vi.mock('child_process', async (importOriginal) => {
  const actual = await importOriginal<typeof import('child_process')>();
  return { ...actual, execSync: vi.fn(() => '') };
});

import { handleVerifyTests } from './verify-tests.js';
import * as index from '../index.js';

const WORKSPACE = process.env.AGENT_WORKSPACE as string;
const TEST_PATH = 'agent/src/handlers/verify-tests.test.ts';
const TESTCASE = 'same named test';
const baseDetail = 'expected 1 to equal 2';
const currentDetail = 'expected 1 to equal 3';

function vitestReport(detail: string): string {
  return JSON.stringify({
    testResults: [{
      name: path.join(WORKSPACE, TEST_PATH),
      assertionResults: [{
        fullName: TESTCASE,
        status: 'failed',
        failureMessages: [detail],
      }],
    }],
  });
}

describe('handleVerifyTests structured report integration', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(index, 'sendHeartbeatSync').mockImplementation(() => {});
    vi.spyOn(index, 'persistAndPostStatus').mockResolvedValue(undefined);
    fs.mkdirSync(path.join(WORKSPACE, 'agent', 'node_modules'), { recursive: true });
    fs.writeFileSync(path.join(WORKSPACE, 'agent', 'vitest.config.ts'), 'export default {};\n');

    let checkout = 'head';
    vi.spyOn(index, 'execFileAsync').mockImplementation((cmd, args) => {
      if (cmd === 'git') {
        if (args[0] === 'rev-parse') return Promise.resolve(args.includes('HEAD') ? 'head-sha' : 'base-sha');
        if (args[0] === 'checkout') {
          checkout = args.at(-1) === 'origin/main' ? 'base' : 'head';
          return Promise.resolve('');
        }
        if (args[0] === 'diff' && args.includes('--name-only')) return Promise.resolve(`${TEST_PATH}\n`);
        if (args[0] === 'diff' && args.includes('--unified=0')) return Promise.resolve('');
        return Promise.resolve('');
      }
      if (cmd === 'npm') {
        const outputFile = args.find(arg => arg.startsWith('--outputFile='));
        if (!outputFile) return Promise.reject(new Error(`expected Vitest structured report arguments, got ${args.join(' ')}`));
        fs.writeFileSync(outputFile.slice('--outputFile='.length), vitestReport(checkout === 'base' ? baseDetail : currentDetail));
        return Promise.reject(new Error(`FAIL ${TEST_PATH}\n1 failed`));
      }
      return Promise.reject(new Error(`unexpected command: ${cmd}`));
    });
  });

  afterEach(() => {
    fs.rmSync(path.join(WORKSPACE, 'agent'), { recursive: true, force: true });
  });

  it('test_same_named_test_failing_differently_is_not_scored_pre_existing', async () => {
    await handleVerifyTests(
      'changed-detail-integration',
      { branch: 'feature/x', base_branch: 'main' },
      {}, 'tok', 'owner', 'repo', null, { value: '' },
    );

    const npmCalls = vi.mocked(index.execFileAsync).mock.calls.filter(([cmd]) => cmd === 'npm');
    expect(npmCalls).toHaveLength(2);
    expect(npmCalls.every(([, args]) => args.includes('--reporter=json'))).toBe(true);
    expect(npmCalls.every(([, args]) => args.some(arg => arg.startsWith('--outputFile=')))).toBe(true);

    const posted = vi.mocked(index.persistAndPostStatus).mock.calls[0][1] as {
      artifact_type: string;
      status: string;
      logs: string;
    };
    const result = JSON.parse(posted.logs) as { detected: boolean; passed: boolean };
    expect(posted.artifact_type).toBe('MERGE_TEST_RESULT');
    expect(posted.status).toBe('COMPLETED');
    expect(result.detected).toBe(true);
    expect(result.passed).toBe(false);
  });
});
