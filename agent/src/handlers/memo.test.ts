import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({
    exitCode: 0, allOutput: '', lastResultText: '',
    codexTurnCompleted: false, stderrTail: '',
  }),
  persistAndPostStatus: vi.fn().mockResolvedValue(undefined),
  strVal: (v: unknown, def = '') => (typeof v === 'string' ? v : def),
  sanitize: (s: string) => s,
  setCurrentPromptFingerprint: vi.fn(),
  sendHeartbeatSync: vi.fn(),
}));

vi.mock('../prompt.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../prompt.js')>();
  return {
    ...actual,
    buildContextPrefix: vi.fn().mockReturnValue(''),
    configureAgentPermissions: vi.fn().mockReturnValue([]),
  };
});

vi.mock('fs', () => ({
  default: {
    readFileSync: vi.fn().mockReturnValue(''),
    writeFileSync: vi.fn(),
    existsSync: vi.fn().mockReturnValue(false),
    appendFileSync: vi.fn(),
  },
}));

vi.mock('child_process', () => ({
  execFileSync: vi.fn().mockReturnValue(Buffer.from('')),
}));

vi.mock('../telemetry.js', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../telemetry.js')>();
  return {
    ...actual,
    recordFormatRepairTokens: vi.fn(),
    recordFormatRepairMiss: vi.fn(),
    getLlmTelemetry: vi.fn().mockReturnValue({
      llm_call_count: 0,
      total_input_tokens: 0,
      total_output_tokens: 0,
      viewport_screenshot_forwarded_count: 0,
      total_cache_creation_input_tokens: 0,
      total_cache_read_input_tokens: 0,
      num_turns: 0,
      total_tool_result_bytes: 0,
      prompt_size_bytes: null,
      prompt_section_count: null,
      prompt_section_sizes: null,
    }),
  };
});

// ── Imports under test ────────────────────────────────────────────────────────
import { handleMemo } from './memo.js';
import { runClaudeCode, persistAndPostStatus, sendHeartbeatSync, setCurrentPromptFingerprint } from '../index.js';
import { configureAgentPermissions } from '../prompt.js';
import { recordFormatRepairTokens, recordFormatRepairMiss, getLlmTelemetry } from '../telemetry.js';
import { expectCalledWith } from '../utils/typed-mock-call.js';

// ── Shared helpers ────────────────────────────────────────────────────────────
const TASK_ID = 'task-memo-123';
const GH_TOKEN = 'gh-token';
const OWNER = 'owner';
const REPO = 'repo';
const WS = null;
const STATUS_REF = () => ({ value: '' });

const BASE_TASK = { title: 'Memo Task', description: 'Analyze something', context: {} };

const VALID_MEMO_JSON = JSON.stringify({
  summary: 'A concise summary.',
  reasoning: 'Detailed reasoning here.',
  citations: ['task:abc', 'direction:def'],
  tags: ['performance', 'architecture'],
});

describe('handleMemo', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: VALID_MEMO_JSON,
      codexTurnCompleted: true, stderrTail: '',
    });
    vi.mocked(getLlmTelemetry).mockReturnValue({
      llm_call_count: 0, total_input_tokens: 0, total_output_tokens: 0,
      total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0,
      viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0,
      prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null,
    });
  });

  it('posts COMPLETED with no artifact_type when exitCode 0 and lastResultText has valid JSON', async () => {
    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBeUndefined();
    expect(call.failure_category).toBeUndefined();
  });

  it('posts FAILED with claude_code_failed when exitCode non-zero but JSON is parseable', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 1, allOutput: '', lastResultText: VALID_MEMO_JSON,
      codexTurnCompleted: false, stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('claude_code_failed');
  });

  it('falls back to allOutput when lastResultText does not contain the required JSON keys', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: `Some preamble\n${VALID_MEMO_JSON}`,
      lastResultText: 'Here is my analysis (no JSON object).',
      codexTurnCompleted: true, stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBeUndefined();
  });

  it('posts FAILED with incomplete when neither source contains the schema markers', async () => {
    // Layer B (2026-05-26): parse_failed should only fire when the agent
    // actually tried to emit the schema (`"summary"` / `"reasoning"`
    // present somewhere). Output that is pure prose with no schema markers
    // means the agent silently bailed — `incomplete` is the canonical
    // wrapper-fallback label so analytics can separate "agent bailed" from
    // "agent emitted bad JSON".
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'no json here',
      lastResultText: 'no json here either',
      codexTurnCompleted: true, stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('incomplete');
  });

  it('posts FAILED with parse_failed when JSON is missing required keys (citations, tags)', async () => {
    // The agent DID try to emit the schema (output contains `"summary"`
    // and `"reasoning"`) — Layer B keeps the `parse_failed` label here
    // because the diagnostic value is real: the agent produced malformed
    // JSON, not silently bailed.
    const incomplete = JSON.stringify({ summary: 'ok', reasoning: 'ok' });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: incomplete,
      codexTurnCompleted: true, stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('includes the output tail in the failure error logs', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'agent output without valid json',
      lastResultText: '',
      codexTurnCompleted: true, stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toContain('output tail');
    // Retry is attempted on exitCode=0 + no JSON, so both run results are
    // reported in the failure log for diagnosis.
    expect(call.logs).toContain('retryExitCode=');
    expect(call.logs).toContain('agent output without valid json');
  });

  it('reopens with continuation prompt and succeeds if retry emits valid JSON', async () => {
    // First run: agent drifts, emits prose but no JSON
    // Second run (the reopen): agent emits valid JSON
    const validJson = JSON.stringify({
      summary: 'A concise summary.',
      reasoning: 'Detailed reasoning.',
      citations: ['task:abc'],
      tags: ['tag-a'],
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'prose without any valid JSON object emitted',
        lastResultText: '',
        codexTurnCompleted: true, stderrTail: '',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: validJson,
        lastResultText: validJson,
        codexTurnCompleted: true, stderrTail: '',
      });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Two runClaudeCode invocations: initial + reopen retry.
    expect(vi.mocked(runClaudeCode).mock.calls.length).toBe(2);
    // Retry prompt must re-state the schema and include the prior reasoning
    // so a fresh-session Claude can fill the fields from context.
    const retryPrompt = vi.mocked(runClaudeCode).mock.calls[1][0];
    expect(retryPrompt).toContain('Output ONLY the JSON object now');
    expect(retryPrompt).toContain('"summary"');
    expect(retryPrompt).toContain('prose without any valid JSON object emitted');
    // Final status should be COMPLETED with the parsed memo, not FAILED.
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toContain('"summary"');
    expect(call.logs).toContain('A concise summary.');
  });

  it('records repair token delta when retry attempt consumes LLM tokens', async () => {
    // First run: agent drifts, emits prose but no JSON
    // Second run (the reopen): agent emits valid JSON and consumes tokens
    const validJson = JSON.stringify({
      summary: 'A concise summary.',
      reasoning: 'Detailed reasoning.',
      citations: ['task:abc'],
      tags: ['tag-a'],
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'prose without any valid JSON object emitted',
        lastResultText: '',
        codexTurnCompleted: true, stderrTail: '',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: validJson,
        lastResultText: validJson,
        codexTurnCompleted: true, stderrTail: '',
      });

    // Mock token consumption: first call returns before-repair tokens, second returns after-repair
    let callCount = 0;
    vi.mocked(getLlmTelemetry).mockImplementation(() => {
      callCount++;
      return {
        llm_call_count: 1,
        // First call (before repair) returns 100/50
        // Second call (after repair) returns 300/150 (consumed 200 input, 100 output)
        total_input_tokens: callCount === 1 ? 100 : 300,
        total_output_tokens: callCount === 1 ? 50 : 150,
        total_cache_creation_input_tokens: 0,
        total_cache_read_input_tokens: 0,
        viewport_screenshot_forwarded_count: 0,
        num_turns: 0,
        total_tool_result_bytes: 0,
        prompt_size_bytes: null,
        prompt_section_count: null,
        prompt_section_sizes: null,
      };
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Verify recordFormatRepairTokens is called with positive delta and schemaLabel='memo'
    expectCalledWith(recordFormatRepairTokens, 200, 100, 'memo');
  });

  it('does NOT reopen when first run failed (exitCode !== 0)', async () => {
    // If the agent itself crashed (rate limit, OOM, etc), retrying with the
    // same setup wastes a slot. Reopen only fires for exitCode=0 drift.
    vi.mocked(runClaudeCode).mockClear();
    vi.mocked(runClaudeCode).mockResolvedValueOnce({
      exitCode: 1,
      allOutput: 'Claude init banner only',
      lastResultText: '',
      codexTurnCompleted: false, stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // Only one runClaudeCode call — no retry on non-zero exit.
    expect(vi.mocked(runClaudeCode).mock.calls.length).toBe(1);
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
  });

  it('records repair miss when retry fails with clean exit (exitCode=0)', async () => {
    // First run: agent drifts, emits prose but no JSON
    // Second run (the reopen): agent also emits bad JSON, missing required keys
    const incompleteJson = JSON.stringify({
      summary: 'Only has summary',
      reasoning: 'and reasoning',
      // Missing citations and tags — will fail extraction
    });
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: 'prose without any valid JSON object emitted',
        lastResultText: '',
        codexTurnCompleted: true, stderrTail: '',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: incompleteJson,
        lastResultText: incompleteJson,
        codexTurnCompleted: true, stderrTail: '',
      });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    // recordFormatRepairMiss should be called with the retry's miss reason
    expectCalledWith(recordFormatRepairMiss, 'parsed_object_missing_key', 'memo');
  });

  it('logs the serialized memo object on success', async () => {
    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toContain('"summary"');
    expect(call.logs).toContain('A concise summary.');
  });

  it('configures agent permissions as "memo" for read-only posture', async () => {
    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(configureAgentPermissions).toHaveBeenCalledWith('memo');
  });

  it('sends a heartbeat with prompt_fingerprint before invoking Claude', async () => {
    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(setCurrentPromptFingerprint).toHaveBeenCalledTimes(1);
    expect(sendHeartbeatSync).toHaveBeenCalledTimes(1);
    const heartbeatTaskId = vi.mocked(sendHeartbeatSync).mock.calls[0][0];
    expect(heartbeatTaskId).toBe(TASK_ID);
  });

  // ── Prompt content invariants ─────────────────────────────────────────────

  async function capturePrompt(payload: Record<string, unknown> = {}, task: Record<string, unknown> = BASE_TASK): Promise<string> {
    await handleMemo(TASK_ID, payload, task, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());
    return vi.mocked(runClaudeCode).mock.calls[0][0];
  }

  it('includes topic in the prompt when provided', async () => {
    const prompt = await capturePrompt({ topic: 'system-coupling' });
    expect(prompt).toContain('Topic: system-coupling');
  });

  it('includes reasoning_kind in the prompt when provided', async () => {
    const prompt = await capturePrompt({ reasoning_kind: 'cost-analysis' });
    expect(prompt).toContain('Reasoning kind: cost-analysis');
  });

  it('includes direction_id in the prompt when provided', async () => {
    const prompt = await capturePrompt({ direction_id: 'd-abc123' });
    expect(prompt).toContain('Direction id: d-abc123');
  });

  it('includes source_task_id in the prompt when provided', async () => {
    const prompt = await capturePrompt({ source_task_id: 'task-xyz789' });
    expect(prompt).toContain('Source task id: task-xyz789');
  });

  it('includes source_finding as JSON in the prompt when provided', async () => {
    const finding = { file: 'src/foo.ts', line: 42, issue: 'N+1 query' };
    const prompt = await capturePrompt({ source_finding: finding });
    expect(prompt).toContain('Source finding (JSON):');
    expect(prompt).toContain('"file": "src/foo.ts"');
  });

  it('falls back to task.description when payload.prompt is absent', async () => {
    const task = { ...BASE_TASK, description: 'Analyze the auth coupling' };
    const prompt = await capturePrompt({}, task);
    expect(prompt).toContain('Analyze the auth coupling');
  });

  it('prefers payload.prompt over task.description', async () => {
    const task = { ...BASE_TASK, description: 'From task description' };
    const prompt = await capturePrompt({ prompt: 'From payload prompt' }, task);
    expect(prompt).toContain('From payload prompt');
    expect(prompt).not.toContain('From task description');
  });

  it('includes DEFAULT_TOOL_GUIDANCE (structural invariant)', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('Use Grep/Glob for code search');
  });

  it('instructs the agent not to modify files (read-only design invariant)', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('Do NOT modify any files');
  });

  it('includes all four required JSON schema keys in the instructions', async () => {
    const prompt = await capturePrompt();
    expect(prompt).toContain('"summary"');
    expect(prompt).toContain('"reasoning"');
    expect(prompt).toContain('"citations"');
    expect(prompt).toContain('"tags"');
  });

  it('omits optional sections when payload fields are absent', async () => {
    const prompt = await capturePrompt({});
    expect(prompt).not.toContain('Topic:');
    expect(prompt).not.toContain('Reasoning kind:');
    expect(prompt).not.toContain('Direction id:');
    expect(prompt).not.toContain('Source task id:');
    expect(prompt).not.toContain('Source finding (JSON):');
  });

  // ── Direct synthesis for dispatcher-sourced memos ────────────────────────

  it('falls through to Claude when reasoning_kind is not a dispatcher kind', async () => {
    const payload = {
      reasoning_kind: 'deferred_finding',
      source_finding: { title: 'Some finding' },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
  });

  it('direction_retirement: synthesizes memo with outcome metrics from retirement_metrics without calling Claude', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      direction_id: 'dir-abc',
      source_task_id: 'task-ret-1',
      source_finding: {
        direction_text: 'Improve DB query performance across the board.',
        rationale: 'Direction fully achieved — all queries now under 50ms.',
        success_evidence: 'P95 latency dropped from 800ms to 40ms after 3 PRs.',
        retirement_condition: 'all_objectives_met',
        retirement_metrics: {
          final_quality_score: 0.91,
          final_prs_merged: 5,
          final_cycles_active: 3,
        },
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    // rationale field read from sf.rationale (not sf.retirement_rationale)
    expect(call.logs).toContain('Direction fully achieved');
    // Outcome metrics must appear (from sf.retirement_metrics, not top-level sf)
    expect(call.logs).toContain('quality_score=0.91');
    expect(call.logs).toContain('prs_merged=5');
    expect(call.logs).toContain('cycles_active=3');
    // success_evidence and retirement_condition should also appear
    expect(call.logs).toContain('P95 latency dropped');
    expect(call.logs).toContain('all_objectives_met');
    // direction citation must appear when direction_id is present
    expect(call.logs).toContain('direction:dir-abc');
    expect(call.logs).toContain('task:task-ret-1');
  });

  it('direction_retirement: falls through to Claude when both direction_text and rationale are absent', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      source_finding: { retirement_condition: 'timeout' },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
  });

  it('direction_retirement: omits Outcome metrics block when retirement_metrics is absent', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      source_finding: {
        direction_text: 'Refactor auth module.',
        rationale: 'Scope too large; retired.',
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).not.toContain('Outcome metrics');
  });

  it('direction_retirement: emits direction:<id> citation when direction_id present', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      direction_id: 'dir-xyz',
      source_finding: {
        direction_text: 'Reduce DB coupling.',
        rationale: 'Goal achieved.',
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    const parsed = JSON.parse(call.logs as string) as { citations: string[] };
    expect(parsed.citations).toContain('direction:dir-xyz');
    expect(parsed.citations).not.toContain(expect.stringMatching(/^task:/));
  });

  it('direction_retirement: emits both direction:<id> and task:<id> when both present', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      direction_id: 'dir-123',
      source_task_id: 'task-456',
      source_finding: {
        direction_text: 'Improve logging.',
        rationale: 'Shipped.',
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const parsed = JSON.parse(call.logs as string) as { citations: string[] };
    expect(parsed.citations).toContain('direction:dir-123');
    expect(parsed.citations).toContain('task:task-456');
  });

  it('direction_retirement: emits empty citations when neither direction_id nor source_task_id present', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      source_finding: {
        direction_text: 'Speed up CI.',
        rationale: 'Obsolete direction.',
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    const parsed = JSON.parse(call.logs as string) as { citations: string[] };
    expect(parsed.citations).toEqual([]);
  });

  it('direction_retirement: renders recent_history_summary when present in source_finding', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      direction_id: 'dir-hist',
      source_finding: {
        direction_text: 'Reduce flaky tests.',
        rationale: 'Flakiness resolved.',
        recent_history_summary: [
          { cycles: 2, prs_merged: 3, tasks_completed: 5, tasks_failed: 1 },
          { cycles: 1, prs_merged: 1, tasks_completed: 2, tasks_failed: 0 },
        ],
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toContain('Recent history:');
    expect(call.logs).toContain('cycles=2');
    expect(call.logs).toContain('prs_merged=3');
    expect(call.logs).toContain('tasks_completed=5');
    expect(call.logs).toContain('tasks_failed=1');
  });

  it('direction_retirement: omits recent_history_summary block when absent from source_finding', async () => {
    const payload = {
      reasoning_kind: 'direction_retirement',
      source_finding: {
        direction_text: 'Remove legacy auth.',
        rationale: 'Done.',
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).not.toContain('Recent history:');
  });

  it('synthesizes direction_retirement memo from production payload shape', async () => {
    // Production shape: direction_id set, source_task_id absent,
    // source_finding has recent_history_summary and retirement_metrics.
    const payload = {
      reasoning_kind: 'direction_retirement',
      direction_id: 'dir-prod-001',
      source_finding: {
        direction_text: 'Consolidate duplicate service clients across the codebase.',
        rationale: 'All clients replaced with shared singleton.',
        success_evidence: 'PR #1012 removed 4 duplicate client instantiations.',
        retirement_condition: 'all_objectives_met',
        retirement_metrics: {
          final_quality_score: 0.88,
          final_prs_merged: 4,
          final_cycles_active: 2,
        },
        recent_history_summary: [
          { cycles: 2, prs_merged: 4, tasks_completed: 7, tasks_failed: 2 },
        ],
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    const parsed = JSON.parse(call.logs as string) as { citations: string[]; reasoning: string };
    // direction citation must appear (no task citation since source_task_id absent)
    expect(parsed.citations).toContain('direction:dir-prod-001');
    expect(parsed.citations.some((c) => c.startsWith('task:'))).toBe(false);
    // recent_history_summary rendered
    expect(parsed.reasoning).toContain('Recent history:');
    expect(parsed.reasoning).toContain('cycles=2');
    expect(parsed.reasoning).toContain('prs_merged=4');
    // outcome metrics rendered
    expect(parsed.reasoning).toContain('quality_score=0.88');
    expect(parsed.reasoning).toContain('prs_merged=4');
    expect(parsed.reasoning).toContain('cycles_active=2');
  });

  // ── Codex JSONL path regression tests ────────────────────────────────────

  it('succeeds when memo JSON in lastResultText exceeds 200 characters', async () => {
    // Regression: Codex item.completed used to truncate item.text to 200 chars
    // before storing in lastResultText, silently breaking long memo JSON objects.
    const longMemoJson = JSON.stringify({
      summary: 'A detailed summary describing the architectural decision around service coupling.',
      reasoning: 'Extended reasoning that covers multiple aspects: (1) the current coupling between modules A and B creates a hidden dependency that makes isolated deploys impossible; (2) moving the shared state into a dedicated service resolves this; (3) the migration path is low-risk because the interface is already well-defined.',
      citations: ['task:abc123', 'direction:def456', 'file:backend/app/shared_state.py'],
      tags: ['coupling', 'architecture', 'migration'],
    });
    expect(longMemoJson.length).toBeGreaterThan(200);

    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: '',
      lastResultText: longMemoJson,
      codexTurnCompleted: true, stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBeUndefined();
    expect(call.logs).toContain('architectural decision');
  });

  it('recovers memo JSON from a Codex item.completed JSONL envelope in allOutput', async () => {
    // Regression: when allOutput contains raw Codex JSONL, the memo JSON is
    // embedded as an escaped string inside item.text. The normal extractor
    // cannot find it (escaped keys like \"summary\" trip the bracket matcher),
    // so extractCodexAgentMessageText must parse the envelope and return item.text.
    const fullMemoJson = JSON.stringify({
      summary: 'Outcome analysis for direction d-3da640d2.',
      reasoning: 'The direction was retired because the targeted refactor scope exceeded the available task budget. Evidence from three prior attempts shows consistent timeout at the 45-minute mark when the diff grows past 800 lines. Recommendation: decompose into smaller clusters before re-attempting.',
      citations: ['direction:d-3da640d2', 'task:t-aabbccdd'],
      tags: ['direction-retired', 'scope', 'decomposition'],
    });

    const codexJsonlLine = JSON.stringify({
      type: 'item.completed',
      item: { type: 'agent_message', text: fullMemoJson },
    });

    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      // lastResultText is truncated (simulating the pre-fix behaviour)
      lastResultText: fullMemoJson.slice(0, 200),
      allOutput: `[codex] turn.started\n${codexJsonlLine}\n[codex] turn.completed\n`,
      codexTurnCompleted: true, stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBeUndefined();
    expect(call.logs).toContain('direction-retired');
  });

  // ── Layer A: cause-C reclassification (2026-05-26) ───────────────────────
  // Regression for the Codex mid-turn silent-success failure mode. The
  // CLI emits {"type":"thread.started"} + {"type":"turn.started"}, then
  // exits clean (exitCode=0) before reaching turn.completed. The handler
  // previously labeled this `parse_failed` → backend classified as
  // CODE_ERROR → 3 retries → terminal FAIL on a transient provider issue.

  it('reclassifies as claude_code_failed when Codex exits clean before turn.completed', async () => {
    // Both runs simulate the cause-C pattern: thread.started + turn.started,
    // no turn.completed, exitCode=0. The retry runs because exitCode=0
    // satisfies the existing reopen gate, but the same provider drop
    // happens again. The reclassification should fire on the retry's
    // codexTurnCompleted=false (the most recent run's signal).
    const codexPartial = (
      `{"type":"thread.started","thread_id":"t1"}\n`
      + `{"type":"turn.started"}\n`
    );
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: codexPartial,
        lastResultText: '',
        codexTurnCompleted: false,
        stderrTail: 'stream closed unexpectedly',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: codexPartial,
        lastResultText: '',
        codexTurnCompleted: false,
        stderrTail: 'stream closed unexpectedly',
      });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('claude_code_failed');
    // Diagnostic surface should include the codex completion marker AND
    // the stderr tail so future investigators don't have to guess.
    expect(call.logs).toContain('codexTurnCompleted=false');
    expect(call.logs).toContain('stderr tail');
    expect(call.logs).toContain('stream closed unexpectedly');
  });

  it('uses parse_failed when Codex DID complete its turn but emitted malformed JSON', async () => {
    // Control case for the reclassification: if Codex finished its turn
    // (turn.completed observed) but emitted JSON that fails the extractor,
    // that is a genuine `parse_failed` — the agent owned the output.
    const malformedJson = `{"summary": "ok", "reasoning": `; // truncated
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: `{"type":"thread.started"}\n{"type":"turn.completed"}\n`,
      lastResultText: malformedJson,
      codexTurnCompleted: true,
      stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    // turn.completed was seen + the output contains "summary"/"reasoning",
    // so this is a real parse failure, not a silent CLI death.
    expect(call.failure_category).toBe('parse_failed');
  });

  // ── Multiple agent_message events (2026-05-27) ───────────────────────────
  // When allOutput contains multiple agent_message JSONL lines (e.g. a short
  // item.updated followed by a full item.completed), extractCodexAgentMessageTexts
  // must return all of them in priority order so tryExtractMemoObject can pick
  // the first one with a valid memo schema.

  it('succeeds when allOutput has a short item.updated followed by a full item.completed memo', async () => {
    // item.updated arrives first with only partial text; item.completed carries
    // the full memo JSON. The handler must succeed using the completed event.
    const fullMemoJson = JSON.stringify({
      summary: 'Full memo summary.',
      reasoning: 'Full reasoning from item.completed envelope.',
      citations: ['task:abc'],
      tags: ['completed'],
    });
    const shortText = 'partial output, no JSON yet';

    const updatedLine = JSON.stringify({
      type: 'item.updated',
      item: { type: 'agent_message', text: shortText },
    });
    const completedLine = JSON.stringify({
      type: 'item.completed',
      item: { type: 'agent_message', text: fullMemoJson },
    });

    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      // lastResultText truncated — doesn't have the full JSON
      lastResultText: shortText,
      allOutput: `${updatedLine}\n${completedLine}\n`,
      codexTurnCompleted: true,
      stderrTail: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBeUndefined();
    expect(call.logs).toContain('Full memo summary.');
  });

  it('includes recovered-agent-message-text marker in failure log when agent_message candidates exist', async () => {
    // When extraction fails but the allOutput contains agent_message envelopes,
    // the failure log must include the recovered-agent-message-text marker blocks
    // so the backend recovery path can re-attempt extraction.
    const candidateText = JSON.stringify({
      summary: 'Almost valid',
      reasoning: 'Reasoning present',
      // deliberately missing citations and tags — will fail extraction
    });
    const completedLine = JSON.stringify({
      type: 'item.completed',
      item: { type: 'agent_message', text: candidateText },
    });

    // Both runs fail to find a valid memo (missing required keys)
    vi.mocked(runClaudeCode)
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: completedLine,
        lastResultText: '',
        codexTurnCompleted: true,
        stderrTail: '',
      })
      .mockResolvedValueOnce({
        exitCode: 0,
        allOutput: completedLine,
        lastResultText: '',
        codexTurnCompleted: true,
        stderrTail: '',
      });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.logs).toContain('--- recovered-agent-message-text ---');
    expect(call.logs).toContain(candidateText);
  });
});
