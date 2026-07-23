import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { describe, expect, test, vi } from 'vitest';
import {
  PostPromotionOutcomeBadge,
  OutOfBandPromotionBadge,
  PromotionPolicyBadge,
  QualSummary,
  CiBadge,
  ManagedCheckWaitSnapshotDetails,
  AuditFindingsList,
  HistoricalAuditContext,
  AuditReconciliationRow,
  DurableQualificationEvidenceRow,
  PromotionReadinessSummary,
  PinnedServingVersionCallout,
  StagedDirectionsSummary,
  CutConsequences,
  ChallengedVsUnchallengedSummary,
  VersionHighlightsLine,
  VersionHistorySummary,
  VersionLifecycleRefreshBanner,
  DimensionBreakdown,
  ConfidenceSparkline,
  ServingStatusBadge,
  ArtifactEvidenceList,
  AuditProvenance,
  formatPromotionFreshnessContext,
  stagedActivatedByCut,
  type DurableQualificationEvidence,
} from './VersionLifecyclePanelPrimitives';
import type {
  OutOfBandPromotion, VersionQualification, CiResolution, QualificationState,
  LatestAudit, AuditReconciliation, PromotionReadiness,
  RecentVersion, StagedDirection, CurrentVersion, ChallengedVsUnchallengedTelemetry,
  VersionSummaryHighlights, DimensionState, QualificationVerdictRecord,
  ReleaseArtifactEvidence, AgentServingShasResponse,
} from '../api';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  getIdTokenAfterAuthRestore: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    authGet: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
    apiFetch: vi.fn(async () => new Response(JSON.stringify({}), { status: 200 })),
    API_BASE_URL: 'http://localhost:8000',
    fetchVersionSummary: vi.fn(async () => ({
      version_id: 'ver-1',
      highlights: {
        headline: 'Performance improvements',
        merged_count: 5,
        proven: 3,
        total_merged_for_acceptance: 4,
      },
      phase: 'active',
      status: 'current',
      tasks_merged: [],
      acceptance_aggregate: null,
    })),
  });
});

// ---------------------------------------------------------------------------
// Render helper
// ---------------------------------------------------------------------------

function render(element: React.ReactElement): HTMLElement {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  act(() => { root.render(element); });
  return host;
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('PostPromotionOutcomeBadge', () => {
  test('renders serving_confirmed', () => {
    const host = render(<PostPromotionOutcomeBadge outcome="serving_confirmed" />);
    expect(host.textContent).toContain('✓ serving');
  });

  test('renders deploy_failed', () => {
    const host = render(<PostPromotionOutcomeBadge outcome="deploy_failed" />);
    expect(host.textContent).toContain('⚠ deploy failed');
  });

  test('renders awaiting outcome for null', () => {
    const host = render(<PostPromotionOutcomeBadge outcome={null} />);
    expect(host.textContent).toContain('awaiting outcome');
  });

  test('renders awaiting outcome for undefined', () => {
    const host = render(<PostPromotionOutcomeBadge outcome={undefined} />);
    expect(host.textContent).toContain('awaiting outcome');
  });

  test('renders awaiting outcome for unknown value', () => {
    const host = render(<PostPromotionOutcomeBadge outcome="unknown_status" />);
    expect(host.textContent).toContain('awaiting outcome');
  });
});

describe('OutOfBandPromotionBadge', () => {
  const oob: OutOfBandPromotion = {
    candidate_sha: 'abc123def456',
    promoted_sha: '789012345678',
    recorded_at: '2026-07-01T12:00:00Z',
    source: 'operator-override',
  };

  test('renders SHA range and timestamp', () => {
    const host = render(<OutOfBandPromotionBadge oob={oob} />);
    expect(host.textContent).toContain('promoted out-of-band');
    expect(host.textContent).toContain('abc123d');
    expect(host.textContent).toContain('7890123');
  });
});

describe('PromotionPolicyBadge', () => {
  test('renders nothing when source is null', () => {
    const host = render(<PromotionPolicyBadge source={null} />);
    expect(host.textContent).toBe('');
  });

  test('renders runtime_config source', () => {
    const host = render(<PromotionPolicyBadge source="runtime_config" />);
    expect(host.textContent).toContain('runtime_config');
  });

  test('renders yaml_file source', () => {
    const host = render(<PromotionPolicyBadge source="yaml_file" />);
    expect(host.textContent).toContain('yaml_file');
  });

  test('renders hardcoded_defaults source', () => {
    const host = render(<PromotionPolicyBadge source="hardcoded_defaults" />);
    expect(host.textContent).toContain('hardcoded_defaults');
  });

  test('renders error fallback', () => {
    const host = render(<PromotionPolicyBadge source="runtime_config" error="Config file not found" />);
    expect(host.textContent).toContain('Config file not found');
  });
});

describe('QualSummary', () => {
  test('renders dash for null', () => {
    const host = render(<QualSummary q={null} />);
    expect(host.textContent).toContain('—');
  });

  test('renders progress for pending', () => {
    const q: VersionQualification = { status: 'pending' } as VersionQualification;
    const host = render(<QualSummary q={q} />);
    expect(host.textContent).toContain('qualification in progress');
  });

  test('renders progress for qualifying', () => {
    const q: VersionQualification = { status: 'qualifying' } as VersionQualification;
    const host = render(<QualSummary q={q} />);
    expect(host.textContent).toContain('qualification in progress');
  });

  test('renders confidence and threshold', () => {
    const q: VersionQualification = { status: 'promoted', confidence: 0.85, threshold: 0.7 } as VersionQualification;
    const host = render(<QualSummary q={q} />);
    expect(host.textContent).toContain('85%');
    expect(host.textContent).toContain('70%');
  });

  test('renders status keyed color', () => {
    const q: VersionQualification = { status: 'promoted', confidence: 0.9 } as VersionQualification;
    const host = render(<QualSummary q={q} />);
    expect(host.textContent).toContain('90%');
  });

  test('handles null confidence dash', () => {
    const q: VersionQualification = { status: 'error' } as VersionQualification;
    const host = render(<QualSummary q={q} />);
    expect(host.textContent).toContain('—');
  });
});

describe('CiBadge', () => {
  test('renders green state', () => {
    const ci: CiResolution = { state: 'green', detail: 'All checks passed' } as CiResolution;
    const host = render(<CiBadge ci={ci} />);
    expect(host.textContent).toContain('ci:green');
  });

  test('renders red state', () => {
    const ci: CiResolution = { state: 'red', detail: 'Checks failed' } as CiResolution;
    const host = render(<CiBadge ci={ci} />);
    expect(host.textContent).toContain('ci:red');
  });

  test('renders pending state', () => {
    const ci: CiResolution = { state: 'pending', detail: 'Running' } as CiResolution;
    const host = render(<CiBadge ci={ci} />);
    expect(host.textContent).toContain('ci:pending');
  });

  test('renders no_checks state', () => {
    const ci: CiResolution = { state: 'no_checks', detail: 'No CI configured' } as CiResolution;
    const host = render(<CiBadge ci={ci} />);
    expect(host.textContent).toContain('ci:no_checks');
  });

  test('renders resolved_sha', () => {
    const ci: CiResolution = { state: 'green', detail: '', resolved_sha: 'abc123def456' } as CiResolution;
    const host = render(<CiBadge ci={ci} />);
    expect(host.textContent).toContain('@abc123d');
  });
});

describe('ManagedCheckWaitSnapshotDetails', () => {
  test('renders nothing when state is null', () => {
    const host = render(<ManagedCheckWaitSnapshotDetails state={null} />);
    expect(host.textContent).toBe('');
  });

  test('renders check snapshot details', () => {
    const state = {
      state: 'promotion_check_wait',
      check_snapshot: {
        state: 'green', classified_at: '2026-07-01T12:00:00Z',
        head_sha: 'abc123def456', total_checks: 5,
        checks: [
          { name: 'CI / test', conclusion: 'success', status: 'completed' },
          { name: 'Lint', conclusion: 'failure', status: 'completed' },
        ],
      },
    } as unknown as QualificationState;
    const host = render(<ManagedCheckWaitSnapshotDetails state={state} />);
    expect(host.textContent).toContain('ci:green');
    expect(host.textContent).toContain('5 checks');
    expect(host.textContent).toContain('CI / test: success');
    expect(host.textContent).toContain('Lint: failure');
  });

  test('renders truncated checks count', () => {
    const state = {
      state: 'promotion_check_wait',
      check_snapshot: {
        state: 'green', classified_at: '2026-07-01T12:00:00Z',
        total_checks: 10, truncated: true,
        checks: [{ name: 'CI', conclusion: 'success', status: 'completed' }],
      },
    } as unknown as QualificationState;
    const host = render(<ManagedCheckWaitSnapshotDetails state={state} />);
    expect(host.textContent).toContain('+9 more');
  });

  test('renders "No check runs" when empty checks array', () => {
    const state = {
      state: 'promotion_check_wait',
      check_snapshot: {
        state: 'pending', classified_at: '2026-07-01T12:00:00Z',
        checks: [],
      },
    } as unknown as QualificationState;
    const host = render(<ManagedCheckWaitSnapshotDetails state={state} />);
    expect(host.textContent).toContain('No check runs reported yet');
  });
});

describe('AuditProvenance', () => {
  test('renders nothing when candidate_sha and created_at missing', () => {
    const audit: LatestAudit = {} as LatestAudit;
    const host = render(<AuditProvenance audit={audit} />);
    expect(host.textContent).toBe('');
  });

  test('renders SHA and timestamp', () => {
    const audit: LatestAudit = {
      candidate_sha: 'abc123def456', created_at: '2026-07-01T12:00:00Z',
    } as LatestAudit;
    const host = render(<AuditProvenance audit={audit} />);
    expect(host.textContent).toContain('@abc123d');
    expect(host.textContent).toContain('ran');
  });
});

describe('AuditFindingsList', () => {
  const baseAudit: LatestAudit = {
    candidate_sha: 'abc123', created_at: '2026-07-01T12:00:00Z',
  } as LatestAudit;

  test('renders running state', () => {
    const audit: LatestAudit = { ...baseAudit, task_status: 'IN_PROGRESS' };
    const host = render(<AuditFindingsList audit={audit} />);
    expect(host.textContent).toContain('audit running — no report yet');
  });

  test('renders blocked state', () => {
    const audit: LatestAudit = { ...baseAudit, task_status: 'BLOCKED' };
    const host = render(<AuditFindingsList audit={audit} />);
    expect(host.textContent).toContain('audit blocked — waiting on a precondition');
  });

  test('renders crashed state', () => {
    const audit: LatestAudit = { ...baseAudit, task_status: 'FAILED' };
    const host = render(<AuditFindingsList audit={audit} />);
    expect(host.textContent).toContain('audit crashed — no report');
  });

  test('renders completed-empty state', () => {
    const audit: LatestAudit = { ...baseAudit, task_status: 'COMPLETED' };
    const host = render(<AuditFindingsList audit={audit} />);
    expect(host.textContent).toContain('audit completed without a report');
  });

  test('renders "no findings" when report has empty findings', () => {
    const audit: LatestAudit = { ...baseAudit, task_status: 'COMPLETED', report: { findings: [] } };
    const host = render(<AuditFindingsList audit={audit} />);
    expect(host.textContent).toContain('audit: no findings');
  });

  test('renders findings sorted by severity', () => {
    const audit: LatestAudit = {
      ...baseAudit, task_status: 'COMPLETED',
      report: {
        findings: [
          { severity: 'low', summary: 'Minor issue' },
          { severity: 'critical', summary: 'Critical bug' },
          { severity: 'medium', summary: 'Medium concern' },
        ],
      },
    };
    const host = render(<AuditFindingsList audit={audit} />);
    expect(host.textContent).toContain('Critical bug');
    expect(host.textContent).toContain('Medium concern');
    expect(host.textContent).toContain('Minor issue');
  });
});

describe('HistoricalAuditContext', () => {
  test('renders stale audit context with active SHA contrast', () => {
    const audit: LatestAudit = {
      candidate_sha: 'old-sha-123456', created_at: '2026-07-01T12:00:00Z',
    } as LatestAudit;
    const host = render(<HistoricalAuditContext audit={audit} activeCandidateSha="new-sha-789012" />);
    expect(host.textContent).toContain('Historical Audit Context');
    expect(host.textContent).toContain('no current audit for this candidate');
    expect(host.textContent).toContain('new-sha');
    expect(host.textContent).toContain('old-sha');
  });

  test('falls back to AuditProvenance when activeCandidateSha missing', () => {
    const audit: LatestAudit = {
      candidate_sha: 'sha-1', created_at: '2026-07-01T12:00:00Z',
    } as LatestAudit;
    const host = render(<HistoricalAuditContext audit={audit} />);
    expect(host.textContent).toContain('@sha-1');
  });
});

describe('AuditReconciliationRow', () => {
  test('renders reconciliation scores', () => {
    const rec: AuditReconciliation = {
      model_reported_score: 0.85, findings_derived_score: 0.80,
      score_delta: 0.05, excluded_findings_count: 0,
    } as AuditReconciliation;
    const host = render(<AuditReconciliationRow rec={rec} />);
    expect(host.textContent).toContain('model 85%');
    expect(host.textContent).toContain('findings 80%');
    expect(host.textContent).toContain('δ 5%');
  });

  test('highlights divergence when score_delta >= 0.1', () => {
    const rec: AuditReconciliation = {
      model_reported_score: 0.90, findings_derived_score: 0.70,
      score_delta: 0.20, excluded_findings_count: 2,
    } as AuditReconciliation;
    const host = render(<AuditReconciliationRow rec={rec} />);
    expect(host.textContent).toContain('2 findings excluded');
  });
});

describe('DurableQualificationEvidenceRow', () => {
  test('renders nothing when all fields are empty', () => {
    const q: DurableQualificationEvidence = {};
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toBe('');
  });

  test('renders migration safety chip', () => {
    const q: DurableQualificationEvidence = { migration_safety: 'safe', migration_safety_detail: 'All good' };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('migration safe');
  });

  test('renders migration apply safety chip', () => {
    const q: DurableQualificationEvidence = { migration_apply_safety: 'verified' };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('apply verified');
  });

  test('renders audit findings count chip', () => {
    const q: DurableQualificationEvidence = {
      audit_findings: [{ severity: 'high', summary: 'Found issue' }],
    };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('1 audit finding');
  });

  test('renders excluded audit findings chip', () => {
    const q: DurableQualificationEvidence = {
      excluded_audit_findings: [{ severity: 'low', summary: 'Excluded' }],
    };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('1 excluded audit finding');
  });

  test('renders challenger refuted findings chip', () => {
    const q: DurableQualificationEvidence = {
      challenger_refuted_audit_findings: [{ severity: 'high', summary: 'Refuted', challenger_reason: 'Not a bug' }],
    };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('1 challenger false positive');
  });

  test('renders test erosion detail chip', () => {
    const q: DurableQualificationEvidence = { test_erosion_detail: { eroded: 3 } };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('erosion');
  });

  test('renders smoketest detail chip', () => {
    const q: DurableQualificationEvidence = { smoketest_detail: { passed: true } };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('smoke');
  });

  test('renders promotion PR and conflict', () => {
    const q: DurableQualificationEvidence = {
      promotion_pr_url: 'https://github.com/test/repo/pull/1',
      promotion_conflict: true,
      promotion_error_detail: 'Merge conflict',
    };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('promotion PR');
    expect(host.textContent).toContain('promotion conflict');
  });

  test('renders goal alignment chip when aligned', () => {
    const q: DurableQualificationEvidence = {
      goal_alignment: { state: 'aligned', assessed_grade: 0.85, biggest_gap: null },
    };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('goal aligned');
    expect(host.textContent).toContain('85%');
  });

  test('renders goal alignment chip when shortfall', () => {
    const q: DurableQualificationEvidence = {
      goal_alignment: { state: 'shortfall', assessed_grade: 0.4, biggest_gap: 'Coverage regression' },
    };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('goal shortfall');
  });

  test('skips goal_alignment when not_applicable', () => {
    const q: DurableQualificationEvidence = {
      goal_alignment: { state: 'not_applicable' },
    };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toBe('');
  });

  test('renders blind merge grade chip', () => {
    const q: DurableQualificationEvidence = {
      blind_merge_grade: { coverage_verdict: 'covers', biggest_gap: null },
    };
    const host = render(<DurableQualificationEvidenceRow q={q} />);
    expect(host.textContent).toContain('merge covers');
  });
});

describe('PromotionReadinessSummary', () => {
  test('renders nothing when readiness is null', () => {
    const host = render(<PromotionReadinessSummary readiness={null} />);
    expect(host.textContent).toBe('');
  });

  test('renders historical calibration when is_current_readiness is false', () => {
    const readiness: PromotionReadiness = {
      is_current_readiness: false,
      ready: false,
      reason: 'Pre-go-live window',
      verdicts_evaluated: 10,
      would_promote_rate: 0.8,
      consecutive_clean_passes: 3,
      recurring_blockers: {},
    } as PromotionReadiness;
    const host = render(<PromotionReadinessSummary readiness={readiness} />);
    expect(host.textContent).toContain('pre-go-live dry-run calibration');
    expect(host.textContent).toContain('10 pre-go-live dry-runs');
  });

  test('renders ready state', () => {
    const readiness: PromotionReadiness = {
      ready: true,
      verdicts_evaluated: 15,
      would_promote_rate: 0.9,
      consecutive_clean_passes: 5,
      recurring_blockers: {},
    } as PromotionReadiness;
    const host = render(<PromotionReadinessSummary readiness={readiness} />);
    expect(host.textContent).toContain('self-approval streak clears thresholds');
  });

  test('renders not-ready state', () => {
    const readiness: PromotionReadiness = {
      ready: false,
      reason: 'Insufficient evidence',
      verdicts_evaluated: 2,
      would_promote_rate: 0.5,
      consecutive_clean_passes: 0,
      recurring_blockers: {},
    } as PromotionReadiness;
    const host = render(<PromotionReadinessSummary readiness={readiness} />);
    expect(host.textContent).toContain('not ready');
    expect(host.textContent).toContain('Insufficient evidence');
  });

  test('renders validated precision', () => {
    const readiness: PromotionReadiness = {
      ready: true,
      ground_truth_validated: true,
      validated_precision: 0.92,
      resolved_outcome_sample: 25,
      verdicts_evaluated: 15,
      would_promote_rate: 0.9,
      consecutive_clean_passes: 5,
      recurring_blockers: {},
    } as PromotionReadiness;
    const host = render(<PromotionReadinessSummary readiness={readiness} />);
    expect(host.textContent).toContain('92% validated precision');
    expect(host.textContent).toContain('25 resolved');
  });

  test('renders acceptance caveat', () => {
    const readiness: PromotionReadiness = {
      ready: true,
      acceptance_caveat: 'Visual approval pending',
      verdicts_evaluated: 5,
      would_promote_rate: 1.0,
      consecutive_clean_passes: 5,
      recurring_blockers: {},
    } as PromotionReadiness;
    const host = render(<PromotionReadinessSummary readiness={readiness} />);
    expect(host.textContent).toContain('Visual approval pending');
  });

  test('renders recurring blockers', () => {
    const readiness: PromotionReadiness = {
      ready: false,
      reason: 'Blockers pending',
      verdicts_evaluated: 8,
      would_promote_rate: 0.3,
      consecutive_clean_passes: 0,
      recurring_blockers: { lint_failure: 3, test_flake: 2 } as unknown as Record<string, number>,
    } as PromotionReadiness;
    const host = render(<PromotionReadinessSummary readiness={readiness} />);
    expect(host.textContent).toContain('lint_failure×3');
    expect(host.textContent).toContain('test_flake×2');
  });
});

describe('PinnedServingVersionCallout', () => {
  test('renders nothing when serving status is not serving_confirmed', () => {
    const version: RecentVersion = { serving: { status: 'deploy_failed' } } as RecentVersion;
    const host = render(<PinnedServingVersionCallout version={version} />);
    expect(host.textContent).toBe('');
  });

  test('renders serving confirmation with version number', () => {
    const version: RecentVersion = {
      version_number: 5,
      serving: { status: 'serving_confirmed', confirmed_at: '2026-07-01T12:00:00Z' },
      highlights: { headline: 'Stable release' },
      merged_count: 12,
    } as RecentVersion;
    const host = render(<PinnedServingVersionCallout version={version} />);
    expect(host.textContent).toContain('✓ currently serving');
    expect(host.textContent).toContain('v5');
    expect(host.textContent).toContain('12 merged');
    expect(host.textContent).toContain('Stable release');
  });
});

describe('StagedDirectionsSummary', () => {
  test('renders nothing when empty', () => {
    const host = render(<StagedDirectionsSummary staged={[]} />);
    expect(host.textContent).toBe('');
  });

  test('renders staged directions list', () => {
    const staged: StagedDirection[] = [
      { id: 'dir-1', staged_for_version: 3, text: 'Add dark mode', priority: 'high', staged_age_days: 2, staged_at: '2026-07-01T10:00:00Z' },
      { id: 'dir-2', text: 'Fix login bug', staged_age_days: 5, priority: null, staged_for_version: null, staged_at: '2026-07-01T10:00:00Z' },
    ];
    const host = render(<StagedDirectionsSummary staged={staged} />);
    expect(host.textContent).toContain('Staged for next version (2)');
    expect(host.textContent).toContain('→v3');
    expect(host.textContent).toContain('Add dark mode');
    expect(host.textContent).toContain('high');
    expect(host.textContent).toContain('2d staged');
    expect(host.textContent).toContain('Fix login bug');
  });
});

describe('CutConsequences', () => {
  const cv: CurrentVersion = {
    version_number: 3,
    in_flight_task_count: 2,
    qualification_state: null,
  } as CurrentVersion;

  test('renders activating directions', () => {
    const staged: StagedDirection[] = [
      { id: 'dir-1', staged_for_version: 3, text: 'Feature A', priority: null, staged_at: null, staged_age_days: null },
      { id: 'dir-2', staged_for_version: 5, text: 'Feature B (future)', priority: null, staged_at: null, staged_age_days: null },
      { id: 'dir-3', text: 'Feature C (no target)', priority: null, staged_for_version: null, staged_at: null, staged_age_days: null },
    ];
    const host = render(<CutConsequences cv={cv} staged={staged} />);
    expect(host.textContent).toContain('Cutting v3 now will:');
    expect(host.textContent).toContain('Start 2 staged directions immediately');
    expect(host.textContent).toContain('Feature A');
    expect(host.textContent).toContain('Feature C');
    expect(host.textContent).not.toContain('Feature B');
    expect(host.textContent).toContain('2 in-flight tasks');
  });

  test('renders "No staged directions" when empty', () => {
    const host = render(<CutConsequences cv={cv} staged={[]} />);
    expect(host.textContent).toContain('No staged directions will activate');
  });

  test('renders "No tasks in flight" when zero', () => {
    const zeroCv: CurrentVersion = { ...cv, in_flight_task_count: 0 };
    const host = render(<CutConsequences cv={zeroCv} staged={[]} />);
    expect(host.textContent).toContain('No tasks are currently in flight');
  });

  test('renders "unavailable" when in_flight_task_count is null', () => {
    const nullCv: CurrentVersion = { ...cv, in_flight_task_count: null as unknown as number };
    const host = render(<CutConsequences cv={nullCv} staged={[]} />);
    expect(host.textContent).toContain('In-flight task count unavailable');
  });

  test('renders promotion_in_flight warning', () => {
    const promoCv: CurrentVersion = {
      ...cv,
      qualification_state: { state: 'promotion_in_flight', promotion_pr_number: 42, promotion_age_seconds: 180 },
    } as CurrentVersion;
    const staged: StagedDirection[] = [];
    const host = render(<CutConsequences cv={promoCv} staged={staged} />);
    expect(host.textContent).toContain('#42');
    expect(host.textContent).toContain('auto-merging');
  });

  test('renders promotion_check_wait warning', () => {
    const waitCv: CurrentVersion = {
      ...cv,
      qualification_state: { state: 'promotion_check_wait', promotion_pr_number: 43, promotion_age_seconds: 300 },
    } as CurrentVersion;
    const host = render(<CutConsequences cv={waitCv} staged={[]} />);
    expect(host.textContent).toContain('#43');
    expect(host.textContent).toContain('waiting for required checks');
  });
});

describe('ChallengedVsUnchallengedSummary', () => {
  const telemetry: ChallengedVsUnchallengedTelemetry = {
    total_verdicts: 20,
    challenged: { sample_size: 8, insufficient_sample: false, exclusion_rate: 0.1, disagreement_rate: 0.15, wrong_decision_rate: 0.05, wrong_decision_resolved_sample: 10, challenge_refuted_rate: 0.08, challenge_refuted_count: 2 } as ChallengedVsUnchallengedTelemetry['challenged'],
    unchallenged: { sample_size: 12, insufficient_sample: false, exclusion_rate: 0.2, disagreement_rate: 0.25, wrong_decision_rate: 0.08, wrong_decision_resolved_sample: 12, challenge_refuted_rate: 0, challenge_refuted_count: 0 } as ChallengedVsUnchallengedTelemetry['unchallenged'],
  };

  test('renders telemetry per cohort', () => {
    const host = render(<ChallengedVsUnchallengedSummary telemetry={telemetry} />);
    expect(host.textContent).toContain('20 verdicts');
    expect(host.textContent).toContain('challenged (8)');
    expect(host.textContent).toContain('unchallenged (12)');
    expect(host.textContent).toContain('excl 10%');
    expect(host.textContent).toContain('disagree 15%');
    expect(host.textContent).toContain('refuted 8%');
  });

  test('renders insufficient message when both insufficient', () => {
    const insufficientTelemetry: ChallengedVsUnchallengedTelemetry = {
      total_verdicts: 3,
      challenged: { sample_size: 1, insufficient_sample: true } as ChallengedVsUnchallengedTelemetry['challenged'],
      unchallenged: { sample_size: 2, insufficient_sample: true } as ChallengedVsUnchallengedTelemetry['unchallenged'],
    };
    const host = render(<ChallengedVsUnchallengedSummary telemetry={insufficientTelemetry} />);
    expect(host.textContent).toContain('not enough data yet');
  });
});

describe('VersionHighlightsLine', () => {
  const baseHighlights: VersionSummaryHighlights = {
    headline: 'Bug fixes and performance',
    merged_count: 8,
    proven: 5,
    total_merged_for_acceptance: 6,
    qualification_confidence: null,
    qualification_threshold: null,
  };

  test('renders headline and merged counts', () => {
    const host = render(<VersionHighlightsLine highlights={baseHighlights} phase="active" status="current" />);
    expect(host.textContent).toContain('Bug fixes and performance');
    expect(host.textContent).toContain('8 merged');
    expect(host.textContent).toContain('5/6 merged-task proofs');
  });

  test('renders no-eligible-merged-task-proofs when total_merged_for_acceptance is 0', () => {
    const zeroHighlights: VersionSummaryHighlights = {
      ...baseHighlights, total_merged_for_acceptance: 0,
    };
    const host = render(<VersionHighlightsLine highlights={zeroHighlights} phase="active" status="current" />);
    expect(host.textContent).toContain('no eligible merged-task proofs');
  });

  test('renders qualification in progress', () => {
    const host = render(
      <VersionHighlightsLine highlights={baseHighlights} phase="active" status="current" qualificationState="qualifying" />,
    );
    expect(host.textContent).toContain('qualification in progress');
  });

  test('renders confidence and threshold', () => {
    const confHighlights: VersionSummaryHighlights = {
      ...baseHighlights, qualification_confidence: 0.85, qualification_threshold: 0.7,
    };
    const host = render(<VersionHighlightsLine highlights={confHighlights} phase="active" status="current" />);
    expect(host.textContent).toContain('conf 85%');
    expect(host.textContent).toContain('thr 70%');
  });

  test('renders phase and status', () => {
    const host = render(<VersionHighlightsLine highlights={baseHighlights} phase="active" status="current" />);
    expect(host.textContent).toContain('active · current');
  });

  test('renders narrative', () => {
    const withNarrative: VersionSummaryHighlights = {
      ...baseHighlights, narrative: 'This version addresses key stability issues.',
    };
    const host = render(<VersionHighlightsLine highlights={withNarrative} phase="active" status="current" />);
    expect(host.textContent).toContain('This version addresses key stability issues.');
  });
});

describe('VersionHistorySummary', () => {
  test('renders loading state', () => {
    const host = render(<VersionHistorySummary versionId="ver-1" />);
    expect(host.textContent).toContain('Loading');
  });

  test('renders summary after fetch', async () => {
    const host = render(<VersionHistorySummary versionId="ver-1" />);
    await vi.waitFor(() => { expect(host.textContent).toContain('Performance improvements'); });
    expect(host.textContent).toContain('5 merged');
  });
});

describe('VersionLifecycleRefreshBanner', () => {
  test('renders stale banner', () => {
    const host = render(
      <VersionLifecycleRefreshBanner
        kind="stale"
        message="Data is 5 minutes old"
        refreshing={false}
        canRetry={true}
        onRefresh={vi.fn()}
      />,
    );
    expect(host.textContent).toContain('Version lifecycle data may be stale');
    expect(host.textContent).toContain('Data is 5 minutes old');
  });

  test('renders unavailable banner', () => {
    const host = render(
      <VersionLifecycleRefreshBanner
        kind="unavailable"
        message="Server returned 500"
        refreshing={false}
        canRetry={true}
        onRefresh={vi.fn()}
      />,
    );
    expect(host.textContent).toContain('Version lifecycle unavailable');
    expect(host.textContent).toContain('Server returned 500');
  });

  test('calls onRefresh when retry clicked', () => {
    const onRefresh = vi.fn();
    const host = render(
      <VersionLifecycleRefreshBanner
        kind="stale"
        message="Retry needed"
        refreshing={false}
        canRetry={true}
        onRefresh={onRefresh}
      />,
    );
    host.querySelector('button')?.click();
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  test('disables retry when refreshing', () => {
    const host = render(
      <VersionLifecycleRefreshBanner
        kind="stale"
        message="Refreshing"
        refreshing={true}
        canRetry={true}
        onRefresh={vi.fn()}
      />,
    );
    expect(host.textContent).toContain('Retrying...');
    expect(host.querySelector('button')?.hasAttribute('disabled')).toBe(true);
  });

  test('last successful refresh text shown when stale', () => {
    const host = render(
      <VersionLifecycleRefreshBanner
        kind="stale"
        message="Data outdated"
        lastSuccessfulRefreshAt="2026-07-01T11:00:00Z"
        refreshing={false}
        canRetry={true}
        onRefresh={vi.fn()}
      />,
    );
    expect(host.textContent).toContain('last successful refresh');
  });
});

describe('DimensionBreakdown', () => {
  test('renders nothing when empty', () => {
    const host = render(<DimensionBreakdown dimensions={[]} />);
    expect(host.textContent).toBe('');
  });

  test('renders dimension rows with scores', () => {
    const dimensions: DimensionState[] = [
      { key: 'code_quality', label: 'Code Quality', status: 'passed', score: 0.85, reason: null },
      { key: 'security', label: 'Security', status: 'failed', score: 0.3, reason: 'Critical vulnerability' },
      { key: 'perf', label: 'Performance', status: 'unavailable', score: null, reason: 'No baseline' },
    ];
    const host = render(<DimensionBreakdown dimensions={dimensions} />);
    expect(host.textContent).toContain('Code Quality');
    expect(host.textContent).toContain('85%');
    expect(host.textContent).toContain('Security');
    expect(host.textContent).toContain('30%');
    expect(host.textContent).toContain('Critical vulnerability');
    expect(host.textContent).toContain('Performance');
    expect(host.textContent).toContain('No baseline');
  });
});

describe('ConfidenceSparkline', () => {
  const makeVerdict = (overrides: Partial<QualificationVerdictRecord> = {}): QualificationVerdictRecord => ({
    id: 'v-1', release_version_id: 'rv-1', candidate_sha: 'abc123', confidence: null,
    sub_scores: null, blocker: null, audit_missing_reason: null, would_promote: null,
    ...overrides,
  } as QualificationVerdictRecord);

  test('renders nothing with less than 2 points', () => {
    const verdicts: QualificationVerdictRecord[] = [makeVerdict({ confidence: 0.8, would_promote: true })];
    const host = render(<ConfidenceSparkline verdicts={verdicts} />);
    expect(host.textContent).toBe('');
  });

  test('renders sparkline with 2+ points', () => {
    const verdicts: QualificationVerdictRecord[] = [
      makeVerdict({ confidence: 0.7, would_promote: false }),
      makeVerdict({ confidence: 0.8, would_promote: true }),
      makeVerdict({ confidence: 0.9, would_promote: true }),
    ];
    const host = render(<ConfidenceSparkline verdicts={verdicts} />);
    expect(host.querySelector('[data-testid="confidence-sparkline"]')).not.toBeNull();
  });
});

describe('ServingStatusBadge', () => {
  test('renders nothing when no servingStatus', () => {
    const host = render(<ServingStatusBadge servingStatus={null} />);
    expect(host.textContent).toBe('');
  });

  test('renders serving_confirmed', () => {
    const host = render(<ServingStatusBadge servingStatus="serving_confirmed" servedSha="abc123def456" servedAt="2026-07-01T12:00:00Z" />);
    expect(host.textContent).toContain('✓ serving confirmed');
    expect(host.textContent).toContain('@abc123d');
  });

  test('renders deploy_failed', () => {
    const host = render(<ServingStatusBadge servingStatus="deploy_failed" deployFailureReason="CrashLoop back-off" />);
    expect(host.textContent).toContain('⚠ deploy failed — NOT serving');
    expect(host.textContent).toContain('CrashLoop back-off');
  });

  test('renders confirmation_timed_out', () => {
    const host = render(<ServingStatusBadge servingStatus="confirmation_timed_out" />);
    expect(host.textContent).toContain('⧖ confirmation timed out — unverified');
  });

  test('renders unconfirmed state', () => {
    const host = render(<ServingStatusBadge servingStatus="unconfirmed" />);
    expect(host.textContent).toContain('awaiting deploy confirmation');
  });

  test('renders overdue for unconfirmed past deadline', () => {
    const pastDate = new Date(Date.now() - 3600000).toISOString();
    const host = render(<ServingStatusBadge servingStatus="unconfirmed" deployConfirmDeadline={pastDate} />);
    expect(host.textContent).toContain('OVERDUE');
  });
});

describe('ArtifactEvidenceList', () => {
  test('renders nothing for single artifact', () => {
    const artifacts: ReleaseArtifactEvidence[] = [{ artifact_kind: 'agent_image', required: true, serving_status: 'serving_confirmed' }];
    const host = render(<ArtifactEvidenceList artifacts={artifacts} />);
    expect(host.textContent).toBe('');
  });

  test('renders nothing for null', () => {
    const host = render(<ArtifactEvidenceList artifacts={null} />);
    expect(host.textContent).toBe('');
  });

  test('renders artifact rows', () => {
    const artifacts: ReleaseArtifactEvidence[] = [
      { artifact_kind: 'agent_image', required: true, serving_status: 'serving_confirmed', served_identifier: 'agent-v3', served_at: '2026-07-01T12:00:00Z' },
      { artifact_kind: 'config_map', required: false, serving_status: 'unconfirmed', served_identifier: 'cfg-v3' },
    ];
    const host = render(<ArtifactEvidenceList artifacts={artifacts} />);
    expect(host.textContent).toContain('agent_image');
    expect(host.textContent).toContain('✓ serving');
    expect(host.textContent).toContain('config_map');
    expect(host.textContent).toContain('pending');
  });

  test('renders fleet match count for non-serving agent images', () => {
    const artifacts: ReleaseArtifactEvidence[] = [
      { artifact_kind: 'agent_image', required: true, serving_status: 'unconfirmed' },
      { artifact_kind: 'config_map', required: false, serving_status: 'serving_confirmed' },
    ];
    const agentShas: AgentServingShasResponse = {
      fleet: {
        buckets: [
          { matches_active_candidate: true, count: 3 },
          { matches_active_candidate: true, count: 2 },
        ],
      },
    } as AgentServingShasResponse;
    const host = render(<ArtifactEvidenceList artifacts={artifacts} agentServingShas={agentShas} />);
    expect(host.textContent).toContain('5 VMs on candidate');
  });
});

describe('formatPromotionFreshnessContext', () => {
  test('returns null when age is missing', () => {
    expect(formatPromotionFreshnessContext(null)).toBeNull();
  });

  test('returns detail text for normal case', () => {
    const state: QualificationState = { promotion_age_seconds: 120 } as QualificationState;
    const result = formatPromotionFreshnessContext(state);
    expect(result?.detailText).toContain('in flight ~');
    expect(result?.toneClassName).toBe('text-emerald-300');
  });

  test('adds timeout cap when timeout_seconds present', () => {
    const state: QualificationState = { promotion_age_seconds: 300, promotion_timeout_seconds: 3600 } as QualificationState;
    const result = formatPromotionFreshnessContext(state);
    expect(result?.detailText).toContain('auto-timeout cap');
  });

  test('returns past-auto-timeout tone', () => {
    const state: QualificationState = { promotion_age_seconds: 5000, promotion_past_timeout: true } as QualificationState;
    const result = formatPromotionFreshnessContext(state);
    expect(result?.detailText).toContain('past auto-timeout');
    expect(result?.toneClassName).toBe('text-red-400');
  });

  test('returns nearing-auto-timeout tone', () => {
    const state: QualificationState = { promotion_age_seconds: 3000, promotion_nearing_timeout: true } as QualificationState;
    const result = formatPromotionFreshnessContext(state);
    expect(result?.detailText).toContain('nearing auto-timeout');
    expect(result?.toneClassName).toBe('text-amber-400');
  });
});

describe('stagedActivatedByCut', () => {
  test('filters directions with staged_for_version <= versionNumber', () => {
    const staged: StagedDirection[] = [
      { id: 'a', staged_for_version: 2, text: 'Early', priority: null, staged_at: null, staged_age_days: null },
      { id: 'b', staged_for_version: 3, text: 'This version', priority: null, staged_at: null, staged_age_days: null },
      { id: 'c', staged_for_version: 5, text: 'Future', priority: null, staged_at: null, staged_age_days: null },
      { id: 'd', staged_for_version: null, text: 'No target', priority: null, staged_at: null, staged_age_days: null },
    ];
    const activated = stagedActivatedByCut(staged, 3);
    expect(activated).toHaveLength(3);
    expect(activated.map(a => a.id)).toEqual(['a', 'b', 'd']);
  });

  test('returns empty array when none match', () => {
    const staged: StagedDirection[] = [
      { id: 'a', staged_for_version: 5, text: 'Far future', priority: null, staged_at: null, staged_age_days: null },
    ];
    expect(stagedActivatedByCut(staged, 3)).toHaveLength(0);
  });

  test('returns empty array for empty input', () => {
    expect(stagedActivatedByCut([], 3)).toHaveLength(0);
  });
});
