import { Search, ChevronDown, Check } from 'lucide-react';
import { useRef, useState, useEffect } from 'react';

interface SearchFilterBarProps {
  searchQuery: string;
  onSearchChange: (query: string) => void;
  taskTypeFilter: string[];
  onTypeFilterChange: (filter: string[]) => void;
  statusFilter: string[];
  onStatusFilterChange: (status: string[]) => void;
  filteredCount: number;
  totalCount: number;
}

const TYPE_OPTIONS = ['create', 'plan', 'research', 'review', 'fix'] as const;

const STATUS_OPTIONS: { label: string; value: string }[] = [
  { label: 'Queued', value: 'QUEUED' },
  { label: 'Provisioning', value: 'PROVISIONING' },
  { label: 'In Progress', value: 'IN_PROGRESS' },
  { label: 'Evaluating', value: 'EVALUATING' },
  { label: 'Failed', value: 'FAILED' },
  { label: 'Cancelled', value: 'CANCELLED' },
  { label: 'Completed', value: 'COMPLETED' },
];

interface MultiSelectDropdownProps {
  label: string;
  options: { label: string; value: string }[];
  selected: string[];
  onChange: (selected: string[]) => void;
}

function MultiSelectDropdown({ label, options, selected, onChange }: MultiSelectDropdownProps) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    if (open) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  function toggle(value: string) {
    if (selected.includes(value)) {
      onChange(selected.filter(v => v !== value));
    } else {
      onChange([...selected, value]);
    }
  }

  const buttonLabel = selected.length === 0
    ? label
    : selected.length === 1
      ? options.find(o => o.value === selected[0])?.label ?? label
      : `${label} (${selected.length})`;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(prev => !prev)}
        className={`flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-xs font-medium transition-all border ${
          selected.length > 0
            ? 'bg-white/5 border-neutral-600 text-neutral-100'
            : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
        }`}
      >
        <span>{buttonLabel}</span>
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute top-full mt-1 left-0 z-50 min-w-[140px] bg-neutral-900 border border-neutral-700 rounded-lg shadow-xl overflow-hidden">
          <button
            onClick={() => { onChange([]); setOpen(false); }}
            className={`w-full flex items-center justify-between px-3 py-2 text-xs hover:bg-neutral-800 transition-colors ${
              selected.length === 0 ? 'text-neutral-100' : 'text-neutral-400'
            }`}
          >
            <span>All</span>
            {selected.length === 0 && <Check className="w-3 h-3 text-neutral-400" />}
          </button>
          <div className="border-t border-neutral-800" />
          {options.map(opt => {
            const isSelected = selected.includes(opt.value);
            return (
              <button
                key={opt.value}
                onClick={() => toggle(opt.value)}
                className={`w-full flex items-center justify-between px-3 py-2 text-xs hover:bg-neutral-800 transition-colors ${
                  isSelected ? 'text-neutral-100' : 'text-neutral-400'
                }`}
              >
                <span>{opt.label}</span>
                {isSelected && <Check className="w-3 h-3 text-neutral-400" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function SearchFilterBar({ searchQuery, onSearchChange, taskTypeFilter, onTypeFilterChange, statusFilter, onStatusFilterChange, filteredCount, totalCount }: SearchFilterBarProps) {
  const activeFilterCount = [
    searchQuery !== '',
    taskTypeFilter.length > 0,
    statusFilter.length > 0,
  ].filter(Boolean).length;

  const hasActiveFilters = activeFilterCount > 0;

  function handleClear() {
    onSearchChange('');
    onTypeFilterChange([]);
    onStatusFilterChange([]);
  }

  const typeOptions = TYPE_OPTIONS.map(opt => ({ label: opt.charAt(0).toUpperCase() + opt.slice(1), value: opt }));

  return (
    <div className="flex items-center gap-2 mb-4 shrink-0 flex-wrap">
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

      <MultiSelectDropdown
        label="Type"
        options={typeOptions}
        selected={taskTypeFilter}
        onChange={onTypeFilterChange}
      />

      <MultiSelectDropdown
        label="Status"
        options={STATUS_OPTIONS}
        selected={statusFilter}
        onChange={onStatusFilterChange}
      />

      <span className="text-xs text-neutral-500">
        {filteredCount} of {totalCount}
        {hasActiveFilters && (
          <span className="ml-1.5 px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 text-xs font-medium">
            {activeFilterCount} {activeFilterCount === 1 ? 'filter' : 'filters'}
          </span>
        )}
      </span>

      {hasActiveFilters && (
        <button
          onClick={handleClear}
          className="px-2.5 py-1 rounded-lg text-xs font-medium transition-all border bg-red-950/30 border-red-900/50 text-red-400 hover:border-red-700/60 hover:text-red-300"
        >
          Clear
        </button>
      )}
    </div>
  );
}
