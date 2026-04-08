import React, { useState, useMemo, useRef, useEffect } from 'react';
import type { Task, TaskStatus } from '../types/task';
import { ChevronDown, ChevronRight } from 'lucide-react';

interface TimelineViewProps {
  tasks: Task[];
  hasActiveFilters?: boolean;
  onViewLogs: (task: Task) => void;
}

type WindowHours = 1 | 2 | 6 | 12 | 24;

const WINDOW_OPTIONS: WindowHours[] = [1, 2, 6, 12, 24];

const QUEUE_COLOR = 'bg-neutral-600';
const ACTIVE_COLOR = 'bg-amber-500';
const TERMINAL_COLORS: Partial<Record<TaskStatus, string>> = {
  COMPLETED: 'bg-emerald-500',
  FAILED: 'bg-red-500',
  CANCELLED: 'bg-neutral-500',
};

const TERMINAL_STATUSES: TaskStatus[] = ['COMPLETED', 'FAILED', 'CANCELLED'];
const MAX_VISIBLE_ROWS = 100;
const ROW_HEIGHT = 36;
const SIDEBAR_WIDTH = 200;

function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${rs}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${rm}m`;
}

function getTaskEnd(task: Task, now: number): number {
  if (TERMINAL_STATUSES.includes(task.status)) {
    return task.last_heartbeat ? new Date(task.last_heartbeat).getTime() : new Date(task.created_at).getTime();
  }
  if (task.last_heartbeat) return Math.max(new Date(task.last_heartbeat).getTime(), now);
  return now;
}

interface TaskGroup {
  parent: Task | null;
  children: Task[];
}

export function TimelineView({ tasks, hasActiveFilters, onViewLogs }: TimelineViewProps) {
  const [windowHours, setWindowHours] = useState<WindowHours>(6);
  const [collapsedGroups, setCollapsedGroups] = useState<Set<string>>(new Set());
  const [hoveredTask, setHoveredTask] = useState<{ task: Task; x: number; y: number; containerWidth: number } | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const interval = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(interval);
  }, []);

  const windowMs = windowHours * 60 * 60 * 1000;
  const windowStart = now - windowMs;
  const windowEnd = now;

  // Filter tasks to those within (or overlapping) the visible window
  const visibleTasks = useMemo(() => {
    return tasks.filter((task) => {
      const taskStart = new Date(task.created_at).getTime();
      const taskEnd = getTaskEnd(task, now);
      return taskEnd >= windowStart && taskStart <= windowEnd;
    });
  }, [tasks, windowStart, windowEnd, now]);

  // Build groups: top-level tasks and their children
  const groups = useMemo((): TaskGroup[] => {
    const inList = new Set(visibleTasks.map((t) => t.id));

    // Find top-level tasks: no parent, or parent not in current visible list
    const topLevel = visibleTasks
      .filter((t) => !t.parent_task_id || !inList.has(t.parent_task_id))
      .sort((a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime());

    const childMap = new Map<string, Task[]>();
    for (const t of visibleTasks) {
      if (t.parent_task_id && inList.has(t.parent_task_id)) {
        const arr = childMap.get(t.parent_task_id) ?? [];
        arr.push(t);
        childMap.set(t.parent_task_id, arr);
      }
    }

    return topLevel.map((parent) => ({
      parent,
      children: (childMap.get(parent.id) ?? []).sort(
        (a, b) => new Date(a.created_at).getTime() - new Date(b.created_at).getTime()
      ),
    }));
  }, [visibleTasks]);

  // Flatten rows for rendering, respecting collapse state, and precompute bar segments
  const rows = useMemo(() => {
    function computeBarSegments(task: Task): Array<{ left: number; width: number; color: string; key: string }> {
      const createdMs = new Date(task.created_at).getTime();
      const startedMs = task.started_at ? new Date(task.started_at).getTime() : null;
      const taskEndMs = getTaskEnd(task, now);

      const segments: Array<{ left: number; width: number; color: string; key: string }> = [];

      function pct(ms: number) {
        return ((ms - windowStart) / windowMs) * 100;
      }
      function clamp(v: number) {
        return Math.max(0, Math.min(100, v));
      }

      if (startedMs) {
        // Queue segment
        const qLeft = clamp(pct(createdMs));
        const qRight = clamp(pct(startedMs));
        if (qRight > qLeft) {
          segments.push({ left: qLeft, width: qRight - qLeft, color: QUEUE_COLOR, key: 'queue' });
        }
        // Active / terminal segment
        const aLeft = clamp(pct(startedMs));
        const aRight = clamp(pct(taskEndMs));
        if (aRight > aLeft) {
          const termColor = TERMINAL_COLORS[task.status];
          const color = termColor ?? ACTIVE_COLOR;
          segments.push({ left: aLeft, width: aRight - aLeft, color, key: 'active' });
        }
      } else {
        // Only queue segment
        const qLeft = clamp(pct(createdMs));
        const qRight = clamp(pct(taskEndMs));
        if (qRight > qLeft) {
          segments.push({ left: qLeft, width: qRight - qLeft, color: QUEUE_COLOR, key: 'queue-only' });
        }
      }

      return segments;
    }

    const result: Array<{ task: Task; depth: number; segments: Array<{ left: number; width: number; color: string; key: string }> }> = [];
    for (const group of groups) {
      if (!group.parent) continue;
      result.push({ task: group.parent, depth: 0, segments: computeBarSegments(group.parent) });
      if (!collapsedGroups.has(group.parent.id)) {
        for (const child of group.children) {
          result.push({ task: child, depth: 1, segments: computeBarSegments(child) });
        }
      }
    }
    return result;
  }, [groups, collapsedGroups, now, windowStart, windowMs]);

  const visibleRows = rows.slice(0, MAX_VISIBLE_ROWS);
  const hiddenCount = rows.length - visibleRows.length;

  // Compute tick marks for the time ruler
  const ticks = useMemo(() => {
    const result: Array<{ pct: number; label: string }> = [];
    // Choose tick interval based on window
    const tickIntervalMs =
      windowHours <= 1 ? 10 * 60 * 1000 :
      windowHours <= 2 ? 20 * 60 * 1000 :
      windowHours <= 6 ? 60 * 60 * 1000 :
      windowHours <= 12 ? 2 * 60 * 60 * 1000 :
      4 * 60 * 60 * 1000;

    // Align ticks to round intervals
    const firstTick = Math.ceil(windowStart / tickIntervalMs) * tickIntervalMs;
    for (let t = firstTick; t <= windowEnd; t += tickIntervalMs) {
      const pct = ((t - windowStart) / windowMs) * 100;
      const d = new Date(t);
      const hh = d.getHours().toString().padStart(2, '0');
      const mm = d.getMinutes().toString().padStart(2, '0');
      const ss = d.getSeconds().toString().padStart(2, '0');
      const label = windowHours <= 2 ? `${hh}:${mm}:${ss}` : `${hh}:${mm}`;
      result.push({ pct, label });
    }
    return result;
  }, [windowStart, windowEnd, windowMs, windowHours]);

  function toggleGroup(parentId: string) {
    setCollapsedGroups((prev) => {
      const next = new Set(prev);
      if (next.has(parentId)) next.delete(parentId);
      else next.add(parentId);
      return next;
    });
  }

  function handleBarMouseEnter(e: React.MouseEvent, task: Task) {
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const containerRect = containerRef.current?.getBoundingClientRect();
    if (!containerRect) return;
    setHoveredTask({
      task,
      x: rect.left - containerRect.left + rect.width / 2,
      y: rect.top - containerRect.top,
      containerWidth: containerRef.current?.clientWidth ?? 400,
    });
  }

  function handleBarMouseLeave() {
    setHoveredTask(null);
  }

  // Duration for tooltip
  function getTaskDuration(task: Task): string {
    const start = task.started_at ? new Date(task.started_at).getTime() : new Date(task.created_at).getTime();
    const end = getTaskEnd(task, now);
    return formatDuration(end - start);
  }

  const totalHeight = visibleRows.length * ROW_HEIGHT;

  return (
    <div className="flex flex-col h-full bg-neutral-900 text-neutral-300">
      {/* Time window selector */}
      <div className="flex items-center gap-2 px-4 py-2 border-b border-neutral-800">
        <span className="text-xs text-neutral-500 mr-1">Window:</span>
        {WINDOW_OPTIONS.map((w) => (
          <button
            key={w}
            onClick={() => setWindowHours(w)}
            className={`px-3 py-1 rounded-full text-xs font-medium border transition-colors ${
              windowHours === w
                ? 'bg-neutral-700 border-neutral-600 text-neutral-100'
                : 'bg-neutral-900/50 border-neutral-800/50 text-neutral-400 hover:border-neutral-700 hover:text-neutral-300'
            }`}
          >
            {w}h
          </button>
        ))}
        {hasActiveFilters && (
          <span className="ml-2 text-xs text-amber-400">Filters active</span>
        )}
      </div>

      {/* Empty state */}
      {visibleTasks.length === 0 && (
        <div className="flex-1 flex items-center justify-center text-neutral-500 text-sm">
          No tasks in the selected time window
        </div>
      )}

      {visibleTasks.length > 0 && (
        <div className="relative flex-1 overflow-auto" ref={containerRef}>
          <div className="flex min-w-0">
            {/* Left sidebar */}
            <div
              className="flex-shrink-0 border-r border-neutral-800 bg-neutral-900 z-10"
              style={{ width: SIDEBAR_WIDTH }}
            >
              {/* Sidebar header */}
              <div className="h-8 border-b border-neutral-800 flex items-center px-2">
                <span className="text-xs text-neutral-500 font-medium">Task</span>
              </div>
              {/* Sidebar rows */}
              {visibleRows.map(({ task, depth }) => {
                const group = groups.find((g) => g.parent?.id === task.id);
                const hasChildren = group && group.children.length > 0;
                const isCollapsed = collapsedGroups.has(task.id);
                return (
                  <div
                    key={task.id}
                    className="flex items-center gap-1 border-b border-neutral-800/50 px-2 text-xs text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800/30 cursor-pointer"
                    style={{ height: ROW_HEIGHT, paddingLeft: depth * 16 + 8 }}
                    onClick={() => onViewLogs(task)}
                  >
                    {depth === 0 && hasChildren && (
                      <button
                        className="flex-shrink-0 text-neutral-500 hover:text-neutral-300"
                        onClick={(e) => { e.stopPropagation(); toggleGroup(task.id); }}
                      >
                        {isCollapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
                      </button>
                    )}
                    {(depth > 0 || !hasChildren) && <span className="w-3 flex-shrink-0" />}
                    <span className="truncate" title={task.title}>
                      {task.task_type ? (
                        <span className="text-neutral-500 mr-1">[{task.task_type}]</span>
                      ) : null}
                      {task.title}
                    </span>
                  </div>
                );
              })}
              {hiddenCount > 0 && (
                <div className="px-3 py-2 text-xs text-neutral-500">
                  +{hiddenCount} more tasks outside view
                </div>
              )}
            </div>

            {/* Timeline area */}
            <div className="flex-1 overflow-x-auto">
              {/* Time ruler */}
              <div className="h-8 border-b border-neutral-800 relative bg-neutral-900/80">
                {ticks.map((tick) => (
                  <div
                    key={tick.pct}
                    className="absolute top-0 h-full flex flex-col items-center"
                    style={{ left: `${tick.pct}%` }}
                  >
                    <div className="w-px h-2 bg-neutral-700 mt-1" />
                    <span className="text-xs text-neutral-500 mt-0.5 whitespace-nowrap" style={{ transform: 'translateX(-50%)' }}>
                      {tick.label}
                    </span>
                  </div>
                ))}
                {/* "now" marker */}
                <div
                  className="absolute top-0 h-full w-px bg-amber-500/50"
                  style={{ left: '100%' }}
                />
              </div>

              {/* Task rows */}
              <div className="relative" style={{ height: totalHeight }}>
                {/* Tick grid lines */}
                {ticks.map((tick) => (
                  <div
                    key={tick.pct}
                    className="absolute top-0 bottom-0 w-px bg-neutral-800/60"
                    style={{ left: `${tick.pct}%` }}
                  />
                ))}
                {/* "now" line */}
                <div
                  className="absolute top-0 bottom-0 w-px bg-amber-500/30"
                  style={{ left: '100%' }}
                />

                {visibleRows.map(({ task, segments }, idx) => {
                  return (
                    <div
                      key={task.id}
                      className="absolute left-0 right-0 border-b border-neutral-800/30"
                      style={{ top: idx * ROW_HEIGHT, height: ROW_HEIGHT }}
                    >
                      {segments.map((seg) => (
                        <div
                          key={seg.key}
                          className={`absolute top-1/2 -translate-y-1/2 rounded-sm cursor-pointer opacity-90 hover:opacity-100 transition-opacity ${seg.color}`}
                          style={{
                            left: `${seg.left}%`,
                            width: `${Math.max(seg.width, 0.3)}%`,
                            height: 20,
                          }}
                          onMouseEnter={(e) => handleBarMouseEnter(e, task)}
                          onMouseLeave={handleBarMouseLeave}
                          onClick={() => onViewLogs(task)}
                        />
                      ))}
                    </div>
                  );
                })}
              </div>
            </div>
          </div>

          {/* Hover tooltip */}
          {hoveredTask && (
            <div
              className="absolute z-50 pointer-events-none bg-neutral-800 border border-neutral-700 rounded-md p-2 shadow-lg text-xs max-w-xs"
              style={{
                left: Math.min(hoveredTask.x, hoveredTask.containerWidth - 200),
                top: Math.max(hoveredTask.y - 90, 0),
              }}
            >
              <div className="font-medium text-neutral-200 truncate mb-1">{hoveredTask.task.title}</div>
              <div className="flex gap-2 text-neutral-400">
                <span>{hoveredTask.task.status}</span>
                {hoveredTask.task.task_type && <span>· {hoveredTask.task.task_type}</span>}
              </div>
              <div className="text-neutral-400 mt-0.5">Duration: {getTaskDuration(hoveredTask.task)}</div>
              {hoveredTask.task.latest_status_message && (
                <div className="text-neutral-500 mt-1 truncate">{hoveredTask.task.latest_status_message}</div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
