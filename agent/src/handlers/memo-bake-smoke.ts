const PACKER_MEMO_LOG_FRAGMENT = 'stale memo runtime';

const memoTaskPayload = {
  reasoning_kind: 'regression_false_positive',
  source_task_id: 'task-123',
  source_finding: {
    verdict_reason: PACKER_MEMO_LOG_FRAGMENT,
    investigation_summary: 'smoke check',
    regression_class: 'memo',
  },
} satisfies Record<string, unknown>;

const memoTask = {
  title: 'Memo smoke',
  description: '',
  context: {},
} satisfies Record<string, unknown>;

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
    if (
      status !== 'COMPLETED' ||
      !logs.includes(PACKER_MEMO_LOG_FRAGMENT)
    ) {
      throw new Error(
        `unexpected status payload: ${status} ${logs.slice(0, 200)}`,
      );
    }
    return Promise.resolve(
      new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    );
  }
  return Promise.reject(new Error(`unexpected fetch: ${target}`));
};

Object.defineProperty(globalThis, 'fetch', {
  value: fetchStub,
  configurable: true,
});

const handlersModuleUrl = new URL('./index.ts', import.meta.url).href;
const { handlers } = (await import(handlersModuleUrl)) as typeof import('./index.js');

await handlers.memo(
  'packer-memo-smoke',
  memoTaskPayload,
  memoTask,
  'gh',
  'owner',
  'repo',
  null,
  { value: '' },
);
