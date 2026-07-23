import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { TaskFixLoopPanel } from './TaskFixLoopPanel';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response(JSON.stringify({
    task_id: 'task-1',
    fix_loop_history: {
      lint: Array.from({ length: 5 }, (_, i) => ({ loop: 'lint', iteration: i + 1, fingerprint_hash: `lintfingerprint${i + 1}abcdef1234567890`, error_tail: `Lint pass ${i + 1} still repeats a long error tail that should wrap cleanly in a narrow viewport without forcing horizontal scrolling.`, change_summary: `lint change summary ${i + 1} with enough descriptive text to pressure the row layout`, exit_reason: i === 4 ? 'passed' : 'similar_error', source: i === 0 ? 'trace_synthesized' : 'live', started_at: `2026-05-06T00:0${i}:00Z`, ended_at: `2026-05-06T00:0${i}:45Z` })),
      test: Array.from({ length: 4 }, (_, i) => ({ loop: 'test', iteration: i + 1, fingerprint_hash: `testfingerprint${i + 1}abcdef1234567890`, error_tail: `Test loop attempt ${i + 1} includes a dense stack trace fragment and multi-line notes so the viewport test sees realistic pressure.`, change_summary: `test iteration ${i + 1} summary with extra words to ensure wrapping`, exit_reason: i === 3 ? 'max_attempts' : 'diff_revert', started_at: `2026-05-06T01:0${i}:00Z`, ended_at: `2026-05-06T01:0${i}:32Z` })),
      check: Array.from({ length: 3 }, (_, i) => ({ loop: 'check', iteration: i + 1, fingerprint_hash: `checkfingerprint${i + 1}abcdef1234567890`, error_tail: `Check iteration ${i + 1} keeps a long diagnostic tail to exercise the compact row layout at mobile width.`, change_summary: `check summary ${i + 1} with sufficient length to require wrapping`, exit_reason: i === 2 ? 'runaway_detected' : 'oscillation', started_at: `2026-05-06T02:0${i}:00Z`, ended_at: `2026-05-06T02:0${i}:19Z` })),
    },
    summary: { lint: { attempts: 5, last_exit_reason: 'passed' }, test: { attempts: 4, last_exit_reason: 'max_attempts' }, check: { attempts: 3, last_exit_reason: 'runaway_detected' } },
  }), { status: 200 })),
}));

describe('TaskFixLoopPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => { if (root) { const r = root; act(() => r.unmount()); } root = null; host?.remove(); host = null; });
  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => { root?.render(<TaskFixLoopPanel taskId="task-1" fixLoopAbandoned={{ loop: 'check', exit_reason: 'divergence_detected', at: '2026-05-06T03:00:00Z' }} />); });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});
