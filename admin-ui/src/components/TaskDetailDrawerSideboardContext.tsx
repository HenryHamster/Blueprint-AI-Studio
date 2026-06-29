import type { Task } from '../types/task';
import { DetailRow, SectionHeader } from './TaskDetailDrawerPrimitives';

function artifactSummary(byType: Record<string, number>): string {
  const entries = Object.entries(byType);
  if (entries.length === 0) return 'none';
  return entries
    .map(([kind, count]) => `${kind.toLowerCase().replace(/_/g, ' ')} ${count}`)
    .join(' · ');
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

  return (
    <>
      <SectionHeader>Context</SectionHeader>
      <DetailRow label="Project">
        <div className="min-w-0">
          <span className="block truncate text-sm text-neutral-200">
            {context.project.repo || context.project.name || context.project.id}
          </span>
          {context.project.repo && context.project.name && (
            <span className="block truncate text-xs text-neutral-500">{context.project.name}</span>
          )}
        </div>
      </DetailRow>
      {context.purpose && (
        <DetailRow label="Purpose">
          <span className="text-sm text-neutral-200 break-words">{context.purpose}</span>
        </DetailRow>
      )}
      {direction && (
        <DetailRow label="Direction">
          <div className="min-w-0">
            {direction.text && (
              <span className="block break-words text-sm text-neutral-200">{direction.text}</span>
            )}
            <span className="font-mono text-xs text-neutral-500">{direction.id}</span>
          </div>
        </DetailRow>
      )}
      {cycle && (
        <DetailRow label="Strategic cycle">
          <span className="inline-flex min-w-0 flex-wrap gap-x-2 gap-y-1 text-sm">
            <span className="font-mono text-neutral-300">{cycle.id}</span>
            {cycle.phase && <span className="text-neutral-500">{cycle.phase.replace(/_/g, ' ')}</span>}
            <span className="text-neutral-500">{cycle.outcome}</span>
          </span>
        </DetailRow>
      )}
      {(parentTaskId || context.pipeline.child_count > 0 || context.pipeline.dependency_count > 0) && (
        <DetailRow label="Pipeline">
          <div className="flex min-w-0 flex-wrap items-center gap-2 text-sm text-neutral-300">
            {parentTaskId && onNavigateToTask ? (
              <button
                type="button"
                onClick={() => onNavigateToTask(parentTaskId)}
                data-viewport-allow-small-target
                className="font-mono text-cyan-300 hover:text-cyan-200"
              >
                parent {parentTaskId.slice(0, 8)}
              </button>
            ) : parentTaskId ? (
              <span className="font-mono">parent {parentTaskId.slice(0, 8)}</span>
            ) : null}
            {context.pipeline.dependency_count > 0 && <span>{context.pipeline.dependency_count} dependencies</span>}
            {context.pipeline.child_count > 0 && <span>{context.pipeline.child_count} children</span>}
          </div>
        </DetailRow>
      )}
      {context.pipeline.blocked_reason && (
        <DetailRow label="Blocked">
          <span className="text-sm text-amber-300 break-words">{context.pipeline.blocked_reason}</span>
        </DetailRow>
      )}
      {context.artifacts.total > 0 && (
        <DetailRow label="Artifacts">
          <div className="min-w-0 text-sm">
            <span className="block text-neutral-300">{artifactSummary(context.artifacts.by_type)}</span>
            <span className="mt-0.5 flex min-w-0 flex-wrap gap-x-2 gap-y-1 text-xs text-neutral-500">
              {context.artifacts.primary_pr_url && (
                <a
                  href={context.artifacts.primary_pr_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-viewport-allow-small-target
                  className="text-cyan-300 hover:text-cyan-200"
                >
                  pull request
                </a>
              )}
              {context.artifacts.primary_commit && (
                <span className="font-mono">{context.artifacts.primary_commit.slice(0, 12)}</span>
              )}
            </span>
          </div>
        </DetailRow>
      )}
      {recentMessages.length > 0 && (
        <DetailRow label="Messages">
          <div className="min-w-0 space-y-1">
            {recentMessages.map(message => (
              <p key={message.id} className="line-clamp-2 text-sm text-neutral-300">
                {message.content}
              </p>
            ))}
          </div>
        </DetailRow>
      )}
    </>
  );
}
