import { useState } from 'react';
import { AlertTriangle, Activity, CheckCircle, Check, ChevronDown, ChevronUp } from 'lucide-react';
import type { Task } from './TaskCard';
import { isSafeUrl } from './TaskCard';

interface SummaryPanelsProps {
  actionRequiredTasks: Task[];
  runningTasks: Task[];
  completedRecently: Task[];
  queuedTasks: Task[];
  onSelectTask: (task: Task) => void;
  onApproveTask: (taskId: string) => void;
}

export function SummaryPanels({ actionRequiredTasks, runningTasks, completedRecently, queuedTasks, onSelectTask, onApproveTask }: SummaryPanelsProps) {
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem('summaryPanelsCollapsed') === 'true');

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem('summaryPanelsCollapsed', String(next));
  };

  return (
    <div className="mb-6 shrink-0">
      <button
        onClick={toggle}
        className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2 hover:text-neutral-300 transition-colors"
      >
        {collapsed ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
        Summary
        {actionRequiredTasks.length > 0 && (
          <span className="text-[10px] normal-case tracking-normal font-medium text-amber-400 ml-1">
            {actionRequiredTasks.length} action required
          </span>
        )}
      </button>
      {!collapsed && (
    <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4 flex flex-col gap-3 min-h-[120px] max-h-[200px]">
        <div className="flex items-center justify-between">
          <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest flex items-center gap-2">
            <AlertTriangle className="w-3.5 h-3.5 text-amber-500" /> Action Required
          </h3>
          <span className="text-[10px] font-medium bg-neutral-800 px-2 py-0.5 rounded-full border border-neutral-700">{actionRequiredTasks.length}</span>
        </div>
        <div className="flex-1 flex flex-col gap-2 overflow-y-auto custom-scrollbar">
          {actionRequiredTasks.map(t => (
            <div key={t.id} title={t.title} onClick={() => onSelectTask(t)} className="text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded flex justify-between items-center cursor-pointer hover:bg-neutral-800/50">
              <span className="truncate pr-2 text-neutral-300">{t.title}</span>
              <div className="shrink-0 flex items-center gap-1.5">
                {t.requires_approval && (t.status === 'IN_PROGRESS' || t.status === 'EVALUATING' || t.status === 'QUEUED') && (
                  <button
                    onClick={e => { e.stopPropagation(); onApproveTask(t.id); }}
                    className="flex items-center gap-0.5 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 border border-emerald-500/20"
                  >
                    <Check className="w-3 h-3" /> Approve
                  </button>
                )}
                <span className={`text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${t.requires_approval ? 'bg-amber-500/10 text-amber-500' : 'bg-red-500/10 text-red-500'}`}>
                  {t.requires_approval ? 'Approval' : 'Failed'}
                </span>
              </div>
            </div>
          ))}
          {actionRequiredTasks.length === 0 && <div className="text-xs text-neutral-600 m-auto flex items-center gap-1.5"><CheckCircle className="w-3.5 h-3.5" /> All clear</div>}
        </div>
      </div>

      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4 flex flex-col gap-3 min-h-[120px] max-h-[200px]">
        <div className="flex items-center justify-between">
          <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest flex items-center gap-2">
            <Activity className="w-3.5 h-3.5 text-blue-500" /> Active & Queued
          </h3>
          <span className="text-[10px] font-medium bg-neutral-800 px-2 py-0.5 rounded-full border border-neutral-700">{runningTasks.length} active / {queuedTasks.length} queued</span>
        </div>
        <div className="flex-1 flex flex-col gap-2 overflow-y-auto custom-scrollbar">
          {runningTasks.map(t => (
            <div key={t.id} title={t.title} onClick={() => onSelectTask(t)} className="text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded flex justify-between items-center cursor-pointer hover:bg-neutral-800/50">
              <span className="truncate pr-2 text-neutral-300">{t.title}</span>
              <span className={`shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${t.status === 'EVALUATING' ? 'bg-amber-500/10 text-amber-500' : 'bg-blue-500/10 text-blue-500'}`}>
                {t.status === 'PROVISIONING' ? 'Provisioning' : t.status === 'EVALUATING' ? 'Evaluating' : 'Running'}
              </span>
            </div>
          ))}
          {queuedTasks.map(t => (
            <div key={t.id} title={t.title} onClick={() => onSelectTask(t)} className="text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded flex justify-between items-center cursor-pointer hover:bg-neutral-800/50">
              <span className="truncate pr-2 text-neutral-300">{t.title}</span>
              <span className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-neutral-500/10 text-neutral-500">
                Queued
              </span>
            </div>
          ))}
          {runningTasks.length === 0 && queuedTasks.length === 0 && <div className="text-xs text-neutral-600 m-auto">No active tasks</div>}
        </div>
      </div>

      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4 flex flex-col gap-3 min-h-[120px] max-h-[200px]">
        <div className="flex items-center justify-between">
          <h3 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest flex items-center gap-2">
            <CheckCircle className="w-3.5 h-3.5 text-emerald-500" /> Completed (24h)
          </h3>
          <span className="text-[10px] font-medium bg-neutral-800 px-2 py-0.5 rounded-full border border-neutral-700">{completedRecently.length}</span>
        </div>
        <div className="flex-1 flex flex-col gap-2 overflow-y-auto custom-scrollbar">
          {completedRecently.map(t => (
            <div key={t.id} title={t.title} onClick={() => onSelectTask(t)} className="text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded flex justify-between items-center cursor-pointer hover:bg-neutral-800/50">
              <span className="truncate pr-2 text-neutral-400">{t.title}</span>
              {t.pr_url && isSafeUrl(t.pr_url) ? (
                <a href={t.pr_url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} className="shrink-0 text-[9px] text-emerald-400 hover:text-emerald-300 font-medium">PR</a>
              ) : (
                <span className="shrink-0 text-[10px] text-neutral-500 font-mono">
                  {new Date(t.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </span>
              )}
            </div>
          ))}
          {completedRecently.length === 0 && <div className="text-xs text-neutral-600 m-auto">No recent completions</div>}
        </div>
      </div>
    </div>
      )}
    </div>
  );
}
