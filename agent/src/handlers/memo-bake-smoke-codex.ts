// Bake-time smoke for the runClaudeCode + Codex-extractor memo path.
//
// The dispatcher-path smoke (memo-bake-smoke.ts) hits the synthesize-
// directly short-circuit in handlers/memo.ts and never invokes
// runClaudeCode, extractCodexAgentMessageText, or the JSON extractor.
// This file covers the other arm: a memo payload whose reasoning_kind
// is not a dispatcher kind, so execution reaches runClaudeCode. We stub
// runClaudeCode via the __setRunClaudeCodeOverrideForSmoke hook in
// src/index.ts so no real Claude/Codex subprocess is spawned, and feed
// it a Codex JSONL envelope so extractCodexAgentMessageText runs
// end-to-end. The serialized memo JSON is deliberately longer than 200
// characters to lock in the regression fix for the historical Codex
// item.text 200-char truncation bug.
//
// Run from /opt/agent on the bake VM:
//   sudo -u agent -H bash -lc 'cd /opt/agent && node --import tsx ./src/handlers/memo-bake-smoke-codex.ts'

const MEMO_CONTENT_SENTINEL = 'codex-path memo smoke';

const memoObj = {
  summary:
    'Codex path memo bake smoke succeeded — extractor recovered memo JSON from a Codex JSONL envelope end-to-end.',
  reasoning:
    'This bake-time smoke proves that runClaudeCode and the Codex JSONL extractor still function end-to-end inside the baked agent VM image. The historical 200-char item.text truncation bug is exercised by keeping the serialized memo JSON above 200 characters and surfacing the recovered text through the codex-agent_message extractor source. Sentinel marker: ' +
    MEMO_CONTENT_SENTINEL,
  citations: ['task:packer-codex-smoke'],
  tags: ['bake', 'codex-extractor-path', 'smoke'],
};

const memoJsonString = JSON.stringify(memoObj);
if (memoJsonString.length <= 200) {
  throw new Error(
    `memo JSON must exceed 200 chars to exercise the Codex item.text truncation regression (got ${memoJsonString.length})`,
  );
}

const codexEnvelope = JSON.stringify({
  type: 'item.completed',
  item: { type: 'agent_message', text: memoJsonString },
});

const allOutput =
  '[codex] turn.started\n' + codexEnvelope + '\n[codex] turn.completed\n';

// Simulate the pre-fix Codex behaviour: lastResultText is truncated to 200
// characters, so the extractor MUST fall back to scanning allOutput and
// recovering the memo JSON from the Codex agent_message envelope.
const truncatedLastResultText = memoJsonString.slice(0, 200);

const indexModuleUrl = new URL('../index.ts', import.meta.url).href;
const indexMod = (await import(indexModuleUrl)) as typeof import('../index.js');
if (typeof indexMod.__setRunClaudeCodeOverrideForSmoke !== 'function') {
  throw new Error(
    'src/index.ts must export __setRunClaudeCodeOverrideForSmoke for the bake smoke to stub runClaudeCode',
  );
}

indexMod.__setRunClaudeCodeOverrideForSmoke(() =>
  Promise.resolve({
    exitCode: 0,
    allOutput,
    lastResultText: truncatedLastResultText,
  }),
);

let sawCompleted = false;
const fetchStub: typeof fetch = (url, init = {}) => {
  const target =
    typeof url === 'string'
      ? url
      : url instanceof URL
        ? url.toString()
        : url.url;
  if (target.endsWith('/api/agent/status') && init.method === 'POST') {
    const requestBody = typeof init.body === 'string' ? init.body : '{}';
    const body = JSON.parse(requestBody) as {
      status?: unknown;
      logs?: unknown;
    };
    const status = typeof body.status === 'string' ? body.status : '';
    const logs = typeof body.logs === 'string' ? body.logs : '';
    if (status !== 'COMPLETED') {
      throw new Error(
        'codex-path memo smoke: expected COMPLETED, got ' + status,
      );
    }
    if (!logs.includes(MEMO_CONTENT_SENTINEL)) {
      throw new Error(
        'codex-path memo smoke: logs missing sentinel ' +
          MEMO_CONTENT_SENTINEL +
          '; got: ' +
          logs.slice(0, 300),
      );
    }
    sawCompleted = true;
    return Promise.resolve(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  }
  return Promise.reject(new Error('codex-path memo smoke: unexpected fetch ' + target));
};

Object.defineProperty(globalThis, 'fetch', {
  value: fetchStub,
  configurable: true,
});

const handlersModuleUrl = new URL('./index.ts', import.meta.url).href;
const { handlers } = (await import(handlersModuleUrl)) as typeof import('./index.js');

// Payload deliberately omits the dispatcher synth fields AND uses a non-
// dispatcher reasoning_kind, so the synth short-circuit in handlers/memo.ts
// cannot fire and execution must reach the runClaudeCode call. See
// _synthesizeDispatcherMemo in agent/src/handlers/memo.ts for the kinds that
// route through the synth path and must NOT appear in this payload.
await handlers.memo(
  'packer-codex-smoke',
  { reasoning_kind: 'general' },
  { title: 'Codex memo bake smoke', description: '', context: {} },
  'gh',
  'owner',
  'repo',
  null,
  { value: '' },
);

// eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- sawCompleted is mutated inside the fetchStub closure; TS narrows it to `false` here.
if (!sawCompleted) {
  throw new Error('codex-path memo smoke: status fetch never invoked');
}

indexMod.__setRunClaudeCodeOverrideForSmoke(null);
console.log('PACKER_MEMO_CLAUDE_PATH_OK');
