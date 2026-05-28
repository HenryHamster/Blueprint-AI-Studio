import React from 'react';
import { render, screen, fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { CatchUpReviewQualitySection } from './CatchUpReviewQualitySection';

vi.mock('./SrTrendBreakdown', () => ({
  SrTrendBreakdown: () => <div data-testid="sr-trend-breakdown" />,
}));

describe('CatchUpReviewQualitySection', () => {
  it('returns null when no data is present', () => {
    const { container } = render(<CatchUpReviewQualitySection />);
    expect(container.firstChild).toBeNull();
  });

  it('returns null when fra sample_size < 5', () => {
    const { container } = render(
      <CatchUpReviewQualitySection
        firstReviewApprovalRate={{ overall: 0.9, sample_size: 3 }}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders toggle button when data is present', () => {
    render(
      <CatchUpReviewQualitySection
        selfReviewEffectiveness={{
          with_self_review_approval_rate: 0.85,
          without_self_review_approval_rate: 0.6,
          clean_verdict_approval_rate: null,
          fixed_verdict_approval_rate: null,
          sample_size: 20,
        }}
      />
    );
    expect(screen.getByRole('button', { name: /review quality/i })).toBeInTheDocument();
  });

  it('collapsed by default — body not visible', () => {
    render(
      <CatchUpReviewQualitySection
        selfReviewEffectiveness={{
          with_self_review_approval_rate: 0.85,
          without_self_review_approval_rate: 0.6,
          clean_verdict_approval_rate: null,
          fixed_verdict_approval_rate: null,
          sample_size: 20,
        }}
      />
    );
    expect(screen.queryByText('Self-review')).toBeNull();
  });

  it('expands on toggle click', () => {
    render(
      <CatchUpReviewQualitySection
        selfReviewEffectiveness={{
          with_self_review_approval_rate: 0.85,
          without_self_review_approval_rate: 0.6,
          clean_verdict_approval_rate: null,
          fixed_verdict_approval_rate: null,
          sample_size: 20,
        }}
      />
    );
    const btn = screen.getByRole('button', { name: /review quality/i });
    expect(btn).toHaveAttribute('aria-expanded', 'false');
    fireEvent.click(btn);
    expect(btn).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getByText('Self-review')).toBeInTheDocument();
  });

  it('collapses on second toggle click', () => {
    render(
      <CatchUpReviewQualitySection
        selfReviewEffectiveness={{
          with_self_review_approval_rate: 0.85,
          without_self_review_approval_rate: null,
          clean_verdict_approval_rate: null,
          fixed_verdict_approval_rate: null,
          sample_size: 10,
        }}
      />
    );
    const btn = screen.getByRole('button', { name: /review quality/i });
    fireEvent.click(btn);
    expect(screen.getByText('Self-review')).toBeInTheDocument();
    fireEvent.click(btn);
    expect(screen.queryByText('Self-review')).toBeNull();
  });

  it('renders fresh-reviewer sample-too-small branch when n < 5', () => {
    render(
      <CatchUpReviewQualitySection
        freshReviewerEffectiveness={{
          fresh_reviewer_sample_size: 3,
          fresh_reviewer_first_attempt_approval_rate: null,
          baseline_first_attempt_approval_rate: null,
          per_category_hit_rate: {},
          reviewer_finding_added_value: {},
          prediction_sample_size: 0,
        }}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /review quality/i }));
    expect(screen.getByText(/sample too small/)).toBeInTheDocument();
  });

  it('renders fresh-reviewer metrics when sample is sufficient', () => {
    render(
      <CatchUpReviewQualitySection
        freshReviewerEffectiveness={{
          fresh_reviewer_sample_size: 10,
          fresh_reviewer_first_attempt_approval_rate: 0.8,
          baseline_first_attempt_approval_rate: 0.7,
          per_category_hit_rate: {},
          reviewer_finding_added_value: {},
          prediction_sample_size: 10,
        }}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /review quality/i }));
    expect(screen.getByText('Fresh-reviewer')).toBeInTheDocument();
    expect(screen.getByText('80%')).toBeInTheDocument();
  });

  it('renders SR skip rate row', () => {
    render(
      <CatchUpReviewQualitySection
        selfReviewSkipRate={{
          overall_rate: 0.1,
          skipped_count: 5,
          sample_size: 50,
          by_task_type: {},
        }}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /review quality/i }));
    expect(screen.getByText('SR Skip')).toBeInTheDocument();
  });

  it('renders first-review approval row when sample >= 5', () => {
    render(
      <CatchUpReviewQualitySection
        firstReviewApprovalRate={{ overall: 0.75, sample_size: 10 }}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /review quality/i }));
    expect(screen.getByText('First-review approval')).toBeInTheDocument();
    expect(screen.getByText('75%')).toBeInTheDocument();
  });

  it('renders SrTrendBreakdown when trends present', () => {
    render(
      <CatchUpReviewQualitySection
        selfReviewPredictionAccuracyTrends={{
          categories: { correctness: { recent_hit_rate: 0.8, trend: 'stable', sample_size: 10 } },
        } as never}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: /review quality/i }));
    expect(screen.getByTestId('sr-trend-breakdown')).toBeInTheDocument();
  });
});
