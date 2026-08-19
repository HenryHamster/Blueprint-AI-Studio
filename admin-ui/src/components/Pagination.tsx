interface PaginationProps {
  page: number;
  onPageChange: (page: number) => void;
  taskCount: number;
  serverTaskCount: number;
  limit: number;
  onLimitChange: (limit: number) => void;
  totalTasks: number;
}

export function Pagination({ page, onPageChange, taskCount, serverTaskCount, limit, onLimitChange, totalTasks }: PaginationProps) {
  if (page <= 1 && serverTaskCount < limit && totalTasks <= limit) return null;

  const totalPages = Math.max(1, Math.ceil(totalTasks / limit));
  const isLastPage = (page - 1) * limit + serverTaskCount >= totalTasks;

  return (
    <div className="flex flex-col md:flex-row md:items-center md:justify-between px-6 py-2 border-t border-neutral-800 shrink-0 gap-3 md:gap-0">
      <div className="flex items-center gap-3 text-xs text-neutral-500">
        <span>
          {taskCount > 0
            ? `Showing ${(page - 1) * limit + 1}–${(page - 1) * limit + taskCount} of ${totalTasks}`
            : 'No tasks'}
        </span>
        <select
          value={limit}
          onChange={(e) => {
            onLimitChange(Number(e.target.value));
            onPageChange(1);
          }}
          className="bg-workbench-panel border border-neutral-700 text-neutral-300 text-xs rounded px-2 py-1"
        >
          <option value={25}>25 / page</option>
          <option value={50}>50 / page</option>
          <option value={100}>100 / page</option>
          <option value={200}>200 / page</option>
          <option value={10000}>All</option>
        </select>
      </div>
      <div className="flex items-center gap-3">
        <span className="text-xs text-neutral-500">Page {page} of {totalPages}</span>
        <div className="flex gap-2">
          <button
            onClick={() => onPageChange(Math.max(1, page - 1))}
            disabled={page === 1}
            className="px-3 py-1.5 min-h-11 text-xs font-medium rounded-md bg-workbench-panel text-neutral-300 hover:bg-workbench-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center"
          >
            Previous
          </button>
          <button
            onClick={() => onPageChange(page + 1)}
            disabled={isLastPage}
            className="px-3 py-1.5 min-h-11 text-xs font-medium rounded-md bg-workbench-panel text-neutral-300 hover:bg-workbench-hover disabled:opacity-50 disabled:cursor-not-allowed transition-colors flex items-center"
          >
            Next
          </button>
        </div>
      </div>
    </div>
  );
}
