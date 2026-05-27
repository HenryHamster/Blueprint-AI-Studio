// Bake-time smoke for the investigation_unactionable branch of
// _synthesizeDispatcherMemo in handlers/memo.ts.
//
// The original dispatcher-path smoke (memo-bake-smoke.ts) only exercises
// the regression_false_positive arm of the synth short-circuit. The
// investigation_unactionable arm has distinct field-extraction logic
// (root_cause, recommended_fix, summary, outcome) and is emitted in
// production by backend/app/task_completion.py, so a regression there
// could ship inside a baked image without the bake catching it. This
// smoke locks the second arm into the bake gate.
//
// Run from /opt/agent on the bake VM:
//   sudo -u agent -H bash -lc 'cd /opt/agent && node --import tsx ./src/handlers/memo-bake-smoke-investigation.ts'

const ROOT_CAUSE_SENTINEL = 'investigation-unactionable bake smoke sentinel';
const RECOMMENDED_FIX_SENTINEL = 'investigation-unactionable bake smoke fix';
const SUMMARY_SENTINEL = 'investigation-unactionable bake smoke summary';
const OUTCOME_SENTINEL = 'investigation-unactionable-bake-outcome';

const memoTaskPayload = {
  reasoning_kind: 'investigation_unactionable',
  source_task_id: 'task-investigation-smoke',
  source_finding: {
    root_cause: ROOT_CAUSE_SENTINEL,
    recommended_fix: RECOMMENDED_FIX_SENTINEL,
    summary: SUMMARY_SENTINEL,
    outcome: OUTCOME_SENTINEL,
  },
} satisfies Record<string, unknown>;

const memoTask = {
  title: 'Investigation memo smoke',
  description: '',
  context: {},
} satisfies Record<string, unknown>;

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
        'investigation-unactionable memo smoke: expected COMPLETED, got ' + status,
      );
    }
    // The investigation_unactionable synth branch emits a `Root cause: ...`
    // prefix in the reasoning field — this prefix is unique to that branch
    // (regression_false_positive emits `Verdict reason: ...` instead), so its
    // presence proves the correct dispatcher arm fired.
    if (!logs.includes('Root cause:')) {
      throw new Error(
        'investigation-unactionable memo smoke: logs missing `Root cause:` prefix; got: ' +
          logs.slice(0, 300),
      );
    }
    // Custom payload sentinel: round-trips the embedded root_cause text
    // into the memo's reasoning JSON and back through the COMPLETED logs.
    if (!logs.includes(ROOT_CAUSE_SENTINEL)) {
      throw new Error(
        'investigation-unactionable memo smoke: logs missing root_cause sentinel ' +
          ROOT_CAUSE_SENTINEL +
          '; got: ' +
          logs.slice(0, 300),
      );
    }
    if (!logs.includes(RECOMMENDED_FIX_SENTINEL)) {
      throw new Error(
        'investigation-unactionable memo smoke: logs missing recommended_fix sentinel ' +
          RECOMMENDED_FIX_SENTINEL +
          '; got: ' +
          logs.slice(0, 300),
      );
    }
    if (!logs.includes(OUTCOME_SENTINEL)) {
      throw new Error(
        'investigation-unactionable memo smoke: logs missing outcome sentinel ' +
          OUTCOME_SENTINEL +
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
  return Promise.reject(
    new Error('investigation-unactionable memo smoke: unexpected fetch ' + target),
  );
};

Object.defineProperty(globalThis, 'fetch', {
  value: fetchStub,
  configurable: true,
});

const handlersModuleUrl = new URL('./index.ts', import.meta.url).href;
const { handlers } = (await import(handlersModuleUrl)) as typeof import('./index.js');

await handlers.memo(
  'packer-memo-investigation-smoke',
  memoTaskPayload,
  memoTask,
  'gh',
  'owner',
  'repo',
  null,
  { value: '' },
);

// eslint-disable-next-line @typescript-eslint/no-unnecessary-condition -- sawCompleted is mutated inside the fetchStub closure; TS narrows it to `false` here.
if (!sawCompleted) {
  throw new Error('investigation-unactionable memo smoke: status fetch never invoked');
}

console.log('PACKER_MEMO_INVESTIGATION_OK');
