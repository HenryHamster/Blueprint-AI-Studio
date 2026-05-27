import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  expectNoOverflowAt,
  expectNoTextTruncationAt,
  expectMinTouchTargetAt,
} from '../test/viewport';
import { ProjectReleaseQualificationSection } from './ProjectReleaseQualificationSection';
import type { QualificationVerdictRecord, CurrentVersion } from '../api';

const mockCurrentVersion: CurrentVersion = {
  version_number: 42,
  phase: 'qualifying',
  phase_changed_at: '2026-05-01T00:00:00Z',
  phase_reasoning: null,
  goal: null,
  opened_at: '2026-05-01T00:00:00Z',
  qualification_attempts: 3,
  merged_task_count: 10,
  last_qualification: {
    status: 'not_promoted',
    confidence: 0.62,
    threshold: 0.7,
    sub_scores: { audit: 0.55, smoketest: 0.9, test_erosion: 0.8 },
    ci_resolution: null,
    blocker: 'audit_score_below_threshold',
    finalized_at: '2026-05-27T10:00:00Z',
  },
};

const mockVerdicts: QualificationVerdictRecord[] = [
  {
    id: 'v1',
    release_version_id: 'rv1',
    candidate_sha: 'a71ac258deadbeef',
    confidence: 0.62,
    sub_scores: { audit: 0.55, smoketest: 0.9, test_erosion: 0.8 },
    blocker: 'audit_score_below_threshold',
    audit_missing_reason: null,
    would_promote: false,
    promotion_enabled: true,
    terminal_status: 'not_promoted',
    threshold: 0.7,
    started_at: '2026-05-27T09:50:00Z',
    finalized_at: '2026-05-27T10:00:00Z',
    created_at: '2026-05-27T09:50:00Z',
  },
  {
    id: 'v2',
    release_version_id: 'rv1',
    candidate_sha: 'b82bd369cafebabe',
    confidence: null,
    sub_scores: null,
    blocker: 'infra_failure',
    audit_missing_reason: 'audit_report_missing',
    would_promote: false,
    promotion_enabled: true,
    terminal_status: 'infra_failure',
    threshold: 0.7,
    started_at: '2026-05-27T08:00:00Z',
    finalized_at: '2026-05-27T08:10:00Z',
    created_at: '2026-05-27T08:00:00Z',
  },
];

describe('ProjectReleaseQualificationSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { act(() => root.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px with verdicts', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectReleaseQualificationSection
            currentVersion={mockCurrentVersion}
            verdicts={mockVerdicts}
          />,
        );
      });
    });
  });

  test('renders without overflow at 375px with empty verdicts', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectReleaseQualificationSection
            currentVersion={null}
            verdicts={[]}
          />,
        );
      });
    });
  });

  test('labels and scores do not silently clip text at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectReleaseQualificationSection
            currentVersion={mockCurrentVersion}
            verdicts={mockVerdicts}
          />,
        );
      });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectReleaseQualificationSection
            currentVersion={mockCurrentVersion}
            verdicts={mockVerdicts}
          />,
        );
      });
    }, { selector: 'button, a[href]' });
  });

  test('renders infra failure warning without overflow at 375px', async () => {
    const versionWithInfraErrors: CurrentVersion = {
      ...mockCurrentVersion,
      last_qualification: {
        status: 'error',
        confidence: null,
        threshold: 0.7,
        sub_scores: null,
        ci_resolution: null,
        blocker: 'infra_failure',
        finalized_at: '2026-05-27T10:00:00Z',
        consecutive_infra_errors: 3,
        error: 'audit report missing',
      },
    };
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProjectReleaseQualificationSection
            currentVersion={versionWithInfraErrors}
            verdicts={mockVerdicts}
          />,
        );
      });
    });
  });
});
