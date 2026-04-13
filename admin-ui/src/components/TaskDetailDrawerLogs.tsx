import { useRef, useEffect, type ReactNode } from 'react';
import { Search, X, Loader2 } from 'lucide-react';
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

  useEffect(() => {
    if (logFilter || logLevel !== 'all') return;
    logEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [logLines, logFilter, logLevel]);

  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';

  const errorCount = logLines.filter(l => matchesLevel(l, 'error')).length;
  const warnCount = logLines.filter(l => matchesLevel(l, 'warn')).length;
  const statusCount = logLines.filter(l => matchesLevel(l, 'status')).length;

  const indexedLines = logLines.map((line, i) => ({ line, idx: i }));
  const filteredLines = indexedLines.filter(({ line }) => {
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

  const levelButtons: { level: LogLevel; label: string; count?: number; activeClass: string }[] = [
    { level: 'all', label: 'ALL', activeClass: 'bg-neutral-700 text-neutral-100' },
    { level: 'error', label: 'ERROR', count: errorCount, activeClass: 'bg-red-500/20 text-red-400 border-red-500/30' },
    { level: 'warn', label: 'WARN', count: warnCount, activeClass: 'bg-amber-500/20 text-amber-400 border-amber-500/30' },
    { level: 'status', label: 'STATUS', count: statusCount, activeClass: 'bg-blue-500/20 text-blue-400 border-blue-500/30' },
  ];

  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="px-3 py-2 border-b border-neutral-800 flex flex-col gap-1.5 shrink-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          {levelButtons.map(({ level, label, count, activeClass }) => (
            <button
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
      <div className="flex-1 overflow-y-auto p-4 custom-scrollbar font-mono text-xs leading-relaxed">
        {filteredLines.map(({ line, idx }) => {
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
        })}
        <div ref={logEndRef} />
      </div>
    </div>
  );
}
