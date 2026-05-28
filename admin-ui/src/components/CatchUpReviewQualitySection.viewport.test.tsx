import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CatchUpReviewQualitySection } from './CatchUpReviewQualitySection';
import { expectNoOverflowAt } from '../test/viewport';

describe('CatchUpReviewQualitySection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders dense review-quality metrics without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <CatchUpReviewQualitySection
            selfReviewEffectiveness={{
              with_self_review_approval_rate: 0.82,
              without_self_review_approval_rate: 0.61,
              clean_verdict_approval_rate: 0.74,
              fixed_verdict_approval_rate: 0.58,
              sample_size: 412,
            }}
            selfReviewPredictionAccuracy={{
              prediction_hit_rate: 0.68,
              miss_rate: 0.32,
              false_alarm_rate: 0.14,
              sample_size: 308,
            }}
            selfReviewSkipRate={{
              overall_rate: 0.07,
              skipped_count: 23,
              sample_size: 329,
              by_task_type: {
                fix_review_feedback_loop_iteration: { rate: 0.12, skipped_count: 14, sample_size: 117 },
                create_with_long_descriptive_label: { rate: 0.04, skipped_count: 5, sample_size: 122 },
              },
            }}
            reviewAccuracy={{
              total_reviews: 540,
              overrides: 18,
              dismissals: 9,
              misses: 27,
              total_approvals: 486,
              precision: 0.93,
              recall: 0.81,
            }}
            firstReviewApprovalRate={{
              overall: 0.74,
              sample_size: 412,
              by_type: {
                create_long_label_for_overflow_pressure_at_narrow_width: 0.72,
                fix_review_feedback_loop_iteration: 0.78,
                refactor_extraction_cluster: 0.71,
              },
              by_scope: { small: 0.81, medium: 0.74, large: 0.66 },
              with_playbook: 0.79,
              without_playbook: 0.69,
              with_measurements: 0.76,
              without_measurements: 0.71,
            }}
            freshReviewerEffectiveness={{
              fresh_reviewer_sample_size: 92,
              fresh_reviewer_first_attempt_approval_rate: 0.83,
              baseline_first_attempt_approval_rate: 0.74,
              per_category_hit_rate: {
                correctness: 0.62,
                'error-handling': 0.55,
                security: 0.41,
                'scope-drift': 0.78,
              },
              reviewer_finding_added_value: {
                correctness: 0.09,
                'error-handling': 0.07,
              },
              prediction_sample_size: 87,
            }}
            srCatchRateTargets={{ correctness: 0.7, 'error-handling': 0.65 }}
          />,
        );
      });
    });
  });
});
