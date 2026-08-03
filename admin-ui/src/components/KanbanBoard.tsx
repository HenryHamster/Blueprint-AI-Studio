/* eslint-disable react-hooks/immutability */
import React, { useMemo, useState, useRef, useCallback, useEffect } from 'react';
import { ArrowDownUp } from 'lucide-react';
import {
  DndContext,
  closestCenter,
  PointerSensor,
  useSensor,
  useSensors,
  DragOverlay,
} from '@dnd-kit/core';
import type { DragEndEvent, DragStartEvent } from '@dnd-kit/core';
import { arrayMove, SortableContext, verticalListSortingStrategy } from '@dnd-kit/sortable';
import type { Task, TaskStatus } from './TaskCard';
import { sortTasks, type TaskSortBy } from '../utils/taskCsv';
import {
  KanbanColumn as KanbanColumnComponent,
  SortableCard,
  ColumnSelectCheckbox,
  ColumnArchiveButton,
} from './KanbanColumn';

export type SortBy = TaskSortBy;

const COLLAPSED_COLUMNS_KEY = 'kanban_collapsed_columns';
const DEFAULT_MAX_VISIBLE = 50;

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

const STORAGE_KEY = 'kanban_card_order';
const ARCHIVABLE_COLUMNS: TaskStatus[] = ['COMPLETED', 'CANCELLED', 'FAILED'];
const SORT_LABELS: Record<SortBy, string> = {
  newest: 'Newest first',
  oldest: 'Oldest first',
  type: 'Type',
  title: 'Title',
  duration: 'Longest running',
  attempts: 'Most retried',
};

interface KanbanBoardProps {
  tasks: Task[];
  readOnly: boolean;
  columns: KanbanColumn[];
  loading: boolean; // true only on first load before data is available
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  compact?: boolean;
  sortBy: SortBy;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  onToggleTaskSelection: (id: string) => void;
  onCtrlClick?: (taskId: string) => void;
  onBulkToggle: (taskIds: string[], selected: boolean) => void;
  onShiftSelect?: (taskId: string, orderedTaskIds: string[]) => void;
  onArchiveColumn: (taskIds: string[], columnLabel: string) => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  onTrack?: (task: Task) => void;
  trackedTaskIds?: Set<string>;
  onDuplicate?: (task: Task) => void;
  hasActiveFilters?: boolean;
  searchQuery?: string;
  onDirectionClick?: (directionId: string) => void;
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
  readOnly = true,
  columns,
  loading,
  maxRetries,
  actionLoadingMap,
  compact,
  sortBy,
  selectionMode,
  selectedTaskIds,
  onToggleTaskSelection,
  onCtrlClick,
  onBulkToggle,
  onShiftSelect,
  onArchiveColumn,
  onApprove,
  onRequestChanges,
  onCancel,
  onRetry,
  onEditPrompt,
  onDismiss,
  onArchive,
  onViewLogs,
  onTrack,
  trackedTaskIds,
  onDuplicate,
  hasActiveFilters,
  searchQuery,
  onDirectionClick,
}: KanbanBoardProps) {
  const [columnOrder, setColumnOrder] = useState<Record<string, string[]>>(loadColumnOrder);
  const [activeTask, setActiveTask] = useState<Task | null>(null);
  const [collapsedColumns, setCollapsedColumns] = useState<Set<string>>(loadCollapsedColumns);
  const [activeColumnId, setActiveColumnId] = useState<string | null>(null);
  const [mobileShowAll, setMobileShowAll] = useState(false);

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

  // Column resize state: colId -> width in px (null = flex default)
  const [columnWidths, setColumnWidths] = useState<Record<string, number>>({});
  const resizingRef = useRef<{ colId: string; startX: number; startWidth: number } | null>(null);

  const handleResizeStart = useCallback((colId: string, e: React.MouseEvent) => {
    e.preventDefault();
    const colEl = document.getElementById(`kanban-col-${colId}`);
    if (!colEl) return;
    resizingRef.current = { colId, startX: e.clientX, startWidth: colEl.getBoundingClientRect().width };

    const onMouseMove = (ev: MouseEvent) => {
      const r = resizingRef.current;
      if (!r) return;
      const delta = ev.clientX - r.startX;
      const newWidth = Math.max(220, Math.min(700, r.startWidth + delta));
      setColumnWidths(prev => ({ ...prev, [r.colId]: newWidth }));
    };
    const onMouseUp = () => {
      resizingRef.current = null;
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', onMouseUp);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
    };
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', onMouseUp);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
  }, []);

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
          const t = taskMap.get(id);
          if (t) {
            ordered.push(t);
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

  const defaultActiveColId = useMemo(() => {
    for (const col of columns) {
      if ((tasksByColumn[col.id] ?? []).length > 0) return col.id;
    }
    return columns.find(c => c.id === 'QUEUED')?.id ?? columns[0]?.id ?? 'QUEUED';
  }, [columns, tasksByColumn]);

  const effectiveActiveColId = activeColumnId ?? defaultActiveColId;

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

  // Stable factory that returns a cached ref callback for (colIdx, cardIdx).
  // Uses a plain-object cache inside useMemo so no ref is accessed during render.
  const makeCardRef = useMemo(() => {
    const cache: Record<string, (el: HTMLDivElement | null) => void> = {};
    return (colIdx: number, cardIdx: number): (el: HTMLDivElement | null) => void => {
      const key = `${colIdx}:${cardIdx}`;
      if (!cache[key]) {
        cache[key] = (el: HTMLDivElement | null) => {
          if (!columnRefs.current[colIdx]) columnRefs.current[colIdx] = [];
          columnRefs.current[colIdx][cardIdx] = el;
        };
      }
      return cache[key];
    };
  }, []);

  // Curried variant for KanbanColumnComponent's getCardRef prop.
  const makeGetCardRef = useMemo(() => {
    const cache: Record<number, (cardIdx: number) => (el: HTMLDivElement | null) => void> = {};
    return (colIdx: number): (cardIdx: number) => (el: HTMLDivElement | null) => void => {
      if (!cache[colIdx]) {
        cache[colIdx] = (cardIdx: number) => makeCardRef(colIdx, cardIdx);
      }
      return cache[colIdx];
    };
  }, [makeCardRef]);

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

  const blockingMap = useMemo(() => {
    const map: Record<string, number> = {};
    for (const task of tasks) {
      if (task.depends_on) {
        for (const depId of task.depends_on) {
          map[depId] = (map[depId] ?? 0) + 1;
        }
      }
    }
    return map;
  }, [tasks]);

  const taskIdsByColumn = useMemo(() => {
    const result: Record<string, string[]> = {};
    for (const col of columns) {
      result[col.id] = (tasksByColumn[col.id] ?? []).map(t => t.id);
    }
    return result;
  }, [columns, tasksByColumn]);

  // Stable per-column handlers so TaskCard React.memo is not bypassed by a new
  // function reference on every parent render. taskIdsByColumn is already memoized
  // so the handlers only change when tasks or column membership actually changes.
  const shiftClickHandlersByColumn = useMemo(() => {
    if (!onShiftSelect) return {} as Record<string, (taskId: string) => void>;
    const handlers: Record<string, (taskId: string) => void> = {};
    for (const col of columns) {
      const ids = taskIdsByColumn[col.id] ?? [];
      handlers[col.id] = (taskId: string) => onShiftSelect(taskId, ids);
    }
    return handlers;
  }, [onShiftSelect, taskIdsByColumn, columns]);

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragStart={handleDragStart}
      onDragEnd={handleDragEnd}
    >
      {/* Mobile: tab bar + single active column */}
      <div className="flex flex-col flex-1 min-w-0 overflow-hidden md:hidden">
        {/* Horizontal-scrollable tab bar: column tabs overflow on narrow viewports to keep wrapper in-bounds */}
        <div
          className="flex flex-nowrap gap-1 overflow-x-auto pb-2 sticky top-0 z-20 bg-neutral-950 px-2 pt-2 shrink-0 min-w-0 max-w-full"
          data-viewport-allow-overflow
          data-viewport-allow-truncation
        >
          {columns.map(col => {
            const count = (tasksByColumn[col.id] ?? []).length;
            const isActive = effectiveActiveColId === col.id;
            return (
              <button
                key={col.id}
                onClick={() => setActiveColumnId(col.id)}
                className={`flex items-center gap-1.5 px-3 rounded-lg text-xs font-semibold whitespace-nowrap min-h-[44px] shrink-0 transition-colors ${
                  isActive
                    ? 'bg-neutral-700 text-neutral-100'
                    : 'bg-neutral-900 text-neutral-400 hover:text-neutral-200'
                }`}
              >
                <span>{col.label}</span>
                <span className={`text-ui-sm px-1.5 py-0.5 rounded-full ${isActive ? 'bg-neutral-600 text-neutral-200' : 'bg-neutral-800 text-neutral-500'}`}>
                  {count}
                </span>
              </button>
            );
          })}
        </div>
        {columns.map((col, colIdx) => {
          if (col.id !== effectiveActiveColId) return null;
          const columnTasks = tasksByColumn[col.id] ?? [];
          const isArchivable = ARCHIVABLE_COLUMNS.includes(col.id);
          const taskIds = taskIdsByColumn[col.id] ?? [];
          const shiftClickHandler = shiftClickHandlersByColumn[col.id];

          const mobileContent = (
              <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
                <SortableContext items={taskIds} strategy={verticalListSortingStrategy}>
                  {columnTasks.slice(0, mobileShowAll ? columnTasks.length : DEFAULT_MAX_VISIBLE).map((task, cardIdx) => (
                    <SortableCard
                      readOnly={readOnly}
                      key={task.id}
                      task={task}
                      maxRetries={maxRetries}
                      actionLoadingMap={actionLoadingMap}
                      selectionMode={selectionMode}
                      selectedTaskIds={selectedTaskIds}
                      onToggleTaskSelection={onToggleTaskSelection}
                      onShiftClick={shiftClickHandler}
                      onCtrlClick={onCtrlClick}
                      compact={compact}
                      blockingCount={blockingMap[task.id]}
                      colIdx={colIdx}
                      cardIdx={cardIdx}
                      cardRef={makeCardRef(colIdx, cardIdx)}
                      onNavigate={handleNavigate}
                      onApprove={onApprove}
                      onRequestChanges={onRequestChanges}
                      onCancel={onCancel}
                      onRetry={onRetry}
                      onEditPrompt={onEditPrompt}
                      onDismiss={onDismiss}
                      onArchive={onArchive}
                      onViewLogs={onViewLogs}
                      onTrack={onTrack}
                      isTracked={trackedTaskIds?.has(task.id)}
                      onDuplicate={onDuplicate}
                      searchQuery={searchQuery}
                      onDirectionClick={onDirectionClick}
                    />
                  ))}
                </SortableContext>
                {columnTasks.length > DEFAULT_MAX_VISIBLE && !mobileShowAll && (
                  <button
                    onClick={() => setMobileShowAll(true)}
                    className="w-full text-xs text-neutral-500 hover:text-neutral-300 py-2 rounded-lg border border-dashed border-neutral-800 hover:border-neutral-700 transition-colors min-h-[44px]"
                  >
                    Show {columnTasks.length - DEFAULT_MAX_VISIBLE} more
                  </button>
                )}
                {mobileShowAll && columnTasks.length > DEFAULT_MAX_VISIBLE && (
                  <button
                    onClick={() => setMobileShowAll(false)}
                    className="w-full text-xs text-neutral-500 hover:text-neutral-300 py-2 rounded-lg border border-dashed border-neutral-800 hover:border-neutral-700 transition-colors min-h-[44px]"
                  >
                    Show less
                  </button>
                )}
                {columnTasks.length === 0 && !loading && (
                  <div className="text-center text-neutral-600 text-xs py-8 border border-dashed border-neutral-800 rounded-xl bg-neutral-900/20">
                    {hasActiveFilters ? 'No tasks match filters' : 'No tasks'}
                  </div>
                )}
              </div>
            );

          return (
            <div key={col.id} className="flex-1 overflow-y-auto p-3 custom-scrollbar">
              <div className="flex items-center justify-between mb-3">
                <h2 className={`text-ui-md font-bold uppercase tracking-widest ${col.id === 'EVALUATING' ? 'text-amber-400' : 'text-neutral-400'}`}>{col.label}</h2>
                <div className="flex items-center gap-1.5">
                  {!readOnly && selectionMode && columnTasks.length > 0 && (
                    <ColumnSelectCheckbox
                      columnTasks={columnTasks}
                      selectedTaskIds={selectedTaskIds}
                      onBulkToggle={onBulkToggle}
                    />
                  )}
                  <span title={`Sorted: ${SORT_LABELS[sortBy]}`} className="text-neutral-600 flex items-center">
                    <ArrowDownUp className="w-3 h-3" />
                  </span>
                  <span className="text-ui-sm bg-neutral-800 text-neutral-300 px-2 py-0.5 rounded-full font-medium border border-neutral-700">
                    {columnTasks.length}
                  </span>
                  {!readOnly && isArchivable && columnTasks.length > 0 && (
                    <ColumnArchiveButton
                      taskIds={taskIds}
                      columnLabel={col.label}
                      onArchiveColumn={onArchiveColumn}
                      extraClassName="min-h-11 min-w-11 flex items-center justify-center"
                    />
                  )}
                </div>
              </div>
              {mobileContent}
            </div>
          );
        })}
      </div>

      {/* Desktop: existing multi-column horizontal layout */}
      <main className="hidden md:flex justify-center gap-4 overflow-x-auto overflow-y-hidden pb-4 flex-1 custom-scrollbar">
        {/* eslint-disable-next-line react-hooks/refs */}
        {columns.map((col, colIdx) => {
          const columnTasks = tasksByColumn[col.id] ?? [];
          if (!columnRefs.current[colIdx]) columnRefs.current[colIdx] = [];
          const isCollapsed = collapsedColumns.has(col.id);
          const showResizeHandle = colIdx > 0 && !collapsedColumns.has(columns[colIdx - 1].id) && !isCollapsed;
          const prevColId = colIdx > 0 ? columns[colIdx - 1].id : undefined;
          const colWidth = columnWidths[col.id];

          return (
            <KanbanColumnComponent
              readOnly={readOnly}
              key={col.id}
              col={col}
              colIdx={colIdx}
              columnTasks={columnTasks}
              loading={loading}
              isCollapsed={isCollapsed}
              colWidth={colWidth}
              sortBy={sortBy}
              compact={compact}
              selectionMode={selectionMode}
              selectedTaskIds={selectedTaskIds}
              blockingMap={blockingMap}
              trackedTaskIds={trackedTaskIds}
              maxRetries={maxRetries}
              actionLoadingMap={actionLoadingMap}
              searchQuery={searchQuery}
              hasActiveFilters={hasActiveFilters}
              onBulkToggle={onBulkToggle}
              onShiftSelect={onShiftSelect}
              onApprove={onApprove}
              onRequestChanges={onRequestChanges}
              onCancel={onCancel}
              onRetry={onRetry}
              onEditPrompt={onEditPrompt}
              onDismiss={onDismiss}
              onArchive={onArchive}
              onArchiveColumn={onArchiveColumn}
              onViewLogs={onViewLogs}
              onTrack={onTrack}
              onDuplicate={onDuplicate}
              onToggleTaskSelection={onToggleTaskSelection}
              onCtrlClick={onCtrlClick}
              toggleCollapse={toggleCollapse}
              handleResizeStart={handleResizeStart}
              showResizeHandle={showResizeHandle}
              prevColId={prevColId}
              getCardRef={makeGetCardRef(colIdx)}
              handleNavigate={handleNavigate}
              onDirectionClick={onDirectionClick}
            />
          );
        })}
      </main>
      <DragOverlay>
        {activeTask && (
          <SortableCard
            readOnly={readOnly}
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
            onTrack={onTrack}
            isTracked={trackedTaskIds?.has(activeTask.id)}
            onDuplicate={onDuplicate}
            searchQuery={searchQuery}
            onDirectionClick={onDirectionClick}
            isOverlay
          />
        )}
      </DragOverlay>
    </DndContext>
  );
}
