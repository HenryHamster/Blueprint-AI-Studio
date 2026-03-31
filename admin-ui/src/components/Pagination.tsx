interface PaginationProps {
  page: number;
  onPageChange: (page: number) => void;
  taskCount: number;
  limit: number;
}

export function Pagination({ page, onPageChange, taskCount, limit }: PaginationProps) {
  if (page <= 1 && taskCount < limit) return null;

  return (
    <div className="flex items-center justify-between px-6 py-2 border-t border-neutral-800 shrink-0">
      <div className="text-xs text-neutral-500">
        {taskCount > 0
          ? `Showing ${(page - 1) * limit + 1}–${(page - 1) * limit + taskCount}`
          : 'No tasks'}
      </div>
      <div className="flex gap-2">
        <button
          onClick={() => onPageChange(Math.max(1, page - 1))}
          disabled={page === 1}
          className="px-3 py-1 text-xs font-medium rounded-md bg-neutral-800 text-neutral-300 hover:bg-neutral-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          Previous
        </button>
        <button
          onClick={() => onPageChange(page + 1)}
          disabled={taskCount < limit}
          className="px-3 py-1 text-xs font-medium rounded-md bg-neutral-800 text-neutral-300 hover:bg-neutral-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
        >
          Next
        </button>
      </div>
    </div>
  );
}
