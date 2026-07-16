import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../index.js', () => ({
  normalizeClaudeConfigContent: vi.fn((_path: string, content: string) => content),
  sendLog: vi.fn(),
  runClaudeCode: vi.fn(),
}));

vi.mock('../telemetry.js', () => ({
  buildPromptSizeTelemetry: vi.fn(),
  getLlmTelemetry: vi.fn(),
  recordFormatRepairTokens: vi.fn(),
}));

vi.mock('../prompt.js', () => ({
  DEFAULT_TOOL_GUIDANCE: '',
  SHARED_LIFECYCLE_REMAINDER: '',
  formatSkills: vi.fn(() => ''),
  formatProjectTools: vi.fn(() => ''),
  writeSkillHelpers: vi.fn(),
}));

import { attemptStructuredRepair } from './structured-repair.js';
import { getLlmTelemetry, recordFormatRepairTokens } from '../telemetry.js';

const WS = null;
const STATUS_REF = () => ({ value: '' });

beforeEach(() => {
  vi.clearAllMocks();
});

describe('attemptStructuredRepair', () => {
  it('calls recordFormatRepairTokens with delta from getLlmTelemetry snapshots', async () => {
    const mockRun = vi.fn().mockResolvedValue({
      exitCode: 0, allOutput: '{"result":"ok"}', lastResultText: '{"result":"ok"}',
    });
    vi.mocked(getLlmTelemetry)
      .mockReturnValueOnce({ llm_call_count: 1, total_input_tokens: 100, total_output_tokens: 50, total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0, viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0, prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null })
      .mockReturnValueOnce({ llm_call_count: 2, total_input_tokens: 300, total_output_tokens: 120, total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0, viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0, prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null });

    await attemptStructuredRepair(
      'prior text', '{"result":"..."}', 'repair-status',
      WS, STATUS_REF(), undefined, undefined, [],
      mockRun, vi.fn(),
    );

    // No schemaLabel passed → recorded without per-schema attribution.
    expect(recordFormatRepairTokens).toHaveBeenCalledWith(200, 70, undefined);
  });

  it('forwards schemaLabel to recordFormatRepairTokens so callers do not double-record', async () => {
    const mockRun = vi.fn().mockResolvedValue({
      exitCode: 0, allOutput: '{"result":"ok"}', lastResultText: '{"result":"ok"}',
    });
    vi.mocked(getLlmTelemetry)
      .mockReturnValueOnce({ llm_call_count: 1, total_input_tokens: 100, total_output_tokens: 50, total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0, viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0, prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null })
      .mockReturnValueOnce({ llm_call_count: 2, total_input_tokens: 300, total_output_tokens: 120, total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0, viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0, prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null });

    await attemptStructuredRepair(
      'prior text', '{"result":"..."}', 'repair-status',
      WS, STATUS_REF(), undefined, undefined, [],
      mockRun, vi.fn(), 'strategic',
    );

    // The helper is the SINGLE recorder; it carries the caller's schemaLabel so
    // callers must not wrap it with their own getLlmTelemetry/recordFormatRepairTokens.
    expect(recordFormatRepairTokens).toHaveBeenCalledTimes(1);
    expect(recordFormatRepairTokens).toHaveBeenCalledWith(200, 70, 'strategic');
  });

  it('returns the run result from runFn', async () => {
    const expected = { exitCode: 0, allOutput: 'foo', lastResultText: 'bar' };
    const mockRun = vi.fn().mockResolvedValue(expected);
    vi.mocked(getLlmTelemetry)
      .mockReturnValue({ llm_call_count: 0, total_input_tokens: 0, total_output_tokens: 0, total_cache_creation_input_tokens: 0, total_cache_read_input_tokens: 0, viewport_screenshot_forwarded_count: 0, num_turns: 0, total_tool_result_bytes: 0, prompt_size_bytes: null, prompt_section_count: null, prompt_section_sizes: null });

    const result = await attemptStructuredRepair(
      'prior', 'schema', 'status', WS, STATUS_REF(), undefined, undefined, [],
      mockRun, vi.fn(),
    );

    expect(result).toEqual(expected);
  });
});
