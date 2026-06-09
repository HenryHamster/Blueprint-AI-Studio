import type { Task } from '../types/task';
import { parseTimestamp } from '../utils/time';
import { getSidecarBackref } from '../utils/sidecarTasks';

export interface TreeNode {
  task: Task;
  children: TreeNode[];
  /** Side-car tasks (parent_task_id=null by design) nested under this primary
   *  via their context back-reference. Rendered collapsed by default. */
  sidecars: TreeNode[];
  isSyntheticParent?: boolean;
  isOrphan?: boolean;
  /** This node is a side-car nested under its primary via context back-reference. */
  isSidecar?: boolean;
  /** Side-car whose primary task is not on this page/filter — rendered as a
   *  top-level row (fallback) rather than hidden. */
  isSidecarFallback?: boolean;
}

export function buildForest(tasks: Task[]): TreeNode[] {
  const taskMap = new Map<string, Task>();
  for (const t of tasks) taskMap.set(t.id, t);

  const nodeMap = new Map<string, TreeNode>();
  for (const t of tasks) nodeMap.set(t.id, { task: t, children: [], sidecars: [] });

  const roots: TreeNode[] = [];
  // Group orphans (tasks whose parent is not on this page) by parent_task_id
  const orphanGroups = new Map<string, TreeNode[]>();

  for (const t of tasks) {
    const node = nodeMap.get(t.id);
    if (!node) continue;
    const parent = t.parent_task_id ? nodeMap.get(t.parent_task_id) : undefined;
    if (parent) {
      parent.children.push(node);
    } else if (t.parent_task_id) {
      // Orphan: parent exists but is not on this page
      node.isOrphan = true;
      const group = orphanGroups.get(t.parent_task_id) || [];
      group.push(node);
      orphanGroups.set(t.parent_task_id, group);
    } else {
      // Side-cars (graders/challengers) carry parent_task_id=null by design;
      // their link to the task they grade/challenge lives in a context
      // back-reference. Nest them under that primary instead of rendering
      // them as top-level siblings.
      const backref = getSidecarBackref(t);
      const primary = backref && backref.primaryTaskId !== t.id
        ? nodeMap.get(backref.primaryTaskId)
        : undefined;
      // Guard against a (data-corruption) direct mutual reference, which
      // would otherwise create a render cycle.
      const mutual = primary && getSidecarBackref(primary.task)?.primaryTaskId === t.id;
      if (primary && !mutual) {
        node.isSidecar = true;
        primary.sidecars.push(node);
      } else if (backref) {
        // Primary not on this page/filter — fall back to a top-level row
        // rather than hiding the side-car.
        node.isSidecarFallback = true;
        roots.push(node);
      } else {
        roots.push(node);
      }
    }
  }

  // Create synthetic parent nodes for orphan groups
  const syntheticRoots: TreeNode[] = [];
  for (const [parentId, orphans] of orphanGroups) {
    const shortId = parentId.slice(0, 8);
    const syntheticParent: TreeNode = {
      task: {
        id: `synthetic-${parentId}`,
        title: `Task group (parent: ${shortId})`,
        description: '',
        status: 'QUEUED',
        created_by: '',
        requires_approval: false,
        parent_task_id: parentId,
        created_at: orphans[0].task.created_at,
        history: [],
      } as Task,
      children: orphans,
      sidecars: [],
      isSyntheticParent: true,
    };
    syntheticRoots.push(syntheticParent);
  }

  // Sort children by pipeline order: research → plan → create → review → fix
  const typeOrder: Record<string, number> = { research: 0, plan: 1, create: 2, review: 3, fix: 4 };
  const sortChildren = (node: TreeNode) => {
    node.children.sort((a, b) => (typeOrder[a.task.task_type || 'create'] ?? 2) - (typeOrder[b.task.task_type || 'create'] ?? 2));
    node.children.forEach(sortChildren);
    node.sidecars.sort((a, b) => parseTimestamp(a.task.created_at).getTime() - parseTimestamp(b.task.created_at).getTime());
    node.sidecars.forEach(sortChildren);
  };
  roots.forEach(sortChildren);
  syntheticRoots.forEach(sortChildren);

  // Sort roots by creation time (newest first)
  roots.sort((a, b) => parseTimestamp(b.task.created_at).getTime() - parseTimestamp(a.task.created_at).getTime());
  syntheticRoots.sort((a, b) => parseTimestamp(b.task.created_at).getTime() - parseTimestamp(a.task.created_at).getTime());

  // Synthetic groups appear first so users see incomplete groups at the top
  return [...syntheticRoots, ...roots];
}
