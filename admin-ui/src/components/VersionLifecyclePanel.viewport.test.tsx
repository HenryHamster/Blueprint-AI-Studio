import { afterEach, beforeEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { VersionLifecyclePanel } from './VersionLifecyclePanel';
import {
  expectMinTouchTargetAt,
  expectNoOverflowAt,
  expectNoTextTruncationAt,
} from '../test/viewport';
import type {
  VersionLifecycleResponse,
} from '../api';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
  fetchQualificationVerdicts: vi.fn(async () => ({ verdicts: [], total: 0 })),
  cutVersion: vi.fn(),
  triggerQualify: vi.fn(),
  setVersionPhase: vi.fn(),
  setVersionGoal: vi.fn(),
  patchAdminSettings: vi.fn(),
  setProjectPromotionEnabled: vi.fn(),
  clearDeployFailed: vi.fn(),
}));

const QUALIFYING_DATA: VersionLifecycleResponse = {
  current_version: {
    version_number: 10,
    phase: 'stabilize',
    phase_changed_at: '2026-06-10T08:00:00Z',
    phase_reasoning: 'operator advance',
    goal: 'Stabilize v10 by completing the qualification cycle and promoting to main',
    opened_at: '2026-06-01T00:00:00Z',
    qualification_attempts: 3,
    merged_task_count: 47,
    in_flight_task_count: 2,
    active_candidate_sha: 'abc1234def5678901234567890123456789012ab',
    qualification_state: {
      state: 'qualifying',
      summary: 'Qualification in progress — audit task spawned, awaiting verdict',
    },
    last_qualification: null,
    latest_audit: null,
    audit_evidence_state: 'none',
  },
  recent_versions: [
    {
      version_number: 9,
      status: 'promoted',
      opened_at: '2026-05-15T00:00:00Z',
      promoted_at: '2026-05-30T12:00:00Z',
      qualification_attempts: 2,
      last_qualification: {
        id: 'verdict-9',
        candidate_sha: 'def5678901234567890123456789012abcdef01',
        confidence: 0.92,
        threshold: 0.8,
        sub_scores: null,
        ci_resolution: null,
        would_promote: true,
        promotion_enabled: true,
        terminal_status: 'promoted',
        started_at: '2026-05-29T10:00:00Z',
        finalized_at: '2026-05-29T11:00:00Z',
        created_at: '2026-05-29T10:00:00Z',
        status: 'promoted',
        blocker: null,
        audit_missing_reason: null,
        audit_reconciliation: null,
      },
      serving_status: 'serving_confirmed',
    },
  ],
  promotion_enabled: false,
  staged_directions: [],
};

const PROMOTION_CHECK_WAIT_DATA: VersionLifecycleResponse = {
  current_version: {
    version_number: 10,
    phase: 'stabilize',
    phase_changed_at: '2026-06-10T08:00:00Z',
    phase_reasoning: null,
    goal: null,
    opened_at: '2026-06-01T00:00:00Z',
    qualification_attempts: 5,
    merged_task_count: 52,
    in_flight_task_count: 0,
    active_candidate_sha: 'abc1234def5678901234567890123456789012ab',
    qualification_state: {
      state: 'promotion_check_wait',
      summary: 'Migration-carrying promotion PR open; waiting for required CI checks to pass',
      promotion_pr_number: 9876,
      promotion_pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/9876',
      migration_carrying: true,
      promotion_age_seconds: 1800,
      promotion_timeout_seconds: 7200,
      check_snapshot: {
        state: 'pending',
        classified_at: '2026-06-13T10:00:00Z',
        head_sha: 'abc1234def5678901234567890123456789012ab',
        total_checks: 4,
        reason: 'checks_pending',
        detail: 'Waiting for backend-ci, frontend-lint, integration-tests, migration-safety',
        checks: [
          { name: 'backend-ci', status: 'in_progress', conclusion: null },
          { name: 'frontend-lint', status: 'completed', conclusion: 'success' },
          { name: 'integration-tests', status: 'in_progress', conclusion: null },
          { name: 'migration-safety', status: 'queued', conclusion: null },
        ],
        truncated: false,
      },
    },
    last_qualification: {
      id: 'verdict-10a',
      candidate_sha: 'abc1234def5678901234567890123456789012ab',
      confidence: 0.87,
      threshold: 0.8,
      sub_scores: null,
      ci_resolution: { state: 'green', detail: null, resolved_sha: 'abc1234' },
      would_promote: true,
      promotion_enabled: true,
      terminal_status: 'promoted',
      started_at: '2026-06-13T08:00:00Z',
      finalized_at: '2026-06-13T09:00:00Z',
      created_at: '2026-06-13T08:00:00Z',
      status: 'promoted',
      blocker: null,
      audit_missing_reason: null,
      audit_reconciliation: null,
    },
    latest_audit: null,
    audit_evidence_state: 'none',
  },
  recent_versions: [],
  promotion_enabled: true,
  staged_directions: [],
};

const SERVING_CONFIRMED_DATA: VersionLifecycleResponse = {
  current_version: {
    version_number: 11,
    phase: 'build',
    phase_changed_at: '2026-06-13T12:00:00Z',
    phase_reasoning: null,
    goal: null,
    opened_at: '2026-06-13T12:00:00Z',
    qualification_attempts: 0,
    merged_task_count: 1,
    in_flight_task_count: 3,
    active_candidate_sha: null,
    qualification_state: null,
    last_qualification: null,
    latest_audit: null,
    audit_evidence_state: 'none',
  },
  recent_versions: [
    {
      version_number: 10,
      status: 'promoted',
      opened_at: '2026-06-01T00:00:00Z',
      promoted_at: '2026-06-13T12:00:00Z',
      qualification_attempts: 5,
      last_qualification: {
        id: 'verdict-10b',
        candidate_sha: 'abc1234def5678901234567890123456789012ab',
        confidence: 0.87,
        threshold: 0.8,
        sub_scores: null,
        ci_resolution: null,
        would_promote: true,
        promotion_enabled: true,
        terminal_status: 'promoted',
        started_at: '2026-06-13T08:00:00Z',
        finalized_at: '2026-06-13T09:00:00Z',
        created_at: '2026-06-13T08:00:00Z',
        status: 'promoted',
        blocker: null,
        audit_missing_reason: null,
        audit_reconciliation: null,
      },
      serving_status: 'serving_confirmed',
      served_sha: 'abc1234def5678901234567890123456789012ab',
      served_at: '2026-06-13T12:10:00Z',
    },
  ],
  promotion_enabled: true,
  staged_directions: [],
};

const HISTORICAL_VERDICT_DATA: VersionLifecycleResponse = {
  current_version: {
    version_number: 10,
    phase: 'stabilize',
    phase_changed_at: '2026-06-10T08:00:00Z',
    phase_reasoning: null,
    goal: 'v10 stabilization: all audit findings resolved, migration safety verified',
    opened_at: '2026-06-01T00:00:00Z',
    qualification_attempts: 4,
    merged_task_count: 47,
    in_flight_task_count: 0,
    active_candidate_sha: 'abc1234def5678901234567890123456789012ab',
    qualification_state: {
      state: 'not_promoted',
      summary: 'Qualification complete — not promoted (confidence below threshold)',
    },
    last_qualification: {
      id: 'verdict-10c',
      candidate_sha: 'abc1234def5678901234567890123456789012ab',
      confidence: 0.72,
      threshold: 0.8,
      sub_scores: null,
      ci_resolution: null,
      would_promote: false,
      promotion_enabled: false,
      terminal_status: 'not_promoted',
      started_at: '2026-06-12T08:00:00Z',
      finalized_at: '2026-06-12T09:00:00Z',
      created_at: '2026-06-12T08:00:00Z',
      status: 'not_promoted',
      blocker: null,
      audit_missing_reason: null,
      audit_reconciliation: {
        model_reported_score: 0.78,
        findings_derived_score: 0.65,
        score_delta: 0.13,
        excluded_findings_count: 2,
      },
      audit_findings: [
        { severity: 'high', summary: 'Missing input validation in task payload handler — could allow injection of arbitrary fields' },
        { severity: 'medium', summary: 'Admin endpoint lacks rate limiting; repeated calls could exhaust DB connection pool' },
      ],
      migration_safety: 'safe',
      migration_safety_detail: 'No data-destructive operations detected; column additions only',
      test_erosion_detail: { eroded_count: 3, total_tests: 847, erosion_rate: 0.0035 },
    },
    latest_audit: {
      task_status: 'COMPLETED',
      report: {
        findings: [
          { severity: 'high', summary: 'Missing input validation in task payload handler — could allow injection of arbitrary fields' },
          { severity: 'medium', summary: 'Admin endpoint lacks rate limiting; repeated calls could exhaust DB connection pool' },
        ],
      },
      candidate_sha: 'abc1234def5678901234567890123456789012ab',
      created_at: '2026-06-12T08:00:00Z',
    },
    audit_evidence_state: 'current',
  },
  recent_versions: [
    {
      version_number: 9,
      status: 'promoted',
      opened_at: '2026-05-15T00:00:00Z',
      promoted_at: '2026-05-30T12:00:00Z',
      qualification_attempts: 2,
      last_qualification: {
        id: 'verdict-9',
        candidate_sha: 'def5678901234567890123456789012abcdef01',
        confidence: 0.92,
        threshold: 0.8,
        sub_scores: null,
        ci_resolution: null,
        would_promote: true,
        promotion_enabled: true,
        terminal_status: 'promoted',
        started_at: '2026-05-29T10:00:00Z',
        finalized_at: '2026-05-29T11:00:00Z',
        created_at: '2026-05-29T10:00:00Z',
        status: 'promoted',
        blocker: null,
        audit_missing_reason: null,
        audit_reconciliation: null,
      },
      serving_status: 'serving_confirmed',
    },
  ],
  promotion_enabled: false,
  staged_directions: [],
};

describe('VersionLifecyclePanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.clearAllMocks();
  });

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  function makeRender(data: VersionLifecycleResponse) {
    return async function renderPanel() {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <VersionLifecyclePanel
            data={data}
            loading={false}
            projectId="proj-1"
            onRefresh={() => {}}
            userRole="admin"
          />,
        );
      });
      await act(async () => {
        await new Promise((resolve) => setTimeout(resolve, 50));
      });
    };
  }

  test('qualifying state — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, makeRender(QUALIFYING_DATA));
  });

  test('qualifying state — no text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, makeRender(QUALIFYING_DATA));
  });

  test('qualifying state — touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, makeRender(QUALIFYING_DATA), { selector: 'button, a[href]' });
  });

  test('promotion_check_wait with check snapshot — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, makeRender(PROMOTION_CHECK_WAIT_DATA));
  });

  test('promotion_check_wait with check snapshot — no text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, makeRender(PROMOTION_CHECK_WAIT_DATA));
  });

  test('promotion_check_wait with check snapshot — touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, makeRender(PROMOTION_CHECK_WAIT_DATA), { selector: 'button, a[href]' });
  });

  test('serving_confirmed recent version — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, makeRender(SERVING_CONFIRMED_DATA));
  });

  test('serving_confirmed recent version — no text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, makeRender(SERVING_CONFIRMED_DATA));
  });

  test('serving_confirmed recent version — touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, makeRender(SERVING_CONFIRMED_DATA), { selector: 'button, a[href]' });
  });

  test('historical verdict with durable evidence — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, makeRender(HISTORICAL_VERDICT_DATA));
  });

  test('historical verdict with durable evidence — no text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, makeRender(HISTORICAL_VERDICT_DATA));
  });

  test('historical verdict with durable evidence — touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, makeRender(HISTORICAL_VERDICT_DATA), { selector: 'button, a[href]' });
  });
});
