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
      { id: 'task.inspect', mode: 'inspect', parameters: ['task_id'], availability: 'invoke' },
      { id: 'runner.list', mode: 'inspect', parameters: [], availability: 'invoke' },
      { id: 'task.reprioritize', mode: 'mutate', availability: 'invoke' },
      { id: 'task.cancel', mode: 'mutate', availability: 'propose_only' },
    ],
  },
};

const inspectionTaskId = '33333333-3333-3333-3333-333333333333';
const inspectionRevision = `task:${inspectionTaskId}:v1`;
const inspectionChecksum = 'c'.repeat(64);
const inspectionTimestamp = '2026-07-20T06:36:50Z';

function canonicalInspectionEvidence(
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    kind: 'task',
    project_id: providerInput.identity.project_id,
    source_id: inspectionTaskId,
    revision: inspectionRevision,
    checksum: inspectionChecksum,
    inspected_at: inspectionTimestamp,
    ...overrides,
  };
}

function finalResultWithInspection(
  inspection: Record<string, unknown>,
): Record<string, unknown> {
  return {
    reply: 'Verified',
    facts: [],
    evidence: [{
      evidence_id: 'engine-owner-observation',
      kind: 'task',
      source_id: inspectionTaskId,
      revision: inspectionRevision,
      checksum: inspectionChecksum,
      summary: 'Observed the authoritative Resident turn.',
    }],
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
      evidence_inspections: [inspection],
    },
  };
}

function mockInspectionThenFinal(
  inspection: Record<string, unknown>,
  serverEvidence: Record<string, unknown> = canonicalInspectionEvidence(),
): void {
  mocks.runClaudeCode
    .mockResolvedValueOnce({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({
        capability_requests: [{
          capability_id: 'task.inspect',
          parameters: { task_id: inspectionTaskId },
        }],
      }),
    })
    .mockResolvedValueOnce({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify(finalResultWithInspection(inspection)),
    });
  mocks.apiPost.mockImplementation((path: string) => (
    Promise.resolve(path === '/api/agent/resident-turn/capability'
      ? { capability_id: 'task.inspect', evidence: serverEvidence }
      : {})
  ));
}

function mockInspectionBatchThenFinal(
  serverEvidence: Record<string, unknown>[],
  finalResult: Record<string, unknown>,
): void {
  mocks.runClaudeCode
    .mockResolvedValueOnce({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({
        capability_requests: serverEvidence.map((evidence) => ({
          capability_id: 'task.inspect',
          parameters: { task_id: evidence.source_id },
        })),
      }),
    })
    .mockResolvedValueOnce({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify(finalResult),
    });
  let inspectionCall = 0;
  mocks.apiPost.mockImplementation((path: string) => (
    Promise.resolve(path === '/api/agent/resident-turn/capability'
      ? { capability_id: 'task.inspect', evidence: serverEvidence[inspectionCall++] }
      : {})
  ));
}
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

  it('canonicalizes a catalog id and omitted empty parameters before invocation', async () => {
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{ id: 'runner.list' }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          reply: 'Listed', facts: [], evidence: [], operations: [], receipts: [],
          unknown_effects: [], strategic_implications: [], proposed_memory: [],
          follow_up: null, required_attention: [],
          reconciliation: {
            accepted_memory_proposal_ids: [], accepted_implication_ids: [],
            accepted_open_loop_ids: [], resolved_open_loop_ids: [], evidence_inspections: [],
          },
        }),
      });
    mocks.apiPost.mockImplementation((path: string) => (
      Promise.resolve(path === '/api/agent/resident-turn/capability'
        ? { capability_id: 'runner.list', evidence: {} }
        : {})
    ));

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(String(mocks.runClaudeCode.mock.calls[0][0])).toContain(
      'Copy catalog id into request capability_id',
    );
    expect(mocks.apiPost.mock.calls[0]).toEqual([
      '/api/agent/resident-turn/capability',
      { capability_id: 'runner.list', parameters: {} },
    ]);
    expect(mocks.apiPost.mock.calls[1][0]).toBe('/api/agent/resident-turn/complete');
  });

  it.each([
    [
      'conflicting identifiers',
      { id: 'runner.list', capability_id: 'task.inspect', parameters: {} },
      'conflicting capability identifiers',
    ],
    [
      'null parameters',
      { capability_id: 'runner.list', parameters: null },
      'parameters must be a JSON object',
    ],
    [
      'array parameters',
      { capability_id: 'runner.list', parameters: [] },
      'parameters must be a JSON object',
    ],
  ])('rejects %s before invoking a capability', async (_label, request, error) => {
    mocks.runClaudeCode.mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({ capability_requests: [request] }),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow(error);

    expect(mocks.apiPost).not.toHaveBeenCalled();
  });

  it('keeps an aliased unknown capability behind the catalog availability fence', async () => {
    mocks.runClaudeCode.mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: JSON.stringify({ capability_requests: [{ id: 'unknown.inspect' }] }),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('non-invokable inspect capability');

    expect(mocks.apiPost).not.toHaveBeenCalled();
  });
  it('canonicalizes a partial inspection reference from server-validated evidence', async () => {
    const taskId = '33333333-3333-3333-3333-333333333333';
    const revision = `task:${taskId}:v1`;
    const checksum = 'c'.repeat(64);
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: taskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          reply: 'Verified', facts: [],
          evidence: [{
            evidence_id: 'engine-owner-observation',
            kind: 'task',
            source_id: taskId,
            revision,
            checksum,
            summary: 'Observed the authoritative Resident turn.',
          }],
          operations: [], receipts: [], unknown_effects: [],
          strategic_implications: [], proposed_memory: [], follow_up: null,
          required_attention: [],
          reconciliation: {
            accepted_memory_proposal_ids: [], accepted_implication_ids: [],
            accepted_open_loop_ids: [], resolved_open_loop_ids: [],
            evidence_inspections: [{ source_id: taskId, revision }],
          },
        }),
      });
    mocks.apiPost.mockImplementation((path: string) => (
      Promise.resolve(path === '/api/agent/resident-turn/capability'
        ? {
          capability_id: 'task.inspect',
          evidence: {
            kind: 'task', project_id: providerInput.identity.project_id,
            source_id: taskId, revision, checksum,
            inspected_at: '2026-07-20T06:36:50Z',
          },
        }
        : {})
    ));

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(mocks.apiPost.mock.calls[1][0]).toBe('/api/agent/resident-turn/complete');
    expect(mocks.apiPost.mock.calls[1][1]).toMatchObject({
      evidence_inspections: [{
        evidence_id: 'engine-owner-observation',
        kind: 'task',
        source_id: taskId,
        revision,
        checksum,
        inspected_at: '2026-07-20T06:36:50Z',
        decisive: true,
      }],
    });
  });

  it('completes selected result evidence trust metadata from one server inspection', async () => {
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: inspectionTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          ...finalResultWithInspection({ evidence_id: 'engine-owner-observation' }),
          evidence: [{
            evidence_id: 'engine-owner-observation',
            summary: 'Observed the authoritative Resident turn.',
          }],
        }),
      });
    mocks.apiPost.mockImplementation((path: string) => (
      Promise.resolve(path === '/api/agent/resident-turn/capability'
        ? { capability_id: 'task.inspect', evidence: canonicalInspectionEvidence() }
        : {})
    ));

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(mocks.apiPost.mock.calls[1][0]).toBe('/api/agent/resident-turn/complete');
    expect(mocks.apiPost.mock.calls[1][1]).toMatchObject({
      result: {
        evidence: [{
          evidence_id: 'engine-owner-observation',
          kind: 'task',
          source_id: inspectionTaskId,
          revision: inspectionRevision,
          checksum: inspectionChecksum,
          summary: 'Observed the authoritative Resident turn.',
        }],
      },
      evidence_inspections: [{
        evidence_id: 'engine-owner-observation',
        kind: 'task',
        source_id: inspectionTaskId,
        revision: inspectionRevision,
        checksum: inspectionChecksum,
        inspected_at: inspectionTimestamp,
        decisive: true,
      }],
    });
  });

  it('does not synthesize a missing semantic result evidence item', async () => {
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: inspectionTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          ...finalResultWithInspection({ evidence_id: 'engine-owner-observation' }),
          evidence: [],
        }),
      });
    mocks.apiPost.mockResolvedValue({
      capability_id: 'task.inspect', evidence: canonicalInspectionEvidence(),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('does not identify one result evidence item');

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/capability');
  });

  it('does not synthesize a missing result evidence summary', async () => {
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: inspectionTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          ...finalResultWithInspection({ evidence_id: 'engine-owner-observation' }),
          evidence: [{ evidence_id: 'engine-owner-observation' }],
        }),
      });
    mocks.apiPost.mockResolvedValue({
      capability_id: 'task.inspect', evidence: canonicalInspectionEvidence(),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('result evidence requires evidence_id and summary');

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/capability');
  });

  it('accepts exact server-owned evidence extras and strips them from the strict result', async () => {
    const data = { status: 'IN_PROGRESS', nested: { attempts: 1, labels: ['resident'] } };
    const serverEvidence = canonicalInspectionEvidence({ data });
    const finalResult = finalResultWithInspection({ evidence_id: 'engine-owner-observation' });
    finalResult.evidence = [{
      ...(finalResult.evidence as Record<string, unknown>[])[0],
      project_id: providerInput.identity.project_id,
      inspected_at: inspectionTimestamp,
      data: { nested: { labels: ['resident'], attempts: 1 }, status: 'IN_PROGRESS' },
    }];
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: inspectionTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(finalResult) });
    mocks.apiPost.mockImplementation((path: string) => (
      Promise.resolve(path === '/api/agent/resident-turn/capability'
        ? { capability_id: 'task.inspect', evidence: serverEvidence }
        : {})
    ));

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(mocks.apiPost.mock.calls[1][1]).toMatchObject({
      result: {
        evidence: [{
          evidence_id: 'engine-owner-observation',
          kind: 'task',
          source_id: inspectionTaskId,
          revision: inspectionRevision,
          checksum: inspectionChecksum,
          summary: 'Observed the authoritative Resident turn.',
        }],
      },
    });
    const completion = mocks.apiPost.mock.calls[1][1] as {
      result: { evidence: Record<string, unknown>[] };
    };
    expect(completion.result.evidence[0]).not.toHaveProperty('project_id');
    expect(completion.result.evidence[0]).not.toHaveProperty('inspected_at');
    expect(completion.result.evidence[0]).not.toHaveProperty('data');
  });

  it('binds the live eight-inspection final-only result by exact non-positional extras', async () => {
    const inspections = Array.from({ length: 8 }, (_, index) => {
      const suffix = String(index + 1).padStart(12, '0');
      const sourceId = `44444444-4444-4444-4444-${suffix}`;
      return {
        kind: 'task',
        project_id: providerInput.identity.project_id,
        source_id: sourceId,
        revision: `task:${sourceId}:v${index + 1}`,
        checksum: String(index + 1).repeat(64),
        inspected_at: `2026-07-20T06:3${index}:50Z`,
        data: { position: index, nested: { attempts: index + 1, labels: [`resident-${index}`] } },
      };
    });
    const selected = inspections[5];
    const finalResult = finalResultWithInspection({ evidence_id: 'selected-evidence' });
    finalResult.evidence = [
      ...inspections.slice(0, 3).map((inspection, index) => ({
        evidence_id: `unselected-${index}`,
        kind: inspection.kind,
        source_id: inspection.source_id,
        revision: inspection.revision,
        checksum: inspection.checksum,
        summary: `Unselected inspection ${index}.`,
      })),
      {
        evidence_id: 'selected-evidence',
        summary: 'Observed the selected authoritative Resident turn.',
        project_id: selected.project_id,
        inspected_at: selected.inspected_at,
        data: {
          nested: {
            labels: selected.data.nested.labels,
            attempts: selected.data.nested.attempts,
          },
          position: selected.data.position,
        },
      },
    ];
    mockInspectionBatchThenFinal(inspections, finalResult);

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(mocks.runClaudeCode.mock.calls[1][0]).toContain(
      'Capability actions are closed for this turn.',
    );
    expect(mocks.apiPost).toHaveBeenCalledTimes(9);
    expect(mocks.apiPost.mock.calls[8][0]).toBe('/api/agent/resident-turn/complete');
    expect(mocks.apiPost.mock.calls[8][1]).toMatchObject({
      result: {
        evidence: [
          {}, {}, {},
          {
            evidence_id: 'selected-evidence',
            kind: selected.kind,
            source_id: selected.source_id,
            revision: selected.revision,
            checksum: selected.checksum,
            summary: 'Observed the selected authoritative Resident turn.',
          },
        ],
      },
      evidence_inspections: [{
        evidence_id: 'selected-evidence',
        kind: selected.kind,
        source_id: selected.source_id,
        revision: selected.revision,
        checksum: selected.checksum,
        inspected_at: selected.inspected_at,
        decisive: true,
      }],
    });
    const completion = mocks.apiPost.mock.calls[8][1] as {
      result: { evidence: Record<string, unknown>[] };
    };
    expect(completion.result.evidence[3]).not.toHaveProperty('project_id');
    expect(completion.result.evidence[3]).not.toHaveProperty('inspected_at');
    expect(completion.result.evidence[3]).not.toHaveProperty('data');
  });
  it('keeps identical copied extras across different inspections ambiguous', async () => {
    const sharedData = { status: 'IN_PROGRESS', nested: { attempts: 1 } };
    const secondTaskId = '55555555-5555-5555-5555-555555555555';
    const inspections = [
      canonicalInspectionEvidence({ data: sharedData }),
      canonicalInspectionEvidence({
        source_id: secondTaskId,
        revision: `task:${secondTaskId}:v2`,
        checksum: 'd'.repeat(64),
        data: sharedData,
      }),
    ];
    const finalResult = finalResultWithInspection({ evidence_id: 'engine-owner-observation' });
    finalResult.evidence = [{
      evidence_id: 'engine-owner-observation',
      summary: 'Observed one Resident turn.',
      project_id: providerInput.identity.project_id,
      inspected_at: inspectionTimestamp,
      data: sharedData,
    }];
    mockInspectionBatchThenFinal(inspections, finalResult);

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('is not backed by one server inspection');

    expect(mocks.apiPost).toHaveBeenCalledTimes(2);
  });

  it('does not combine copied extras from different inspection receipts', async () => {
    const secondTaskId = '55555555-5555-5555-5555-555555555555';
    const secondData = { status: 'COMPLETED', nested: { attempts: 2 } };
    const inspections = [
      canonicalInspectionEvidence({ data: { status: 'IN_PROGRESS', nested: { attempts: 1 } } }),
      canonicalInspectionEvidence({
        source_id: secondTaskId,
        revision: `task:${secondTaskId}:v2`,
        checksum: 'd'.repeat(64),
        inspected_at: '2026-07-20T06:37:50Z',
        data: secondData,
      }),
    ];
    const finalResult = finalResultWithInspection({ evidence_id: 'engine-owner-observation' });
    finalResult.evidence = [{
      evidence_id: 'engine-owner-observation',
      summary: 'Observed one Resident turn.',
      project_id: providerInput.identity.project_id,
      inspected_at: inspectionTimestamp,
      data: secondData,
    }];
    mockInspectionBatchThenFinal(inspections, finalResult);

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('contains an unverified server field');

    expect(mocks.apiPost).toHaveBeenCalledTimes(2);
  });
  it.each([
    ['project_id', '99999999-9999-9999-9999-999999999999', {}],
    ['inspected_at', '2026-07-20T06:37:50Z', {}],
    ['data', { status: 'COMPLETED' }, { data: { status: 'IN_PROGRESS' } }],
  ])('rejects changed server evidence extra %s', async (field, value, serverOverrides) => {
    const finalResult = finalResultWithInspection({ evidence_id: 'engine-owner-observation' });
    finalResult.evidence = [{
      ...(finalResult.evidence as Record<string, unknown>[])[0],
      [field]: value,
    }];
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: inspectionTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(finalResult) });
    mocks.apiPost.mockResolvedValue({
      capability_id: 'task.inspect',
      evidence: canonicalInspectionEvidence(serverOverrides),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow(`unverified server field: ${field}`);

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/capability');
  });
  it('rejects unknown fields on selected result evidence instead of stripping them', async () => {
    const finalResult = finalResultWithInspection({ evidence_id: 'engine-owner-observation' });
    finalResult.evidence = [{
      ...(finalResult.evidence as Record<string, unknown>[])[0],
      injected_authority: 99,
    }];
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: inspectionTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(finalResult) });
    mocks.apiPost.mockResolvedValue({
      capability_id: 'task.inspect', evidence: canonicalInspectionEvidence(),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('result evidence contains an unknown field');

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/capability');
  });

  it.each([
    ['kind', 'runner'],
    ['source_id', '44444444-4444-4444-4444-444444444444'],
    ['revision', `task:${inspectionTaskId}:stale`],
    ['checksum', 'd'.repeat(64)],
  ])('does not overwrite stale result evidence %s', async (field, value) => {
    const finalResult = finalResultWithInspection({ evidence_id: 'engine-owner-observation' });
    finalResult.evidence = [{
      ...(finalResult.evidence as Record<string, unknown>[])[0],
      [field]: value,
    }];
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: inspectionTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({ exitCode: 0, allOutput: '', lastResultText: JSON.stringify(finalResult) });
    mocks.apiPost.mockResolvedValue({
      capability_id: 'task.inspect', evidence: canonicalInspectionEvidence(),
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('is not backed by one server inspection');

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/capability');
  });

  it('rejects a partial inspection reference not backed by one server receipt', async () => {
    const taskId = '33333333-3333-3333-3333-333333333333';
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: taskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          reply: 'Verified', facts: [],
          evidence: [{
            evidence_id: 'engine-owner-observation', kind: 'task', source_id: taskId,
            revision: 'task:v1', checksum: 'c'.repeat(64), summary: 'Observed.',
          }],
          operations: [], receipts: [], unknown_effects: [], strategic_implications: [],
          proposed_memory: [], follow_up: null, required_attention: [],
          reconciliation: {
            accepted_memory_proposal_ids: [], accepted_implication_ids: [],
            accepted_open_loop_ids: [], resolved_open_loop_ids: [],
            evidence_inspections: [{ source_id: taskId, revision: 'task:v1' }],
          },
        }),
      });
    mocks.apiPost.mockResolvedValue({
      capability_id: 'task.inspect',
      evidence: {
        kind: 'task', source_id: taskId, revision: 'task:v2',
        checksum: 'd'.repeat(64), inspected_at: '2026-07-20T06:36:50Z',
      },
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('is not backed by one server inspection');

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/capability');
  });

  it.each([
    [
      'source_id without revision',
      { source_id: inspectionTaskId },
      'requires evidence_id or source_id plus revision',
    ],
    [
      'revision without source_id',
      { revision: inspectionRevision },
      'requires evidence_id or source_id plus revision',
    ],
    [
      'empty evidence_id with fallback fields',
      { evidence_id: '', source_id: inspectionTaskId, revision: inspectionRevision },
      'evidence_id must be a non-empty string',
    ],
    [
      'non-string evidence_id with fallback fields',
      { evidence_id: 17, source_id: inspectionTaskId, revision: inspectionRevision },
      'evidence_id must be a non-empty string',
    ],
  ])('rejects inspection selector %s', async (_label, inspection, error) => {
    mockInspectionThenFinal(inspection);

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow(error);

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/capability');
  });

  it.each([
    [
      'checksum',
      { evidence_id: 'engine-owner-observation', checksum: 'd'.repeat(64) },
    ],
    [
      'inspected_at',
      { evidence_id: 'engine-owner-observation', inspected_at: '2026-07-20T00:00:00Z' },
    ],
  ])('rejects supplied inspection %s that conflicts with server evidence', async (field, inspection) => {
    mockInspectionThenFinal(inspection);

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow(`.${field} conflicts with server evidence`);

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/capability');
  });

  it('rejects ambiguous duplicate server inspection candidates', async () => {
    const secondTaskId = '44444444-4444-4444-4444-444444444444';
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: inspectionTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: secondTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify(finalResultWithInspection({
          evidence_id: 'engine-owner-observation',
        })),
      });
    let inspectionCall = 0;
    mocks.apiPost.mockImplementation((path: string) => {
      if (path !== '/api/agent/resident-turn/capability') return Promise.resolve({});
      inspectionCall += 1;
      return Promise.resolve({
        capability_id: 'task.inspect',
        evidence: canonicalInspectionEvidence({
          inspected_at: `2026-07-20T06:36:5${inspectionCall}Z`,
        }),
      });
    });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('is not backed by one server inspection');

    expect(mocks.apiPost).toHaveBeenCalledTimes(2);
    expect(mocks.apiPost.mock.calls.every(([path]) => (
      path === '/api/agent/resident-turn/capability'
    ))).toBe(true);
  });

  it('does not treat mutation-response evidence as inspected evidence', async () => {
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          mutation_requests: [{
            capability_id: 'task.reprioritize',
            parameters: { task_id: inspectionTaskId, priority: 50 },
            operation_id: 'reprioritize:inspection-provenance',
            expected_revisions: { target_task_updated_at: '2026-07-20T00:00:00Z' },
            evidence: [],
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify(finalResultWithInspection({
          evidence_id: 'engine-owner-observation',
        })),
      });
    mocks.apiPost.mockImplementation((path: string) => (
      Promise.resolve(path === '/api/agent/resident-turn/mutate'
        ? {
          capability_id: 'task.reprioritize',
          operation_id: 'reprioritize:inspection-provenance',
          status: 'applied',
          evidence: canonicalInspectionEvidence(),
        }
        : {})
    ));

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('is not backed by one server inspection');

    expect(mocks.apiPost).toHaveBeenCalledOnce();
    expect(mocks.apiPost.mock.calls[0][0]).toBe('/api/agent/resident-turn/mutate');
  });

  it('preserves an explicit non-decisive inspection claim', async () => {
    mockInspectionThenFinal({
      evidence_id: 'engine-owner-observation',
      decisive: false,
    });

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(mocks.apiPost.mock.calls[1][0]).toBe('/api/agent/resident-turn/complete');
    expect(mocks.apiPost.mock.calls[1][1]).toMatchObject({
      evidence_inspections: [{
        evidence_id: 'engine-owner-observation',
        decisive: false,
      }],
    });
  });
  it('forces final structured synthesis after two distinct capability rounds', async () => {
    const firstTaskId = '33333333-3333-3333-3333-333333333333';
    const secondTaskId = '44444444-4444-4444-4444-444444444444';
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: firstTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: secondTaskId },
          }],
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          reply: 'Synthesized', facts: [], evidence: [], operations: [], receipts: [],
          unknown_effects: [], strategic_implications: [], proposed_memory: [],
          follow_up: null, required_attention: [],
          reconciliation: {
            accepted_memory_proposal_ids: [], accepted_implication_ids: [],
            accepted_open_loop_ids: [], resolved_open_loop_ids: [], evidence_inspections: [],
          },
        }),
      });
    mocks.apiPost.mockImplementation((path: string, body: Record<string, unknown>) => {
      if (path === '/api/agent/resident-turn/capability') {
        return Promise.resolve({
          capability_id: 'task.inspect',
          evidence: { source_id: (body.parameters as Record<string, unknown>).task_id },
        });
      }
      return Promise.resolve({});
    });

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(mocks.runClaudeCode).toHaveBeenCalledTimes(3);
    expect(String(mocks.runClaudeCode.mock.calls[0][0])).toContain(
      '2 capability action round(s) and 8 request slot(s) remaining',
    );
    expect(String(mocks.runClaudeCode.mock.calls[1][0])).toContain(
      '1 capability action round(s) and 7 request slot(s) remaining',
    );
    const finalPrompt = String(mocks.runClaudeCode.mock.calls[2][0]);
    expect(finalPrompt).toContain('Capability actions are closed for this turn');
    expect(finalPrompt).toContain('evidence_inspections must be an array of objects shaped');
    expect(finalPrompt).toContain('Each cited result.evidence item must be shaped');
    expect(finalPrompt).not.toContain('{"capability_requests"');
    expect(finalPrompt).not.toContain('{"mutation_requests"');
    expect(finalPrompt).not.toContain('Request at most');
    expect(finalPrompt).not.toContain('Request inspections and mutations');
    expect(finalPrompt).toContain(firstTaskId);
    expect(finalPrompt).toContain(secondTaskId);
    expect(mocks.apiPost.mock.calls.filter(([path]) => (
      path === '/api/agent/resident-turn/capability'
    ))).toHaveLength(2);
    expect(mocks.apiPost.mock.calls.at(-1)?.[0]).toBe('/api/agent/resident-turn/complete');
  });

  it('forces final synthesis when all capability request slots are exhausted', async () => {
    const taskIds = Array.from({ length: 8 }, (_, index) => (
      `00000000-0000-4000-8000-${String(index + 1).padStart(12, '0')}`
    ));
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: taskIds.map((taskId) => ({
            capability_id: 'task.inspect',
            parameters: { task_id: taskId },
          })),
        }),
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          reply: 'Budget synthesized', facts: [], evidence: [], operations: [], receipts: [],
          unknown_effects: [], strategic_implications: [], proposed_memory: [],
          follow_up: null, required_attention: [],
          reconciliation: {
            accepted_memory_proposal_ids: [], accepted_implication_ids: [],
            accepted_open_loop_ids: [], resolved_open_loop_ids: [], evidence_inspections: [],
          },
        }),
      });
    mocks.apiPost.mockImplementation((path: string, body: Record<string, unknown>) => {
      if (path === '/api/agent/resident-turn/capability') {
        return Promise.resolve({
          capability_id: 'task.inspect',
          evidence: { source_id: (body.parameters as Record<string, unknown>).task_id },
        });
      }
      return Promise.resolve({});
    });

    await handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    );

    expect(mocks.runClaudeCode).toHaveBeenCalledTimes(2);
    const finalPrompt = String(mocks.runClaudeCode.mock.calls[1][0]);
    expect(finalPrompt).toContain('Capability actions are closed for this turn');
    expect(finalPrompt).toContain('evidence_inspections must be an array of objects shaped');
    expect(finalPrompt).toContain('Each cited result.evidence item must be shaped');
    expect(finalPrompt).not.toContain('{"capability_requests"');
    expect(finalPrompt).not.toContain('{"mutation_requests"');
    expect(finalPrompt).not.toContain('Request at most');
    expect(finalPrompt).not.toContain('Request inspections and mutations');
    expect(mocks.apiPost.mock.calls.filter(([path]) => (
      path === '/api/agent/resident-turn/capability'
    ))).toHaveLength(8);
    expect(mocks.apiPost.mock.calls.at(-1)?.[0]).toBe('/api/agent/resident-turn/complete');
  });
  it('never executes a capability requested after the action phase closes', async () => {
    const taskIds = [
      '33333333-3333-3333-3333-333333333333',
      '44444444-4444-4444-4444-444444444444',
      '55555555-5555-5555-5555-555555555555',
    ];
    for (const taskId of taskIds) {
      mocks.runClaudeCode.mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          capability_requests: [{
            capability_id: 'task.inspect',
            parameters: { task_id: taskId },
          }],
        }),
      });
    }
    mocks.apiPost.mockResolvedValue({ capability_id: 'task.inspect', evidence: {} });

    await expect(handleStrategicResidentTurn(
      'task-id', { _resident_expected_runtime_provider: 'anthropic' }, {}, '', '', '', null, { value: '' },
    )).rejects.toThrow('requested a capability after the action phase closed');

    expect(mocks.runClaudeCode).toHaveBeenCalledTimes(3);
    expect(String(mocks.runClaudeCode.mock.calls[2][0])).toContain(
      'Capability actions are closed for this turn',
    );
    expect(mocks.apiPost.mock.calls.filter(([path]) => (
      path === '/api/agent/resident-turn/capability'
    ))).toHaveLength(2);
    expect(mocks.apiPost.mock.calls.some(([path]) => (
      path === '/api/agent/resident-turn/complete'
      || path === '/api/agent/resident-turn/mutate'
    ))).toBe(false);
  });

  it('posts a mutation with server-owned authority revisions and returns its receipt to the provider', async () => {
    mocks.runClaudeCode
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: '',
        lastResultText: JSON.stringify({
          mutation_requests: [{
            id: 'task.reprioritize',
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
            id: 'task.reprioritize',
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
