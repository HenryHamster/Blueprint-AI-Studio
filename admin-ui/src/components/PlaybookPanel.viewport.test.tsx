import { afterEach, beforeEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { PlaybookPanel } from './PlaybookPanel';
import { expectNoOverflowAt } from '../test/viewport';

const MOCK_ENTRIES = {
  'success-create-small': {
    content: 'First-attempt success pattern for create tasks — keep scope tightly bounded to the declared cluster files and confirm all callers compile before opening the PR. Scope creep is the primary failure mode for small tasks.',
    effectiveness_score: 0.87,
    confirmed_count: 22,
    reference_count: 31,
    age_days: 14,
    stale: false,
    applicable_types: ['create', 'refactor'],
    source: 'success-pattern',
  },
  'alembic-conventions': {
    content: 'EXPECTED_SCHEMA_REVISION in backend/app/main.py is HAND-EDITED. Bump the constant in the SAME PR that adds the migration — splitting them across PRs breaks the invariant test. Never modify existing migration files.',
    effectiveness_score: 0.72,
    confirmed_count: 15,
    reference_count: 20,
    age_days: 30,
    stale: false,
    applicable_types: ['create', 'fix', 'migrate'],
    source: 'review-extracted',
  },
  'backend-test-runner-oom': {
    content: 'Agent VM is e2-standard-2 (8 GB). The full Docker+Postgres pytest suite OOMs (exit 137). Always invoke bash scripts/test-backend.sh --standalone --no-postgres for pre-PR smoke.',
    effectiveness_score: 0.65,
    confirmed_count: 8,
    reference_count: 12,
    age_days: 5,
    stale: false,
    applicable_types: ['fix'],
    source: 'auto-generated',
  },
  'common-patterns': {
    content: 'task.first_payload is the standard accessor for the first payload element (NOT (task.payloads or [{}])[0]). PROJECT-ACCESS ENFORCEMENT: every router function that takes project_id or task_id must call the helper at the top before any business logic.',
    effectiveness_score: 0.58,
    confirmed_count: 5,
    reference_count: 18,
    age_days: 60,
    stale: true,
    applicable_types: ['create', 'fix', 'refactor', 'debug'],
    source: 'first-attempt-pattern',
  },
  'prompt-drift-inventory': {
    content: 'Direction prompt sweep/drift: shipped guards cover TASK_TYPE_HINTS parity, TASK_TYPES_WITHOUT_HINT allowlisting, helper-script scans, CODE_CHANGE_HELPER_GUIDANCE vocabulary. Remaining gaps: runtime prompt fingerprint telemetry.',
    effectiveness_score: 0.12,
    confirmed_count: 0,
    reference_count: 11,
    age_days: 22,
    stale: false,
    applicable_types: ['research', 'plan'],
    source: 'review-extracted',
  },
  'directory-layout': {
    content: 'backend/ — FastAPI orchestrator (Python 3.11). admin-ui/ — React 19.2 + Vite 6.2 + Tailwind 4.2 admin dashboard. agent/ — Node 20 TypeScript runtime dispatching 18 task types via handlers/index.ts mapping.',
    effectiveness_score: 0.43,
    confirmed_count: 3,
    reference_count: 7,
    age_days: 90,
    stale: true,
    applicable_types: ['create', 'fix', 'refactor', 'research', 'plan'],
    source: 'auto-generated',
  },
};

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () =>
    new Response(
      JSON.stringify({ entries: MOCK_ENTRIES, count: Object.keys(MOCK_ENTRIES).length }),
      { status: 200 },
    )
  ),
  API_BASE_URL: '',
}));

describe('PlaybookPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    localStorage.setItem('blueprint-admin:playbookPanelCollapsed', 'false');
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    localStorage.removeItem('blueprint-admin:playbookPanelCollapsed');
  });

  test('renders without horizontal overflow at 375px with dense entries', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<PlaybookPanel selectedProjectId="proj-1" />);
      });
      await act(async () => { await new Promise((resolve) => setTimeout(resolve, 50)); });
    });
  });
});
