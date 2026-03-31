import { useState, useRef, useEffect } from 'react';
import { Terminal, X, Copy, Check, ExternalLink, ChevronRight } from 'lucide-react';
import { StatusBadge, TaskTypeBadge, getHeartbeatStatus, isSafeUrl } from './TaskCard';
import type { Task } from './TaskCard';

interface TaskDetailDrawerProps {
  task: Task;
  logLines: string[];
  actionLoading?: string;
  onClose: () => void;
  onApprove: (id: string) => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
}

type Tab = 'details' | 'logs';

function CopyableId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  return (
    <button
      onClick={handleCopy}
      className="inline-flex items-center gap-1.5 font-mono text-sm text-neutral-200 hover:text-white transition-colors group"
    >
      <span>{value}</span>
      {copied ? (
        <Check className="w-3.5 h-3.5 text-emerald-400" />
      ) : (
        <Copy className="w-3.5 h-3.5 text-neutral-500 group-hover:text-neutral-300 transition-colors" />
      )}
    </button>
  );
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2 mt-5 first:mt-0">{children}</h3>;
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-1.5">
      <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">{label}</span>
      <div className="text-sm text-neutral-200 min-w-0">{children}</div>
    </div>
  );
}

function formatTimestamp(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function DetailsTab({ task }: { task: Task }) {
  const heartbeat = getHeartbeatStatus(task.last_heartbeat);

  return (
    <div className="flex-1 overflow-y-auto p-4 custom-scrollbar space-y-1">
      <SectionHeader>Identifiers</SectionHeader>
      <DetailRow label="Task ID"><CopyableId value={task.id} /></DetailRow>
      {task.project_id && <DetailRow label="Project ID"><span className="font-mono text-sm">{task.project_id}</span></DetailRow>}
      {task.vm_instance_id && <DetailRow label="VM Instance"><span className="font-mono text-sm">{task.vm_instance_id}</span></DetailRow>}

      <SectionHeader>Timing</SectionHeader>
      <DetailRow label="Created at">{formatTimestamp(task.created_at)}</DetailRow>
      <DetailRow label="Heartbeat">
        <span className="inline-flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${heartbeat.color}`} />
          <span className="text-sm">{heartbeat.text}</span>
          {task.last_heartbeat && (
            <span className="text-xs text-neutral-500">{formatTimestamp(task.last_heartbeat)}</span>
          )}
        </span>
      </DetailRow>
      {task.attempt_count != null && (
        <DetailRow label="Attempts"><span>{task.attempt_count}</span></DetailRow>
      )}

      {task.description && (
        <>
          <SectionHeader>Description</SectionHeader>
          <pre className="text-sm text-neutral-200 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-neutral-800">
            {task.description}
          </pre>
        </>
      )}

      {task.plan_output && (
        <>
          <SectionHeader>Plan Output</SectionHeader>
          <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-purple-950/20 rounded-lg p-3 max-h-64 overflow-y-auto custom-scrollbar border border-purple-900/30 font-mono">
            {JSON.stringify(task.plan_output, null, 2)}
          </pre>
        </>
      )}

      {task.research_findings && (
        <>
          <SectionHeader>Research Findings</SectionHeader>
          <pre className="text-sm text-neutral-300 whitespace-pre-wrap break-words bg-cyan-950/20 rounded-lg p-3 max-h-64 overflow-y-auto custom-scrollbar border border-cyan-900/30">
            {task.research_findings}
          </pre>
        </>
      )}

      {task.error_logs && (
        <>
          <SectionHeader>Error Logs</SectionHeader>
          <pre className="text-sm text-red-300 whitespace-pre-wrap break-words bg-red-950/20 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-red-900/30 font-mono">
            {task.error_logs}
          </pre>
        </>
      )}

      {task.history && task.history.length > 0 && (
        <>
          <SectionHeader>History</SectionHeader>
          <div className="space-y-2">
            {task.history.map((entry, i) => (
              <div key={i} className="flex items-start gap-2 text-sm">
                <div className="w-1.5 h-1.5 rounded-full bg-neutral-600 mt-1.5 shrink-0" />
                <div className="min-w-0">
                  <span className="text-neutral-500 text-xs">{formatTimestamp(entry.timestamp)}</span>
                  <span className="text-neutral-300 ml-2 font-medium">{entry.action}</span>
                  {entry.details && Object.keys(entry.details).length > 0 && (
                    <div className="text-xs text-neutral-500 mt-0.5 font-mono">
                      {JSON.stringify(entry.details)}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      {task.pr_url && isSafeUrl(task.pr_url) && (
        <>
          <SectionHeader>Pull Request</SectionHeader>
          <a
            href={task.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
          >
            {task.pr_url}
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </>
      )}

      {task.depends_on && task.depends_on.length > 0 && (
        <>
          <SectionHeader>Dependencies</SectionHeader>
          <div className="space-y-1">
            {task.depends_on.map((dep) => (
              <div key={dep} className="font-mono text-sm text-neutral-300 flex items-center gap-1.5">
                <ChevronRight className="w-3 h-3 text-neutral-600" />
                {dep}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

function LogsTab({ logLines, task }: { logLines: string[]; task: Task }) {
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logLines]);

  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';

  if (logLines.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-500 text-sm">
        {isActive ? 'Waiting for logs...' : 'No logs available'}
      </div>
    );
  }

  return (
    <div className="flex-1 overflow-y-auto p-4 custom-scrollbar font-mono text-xs leading-relaxed">
      {logLines.map((line, i) => (
        <div key={i} className="whitespace-pre-wrap break-all mb-0.5">
          {line.startsWith('[error]') || line.startsWith('[stderr]')
            ? <span className="text-red-400">{line}</span>
            : line.startsWith('[Status:')
              ? <span className="text-blue-400">{line}</span>
              : line.startsWith('[')
                ? <span className="text-neutral-500">{line}</span>
                : line}
        </div>
      ))}
      <div ref={logEndRef} />
    </div>
  );
}

export function TaskDetailDrawer({
  task,
  logLines,
  actionLoading,
  onClose,
  onApprove,
  onCancel,
  onRetry,
  onEditPrompt,
  onDismiss,
}: TaskDetailDrawerProps) {
  const [activeTab, setActiveTab] = useState<Tab>('details');
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [editPromptValue, setEditPromptValue] = useState(task.description);

  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';
  const showApproval = task.requires_approval && task.status === 'QUEUED';
  const showCancel = isActive && !task.requires_approval;
  const showFailedActions = task.status === 'FAILED';

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-[680px] bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl">
        {/* Header */}
        <div className="p-4 border-b border-neutral-800 shrink-0">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 mb-2">
                <Terminal className="w-4 h-4 text-neutral-400 shrink-0" />
                <h2 className="text-base font-semibold text-neutral-100 truncate">{task.title}</h2>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <StatusBadge status={task.status} />
                <TaskTypeBadge taskType={task.task_type} />
              </div>
            </div>
            <button
              onClick={onClose}
              className="p-1.5 rounded-lg hover:bg-neutral-800 text-neutral-400 hover:text-neutral-200 transition-colors shrink-0"
            >
              <X className="w-5 h-5" />
            </button>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex border-b border-neutral-800 shrink-0">
          <button
            onClick={() => setActiveTab('details')}
            className={`px-4 py-2.5 text-sm font-medium transition-colors ${
              activeTab === 'details'
                ? 'text-neutral-100 border-b-2 border-blue-500'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            Details
          </button>
          <button
            onClick={() => setActiveTab('logs')}
            className={`px-4 py-2.5 text-sm font-medium transition-colors ${
              activeTab === 'logs'
                ? 'text-neutral-100 border-b-2 border-blue-500'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            Logs
            {logLines.length > 0 && (
              <span className="ml-1.5 text-xs text-neutral-500">({logLines.length})</span>
            )}
          </button>
        </div>

        {/* Content */}
        {activeTab === 'details' ? (
          <DetailsTab task={task} />
        ) : (
          <LogsTab logLines={logLines} task={task} />
        )}

        {/* Action Footer */}
        {(showApproval || showCancel || showFailedActions) && (
          <div className="border-t border-neutral-800 p-4 shrink-0 flex items-center gap-2">
            {showApproval && (
              <>
                <button
                  onClick={() => onApprove(task.id)}
                  disabled={!!actionLoading}
                  className="px-4 py-2 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium transition-colors disabled:opacity-50"
                >
                  Approve
                </button>
                <button
                  onClick={() => onCancel(task.id)}
                  disabled={!!actionLoading}
                  className="px-4 py-2 rounded-lg bg-red-600 hover:bg-red-500 text-white text-sm font-medium transition-colors disabled:opacity-50"
                >
                  Reject
                </button>
              </>
            )}

            {showCancel && !confirmCancel && (
              <button
                onClick={() => setConfirmCancel(true)}
                className="px-4 py-2 rounded-lg bg-red-600/20 hover:bg-red-600/30 text-red-400 text-sm font-medium transition-colors border border-red-600/30"
              >
                Cancel Task
              </button>
            )}
            {showCancel && confirmCancel && (
              <div className="flex items-center gap-2">
                <span className="text-sm text-neutral-400">Cancel this task?</span>
                <button
                  onClick={() => { onCancel(task.id); setConfirmCancel(false); }}
                  disabled={!!actionLoading}
                  className="px-3 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white text-sm font-medium transition-colors disabled:opacity-50"
                >
                  Confirm
                </button>
                <button
                  onClick={() => setConfirmCancel(false)}
                  className="px-3 py-1.5 rounded-lg bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-sm font-medium transition-colors"
                >
                  No
                </button>
              </div>
            )}

            {showFailedActions && (
              <>
                <button
                  onClick={() => onRetry(task.id)}
                  disabled={!!actionLoading}
                  className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors disabled:opacity-50"
                >
                  Retry
                </button>
                {!editingPrompt ? (
                  <button
                    onClick={() => setEditingPrompt(true)}
                    className="px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 text-neutral-200 text-sm font-medium transition-colors border border-neutral-700"
                  >
                    Edit
                  </button>
                ) : (
                  <div className="flex-1 flex items-center gap-2">
                    <input
                      type="text"
                      value={editPromptValue}
                      onChange={(e) => setEditPromptValue(e.target.value)}
                      className="flex-1 px-3 py-1.5 rounded-lg bg-neutral-900 border border-neutral-700 text-sm text-neutral-200 focus:outline-none focus:border-blue-500"
                    />
                    <button
                      onClick={() => { onEditPrompt(task.id, editPromptValue); setEditingPrompt(false); }}
                      disabled={!!actionLoading}
                      className="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors disabled:opacity-50"
                    >
                      Save
                    </button>
                    <button
                      onClick={() => { setEditingPrompt(false); setEditPromptValue(task.description); }}
                      className="px-3 py-1.5 rounded-lg bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-sm transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                )}
                <button
                  onClick={() => onDismiss(task.id)}
                  disabled={!!actionLoading}
                  className="px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 text-neutral-400 text-sm font-medium transition-colors"
                >
                  Dismiss
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
