import { Search } from 'lucide-react';

interface SearchFilterBarProps {
  searchQuery: string;
  onSearchChange: (query: string) => void;
  taskTypeFilter: string;
  onTypeFilterChange: (filter: string) => void;
  filteredCount: number;
  totalCount: number;
}

export function SearchFilterBar({ searchQuery, onSearchChange, taskTypeFilter, onTypeFilterChange, filteredCount, totalCount }: SearchFilterBarProps) {
  return (
    <div className="flex items-center gap-3 mb-4 shrink-0">
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-neutral-500" />
        <input
          type="text"
          value={searchQuery}
          onChange={e => onSearchChange(e.target.value)}
          placeholder="Search tasks..."
          className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg pl-9 pr-3 py-2 text-sm text-neutral-100 placeholder-neutral-600 focus:ring-1 focus:ring-neutral-700 w-64 outline-none"
        />
      </div>
      <span className="text-xs text-neutral-500">{filteredCount} of {totalCount}</span>
      <div className="flex items-center gap-1">
        {(['all', 'create', 'plan', 'research', 'review', 'fix'] as const).map(opt => (
          <button
            key={opt}
            onClick={() => onTypeFilterChange(opt)}
            className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all border ${
              taskTypeFilter === opt
                ? 'bg-white/5 border-neutral-600 text-neutral-100'
                : 'bg-neutral-950 border-neutral-800 text-neutral-500 hover:border-neutral-700'
            }`}
          >
            {opt.charAt(0).toUpperCase() + opt.slice(1)}
          </button>
        ))}
      </div>
    </div>
  );
}
