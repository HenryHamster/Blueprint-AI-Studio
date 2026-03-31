import { useState } from 'react';
import { Square, CheckCircle, AlertCircle, Clock, SplitSquareHorizontal, Server, Hash, Activity, ExternalLink, Terminal, RotateCcw, Pencil, X, ChevronRight, Search, GitFork, Microscope, Shield } from 'lucide-react';

export type TaskStatus = 'QUEUED' | 'PROVISIONING' | 'IN_PROGRESS' | 'EVALUATING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
export type TaskType = 'create' | 'plan' | 'research' | 'review' | 'fix';

export interface Task {
  id: string;
  project_id?: string;
  title: string;
  description: string;
  status: TaskStatus;
  task_type?: TaskType;
  vm_instance_id?: string;
  latest_status_message?: string;
  created_by: string;
  requires_approval: boolean;
  parent_task_id?: string;
  depends_on?: string[];
  plan_output?: Record<string, unknown>;
  created_at: string;
  last_heartbeat?: string;
  attempt_count?: number;
  payloads?: Record<string, unknown>[];
  pr_url?: string;
  error_logs?: string;
  research_findings?: string;
}

const TASK_TYPE_STYLES: Record<string, { bg: string; text: string; icon: typeof Search; label: string }> = {
  plan: { bg: 'bg-purple-500/10 border-purple-500/20', text: 'text-purple-400', icon: GitFork, label: 'Plan' },
  research: { bg: 'bg-cyan-500/10 border-cyan-500/20', text: 'text-cyan-400', icon: Microscope, label: 'Research' },
  review: { bg: 'bg-amber-500/10 border-amber-500/20', text: 'text-amber-400', icon: Search, label: 'Review' },
  fix: { bg: 'bg-orange-500/10 border-orange-500/20', text: 'text-orange-400', icon: RotateCcw, label: 'Fix' },
};

interface TaskCardProps {
  task: Task;
  onApprove: (id: string) => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onViewLogs: (task: Task) => void;
}

function getHeartbeatStatus(lastHeartbeat?: string) {
  if (!lastHeartbeat) return { color: 'bg-neutral-600', text: 'No heartbeat' };

  const diffMinutes = (new Date().getTime() - new Date(lastHeartbeat).getTime()) / 60000;
  if (diffMinutes < 2) return { color: 'bg-emerald-500', text: 'Healthy (< 2m)' };
  if (diffMinutes < 5) return { color: 'bg-amber-500', text: 'Delayed (2-5m)' };
  return { color: 'bg-red-500', text: 'Stale (> 5m)' };
}

export function TaskCard({ task, onApprove, onCancel, onRetry, onEditPrompt, onDismiss, onViewLogs }: TaskCardProps) {
  const [editing, setEditing] = useState(false);
  const [editPrompt, setEditPrompt] = useState(task.description || '');
  const [statusExpanded, setStatusExpanded] = useState(false);
  const isChild = !!task.parent_task_id;
  const heartbeat = getHeartbeatStatus(task.last_heartbeat);
  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';

  return (
    <div className={`bg-neutral-900 p-4 rounded-xl border transition-shadow ${isChild ? 'border-l-4 border-l-neutral-500 border-y-neutral-800 border-r-neutral-800 shadow-sm' : 'border-neutral-800 shadow-sm hover:shadow-md hover:border-neutral-700'}`}>
      <div className="flex justify-between items-start mb-3">
        <div className="flex items-center gap-2 pr-2 overflow-hidden">
          {isChild && <span title="Child Task" className="shrink-0 flex items-center"><SplitSquareHorizontal className="w-4 h-4 text-neutral-500" /></span>}
          <h3 className="font-medium text-sm text-neutral-100 truncate" title={task.title}>{task.title}</h3>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          {task.task_type && TASK_TYPE_STYLES[task.task_type] && (() => {
            const style = TASK_TYPE_STYLES[task.task_type!];
            const Icon = style.icon;
            return (
              <span className={`${style.bg} ${style.text} border text-[10px] uppercase tracking-wider px-2 py-0.5 rounded font-semibold whitespace-nowrap flex items-center gap-1`}>
                <Icon className="w-3 h-3" />{style.label}
              </span>
            );
          })()}
          {task.requires_approval && (
            <span className="bg-amber-500/10 text-amber-500 border border-amber-500/20 text-[10px] uppercase tracking-wider px-2 py-0.5 rounded font-semibold whitespace-nowrap flex items-center gap-1">
              <Shield className="w-3 h-3" />Approval
            </span>
          )}
          {task.attempt_count && task.attempt_count > 0 && (
            <span className="bg-neutral-800 text-neutral-400 text-[10px] px-1.5 py-0.5 rounded font-mono">
              #{task.attempt_count + 1}
            </span>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-1 mb-3 text-[10px] text-neutral-500 font-mono">
        <div className="flex items-center gap-1.5" title="Task ID">
          <Hash className="w-3 h-3" />
          <span className="truncate">{task.id}</span>
        </div>
        {task.vm_instance_id && (
          <div className="flex items-center gap-1.5" title="VM Instance ID">
            <Server className="w-3 h-3" />
            <span className="truncate">{task.vm_instance_id}</span>
          </div>
        )}
      </div>

      <p className="text-xs text-neutral-400 line-clamp-2 mb-3">{task.description}</p>

      {task.latest_status_message && (
        <div className="bg-neutral-950 text-neutral-400 text-[11px] rounded-lg mb-3 border border-neutral-800/50 font-mono">
          <button
            onClick={() => setStatusExpanded(!statusExpanded)}
            className="w-full flex items-center gap-1.5 p-2.5 text-left hover:text-neutral-300 transition-colors"
          >
            <ChevronRight className={`w-3 h-3 shrink-0 text-neutral-500 transition-transform ${statusExpanded ? 'rotate-90' : ''}`} />
            <span className="font-semibold text-neutral-500 font-sans text-[11px]">Agent Status:</span>
            {!statusExpanded && (
              <span className="truncate ml-1">&gt; {task.latest_status_message.split('\n')[0].slice(0, 80)}</span>
            )}
          </button>
          {statusExpanded && (
            <div className="px-2.5 pb-2.5 whitespace-pre-wrap break-words">
              &gt; {task.latest_status_message}
            </div>
          )}
        </div>
      )}

      {/* PR link for completed tasks */}
      {task.status === 'COMPLETED' && task.pr_url && (
        <a
          href={task.pr_url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-xs px-3 py-2 rounded-lg mb-3 hover:bg-emerald-500/20 transition-colors font-medium"
        >
          <ExternalLink className="w-3.5 h-3.5" />
          View Pull Request
        </a>
      )}

      {/* Error log preview for failed tasks */}
      {task.status === 'FAILED' && task.error_logs && (
        <div className="bg-red-950/30 text-red-400 text-[11px] p-2.5 rounded-lg mb-3 border border-red-900/30 font-mono max-h-24 overflow-y-auto">
          <span className="font-semibold block mb-1 text-red-500 font-sans">Error:</span>
          {task.error_logs.slice(0, 500)}
        </div>
      )}

      {/* Research findings preview */}
      {task.research_findings && (
        <div className="bg-cyan-950/20 text-cyan-300 text-[11px] p-2.5 rounded-lg mb-3 border border-cyan-900/30 font-mono max-h-32 overflow-y-auto">
          <span className="font-semibold block mb-1 text-cyan-400 font-sans">Research Findings:</span>
          {(() => {
            try {
              const findings = JSON.parse(task.research_findings);
              if (Array.isArray(findings)) {
                return findings.slice(0, 5).map((f: Record<string, string>, i: number) => (
                  <div key={i} className="mb-1 text-neutral-300">
                    <span className={`inline-block text-[9px] uppercase px-1 rounded mr-1 ${f.risk === 'high' ? 'bg-red-900/40 text-red-400' : f.risk === 'medium' ? 'bg-amber-900/40 text-amber-400' : 'bg-emerald-900/40 text-emerald-400'}`}>{f.scope}/{f.risk}</span>
                    {f.title}
                  </div>
                ));
              }
            } catch { /* raw text fallback */ }
            return <span className="text-neutral-400">{task.research_findings.slice(0, 500)}</span>;
          })()}
        </div>
      )}

      {/* Plan subtasks preview */}
      {task.plan_output && (task.plan_output as Record<string, unknown>).subtasks && (
        <div className="bg-purple-950/20 text-purple-300 text-[11px] p-2.5 rounded-lg mb-3 border border-purple-900/30 max-h-32 overflow-y-auto">
          <span className="font-semibold block mb-1 text-purple-400 font-sans">Plan ({((task.plan_output as Record<string, unknown>).subtasks as unknown[]).length} subtasks):</span>
          {((task.plan_output as Record<string, unknown>).subtasks as Record<string, unknown>[]).map((s, i) => (
            <div key={i} className="mb-0.5 text-neutral-300 font-mono">
              {(s.depends_on_indices as number[] || []).length > 0 ? '  └─ ' : ''}{String(s.title)}
            </div>
          ))}
        </div>
      )}

      {/* Dependency info */}
      {task.depends_on && task.depends_on.length > 0 && (
        <div className="text-[10px] text-neutral-500 mb-3 font-mono flex items-center gap-1">
          <GitFork className="w-3 h-3" />
          Waiting on {task.depends_on.length} task{task.depends_on.length > 1 ? 's' : ''}
        </div>
      )}

      <div className="flex items-center justify-between text-[11px] text-neutral-500 mt-auto pt-3 border-t border-neutral-800">
        <div className="flex items-center gap-1.5">
          <Clock className="w-3 h-3" />
          {new Date(task.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </div>

        <div className="flex items-center gap-2">
          {isActive && (
            <div className="flex items-center gap-1.5" title={heartbeat.text}>
              <Activity className="w-3 h-3" />
              <div className={`w-1.5 h-1.5 rounded-full ${heartbeat.color}`} />
            </div>
          )}

          {/* View logs button for active/completed/failed tasks */}
          {task.status !== 'QUEUED' && (
            <button
              onClick={() => onViewLogs(task)}
              className="flex items-center gap-1 text-neutral-400 hover:text-neutral-200 transition-colors"
              title="View logs"
            >
              <Terminal className="w-3 h-3" />
            </button>
          )}
        </div>
      </div>

      {task.requires_approval && (
        <div className="mt-3 flex gap-2">
          <button onClick={() => onApprove(task.id)} className="flex-1 bg-emerald-500/10 hover:bg-emerald-500/20 text-emerald-500 border border-emerald-500/20 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5">
            <CheckCircle className="w-3.5 h-3.5" /> Approve
          </button>
          <button onClick={() => onCancel(task.id)} className="flex-1 bg-red-500/10 hover:bg-red-500/20 text-red-500 border border-red-500/20 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5">
            <AlertCircle className="w-3.5 h-3.5" /> Reject
          </button>
        </div>
      )}

      {!task.requires_approval && isActive && (
        <div className="mt-3 flex gap-2">
          <button onClick={() => onCancel(task.id)} className="flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5">
            <Square className="w-3.5 h-3.5" /> Cancel
          </button>
        </div>
      )}

      {task.status === 'FAILED' && (
        <div className="mt-3 space-y-2">
          {editing ? (
            <div className="space-y-2">
              <textarea
                value={editPrompt}
                onChange={e => setEditPrompt(e.target.value)}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1.5 text-xs resize-none focus:outline-none focus:ring-1 focus:ring-blue-500/50 h-20 font-mono"
              />
              <div className="flex gap-2">
                <button onClick={() => { onEditPrompt(task.id, editPrompt); setEditing(false); }}
                  className="flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-1.5 rounded text-xs font-medium transition-colors">
                  Save
                </button>
                <button onClick={() => { setEditing(false); setEditPrompt(task.description || ''); }}
                  className="px-3 bg-neutral-800 hover:bg-neutral-700 text-neutral-400 border border-neutral-700 py-1.5 rounded text-xs transition-colors">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <button onClick={() => onRetry(task.id)} className="flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5">
                <RotateCcw className="w-3.5 h-3.5" /> Retry
              </button>
              <button onClick={() => setEditing(true)} className="flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5">
                <Pencil className="w-3.5 h-3.5" /> Edit
              </button>
              <button onClick={() => onDismiss(task.id)} className="bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border border-neutral-700 py-1.5 px-2.5 rounded text-xs transition-colors flex items-center justify-center" title="Dismiss">
                <X className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
