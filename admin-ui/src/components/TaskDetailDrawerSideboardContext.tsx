import type { ReactNode } from 'react';
import type { Task } from '../types/task';
import { DetailRow, SectionHeader } from './TaskDetailDrawerPrimitives';

type SideboardLink = NonNullable<Task['sideboard_context']>['links'][number];

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

function safeExternalUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
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

function ContextBlock({ children }: { children: ReactNode }) {
  return (
    <div className="mb-2 min-w-0 rounded-md border border-neutral-800 bg-neutral-950/45 px-3 py-2.5 last:mb-0">
      {children}
    </div>
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
    <div className="min-w-0 rounded-md border border-neutral-800/80 bg-[#10151c] px-2 py-1.5">
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
  const parentTaskId = context.pipeline.parent_task_id;
  const direction = context.strategic.direction;
  const cycle = context.strategic.cycle;
  const recentMessages = context.messages.items.slice(0, 2);
  const hiddenMessageCount = Math.max(0, context.messages.total_recent - recentMessages.length);
  const hasMessages = recentMessages.length > 0 || context.messages.total_recent > 0;
  const primaryPrUrl = safeExternalUrl(context.artifacts.primary_pr_url);

  return (
    <>
      <SectionHeader>Context</SectionHeader>
      <ContextBlock>
        <div className="grid min-w-0 gap-2">
          <div className="min-w-0">
            <ContextLabel>Project</ContextLabel>
            <ContextValue>{context.project.repo || context.project.name || context.project.id}</ContextValue>
            {context.project.repo && context.project.name && (
              <div className="truncate text-xs text-neutral-500">{context.project.name}</div>
            )}
          </div>
          {context.purpose && (
            <div className="min-w-0 border-t border-neutral-800 pt-2">
              <ContextLabel>Purpose</ContextLabel>
              <ContextValue>{context.purpose}</ContextValue>
            </div>
          )}
        </div>
      </ContextBlock>
      {(direction || cycle) && (
        <ContextBlock>
          <div className="grid min-w-0 gap-2">
            {direction && (
              <div className="min-w-0">
                <ContextLabel>Direction</ContextLabel>
                {direction.text && <ContextValue>{direction.text}</ContextValue>}
                <div className="break-all font-mono text-xs text-neutral-500">{direction.id}</div>
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
          </div>
        </ContextBlock>
      )}
      {(parentTaskId || context.pipeline.child_count > 0 || context.pipeline.dependency_count > 0) && (
        <ContextBlock>
          <ContextLabel>Pipeline</ContextLabel>
          <div className="mt-1.5 grid min-w-0 grid-cols-2 gap-1.5 sm:grid-cols-3">
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
        </ContextBlock>
      )}
      {context.pipeline.blocked_reason && (
        <ContextBlock>
          <ContextLabel>Blocked</ContextLabel>
          <div className="mt-1 break-words text-sm text-rose-300">{context.pipeline.blocked_reason}</div>
        </ContextBlock>
      )}
      {context.artifacts.total > 0 && (
        <ContextBlock>
          <div className="grid min-w-0 grid-cols-[minmax(0,1fr)_auto] items-start gap-3">
            <div className="min-w-0">
              <ContextLabel>Artifacts</ContextLabel>
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
                className="inline-flex min-h-[44px] items-center rounded-md border border-neutral-800 bg-[#10151c] px-2 text-xs font-semibold text-cyan-300 hover:border-cyan-500/50 hover:text-cyan-200"
              >
                PR
              </a>
            )}
          </div>
        </ContextBlock>
      )}
      {context.links.length > 0 && (
        <DetailRow label="Links">
          <div className="flex min-w-0 flex-wrap gap-1.5">
            {context.links.map(link => {
              const targetLabel = linkTargetLabel(link.target);
              const taskId = resolveTaskTargetId(link.target);
              const canOpenTask = Boolean(taskId && onNavigateToTask);
              const externalUrl = externalTargetUrl(link.target);
              const className = 'inline-flex max-w-full min-w-0 flex-col rounded-md border border-neutral-800 bg-neutral-950/60 px-2 py-1 text-left';
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
        </DetailRow>
      )}
      {hasMessages && (
        <ContextBlock>
          <ContextLabel>Messages</ContextLabel>
          <div className="mt-1.5 min-w-0 space-y-1.5">
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
        </ContextBlock>
      )}
    </>
  );
}
