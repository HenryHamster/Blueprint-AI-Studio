// Bake-time smoke for the runClaudeCode + lastResultText primary-extractor
// memo path.
//
// The dispatcher-path smoke (memo-bake-smoke.ts) short-circuits through
// _synthesizeDispatcherMemo and never invokes runClaudeCode. The codex-path
// smoke (memo-bake-smoke-codex.ts) deliberately truncates lastResultText to
// force the Codex JSONL fallback. Neither exercises the most common
// production outcome: lastResultText contains a clean, well-formed memo JSON
// object and the very first extractor source (lastResultText) succeeds,
// without the handler ever touching the allOutput or Codex-envelope
// fallbacks.
//
// This smoke covers that happy path. We stub runClaudeCode via the
// __setRunClaudeCodeOverrideForSmoke hook in src/index.ts so no real
// Claude/Codex subprocess is spawned, return exitCode=0 with a clean memo
// JSON in lastResultText and an empty allOutput, and assert the fetch stub
// observes status=COMPLETED with the sentinel preserved end-to-end.
//
// Run from /opt/agent on the bake VM:
//   sudo -u agent -H bash -lc 'cd /opt/agent && node --import tsx ./src/handlers/memo-bake-smoke-primary.ts'

const MEMO_CONTENT_SENTINEL = 'primary-path memo smoke';

const memoObj = {
  summary:
    'Primary path memo bake smoke succeeded — extractor recovered memo JSON directly from lastResultText.',
  reasoning:
    'This bake-time smoke proves that runClaudeCode and the primary lastResultText extractor source still function end-to-end inside the baked agent VM image. The most common production outcome is that lastResultText already contains a full memo JSON; this smoke locks that path in. Sentinel marker: ' +
    MEMO_CONTENT_SENTINEL,
  citations: ['task:packer-primary-smoke'],
  tags: ['bake', 'primary-extractor-path', 'smoke'],
};

const memoJsonString = JSON.stringify(memoObj);

// allOutput is deliberately empty so the only viable extractor source is
// lastResultText. If the handler ever tried to use allOutput first, the
// JSON extractor would fail and the smoke would catch the regression.
const allOutput = '';

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
    lastResultText: memoJsonString,
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
        'primary-path memo smoke: expected COMPLETED, got ' + status,
      );
    }
    if (!logs.includes(MEMO_CONTENT_SENTINEL)) {
      throw new Error(
        'primary-path memo smoke: logs missing sentinel ' +
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
  return Promise.reject(new Error('primary-path memo smoke: unexpected fetch ' + target));
};

Object.defineProperty(globalThis, 'fetch', {
  value: fetchStub,
  configurable: true,
});

const handlersModuleUrl = new URL('./index.ts', import.meta.url).href;
const { handlers } = (await import(handlersModuleUrl)) as typeof import('./index.js');

// This payload bypasses the dispatcher synth short-circuit (below), so
// handleMemo reaches configureAgentPermissions — which requires the
// task-capability contract the real agent loads at VM startup. Load the
// bake-smoke stand-in first or classification throws (fail explicit).
const contractModuleUrl = new URL('./bake-smoke-capability-contract.ts', import.meta.url).href;
const contractMod = (await import(contractModuleUrl)) as typeof import('./bake-smoke-capability-contract.js');
contractMod.loadBakeSmokeCapabilityContract();

// Payload deliberately omits the dispatcher synth fields AND uses a non-
// dispatcher reasoning_kind, so the synth short-circuit in handlers/memo.ts
// cannot fire and execution must reach the runClaudeCode call. See
// _synthesizeDispatcherMemo in agent/src/handlers/memo.ts for the kinds that
// route through the synth path and must NOT appear in this payload.
await handlers.memo(
  'packer-primary-smoke',
  { reasoning_kind: 'general' },
  { title: 'Primary memo bake smoke', description: '', context: {} },
  'gh',
  'owner',
  'repo',
  null,
  { value: '' },
);

// eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- sawCompleted is mutated inside the fetchStub closure; TS narrows it to `false` here.
if (!sawCompleted) {
  throw new Error('primary-path memo smoke: status fetch never invoked');
}

indexMod.__setRunClaudeCodeOverrideForSmoke(null);
console.log('PACKER_MEMO_PRIMARY_PATH_OK');
