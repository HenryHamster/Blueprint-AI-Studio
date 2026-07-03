import type { StrategicStateAttentionItem } from '../strategicStateApi';

export function pipelineTaskIdForAttentionItem(item: StrategicStateAttentionItem): string | null {
  if (item.kind !== 'task') return null;
  return item.target?.task_id || item.task_id || item.ref.id || null;
}
