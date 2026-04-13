import { useState, useRef, useEffect, type ReactNode } from 'react';
import { Terminal, X, Copy, Check, CheckCircle, ExternalLink, ChevronRight, ChevronDown, Search, Hash, Maximize2, Minimize2, Loader2, WifiOff, Download } from 'lucide-react';
import { StatusBadge, TaskTypeBadge, getHeartbeatStatus, isSafeUrl, useElapsedTime, linkifyStatusMessage, ResearchFindings } from './TaskCard';
import type { Task } from './TaskCard';
import { formatAbsolute, parseTimestamp, timeAgo } from '../utils/time';

interface TaskDetailDrawerProps {
  task: Task;
  logLines: string[];
  logsConnecting?: boolean;
  wsDisconnected?: boolean;
  wsDisconnectReason?: string | null;
  actionLoading?: string;
  gcpProjectId?: string;
  onClose: () => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onNavigateToTask?: (taskId: string) => void;
  onWidthChange?: (width: number) => void;
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

function SectionHeader({ children }: { children: ReactNode }) {
  return <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-400 mb-2 mt-5 first:mt-0">{children}</h3>;
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex items-start gap-3 py-1.5">
      <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">{label}</span>
      <div className="text-sm text-neutral-200 min-w-0">{children}</div>
    </div>
  );
}

function formatCreatedBy(value: string): string {
  if (value === 'USER') return 'User';
  if (value === 'ORCHESTRATOR') return 'Orchestrator';
  if (value.startsWith('AGENT_')) return `Agent (${value.slice(6)})`;
  return value;
}

function getLogEntryColor(msg: string): string {
  const lower = msg.toLowerCase();
  if (lower.includes('failed') || lower.includes('error') || lower.includes('cancelled')) return 'text-red-400';
  if (lower.includes('blocked') || lower.includes('conflict')) return 'text-amber-400';
  if (lower.includes('merged') || lower.includes('completed') || lower.includes('passed')) return 'text-emerald-400';
  if (msg.startsWith('Status changed:')) return 'text-blue-400';
  return 'text-neutral-300';
}


function DirectionsSection({ directions }: { directions: { text: string; priority?: string; status?: string }[] }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="mt-5">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wider text-neutral-400 hover:text-neutral-200 transition-colors w-full text-left mb-2"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Directions ({directions.length})
      </button>
      {open && (
        <ul className="space-y-1.5 pl-1">
          {directions.map((d, i) => (
            <li key={i} className="flex items-start gap-2 text-sm text-neutral-200">
              <span className="mt-1.5 w-1.5 h-1.5 rounded-full bg-neutral-500 shrink-0" />
              <span className="break-words min-w-0">{d.text}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function DetailsTab({ task, gcpProjectId, onNavigateToTask, onGoToLogs }: { task: Task; gcpProjectId?: string; onNavigateToTask?: (taskId: string) => void; onGoToLogs?: () => void }) {
  const heartbeat = getHeartbeatStatus(task.last_heartbeat);
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const duration = useElapsedTime(task.started_at ?? '', isTerminal ? task.last_heartbeat : undefined);
  const [rawContextExpanded, setRawContextExpanded] = useState(false);
  const prArtifacts = (task.artifacts ?? []).filter(a => a.artifact_type === 'PULL_REQUEST' && a.pr_url && isSafeUrl(a.pr_url));
  const commitArtifacts = (task.artifacts ?? []).filter(a => a.artifact_type === 'GIT_COMMIT');
  const investigationArtifacts = (task.artifacts ?? []).filter(a => a.artifact_type === 'INVESTIGATION_REPORT');
  // Group PR and commit artifacts by iteration for display
  const artifactIterations: { iteration: number; prs: typeof prArtifacts; commits: typeof commitArtifacts }[] = [];
  if (task.artifacts && task.artifacts.length > 0) {
    const iterSet = new Set<number>([
      ...prArtifacts.map(a => a.iteration ?? 1),
      ...commitArtifacts.map(a => a.iteration ?? 1),
    ]);
    Array.from(iterSet).sort((a, b) => a - b).forEach(iter => {
      artifactIterations.push({
        iteration: iter,
        prs: prArtifacts.filter(a => (a.iteration ?? 1) === iter),
        commits: commitArtifacts.filter(a => (a.iteration ?? 1) === iter),
      });
    });
  }
  const multiIteration = artifactIterations.length > 1;

  return (
    <div className="flex-1 overflow-y-auto p-4 custom-scrollbar space-y-1">
      <SectionHeader>Identifiers</SectionHeader>
      <DetailRow label="Task ID"><CopyableId value={task.id} /></DetailRow>
      {task.created_by && (
        <DetailRow label="Created By"><span className="text-sm">{formatCreatedBy(task.created_by)}</span></DetailRow>
      )}
      {task.parent_task_id && (
        <DetailRow label="Parent Task">
          {onNavigateToTask ? (
            <button
              onClick={() => onNavigateToTask(task.parent_task_id!)}
              className="font-mono text-sm text-blue-400 hover:text-blue-300 hover:underline cursor-pointer"
            >
              {task.parent_task_id}
            </button>
          ) : (
            <CopyableId value={task.parent_task_id} />
          )}
        </DetailRow>
      )}
      {task.project_id && <DetailRow label="Project ID"><span className="font-mono text-sm">{task.project_id}</span></DetailRow>}
      {task.vm_instance_id && (
        <DetailRow label="VM Instance">
          <span className="inline-flex items-center gap-1.5">
            <span className="font-mono text-sm">{task.vm_instance_id}</span>
            <a
              href={gcpProjectId
                ? `https://console.cloud.google.com/compute/instances?project=${gcpProjectId}`
                : `https://console.cloud.google.com/compute/instances`}
              target="_blank"
              rel="noopener noreferrer"
              className="text-gray-400 hover:text-gray-200 transition-colors"
              title="Open in GCP Console"
            >
              <ExternalLink className="w-3.5 h-3.5" />
            </a>
          </span>
        </DetailRow>
      )}

      <SectionHeader>Timing</SectionHeader>
      <DetailRow label="Created at">{formatAbsolute(task.created_at)}</DetailRow>
      {task.started_at && (
        <DetailRow label="Started at">{formatAbsolute(task.started_at)}</DetailRow>
      )}
      {task.started_at && (
        <DetailRow label="Duration"><span className="font-mono">{duration}</span></DetailRow>
      )}
      <DetailRow label="Heartbeat">
        <span className="inline-flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${heartbeat.color}`} />
          <span className="text-sm">{heartbeat.text}</span>
          {task.last_heartbeat && (
            <span className="text-xs text-neutral-500">{formatAbsolute(task.last_heartbeat)}</span>
          )}
        </span>
      </DetailRow>
      {task.attempt_count != null && (
        <DetailRow label="Attempts"><span>{task.attempt_count}</span></DetailRow>
      )}

      {(() => {
        const metrics = task.context?.last_metrics as { rss_mb?: number; heap_used_mb?: number; disk_free_mb?: number; disk_total_mb?: number } | undefined;
        if (!metrics) return null;
        return (
          <>
            <SectionHeader>System Metrics</SectionHeader>
            {metrics.rss_mb != null && (
              <DetailRow label="RSS Memory"><span className="font-mono text-sm">{metrics.rss_mb} MB</span></DetailRow>
            )}
            {metrics.heap_used_mb != null && (
              <DetailRow label="Heap Used"><span className="font-mono text-sm">{metrics.heap_used_mb} MB</span></DetailRow>
            )}
            {metrics.disk_free_mb != null && metrics.disk_total_mb != null && (
              <DetailRow label="Disk (/tmp)">
                <span className="font-mono text-sm">{metrics.disk_free_mb} MB free / {metrics.disk_total_mb} MB total</span>
              </DetailRow>
            )}
          </>
        );
      })()}

      {task.description && (
        <>
          <SectionHeader>Description</SectionHeader>
          <pre className="text-sm text-neutral-200 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-neutral-800">
            {task.description}
          </pre>
        </>
      )}

      {task.payloads && task.payloads.length > 0 && (() => {
        const directions = task.payloads![0].directions as { text: string; priority?: string; status?: string }[] | undefined;
        if (!directions || directions.length === 0) return null;
        return <DirectionsSection directions={directions} />;
      })()}

      {task.payloads && task.payloads.length > 0 && (() => {
        const payload = task.payloads![0];
        const prompt = payload.prompt as string | undefined;
        const baseBranch = payload.base_branch as string | undefined;
        const prNumber = payload.pr_number as number | undefined;
        const branch = payload.branch as string | undefined;
        const payloadRepoUrl = task.repo_url ?? commitArtifacts[0]?.repo_url;
        const prLink = prNumber && payloadRepoUrl ? `${payloadRepoUrl}/pull/${prNumber}` : undefined;
        return (
          <>
            <SectionHeader>Task Config</SectionHeader>
            {prompt && prompt !== task.description && (
              <DetailRow label="Prompt">
                <pre className="text-sm text-neutral-200 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-32 overflow-y-auto custom-scrollbar border border-neutral-800">
                  {prompt}
                </pre>
              </DetailRow>
            )}
            {baseBranch && <DetailRow label="Base Branch"><span className="font-mono text-sm">{baseBranch}</span></DetailRow>}
            {prNumber && (
              <DetailRow label="PR Number">
                {prLink && isSafeUrl(prLink) ? (
                  <a href={prLink} target="_blank" rel="noopener noreferrer" className="inline-flex items-center gap-1 font-mono text-sm text-blue-400 hover:text-blue-300 transition-colors">
                    #{prNumber}<ExternalLink className="w-3 h-3" />
                  </a>
                ) : (
                  <span className="font-mono text-sm">#{prNumber}</span>
                )}
              </DetailRow>
            )}
            {branch && <DetailRow label="Branch"><span className="font-mono text-sm">{branch}</span></DetailRow>}
          </>
        );
      })()}

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
          <SectionHeader>Research Findings ({task.research_findings.length})</SectionHeader>
          <div className="bg-cyan-950/20 rounded-lg p-3 border border-cyan-900/30">
            <ResearchFindings findings={task.research_findings} />
          </div>
        </>
      )}

      {(task.task_type === 'investigate' && task.plan_output) || investigationArtifacts.length > 0 ? (() => {
        const report = task.plan_output as {
          summary?: string;
          root_cause?: string;
          affected_files?: { path: string; issue: string }[];
          recommended_fix?: string;
          severity?: 'critical' | 'high' | 'medium' | 'low';
          confidence?: 'high' | 'medium' | 'low';
        } | null;
        if (!report) return null;

        const severityColors: Record<string, string> = {
          critical: 'text-red-400 bg-red-950/30 border-red-800/40',
          high: 'text-orange-400 bg-orange-950/30 border-orange-800/40',
          medium: 'text-yellow-400 bg-yellow-950/30 border-yellow-800/40',
          low: 'text-green-400 bg-green-950/30 border-green-800/40',
        };

        return (
          <>
            <SectionHeader>Investigation Report</SectionHeader>
            <div className="space-y-3 bg-amber-950/10 rounded-lg p-3 border border-amber-900/30">
              {report.severity && (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0">Severity</span>
                  <span className={`text-xs font-semibold uppercase tracking-wider px-2 py-0.5 rounded border ${severityColors[report.severity] ?? 'text-neutral-400'}`}>
                    {report.severity}
                  </span>
                </div>
              )}
              {report.confidence && (
                <div className="flex items-center gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0">Confidence</span>
                  <span className="text-sm text-neutral-200 capitalize">{report.confidence}</span>
                </div>
              )}
              {report.summary && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Summary</span>
                  <p className="text-sm text-neutral-200 min-w-0 break-words">{report.summary}</p>
                </div>
              )}
              {report.root_cause && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Root Cause</span>
                  <p className="text-sm text-neutral-200 min-w-0 break-words">{report.root_cause}</p>
                </div>
              )}
              {report.recommended_fix && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Recommended Fix</span>
                  <p className="text-sm text-neutral-200 min-w-0 break-words">{report.recommended_fix}</p>
                </div>
              )}
              {report.affected_files && report.affected_files.length > 0 && (
                <div className="flex items-start gap-2">
                  <span className="text-xs text-neutral-500 w-28 shrink-0 pt-0.5">Affected Files</span>
                  <div className="min-w-0 space-y-1.5">
                    {report.affected_files.map((f, i) => (
                      <div key={i} className="text-sm">
                        <span className="font-mono text-blue-300 break-all">{f.path}</span>
                        {f.issue && <p className="text-neutral-400 text-xs mt-0.5 break-words">{f.issue}</p>}
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          </>
        );
      })() : null}

      {task.context && Object.keys(task.context).length > 0 && (() => {
        const ctx = task.context as Record<string, unknown>;
        const diagFields = ['retry_after', 'provision_zone', 'provision_failures', 'last_serial_log', 'blacklisted_accounts', 'max_plan_account_id', '_first_attempt_succeeded'];
        const hasDiag = diagFields.some(f => f in ctx);

        function formatRetryAfter(val: unknown): string {
          if (typeof val !== 'string') return String(val);
          const d = parseTimestamp(val);
          const ms = d.getTime() - Date.now();
          if (ms > 0) {
            const sec = Math.round(ms / 1000);
            if (sec < 60) return `in ${sec}s`;
            const min = Math.round(sec / 60);
            return `in ${min}m`;
          }
          return timeAgo(val);
        }

        return (
          <>
            <SectionHeader>Context</SectionHeader>
            {hasDiag && (
              <div className="bg-neutral-900/50 rounded-lg border border-neutral-800 p-3 space-y-2 mb-2">
                <p className="text-xs font-semibold text-neutral-400 uppercase tracking-wide mb-1">Diagnostics</p>
                {'retry_after' in ctx && (
                  <div className="flex gap-2 text-xs">
                    <span className="text-neutral-500 w-36 shrink-0">Retry after</span>
                    <span className="text-neutral-200">{formatRetryAfter(ctx.retry_after)}</span>
                  </div>
                )}
                {'provision_zone' in ctx && (
                  <div className="flex gap-2 text-xs">
                    <span className="text-neutral-500 w-36 shrink-0">Provision zone</span>
                    <span className="text-neutral-200 font-mono">{String(ctx.provision_zone)}</span>
                  </div>
                )}
                {'provision_failures' in ctx && (
                  <div className="flex gap-2 text-xs">
                    <span className="text-neutral-500 w-36 shrink-0">Provision attempts</span>
                    <span className="text-neutral-200">{String(ctx.provision_failures)}</span>
                  </div>
                )}
                {'max_plan_account_id' in ctx && (
                  <div className="flex gap-2 text-xs">
                    <span className="text-neutral-500 w-36 shrink-0">Account</span>
                    <span className="text-neutral-200 font-mono break-all">{String(ctx.max_plan_account_id)}</span>
                  </div>
                )}
                {'blacklisted_accounts' in ctx && Array.isArray(ctx.blacklisted_accounts) && ctx.blacklisted_accounts.length > 0 && (
                  <div className="flex gap-2 text-xs">
                    <span className="text-neutral-500 w-36 shrink-0">Blacklisted accts</span>
                    <span className="text-neutral-200 font-mono break-all">{(ctx.blacklisted_accounts as unknown[]).join(', ')}</span>
                  </div>
                )}
                {'_first_attempt_succeeded' in ctx && (
                  <div className="flex gap-2 text-xs">
                    <span className="text-neutral-500 w-36 shrink-0">First attempt</span>
                    <span className={ctx._first_attempt_succeeded ? 'text-green-400' : 'text-red-400'}>{ctx._first_attempt_succeeded ? 'succeeded' : 'failed'}</span>
                  </div>
                )}
                {'last_serial_log' in ctx && typeof ctx.last_serial_log === 'string' && ctx.last_serial_log.trim() && (
                  <div className="text-xs">
                    <p className="text-neutral-500 mb-1">Serial log</p>
                    <pre className="text-neutral-300 whitespace-pre-wrap break-words bg-neutral-950/60 rounded p-2 max-h-32 overflow-y-auto custom-scrollbar font-mono text-xs border border-neutral-800">{ctx.last_serial_log}</pre>
                  </div>
                )}
              </div>
            )}
            <div>
              <button
                className="flex items-center gap-1 text-xs text-neutral-500 hover:text-neutral-300 mb-1"
                onClick={() => setRawContextExpanded(v => !v)}
              >
                {rawContextExpanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
                Raw Context
              </button>
              {rawContextExpanded && (
                <pre className="text-xs text-neutral-300 whitespace-pre-wrap break-words bg-neutral-900/50 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-neutral-800 font-mono">
                  {JSON.stringify(task.context, null, 2)}
                </pre>
              )}
            </div>
          </>
        );
      })()}

      {task.error_logs && (
        <>
          <SectionHeader>Error Logs</SectionHeader>
          <pre className="text-sm text-red-300 whitespace-pre-wrap break-words bg-red-950/20 rounded-lg p-3 max-h-48 overflow-y-auto custom-scrollbar border border-red-900/30 font-mono">
            {task.error_logs}
          </pre>
          {task.error_logs_truncated && (
            <p className="text-xs text-neutral-500 mt-1 italic">Output truncated to 2000 characters.{' '}
              {onGoToLogs ? (
                <button onClick={onGoToLogs} className="text-blue-400 underline text-xs">Go to Logs tab</button>
              ) : (
                'View full logs in the Logs tab.'
              )}
            </p>
          )}
        </>
      )}

      {task.status_log && task.status_log.length > 0 && (
        <>
          <SectionHeader>Status Log ({task.status_log.length})</SectionHeader>
          <div className="space-y-1 max-h-48 overflow-y-auto custom-scrollbar bg-neutral-900/50 rounded-lg p-3 border border-neutral-800">
            {task.status_log.slice().reverse().map((entry, i) => {
              const isStatusTransition = entry.msg.startsWith('Status changed:');
              return (
                <div key={i} className={`flex items-start gap-2 text-xs${isStatusTransition ? ' pl-2 border-l-2 border-blue-600/50' : ''}`}>
                  <span className="text-neutral-600 shrink-0 font-mono">{formatAbsolute(entry.t)}</span>
                  <span className={`${getLogEntryColor(entry.msg)} break-words min-w-0`}>{linkifyStatusMessage(entry.msg, task.pr_url)}</span>
                </div>
              );
            })}
          </div>
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
                  <span className="text-neutral-500 text-xs">{formatAbsolute(entry.timestamp)}</span>
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

      {task.artifacts && task.artifacts.length > 0 ? (
        <>
          {(prArtifacts.length > 0 || commitArtifacts.length > 0) && (
            <>
              <SectionHeader>Code Output</SectionHeader>
              <div className="space-y-3">
                {artifactIterations.map(({ iteration, prs, commits }) => (
                  <div key={iteration}>
                    {multiIteration && (
                      <div className="flex items-center gap-2 mb-1.5">
                        <span className="text-xs text-neutral-500 font-mono">Iteration {iteration}</span>
                        <div className="flex-1 border-t border-neutral-700" />
                      </div>
                    )}
                    <div className="space-y-1">
                      {prs.map((a, i) => (
                        <a
                          key={i}
                          href={a.pr_url!}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
                        >
                          {a.pr_url}
                          <ExternalLink className="w-3.5 h-3.5" />
                        </a>
                      ))}
                      {commits.map((a, i) => (
                        <div key={i} className="flex items-center gap-2 text-sm font-mono text-neutral-300">
                          {a.commit_hash && <span className="text-emerald-400">{a.commit_hash.slice(0, 8)}</span>}
                          {a.repo_url && isSafeUrl(a.repo_url) ? (
                            <a href={a.repo_url} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:text-blue-300 transition-colors flex items-center gap-1">
                              {a.repo_url}<ExternalLink className="w-3 h-3" />
                            </a>
                          ) : a.repo_url ? (
                            <span className="text-neutral-400">{a.repo_url}</span>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </>
      ) : (task.pr_url && isSafeUrl(task.pr_url) || task.commit_hash || task.repo_url && isSafeUrl(task.repo_url)) ? (
        <>
          <SectionHeader>Code Output</SectionHeader>
          <div className="space-y-2">
            {task.pr_url && isSafeUrl(task.pr_url) && (
              <a
                href={task.pr_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
              >
                {task.pr_url}
                <ExternalLink className="w-3.5 h-3.5" />
              </a>
            )}
            {task.commit_hash && (
              <div className="flex items-center gap-1.5">
                <Hash className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
                <CopyableId value={task.commit_hash} />
              </div>
            )}
            {task.repo_url && isSafeUrl(task.repo_url) && !task.pr_url && (
              <a
                href={`${task.repo_url}/pulls`}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
              >
                View Pull Requests
                <ExternalLink className="w-3.5 h-3.5" />
              </a>
            )}
          </div>
        </>
      ) : (task.task_type === 'research' || task.task_type === 'plan') && task.repo_url && isSafeUrl(task.repo_url) ? (
        <>
          <SectionHeader>Repository</SectionHeader>
          <a
            href={`${task.repo_url}/pulls`}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-sm text-blue-400 hover:text-blue-300 transition-colors"
          >
            View Pull Requests
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </>
      ) : null}

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

      {task.child_task_ids && task.child_task_ids.length > 0 && (
        <>
          <SectionHeader>Child Tasks</SectionHeader>
          <div className="space-y-1">
            {task.child_task_ids.map((childId) => (
              <div key={childId} className="font-mono text-sm text-neutral-300 flex items-center gap-1.5">
                <ChevronRight className="w-3 h-3 text-neutral-600" />
                {onNavigateToTask ? (
                  <button
                    onClick={() => onNavigateToTask(childId)}
                    className="text-blue-400 hover:text-blue-300 transition-colors text-left"
                  >
                    {childId}
                  </button>
                ) : (
                  <CopyableId value={childId} />
                )}
              </div>
            ))}
          </div>
        </>
      )}
    </div>
  );
}

type LogLevel = 'all' | 'error' | 'warn' | 'status';

function matchesLevel(line: string, level: LogLevel): boolean {
  if (level === 'all') return true;
  if (level === 'error') {
    return (
      line.includes('[error]') || line.includes('[stderr]') ||
      line.includes('Error:') || line.includes('ERROR') ||
      line.includes('Exception') || line.includes('Traceback')
    );
  }
  if (level === 'warn') {
    return line.includes('[warn]') || line.includes('WARNING') || line.includes('[warning]');
  }
  if (level === 'status') {
    return line.startsWith('[Status:') || line.startsWith('Status changed:');
  }
  return true;
}

function highlightMatch(text: string, filter: string): ReactNode {
  if (!filter) return text;
  const filterLower = filter.toLowerCase();
  const textLower = text.toLowerCase();
  const parts: ReactNode[] = [];
  let lastIdx = 0;
  let idx = textLower.indexOf(filterLower);
  while (idx !== -1) {
    if (idx > lastIdx) parts.push(text.slice(lastIdx, idx));
    parts.push(
      <mark key={idx} className="bg-amber-500/30 text-amber-200 rounded">
        {text.slice(idx, idx + filter.length)}
      </mark>
    );
    lastIdx = idx + filter.length;
    idx = textLower.indexOf(filterLower, lastIdx);
  }
  if (lastIdx < text.length) parts.push(text.slice(lastIdx));
  return parts.length === 1 ? parts[0] : <>{parts}</>;
}

function LogsTab({
  logLines,
  task,
  logFilter,
  setLogFilter,
  logLevel,
  setLogLevel,
  showLineNumbers,
  logsConnecting,
}: {
  logLines: string[];
  task: Task;
  logFilter: string;
  setLogFilter: (v: string) => void;
  logLevel: LogLevel;
  setLogLevel: (v: LogLevel) => void;
  showLineNumbers: boolean;
  logsConnecting: boolean;
}) {
  const logEndRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (logFilter || logLevel !== 'all') return;
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logLines, logFilter, logLevel]);

  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';

  const errorCount = logLines.filter(l => matchesLevel(l, 'error')).length;
  const warnCount = logLines.filter(l => matchesLevel(l, 'warn')).length;
  const statusCount = logLines.filter(l => matchesLevel(l, 'status')).length;

  const indexedLines = logLines.map((line, i) => ({ line, idx: i }));
  const filteredLines = indexedLines.filter(({ line }) => {
    if (logLevel !== 'all' && !matchesLevel(line, logLevel)) return false;
    if (logFilter && !line.toLowerCase().includes(logFilter.toLowerCase())) return false;
    return true;
  });

  if (logLines.length === 0) {
    if (logsConnecting) {
      return (
        <div className="flex-1 flex items-center justify-center gap-2 text-neutral-500 text-sm">
          <Loader2 className="w-4 h-4 animate-spin" />
          Connecting to log stream...
        </div>
      );
    }
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-500 text-sm">
        {isActive ? 'Waiting for logs...' : 'No logs available'}
      </div>
    );
  }

  const levelButtons: { level: LogLevel; label: string; count?: number; activeClass: string }[] = [
    { level: 'all', label: 'ALL', activeClass: 'bg-neutral-700 text-neutral-100' },
    { level: 'error', label: 'ERROR', count: errorCount, activeClass: 'bg-red-500/20 text-red-400 border-red-500/30' },
    { level: 'warn', label: 'WARN', count: warnCount, activeClass: 'bg-amber-500/20 text-amber-400 border-amber-500/30' },
    { level: 'status', label: 'STATUS', count: statusCount, activeClass: 'bg-blue-500/20 text-blue-400 border-blue-500/30' },
  ];

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="px-3 py-2 border-b border-neutral-800 flex flex-col gap-1.5 shrink-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          {levelButtons.map(({ level, label, count, activeClass }) => (
            <button
              key={level}
              onClick={() => setLogLevel(level)}
              className={`px-2 py-0.5 rounded-full text-xs font-medium border transition-colors ${
                logLevel === level
                  ? `${activeClass} border-current`
                  : 'bg-transparent text-neutral-500 border-neutral-700 hover:text-neutral-300 hover:border-neutral-600'
              }`}
            >
              {label}{count !== undefined ? ` (${count})` : ''}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <Search className="w-3.5 h-3.5 text-neutral-500 flex-shrink-0" />
          <input
            type="text"
            placeholder="Filter logs..."
            className="flex-1 bg-transparent text-xs text-neutral-300 outline-none placeholder-neutral-600"
            value={logFilter}
            onChange={e => setLogFilter(e.target.value)}
          />
          {(logFilter || logLevel !== 'all') && (
            <span className="text-xs text-neutral-500 shrink-0">
              {filteredLines.length} / {logLines.length} lines
            </span>
          )}
          {logFilter && (
            <button onClick={() => setLogFilter('')} aria-label="Clear filter">
              <X className="w-3 h-3 text-neutral-500" />
            </button>
          )}
        </div>
      </div>
      <div className="flex-1 overflow-y-auto p-4 custom-scrollbar font-mono text-xs leading-relaxed">
        {filteredLines.map(({ line, idx }) => {
          const lineNum = idx + 1;
          const content = highlightMatch(line, logFilter);
          const colored =
            line.startsWith('[error]') || line.startsWith('[stderr]')
              ? <span className="text-red-400">{content}</span>
              : line.startsWith('[Status:')
                ? <span className="text-blue-400">{content}</span>
                : line.startsWith('[')
                  ? <span className="text-neutral-500">{content}</span>
                  : <>{content}</>;
          return (
            <div key={idx} className="whitespace-pre-wrap break-all mb-0.5">
              {showLineNumbers && (
                <span className="text-neutral-600 select-none min-w-8 inline-block text-right mr-3">
                  {lineNum}
                </span>
              )}
              {colored}
            </div>
          );
        })}
        <div ref={logEndRef} />
      </div>
    </div>
  );
}

export function TaskDetailDrawer({
  task,
  logLines,
  logsConnecting = false,
  wsDisconnected = false,
  wsDisconnectReason = null,
  actionLoading,
  gcpProjectId,
  onClose,
  onApprove,
  onRequestChanges,
  onCancel,
  onRetry,
  onEditPrompt,
  onDismiss,
  onNavigateToTask,
  onWidthChange,
}: TaskDetailDrawerProps) {
  const [activeTab, setActiveTab] = useState<Tab>('details');
  const [logFilter, setLogFilter] = useState('');
  const [logLevel, setLogLevel] = useState<LogLevel>('all');
  const [showLineNumbers, setShowLineNumbers] = useState(false);
  const [copiedLogs, setCopiedLogs] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [editPromptValue, setEditPromptValue] = useState(task.description);
  const [drawerWidth, setDrawerWidth] = useState(680);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const dragCleanupRef = useRef<(() => void) | null>(null);

  // Report drawer width to parent so sibling panels can offset
  useEffect(() => {
    onWidthChange?.(isFullscreen ? window.innerWidth : drawerWidth);
  }, [drawerWidth, isFullscreen, onWidthChange]);

  // Reset log filter when task changes
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setLogFilter('');
    setLogLevel('all');
  }, [task.id]);

  // Clean up drag listeners on unmount
  useEffect(() => {
    return () => { dragCleanupRef.current?.(); };
  }, []);

  // Keyboard shortcut: f to toggle fullscreen
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const target = e.target as HTMLElement;
        if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') return;
        setIsFullscreen(prev => !prev);
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  const handleDragMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = drawerWidth;

    const onMouseMove = (moveE: MouseEvent) => {
      const delta = startX - moveE.clientX;
      setDrawerWidth(Math.min(1400, Math.max(400, startWidth + delta)));
    };

    const cleanup = () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', cleanup);
      dragCleanupRef.current = null;
    };

    dragCleanupRef.current = cleanup;
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', cleanup);
  };

  const filteredLogLines = logFilter
    ? logLines.filter(line => line.toLowerCase().includes(logFilter.toLowerCase()))
    : logLines;

  const handleCopyLogs = () => {
    navigator.clipboard.writeText(filteredLogLines.join('\n'));
    setCopiedLogs(true);
    setTimeout(() => setCopiedLogs(false), 2000);
  };

  const handleDownloadLogs = () => {
    const blob = new Blob([logLines.join('\n')], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `task-${task.id}-logs.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';
  const showApproval = task.requires_approval && !['FAILED', 'CANCELLED'].includes(task.status);
  const showCancel = isActive && !task.requires_approval;
  const showFailedActions = task.status === 'FAILED';
  const [showChangesForm, setShowChangesForm] = useState(false);
  const [changesFeedback, setChangesFeedback] = useState('');

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div
        className="relative bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl"
        style={isFullscreen ? { width: '100%' } : { width: drawerWidth }}
      >
        {/* Drag handle */}
        {!isFullscreen && (
          <div
            className="absolute left-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-blue-500/40 transition-colors z-10"
            onMouseDown={handleDragMouseDown}
          />
        )}

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
            <div className="flex items-center gap-1 shrink-0">
              <button
                onClick={() => setIsFullscreen(prev => !prev)}
                title={isFullscreen ? 'Exit fullscreen (f)' : 'Fullscreen (f)'}
                className="p-1.5 rounded-lg hover:bg-neutral-800 text-neutral-400 hover:text-neutral-200 transition-colors"
              >
                {isFullscreen ? <Minimize2 className="w-5 h-5" /> : <Maximize2 className="w-5 h-5" />}
              </button>
              <button
                onClick={onClose}
                className="p-1.5 rounded-lg hover:bg-neutral-800 text-neutral-400 hover:text-neutral-200 transition-colors"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex items-center border-b border-neutral-800 shrink-0">
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
          {activeTab === 'logs' && (
            <div className="ml-auto mr-2 flex items-center gap-1">
              <button
                onClick={() => setShowLineNumbers(v => !v)}
                title="Toggle line numbers"
                className={`p-1.5 rounded transition-colors ${
                  showLineNumbers
                    ? 'text-blue-400 bg-blue-500/10'
                    : 'text-neutral-500 hover:text-neutral-300'
                }`}
              >
                <Hash className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={handleCopyLogs}
                disabled={logLines.length === 0}
                title="Copy logs"
                className="p-1.5 rounded transition-colors text-neutral-500 hover:text-neutral-300 disabled:text-neutral-700 disabled:cursor-not-allowed"
              >
                {copiedLogs ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              </button>
              <button
                onClick={handleDownloadLogs}
                disabled={logLines.length === 0}
                title="Download logs"
                className="p-1.5 rounded transition-colors text-neutral-500 hover:text-neutral-300 disabled:text-neutral-700 disabled:cursor-not-allowed"
              >
                <Download className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>

        {/* Content */}
        {wsDisconnected && (
          <div className="flex items-center gap-2 px-4 py-2 bg-amber-950/40 border-b border-amber-800/50 text-amber-400 text-xs shrink-0">
            <WifiOff className="w-3.5 h-3.5 shrink-0" />
            Log stream disconnected
          </div>
        )}
        {activeTab === 'details' ? (
          <DetailsTab task={task} gcpProjectId={gcpProjectId} onNavigateToTask={onNavigateToTask} onGoToLogs={() => setActiveTab('logs')} />
        ) : (
          <>
            {wsDisconnected && (
              <div className="flex items-center gap-2 px-4 py-2 bg-amber-950/40 border-b border-amber-800/50 text-amber-400 text-xs shrink-0">
                <WifiOff className="w-3.5 h-3.5 shrink-0" />
                {wsDisconnectReason ?? 'Log stream disconnected'}
              </div>
            )}
            <LogsTab
              logLines={logLines}
              task={task}
              logFilter={logFilter}
              setLogFilter={setLogFilter}
              logLevel={logLevel}
              setLogLevel={setLogLevel}
              showLineNumbers={showLineNumbers}
              logsConnecting={logsConnecting}
            />
          </>
        )}

        {/* Action Footer */}
        {(showApproval || showCancel || showFailedActions) && (
          <div className="border-t border-neutral-800 p-4 shrink-0 flex items-center gap-2">
            {showApproval && (
              <div className="flex flex-col gap-2 w-full">
                <div className="flex gap-2">
                  <button
                    onClick={() => onApprove(task.id)}
                    disabled={!!actionLoading}
                    className="flex-1 px-6 py-3 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-semibold transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
                  >
                    {actionLoading === 'approve' ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />} Approve & Merge
                  </button>
                  <button
                    onClick={() => setShowChangesForm(!showChangesForm)}
                    className="px-4 py-3 rounded-lg bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-sm font-medium transition-colors border border-neutral-700 flex items-center justify-center gap-1.5"
                  >
                    Request Changes
                  </button>
                  <button
                    onClick={() => onCancel(task.id)}
                    disabled={!!actionLoading}
                    className="px-3 py-3 rounded-lg bg-neutral-800 hover:bg-red-900/50 text-neutral-500 hover:text-red-400 text-sm transition-colors disabled:opacity-50 border border-neutral-700"
                  >
                    Reject
                  </button>
                </div>
                {showChangesForm && (
                  <div className="flex flex-col gap-2">
                    <textarea
                      value={changesFeedback}
                      onChange={e => setChangesFeedback(e.target.value)}
                      placeholder="Describe what needs to change..."
                      className="w-full bg-neutral-900 border border-neutral-700 rounded-lg text-sm text-neutral-200 p-3 resize-none focus:outline-none focus:border-neutral-500 placeholder-neutral-600"
                      rows={3}
                    />
                    <div className="flex gap-2">
                      <button
                        onClick={() => { onRequestChanges(task.id, changesFeedback, 'fix'); setShowChangesForm(false); setChangesFeedback(''); }}
                        disabled={!changesFeedback.trim() || !!actionLoading}
                        className="flex-1 bg-amber-500/10 hover:bg-amber-500/20 text-amber-400 border border-amber-500/20 py-2 rounded-lg text-sm font-medium transition-colors disabled:opacity-30 disabled:cursor-not-allowed flex items-center justify-center gap-1.5"
                      >
                        {actionLoading === 'request-changes' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null} Send Fix
                      </button>
                      <button
                        onClick={() => { onRequestChanges(task.id, changesFeedback || 'Re-review requested', 'review'); setShowChangesForm(false); setChangesFeedback(''); }}
                        disabled={!!actionLoading}
                        className="flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-2 rounded-lg text-sm font-medium transition-colors flex items-center justify-center gap-1.5"
                      >
                        Re-review
                      </button>
                    </div>
                  </div>
                )}
              </div>
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
                  className="px-3 py-1.5 rounded-lg bg-red-600 hover:bg-red-500 text-white text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
                >
                  {actionLoading === 'cancel' && <Loader2 className="w-3 h-3 animate-spin" />}Confirm
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
                  className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
                >
                  {actionLoading === 'retry' && <Loader2 className="w-3 h-3 animate-spin" />}Retry
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
                      className="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
                    >
                      {actionLoading === 'edit' && <Loader2 className="w-3 h-3 animate-spin" />}Save
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
                  className="px-4 py-2 rounded-lg bg-neutral-800 hover:bg-neutral-700 text-neutral-400 text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
                >
                  {actionLoading === 'dismiss' && <Loader2 className="w-3 h-3 animate-spin" />}Dismiss
                </button>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
