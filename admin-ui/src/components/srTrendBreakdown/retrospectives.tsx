/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useState } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react';
import type { SrMissedTask, SrUnhandledEdgesTask } from './types';

const WINDOW_OPTIONS: { label: string; days: number }[] = [
  { label: '7d', days: 7 },
  { label: '14d', days: 14 },
  { label: '30d', days: 30 },
];

interface SrMissedRetrospectiveProps {
  tasks: SrMissedTask[];
  onTaskClick?: (taskId: string) => void;
  selectedCategory?: string;
  windowDays?: 7 | 14 | 30;
  onWindowDaysChange?: (days: 7 | 14 | 30) => void;
  truncated?: boolean;
}

export function SrMissedRetrospective({
  tasks,
  onTaskClick,
  selectedCategory,
  windowDays = 14,
  onWindowDaysChange,
  truncated = false,
}: SrMissedRetrospectiveProps) {
  const [categoryFilter, setCategoryFilter] = useState(selectedCategory ?? '');
  useEffect(() => {
    setCategoryFilter(selectedCategory ?? '');
  }, [selectedCategory]);

  const [typeFilter, setTypeFilter] = useState('');
  const [openTaskIds, setOpenTaskIds] = useState<Set<string>>(() => new Set());
  const [cutoff, setCutoff] = useState(() => Date.now() - windowDays * 86400_000);

  useEffect(() => {
    setCutoff(Date.now() - windowDays * 86400_000);
  }, [windowDays]);

  const allCategories = Array.from(new Set(tasks.flatMap(task => task.missed_categories))).sort();
  const allTypes = Array.from(new Set(tasks.map(task => task.task_type).filter(Boolean) as string[])).sort();
  const visible = tasks.filter(task => {
    if (task.created_at && new Date(task.created_at).getTime() < cutoff) return false;
    if (categoryFilter && !task.missed_categories.includes(categoryFilter)) return false;
    if (typeFilter && task.task_type !== typeFilter) return false;
    return true;
  });
  const toggleTask = (taskId: string) => setOpenTaskIds(prev => {
    const next = new Set(prev);
    if (next.has(taskId)) next.delete(taskId); else next.add(taskId);
    return next;
  });

  return (
    <div data-testid="sr-missed-retrospective" className="flex flex-col gap-2">
      {truncated && (
        <div className="bg-amber-950/30 border border-amber-900/50 rounded px-2 py-1.5 text-ui-sm text-amber-300 flex items-start gap-2">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-0.5" />
          <span>Results truncated at 500 items. Task count and filters reflect this subset — apply filters to narrow results for visibility of the full dataset.</span>
        </div>
      )}
      <div className="flex flex-wrap items-center gap-2 text-ui-sm">
        <span className="text-neutral-500 uppercase tracking-wide">Time</span>
        {WINDOW_OPTIONS.map(window => (
          // eslint-disable-next-line viewport/min-touch-target -- dense toolbar preserves the existing compact drilldown controls
          <button
            key={window.days}
            onClick={() => onWindowDaysChange?.(window.days as 7 | 14 | 30)}
            className={`px-1.5 py-0.5 rounded border ${windowDays === window.days ? 'border-blue-500 text-blue-300' : 'border-neutral-700 text-neutral-400 hover:border-neutral-500'}`}
          >
            {window.label}
          </button>
        ))}
        {allCategories.length > 0 && (
          <select
            value={categoryFilter}
            onChange={event => setCategoryFilter(event.target.value)}
            className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300"
          >
            <option value="">All categories</option>
            {allCategories.map(category => <option key={category} value={category}>{category}</option>)}
          </select>
        )}
        {allTypes.length > 0 && (
          <select
            value={typeFilter}
            onChange={event => setTypeFilter(event.target.value)}
            className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300"
          >
            <option value="">All types</option>
            {allTypes.map(type => <option key={type} value={type}>{type}</option>)}
          </select>
        )}
        <span className="text-neutral-600">{visible.length} task{visible.length !== 1 ? 's' : ''}</span>
      </div>
      {visible.length === 0 ? (
        <p className="text-ui-sm text-neutral-600 italic">No missed tasks for this filter.</p>
      ) : (
        <div className="flex flex-col gap-1">
          {visible.map(task => (
            // eslint-disable-next-line viewport/min-touch-target -- task rows are intentionally compact and match the pre-refactor panel behavior
            <div
              key={task.task_id}
              className={`bg-neutral-900/50 border border-neutral-800/50 rounded px-2 py-1.5 text-ui-sm ${onTaskClick ? 'cursor-pointer hover:border-neutral-600' : ''}`}
              onClick={() => onTaskClick?.(task.task_id)}
            >
              <div className="flex items-center gap-1.5 mb-0.5">
                <button
                  type="button"
                  className="text-neutral-500 hover:text-neutral-300 shrink-0"
                  aria-label={`${openTaskIds.has(task.task_id) ? 'collapse' : 'expand'} ${task.title}`}
                  aria-expanded={openTaskIds.has(task.task_id)}
                  onClick={event => { event.stopPropagation(); toggleTask(task.task_id); }}
                >
                  {openTaskIds.has(task.task_id) ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                </button>
                <span className="text-neutral-200 truncate flex-1 font-medium" title={task.title}>{task.title}</span>
                {task.task_type && <span className="text-neutral-500 shrink-0">{task.task_type}</span>}
                {task.scope && <span className="text-neutral-600 shrink-0">{task.scope}</span>}
                {task.created_at && <span className="text-neutral-600 shrink-0" title={task.created_at}>{new Date(task.created_at).toLocaleDateString()}</span>}
                {task.pr_url && (
                  <a
                    href={task.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={event => event.stopPropagation()}
                    className="text-blue-400 hover:underline shrink-0"
                    title={task.pr_url}
                  >
                    PR
                  </a>
                )}
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                {task.missed_categories.length > 0 && <span className="text-red-400">missed: {task.missed_categories.join(', ')}</span>}
                {task.caught_categories.length > 0 && <span className="text-emerald-600">caught: {task.caught_categories.join(', ')}</span>}
              </div>
              {openTaskIds.has(task.task_id) && (
                <div className="mt-1.5 flex flex-col gap-1">
                  {task.missed_categories.map(category => {
                    const sr = task.sr_check_results?.[category];
                    return (
                      <div key={category} className="rounded border border-neutral-800/60 bg-neutral-950/50 p-1.5">
                        <div className="mb-1 text-neutral-400">{category}</div>
                        <div className="rounded border border-neutral-800/50 bg-neutral-900/60 p-1.5">
                          <div className="mb-0.5 text-ui-xs uppercase tracking-wide text-neutral-500">Self-review evidence</div>
                          {sr ? (
                            <>
                              <div className={`font-mono ${sr.passed ? 'text-emerald-400' : 'text-red-400'}`}>{sr.passed ? 'passed' : 'failed'}</div>
                              {sr.issue && <div className="text-neutral-400">{sr.issue}</div>}
                              {sr.origin && <div className="text-neutral-600">origin: {sr.origin}</div>}
                            </>
                          ) : (
                            <div className="text-neutral-500">not recorded</div>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

interface SrUnhandledEdgesRetrospectiveProps {
  tasks: SrUnhandledEdgesTask[];
  onTaskClick?: (taskId: string) => void;
  windowDays?: 7 | 14 | 30;
  onWindowDaysChange?: (days: 7 | 14 | 30) => void;
}

export function SrUnhandledEdgesRetrospective({
  tasks,
  onTaskClick,
  windowDays = 14,
  onWindowDaysChange,
}: SrUnhandledEdgesRetrospectiveProps) {
  const [typeFilter, setTypeFilter] = useState('');
  const [openTaskIds, setOpenTaskIds] = useState<Set<string>>(() => new Set());
  const [cutoff, setCutoff] = useState(() => Date.now() - windowDays * 86400_000);

  useEffect(() => {
    setCutoff(Date.now() - windowDays * 86400_000);
  }, [windowDays]);

  const allTypes = Array.from(new Set(tasks.map(task => task.task_type).filter(Boolean) as string[])).sort();
  const visible = tasks.filter(task => {
    if (task.created_at && new Date(task.created_at).getTime() < cutoff) return false;
    if (typeFilter && task.task_type !== typeFilter) return false;
    return true;
  });
  const toggleTask = (taskId: string) => setOpenTaskIds(prev => {
    const next = new Set(prev);
    if (next.has(taskId)) next.delete(taskId); else next.add(taskId);
    return next;
  });

  return (
    <div data-testid="sr-unhandled-edges-retrospective" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-ui-sm">
        <span className="text-neutral-500 uppercase tracking-wide">Time</span>
        {WINDOW_OPTIONS.map(window => (
          // eslint-disable-next-line viewport/min-touch-target -- dense toolbar preserves the existing compact drilldown controls
          <button
            key={window.days}
            onClick={() => onWindowDaysChange?.(window.days as 7 | 14 | 30)}
            className={`px-1.5 py-0.5 rounded border ${windowDays === window.days ? 'border-blue-500 text-blue-300' : 'border-neutral-700 text-neutral-400 hover:border-neutral-500'}`}
          >
            {window.label}
          </button>
        ))}
        {allTypes.length > 0 && (
          <select
            value={typeFilter}
            onChange={event => setTypeFilter(event.target.value)}
            className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300"
          >
            <option value="">All types</option>
            {allTypes.map(type => <option key={type} value={type}>{type}</option>)}
          </select>
        )}
        <span className="text-neutral-600">{visible.length} task{visible.length !== 1 ? 's' : ''}</span>
      </div>
      {visible.length === 0 ? (
        <p className="text-ui-sm text-neutral-600 italic">No tasks with unhandled edges for this filter.</p>
      ) : (
        <div className="flex flex-col gap-1">
          {visible.map(task => (
            // eslint-disable-next-line viewport/min-touch-target -- task rows are intentionally compact and match the pre-refactor panel behavior
            <div
              key={task.task_id}
              className={`bg-neutral-900/50 border border-neutral-800/50 rounded px-2 py-1.5 text-ui-sm ${onTaskClick ? 'cursor-pointer hover:border-neutral-600' : ''}`}
              onClick={() => onTaskClick?.(task.task_id)}
            >
              <div className="flex items-center gap-1.5 mb-0.5">
                <button
                  type="button"
                  className="text-neutral-500 hover:text-neutral-300 shrink-0"
                  aria-label={`${openTaskIds.has(task.task_id) ? 'collapse' : 'expand'} ${task.title}`}
                  aria-expanded={openTaskIds.has(task.task_id)}
                  onClick={event => { event.stopPropagation(); toggleTask(task.task_id); }}
                >
                  {openTaskIds.has(task.task_id) ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                </button>
                <span className="text-neutral-200 truncate flex-1 font-medium" title={task.title}>{task.title}</span>
                {task.task_type && <span className="text-neutral-500 shrink-0">{task.task_type}</span>}
                {task.scope && <span className="text-neutral-600 shrink-0">{task.scope}</span>}
                {task.created_at && <span className="text-neutral-600 shrink-0" title={task.created_at}>{new Date(task.created_at).toLocaleDateString()}</span>}
                {task.pr_url && (
                  <a
                    href={task.pr_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={event => event.stopPropagation()}
                    className="text-blue-400 hover:underline shrink-0"
                    title={task.pr_url}
                  >
                    PR
                  </a>
                )}
              </div>
              {openTaskIds.has(task.task_id) && (
                <div className="mt-1.5 flex flex-col gap-1">
                  {task.unhandled_edges.map((edge, index) => (
                    <div key={index} className="rounded border border-neutral-800/60 bg-neutral-950/50 p-1.5">
                      <div className="mb-1 text-neutral-400 font-medium text-ui-sm">{edge.case}</div>
                      {edge.blind_spot && <div className="text-neutral-500 text-ui-xs">{edge.blind_spot}</div>}
                      {edge.origin && <div className="text-neutral-600 text-ui-xs">source: {edge.origin}</div>}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
