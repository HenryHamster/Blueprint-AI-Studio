type MessageSummaryCounts = {
  blockers: number;
  questions: number;
  feedback: number;
  bugReports: number;
  routine: number;
  highSignalNotes: number;
};

interface MessagesSummaryCardProps {
  newMessageCounts: MessageSummaryCounts;
  onScrollToBlockers: () => void;
  onScrollToQuestions: () => void;
  onScrollToRoutine: () => void;
}

export function MessagesSummaryCard({ newMessageCounts, onScrollToBlockers, onScrollToQuestions, onScrollToRoutine }: MessagesSummaryCardProps) {
  return (
    <div className="mx-2 my-3 p-3 rounded-lg bg-gradient-to-r from-blue-950/40 to-blue-900/30 border border-blue-800/40 text-ui-md text-blue-200/90">
      <div className="font-semibold mb-2 text-blue-300">Since you last looked</div>
      <div className="space-y-1.5">
        {newMessageCounts.blockers > 0 && (
          <button
            onClick={onScrollToBlockers}
            className="block w-full text-left hover:text-blue-100 transition-colors"
            data-testid="summary-card-blockers"
          >
            <span className="font-medium text-amber-300">{newMessageCounts.blockers}</span> new blocker{newMessageCounts.blockers !== 1 ? 's' : ''}
          </button>
        )}
        {newMessageCounts.questions > 0 && (
          <button
            onClick={onScrollToQuestions}
            className="block w-full text-left hover:text-blue-100 transition-colors"
            data-testid="summary-card-questions"
          >
            <span className="font-medium text-blue-300">{newMessageCounts.questions}</span> question{newMessageCounts.questions !== 1 ? 's' : ''} waiting
          </button>
        )}
        {newMessageCounts.bugReports > 0 && (
          <button
            onClick={onScrollToBlockers}
            className="block w-full text-left hover:text-blue-100 transition-colors"
            data-testid="summary-card-bugs"
          >
            <span className="font-medium text-red-300">{newMessageCounts.bugReports}</span> new bug{newMessageCounts.bugReports !== 1 ? 's' : ''}
          </button>
        )}
        {newMessageCounts.feedback > 0 && (
          <button
            onClick={onScrollToBlockers}
            className="block w-full text-left hover:text-blue-100 transition-colors"
            data-testid="summary-card-feedback"
          >
            <span className="font-medium text-purple-300">{newMessageCounts.feedback}</span> new feedback
          </button>
        )}
        {newMessageCounts.routine > 0 && (
          <button
            onClick={onScrollToRoutine}
            className="block w-full text-left hover:text-blue-100 transition-colors"
            data-testid="summary-card-routine"
          >
            <span className="font-medium text-neutral-300">{newMessageCounts.routine}</span> routine ops
          </button>
        )}
        {newMessageCounts.highSignalNotes > 0 && (
          <button
            onClick={onScrollToBlockers}
            className="block w-full text-left hover:text-blue-100 transition-colors"
            data-testid="summary-card-alerts"
          >
            <span className="font-medium text-orange-300">{newMessageCounts.highSignalNotes}</span> alert{newMessageCounts.highSignalNotes !== 1 ? 's' : ''}
          </button>
        )}
      </div>
    </div>
  );
}
