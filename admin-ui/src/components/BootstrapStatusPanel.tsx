import { useEffect, useState } from 'react';
import { CheckCircle, Clock, AlertTriangle, ChevronDown, ChevronUp } from 'lucide-react';
import type { Project } from '../types/project';
import type { Task } from './TaskCard';
import type {
  BootstrapRetryResponse,
  BuildConfigCommandEntry,
  BuildConfigCommandsResponse,
} from '../api';
import { fetchBuildConfigCommands, retryProjectBootstrap, refreshProjectBootstrap } from '../api';
import { formatAbsolute } from '../utils/time';

interface Props {
  project: Project;
  tasks: Task[];
}

type BootstrapState = 'pending' | 'completed' | 'degraded' | 'retryable' | 'empty' | 'none';
type RetryOutcome = Pick<BootstrapRetryResponse, 'state' | 'task_id' | 'reason'>;

function deriveBootstrapState(project: Project, tasks: Task[]): BootstrapState {
  if (project.is_self_repo) return 'none';
  if (project.build_config?.repo_is_empty) return 'empty';

  const completedAt = project.build_config?.bootstrap_completed_at;
  if (completedAt) return 'completed';

  if (project.build_config?.bootstrap_degraded_reason) return 'degraded';

  const bootstrapTasks = tasks.filter(t => {
    const first = (t.payloads as Array<Record<string, unknown>> | undefined)?.[0];
    return first?.type === 'repo_bootstrap';
  });
  const activeStatuses = new Set(['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING']);
  const failedStatuses = new Set(['FAILED', 'CANCELLED']);

  const hasActive = bootstrapTasks.some(t => activeStatuses.has(t.status));
  if (hasActive) return 'pending';

  const hasFailed = bootstrapTasks.some(t => failedStatuses.has(t.status));
  if (hasFailed || project.build_config?.bootstrap_degraded_reason) return 'degraded';

  return 'retryable';
}

function getRefreshOutcomeMessage(outcome: RetryOutcome): string {
  if (outcome.state === 'spawned') {
    return outcome.task_id
      ? `Bootstrap refresh queued as ${outcome.task_id}.`
      : 'Bootstrap refresh queued.';
  }
  if (outcome.state === 'already_active') {
    return 'A bootstrap is already running.';
  }
  return 'This project is not eligible for a bootstrap refresh.';
}

function getRetryOutcomeMessage(outcome: RetryOutcome): string {
  if (outcome.state === 'spawned') {
    return outcome.task_id
      ? `Bootstrap retry queued as ${outcome.task_id}; bootstrap is now running.`
      : 'Bootstrap retry queued; bootstrap is now running.';
  }
  if (outcome.state === 'already_active') {
    return outcome.task_id
      ? `Bootstrap is already running (task ${outcome.task_id}).`
      : 'Bootstrap is already running.';
  }
  if (outcome.state === 'already_completed') {
    return 'Bootstrap has already completed.';
  }
  return 'This project is not eligible for bootstrap recovery.';
}

function getRecoveryDetail(project: Project): string | null {
  const cfg = project.build_config;
  if (!cfg?.bootstrap_degraded_reason) return null;
  const attempts = cfg.bootstrap_recovery_attempts ?? 0;
  if (cfg.bootstrap_recovery_state === 'exhausted') {
    return attempts > 0
      ? `Automatic recovery exhausted after ${attempts} attempts; operator retry is still available.`
      : 'Automatic recovery exhausted; operator retry is still available.';
  }
  if (cfg.bootstrap_recovery_next_at) {
    return `Automatic retry after ${formatAbsolute(cfg.bootstrap_recovery_next_at)}.`;
  }
  if (attempts > 0) {
    return `Automatic recovery attempt ${attempts} recorded.`;
  }
  return null;
}

export function BootstrapStatusPanel({ project, tasks }: Props) {
  const state = deriveBootstrapState(project, tasks);
  const [commands, setCommands] = useState<BuildConfigCommandEntry[] | null>(null);
  const [commandsError, setCommandsError] = useState<string | null>(null);
  const [retryOutcome, setRetryOutcome] = useState<RetryOutcome | null>(null);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [retryPending, setRetryPending] = useState(false);
  const [refreshOutcome, setRefreshOutcome] = useState<RetryOutcome | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [refreshPending, setRefreshPending] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [overviewExpanded, setOverviewExpanded] = useState(false);

  useEffect(() => {
    setRetryOutcome(null);
    setRetryError(null);
    setRetryPending(false);
    setRefreshOutcome(null);
    setRefreshError(null);
    setRefreshPending(false);
  }, [project.id]);

  useEffect(() => {
    if (state === 'none' || state === 'empty') return;
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
  const repoOverview = project.design_doc?.repo_overview;
  const hasOverviewContent = !!(
    repoOverview?.tech_stack ||
    repoOverview?.directory_layout ||
    repoOverview?.first_steps_summary
  );
  const recoveryDetail = getRecoveryDetail(project);
  const retryState = retryOutcome?.state;
  const refreshState = refreshOutcome?.state;
  const displayState: BootstrapState =
    retryState === 'spawned' || retryState === 'already_active'
      ? 'pending'
      : retryState === 'already_completed'
      ? 'completed'
      : refreshState === 'spawned' || refreshState === 'already_active'
      ? 'pending'
      : state;
  const retryOutcomeMessage = retryOutcome ? getRetryOutcomeMessage(retryOutcome) : null;
  const refreshOutcomeMessage = refreshOutcome ? getRefreshOutcomeMessage(refreshOutcome) : null;
  const showRetryButton =
    !retryPending &&
    !retryOutcome &&
    (state === 'degraded' || state === 'retryable');
  const showRefreshButton =
    !refreshPending &&
    !refreshOutcome &&
    state === 'completed';

  const handleRetry = async () => {
    setRetryPending(true);
    setRetryError(null);
    try {
      const outcome = await retryProjectBootstrap(project.id);
      setRetryOutcome(outcome);
    } catch (err) {
      setRetryError(err instanceof Error ? err.message : 'Failed to retry bootstrap');
    } finally {
      setRetryPending(false);
    }
  };

  const handleRefresh = async () => {
    setRefreshPending(true);
    setRefreshError(null);
    try {
      const outcome = await refreshProjectBootstrap(project.id);
      setRefreshOutcome(outcome);
    } catch (err) {
      setRefreshError(err instanceof Error ? err.message : 'Failed to refresh bootstrap');
    } finally {
      setRefreshPending(false);
    }
  };

  return (
    <div className="mt-3 rounded-xl border border-neutral-800 bg-neutral-950/60 p-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          {displayState === 'completed' && (
            <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />
          )}
          {displayState === 'pending' && (
            <Clock className="w-4 h-4 text-amber-400 shrink-0 animate-pulse" />
          )}
          {displayState === 'degraded' && (
            <AlertTriangle className="w-4 h-4 text-red-400 shrink-0" />
          )}
          {displayState === 'retryable' && (
            <Clock className="w-4 h-4 text-amber-400 shrink-0" />
          )}
          {displayState === 'empty' && (
            <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />
          )}
          <div>
            <div className="text-[11px] font-bold text-neutral-300 uppercase tracking-widest">
              Bootstrap
            </div>
            {displayState === 'completed' && completedAt && (
              <div className="text-[11px] text-emerald-400">
                Completed {formatAbsolute(completedAt)}
              </div>
            )}
            {displayState === 'completed' && (
              project.build_config?.bootstrap_overview_refreshed_at ||
              project.build_config?.bootstrap_overview_source_branch
            ) && (
              <div className="text-[10px] text-neutral-400 mt-0.5">
                {project.build_config?.bootstrap_overview_refreshed_at && (
                  <>Refreshed {formatAbsolute(project.build_config.bootstrap_overview_refreshed_at)}</>
                )}
                {project.build_config?.bootstrap_overview_source_branch && (
                  <> · branch <span className="font-mono text-neutral-300">{project.build_config.bootstrap_overview_source_branch}</span></>
                )}
                {project.build_config?.bootstrap_overview_source_revision && (
                  <> @ <span className="font-mono text-neutral-300">{project.build_config.bootstrap_overview_source_revision.slice(0, 7)}</span></>
                )}
              </div>
            )}
            {displayState === 'pending' && (
              <div className="text-[11px] text-amber-300">
                Bootstrap pending — implementation tasks deferred until complete
              </div>
            )}
            {displayState === 'degraded' && (
              <>
                <div className="text-[11px] text-red-300">
                  {project.build_config?.bootstrap_degraded_reason
                    ? `Bootstrap degraded — ${project.build_config.bootstrap_degraded_reason}`
                    : 'Bootstrap failed — cycles running without repo memory'}
                </div>
                {recoveryDetail && (
                  <div className="text-[10px] text-neutral-400">
                    {recoveryDetail}
                  </div>
                )}
              </>
            )}
            {displayState === 'retryable' && (
              <div className="text-[11px] text-amber-300">
                Bootstrap has not run yet — retry will enqueue the first-cycle bootstrap
              </div>
            )}
            {displayState === 'empty' && (
              <div className="text-[11px] text-emerald-400">
                Empty repository — bootstrap will start after initial project files land
              </div>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          {showRetryButton && (
            <button
              onClick={handleRetry}
              disabled={retryPending}
              className="rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1 text-[10px] font-semibold text-amber-200 transition-colors hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-60 min-h-[44px]"
            >
              {retryPending ? 'Retrying…' : 'Retry bootstrap'}
            </button>
          )}
          {showRefreshButton && (
            <button
              onClick={handleRefresh}
              disabled={refreshPending}
              className="rounded-md border border-emerald-500/40 bg-emerald-500/10 px-2 py-1 text-[10px] font-semibold text-emerald-200 transition-colors hover:bg-emerald-500/20 disabled:cursor-not-allowed disabled:opacity-60 min-h-[44px]"
            >
              {refreshPending ? 'Refreshing…' : 'Refresh bootstrap'}
            </button>
          )}
          {completedAt && hasOverviewContent && (
            <button
              onClick={() => setOverviewExpanded(e => !e)}
              className="flex items-center gap-1 text-[10px] text-neutral-400 hover:text-neutral-200 transition-colors shrink-0"
            >
              Repo Overview
              {overviewExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
            </button>
          )}
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
      </div>

      {retryOutcomeMessage && (
        <p className="mt-2 text-[10px] text-emerald-300 bg-emerald-500/10 border border-emerald-500/20 rounded px-1.5 py-1 break-words">
          {retryOutcomeMessage}
        </p>
      )}

      {retryError && (
        <p className="mt-2 text-[10px] text-red-300 bg-red-500/10 border border-red-500/20 rounded px-1.5 py-1 break-words">
          {retryError}
        </p>
      )}

      {refreshOutcomeMessage && (
        <p className="mt-2 text-[10px] text-emerald-300 bg-emerald-500/10 border border-emerald-500/20 rounded px-1.5 py-1 break-words">
          {refreshOutcomeMessage}
        </p>
      )}

      {refreshError && (
        <p className="mt-2 text-[10px] text-red-300 bg-red-500/10 border border-red-500/20 rounded px-1.5 py-1 break-words">
          {refreshError}
        </p>
      )}

      {commandsError && (
        <p className="mt-2 text-[10px] text-red-300 bg-red-500/10 border border-red-500/20 rounded px-1.5 py-1 break-words">
          {commandsError}
        </p>
      )}

      {overviewExpanded && completedAt && repoOverview && hasOverviewContent && (
        <div className="mt-3 space-y-2">
          {repoOverview.tech_stack && (
            <div>
              <div className="text-[10px] font-semibold text-neutral-400 uppercase tracking-widest mb-0.5">
                Tech Stack
              </div>
              <div className="text-[11px] text-neutral-300 leading-relaxed whitespace-pre-wrap">
                {repoOverview.tech_stack}
              </div>
            </div>
          )}
          {repoOverview.directory_layout && (
            <div>
              <div className="text-[10px] font-semibold text-neutral-400 uppercase tracking-widest mb-0.5">
                Directory Layout
              </div>
              <div className="text-[11px] text-neutral-300 leading-relaxed whitespace-pre-wrap">
                {repoOverview.directory_layout}
              </div>
            </div>
          )}
          {repoOverview.first_steps_summary && (
            <div>
              <div className="text-[10px] font-semibold text-neutral-400 uppercase tracking-widest mb-0.5">
                First Steps
              </div>
              <div className="text-[11px] text-neutral-300 leading-relaxed whitespace-pre-wrap">
                {repoOverview.first_steps_summary}
              </div>
            </div>
          )}
          {repoOverview.entry_points && (
            <div>
              <div className="text-[10px] font-semibold text-neutral-400 uppercase tracking-widest mb-0.5">
                Entry Points
              </div>
              <div className="text-[11px] text-neutral-300 leading-relaxed whitespace-pre-wrap">
                {repoOverview.entry_points}
              </div>
            </div>
          )}
          {repoOverview.test_layout && (
            <div>
              <div className="text-[10px] font-semibold text-neutral-400 uppercase tracking-widest mb-0.5">
                Test Layout
              </div>
              <div className="text-[11px] text-neutral-300 leading-relaxed whitespace-pre-wrap">
                {repoOverview.test_layout}
              </div>
            </div>
          )}
          {repoOverview.key_conventions && (
            <div>
              <div className="text-[10px] font-semibold text-neutral-400 uppercase tracking-widest mb-0.5">
                Key Conventions
              </div>
              <div className="text-[11px] text-neutral-300 leading-relaxed whitespace-pre-wrap">
                {repoOverview.key_conventions}
              </div>
            </div>
          )}
          {repoOverview.environment_requirements && (
            <div>
              <div className="text-[10px] font-semibold text-neutral-400 uppercase tracking-widest mb-0.5">
                Environment Requirements
              </div>
              <div className="text-[11px] text-neutral-300 leading-relaxed whitespace-pre-wrap">
                {repoOverview.environment_requirements}
              </div>
            </div>
          )}
        </div>
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
                <th className="text-left py-1 pr-2 font-semibold">Detected lint</th>
                <th className="text-left py-1 pr-2 font-semibold">Confirmed lint</th>
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
                  <td className="py-1 pr-2 text-neutral-400 max-w-[120px] truncate" title={dir.detected_lint_cmd ?? ''}>
                    {dir.detected_lint_cmd ?? <span className="text-neutral-600">none</span>}
                  </td>
                  <td className="py-1 pr-2 max-w-[120px] truncate" title={dir.confirmed_lint_cmd ?? ''}>
                    {dir.confirmed_lint_cmd
                      ? <span className="text-emerald-400">{dir.confirmed_lint_cmd}</span>
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
