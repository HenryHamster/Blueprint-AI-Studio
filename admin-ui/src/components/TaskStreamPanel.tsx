import { useState, useRef, useEffect } from 'react';
import { X, Radio, ChevronDown, ChevronUp, Trash2 } from 'lucide-react';
import type { Task, TrackLogEntry, TrackLogType } from '../types/task';
import { formatTimeOnly, timeAgo } from '../utils/time';

export type TaskStreamPanelVariant = 'floating' | 'embedded';

export interface TaskStreamPanelProps {
  trackedTaskIds: Set<string>;
  trackedLogs: Map<string, TrackLogEntry[]>;
  tasks: Task[];
  onUntrack: (taskId: string) => void;
  onUntrackAll: () => void;
  offsetRight?: number;
  hideOnDesktop?: boolean;
  variant?: TaskStreamPanelVariant;
}

const LOG_TYPE_COLORS: Record<TrackLogType, string> = {
  log: 'text-neutral-300',
  status: 'text-blue-400',
  artifact: 'text-emerald-400',
  error: 'text-red-400',
  system: 'text-neutral-500 italic',
};

function statusDotColor(status?: string): string {
  switch (status) {
    case 'IN_PROGRESS': return 'bg-blue-500';
    case 'PROVISIONING': return 'bg-amber-500';
    case 'COMPLETED': return 'bg-emerald-500';
    case 'FAILED': return 'bg-red-500';
    case 'CANCELLED': return 'bg-neutral-500';
    case 'QUEUED': return 'bg-neutral-400';
    case 'BLOCKED': return 'bg-amber-500';
    case 'PARKED': return 'bg-purple-500';
    default: return 'bg-neutral-600';
  }
}

function streamFreshnessLabel(task: Task | undefined, entries: TrackLogEntry[]): string {
  const lastEntry = entries.at(-1);
  if (lastEntry) return `last event ${timeAgo(lastEntry.t)}`;
  if (task?.status === 'IN_PROGRESS') return 'in progress, no events yet';
  if (task?.status === 'PROVISIONING') return 'starting, no events yet';
  if (task?.status === 'EVALUATING') return 'evaluating, no events yet';
  if (task?.status === 'QUEUED') return 'queued, no events yet';
  return 'no events yet';
}

export default function TaskStreamPanel({
  trackedTaskIds,
  trackedLogs,
  tasks,
  onUntrack,
  onUntrackAll,
  offsetRight,
  hideOnDesktop = false,
  variant = 'floating',
}: TaskStreamPanelProps) {
  const ids = Array.from(trackedTaskIds);
  const [selectedTab, setSelectedTab] = useState<string>(ids[0] || '');
  const [collapsed, setCollapsed] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);
  const embedded = variant === 'embedded';
  const isCollapsed = !embedded && collapsed;
  const [, setFreshnessTick] = useState(() => Date.now());

  // Derive active tab: use selectedTab if still tracked, otherwise fall back to first
  const activeTab = trackedTaskIds.has(selectedTab) ? selectedTab : (ids[0] || '');

  // Auto-scroll to bottom on new entries
  const activeEntries = trackedLogs.get(activeTab) || [];
  useEffect(() => {
    const el = scrollRef.current;
    if (el && autoScroll) {
      el.scrollTop = el.scrollHeight;
    }
  }, [activeEntries.length, activeTab, autoScroll]);

  useEffect(() => {
    if (isCollapsed) return undefined;
    const id = window.setInterval(() => setFreshnessTick(Date.now()), 30_000);
    return () => window.clearInterval(id);
  }, [isCollapsed]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    // If user scrolled up more than 40px from bottom, disable auto-scroll
    setAutoScroll(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  };

  if (ids.length === 0) return null;

  const activeTask = tasks.find(t => t.id === activeTab);
  const panelClassName = embedded
    ? 'flex h-[360px] max-h-[52vh] min-h-[260px] min-w-0 flex-col overflow-hidden rounded-md border border-neutral-800 bg-[#10151c]'
    : `fixed bottom-0 z-40 flex flex-col bg-neutral-950 border-l border-t border-neutral-800 shadow-2xl transition-all ${hideOnDesktop ? 'lg:hidden' : ''} ${isCollapsed ? 'h-10' : 'h-[420px]'}`;
  const headerClassName = embedded
    ? 'flex shrink-0 items-center justify-between gap-2 border-b border-neutral-800 bg-[#0b1118] px-3 py-2.5'
    : 'flex items-center justify-between px-3 py-2 border-b border-neutral-800 shrink-0 bg-neutral-900/80';
  const tabStripClassName = embedded
    ? 'flex shrink-0 items-center gap-1 overflow-x-auto border-b border-neutral-800 bg-[#0b1118]/60 px-2 py-1.5 custom-scrollbar'
    : 'flex items-center gap-1 px-2 py-1.5 border-b border-neutral-800 overflow-x-auto shrink-0 custom-scrollbar';
  const singleHeaderClassName = embedded
    ? 'flex shrink-0 items-center justify-between gap-2 border-b border-neutral-800 bg-[#0b1118]/45 px-3 py-2'
    : 'flex items-center justify-between px-3 py-1.5 border-b border-neutral-800 shrink-0';
  const logClassName = embedded
    ? 'min-h-0 flex-1 overflow-y-auto px-2.5 py-2 font-mono text-ui-sm leading-snug custom-scrollbar'
    : 'flex-1 overflow-y-auto px-3 py-2 font-mono text-ui-md leading-relaxed custom-scrollbar';
  const footerClassName = embedded
    ? 'flex shrink-0 items-center justify-between border-t border-neutral-800 bg-[#0b1118]/60 px-3 py-1.5 text-ui-sm text-neutral-600'
    : 'flex items-center justify-between px-3 py-1 border-t border-neutral-800 text-ui-sm text-neutral-600 shrink-0';

  return (
    <div
      data-testid="task-stream-panel"
      data-variant={variant}
      className={panelClassName}
      style={embedded ? undefined : { width: 'min(400px, 100vw)', right: offsetRight ?? 0 }}
    >
      {/* Header */}
      <div className={headerClassName}>
        <div className="flex min-w-0 items-center gap-2 text-xs text-neutral-300">
          <Radio className="h-3.5 w-3.5 shrink-0 text-blue-400" />
          <span className="truncate font-semibold">{embedded ? 'Task stream' : `Tracking (${ids.length})`}</span>
          {embedded && <span className="shrink-0 font-mono text-ui-sm text-neutral-500">{ids.length} tracked</span>}
        </div>
        <div className="flex shrink-0 items-center gap-1">
          <button type="button" onClick={() => onUntrackAll()} className={`${embedded ? 'rounded border border-neutral-800 bg-neutral-950/50 p-1' : 'p-0.5'} text-neutral-500 transition-colors hover:text-red-400`} title="Untrack all">
            <Trash2 className="h-3.5 w-3.5" />
          </button>
          {!embedded && (
            // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
            <button type="button" onClick={() => setCollapsed(c => !c)} className="p-0.5 text-neutral-500 transition-colors hover:text-neutral-300" title={collapsed ? 'Expand' : 'Collapse'}>
              {collapsed ? <ChevronUp className="h-3.5 w-3.5" /> : <ChevronDown className="h-3.5 w-3.5" />}
            </button>
          )}
        </div>
      </div>

      {!isCollapsed && (
        <>
          {/* Task Tabs */}
          {ids.length > 1 && (
            <div className={tabStripClassName}>
              {ids.map(id => {
                const t = tasks.find(tk => tk.id === id);
                const title = t?.title || id.slice(0, 8);
                const isActive = id === activeTab;
                return (
                  <div key={id}
                    className={`flex shrink-0 items-center rounded text-ui-sm whitespace-nowrap transition-colors ${isActive ? 'bg-neutral-800 text-neutral-100' : 'text-neutral-500 hover:bg-neutral-900 hover:text-neutral-300'}`}>
                    {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                    <button type="button" onClick={() => { setSelectedTab(id); setAutoScroll(true); }}
                      className="flex min-w-0 items-center gap-1.5 px-2 py-1 text-left">
                      <div className={`h-1.5 w-1.5 shrink-0 rounded-full ${statusDotColor(t?.status)}`} />
                      <span className="max-w-[100px] truncate">{title}</span>
                    </button>
                    {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                    <button type="button" onClick={() => onUntrack(id)}
                      className="mr-1 p-0.5 text-neutral-600 transition-colors hover:text-red-400">
                      <X className="h-3 w-3" />
                    </button>
                  </div>
                );
              })}
            </div>
          )}

          {/* Single-tab header with untrack */}
          {ids.length === 1 && (
            <div className={singleHeaderClassName}>
              <div className="flex min-w-0 items-center gap-1.5 text-ui-md text-neutral-400">
                <div className={`h-1.5 w-1.5 shrink-0 rounded-full ${statusDotColor(activeTask?.status)}`} />
                <span className="truncate">{activeTask?.title || activeTab.slice(0, 8)}</span>
              </div>
              <button type="button" onClick={() => onUntrack(activeTab)}
                className="shrink-0 text-ui-sm text-neutral-600 transition-colors hover:text-red-400">
                Untrack
              </button>
            </div>
          )}

          {/* Log Stream */}
          <div ref={scrollRef} onScroll={handleScroll}
            className={logClassName}>
            {activeEntries.length === 0 && (
              <div className="py-8 text-center text-neutral-600">{embedded ? 'Waiting for events' : 'Waiting for events...'}</div>
            )}
            {activeEntries.map(entry => (
              <div key={entry.id} className="flex gap-2 py-0.5">
                <span className="shrink-0 select-none text-neutral-600">{formatTimeOnly(entry.t)}</span>
                <span className={`${LOG_TYPE_COLORS[entry.type]} min-w-0 break-words`}>{entry.msg}</span>
              </div>
            ))}
          </div>

          {/* Footer */}
          <div className={footerClassName}>
            <div className="flex min-w-0 items-center gap-2">
              <span className="shrink-0">{activeEntries.length} entries</span>
              <span className="min-w-0 truncate">{streamFreshnessLabel(activeTask, activeEntries)}</span>
            </div>
            {!autoScroll && (
              <button type="button" onClick={() => { setAutoScroll(true); scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }); }}
                className="text-blue-500 transition-colors hover:text-blue-400">
                Scroll to bottom
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
