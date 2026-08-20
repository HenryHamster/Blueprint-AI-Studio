/* eslint-disable react-hooks/set-state-in-effect */
import { Search, ChevronDown, Check, GitFork, Timer } from 'lucide-react';
import { useRef, useState, useEffect } from 'react';
import type { RefObject } from 'react';
import type { DateRange } from '../hooks/useSearchFilters';
import { useTaskCapabilities } from '../taskCapabilities';
import { FAILURE_CLASS_CONFIG } from './TaskCard';

interface SearchFilterBarProps {
  searchQuery: string;
  onSearchChange: (query: string) => void;
  taskTypeFilter: string[];
  onTypeFilterChange: (filter: string[]) => void;
  failureClassFilter?: string[];
  onFailureClassFilterChange?: (filter: string[]) => void;
  dateRange: DateRange;
  onDateRangeChange: (range: DateRange) => void;
  blockedFilter: boolean;
  onBlockedFilterChange: (v: boolean) => void;
  stuckFilter?: boolean;
  onStuckFilterChange?: (v: boolean) => void;
  directionFilter?: string;
  onDirectionFilterChange?: (v: string) => void;
  directions?: { id: string; text: string }[];
  inputRef?: RefObject<HTMLInputElement | null>;
}

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

  const allSelected = selected.length === options.length;
  const buttonLabel = allSelected
    ? label
    : selected.length === 1
      ? options.find(o => o.value === selected[0])?.label ?? label
      : `${label} (${selected.length})`;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(prev => !prev)}
        className={`flex items-center gap-1.5 px-2.5 py-1.5 min-h-11 rounded-md text-xs font-medium transition-all border md:min-h-8 md:py-1 ${
          !allSelected
            ? 'bg-white/5 border-neutral-600 text-neutral-100'
            : 'bg-workbench-base/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
        }`}
      >
        <span>{buttonLabel}</span>
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute top-full mt-1 left-0 z-50 min-w-[140px] bg-workbench-base border border-neutral-700 rounded-lg shadow-xl overflow-hidden">
          <button
            onClick={() => { onChange(options.map(o => o.value)); setOpen(false); }}
            className={`w-full flex items-center justify-between px-3 py-2 text-xs hover:bg-workbench-hover transition-colors ${
              allSelected ? 'text-neutral-100' : 'text-neutral-400'
            }`}
          >
            <span>All</span>
            {allSelected && <Check className="w-3 h-3 text-neutral-400" />}
          </button>
          <div className="border-t border-neutral-800" />
          {options.map(opt => {
            const isSelected = selected.includes(opt.value);
            return (
              <button
                key={opt.value}
                onClick={() => toggle(opt.value)}
                className={`w-full flex items-center justify-between px-3 py-2 text-xs hover:bg-workbench-hover transition-colors ${
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

interface SingleSelectDropdownProps {
  options: { label: string; value: string }[];
  value: string;
  onChange: (value: string) => void;
  isActive?: (value: string) => boolean;
}

export function SingleSelectDropdown({ options, value, onChange, isActive }: SingleSelectDropdownProps) {
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

  const active = isActive ? isActive(value) : false;
  const currentLabel = options.find(o => o.value === value)?.label ?? value;

  return (
    <div ref={ref} className="relative">
      <button
        onClick={() => setOpen(prev => !prev)}
        className={`flex items-center gap-1.5 px-2.5 py-1.5 min-h-11 rounded-md text-xs font-medium transition-all border md:min-h-8 md:py-1 ${
          active
            ? 'bg-white/5 border-neutral-600 text-neutral-100'
            : 'bg-workbench-base/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
        }`}
      >
        <span>{currentLabel}</span>
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute top-full mt-1 left-0 z-50 min-w-[140px] bg-workbench-base border border-neutral-700 rounded-lg shadow-xl overflow-hidden">
          {options.map(opt => (
            <button
              key={opt.value}
              onClick={() => { onChange(opt.value); setOpen(false); }}
              className={`w-full flex items-center justify-between px-3 py-2 text-xs hover:bg-workbench-hover transition-colors ${
                opt.value === value ? 'text-neutral-100' : 'text-neutral-400'
              }`}
            >
              <span>{opt.label}</span>
              {opt.value === value && <Check className="w-3 h-3 text-neutral-400" />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const DATE_RANGE_OPTIONS: { label: string; value: DateRange }[] = [
  { label: 'All time', value: 'all' },
  { label: 'Last 1h', value: '1h' },
  { label: 'Last 6h', value: '6h' },
  { label: 'Last 24h', value: '24h' },
  { label: 'Last 7d', value: '7d' },
];

const DATE_RANGE_SELECT_OPTIONS: { label: string; value: string }[] = DATE_RANGE_OPTIONS.map(o => ({ label: o.label, value: o.value }));
const FAILURE_CLASS_OPTIONS = Object.entries(FAILURE_CLASS_CONFIG).map(([value, config]) => ({ value, label: config.label }));

export function SearchFilterBar({ searchQuery, onSearchChange, taskTypeFilter, onTypeFilterChange, failureClassFilter = [], onFailureClassFilterChange, dateRange, onDateRangeChange, blockedFilter, onBlockedFilterChange, stuckFilter = false, onStuckFilterChange, directionFilter = '', onDirectionFilterChange, directions, inputRef }: SearchFilterBarProps) {
  const [inputValue, setInputValue] = useState(searchQuery);
  const { filterOptions: typeOptions } = useTaskCapabilities();
  useEffect(() => {
    const timer = setTimeout(() => {
      onSearchChange(inputValue);
    }, 200);
    return () => clearTimeout(timer);
  }, [inputValue, onSearchChange]);

  useEffect(() => {
    setInputValue(searchQuery);
  }, [searchQuery]);

  const activeFilterCount = [
    searchQuery !== '',
    taskTypeFilter.length < typeOptions.length,
    onFailureClassFilterChange != null && failureClassFilter.length > 0,
    dateRange !== 'all',
    blockedFilter,
    stuckFilter,
    directionFilter !== '',
  ].filter(Boolean).length;

  const hasActiveFilters = activeFilterCount > 0;

  function handleClear() {
    onSearchChange('');
    onTypeFilterChange(typeOptions.map(o => o.value));
    onFailureClassFilterChange?.([]);
    onDateRangeChange('all');
    onBlockedFilterChange(false);
    onStuckFilterChange?.(false);
    onDirectionFilterChange?.('');
  }

  return (
    <>
      <div className="relative flex-1 min-w-0 md:flex-none">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-neutral-500" />
        <input
          ref={inputRef}
          type="text"
          value={inputValue}
          onChange={e => setInputValue(e.target.value)}
          placeholder="Search by title, ID, or status..."
          className="w-full rounded-md border border-neutral-800/70 bg-workbench-shell pl-9 pr-3 py-2 text-base text-neutral-100 placeholder-neutral-600 outline-none md:w-64 md:py-1.5 md:text-sm workbench-focus"
        />
      </div>

      <MultiSelectDropdown
        label="Type"
        options={typeOptions}
        selected={taskTypeFilter}
        onChange={onTypeFilterChange}
      />

      {onFailureClassFilterChange && (
        <MultiSelectDropdown
          label="Failure class"
          options={FAILURE_CLASS_OPTIONS}
          selected={failureClassFilter}
          onChange={onFailureClassFilterChange}
        />
      )}

      <SingleSelectDropdown
        options={DATE_RANGE_SELECT_OPTIONS}
        value={dateRange}
        onChange={v => onDateRangeChange(v as DateRange)}
        isActive={v => v !== 'all'}
      />

      <button
        onClick={() => onBlockedFilterChange(!blockedFilter)}
        className={`flex items-center gap-1.5 px-2.5 py-1.5 min-h-11 rounded-md text-xs font-medium transition-all border md:min-h-8 md:py-1 ${
          blockedFilter
            ? 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300'
            : 'bg-workbench-base/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
        }`}
      >
        <GitFork className="w-3 h-3" />
        <span>Blocked</span>
      </button>

      {onStuckFilterChange && (
        <button
          onClick={() => onStuckFilterChange(!stuckFilter)}
          className={`flex items-center gap-1.5 px-2.5 py-1.5 min-h-11 rounded-md text-xs font-medium transition-all border md:min-h-8 md:py-1 ${
            stuckFilter
              ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
              : 'bg-workbench-base/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
          }`}
        >
          <Timer className="w-3 h-3" />
          <span>Stuck</span>
        </button>
      )}

      {onDirectionFilterChange && (
        <SingleSelectDropdown
          options={[
            { label: 'All directions', value: '' },
            ...(directions ?? []).map(d => ({
              label: d.text.length > 40 ? d.text.slice(0, 40) + '…' : d.text,
              value: d.id,
            })),
          ]}
          value={directionFilter}
          onChange={onDirectionFilterChange}
          isActive={opt => opt === directionFilter && directionFilter !== ''}
        />
      )}

      {hasActiveFilters && (
        <button
          onClick={handleClear}
          className="min-h-11 rounded-md border border-red-900/50 bg-red-950/30 px-2.5 py-1.5 text-xs font-medium text-red-400 transition-all hover:border-red-700/60 hover:text-red-300 md:min-h-8 md:py-1"
        >
          Clear
        </button>
      )}
    </>
  );
}
