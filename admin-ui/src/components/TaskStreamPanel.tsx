import { useState, useRef, useEffect } from 'react';
import { X, Radio, ChevronDown, ChevronUp, Trash2 } from 'lucide-react';
import type { Task, TrackLogEntry, TrackLogType } from '../types/task';
import { formatTimeOnly } from '../utils/time';

interface TaskStreamPanelProps {
  trackedTaskIds: Set<string>;
  trackedLogs: Map<string, TrackLogEntry[]>;
  tasks: Task[];
  onUntrack: (taskId: string) => void;
  onUntrackAll: () => void;
  offsetRight?: number;
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
    default: return 'bg-neutral-600';
  }
}

export default function TaskStreamPanel({ trackedTaskIds, trackedLogs, tasks, onUntrack, onUntrackAll, offsetRight }: TaskStreamPanelProps) {
  const ids = Array.from(trackedTaskIds);
  const [selectedTab, setSelectedTab] = useState<string>(ids[0] || '');
  const [collapsed, setCollapsed] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const [autoScroll, setAutoScroll] = useState(true);

  // Derive active tab: use selectedTab if still tracked, otherwise fall back to first
  const activeTab = trackedTaskIds.has(selectedTab) ? selectedTab : (ids[0] || '');

  // Auto-scroll to bottom on new entries
  const activeEntries = trackedLogs.get(activeTab) || [];
  useEffect(() => {
    const el = scrollRef.current;
    if (el && autoScroll) {
      el.scrollTop = el.scrollHeight;
    }
  }, [activeEntries.length, activeTab]);

  const handleScroll = () => {
    const el = scrollRef.current;
    if (!el) return;
    // If user scrolled up more than 40px from bottom, disable auto-scroll
    setAutoScroll(el.scrollHeight - el.scrollTop - el.clientHeight < 40);
  };

  if (ids.length === 0) return null;

  const activeTask = tasks.find(t => t.id === activeTab);

  return (
    <div className={`fixed bottom-0 z-40 flex flex-col bg-neutral-950 border-l border-t border-neutral-800 shadow-2xl transition-all ${collapsed ? 'h-10' : 'h-[420px]'}`}
      style={{ width: 400, right: offsetRight ?? 0 }}>
      {/* Header */}
      <div className="flex items-center justify-between px-3 py-2 border-b border-neutral-800 shrink-0 bg-neutral-900/80">
        <div className="flex items-center gap-2 text-xs text-neutral-300">
          <Radio className="w-3.5 h-3.5 text-blue-400" />
          <span className="font-semibold">Tracking ({ids.length})</span>
        </div>
        <div className="flex items-center gap-1">
          <button onClick={() => onUntrackAll()} className="text-neutral-500 hover:text-red-400 p-0.5 transition-colors" title="Untrack all">
            <Trash2 className="w-3.5 h-3.5" />
          </button>
          <button onClick={() => setCollapsed(c => !c)} className="text-neutral-500 hover:text-neutral-300 p-0.5 transition-colors" title={collapsed ? 'Expand' : 'Collapse'}>
            {collapsed ? <ChevronUp className="w-3.5 h-3.5" /> : <ChevronDown className="w-3.5 h-3.5" />}
          </button>
        </div>
      </div>

      {!collapsed && (
        <>
          {/* Task Tabs */}
          {ids.length > 1 && (
            <div className="flex items-center gap-1 px-2 py-1.5 border-b border-neutral-800 overflow-x-auto shrink-0 custom-scrollbar">
              {ids.map(id => {
                const t = tasks.find(tk => tk.id === id);
                const title = t?.title || id.slice(0, 8);
                const isActive = id === activeTab;
                return (
                  <button key={id} onClick={() => { setSelectedTab(id); setAutoScroll(true); }}
                    className={`flex items-center gap-1.5 px-2 py-1 rounded text-[10px] whitespace-nowrap shrink-0 transition-colors ${isActive ? 'bg-neutral-800 text-neutral-100' : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-900'}`}>
                    <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDotColor(t?.status)}`} />
                    <span className="max-w-[100px] truncate">{title}</span>
                    <button onClick={(e) => { e.stopPropagation(); onUntrack(id); }}
                      className="text-neutral-600 hover:text-red-400 ml-0.5 transition-colors">
                      <X className="w-3 h-3" />
                    </button>
                  </button>
                );
              })}
            </div>
          )}

          {/* Single-tab header with untrack */}
          {ids.length === 1 && activeTask && (
            <div className="flex items-center justify-between px-3 py-1.5 border-b border-neutral-800 shrink-0">
              <div className="flex items-center gap-1.5 text-[11px] text-neutral-400 min-w-0">
                <div className={`w-1.5 h-1.5 rounded-full shrink-0 ${statusDotColor(activeTask.status)}`} />
                <span className="truncate">{activeTask.title || activeTab.slice(0, 8)}</span>
              </div>
              <button onClick={() => onUntrack(activeTab)}
                className="text-neutral-600 hover:text-red-400 text-[10px] transition-colors shrink-0">
                Untrack
              </button>
            </div>
          )}

          {/* Log Stream */}
          <div ref={scrollRef} onScroll={handleScroll}
            className="flex-1 overflow-y-auto px-3 py-2 font-mono text-[11px] leading-relaxed custom-scrollbar">
            {activeEntries.length === 0 && (
              <div className="text-neutral-600 text-center py-8">Waiting for events...</div>
            )}
            {activeEntries.map(entry => (
              <div key={entry.id} className="flex gap-2 py-0.5">
                <span className="text-neutral-600 shrink-0 select-none">{formatTimeOnly(entry.t)}</span>
                <span className={`${LOG_TYPE_COLORS[entry.type]} break-words min-w-0`}>{entry.msg}</span>
              </div>
            ))}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between px-3 py-1 border-t border-neutral-800 text-[10px] text-neutral-600 shrink-0">
            <span>{activeEntries.length} entries</span>
            {!autoScroll && (
              <button onClick={() => { setAutoScroll(true); scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' }); }}
                className="text-blue-500 hover:text-blue-400 transition-colors">
                Scroll to bottom
              </button>
            )}
          </div>
        </>
      )}
    </div>
  );
}
