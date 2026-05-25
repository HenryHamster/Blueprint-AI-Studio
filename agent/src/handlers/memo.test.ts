import { describe, it, expect, vi, beforeEach } from 'vitest';

// ── Module mocks ──────────────────────────────────────────────────────────────
vi.mock('../index.js', () => ({
  WORKSPACE: '/tmp/workspace',
  sendLog: vi.fn(),
  runClaudeCode: vi.fn().mockResolvedValue({ exitCode: 0, allOutput: '', lastResultText: '' }),
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

// ── Imports under test ────────────────────────────────────────────────────────
import { handleMemo } from './memo.js';
import { runClaudeCode, persistAndPostStatus, sendHeartbeatSync, setCurrentPromptFingerprint } from '../index.js';
import { configureAgentPermissions } from '../prompt.js';

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
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBeUndefined();
  });

  it('posts FAILED with parse_failed when neither source contains valid JSON', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'no json here',
      lastResultText: 'no json here either',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.artifact_type).toBe('ERROR_LOG');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('posts FAILED with parse_failed when JSON is missing required keys (citations, tags)', async () => {
    const incomplete = JSON.stringify({ summary: 'ok', reasoning: 'ok' });
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0, allOutput: '', lastResultText: incomplete,
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('FAILED');
    expect(call.failure_category).toBe('parse_failed');
  });

  it('includes the allOutput tail in the parse_failed error logs', async () => {
    vi.mocked(runClaudeCode).mockResolvedValue({
      exitCode: 0,
      allOutput: 'agent output without valid json',
      lastResultText: '',
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.logs).toContain('allOutput tail');
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

  it('regression_false_positive: synthesizes memo from verdict_reason without calling Claude', async () => {
    const payload = {
      reasoning_kind: 'regression_false_positive',
      source_task_id: 'task-inv-1',
      source_finding: {
        verdict_reason: 'CPU spike is a deployment artifact, not a regression.',
        investigation_summary: 'Checked the past 3 deploys; spike correlates with rollout.',
        regression_class: 'cpu_p99_spike',
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toContain('CPU spike is a deployment artifact, not a regression.');
    expect(call.logs).toContain('Checked the past 3 deploys');
  });

  it('investigation_unactionable: synthesizes memo from root_cause and recommended_fix without calling Claude', async () => {
    const payload = {
      reasoning_kind: 'investigation_unactionable',
      source_task_id: 'task-inv-2',
      source_finding: {
        root_cause: 'Missing index on user_events.created_at causes full table scans.',
        recommended_fix: 'Add CREATE INDEX CONCURRENTLY on user_events(created_at).',
        summary: 'Slow query traced to missing index.',
        outcome: 'no_actionable_fix',
      },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).not.toHaveBeenCalled();
    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.logs).toContain('Missing index on user_events.created_at');
    expect(call.logs).toContain('CREATE INDEX CONCURRENTLY');
  });

  it('falls through to Claude when reasoning_kind is not a dispatcher kind', async () => {
    const payload = {
      reasoning_kind: 'deferred_finding',
      source_finding: { title: 'Some finding' },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
  });

  it('falls through to Claude when verdict_reason is absent for regression_false_positive', async () => {
    const payload = {
      reasoning_kind: 'regression_false_positive',
      source_finding: { regression_class: 'cpu_p99_spike' },
    };
    await handleMemo(TASK_ID, payload, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    expect(runClaudeCode).toHaveBeenCalledTimes(1);
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
    });

    await handleMemo(TASK_ID, {}, BASE_TASK, GH_TOKEN, OWNER, REPO, WS, STATUS_REF());

    const call = vi.mocked(persistAndPostStatus).mock.calls[0][1];
    expect(call.status).toBe('COMPLETED');
    expect(call.artifact_type).toBeUndefined();
    expect(call.logs).toContain('direction-retired');
  });
});
