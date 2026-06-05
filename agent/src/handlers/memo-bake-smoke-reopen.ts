// Bake-time smoke for the memo handler's reopen continuation retry path.
//
// The codex-path smoke (memo-bake-smoke-codex.ts) stubs runClaudeCode with a
// single-shot override that returns valid memo JSON on the first call, so the
// reopen retry path at memo.ts:202 (memoObj === null && exitCode === 0) is
// never reached during that smoke. This file covers that arm: the first
// runClaudeCode call returns a clean exit (exitCode=0) with non-JSON narrative
// output, triggering the focused continuation prompt; the second call returns
// valid memo JSON with a distinct sentinel so the smoke can assert it
// round-trips into the COMPLETED status post.
//
// Run from /opt/agent on the bake VM:
//   sudo -u agent -H bash -lc 'cd /opt/agent && ORCHESTRATOR_URL=http://127.0.0.1:9 AGENT_TOKEN=packer-smoke node --import tsx ./src/handlers/memo-bake-smoke-reopen.ts'

const MEMO_CONTENT_SENTINEL = 'reopen-retry memo smoke';

// Call index 0: clean exit but no JSON — triggers the reopen branch.
// Call index 1: valid memo JSON containing the sentinel.
let callIndex = 0;

const retryMemoObj = {
  summary: 'Reopen retry path bake smoke succeeded end-to-end.',
  reasoning:
    'This bake-time smoke proves that the memo handler reopen continuation retry path (memo.ts line 202: memoObj === null && exitCode === 0) still fires and surfaces valid JSON into the COMPLETED status post. Sentinel marker: ' +
    MEMO_CONTENT_SENTINEL,
  citations: ['task:packer-reopen-smoke'],
  tags: ['bake', 'reopen-retry-path', 'smoke'],
};

const retryMemoJsonString = JSON.stringify(retryMemoObj);

const indexModuleUrl = new URL('../index.ts', import.meta.url).href;
const indexMod = (await import(indexModuleUrl)) as typeof import('../index.js');
if (typeof indexMod.__setRunClaudeCodeOverrideForSmoke !== 'function') {
  throw new Error(
    'src/index.ts must export __setRunClaudeCodeOverrideForSmoke for the bake smoke to stub runClaudeCode',
  );
}

indexMod.__setRunClaudeCodeOverrideForSmoke(() => {
  const idx = callIndex;
  callIndex += 1;
  if (idx === 0) {
    // First call: clean exit, narrative text only — no JSON, triggers reopen.
    const narrative =
      'The memo reasoning follows. First, we consider the overall scope of the task. ' +
      'The outcome was generally positive and the approach was sound. No JSON is included here.';
    return Promise.resolve({
      exitCode: 0,
      allOutput: narrative,
      lastResultText: narrative,
    });
  }
  // Second call (reopen): return the valid memo JSON.
  return Promise.resolve({
    exitCode: 0,
    allOutput: retryMemoJsonString,
    lastResultText: retryMemoJsonString,
  });
});

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
        'reopen-retry memo smoke: expected COMPLETED, got ' + status,
      );
    }
    if (!logs.includes(MEMO_CONTENT_SENTINEL)) {
      throw new Error(
        'reopen-retry memo smoke: logs missing sentinel ' +
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
  return Promise.reject(new Error('reopen-retry memo smoke: unexpected fetch ' + target));
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
// dispatcher reasoning_kind so the synth short-circuit in handlers/memo.ts
// cannot fire and execution must reach the runClaudeCode call. See
// _synthesizeDispatcherMemo in agent/src/handlers/memo.ts for the kinds that
// route through the synth path and must NOT appear in this payload.
await handlers.memo(
  'packer-reopen-smoke',
  { reasoning_kind: 'general' },
  { title: 'Reopen retry memo bake smoke', description: '', context: {} },
  'gh',
  'owner',
  'repo',
  null,
  { value: '' },
);

// Assert the override was invoked at least twice, proving the reopen branch
// fired a second runClaudeCode call. A value of 1 means the retry never ran.
if (callIndex < 2) {
  throw new Error(
    `reopen-retry memo smoke: runClaudeCode override was called ${callIndex} time(s); expected >= 2 (first call must trigger the reopen branch)`,
  );
}

// eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- sawCompleted is mutated inside the fetchStub closure; TS narrows it to `false` here.
if (!sawCompleted) {
  throw new Error('reopen-retry memo smoke: status fetch never invoked');
}

indexMod.__setRunClaudeCodeOverrideForSmoke(null);
console.log('PACKER_MEMO_REOPEN_RETRY_OK');
