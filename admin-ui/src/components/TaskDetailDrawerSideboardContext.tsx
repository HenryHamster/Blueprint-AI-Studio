import type { ReactNode } from 'react';
import { Activity, AlertTriangle, Box, Cpu, DollarSign, GitBranch, GitPullRequest, Link2, MessageSquare, Network, Pin, Route } from 'lucide-react';
import type { Task } from '../types/task';
import { safeExternalUrl } from '../utils/externalUrl';
import { SectionHeader } from './TaskDetailDrawerPrimitives';

type SideboardLink = NonNullable<Task['sideboard_context']>['links'][number];
type SideboardTaskContext = NonNullable<Task['sideboard_context']>['task'];
type SideboardRunnerContext = NonNullable<NonNullable<SideboardTaskContext>['runner']>;

function artifactSummary(byType: Record<string, number>): string {
  const entries = Object.entries(byType);
  if (entries.length === 0) return 'none';
  return entries
    .map(([kind, count]) => `${kind.toLowerCase().replace(/_/g, ' ')} ${count}`)
    .join(' · ');
}

function pluralize(count: number, singular: string, plural = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

function compactTargetId(value?: string | null): string | null {
  if (!value) return null;
  return value.length > 28 ? `${value.slice(0, 18)}...${value.slice(-6)}` : value;
}

function externalTargetUrl(target: SideboardLink['target']): string | null {
  if (target.surface !== 'external') return null;
  return safeExternalUrl(target.url ?? target.id);
}

function resolveTaskTargetId(target: SideboardLink['target']): string | null {
  if (target.surface !== 'task') return null;
  return target.task_id ?? target.id ?? null;
}

function linkTargetLabel(target: SideboardLink['target']): string {
  const id = target.id ?? null;
  if (target.surface === 'task') return compactTargetId(target.task_id ?? id) ?? 'task';
  if (target.surface === 'pipeline') return compactTargetId(target.task_id ?? id) ?? 'pipeline';
  if (target.surface === 'project' && target.section === 'directions') {
    const directionId = compactTargetId(target.direction_id ?? id);
    return directionId ? `direction ${directionId}` : 'directions';
  }
  if (target.surface === 'project') return target.section?.replace(/_/g, ' ') ?? 'project';
  const externalUrl = externalTargetUrl(target);
  if (externalUrl) {
    const url = new URL(externalUrl);
    return `${url.hostname}${url.pathname}`;
  }
  return [target.surface, target.section, id].filter(Boolean).join(' / ');
}

function formatInlineLabel(value?: string | null): string | null {
  return value ? value.replace(/_/g, ' ') : null;
}

function formatCost(value?: number | null): string | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return `$${value.toFixed(2)}`;
}

function truncateText(value: string, maxChars: number): string {
  return value.length > maxChars ? `${value.slice(0, maxChars - 1)}…` : value;
}

function runnerSummary(runner?: SideboardRunnerContext | null): string | null {
  if (!runner) return null;
  return [
    runner.vm_instance_id,
    runner.runner_target_id ? `runner ${runner.runner_target_id}` : null,
    runner.machine_type,
    runner.provider,
    runner.account_id,
  ].filter(Boolean).join(' / ') || null;
}

function ContextSurface({ children }: { children: ReactNode }) {
  return (
    <div className="mb-3 min-w-0 overflow-hidden rounded-md border border-neutral-800 bg-[#0d141c] last:mb-0">
      {children}
    </div>
  );
}

function ContextGroup({
  icon,
  label,
  meta,
  children,
}: {
  icon: ReactNode;
  label: string;
  meta?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="min-w-0 border-b border-neutral-800 p-2.5 last:border-b-0">
      <div className="mb-1.5 flex min-w-0 items-center gap-2">
        {icon}
        <div className="min-w-0 flex-1 text-ui-sm font-semibold uppercase text-neutral-500">{label}</div>
        {meta && <div className="shrink-0 text-ui-sm text-neutral-600">{meta}</div>}
      </div>
      {children}
    </section>
  );
}

function ContextLabel({ children }: { children: ReactNode }) {
  return <div className="text-ui-sm font-semibold uppercase text-neutral-600">{children}</div>;
}

function ContextValue({ children, muted = false }: { children: ReactNode; muted?: boolean }) {
  return (
    <div className={`min-w-0 break-words text-sm ${muted ? 'text-neutral-500' : 'text-neutral-200'}`}>
      {children}
    </div>
  );
}

function ContextStat({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="min-w-0 rounded border border-neutral-800/80 bg-[#10151c] px-2 py-1.5">
      <ContextLabel>{label}</ContextLabel>
      <div className="mt-0.5 min-w-0 break-words text-sm font-medium text-neutral-200">{value}</div>
    </div>
  );
}

export function TaskSideboardContextSection({
  task,
  onNavigateToTask,
}: {
  task: Task;
  onNavigateToTask?: (taskId: string) => void;
}) {
  const context = task.sideboard_context;
  if (!context) return null;
  const taskContext = context.task;
  const blocker = taskContext?.blocker;
  const runner = taskContext?.runner;
  const runnerLabel = runnerSummary(runner);
  const pinnedRunnerId = runner?.preferred_runner_target_id || null;
  const pinHold = runner?.runner_pin_hold ?? null;
  const vmCost = formatCost(taskContext?.cost?.estimated_vm_usd);
  const llmCost = formatCost(taskContext?.cost?.llm_usd);
  const hasTaskContext = Boolean(
    taskContext?.status ||
    taskContext?.task_type ||
    taskContext?.latest_status_message ||
    taskContext?.next_action ||
    blocker?.reason ||
    runnerLabel ||
    vmCost ||
    llmCost,
  );
  const parentTaskId = context.pipeline.parent_task_id;
  const pipelineMovementExplanation = context.pipeline.movement_explanation?.trim();
  const pipelineReason = pipelineMovementExplanation || context.pipeline.blocked_reason;
  const direction = context.strategic.direction;
  const cycle = context.strategic.cycle;
  const recentMessages = context.messages.items.slice(0, 2);
  const hiddenMessageCount = Math.max(0, context.messages.total_recent - recentMessages.length);
  const hasMessages = recentMessages.length > 0 || context.messages.total_recent > 0;
  const primaryPrUrl = safeExternalUrl(context.artifacts.primary_pr_url);
  const projectLabel = context.project.repo || context.project.name || context.project.id;
  const projectSubLabel = context.project.repo && context.project.name ? context.project.name : context.project.id;
  const strategicMeta = [
    formatInlineLabel(direction?.priority),
    formatInlineLabel(direction?.status),
  ].filter(Boolean).join(' / ');
  const cycleMeta = [
    formatInlineLabel(cycle?.phase),
    formatInlineLabel(cycle?.outcome),
  ].filter(Boolean).join(' / ');
  const hasPipeline = Boolean(
    parentTaskId ||
    context.pipeline.child_count > 0 ||
    context.pipeline.dependency_count > 0 ||
    pipelineReason,
  );

  return (
    <>
      <SectionHeader>Context</SectionHeader>
      <ContextSurface>
        {hasTaskContext && taskContext && (
          <ContextGroup
            icon={<Activity className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
            label="Task"
            meta={formatInlineLabel(taskContext.status)}
          >
            <div className="grid min-w-0 gap-2">
              <div className="grid min-w-0 grid-cols-2 gap-1.5 sm:grid-cols-3">
                <ContextStat label="State" value={formatInlineLabel(taskContext.status) ?? task.status} />
                {taskContext.task_type && <ContextStat label="Type" value={formatInlineLabel(taskContext.task_type)} />}
                {taskContext.next_action?.label && (
                  <ContextStat label="Next" value={taskContext.next_action.label} />
                )}
              </div>
              {taskContext.latest_status_message && (
                <ContextValue>{taskContext.latest_status_message}</ContextValue>
              )}
              {taskContext.next_action?.reason && (
                <div className="break-words rounded border border-cyan-500/20 bg-cyan-500/5 px-2 py-1.5 text-sm text-cyan-200">
                  {taskContext.next_action.reason}
                </div>
              )}
            </div>
          </ContextGroup>
        )}

        {(blocker?.reason || blocker?.kind || blocker?.recheck_after || blocker?.parked_at) && (
          <ContextGroup
            icon={<AlertTriangle className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
            label="Current blocker"
            meta={formatInlineLabel(blocker?.kind)}
          >
            <div className="grid min-w-0 gap-1.5">
              {blocker?.reason && (
                <div className="break-words rounded border border-amber-500/25 bg-amber-500/5 px-2 py-1.5 text-sm text-amber-200">
                  {blocker.reason}
                </div>
              )}
              <div className="grid min-w-0 grid-cols-2 gap-1.5 sm:grid-cols-3">
                {blocker?.blocked_at && <ContextStat label="Blocked" value={blocker.blocked_at} />}
                {blocker?.parked_at && <ContextStat label="Parked" value={blocker.parked_at} />}
                {blocker?.recheck_after && <ContextStat label="Recheck" value={blocker.recheck_after} />}
              </div>
            </div>
          </ContextGroup>
        )}

        {(runnerLabel || pinnedRunnerId || pinHold || vmCost || llmCost || taskContext?.cost?.llm_any_unpriced) && (
          <ContextGroup
            icon={<Cpu className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
            label="Runner"
            meta={runner?.spot ? 'spot' : runner?.warm_start ? 'warm' : undefined}
          >
            <div className="grid min-w-0 gap-2">
              {pinnedRunnerId && (
                <div className="min-w-0">
                  <span
                    data-testid="runner-pin-badge"
                    title={`Pinned to runner ${pinnedRunnerId}`}
                    className="inline-flex max-w-full items-center gap-1 rounded border border-sky-500/30 bg-sky-500/10 px-1.5 py-0.5 text-xs font-semibold text-sky-300"
                  >
                    <Pin className="h-3 w-3 shrink-0" aria-hidden="true" />
                    <span className="truncate font-mono">pinned · {compactTargetId(pinnedRunnerId)}</span>
                  </span>
                </div>
              )}
              {pinHold && (
                <div
                  data-testid="runner-pin-hold"
                  className="min-w-0 break-words rounded border border-amber-500/30 bg-amber-500/10 px-2 py-1.5 text-sm text-amber-200"
                >
                  <div className="flex items-center gap-1.5 font-semibold">
                    <AlertTriangle className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    <span>
                      Runner pin hold
                      {pinHold.reason_code ? ` — ${formatInlineLabel(pinHold.reason_code)}` : ''}
                    </span>
                  </div>
                  {pinHold.detail && (
                    <div className="mt-1 text-amber-200/90">{truncateText(pinHold.detail, 220)}</div>
                  )}
                  {pinHold.runner_id && (
                    <div className="mt-1 font-mono text-xs text-amber-300/80">
                      runner {compactTargetId(pinHold.runner_id)}
                    </div>
                  )}
                </div>
              )}
              {runnerLabel && <ContextValue>{runnerLabel}</ContextValue>}
              {(vmCost || llmCost || taskContext?.cost?.llm_any_unpriced) && (
                <div className="grid min-w-0 grid-cols-2 gap-1.5 sm:grid-cols-3">
                  {vmCost && <ContextStat label="VM cost" value={vmCost} />}
                  {llmCost && <ContextStat label="LLM cost" value={llmCost} />}
                  {taskContext?.cost?.llm_any_unpriced && (
                    <ContextStat
                      label="LLM pricing"
                      value={(
                        <span className="inline-flex items-center gap-1">
                          <DollarSign className="h-3 w-3" aria-hidden="true" />
                          partial
                        </span>
                      )}
                    />
                  )}
                </div>
              )}
            </div>
          </ContextGroup>
        )}

        <ContextGroup
          icon={<Network className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
          label="Project"
          meta={context.project.id}
        >
          <div className="grid min-w-0 gap-2">
            <div className="min-w-0">
              <ContextValue>{projectLabel}</ContextValue>
              {projectSubLabel !== projectLabel && (
                <div className="truncate text-xs text-neutral-500">{projectSubLabel}</div>
              )}
            </div>
            {context.purpose && (
              <p className="break-words border-t border-neutral-800 pt-2 text-sm leading-relaxed text-neutral-300">
                {context.purpose}
              </p>
            )}
          </div>
        </ContextGroup>

        {(direction || cycle) && (
          <ContextGroup
            icon={<Route className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
            label="Strategic"
            meta={strategicMeta || cycleMeta || undefined}
          >
            {direction && (
              <div className="min-w-0">
                <ContextLabel>Direction</ContextLabel>
                {direction.text && <ContextValue>{direction.text}</ContextValue>}
                <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-neutral-500">
                  <span className="break-all font-mono">{direction.id}</span>
                  {direction.priority && <span>{formatInlineLabel(direction.priority)}</span>}
                  {direction.status && <span>{formatInlineLabel(direction.status)}</span>}
                </div>
              </div>
            )}
            {cycle && (
              <div className="min-w-0 border-t border-neutral-800 pt-2">
                <ContextLabel>Strategic cycle</ContextLabel>
                <div className="flex min-w-0 flex-wrap gap-x-2 gap-y-1 text-sm">
                  <span className="break-all font-mono text-neutral-300">{cycle.id}</span>
                  {cycle.phase && <span className="text-neutral-500">{cycle.phase.replace(/_/g, ' ')}</span>}
                  <span className="text-neutral-500">{cycle.outcome}</span>
                </div>
              </div>
            )}
          </ContextGroup>
        )}

        {hasPipeline && (
          <ContextGroup
            icon={<GitBranch className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
            label="Pipeline"
            meta={context.pipeline.blocked_reason ? 'blocked' : undefined}
          >
            <div className="grid min-w-0 grid-cols-2 gap-1.5 sm:grid-cols-3">
              {parentTaskId && (
                <ContextStat
                  label="Parent"
                  value={onNavigateToTask ? (
                    <button
                      type="button"
                      onClick={() => onNavigateToTask(parentTaskId)}
                      aria-label={`parent ${parentTaskId}`}
                      data-viewport-allow-small-target
                      className="break-all font-mono text-cyan-300 hover:text-cyan-200"
                    >
                      {parentTaskId.slice(0, 8)}
                    </button>
                  ) : (
                    <span className="break-all font-mono">{parentTaskId.slice(0, 8)}</span>
                  )}
                />
              )}
              {context.pipeline.dependency_count > 0 && (
                <ContextStat label="Dependencies" value={context.pipeline.dependency_count} />
              )}
              {context.pipeline.child_count > 0 && (
                <ContextStat label="Children" value={context.pipeline.child_count} />
              )}
            </div>
            {pipelineReason && (
              <div className="mt-2 break-words rounded border border-rose-500/25 bg-rose-500/5 px-2 py-1.5 text-sm text-rose-300">
                {pipelineReason}
              </div>
            )}
          </ContextGroup>
        )}

        {context.artifacts.total > 0 && (
          <ContextGroup
            icon={<Box className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
            label="Evidence"
            meta={pluralize(context.artifacts.total, 'artifact')}
          >
            <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
              <div className="min-w-0">
                <ContextValue>{artifactSummary(context.artifacts.by_type)}</ContextValue>
                {context.artifacts.primary_commit && (
                  <div className="mt-0.5 break-all font-mono text-xs text-neutral-500">
                    {context.artifacts.primary_commit.slice(0, 12)}
                  </div>
                )}
              </div>
              {primaryPrUrl && (
                <a
                  href={primaryPrUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-viewport-allow-small-target
                  className="inline-flex min-h-[44px] items-center gap-1.5 rounded-md border border-neutral-800 bg-[#10151c] px-2 text-xs font-semibold text-cyan-300 hover:border-cyan-500/50 hover:text-cyan-200"
                >
                  <GitPullRequest className="h-3.5 w-3.5" aria-hidden="true" />
                  PR
                </a>
              )}
            </div>
          </ContextGroup>
        )}

        {context.links.length > 0 && (
          <ContextGroup
            icon={<Link2 className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
            label="Links"
            meta={context.links.length}
          >
            <div className="flex min-w-0 flex-wrap gap-1.5">
              {context.links.map(link => {
                const targetLabel = linkTargetLabel(link.target);
                const taskId = resolveTaskTargetId(link.target);
                const canOpenTask = Boolean(taskId && onNavigateToTask);
                const externalUrl = externalTargetUrl(link.target);
                const className = 'inline-flex max-w-full min-w-0 flex-col rounded border border-neutral-800 bg-[#10151c] px-2 py-1 text-left transition-colors';
                const children = (
                  <>
                    <span className="break-words text-xs font-medium text-neutral-200">{link.label}</span>
                    {targetLabel && (
                      <span className="break-all font-mono text-xs leading-4 text-neutral-500">{targetLabel}</span>
                    )}
                  </>
                );
                if (canOpenTask) {
                  return (
                    <button
                      key={link.id}
                      type="button"
                      onClick={() => {
                        if (taskId) onNavigateToTask?.(taskId);
                      }}
                      aria-label={`${link.label} ${targetLabel}`}
                      data-viewport-allow-small-target
                      className={`${className} hover:border-cyan-500/50 hover:bg-cyan-950/30`}
                    >
                      {children}
                    </button>
                  );
                }
                if (externalUrl) {
                  return (
                    <a
                      key={link.id}
                      href={externalUrl}
                      target="_blank"
                      rel="noopener noreferrer"
                      data-viewport-allow-small-target
                      className={`${className} hover:border-cyan-500/50 hover:bg-cyan-950/30`}
                    >
                      {children}
                    </a>
                  );
                }
                return (
                  <span key={link.id} className={className}>
                    {children}
                  </span>
                );
              })}
            </div>
          </ContextGroup>
        )}

        {hasMessages && (
          <ContextGroup
            icon={<MessageSquare className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
            label="Messages"
            meta={context.messages.total_recent}
          >
            <div className="min-w-0 space-y-1.5">
              {recentMessages.map(message => (
                <p key={message.id} className="line-clamp-2 border-l border-neutral-700 pl-2 text-sm text-neutral-300">
                  {message.content}
                </p>
              ))}
              {hiddenMessageCount > 0 && (
                <p className="text-xs text-neutral-500">+{pluralize(hiddenMessageCount, 'more recent message')}</p>
              )}
              {recentMessages.length === 0 && context.messages.total_recent > 0 && (
                <p className="text-xs text-neutral-500">{pluralize(context.messages.total_recent, 'recent message')}</p>
              )}
            </div>
          </ContextGroup>
        )}
      </ContextSurface>
    </>
  );
}
