import { describe, expect, test, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MessagesSummaryCard } from './MessagesSummaryCard';

function renderCard(overrides: Partial<Parameters<typeof MessagesSummaryCard>[0]['newMessageCounts']> = {}) {
  const props = {
    newMessageCounts: {
      blockers: 0,
      questions: 0,
      feedback: 0,
      bugReports: 0,
      routine: 0,
      highSignalNotes: 0,
      ...overrides,
    },
    onScrollToBlockers: vi.fn(),
    onScrollToQuestions: vi.fn(),
    onScrollToRoutine: vi.fn(),
  };

  render(<MessagesSummaryCard {...props} />);
  return props;
}

describe('MessagesSummaryCard', () => {
  test('hides each row when its count is zero', () => {
    renderCard();

    expect(screen.queryByTestId('summary-card-blockers')).not.toBeInTheDocument();
    expect(screen.queryByTestId('summary-card-questions')).not.toBeInTheDocument();
    expect(screen.queryByTestId('summary-card-bugs')).not.toBeInTheDocument();
    expect(screen.queryByTestId('summary-card-feedback')).not.toBeInTheDocument();
    expect(screen.queryByTestId('summary-card-routine')).not.toBeInTheDocument();
    expect(screen.queryByTestId('summary-card-alerts')).not.toBeInTheDocument();
  });

  test('shows blockers when positive', () => {
    renderCard({ blockers: 1 });
    expect(screen.getByTestId('summary-card-blockers')).toBeInTheDocument();
    expect(screen.getByTestId('summary-card-blockers')).toHaveTextContent('1 new blocker');
  });

  test('shows questions with singular and plural copy', () => {
    const { rerender } = render(<MessagesSummaryCard {...{
      newMessageCounts: { blockers: 0, questions: 1, feedback: 0, bugReports: 0, routine: 0, highSignalNotes: 0 },
      onScrollToBlockers: vi.fn(),
      onScrollToQuestions: vi.fn(),
      onScrollToRoutine: vi.fn(),
    }} />);

    expect(screen.getByTestId('summary-card-questions')).toHaveTextContent('1 question waiting');

    rerender(<MessagesSummaryCard {...{
      newMessageCounts: { blockers: 0, questions: 3, feedback: 0, bugReports: 0, routine: 0, highSignalNotes: 0 },
      onScrollToBlockers: vi.fn(),
      onScrollToQuestions: vi.fn(),
      onScrollToRoutine: vi.fn(),
    }} />);

    expect(screen.getByTestId('summary-card-questions')).toHaveTextContent('3 questions waiting');
  });

  test('shows the remaining rows when positive', () => {
    renderCard({ feedback: 2, bugReports: 1, routine: 4, highSignalNotes: 3 });

    expect(screen.getByTestId('summary-card-feedback')).toBeInTheDocument();
    expect(screen.getByTestId('summary-card-bugs')).toBeInTheDocument();
    expect(screen.getByTestId('summary-card-routine')).toBeInTheDocument();
    expect(screen.getByTestId('summary-card-alerts')).toBeInTheDocument();
  });

  test('clicking rows calls the matching scroll handlers', async () => {
    const user = userEvent.setup();
    const props = renderCard({ blockers: 1, questions: 1, routine: 1, feedback: 1, bugReports: 1, highSignalNotes: 1 });

    await user.click(screen.getByTestId('summary-card-blockers'));
    await user.click(screen.getByTestId('summary-card-questions'));
    await user.click(screen.getByTestId('summary-card-routine'));
    await user.click(screen.getByTestId('summary-card-bugs'));
    await user.click(screen.getByTestId('summary-card-feedback'));
    await user.click(screen.getByTestId('summary-card-alerts'));

    expect(props.onScrollToBlockers).toHaveBeenCalledTimes(4);
    expect(props.onScrollToQuestions).toHaveBeenCalledTimes(1);
    expect(props.onScrollToRoutine).toHaveBeenCalledTimes(1);
  });
});
