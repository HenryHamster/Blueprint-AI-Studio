import { useEffect, useState } from 'react';
import { CheckCircle, Clock, AlertTriangle, ChevronDown, ChevronUp } from 'lucide-react';
import type { Project } from '../types/project';
import type { Task } from './TaskCard';
import type { BuildConfigCommandEntry, BuildConfigCommandsResponse } from '../api';
import { fetchBuildConfigCommands } from '../api';
import { formatAbsolute } from '../utils/time';

interface Props {
  project: Project;
  tasks: Task[];
}

type BootstrapState = 'pending' | 'completed' | 'degraded' | 'none';

function deriveBootstrapState(project: Project, tasks: Task[]): BootstrapState {
  if (project.is_self_repo) return 'none';
  const completedAt = project.build_config?.bootstrap_completed_at;
  if (completedAt) return 'completed';

  const bootstrapTasks = tasks.filter(t => {
    const first = (t.payloads as Array<Record<string, unknown>> | undefined)?.[0];
    return first?.type === 'repo_bootstrap';
  });
  if (bootstrapTasks.length === 0) return 'none';

  const activeStatuses = new Set(['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING']);
  const failedStatuses = new Set(['FAILED', 'CANCELLED']);

  const hasActive = bootstrapTasks.some(t => activeStatuses.has(t.status));
  if (hasActive) return 'pending';

  const hasFailed = bootstrapTasks.some(t => failedStatuses.has(t.status));
  if (hasFailed) return 'degraded';

  return 'none';
}

export function BootstrapStatusPanel({ project, tasks }: Props) {
  const state = deriveBootstrapState(project, tasks);
  const [commands, setCommands] = useState<BuildConfigCommandEntry[] | null>(null);
  const [commandsError, setCommandsError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    if (state === 'none') return;
    const controller = new AbortController();
    fetchBuildConfigCommands(project.id, controller.signal)
      .then((res: BuildConfigCommandsResponse) => {
        setCommands(res.directories);
      })
      .catch((err: unknown) => {
        if (err instanceof Error && err.name === 'AbortError') return;
        setCommandsError(err instanceof Error ? err.message : 'Failed to load commands');
      });
    return () => controller.abort();
  }, [project.id, state]);

  if (state === 'none') return null;

  const completedAt = project.build_config?.bootstrap_completed_at;

  return (
    <div className="mt-3 rounded-xl border border-neutral-800 bg-neutral-950/60 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {state === 'completed' && (
            <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />
          )}
          {state === 'pending' && (
            <Clock className="w-4 h-4 text-amber-400 shrink-0 animate-pulse" />
          )}
          {state === 'degraded' && (
            <AlertTriangle className="w-4 h-4 text-red-400 shrink-0" />
          )}
          <div>
            <div className="text-[11px] font-bold text-neutral-300 uppercase tracking-widest">
              Bootstrap
            </div>
            {state === 'completed' && completedAt && (
              <div className="text-[11px] text-emerald-400">
                Completed {formatAbsolute(completedAt)}
              </div>
            )}
            {state === 'pending' && (
              <div className="text-[11px] text-amber-300">
                Bootstrap pending — implementation tasks deferred until complete
              </div>
            )}
            {state === 'degraded' && (
              <div className="text-[11px] text-red-300">
                Bootstrap failed — cycles running without repo memory
              </div>
            )}
          </div>
        </div>
        {commands !== null && commands.length > 0 && (
          <button
            onClick={() => setExpanded(e => !e)}
            className="flex items-center gap-1 text-[10px] text-neutral-400 hover:text-neutral-200 transition-colors shrink-0"
          >
            Commands
            {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
          </button>
        )}
      </div>

      {commandsError && (
        <p className="mt-2 text-[10px] text-red-300 bg-red-500/10 border border-red-500/20 rounded px-1.5 py-1 break-words">
          {commandsError}
        </p>
      )}

      {expanded && commands !== null && commands.length > 0 && (
        <div className="mt-3 overflow-x-auto">
          <table className="w-full text-[10px] font-mono border-collapse">
            <thead>
              <tr className="text-neutral-500 border-b border-neutral-800">
                <th className="text-left py-1 pr-2 font-semibold">Directory</th>
                <th className="text-left py-1 pr-2 font-semibold">Type</th>
                <th className="text-left py-1 pr-2 font-semibold">Detected test</th>
                <th className="text-left py-1 pr-2 font-semibold">Confirmed test</th>
                <th className="text-left py-1 pr-2 font-semibold">Detected build</th>
                <th className="text-left py-1 pr-2 font-semibold">Confirmed build</th>
                <th className="text-left py-1 font-semibold">Status</th>
              </tr>
            </thead>
            <tbody>
              {commands.map((dir, i) => (
                <tr key={i} className="border-b border-neutral-800/40 hover:bg-neutral-900/30">
                  <td className="py-1 pr-2 text-neutral-300 max-w-[120px] truncate" title={dir.path ?? ''}>
                    {dir.path ?? '—'}
                  </td>
                  <td className="py-1 pr-2 text-neutral-400">{dir.type ?? '—'}</td>
                  <td className="py-1 pr-2 text-neutral-400 max-w-[120px] truncate" title={dir.detected_test_cmd ?? ''}>
                    {dir.detected_test_cmd ?? <span className="text-neutral-600">none</span>}
                  </td>
                  <td className="py-1 pr-2 max-w-[120px] truncate" title={dir.confirmed_test_cmd ?? ''}>
                    {dir.confirmed_test_cmd
                      ? <span className="text-emerald-400">{dir.confirmed_test_cmd}</span>
                      : <span className="text-neutral-600">—</span>}
                  </td>
                  <td className="py-1 pr-2 text-neutral-400 max-w-[120px] truncate" title={dir.detected_build_cmd ?? ''}>
                    {dir.detected_build_cmd ?? <span className="text-neutral-600">none</span>}
                  </td>
                  <td className="py-1 pr-2 max-w-[120px] truncate" title={dir.confirmed_build_cmd ?? ''}>
                    {dir.confirmed_build_cmd
                      ? <span className="text-emerald-400">{dir.confirmed_build_cmd}</span>
                      : <span className="text-neutral-600">—</span>}
                  </td>
                  <td className="py-1">
                    {dir.commands_stale
                      ? <span className="px-1 rounded bg-amber-500/20 text-amber-300">stale</span>
                      : dir.commands_confirmed_at
                        ? <span className="px-1 rounded bg-emerald-500/20 text-emerald-300">confirmed</span>
                        : <span className="text-neutral-600">unconfirmed</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
