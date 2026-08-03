import React, { useState, useRef, useEffect, useMemo, useCallback } from 'react';
import { Archive, ArrowDownUp, ChevronLeft, ChevronRight, GripVertical } from 'lucide-react';
import { ConfirmDialog } from './ConfirmDialog';
import {
  SortableContext,
  useSortable,
  verticalListSortingStrategy,
} from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { TaskCard, FAILURE_CLASS_CONFIG } from './TaskCard';
import type { Task, TaskStatus } from './TaskCard';
import type { KanbanColumn as KanbanColumnConfig, SortBy } from './KanbanBoard';
import { getFailureClassBreakdown } from '../utils/failureClass';

const ARCHIVABLE_COLUMNS: TaskStatus[] = ['COMPLETED', 'CANCELLED', 'FAILED'];
const DEFAULT_MAX_VISIBLE = 50;

const SORT_LABELS: Record<SortBy, string> = {
  newest: 'Newest first',
  oldest: 'Oldest first',
  type: 'Type',
  title: 'Title',
  duration: 'Longest running',
  attempts: 'Most retried',
};

function FailureClassBreakdown({ tasks }: { tasks: Task[] }) {
  const breakdown = useMemo(() => getFailureClassBreakdown(tasks), [tasks]);
  const entries = useMemo(() => Object.entries(breakdown).sort((a, b) => b[1] - a[1]), [breakdown]);

  if (entries.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1">
      {entries.map(([failureClass, count]) => {
        const config = FAILURE_CLASS_CONFIG[failureClass];
        return (
          <div
            key={failureClass}
            className={`text-ui-xs px-1.5 py-0.5 rounded-sm font-medium whitespace-nowrap ${config?.color ?? 'bg-neutral-700/50 text-neutral-400'}`}
            title={config?.hint ?? failureClass}
          >
            {count} {config?.label ?? failureClass}
          </div>
        );
      })}
    </div>
  );
}

interface SortableCardProps {
  task: Task;
  readOnly: boolean;
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
  onDuplicate?: (task: Task) => void;
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
  readOnly = true,
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
  onDuplicate,
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
      {!readOnly && selectionMode && (
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
      {!readOnly && <div
        className="absolute top-1/2 -translate-y-1/2 -left-0.5 z-10 opacity-0 group-hover:opacity-100 transition-opacity cursor-grab active:cursor-grabbing touch-none p-0.5"
        {...attributes}
        {...listeners}
      >
        <GripVertical className="w-3.5 h-3.5 text-neutral-600 hover:text-neutral-400" />
      </div>}
      <div className={selectionMode ? 'pl-6' : ''}>
        <TaskCard
          readOnly={readOnly}
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
          onDuplicate={onDuplicate}
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
  const ids = useMemo(() => columnTasks.map(t => t.id), [columnTasks]);
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

export function ColumnArchiveButton({
  taskIds,
  columnLabel,
  onArchiveColumn,
  extraClassName,
}: {
  taskIds: string[];
  columnLabel: string;
  onArchiveColumn: (taskIds: string[], columnLabel: string) => void;
  extraClassName?: string;
}) {
  const [confirming, setConfirming] = useState(false);
  const [countdown, setCountdown] = useState(4);
  const [showConfirmDialog, setShowConfirmDialog] = useState(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      if (intervalRef.current) clearInterval(intervalRef.current);
    };
  }, []);

  const handleClick = () => {
    if (confirming) {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      if (intervalRef.current) clearInterval(intervalRef.current);
      setConfirming(false);
      setCountdown(4);
      if (taskIds.length > 10) {
        setShowConfirmDialog(true);
        return;
      }
      onArchiveColumn(taskIds, columnLabel);
    } else {
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
      if (intervalRef.current) clearInterval(intervalRef.current);
      setConfirming(true);
      setCountdown(4);
      let remaining = 4;
      intervalRef.current = setInterval(() => {
        remaining -= 1;
        setCountdown(remaining);
        if (remaining <= 0 && intervalRef.current) clearInterval(intervalRef.current);
      }, 1000);
      timeoutRef.current = setTimeout(() => {
        setConfirming(false);
        setCountdown(4);
      }, 4000);
    }
  };

  const stateClass = confirming
    ? 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30'
    : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50';

  return (
    <>
      <button
        data-viewport-allow-small-target
        onClick={handleClick}
        title={confirming ? `Archive ${taskIds.length} tasks? (${countdown}s)` : `Archive all ${columnLabel} tasks`}
        className={`p-1 rounded transition-colors ${stateClass}${extraClassName ? ` ${extraClassName}` : ''}`}
      >
        <Archive className="w-3.5 h-3.5" />
      </button>
      {showConfirmDialog && (
        <ConfirmDialog
          title="Archive tasks"
          message={`Archive all ${taskIds.length} ${columnLabel} tasks? This cannot be undone.`}
          confirmLabel="Archive all"
          onConfirm={() => { setShowConfirmDialog(false); onArchiveColumn(taskIds, columnLabel); }}
          onCancel={() => setShowConfirmDialog(false)}
        />
      )}
    </>
  );
}

interface KanbanColumnProps {
  col: KanbanColumnConfig;
  readOnly: boolean;
  colIdx: number;
  columnTasks: Task[];
  loading: boolean;
  isCollapsed: boolean;
  colWidth?: number;
  sortBy: SortBy;
  compact?: boolean;
  selectionMode: boolean;
  selectedTaskIds: Set<string>;
  blockingMap: Record<string, number>;
  trackedTaskIds?: Set<string>;
  maxRetries: number;
  actionLoadingMap: Record<string, string>;
  searchQuery?: string;
  hasActiveFilters?: boolean;
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
  onDuplicate?: (task: Task) => void;
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
  readOnly = true,
  colIdx,
  columnTasks,
  loading,
  isCollapsed,
  colWidth,
  sortBy,
  compact,
  selectionMode,
  selectedTaskIds,
  blockingMap,
  trackedTaskIds,
  maxRetries,
  actionLoadingMap,
  searchQuery,
  hasActiveFilters,
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
  onDuplicate,
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
  const taskIds = useMemo(() => columnTasks.map(t => t.id), [columnTasks]);
  const [showAll, setShowAll] = useState(false);
  const visibleTasks = showAll ? columnTasks : columnTasks.slice(0, DEFAULT_MAX_VISIBLE);
  const hiddenCount = Math.max(0, columnTasks.length - DEFAULT_MAX_VISIBLE);
  const visibleTaskIds = useMemo(() => visibleTasks.map(t => t.id), [visibleTasks]);
  const shiftClickHandler = useCallback((taskId: string) => {
    onShiftSelect?.(taskId, taskIds);
  }, [onShiftSelect, taskIds]);
  const effectiveShiftClickHandler = onShiftSelect ? shiftClickHandler : undefined;

  const content = (
    <div className={compact ? 'space-y-1.5' : 'space-y-2'}>
      <SortableContext items={visibleTaskIds} strategy={verticalListSortingStrategy}>
        {visibleTasks.map((task, cardIdx) => (
          <SortableCard
            readOnly={readOnly}
            key={task.id}
            task={task}
            maxRetries={maxRetries}
            actionLoadingMap={actionLoadingMap}
            selectionMode={selectionMode}
            selectedTaskIds={selectedTaskIds}
            onToggleTaskSelection={onToggleTaskSelection}
            onShiftClick={effectiveShiftClickHandler}
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
            onDuplicate={onDuplicate}
            searchQuery={searchQuery}
            onDirectionClick={onDirectionClick}
          />
        ))}
      </SortableContext>
      {hiddenCount > 0 && !showAll && (
        <button
          onClick={() => setShowAll(true)}
          className="w-full text-xs text-neutral-500 hover:text-neutral-300 py-2 rounded-lg border border-dashed border-neutral-800 hover:border-neutral-700 transition-colors min-h-[44px]"
        >
          Show {hiddenCount} more
        </button>
      )}
      {showAll && hiddenCount > 0 && (
        <button
          onClick={() => setShowAll(false)}
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
          {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
          <button
            onClick={() => toggleCollapse(col.id)}
            title={`Expand ${col.label}`}
            className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors"
          >
            <ChevronRight className="w-3.5 h-3.5" />
          </button>
          <span className="text-ui-sm bg-neutral-800 text-neutral-300 px-1.5 py-0.5 rounded-full font-medium border border-neutral-700">
            {columnTasks.length}
          </span>
          <span
            className={`text-ui-sm font-bold uppercase tracking-widest ${col.id === 'EVALUATING' ? 'text-amber-400' : 'text-neutral-400'} flex-1 flex items-center`}
            style={{ writingMode: 'vertical-rl', transform: 'rotate(180deg)' }}
          >
            {col.label}
          </span>
          <span title={`Sorted: ${SORT_LABELS[sortBy]}`} className="text-neutral-600 flex items-center">
            <ArrowDownUp className="w-3 h-3" />
          </span>
          {!readOnly && isArchivable && columnTasks.length > 0 && (
            <ColumnArchiveButton
              taskIds={taskIds}
              columnLabel={col.label}
              onArchiveColumn={onArchiveColumn}
            />
          )}
        </div>

        {/* Expanded content */}
        <div className={`flex flex-col flex-1 overflow-hidden ${isCollapsed ? 'hidden' : ''}`}>
          <div className="sticky top-0 z-10 bg-neutral-900/95 backdrop-blur-sm pb-2 -mx-1 px-1 space-y-1.5 mb-3">
            <div className="flex items-center justify-between">
              <div className="flex items-center gap-1">
                <button
                  data-viewport-allow-small-target
                  onClick={() => toggleCollapse(col.id)}
                  title={`Collapse ${col.label}`}
                  className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors"
                >
                  <ChevronLeft className="w-3.5 h-3.5" />
                </button>
                <h2 className={`text-ui-md font-bold uppercase tracking-widest ${col.id === 'EVALUATING' ? 'text-amber-400' : 'text-neutral-400'}`}>{col.label}</h2>
              </div>
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
                  />
                )}
              </div>
            </div>
            {col.id === 'FAILED' && columnTasks.length > 0 && (
              <div className="px-1">
                <FailureClassBreakdown tasks={columnTasks} />
              </div>
            )}
          </div>

          <div className={`overflow-y-auto flex-1 pr-1 custom-scrollbar ${compact ? 'space-y-1.5' : 'space-y-2'}`}>
            {content}
          </div>
        </div>
      </div>
    </React.Fragment>
  );
}
