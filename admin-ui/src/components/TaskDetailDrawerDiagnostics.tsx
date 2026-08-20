import { useState } from 'react';
import { Copy, Check, ChevronDown, ChevronRight } from 'lucide-react';
import type { Task } from './TaskCard';
import { parseTimestamp, timeAgo } from '../utils/time';
import { fetchTaskSerialConsole } from '../api';
import type { SerialConsoleResponse } from '../types/task';

// eslint-disable-next-line react-refresh/only-export-components -- shared status-log color helper, also consumed by TaskDetailDrawerMeta's standalone Status Log
export function getLogEntryColor(msg: string): string {
  const lower = msg.toLowerCase();
  if (lower.includes('failed') || lower.includes('error') || lower.includes('cancelled')) return 'text-red-400';
  if (lower.includes('blocked') || lower.includes('conflict')) return 'text-amber-400';
  if (lower.includes('merged') || lower.includes('completed') || lower.includes('passed')) return 'text-emerald-400';
  if (msg.startsWith('Status changed:')) return 'text-blue-400';
  return 'text-neutral-300';
}

interface ToolTraceEntry {
  tool?: string;
  name?: string;
  ts?: string;
  [key: string]: unknown;
}

function SerialLogBlock({ log, taskId, vmInstanceId }: { log: string | null; taskId: string; vmInstanceId?: string }) {
  const [copied, setCopied] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [liveResult, setLiveResult] = useState<SerialConsoleResponse | null>(null);
  const [fetchError, setFetchError] = useState<string | null>(null);

  const displayLog = liveResult?.serial_log ?? log;
  const byteCount = displayLog ? new TextEncoder().encode(displayLog).length : 0;

  const handleCopy = () => {
    if (!displayLog) return;
    navigator.clipboard.writeText(displayLog);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };

  const handleFetch = async () => {
    setFetching(true);
    setFetchError(null);
    try {
      const result = await fetchTaskSerialConsole(taskId);
      setLiveResult(result);
    } catch (e) {
      setFetchError(e instanceof Error ? e.message : 'Fetch failed');
    } finally {
      setFetching(false);
    }
  };

  return (
    <div className="text-xs">
      <div className="flex items-center gap-2 mb-1.5 flex-wrap">
        <span className="text-neutral-400 font-medium">Serial console</span>
        {displayLog && <span className="text-neutral-600">{byteCount.toLocaleString()} B</span>}
        {liveResult && (
          <span className={`px-1.5 py-0.5 rounded text-ui-sm font-semibold ${liveResult.source === 'live' ? 'bg-emerald-900/40 text-emerald-400' : 'bg-workbench-panel text-neutral-400'}`}>
            {liveResult.source}
          </span>
        )}
        {liveResult?.vm_status && (
          <span className="text-neutral-500">{liveResult.vm_status}</span>
        )}
        {displayLog && (
          <button onClick={handleCopy} className="ml-auto flex items-center gap-1 text-neutral-500 hover:text-neutral-300 transition-colors">
            {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
            {copied ? 'Copied' : 'Copy'}
          </button>
        )}
        {vmInstanceId && (
          <button
            onClick={handleFetch}
            disabled={fetching}
            className="flex items-center gap-1 text-blue-400 hover:text-blue-300 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            {fetching ? (
              <svg className="w-3 h-3 animate-spin" viewBox="0 0 24 24" fill="none">
                <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
                <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z" />
              </svg>
            ) : null}
            {fetching ? 'Fetching…' : 'Fetch current serial'}
          </button>
        )}
      </div>
      {fetchError && <p className="text-red-400 mb-1">{fetchError}</p>}
      {displayLog ? (
        <pre className="text-neutral-300 whitespace-pre-wrap break-words bg-workbench-shell/60 rounded p-2 max-h-48 overflow-y-auto custom-scrollbar font-mono border border-neutral-800">{displayLog}</pre>
      ) : (
        <p className="text-neutral-600 italic">No serial log captured</p>
      )}
    </div>
  );
}

export function TaskErrorLogsPanel({ errorLogs }: { errorLogs: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = () => {
    navigator.clipboard.writeText(errorLogs);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  return (
    <div className="relative group">
      {/* eslint-disable-next-line viewport/min-touch-target -- dense drawer content */}
      <button
        onClick={handleCopy}
        className="absolute top-3 right-3 z-10 p-1.5 rounded opacity-0 group-hover:opacity-100 transition-opacity bg-workbench-base/80 hover:bg-workbench-hover text-neutral-400 hover:text-emerald-400"
        title="Copy full error logs"
      >
        {copied ? <Check className="w-3.5 h-3.5" /> : <Copy className="w-3.5 h-3.5" />}
      </button>
      <pre className="text-sm text-red-300 whitespace-pre-wrap break-words bg-red-950/20 rounded-lg p-3 max-h-96 overflow-y-auto custom-scrollbar border border-red-900/30 font-mono">
        {errorLogs}
      </pre>
    </div>
  );
}

function ToolTraceTail({ entries }: { entries: ToolTraceEntry[] }) {
  const tail = entries.slice(-30);
  const [expandedSet, setExpandedSet] = useState<Set<number>>(new Set());
  const toggle = (i: number) => setExpandedSet(prev => {
    const next = new Set(prev);
    if (next.has(i)) { next.delete(i); } else { next.add(i); }
    return next;
  });
  return (
    <div className="space-y-0.5">
      {tail.map((entry, i) => {
        const label = entry.tool ?? entry.name ?? '(unknown)';
        const extra = Object.entries(entry).filter(([k]) => k !== 'tool' && k !== 'name' && k !== 'ts');
        const extraStr = extra.map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join(' | ');
        const isLong = extraStr.length > 200;
        const isExpanded = expandedSet.has(i);
        return (
          <div key={i} className="font-mono text-ui-md text-neutral-400 leading-relaxed">
            <span className="text-blue-400">{label}</span>
            {entry.ts && <span className="text-neutral-600 ml-1.5">{String(entry.ts).slice(11, 19)}</span>}
            {extraStr && (
              <>
                <span className="text-neutral-600 ml-1.5">
                  {isLong && !isExpanded ? extraStr.slice(0, 200) + '…' : extraStr}
                </span>
                {isLong && (
                  <button onClick={() => toggle(i)} className="ml-1 text-neutral-500 hover:text-neutral-300 underline">{isExpanded ? 'less' : 'more'}</button>
                )}
              </>
            )}
          </div>
        );
      })}
    </div>
  );
}

function formatRetryAfter(val: unknown): string {
  if (typeof val !== 'string') return String(val);
  const d = parseTimestamp(val);
  const ms = d.getTime() - Date.now();
  if (ms > 0) {
    const sec = Math.round(ms / 1000);
    return sec < 60 ? `in ${sec}s` : `in ${Math.round(sec / 60)}m`;
  }
  return timeAgo(val);
}

function isHeartbeatStale(lastHeartbeat: string): boolean {
  return (Date.now() - parseTimestamp(lastHeartbeat).getTime()) > 300_000;
}

export function TaskDiagnosticsPanel({ task }: { task: Task }) {
  const ctx = task.context as Record<string, unknown> | undefined;
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const [open, setOpen] = useState(() => {
    if (task.status === 'FAILED') return true;
    if (!isTerminal && task.last_heartbeat) return isHeartbeatStale(task.last_heartbeat);
    return false;
  });

  const diagScalarFields = ['retry_after', 'provision_zone', 'provision_failures', 'max_plan_account_id', 'blacklisted_accounts', '_first_attempt_succeeded'];
  const hasScalar = diagScalarFields.some(f => f in (ctx ?? {}));
  const toolTrace = Array.isArray(ctx?._tool_trace) ? (ctx._tool_trace as ToolTraceEntry[]) : null;
  const serialLog = typeof ctx?.last_serial_log === 'string' ? ctx.last_serial_log : null;
  const hasAny = serialLog !== null || toolTrace !== null || hasScalar || task.vm_instance_id;
  if (!hasAny) return null;

  const c = ctx ?? {};

  return (
    <div className="bg-workbench-base/50 rounded-lg border border-neutral-800 mb-2">
      {/* reason: collapsible section header; text-xs py-2 single-row height matches drawer density convention */}
      <button
        data-viewport-allow-small-target
        onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1.5 w-full text-left px-3 py-2 text-xs font-semibold uppercase tracking-wide text-neutral-400 hover:text-neutral-200 transition-colors"
      >
        {open ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronRight className="w-3.5 h-3.5" />}
        Diagnostics
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-3 border-t border-neutral-800 pt-3">
          <SerialLogBlock
            log={serialLog}
            taskId={task.id}
            vmInstanceId={task.vm_instance_id}
          />

          {task.status_log && task.status_log.length > 0 && (
            <div className="text-xs">
              <p className="text-neutral-400 font-medium mb-1.5">Recent status log</p>
              <div className="space-y-0.5 max-h-32 overflow-y-auto custom-scrollbar bg-workbench-shell/60 rounded p-2 border border-neutral-800">
                {task.status_log.slice(-20).reverse().map((entry, i) => (
                  <div key={i} className="flex items-start gap-2 font-mono">
                    <span className="text-neutral-600 shrink-0">{String(entry.t).slice(11, 19)}</span>
                    <span className={getLogEntryColor(entry.msg)}>{entry.msg}</span>
                  </div>
                ))}
              </div>
            </div>
          )}

          {toolTrace && toolTrace.length > 0 && (
            <div className="text-xs">
              <p className="text-neutral-400 font-medium mb-1.5">Tool trace (last {Math.min(toolTrace.length, 30)} of {toolTrace.length})</p>
              <div className="bg-workbench-shell/60 rounded p-2 max-h-40 overflow-y-auto custom-scrollbar border border-neutral-800">
                <ToolTraceTail entries={toolTrace} />
              </div>
            </div>
          )}

          {hasScalar && (
            <div className="space-y-1.5 text-xs">
              <p className="text-neutral-400 font-medium mb-1">Scalar fields</p>
              {'retry_after' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Retry after</span>
                  <span className="text-neutral-200">{formatRetryAfter(c.retry_after)}</span>
                </div>
              )}
              {'provision_zone' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Provision zone</span>
                  <span className="text-neutral-200 font-mono">{String(c.provision_zone)}</span>
                </div>
              )}
              {'provision_failures' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Provision attempts</span>
                  <span className="text-neutral-200">{String(c.provision_failures)}</span>
                </div>
              )}
              {'max_plan_account_id' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Account</span>
                  <span className="text-neutral-200 font-mono break-all">{String(c.max_plan_account_id)}</span>
                </div>
              )}
              {'blacklisted_accounts' in c && Array.isArray(c.blacklisted_accounts) && (c.blacklisted_accounts as unknown[]).length > 0 && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">Blacklisted accts</span>
                  <span className="text-neutral-200 font-mono break-all">{(c.blacklisted_accounts as unknown[]).join(', ')}</span>
                </div>
              )}
              {'_first_attempt_succeeded' in c && (
                <div className="flex gap-2">
                  <span className="text-neutral-500 w-36 shrink-0">First attempt</span>
                  <span className={c._first_attempt_succeeded ? 'text-green-400' : 'text-red-400'}>{c._first_attempt_succeeded ? 'succeeded' : 'failed'}</span>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
