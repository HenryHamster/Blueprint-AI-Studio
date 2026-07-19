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
  capabilities: {
    catalog: [
      { id: 'task.inspect', mode: 'inspect', availability: 'invoke' },
      { id: 'task.reprioritize', mode: 'mutate', availability: 'invoke' },
      { id: 'task.cancel', mode: 'mutate', availability: 'propose_only' },
    ],
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

  it('runs a bounded server capability inspection before the final result', async () => {
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: '33333333-3333-3333-3333-333333333333' },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          reply: 'Inspected', facts: [], evidence: [], operations: [], receipts: [],
          unknown_effects: [], strategic_implications: [], proposed_memory: [],
          follow_up: null, required_attention: [],
          reconciliation: {
            accepted_memory_proposal_ids: [], accepted_implication_ids: [],
            accepted_open_loop_ids: [], resolved_open_loop_ids: [], evidence_inspections: [],
          },
        }),
      });
    mocks.apiPost.mockImplementation((path: string) => {
      if (path === '/api/agent/resident-turn/capability') {
        return Promise.resolve({ capability_id: 'task.inspect', evidence: { checksum: 'a'.repeat(64) } });
      }
      return Promise.resolve({});
    });

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(mocks.runClaudeCode).toHaveBeenCalledTimes(2);
    expect(mocks.apiPost.mock.calls[0]).toEqual([
      '/api/agent/resident-turn/capability',
      {
        capability_id: 'task.inspect',
        parameters: { task_id: '33333333-3333-3333-3333-333333333333' },
      },
    ]);
    expect(String(mocks.runClaudeCode.mock.calls[1][0])).toContain(
      'Server-validated capability evidence',
    );
    expect(mocks.apiPost.mock.calls[1][0]).toBe('/api/agent/resident-turn/complete');
  });

  it('posts a mutation with server-owned authority revisions and returns its receipt to the provider', async () => {
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          mutation_requests: [{
            capability_id: 'task.reprioritize',
            parameters: {
              task_id: '33333333-3333-3333-3333-333333333333',
              priority: 50,
            },
            operation_id: 'reprioritize:33333333',
            expected_revisions: {
              target_task_updated_at: '2026-07-18T00:00:00Z',
            },
            evidence: [{
              kind: 'task',
              project_id: providerInput.identity.project_id,
              source_id: '33333333-3333-3333-3333-333333333333',
              revision: 'task:v1',
              checksum: 'c'.repeat(64),
              inspected_at: '2026-07-18T00:00:00Z',
            }],
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          reply: 'Reprioritized', facts: [], evidence: [], operations: [], receipts: [],
          unknown_effects: [], strategic_implications: [], proposed_memory: [],
          follow_up: null, required_attention: [],
          reconciliation: {
            accepted_memory_proposal_ids: [], accepted_implication_ids: [],
            accepted_open_loop_ids: [], resolved_open_loop_ids: [], evidence_inspections: [],
          },
        }),
      });
    mocks.apiPost.mockImplementation((path: string) => {
      if (path === '/api/agent/resident-turn/mutate') {
        return Promise.resolve({
          capability_id: 'task.reprioritize',
          operation_id: 'reprioritize:33333333',
          status: 'applied',
          receipt_id: '44444444-4444-4444-4444-444444444444',
        });
      }
      return Promise.resolve({});
    });

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    const [path, bodyRaw] = mocks.apiPost.mock.calls[0] as unknown[];
    expect(path).toBe('/api/agent/resident-turn/mutate');
    expect(bodyRaw).toMatchObject({
      capability_id: 'task.reprioritize',
      project_id: providerInput.identity.project_id,
      turn_task_id: 'task-id',
      authority_generation: 7,
      operation_id: 'reprioritize:33333333',
      expected_revisions: {
        authority_generation: 7,
        active_turn_task_version: 9,
        current_checkpoint_id: providerInput.lineage.checkpoint_id,
        resident_checkpoint_version: providerInput.lineage.checkpoint_version,
        resident_checkpoint_hash: providerInput.lineage.checkpoint_hash,
        context_hash: providerInput.lineage.context_hash,
        target_task_updated_at: '2026-07-18T00:00:00Z',
      },
    });
    expect(String(mocks.runClaudeCode.mock.calls[1][0])).toContain(
      'reprioritize:33333333',
    );
    expect(mocks.apiPost.mock.calls[1][0]).toBe('/api/agent/resident-turn/complete');
  });

  it('carries a durable unknown-effect projection into an admissible completion', async () => {
    const completionProjection = {
      operation: {
        operation_id: 'reprioritize:unknown',
        capability: 'task.reprioritize',
        status: 'applying',
        summary: 'Server-authorized Resident operation.',
      },
      receipt: {
        operation_id: 'reprioritize:unknown',
        receipt_id: '44444444-4444-4444-4444-444444444444',
        status: 'unknown_effect',
        summary: 'Durable server operation receipt.',
      },
      unknown_effects: [{
        effect_id: 'reprioritize_task:reprioritize:unknown',
        operation_id: 'reprioritize:unknown',
        receipt_id: '44444444-4444-4444-4444-444444444444',
        summary: 'The external effect outcome is unknown and requires reconciliation.',
        reconciliation_status: 'requires_attention',
      }],
      required_attention: [{
        attention_id: 'unknown-effect:44444444-4444-4444-4444-444444444444',
        severity: 'critical',
        summary: 'Reconcile the uncertain external effect before retrying.',
        evidence_ids: [],
        receipt_ids: ['44444444-4444-4444-4444-444444444444'],
      }],
    };
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          mutation_requests: [{
            capability_id: 'task.reprioritize',
            parameters: {
              task_id: '33333333-3333-3333-3333-333333333333',
              priority: 50,
            },
            operation_id: 'reprioritize:unknown',
            expected_revisions: {
              target_task_updated_at: '2026-07-18T00:00:00Z',
            },
            evidence: [],
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          reply: 'The operation outcome needs reconciliation.',
          facts: [], evidence: [],
          operations: [completionProjection.operation],
          receipts: [completionProjection.receipt],
          unknown_effects: completionProjection.unknown_effects,
          strategic_implications: [], proposed_memory: [], follow_up: null,
          required_attention: completionProjection.required_attention,
          reconciliation: {
            accepted_memory_proposal_ids: [], accepted_implication_ids: [],
            accepted_open_loop_ids: [], resolved_open_loop_ids: [], evidence_inspections: [],
          },
        }),
      });
    mocks.apiPost.mockImplementation((path: string) => {
      if (path === '/api/agent/resident-turn/mutate') {
        return Promise.resolve({
          capability_id: 'task.reprioritize',
          operation_id: 'reprioritize:unknown',
          status: 'unknown_effect',
          receipt_id: '44444444-4444-4444-4444-444444444444',
          completion_projection: completionProjection,
        });
      }
      return Promise.resolve({});
    });

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    const secondPrompt = String(mocks.runClaudeCode.mock.calls[1][0]);
    expect(secondPrompt).toContain('completion_projection');
    expect(secondPrompt).toContain('unknown_effect');
    const completeBody = mocks.apiPost.mock.calls[1][1] as Record<string, unknown>;
    expect(completeBody.result).toMatchObject({
      operations: [completionProjection.operation],
      receipts: [completionProjection.receipt],
      unknown_effects: completionProjection.unknown_effects,
      required_attention: completionProjection.required_attention,
    });
  });

  it('rejects propose-only mutations before calling the backend', async () => {
    mocks.runClaudeCode.mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({
        mutation_requests: [{
          capability_id: 'task.cancel',
          parameters: {
            task_id: '33333333-3333-3333-3333-333333333333',
            reason: 'stop',
          },
          operation_id: 'cancel:33333333',
          expected_revisions: { target_task_updated_at: '2026-07-18T00:00:00Z' },
          evidence: [],
        }],
      }),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('non-invokable mutate capability');
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('rejects provider attempts to override server-owned mutation revisions', async () => {
    mocks.runClaudeCode.mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({
        mutation_requests: [{
          capability_id: 'task.reprioritize',
          parameters: {
            task_id: '33333333-3333-3333-3333-333333333333',
            priority: 50,
          },
          operation_id: 'reprioritize:33333333',
          expected_revisions: { authority_generation: 999 },
          evidence: [],
        }],
      }),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('server-owned revision');
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('fails closed when capability results exceed the aggregate byte budget', async () => {
    mocks.runClaudeCode.mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({
        capability_requests: [{
          capability_id: 'task.inspect',
          parameters: { task_id: '33333333-3333-3333-3333-333333333333' },
        }],
      }),
    });
    mocks.apiPost.mockResolvedValue({
      capability_id: 'task.inspect',
      evidence: { oversized: 'x'.repeat(48_000) },
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('capability results exceed the hard byte budget');
    expect(mocks.runClaudeCode).toHaveBeenCalledTimes(1);
    expect(mocks.apiPost).toHaveBeenCalledTimes(1);
  });

  it('fails closed before provider launch when the assembled prompt exceeds 160 KB', async () => {
    mocks.apiGet.mockResolvedValue({
      provider_input: { ...providerInput, oversized: 'x'.repeat(160_000) },
      turn_task_version: 9,
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('prompt exceeds the hard byte budget');
    expect(mocks.runClaudeCode).not.toHaveBeenCalled();
    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('fails closed when the provider repeats an identical capability request', async () => {
    const request = {
      capability_requests: [{
        capability_id: 'task.inspect',
        parameters: { task_id: '33333333-3333-3333-3333-333333333333' },
      }],
    };
    mocks.runClaudeCode.mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify(request),
    });
    mocks.apiPost.mockResolvedValue({ capability_id: 'task.inspect', evidence: {} });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('repeated an identical capability request');
    expect(mocks.apiPost).toHaveBeenCalledTimes(1);
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
