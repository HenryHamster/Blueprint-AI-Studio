import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { useRef } from 'react';
import { ReviewQualitySection } from './ReviewQualitySection';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

vi.mock('./SrTrendBreakdown', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./SrTrendBreakdown')>();
  return {
    ...actual,
    SrTrendBreakdown: () => null,
    EdgesTrendBreakdown: () => null,
  };
});

const digest = {
  review_accuracy: {
    precision: 0.82,
    recall: 0.71,
    total_reviews: 48,
    total_approvals: 62,
  },
  self_review_effectiveness: {
    with_self_review_approval_rate: 0.74,
    without_self_review_approval_rate: 0.58,
    sample_size: 32,
    sr_fixed_count: 12,
    sr_clean_count: 20,
    fixed_verdict_approval_rate: 0.67,
    clean_verdict_approval_rate: 0.80,
  },
  self_review_prediction_accuracy: {
    prediction_hit_rate: 0.69,
    false_alarm_rate: 0.14,
    self_flagged_shipped_unfixed_task_rate: 0.18,
    self_flagged_shipped_unfixed_task_count: 5,
  },
  self_review_skip_rate: {
    overall_rate: 0.07,
    skipped_count: 4,
    sample_size: 57,
    by_task_type: { fix: { rate: 0.09, skipped_count: 3, sample_size: 33 } },
  },
};

function Wrapper() {
  const ref = useRef<HTMLElement | null>(null);
  return (
    <ReviewQualitySection
      digest={digest}
      loading={false}
      selectedProjectId={null}
      tasks={[]}
      onViewTask={() => {}}
      srCatchRateTargets={undefined}
      onCategoryClick={() => {}}
      onConfigureTargets={() => {}}
      sectionRef={ref}
    />
  );
}

describe('ReviewQualitySection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

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

  test('no overflow at 375px (mobile)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<Wrapper />);
      });
    });
  });
});
