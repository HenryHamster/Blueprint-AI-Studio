// Canonical mapper from task execution status to shared styling tokens.
// Centralizes colors that were previously hand-rolled in PipelineView (STATUS_COLORS),
// TaskCard (StatusBadge configs), and TaskDetailDrawerPrimitives (DEP_STATUS_COLORS).

import { statusBadgeClasses, type StatusTone } from './projectStatus';

/** Map a task execution status to a semantic tone shared with the project/verdict badge system. */
export function taskStatusTone(status: string): StatusTone {
  switch (status) {
    case 'COMPLETED':   return 'success';
    case 'FAILED':      return 'blocking';
    case 'IN_PROGRESS': return 'warning';
    case 'BLOCKED':     return 'warning';
    case 'PROVISIONING':
    case 'EVALUATING':  return 'info';
    case 'QUEUED':
    case 'CANCELLED':
    case 'PARKED':
    default:            return 'neutral';
  }
}

/**
 * Concrete `bg-*` Tailwind class for status indicator dots and progress-bar
 * segments. Preserves the same palette as the original STATUS_COLORS in PipelineView
 * so small colored dots stay pixel-identical to their prior appearance.
 */
export function taskStatusDotColor(status: string): string {
  switch (status) {
    case 'COMPLETED':    return 'bg-emerald-500';
    case 'IN_PROGRESS':  return 'bg-yellow-500';
    case 'PROVISIONING': return 'bg-sky-700/50';
    case 'EVALUATING':   return 'bg-indigo-600/50';
    case 'FAILED':       return 'bg-red-500';
    case 'CANCELLED':    return 'bg-workbench-card';
    case 'BLOCKED':      return 'bg-amber-500';
    case 'PARKED':       return 'bg-purple-600/50';
    case 'QUEUED':
    default:             return 'bg-workbench-control';
  }
}

/** Token-backed badge class string for a task status, via the shared tone mapper. */
export function taskStatusBadgeClasses(status: string): string {
  return statusBadgeClasses(taskStatusTone(status));
}

/**
 * `text-*` class for status text in dependency lists and similar inline contexts.
 * Preserves the same palette as the original DEP_STATUS_COLORS in TaskDetailDrawerPrimitives.
 */
export function taskStatusTextColor(status: string): string {
  switch (status) {
    case 'COMPLETED':    return 'text-emerald-400';
    case 'IN_PROGRESS':  return 'text-amber-400';
    case 'FAILED':       return 'text-red-400';
    case 'EVALUATING':
    case 'PROVISIONING': return 'text-blue-400';
    case 'BLOCKED':      return 'text-amber-400';
    case 'PARKED':       return 'text-purple-400';
    case 'CANCELLED':    return 'text-neutral-500';
    case 'QUEUED':
    default:             return 'text-neutral-400';
  }
}
