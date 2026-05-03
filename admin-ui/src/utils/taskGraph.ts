import type { Task } from '../components/TaskCard';

export function hasTransitiveCycle(startId: string, targetId: string, tasks: Task[], maxDepth = 10): boolean {
  // BFS from targetId through depends_on edges, looking for startId
  // maxDepth prevents infinite loops on corrupt data
  const visited = new Set<string>();
  const queue: Array<{ id: string; depth: number }> = [{ id: targetId, depth: 0 }];
  while (queue.length > 0) {
    const next = queue.shift();
    if (!next) break;
    const { id, depth } = next;
    if (depth >= maxDepth) continue;
    if (visited.has(id)) continue;
    visited.add(id);
    const task = tasks.find(t => t.id === id);
    if (!task?.depends_on) continue;
    for (const depId of task.depends_on) {
      if (depId === startId) return true;
      queue.push({ id: depId, depth: depth + 1 });
    }
  }
  return false;
}
