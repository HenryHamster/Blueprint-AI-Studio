import React, { useRef, useEffect } from 'react';
import { Archive, ArrowDownUp, ChevronLeft, ChevronRight, GripVertical } from 'lucide-react';
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { TaskCard, TASK_TYPE_STYLES } from './TaskCard';
import type { Task, TaskStatus } from './TaskCard';
import type { KanbanColumn as KanbanColumnConfig, SortBy } from './KanbanBoard';

const ARCHIVABLE_COLUMNS: TaskStatus[] = ['COMPLETED', 'CANCELLED', 'FAILED'];

const SORT_LABELS: Record<SortBy, string> = {
  newest: 'Newest first',
  oldest: 'Oldest first',
  type: 'Type',
  title: 'Title',
  duration: 'Longest running',
  attempts: 'Most retried',
};

export function TaskItem({ task, selectionMode, selectedTaskIds, onToggleTaskSelection, onShiftClick, onCtrlClick, maxRetries, actionLoadingMap, compact, blockingCount, onApprove, onRequestChanges, onCancel, onRetry, onEditPrompt, onDismiss, onArchive, onViewLogs, onTrack, isTracked, colIdx, cardIdx, cardRef, onNavigate, searchQuery, onDirectionClick }: {
  task: Task;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  onToggleTaskSelection: (id: string) => void;
  onShiftClick?: (id: string) => void;
  onCtrlClick?: (taskId: string) => void;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  compact?: boolean;
  blockingCount?: number;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  onTrack?: (task: Task) => void;
  isTracked?: boolean;
  colIdx?: number;
  cardIdx?: number;
  cardRef?: (el: HTMLDivElement | null) => void;
  onNavigate?: (colIdx: number, cardIdx: number, direction: 'up' | 'down' | 'left' | 'right') => void;
  searchQuery?: string;
  onDirectionClick?: (directionId: string) => void;
}) {
  return (
    <div className="relative">
      {selectionMode && (
        <div className="absolute top-2 left-2 z-10">
          <input
            type="checkbox"
            checked={selectedTaskIds.has(task.id)}
            onChange={(e) => {
              const shiftHeld = !!(e.nativeEvent as MouseEvent).shiftKey;
              if (shiftHeld && onShiftClick) {
                onShiftClick(task.id);
              } else {
                onToggleTaskSelection(task.id);
              }
            }}
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
          blockingCount={blockingCount}
          selectionMode={selectionMode}
          onToggleTaskSelection={onToggleTaskSelection}
          onCtrlClick={onCtrlClick}
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
          onTrack={onTrack}
          isTracked={isTracked}
          searchQuery={searchQuery}
          onDirectionClick={onDirectionClick}
        />
      </div>
    </div>
  );
}

export interface SortableCardProps {
  task: Task;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  onToggleTaskSelection: (id: string) => void;
  onShiftClick?: (id: string) => void;
  onCtrlClick?: (taskId: string) => void;
  compact?: boolean;
  blockingCount?: number;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  onTrack?: (task: Task) => void;
  isTracked?: boolean;
  isOverlay?: boolean;
  colIdx: number;
  cardIdx: number;
  cardRef: (el: HTMLDivElement | null) => void;
  onNavigate: (colIdx: number, cardIdx: number, direction: 'up' | 'down' | 'left' | 'right') => void;
  searchQuery?: string;
  onDirectionClick?: (directionId: string) => void;
}

export function SortableCard({
  task,
  maxRetries,
  actionLoadingMap,
  selectionMode,
  selectedTaskIds,
  onToggleTaskSelection,
  onShiftClick,
  onCtrlClick,
  compact,
  blockingCount,
  onApprove,
  onRequestChanges,
  onCancel,
  onRetry,
  onEditPrompt,
  onDismiss,
  onArchive,
  onViewLogs,
  onTrack,
  isTracked,
  isOverlay = false,
  colIdx,
  cardIdx,
  cardRef,
  onNavigate,
  searchQuery,
  onDirectionClick,
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
            onChange={(e) => {
              const shiftHeld = !!(e.nativeEvent as MouseEvent).shiftKey;
              if (shiftHeld && onShiftClick) {
                onShiftClick(task.id);
              } else {
                onToggleTaskSelection(task.id);
              }
            }}
            className="w-4 h-4 accent-amber-400 cursor-pointer"
          />
        </div>
      )}
      <div
        className="absolute top-1/2 -translate-y-1/2 -left-0.5 z-10 opacity-0 group-hover:opacity-100 transition-opacity cursor-grab active:cursor-grabbing touch-none p-0.5"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="w-3.5 h-3.5 text-neutral-600 hover:text-neutral-400" />
      </div>
      <div className={selectionMode ? 'pl-6' : ''}>
        <TaskCard
          ref={cardRef}
          task={task}
          maxRetries={maxRetries}
          actionLoading={actionLoadingMap[task.id]}
          compact={compact}
          blockingCount={blockingCount}
          selectionMode={selectionMode}
          onToggleTaskSelection={onToggleTaskSelection}
          onCtrlClick={onCtrlClick}
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
          onTrack={onTrack}
          isTracked={isTracked}
          searchQuery={searchQuery}
          onDirectionClick={onDirectionClick}
        />
      </div>
    </div>
  );
}

export function ColumnSelectCheckbox({ columnTasks, selectedTaskIds, onBulkToggle }: {
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

export interface KanbanColumnProps {
  col: KanbanColumnConfig;
  colIdx: number;
  columnTasks: Task[];
  loading: boolean;
  isCollapsed: boolean;
  colWidth?: number;
  sortBy: SortBy;
  compact?: boolean;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  groupBy: 'status' | 'type' | 'project';
  projectMap: Record<string, string>;
  blockingMap: Record<string, number>;
  trackedTaskIds?: Set<string>;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  searchQuery?: string;
  onBulkToggle: (taskIds: string[], selected: boolean) => void;
  onShiftSelect?: (taskId: string, orderedTaskIds: string[]) => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onArchiveColumn: (taskIds: string[], columnLabel: string) => void;
  onViewLogs: (task: Task) => void;
  onTrack?: (task: Task) => void;
  onToggleTaskSelection: (id: string) => void;
  onCtrlClick?: (taskId: string) => void;
  toggleCollapse: (colId: string) => void;
  handleResizeStart: (colId: string, e: React.MouseEvent) => void;
  showResizeHandle: boolean;
  prevColId?: string;
  getCardRef: (cardIdx: number) => (el: HTMLDivElement | null) => void;
  handleNavigate: (colIdx: number, cardIdx: number, direction: 'up' | 'down' | 'left' | 'right') => void;
  onDirectionClick?: (directionId: string) => void;
}

export function KanbanColumn({
  col,
  colIdx,
  columnTasks,
  loading,
  isCollapsed,
  colWidth,
  sortBy,
  compact,
  selectionMode,
  selectedTaskIds,
  groupBy,
  projectMap,
  blockingMap,
  trackedTaskIds,
  maxRetries,
  actionLoadingMap,
  searchQuery,
  onBulkToggle,
  onShiftSelect,
  onApprove,
  onRequestChanges,
  onCancel,
  onRetry,
  onEditPrompt,
  onDismiss,
  onArchive,
  onArchiveColumn,
  onViewLogs,
  onTrack,
  onToggleTaskSelection,
  onCtrlClick,
  toggleCollapse,
  handleResizeStart,
  showResizeHandle,
  prevColId,
  getCardRef,
  handleNavigate,
  onDirectionClick,
}: KanbanColumnProps) {
  const isArchivable = ARCHIVABLE_COLUMNS.includes(col.id);
  const taskIds = columnTasks.map(t => t.id);
  const shiftClickHandler = onShiftSelect
    ? (taskId: string) => onShiftSelect(taskId, taskIds)
    : undefined;

  const taskProps = { selectionMode, selectedTaskIds, onToggleTaskSelection, onCtrlClick, maxRetries, actionLoadingMap, compact, onApprove, onRequestChanges, onCancel, onRetry, onEditPrompt, onDismiss, onArchive, onViewLogs, onTrack, searchQuery, onDirectionClick };

  let content: React.ReactNode;

  if (groupBy === 'type') {
    const groups = new Map<string, Task[]>();
    for (const task of columnTasks) {
      const type = task.task_type || 'create';
      const list = groups.get(type) ?? [];
      list.push(task);
      groups.set(type, list);
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
              <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
                {typeTasks.map(task => {
                  const flatIdx = taskFlatIndex.get(task.id) ?? 0;
                  return (
                    <TaskItem
                      key={task.id}
                      task={task}
                      {...taskProps}
                      onShiftClick={shiftClickHandler}
                      blockingCount={blockingMap[task.id]}
                      isTracked={trackedTaskIds?.has(task.id)}
                      colIdx={colIdx}
                      cardIdx={flatIdx}
                      cardRef={getCardRef(flatIdx)}
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
      const list = groups.get(key) ?? [];
      list.push(task);
      groups.set(key, list);
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
              <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
                {projectTasks.map(task => {
                  const flatIdx = taskFlatIndex.get(task.id) ?? 0;
                  return (
                    <TaskItem
                      key={task.id}
                      task={task}
                      {...taskProps}
                      onShiftClick={shiftClickHandler}
                      blockingCount={blockingMap[task.id]}
                      isTracked={trackedTaskIds?.has(task.id)}
                      colIdx={colIdx}
                      cardIdx={flatIdx}
                      cardRef={getCardRef(flatIdx)}
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
      <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
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
              onShiftClick={shiftClickHandler}
              onCtrlClick={onCtrlClick}
              compact={compact}
              blockingCount={blockingMap[task.id]}
              colIdx={colIdx}
              cardIdx={cardIdx}
              cardRef={getCardRef(cardIdx)}
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
              searchQuery={searchQuery}
              onDirectionClick={onDirectionClick}
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
    <React.Fragment>
      {showResizeHandle && prevColId && (
        <div
          onMouseDown={(e) => handleResizeStart(prevColId, e)}
          className="w-1 shrink-0 cursor-col-resize group/resize flex items-center justify-center hover:bg-neutral-700/50 rounded transition-colors self-stretch"
          title="Drag to resize"
        >
          <div className="w-0.5 h-8 bg-neutral-700 group-hover/resize:bg-neutral-500 rounded-full transition-colors" />
        </div>
      )}
      <div
        id={`kanban-col-${col.id}`}
        className={`flex-shrink-0 flex flex-col bg-neutral-900/50 rounded-xl border border-neutral-800/50 overflow-hidden ${
          isCollapsed ? 'w-10' : colWidth ? 'p-3' : 'flex-1 min-w-[280px] max-w-[520px] p-3'
        }`}
        style={!isCollapsed && colWidth ? { width: colWidth } : undefined}
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

          <div className={`overflow-y-auto flex-1 pr-1 custom-scrollbar ${compact ? 'space-y-1.5' : 'space-y-2'}`}>
            {content}
          </div>
        </div>
      </div>
    </React.Fragment>
  );
}
