import { useMemo, useState, useRef, useCallback, useEffect } from 'react';
import { Archive, ArrowDownUp, GripVertical, ChevronLeft, ChevronRight, Inbox, Search } from 'lucide-react';
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  DragOverlay,
} from '@dnd-kit/core';
import type { DragEndEvent, DragStartEvent } from '@dnd-kit/core';
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
  arrayMove,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { TaskCard, TASK_TYPE_STYLES } from './TaskCard';
import type { Task, TaskStatus } from './TaskCard';
import type { Project } from './ProjectDashboard';

export type SortBy = 'newest' | 'oldest' | 'type' | 'title';

const COLLAPSED_COLUMNS_KEY = 'kanban_collapsed_columns';

function loadCollapsedColumns(): Set<string> {
  try {
    const stored = localStorage.getItem(COLLAPSED_COLUMNS_KEY);
    return stored ? new Set(JSON.parse(stored)) : new Set();
  } catch {
    return new Set();
  }
}

export interface KanbanColumn {
  id: TaskStatus;
  label: string;
  includes: TaskStatus[];
}

const ARCHIVABLE_COLUMNS: TaskStatus[] = ['COMPLETED', 'CANCELLED', 'FAILED'];
const STORAGE_KEY = 'kanban_card_order';

const SORT_LABELS: Record<SortBy, string> = {
  newest: 'Newest first',
  oldest: 'Oldest first',
  type: 'Type',
  title: 'Title',
};

function sortTasks(tasks: Task[], sortBy: SortBy): Task[] {
  return [...tasks].sort((a, b) => {
    switch (sortBy) {
      case 'newest':
        return new Date(b.created_at).getTime() - new Date(a.created_at).getTime();
      case 'oldest':
        return new Date(a.created_at).getTime() - new Date(b.created_at).getTime();
      case 'type':
        return (a.task_type ?? '').localeCompare(b.task_type ?? '');
      case 'title':
        return (a.title ?? '').localeCompare(b.title ?? '');
    }
  });
}

interface KanbanBoardProps {
  tasks: Task[];
  columns: KanbanColumn[];
  loading: boolean;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  compact?: boolean;
  sortBy: SortBy;
  onToggleTaskSelection: (id: string) => void;
  onBulkToggle: (taskIds: string[], selected: boolean) => void;
  onArchiveColumn: (taskIds: string[], columnLabel: string) => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  groupBy?: 'status' | 'type' | 'project';
  projects?: Project[];
  hasActiveFilters?: boolean;
}

function TaskItem({ task, selectionMode, selectedTaskIds, onToggleTaskSelection, maxRetries, actionLoadingMap, compact, onApprove, onRequestChanges, onCancel, onRetry, onEditPrompt, onDismiss, onArchive, onViewLogs, colIdx, cardIdx, cardRef, onNavigate }: {
  task: Task;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  onToggleTaskSelection: (id: string) => void;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  compact?: boolean;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  colIdx?: number;
  cardIdx?: number;
  cardRef?: (el: HTMLDivElement | null) => void;
  onNavigate?: (colIdx: number, cardIdx: number, direction: 'up' | 'down' | 'left' | 'right') => void;
}) {
  return (
    <div className="relative">
      {selectionMode && (
        <div className="absolute top-2 left-2 z-10">
          <input
            type="checkbox"
            checked={selectedTaskIds.has(task.id)}
            onChange={() => onToggleTaskSelection(task.id)}
            className="w-4 h-4 accent-amber-400 cursor-pointer"
          />
        </div>
      )}
      <div className={selectionMode ? 'pl-6' : ''}>
        <TaskCard
          ref={cardRef}
          task={task}
          maxRetries={maxRetries}
          actionLoading={actionLoadingMap[task.id]}
          compact={compact}
          selectionMode={selectionMode}
          onToggleTaskSelection={onToggleTaskSelection}
          colIdx={colIdx}
          cardIdx={cardIdx}
          onNavigate={onNavigate}
          onApprove={onApprove}
          onRequestChanges={onRequestChanges}
          onCancel={onCancel}
          onRetry={onRetry}
          onEditPrompt={onEditPrompt}
          onDismiss={onDismiss}
          onArchive={onArchive}
          onViewLogs={onViewLogs}
        />
      </div>
    </div>
  );
}

interface SortableCardProps {
  task: Task;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  onToggleTaskSelection: (id: string) => void;
  compact?: boolean;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  isOverlay?: boolean;
  colIdx: number;
  cardIdx: number;
  cardRef: (el: HTMLDivElement | null) => void;
  onNavigate: (colIdx: number, cardIdx: number, direction: 'up' | 'down' | 'left' | 'right') => void;
}

function SortableCard({
  task,
  maxRetries,
  actionLoadingMap,
  selectionMode,
  selectedTaskIds,
  onToggleTaskSelection,
  compact,
  onApprove,
  onRequestChanges,
  onCancel,
  onRetry,
  onEditPrompt,
  onDismiss,
  onArchive,
  onViewLogs,
  isOverlay = false,
  colIdx,
  cardIdx,
  cardRef,
  onNavigate,
}: SortableCardProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: task.id });

  const style = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.4 : 1,
  };

  return (
    <div
      ref={setNodeRef}
      style={style}
      className={`relative group ${isOverlay ? 'shadow-2xl ring-1 ring-amber-500/50 rounded-xl' : ''}`}
    >
      {selectionMode && (
        <div className="absolute top-2 left-2 z-10">
          <input
            type="checkbox"
            checked={selectedTaskIds.has(task.id)}
            onChange={() => onToggleTaskSelection(task.id)}
            className="w-4 h-4 accent-amber-400 cursor-pointer"
          />
        </div>
      )}
      <div
        className="absolute top-2 right-2 z-10 opacity-0 group-hover:opacity-100 transition-opacity cursor-grab active:cursor-grabbing touch-none"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="w-4 h-4 text-neutral-500 hover:text-neutral-300" />
      </div>
      <div className={selectionMode ? 'pl-6' : ''}>
        <TaskCard
          ref={cardRef}
          task={task}
          maxRetries={maxRetries}
          actionLoading={actionLoadingMap[task.id]}
          compact={compact}
          selectionMode={selectionMode}
          onToggleTaskSelection={onToggleTaskSelection}
          colIdx={colIdx}
          cardIdx={cardIdx}
          onNavigate={onNavigate}
          onApprove={onApprove}
          onRequestChanges={onRequestChanges}
          onCancel={onCancel}
          onRetry={onRetry}
          onEditPrompt={onEditPrompt}
          onDismiss={onDismiss}
          onArchive={onArchive}
          onViewLogs={onViewLogs}
        />
      </div>
    </div>
  );
}

function ColumnSelectCheckbox({ columnTasks, selectedTaskIds, onBulkToggle }: {
  columnTasks: Task[];
  selectedTaskIds: Set<string>;
  onBulkToggle: (taskIds: string[], selected: boolean) => void;
}) {
  const checkboxRef = useRef<HTMLInputElement>(null);
  const ids = columnTasks.map(t => t.id);
  const selectedCount = ids.filter(id => selectedTaskIds.has(id)).length;
  const allSelected = columnTasks.length > 0 && selectedCount === columnTasks.length;
  const someSelected = selectedCount > 0 && selectedCount < columnTasks.length;

  useEffect(() => {
    if (checkboxRef.current) {
      checkboxRef.current.indeterminate = someSelected;
    }
  }, [someSelected]);

  return (
    <input
      ref={checkboxRef}
      type="checkbox"
      checked={allSelected}
      onChange={() => onBulkToggle(ids, !allSelected)}
      className="w-4 h-4 accent-amber-400 cursor-pointer"
      title={allSelected ? 'Deselect all in column' : 'Select all in column'}
    />
  );
}

function loadColumnOrder(): Record<string, string[]> {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveColumnOrder(order: Record<string, string[]>) {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(order));
  } catch {
    // ignore storage errors
  }
}

export function KanbanBoard({
  tasks,
  columns,
  loading,
  maxRetries,
  actionLoadingMap,
  selectionMode,
  selectedTaskIds,
  compact,
  sortBy,
  onToggleTaskSelection,
  onBulkToggle,
  onArchiveColumn,
  onApprove,
  onRequestChanges,
  onCancel,
  onRetry,
  onEditPrompt,
  onDismiss,
  onArchive,
  onViewLogs,
  groupBy = 'status',
  projects = [],
  hasActiveFilters = false,
}: KanbanBoardProps) {
  const [columnOrder, setColumnOrder] = useState<Record<string, string[]>>(loadColumnOrder);
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [collapsedColumns, setCollapsedColumns] = useState<Set<string>>(loadCollapsedColumns);

  // Persist order changes
  useEffect(() => {
    saveColumnOrder(columnOrder);
  }, [columnOrder]);

  // Clear manual drag order when sort changes so the new sort takes effect immediately
  const prevSortByRef = useRef(sortBy);
  useEffect(() => {
    if (prevSortByRef.current !== sortBy) {
      prevSortByRef.current = sortBy;
      setColumnOrder({});
    }
  }, [sortBy]);

  useEffect(() => {
    localStorage.setItem(COLLAPSED_COLUMNS_KEY, JSON.stringify([...collapsedColumns]));
  }, [collapsedColumns]);

  const toggleCollapse = (colId: string) => {
    setCollapsedColumns(prev => {
      const next = new Set(prev);
      if (next.has(colId)) next.delete(colId);
      else next.add(colId);
      return next;
    });
  };
  const tasksByColumn = useMemo(() => {
    const map: Record<string, Task[]> = {};
    for (const col of columns) {
      const colTasks = tasks.filter(t => col.includes.includes(t.status));
      const sortedTasks = sortTasks(colTasks, sortBy);
      const savedOrder = columnOrder[col.id] ?? [];

      if (savedOrder.length === 0) {
        map[col.id] = sortedTasks;
      } else {
        // Build ordered list: saved (manual drag) order first, then new tasks appended in sort order
        const taskMap = new Map(sortedTasks.map(t => [t.id, t]));
        const ordered: Task[] = [];
        for (const id of savedOrder) {
          if (taskMap.has(id)) {
            ordered.push(taskMap.get(id)!);
            taskMap.delete(id);
          }
        }
        // Append remaining tasks not in saved order (in sort order)
        for (const t of taskMap.values()) {
          ordered.push(t);
        }
        map[col.id] = ordered;
      }
    }
    return map;
  }, [tasks, columns, sortBy, columnOrder]);

  // Map from task id to column id for quick lookup during drag
  const taskColumnMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (const col of columns) {
      for (const task of tasksByColumn[col.id] ?? []) {
        map[task.id] = col.id;
      }
    }
    return map;
  }, [columns, tasksByColumn]);

  const sensors = useSensors(
    useSensor(PointerSensor, {
      activationConstraint: { distance: 5 },
    })
  );

  const handleDragStart = useCallback((event: DragStartEvent) => {
    const task = tasks.find(t => t.id === event.active.id);
    setActiveTask(task ?? null);
  }, [tasks]);

  const handleDragEnd = useCallback((event: DragEndEvent) => {
    setActiveTask(null);
    const { active, over } = event;
    if (!over || active.id === over.id) return;

    const activeColId = taskColumnMap[active.id as string];
    const overColId = taskColumnMap[over.id as string];

    // Only reorder within the same column
    if (!activeColId || !overColId || activeColId !== overColId) return;

    const colTasks = tasksByColumn[activeColId];
    const ids = colTasks.map(t => t.id);
    const oldIndex = ids.indexOf(active.id as string);
    const newIndex = ids.indexOf(over.id as string);
    if (oldIndex === -1 || newIndex === -1) return;

    const reordered = arrayMove(ids, oldIndex, newIndex);
    setColumnOrder(prev => ({ ...prev, [activeColId]: reordered }));
  }, [taskColumnMap, tasksByColumn]);

  const columnRefs = useRef<(HTMLDivElement | null)[][]>([]);
  const tasksByColumnRef = useRef(tasksByColumn);
  // eslint-disable-next-line react-hooks/refs
  tasksByColumnRef.current = tasksByColumn;
  const columnsRef = useRef(columns);
  // eslint-disable-next-line react-hooks/refs
  columnsRef.current = columns;
  const collapsedColumnsRef = useRef(collapsedColumns);
  // eslint-disable-next-line react-hooks/refs
  collapsedColumnsRef.current = collapsedColumns;

  const handleNavigate = useCallback((colIndex: number, cardIndex: number, direction: 'up' | 'down' | 'left' | 'right') => {
    const cols = columnsRef.current;
    const numCols = cols.length;
    let targetColIndex = colIndex;
    let targetCardIndex = cardIndex;

    if (direction === 'up' || direction === 'down') {
      const numCards = (tasksByColumnRef.current[cols[colIndex]?.id] ?? []).length;
      if (numCards === 0) return;
      targetCardIndex = direction === 'up'
        ? (cardIndex - 1 + numCards) % numCards
        : (cardIndex + 1) % numCards;
    } else {
      // Skip collapsed columns when navigating left/right
      const step = direction === 'left' ? -1 : 1;
      let next = (colIndex + step + numCols) % numCols;
      let attempts = 0;
      while (attempts < numCols - 1) {
        if (!collapsedColumnsRef.current.has(cols[next]?.id)) break;
        next = (next + step + numCols) % numCols;
        attempts++;
      }
      // All other columns are collapsed — do nothing
      if (collapsedColumnsRef.current.has(cols[next]?.id)) return;
      targetColIndex = next;
      targetCardIndex = 0;
    }

    columnRefs.current[targetColIndex]?.[targetCardIndex]?.focus();
  }, []);

  const projectMap = useMemo(() => {
    const map: Record<string, string> = {};
    for (const p of projects) {
      map[p.id] = p.name;
    }
    return map;
  }, [projects]);

  const taskProps = { selectionMode, selectedTaskIds, onToggleTaskSelection, maxRetries, actionLoadingMap, compact, onApprove, onRequestChanges, onCancel, onRetry, onEditPrompt, onDismiss, onArchive, onViewLogs };

  if (tasks.length === 0 && !loading) {
    return (
      <main className="flex-1 flex items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-center">
          {hasActiveFilters ? (
            <>
              <Search className="w-10 h-10 text-neutral-700" />
              <p className="text-neutral-400 text-sm font-medium">No tasks match your filters</p>
              <p className="text-neutral-600 text-xs">Try adjusting your search or filter settings</p>
            </>
          ) : (
            <>
              <Inbox className="w-10 h-10 text-neutral-700" />
              <p className="text-neutral-400 text-sm font-medium">No tasks yet</p>
              <p className="text-neutral-600 text-xs">Tasks will appear here once they are created</p>
            </>
          )}
        </div>
      </main>
    );
  }

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
    >
      <main className="flex justify-center gap-4 overflow-x-auto overflow-y-hidden pb-4 flex-1 custom-scrollbar">
        {/* eslint-disable-next-line react-hooks/refs */}
        {columns.map((col, colIdx) => {
          const columnTasks = tasksByColumn[col.id] ?? [];
          const isArchivable = ARCHIVABLE_COLUMNS.includes(col.id);
          if (!columnRefs.current[colIdx]) columnRefs.current[colIdx] = [];
          const isCollapsed = collapsedColumns.has(col.id);
          const taskIds = columnTasks.map(t => t.id);


          let content: React.ReactNode;

          if (groupBy === 'type') {
            const groups = new Map<string, Task[]>();
            for (const task of columnTasks) {
              const type = task.task_type || 'create';
              if (!groups.has(type)) groups.set(type, []);
              groups.get(type)!.push(task);
            }
            const sorted = [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
            const taskFlatIndex = new Map(columnTasks.map((t, i) => [t.id, i]));
            content = sorted.length === 0 && !loading ? (
              <div className="text-center text-neutral-600 text-xs py-8 border border-dashed border-neutral-800 rounded-xl bg-neutral-900/20">No tasks</div>
            ) : (
              <>
                {sorted.map(([type, typeTasks]) => {
                  const style = TASK_TYPE_STYLES[type];
                  const label = style?.label ?? type.toUpperCase();
                  return (
                    <div key={type} className="mb-3">
                      <div className={`sticky top-0 z-[5] flex items-center gap-1.5 px-2 py-1 mb-2 rounded text-[10px] font-bold uppercase tracking-wider border ${style ? `${style.bg} ${style.text}` : 'bg-neutral-800/50 border-neutral-700/50 text-neutral-400'}`}>
                        <span>{label}</span>
                        <span className="opacity-60">({typeTasks.length})</span>
                      </div>
                      <div className={compact ? 'space-y-2' : 'space-y-3'}>
                        {typeTasks.map(task => {
                          const flatIdx = taskFlatIndex.get(task.id) ?? 0;
                          return (
                            <TaskItem
                              key={task.id}
                              task={task}
                              {...taskProps}
                              colIdx={colIdx}
                              cardIdx={flatIdx}
                              cardRef={(el) => { columnRefs.current[colIdx][flatIdx] = el; }}
                              onNavigate={handleNavigate}
                            />
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </>
            );
          } else if (groupBy === 'project') {
            const groups = new Map<string, Task[]>();
            for (const task of columnTasks) {
              const key = task.project_id ?? '__none__';
              if (!groups.has(key)) groups.set(key, []);
              groups.get(key)!.push(task);
            }
            const sorted = [...groups.entries()].sort((a, b) => {
              if (a[0] === '__none__') return 1;
              if (b[0] === '__none__') return -1;
              const nameA = projectMap[a[0]] ?? a[0];
              const nameB = projectMap[b[0]] ?? b[0];
              return nameA.localeCompare(nameB);
            });
            const taskFlatIndex = new Map(columnTasks.map((t, i) => [t.id, i]));
            content = sorted.length === 0 && !loading ? (
              <div className="text-center text-neutral-600 text-xs py-8 border border-dashed border-neutral-800 rounded-xl bg-neutral-900/20">No tasks</div>
            ) : (
              <>
                {sorted.map(([projectId, projectTasks]) => {
                  const name = projectId === '__none__' ? 'No Project' : (projectMap[projectId] ?? projectId);
                  return (
                    <div key={projectId} className="mb-3">
                      <div className="sticky top-0 z-[5] flex items-center gap-1.5 px-2 py-1 mb-2 rounded text-[10px] font-bold uppercase tracking-wider bg-neutral-800/50 border border-neutral-700/50 text-neutral-400">
                        <span className="truncate max-w-[200px]" title={name}>{name}</span>
                        <span className="opacity-60 shrink-0">({projectTasks.length})</span>
                      </div>
                      <div className={compact ? 'space-y-2' : 'space-y-3'}>
                        {projectTasks.map(task => {
                          const flatIdx = taskFlatIndex.get(task.id) ?? 0;
                          return (
                            <TaskItem
                              key={task.id}
                              task={task}
                              {...taskProps}
                              colIdx={colIdx}
                              cardIdx={flatIdx}
                              cardRef={(el) => { columnRefs.current[colIdx][flatIdx] = el; }}
                              onNavigate={handleNavigate}
                            />
                          );
                        })}
                      </div>
                    </div>
                  );
                })}
              </>
            );
          } else {
            content = (
              <div className={compact ? 'space-y-2' : 'space-y-3'}>
                <SortableContext items={taskIds} strategy={verticalListSortingStrategy}>
                  {columnTasks.map((task, cardIdx) => (
                    <SortableCard
                      key={task.id}
                      task={task}
                      maxRetries={maxRetries}
                      actionLoadingMap={actionLoadingMap}
                      selectionMode={selectionMode}
                      selectedTaskIds={selectedTaskIds}
                      onToggleTaskSelection={onToggleTaskSelection}
                      compact={compact}
                      colIdx={colIdx}
                      cardIdx={cardIdx}
                      cardRef={(el) => { columnRefs.current[colIdx][cardIdx] = el; }}
                      onNavigate={handleNavigate}
                      onApprove={onApprove}
                      onRequestChanges={onRequestChanges}
                      onCancel={onCancel}
                      onRetry={onRetry}
                      onEditPrompt={onEditPrompt}
                      onDismiss={onDismiss}
                      onArchive={onArchive}
                      onViewLogs={onViewLogs}
                    />
                  ))}
                </SortableContext>
                {columnTasks.length === 0 && !loading && (
                  <div className="text-center text-neutral-600 text-xs py-8 border border-dashed border-neutral-800 rounded-xl bg-neutral-900/20">
                    No tasks
                  </div>
                )}
              </div>
            );
          }


          return (
            <div
              key={col.id}
              className={`flex-shrink-0 flex flex-col bg-neutral-900/50 rounded-xl border border-neutral-800/50 overflow-hidden transition-all duration-200 ${
                isCollapsed ? 'w-10' : 'flex-1 min-w-[300px] max-w-[400px] p-3'
              }`}
            >
              {/* Collapsed strip */}
              <div className={`flex flex-col items-center py-3 gap-2 ${isCollapsed ? '' : 'hidden'}`}>
                <button
                  onClick={() => toggleCollapse(col.id)}
                  title={`Expand ${col.label}`}
                  className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors"
                >
                  <ChevronRight className="w-3.5 h-3.5" />
                </button>
                <span className="text-[10px] bg-neutral-800 text-neutral-300 px-1.5 py-0.5 rounded-full font-medium border border-neutral-700">
                  {columnTasks.length}
                </span>
                <span
                  className={`text-[10px] font-bold uppercase tracking-widest ${col.id === 'EVALUATING' ? 'text-amber-400' : 'text-neutral-400'} flex-1 flex items-center`}
                  style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)' }}
                >
                  {col.label}
                </span>
                <span title={`Sorted: ${SORT_LABELS[sortBy]}`} className="text-neutral-600 flex items-center">
                  <ArrowDownUp className="w-3 h-3" />
                </span>
                {isArchivable && columnTasks.length > 0 && (
                  <button
                    onClick={() => onArchiveColumn(columnTasks.map(t => t.id), col.label)}
                    title={`Archive all ${col.label} tasks`}
                    className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors"
                  >
                    <Archive className="w-3.5 h-3.5" />
                  </button>
                )}
              </div>

              {/* Expanded content */}
              <div className={`flex flex-col flex-1 overflow-hidden ${isCollapsed ? 'hidden' : ''}`}>
                <div className="sticky top-0 z-10 bg-neutral-900/95 backdrop-blur-sm pb-2 -mx-1 px-1 flex items-center justify-between mb-3">
                  <div className="flex items-center gap-1">
                    <button
                      onClick={() => toggleCollapse(col.id)}
                      title={`Collapse ${col.label}`}
                      className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors"
                    >
                      <ChevronLeft className="w-3.5 h-3.5" />
                    </button>
                    <h2 className={`text-[11px] font-bold uppercase tracking-widest ${col.id === 'EVALUATING' ? 'text-amber-400' : 'text-neutral-400'}`}>{col.label}</h2>
                  </div>
                  <div className="flex items-center gap-1.5">
                    {selectionMode && columnTasks.length > 0 && (
                      <ColumnSelectCheckbox
                        columnTasks={columnTasks}
                        selectedTaskIds={selectedTaskIds}
                        onBulkToggle={onBulkToggle}
                      />
                    )}
                    <span title={`Sorted: ${SORT_LABELS[sortBy]}`} className="text-neutral-600 flex items-center">
                      <ArrowDownUp className="w-3 h-3" />
                    </span>
                    <span className="text-[10px] bg-neutral-800 text-neutral-300 px-2 py-0.5 rounded-full font-medium border border-neutral-700">
                      {columnTasks.length}
                    </span>
                    {isArchivable && columnTasks.length > 0 && (
                      <button
                        onClick={() => onArchiveColumn(columnTasks.map(t => t.id), col.label)}
                        title={`Archive all ${col.label} tasks`}
                        className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors"
                      >
                        <Archive className="w-3.5 h-3.5" />
                      </button>
                    )}
                  </div>
                </div>

                <div className={`overflow-y-auto flex-1 pr-1 custom-scrollbar ${compact ? 'space-y-2' : 'space-y-3'}`}>
                  {content}
                </div>
              </div>
            </div>
          );
        })}
      </main>
      <DragOverlay>
        {activeTask && (
          <SortableCard
            task={activeTask}
            maxRetries={maxRetries}
            actionLoadingMap={actionLoadingMap}
            selectionMode={selectionMode}
            selectedTaskIds={selectedTaskIds}
            onToggleTaskSelection={onToggleTaskSelection}
            compact={compact}
            colIdx={-1}
            cardIdx={-1}
            cardRef={() => {}}
            onNavigate={handleNavigate}
            onApprove={onApprove}
            onRequestChanges={onRequestChanges}
            onCancel={onCancel}
            onRetry={onRetry}
            onEditPrompt={onEditPrompt}
            onDismiss={onDismiss}
            onArchive={onArchive}
            onViewLogs={onViewLogs}
            isOverlay
          />
        )}
      </DragOverlay>
    </DndContext>
  );
}
