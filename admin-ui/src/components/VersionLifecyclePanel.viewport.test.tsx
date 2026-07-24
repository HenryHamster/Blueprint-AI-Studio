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
  VersionSummaryResponse,
} from '../api';
import { fetchVersionSummary } from '../api';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost:8000',
  fetchQualificationVerdicts: vi.fn(async () => ({ verdicts: [], total: 0 })),
  fetchVersionSummary: vi.fn(async () => ({})),
  cutVersion: vi.fn(),
  triggerQualify: vi.fn(),
  setVersionPhase: vi.fn(),
  setVersionGoal: vi.fn(),
  patchAdminSettings: vi.fn(),
  setProjectPromotionEnabled: vi.fn(),
  clearDeployFailed: vi.fn(),
  // VersionLifecyclePanel statically imports the acceptance-criteria actions;
  // browser-mode ESM requires the mock factory to expose every named export the
  // subtree imports or the linker fails at import time with "does not provide an
  // export named '…'". These three were added with the acceptance-criteria
  // attestation UI and must be mocked here.
  attestAcceptanceCriterion: vi.fn(async () => ({ status: 'attested', criterion_key: 'k', coverage: 'covered' })),
  linkCriteriaToDirection: vi.fn(async () => ({ status: 'ok', direction_key: 'd', criterion_keys: [] })),
  reinstateAcceptanceCriterion: vi.fn(async () => ({ status: 'reinstated', criterion_key: 'k' })),
  // Hotfix-promote UI added getHotfixPreview/triggerHotfixPromote as static imports;
  // both must be present in the mock or browser-mode ESM link fails at import time.
  getHotfixPreview: vi.fn(async () => ({})),
  triggerHotfixPromote: vi.fn(async () => ({ status: 'ok' })),
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
        candidate_sha: 'def5678901234567890123456789012abcdef01',
        confidence: 0.92,
        threshold: 0.8,
        sub_scores: null,
        ci_resolution: null,
        started_at: '2026-05-29T10:00:00Z',
        finalized_at: '2026-05-29T11:00:00Z',
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
      candidate_sha: 'abc1234def5678901234567890123456789012ab',
      confidence: 0.87,
      threshold: 0.8,
      sub_scores: null,
      ci_resolution: { state: 'green', detail: '', resolved_sha: 'abc1234' },
      started_at: '2026-06-13T08:00:00Z',
      finalized_at: '2026-06-13T09:00:00Z',
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
        candidate_sha: 'abc1234def5678901234567890123456789012ab',
        confidence: 0.87,
        threshold: 0.8,
        sub_scores: null,
        ci_resolution: null,
        started_at: '2026-06-13T08:00:00Z',
        finalized_at: '2026-06-13T09:00:00Z',
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
      candidate_sha: 'abc1234def5678901234567890123456789012ab',
      confidence: 0.72,
      threshold: 0.8,
      sub_scores: null,
      ci_resolution: null,
      started_at: '2026-06-12T08:00:00Z',
      finalized_at: '2026-06-12T09:00:00Z',
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
      audit_task_id: 'audit-task-1',
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
        candidate_sha: 'def5678901234567890123456789012abcdef01',
        confidence: 0.92,
        threshold: 0.8,
        sub_scores: null,
        ci_resolution: null,
        started_at: '2026-05-29T10:00:00Z',
        finalized_at: '2026-05-29T11:00:00Z',
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

const EXPANDED_HISTORY_DATA: VersionLifecycleResponse = {
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
      id: 'ver-uuid-9',
      version_number: 9,
      status: 'promoted',
      opened_at: '2026-05-15T00:00:00Z',
      promoted_at: '2026-05-30T12:00:00Z',
      qualification_attempts: 2,
      last_qualification: {
        candidate_sha: 'def5678901234567890123456789012abcdef01',
        confidence: 0.92,
        threshold: 0.8,
        sub_scores: null,
        ci_resolution: null,
        started_at: '2026-05-29T10:00:00Z',
        finalized_at: '2026-05-29T11:00:00Z',
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

const EXPANDED_SUMMARY_RESPONSE: VersionSummaryResponse = {
  version_number: 9,
  phase: 'stabilize',
  status: 'promoted',
  goal: 'Ship v9 reliability improvements and complete qualification cycle',
  opened_at: '2026-05-15T00:00:00Z',
  promoted_at: '2026-05-30T12:00:00Z',
  served_at: '2026-05-30T12:10:00Z',
  phase_changed_at: '2026-05-20T08:00:00Z',
  merged_count: 23,
  acceptance_aggregate: {},
  qualification_state: null,
  serving_status: 'serving_confirmed',
  served_sha: 'def5678901234567890123456789012abcdef01',
  deploy_failure_reason: null,
  highlights: {
    headline: 'v9 promoted — 23 tasks merged, 20/23 proven, confidence 92%',
    merged_count: 23,
    proven: 20,
    total_merged_for_acceptance: 23,
    qualification_confidence: 0.92,
    qualification_threshold: 0.8,
  },
  tasks_merged: [
    {
      task_type: 'fix',
      title: 'Fix race condition in task locking under concurrent reaper sweeps',
      pr_number: 9901,
      pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/9901',
    },
    {
      task_type: 'feat',
      title: 'Add aggregate_serving_status to version history list entries',
      pr_number: 9887,
      pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/9887',
    },
    {
      task_type: 'fix',
      title: 'Resolve stranded-lane detection false positives on warm-pool restart',
      pr_number: 9872,
      pr_url: 'https://github.com/HenryHamster/BlueprintAIStudio/pull/9872',
    },
  ],
};

const SERVING_DISAGREE_DATA: VersionLifecycleResponse = {
  current_version: {
    version_number: 12,
    phase: 'stabilize',
    phase_changed_at: '2026-06-14T08:00:00Z',
    phase_reasoning: null,
    goal: null,
    opened_at: '2026-06-14T00:00:00Z',
    qualification_attempts: 1,
    merged_task_count: 5,
    in_flight_task_count: 0,
    active_candidate_sha: 'abc1234def5678901234567890123456789012ab',
    qualification_state: {
      state: 'qualifying',
      summary: 'Qualification in progress',
      serving_status: 'unconfirmed',
    },
    last_qualification: null,
    latest_audit: null,
    audit_evidence_state: 'none',
    serving_status: 'serving_confirmed',
    aggregate_serving_status: 'deploy_failed',
  },
  recent_versions: [
    {
      version_number: 11,
      status: 'promoted',
      opened_at: '2026-06-01T00:00:00Z',
      promoted_at: '2026-06-13T12:00:00Z',
      qualification_attempts: 2,
      last_qualification: null,
      serving_status: 'serving_confirmed',
      aggregate_serving_status: 'deploy_failed',
    },
  ],
  promotion_enabled: false,
  staged_directions: [],
};

// Regression coverage relocated from DashboardHome.viewport.test.tsx: the
// version-lifecycle panel moved out of DashboardHome into its own nav tab
// (#12985), so these audit/infra reason-chip assertions now live with the panel
// that actually renders them.
const AUDIT_MISSING_REASON_DATA: VersionLifecycleResponse = {
  current_version: {
    version_number: 42,
    phase: 'stabilize',
    phase_changed_at: '2026-05-20T12:00:00Z',
    phase_reasoning: null,
    goal: null,
    opened_at: '2026-05-15T00:00:00Z',
    qualification_attempts: 2,
    merged_task_count: 15,
    in_flight_task_count: 0,
    active_candidate_sha: null,
    qualification_state: null,
    last_qualification: {
      status: 'dry_run',
      confidence: 0.78,
      threshold: 0.8,
      sub_scores: null,
      ci_resolution: null,
      blocker: 'audit below threshold',
      finalized_at: '2026-05-20T11:00:00Z',
      error: null,
      consecutive_infra_errors: 0,
      audit_missing_reason: 'parseable_but_unscoreable',
      audit_reconciliation: null,
    },
    latest_audit: null,
    audit_evidence_state: 'none',
  },
  recent_versions: [],
  promotion_enabled: false,
  staged_directions: [],
};

const INFRA_STREAK_DATA: VersionLifecycleResponse = {
  current_version: {
    version_number: 42,
    phase: 'stabilize',
    phase_changed_at: '2026-05-20T12:00:00Z',
    phase_reasoning: null,
    goal: null,
    opened_at: '2026-05-15T00:00:00Z',
    qualification_attempts: 2,
    merged_task_count: 15,
    in_flight_task_count: 0,
    active_candidate_sha: null,
    qualification_state: null,
    last_qualification: {
      status: 'infra_failure',
      confidence: 0.78,
      threshold: 0.8,
      sub_scores: null,
      ci_resolution: null,
      blocker: null,
      finalized_at: '2026-05-20T11:00:00Z',
      error: 'release_audit heartbeat stale',
      consecutive_infra_errors: 3,
      audit_missing_reason: null,
      audit_reconciliation: null,
    },
    latest_audit: null,
    audit_evidence_state: 'none',
  },
  recent_versions: [],
  promotion_enabled: false,
  staged_directions: [],
};

const DIMENSION_STATES_DATA: VersionLifecycleResponse = {
  current_version: {
    version_number: 15,
    phase: 'stabilize',
    phase_changed_at: '2026-07-10T08:00:00Z',
    phase_reasoning: null,
    goal: 'v15 qualification: resolve audit failures and re-qualify',
    opened_at: '2026-07-01T00:00:00Z',
    qualification_attempts: 5,
    merged_task_count: 33,
    in_flight_task_count: 0,
    active_candidate_sha: 'abc1234def5678901234567890123456789012ab',
    qualification_state: {
      state: 'not_promoted',
      summary: 'Qualification complete — not promoted (audit below threshold)',
    },
    last_qualification: {
      candidate_sha: 'abc1234def5678901234567890123456789012ab',
      confidence: 0.68,
      threshold: 0.8,
      sub_scores: {
        audit: 0.62,
        test_erosion: 0.95,
        smoketest: 0.88,
        debt_pressure: 0.9,
      },
      ci_resolution: { state: 'green', detail: 'all checks passed', resolved_sha: 'abc1234' },
      started_at: '2026-07-10T07:00:00Z',
      finalized_at: '2026-07-10T08:00:00Z',
      status: 'not_promoted',
      blocker: 'audit_below_threshold',
      audit_missing_reason: null,
      audit_reconciliation: null,
    },
    dimension_states: [
      { key: 'audit', label: 'Audit', status: 'failed', score: 0.62, reason: 'audit_below_threshold' },
      { key: 'test_erosion', label: 'Test Erosion', status: 'passed', score: 0.95, reason: null },
      { key: 'smoketest', label: 'Smoke Test', status: 'passed', score: 0.88, reason: null },
      { key: 'ci', label: 'CI', status: 'passed', score: null, reason: null },
      { key: 'migration_identity', label: 'Migration Identity', status: 'unavailable', score: null, reason: 'heartbeat_stale' },
      { key: 'merge_grade', label: 'Merge Grade', status: 'passed', score: null, reason: null },
      { key: 'criterion_coverage', label: 'Criterion Coverage', status: 'passed', score: null, reason: null },
    ],
    latest_audit: null,
    audit_evidence_state: 'none',
  },
  recent_versions: [],
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

  test('serving disagree — badge renders when aggregate_serving_status differs from serving_status', async () => {
    await makeRender(SERVING_DISAGREE_DATA)();
    const container = host;
    if (!container) throw new Error('host not initialized');
    const badges = container.querySelectorAll('[data-testid="serving-disagree-badge"]');
    if (badges.length === 0) {
      throw new Error('Expected at least one serving-disagree-badge when aggregate and scalar serving statuses differ');
    }
  });

  test('parseable-but-unscoreable audit reason renders a readable chip', async () => {
    await makeRender(AUDIT_MISSING_REASON_DATA)();
    if (!host?.textContent?.includes('audit findings present but missing severity tags')) {
      throw new Error('Expected the parseable_but_unscoreable audit-missing-reason chip to render its readable label');
    }
  });

  test('infra streak and error render as distinct chips', async () => {
    await makeRender(INFRA_STREAK_DATA)();
    if (!host?.textContent?.includes('3 consecutive infra failures')) {
      throw new Error('Expected the consecutive-infra-failures streak chip to render');
    }
    if (!host?.textContent?.includes('release_audit heartbeat stale')) {
      throw new Error('Expected the infra-failure error text to render distinctly from the streak chip');
    }
  });

  test('dimension states — no overflow at 375px', async () => {
    await expectNoOverflowAt(375, makeRender(DIMENSION_STATES_DATA));
  });

  test('dimension states — no text truncation at 375px', async () => {
    await expectNoTextTruncationAt(375, makeRender(DIMENSION_STATES_DATA));
  });

  test('dimension states — touch targets at 375px', async () => {
    await expectMinTouchTargetAt(375, makeRender(DIMENSION_STATES_DATA), { selector: 'button, a[href]' });
  });

  test('dimension states — renders dimension-states section with failed and passed rows', async () => {
    await makeRender(DIMENSION_STATES_DATA)();
    const container = host;
    if (!container) throw new Error('host not initialized');
    const section = container.querySelector('[data-testid="dimension-states"]');
    if (!section) {
      throw new Error('Expected [data-testid="dimension-states"] to be present when dimension_states is populated');
    }
    const failedRow = section.querySelector('[data-status="failed"]');
    if (!failedRow) {
      throw new Error('Expected at least one failed dimension row inside dimension-states');
    }
    const passedRow = section.querySelector('[data-status="passed"]');
    if (!passedRow) {
      throw new Error('Expected at least one passed dimension row inside dimension-states');
    }
    const unavailableRow = section.querySelector('[data-status="unavailable"]');
    if (!unavailableRow) {
      throw new Error('Expected at least one unavailable dimension row inside dimension-states');
    }
  });

  describe('expanded VersionHistorySummary with highlights and changelog', () => {
    function makeExpandedRender() {
      return async function renderExpandedPanel() {
        vi.mocked(fetchVersionSummary).mockResolvedValue(EXPANDED_SUMMARY_RESPONSE);

        host = document.createElement('div');
        host.style.padding = '16px';
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(
            <VersionLifecyclePanel
              data={EXPANDED_HISTORY_DATA}
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

        // Click the expand button on the recent version entry to load VersionHistorySummary
        const expandBtn = host?.querySelector('button[title="What\'s new"]') as HTMLButtonElement | null;
        if (!expandBtn) throw new Error('Expand button not found — ensure EXPANDED_HISTORY_DATA has recent_versions[0].id set');
        await act(async () => {
          expandBtn.click();
        });
        // Wait for fetchVersionSummary to resolve and VersionHistorySummary to render
        await act(async () => {
          await new Promise((resolve) => setTimeout(resolve, 100));
        });
      };
    }

    test('no overflow at 375px', async () => {
      await expectNoOverflowAt(375, makeExpandedRender());
    });

    test('no text truncation at 375px', async () => {
      await expectNoTextTruncationAt(375, makeExpandedRender());
    });

    test('touch targets at 375px', async () => {
      // Check buttons only: the PR reference links in the changelog are intentionally
      // compact inline elements (30×14px) and not primary touch targets.
      await expectMinTouchTargetAt(375, makeExpandedRender(), { selector: 'button' });
    });
  });
});
