/* eslint-disable react-hooks/set-state-in-effect */
import { useRef, useEffect, useState, useMemo, useCallback, type ReactNode } from 'react';
import { Search, X, Loader2, ArrowDown, ChevronRight } from 'lucide-react';
import type { Task } from './TaskCard';

export type LogLevel = 'all' | 'error' | 'warn' | 'status';

function matchesLevel(line: string, level: LogLevel): boolean {
  if (level === 'all') return true;
  if (level === 'error') {
    return (
      line.includes('[error]') || line.includes('[stderr]') ||
      line.includes('Error:') || line.includes('ERROR') ||
      line.includes('Exception') || line.includes('Traceback')
    );
  }
  if (level === 'warn') {
    return line.includes('[warn]') || line.includes('WARNING') || line.includes('[warning]');
  }
  if (level === 'status') {
    return line.startsWith('[Status:') || line.startsWith('Status changed:');
  }
  return true;
}

function highlightMatch(text: string, filter: string): ReactNode {
  if (!filter) return text;
  const filterLower = filter.toLowerCase();
  const textLower = text.toLowerCase();
  const parts: ReactNode[] = [];
  let lastIdx = 0;
  let idx = textLower.indexOf(filterLower);
  while (idx !== -1) {
    if (idx > lastIdx) parts.push(text.slice(lastIdx, idx));
    parts.push(
      <mark key={idx} className="bg-amber-500/30 text-amber-200 rounded">
        {text.slice(idx, idx + filter.length)}
      </mark>
    );
    lastIdx = idx + filter.length;
    idx = textLower.indexOf(filterLower, lastIdx);
  }
  if (lastIdx < text.length) parts.push(text.slice(lastIdx));
  return parts.length === 1 ? parts[0] : <>{parts}</>;
}

const PHASE_KEYWORDS: { keyword: string; label: string }[] = [
  { keyword: 'Cloning', label: 'Cloning' },
  { keyword: 'Running Claude', label: 'Working' },
  { keyword: 'Running test', label: 'Testing' },
  { keyword: 'Pushing', label: 'Pushing' },
  { keyword: 'Creating pull request', label: 'Creating PR' },
  { keyword: 'Rebasing', label: 'Rebasing' },
  { keyword: 'Spawning', label: 'Spawning' },
];

interface PhaseSection {
  label: string;
  headerIdx: number;
  lines: { line: string; idx: number }[];
}

function detectPhaseLabel(line: string): string | null {
  for (const { keyword, label } of PHASE_KEYWORDS) {
    if (line.includes(keyword)) return label;
  }
  return null;
}

function groupIntoPhases(indexedLines: { line: string; idx: number }[]): {
  preamble: { line: string; idx: number }[];
  sections: PhaseSection[];
} {
  const preamble: { line: string; idx: number }[] = [];
  const sections: PhaseSection[] = [];
  for (const item of indexedLines) {
    const label = detectPhaseLabel(item.line);
    if (label !== null) {
      sections.push({ label, headerIdx: item.idx, lines: [item] });
    } else if (sections.length === 0) {
      preamble.push(item);
    } else {
      sections[sections.length - 1].lines.push(item);
    }
  }
  return { preamble, sections };
}

export function LogsTab({
  logLines,
  task,
  logFilter,
  setLogFilter,
  logLevel,
  setLogLevel,
  showLineNumbers,
  logsConnecting,
}: {
  logLines: string[];
  task: Task;
  logFilter: string;
  setLogFilter: (v: string) => void;
  logLevel: LogLevel;
  setLogLevel: (v: LogLevel) => void;
  showLineNumbers: boolean;
  logsConnecting: boolean;
}) {
  const logEndRef = useRef<HTMLDivElement>(null);

  // Phase collapsing (from research/fb6a6093)
  const [collapsedSections, setCollapsedSections] = useState<Set<number>>(new Set());
  const initDoneRef = useRef(false);

  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';

  const allIndexedLines = useMemo(
    () => logLines.map((line, i) => ({ line, idx: i })),
    [logLines]
  );

  const { preamble, sections } = useMemo(
    () => groupIntoPhases(allIndexedLines),
    [allIndexedLines]
  );

  // Initialize collapsed state once when sections first appear
  useEffect(() => {
    if (!initDoneRef.current && sections.length > 0) {
      initDoneRef.current = true;
      if (!isActive && sections.length > 1) {
        setCollapsedSections(new Set(Array.from({ length: sections.length - 1 }, (_, i) => i)));
      }
    }
  }, [sections.length, isActive]);

  // Scroll-aware auto-scroll (from HEAD)
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const isAtBottomRef = useRef(true);
  const [newLogCount, setNewLogCount] = useState(0);
  const prevLogLengthRef = useRef(logLines.length);
  const [showNewLogsButton, setShowNewLogsButton] = useState(false);

  const scrollToBottom = useCallback(() => {
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    isAtBottomRef.current = true;
    setNewLogCount(0);
    setShowNewLogsButton(false);
  }, []);

  const handleScroll = useCallback(() => {
    const el = scrollContainerRef.current;
    if (!el) return;
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 40;
    isAtBottomRef.current = atBottom;
    if (atBottom) {
      setNewLogCount(0);
      setShowNewLogsButton(false);
    }
  }, []);

  useEffect(() => {
    if (logFilter || logLevel !== 'all') return;
    const newLines = logLines.length - prevLogLengthRef.current;
    prevLogLengthRef.current = logLines.length;
    if (newLines <= 0) return;

    if (isAtBottomRef.current) {
      logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    } else {
      setNewLogCount(prev => prev + newLines);
      setShowNewLogsButton(true);
    }
  }, [logLines, logFilter, logLevel]);

  // Reset scroll state when filter/level changes
  useEffect(() => {
    isAtBottomRef.current = true;
    setNewLogCount(0);
    setShowNewLogsButton(false);
    prevLogLengthRef.current = logLines.length;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [logFilter, logLevel]);

  const errorCount = logLines.filter(l => matchesLevel(l, 'error')).length;
  const warnCount = logLines.filter(l => matchesLevel(l, 'warn')).length;
  const statusCount = logLines.filter(l => matchesLevel(l, 'status')).length;

  const filteredLines = allIndexedLines.filter(({ line }) => {
    if (logLevel !== 'all' && !matchesLevel(line, logLevel)) return false;
    if (logFilter && !line.toLowerCase().includes(logFilter.toLowerCase())) return false;
    return true;
  });

  if (logLines.length === 0) {
    if (logsConnecting) {
      return (
        <div className="flex-1 flex items-center justify-center gap-2 text-neutral-500 text-sm">
          <Loader2 className="w-4 h-4 animate-spin" />
          Connecting to log stream...
        </div>
      );
    }
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-500 text-sm">
        {isActive ? 'Waiting for logs...' : 'No logs available'}
      </div>
    );
  }

  const hasPhases = sections.length > 0;
  const isFiltering = logFilter !== '' || logLevel !== 'all';
  const allCollapsed = hasPhases && collapsedSections.size === sections.length;

  const toggleSection = (idx: number) => {
    setCollapsedSections(prev => {
      const next = new Set(prev);
      if (next.has(idx)) next.delete(idx);
      else next.add(idx);
      return next;
    });
  };

  const toggleAll = () => {
    if (allCollapsed) {
      setCollapsedSections(new Set());
    } else {
      setCollapsedSections(new Set(sections.map((_, i) => i)));
    }
  };

  const renderLine = (item: { line: string; idx: number }) => {
    const { line, idx } = item;
    const lineNum = idx + 1;
    const content = highlightMatch(line, logFilter);
    const colored =
      line.startsWith('[error]') || line.startsWith('[stderr]')
        ? <span className="text-red-400">{content}</span>
        : line.startsWith('[Status:')
          ? <span className="text-blue-400">{content}</span>
          : line.startsWith('[')
            ? <span className="text-neutral-500">{content}</span>
            : <>{content}</>;
    return (
      <div key={idx} className="whitespace-pre-wrap break-all mb-0.5">
        {showLineNumbers && (
          <span className="text-neutral-600 select-none min-w-8 inline-block text-right mr-3">
            {lineNum}
          </span>
        )}
        {colored}
      </div>
    );
  };

  const levelButtons: { level: LogLevel; label: string; count?: number; activeClass: string }[] = [
    { level: 'all', label: 'ALL', activeClass: 'bg-workbench-layer text-neutral-100' },
    { level: 'error', label: 'ERROR', count: errorCount, activeClass: 'bg-red-500/20 text-red-400 border-red-500/30' },
    { level: 'warn', label: 'WARN', count: warnCount, activeClass: 'bg-amber-500/20 text-amber-400 border-amber-500/30' },
    { level: 'status', label: 'STATUS', count: statusCount, activeClass: 'bg-blue-500/20 text-blue-400 border-blue-500/30' },
  ];

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="px-3 py-2 border-b border-neutral-800 flex flex-col gap-1.5 shrink-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          {levelButtons.map(({ level, label, count, activeClass }) => (
            // reason: log filter pill in compact toolbar; py-0.5 matches log viewer density convention
            <button
              data-viewport-allow-small-target
              key={level}
              onClick={() => setLogLevel(level)}
              className={`px-2 py-0.5 rounded-full text-xs font-medium border transition-colors ${
                logLevel === level
                  ? `${activeClass} border-current`
                  : 'bg-transparent text-neutral-500 border-neutral-700 hover:text-neutral-300 hover:border-neutral-600'
              }`}
            >
              {label}{count !== undefined ? ` (${count})` : ''}
            </button>
          ))}
          {hasPhases && !isFiltering && (
            // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
            <button
              type="button"
              onClick={toggleAll}
              className="ml-auto px-2 py-0.5 rounded text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
            >
              {allCollapsed ? 'Expand All' : 'Collapse All'}
            </button>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Search className="w-3.5 h-3.5 text-neutral-500 flex-shrink-0" />
          <input
            type="text"
            placeholder="Filter logs..."
            className="flex-1 bg-transparent text-xs text-neutral-300 outline-none placeholder-neutral-600"
            value={logFilter}
            onChange={e => setLogFilter(e.target.value)}
          />
          {(logFilter || logLevel !== 'all') && (
            <span className="text-xs text-neutral-500 shrink-0">
              {filteredLines.length} / {logLines.length} lines
            </span>
          )}
          {logFilter && (
            <button onClick={() => setLogFilter('')} aria-label="Clear filter">
              <X className="w-3 h-3 text-neutral-500" />
            </button>
          )}
        </div>
      </div>
      <div className="relative flex-1 min-h-0">
        <div
          ref={scrollContainerRef}
          onScroll={handleScroll}
          className="absolute inset-0 overflow-y-auto p-4 custom-scrollbar font-mono text-xs leading-relaxed"
        >
          {hasPhases && !isFiltering ? (
            <>
              {preamble.map(item => renderLine(item))}
              {sections.map((section, sIdx) => {
                const isCollapsed = collapsedSections.has(sIdx);
                return (
                  <div key={section.headerIdx} className="mb-1">
                    {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                    <button
                      type="button"
                      className="w-full bg-workbench-base/80 border border-neutral-700/50 rounded px-2 py-1 text-ui-md text-neutral-400 flex items-center gap-2 cursor-pointer sticky top-0 z-10 hover:bg-workbench-hover/80 transition-colors"
                      onClick={() => toggleSection(sIdx)}
                    >
                      <ChevronRight
                        className={`w-3 h-3 flex-shrink-0 transition-transform ${isCollapsed ? '' : 'rotate-90'}`}
                      />
                      <span className="font-medium">{section.label}</span>
                      <span className="text-neutral-600">{section.lines.length} lines</span>
                    </button>
                    {!isCollapsed && section.lines.map(item => renderLine(item))}
                  </div>
                );
              })}
            </>
          ) : (
            filteredLines.map(item => renderLine(item))
          )}
          <div ref={logEndRef} />
        </div>
        {showNewLogsButton && (
          <button
            onClick={scrollToBottom}
            className="absolute bottom-3 right-3 flex items-center gap-1.5 bg-blue-600 hover:bg-blue-700 text-white text-xs rounded-full px-3 py-1 shadow-lg transition-colors z-10"
          >
            {newLogCount} new logs
            <ArrowDown className="w-3 h-3" />
          </button>
        )}
      </div>
    </div>
  );
}
