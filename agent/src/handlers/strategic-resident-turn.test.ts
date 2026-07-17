import fs from 'fs';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  PROVIDER: 'anthropic',
  apiGet: vi.fn(),
  apiPost: vi.fn(),
  runClaudeCode: vi.fn(),
  sendLog: vi.fn(),
}));

vi.mock('../index.js', () => mocks);

import {
  handleStrategicResidentTurn,
  residentProviderSupportsNoTools,
} from './strategic-resident-turn.js';

const providerInput = {
  identity: {
    project_id: '11111111-1111-1111-1111-111111111111',
    authority_generation: 7,
  },
  lineage: {
    checkpoint_id: '22222222-2222-2222-2222-222222222222',
    checkpoint_version: 3,
    checkpoint_hash: 'a'.repeat(64),
    context_hash: 'b'.repeat(64),
  },
};

describe('strategic Resident turn handler', () => {
  it('fails closed for provider CLIs that cannot enforce the tool denylist', () => {
    expect(residentProviderSupportsNoTools('openai')).toBe(false);
    expect(residentProviderSupportsNoTools('gemini')).toBe(false);
    expect(residentProviderSupportsNoTools('anthropic')).toBe(true);
  });

  beforeEach(() => {
    vi.clearAllMocks();
    mocks.apiGet.mockResolvedValue({
      provider_input: providerInput,
      turn_task_version: 9,
    });
  });

  it('disables all tools and posts exact reconciliation lineage', async () => {
    mocks.runClaudeCode.mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({
        reply: 'Done',
        facts: [],
        evidence: [],
        operations: [],
        receipts: [],
        unknown_effects: [],
        strategic_implications: [],
        proposed_memory: [],
        follow_up: null,
        required_attention: [],
        reconciliation: {
          accepted_memory_proposal_ids: [],
          accepted_implication_ids: [],
          accepted_open_loop_ids: [],
          resolved_open_loop_ids: [],
          evidence_inspections: [],
        },
      }),
    });
    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );
    const tools = mocks.runClaudeCode.mock.calls[0][5] as string[];
    expect(tools).toEqual(['*', 'Task', 'Agent', 'Workflow', 'Skill']);
    const options = mocks.runClaudeCode.mock.calls[0][7] as unknown as {
      processBoundary: {
        cwd: string;
        allowedEnvKeys: string[];
        envOverrides: Record<string, string>;
        skipAmbientHelpers: boolean;
        enforceNoTools: boolean;
      };
    };
    expect(options.processBoundary.skipAmbientHelpers).toBe(true);
    expect(options.processBoundary.enforceNoTools).toBe(true);
    expect(options.processBoundary.cwd).not.toContain('/workspace');
    expect(options.processBoundary.allowedEnvKeys).not.toContain('ORCHESTRATOR_URL');
    expect(options.processBoundary.allowedEnvKeys).not.toContain('AGENT_API_TOKEN');
    expect(options.processBoundary.allowedEnvKeys).not.toContain('GITHUB_TOKEN');
    expect(options.processBoundary.envOverrides.HOME).not.toBe(process.env.HOME);
    expect(fs.existsSync(options.processBoundary.cwd)).toBe(false);
    expect(mocks.apiPost).toHaveBeenCalledOnce();
    const [completePath, completeBodyRaw] = mocks.apiPost.mock.calls[0] as unknown[];
    const completeBody = completeBodyRaw as Record<string, unknown>;
    expect(completePath).toBe('/api/agent/resident-turn/complete');
    expect(completeBody).toMatchObject({
      task_id: 'task-id',
      project_id: providerInput.identity.project_id,
      authority_generation: 7,
      expected_turn_task_version: 9,
      expected_checkpoint_id: providerInput.lineage.checkpoint_id,
      expected_checkpoint_hash: providerInput.lineage.checkpoint_hash,
      context_hash: providerInput.lineage.context_hash,
    });
    expect(completeBody.result).not.toHaveProperty('reconciliation');
  });

  it('fails closed on malformed provider output', async () => {
    mocks.runClaudeCode.mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: '{"reply":"narrative only"}',
    });
    await expect(
      handleStrategicResidentTurn(
        'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
      ),
    ).rejects.toThrow('exact structured result');
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('fails closed before context retrieval when the server provider fence is absent', async () => {
    await expect(
      handleStrategicResidentTurn(
        'task-id', {}, {}, '', '', '', null, { value: '' },
      ),
    ).rejects.toThrow('expected missing');
    expect(mocks.apiGet).not.toHaveBeenCalled();
    expect(mocks.runClaudeCode).not.toHaveBeenCalled();
  });

  it('fails closed before model invocation on a provider mismatch', async () => {
    await expect(
      handleStrategicResidentTurn(
        'task-id', { _resident_expected_runtime_provider: 'zai' }, {}, '', '', '', null, { value: '' },
      ),
    ).rejects.toThrow('expected zai, got anthropic');
    expect(mocks.apiGet).not.toHaveBeenCalled();
    expect(mocks.runClaudeCode).not.toHaveBeenCalled();
  });
});
