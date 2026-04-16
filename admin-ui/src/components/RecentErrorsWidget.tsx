import { useState, useEffect, useCallback, useRef } from 'react';
import { AlertTriangle, CheckCircle, ChevronDown, ChevronRight, Copy, Check } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';

interface ErrorBucket {
  exception_class: string;
  source: string;
  count: number;
  first_seen_iso: string;
  last_seen_iso: string;
  sample_message: string;
}

interface RecentErrorsResponse {
  buckets: ErrorBucket[];
  hours: number;
  total_entries_scanned?: number;
  generated_at: string;
  error?: string;
}

const POLL_INTERVAL_MS = 60_000;
const DEFAULT_HOURS = 1;
const DEFAULT_LIMIT = 20;

export function RecentErrorsWidget() {
  const [data, setData] = useState<RecentErrorsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [fetchError, setFetchError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [hoveredBucket, setHoveredBucket] = useState<string | null>(null);
  const mountedRef = useRef(true);

  const fetchErrors = useCallback(async () => {
    try {
      const res = await apiFetch(
        `/api/admin/recent-orchestrator-errors?hours=${DEFAULT_HOURS}&limit=${DEFAULT_LIMIT}`,
      );
      if (!mountedRef.current) return;
      if (!res.ok) {
        setFetchError(`HTTP ${res.status}`);
        setData(null);
        setLoading(false);
        return;
      }
      const body = (await res.json()) as RecentErrorsResponse;
      if (!mountedRef.current) return;
      setData(body);
      setFetchError(null);
      setLoading(false);
    } catch (e) {
      if (!mountedRef.current) return;
      setFetchError(e instanceof Error ? e.message : 'network error');
      setData(null);
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchErrors();
    const id = setInterval(() => {
      if (document.visibilityState === 'visible') {
        fetchErrors();
      }
    }, POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [fetchErrors]);

  const buckets = data?.buckets ?? [];
  const totalCount = buckets.reduce((sum, b) => sum + b.count, 0);
  const hasApiError = Boolean(data?.error) || Boolean(fetchError);
  const backendErrorMessage = data?.error ?? fetchError ?? '';
  const [copied, setCopied] = useState(false);

  const copyAll = useCallback(() => {
    const lines = buckets.map(b =>
      `[${b.count}x] ${b.exception_class} — ${b.source}\n  Last: ${b.last_seen_iso}\n  ${b.sample_message || '(no sample)'}`
    );
    navigator.clipboard.writeText(lines.join('\n\n')).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    });
  }, [buckets]);

  if (loading) {
    return (
      <section aria-label="Recent orchestrator errors">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Orchestrator Errors</h2>
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 h-14 animate-pulse" />
      </section>
    );
  }

  if (hasApiError) {
    return (
      <section aria-label="Recent orchestrator errors">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Orchestrator Errors</h2>
        <div
          className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-neutral-400 flex items-center gap-2"
          role="status"
        >
          <AlertTriangle className="w-3.5 h-3.5 text-neutral-500 shrink-0" aria-hidden="true" />
          <span>Error fetching log data</span>
          <span className="text-[10px] text-neutral-600 truncate" title={backendErrorMessage}>
            {backendErrorMessage}
          </span>
        </div>
      </section>
    );
  }

  if (totalCount === 0) {
    return (
      <section aria-label="Recent orchestrator errors">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Orchestrator Errors</h2>
        <div
          className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-emerald-400 flex items-center gap-2"
          role="status"
        >
          <CheckCircle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
          <span>No errors in last hour</span>
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Recent orchestrator errors">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Orchestrator Errors</h2>
      <div className="bg-neutral-900/50 border border-red-500/30 rounded-lg overflow-hidden">
        <div className="flex items-center">
          <button
            type="button"
            onClick={() => setExpanded(e => !e)}
            className="flex-1 px-3 py-2 flex items-center gap-2 text-left hover:bg-neutral-900/80 focus:outline-none focus:bg-neutral-900/80"
            aria-expanded={expanded}
            aria-controls="orchestrator-errors-list"
          >
            {expanded ? (
              <ChevronDown className="w-3.5 h-3.5 text-neutral-400 shrink-0" aria-hidden="true" />
            ) : (
              <ChevronRight className="w-3.5 h-3.5 text-neutral-400 shrink-0" aria-hidden="true" />
            )}
            <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" aria-hidden="true" />
            <span className="text-xs text-red-400 font-semibold">
              {totalCount} error{totalCount !== 1 ? 's' : ''} in last hour
            </span>
            <span className="text-[10px] text-neutral-500 ml-auto">
              {buckets.length} unique
            </span>
          </button>
          <button
            type="button"
            onClick={(e) => { e.stopPropagation(); copyAll(); }}
            title="Copy all errors to clipboard"
            className="px-2.5 py-2 text-neutral-400 hover:text-neutral-100 transition-colors focus:outline-none"
          >
            {copied
              ? <Check className="w-4 h-4 text-emerald-400" />
              : <Copy className="w-4 h-4" />}
          </button>
        </div>
        {expanded && (
          <ul
            id="orchestrator-errors-list"
            className="border-t border-neutral-800/60 divide-y divide-neutral-800/60"
          >
            {buckets.map(bucket => {
              const bucketId = `${bucket.exception_class}|${bucket.source}`;
              const isHovered = hoveredBucket === bucketId;
              return (
                <li
                  key={bucketId}
                  className="px-3 py-2 hover:bg-neutral-900/80 focus-within:bg-neutral-900/80"
                  onMouseEnter={() => setHoveredBucket(bucketId)}
                  onMouseLeave={() => setHoveredBucket(null)}
                  onFocus={() => setHoveredBucket(bucketId)}
                  onBlur={() => setHoveredBucket(null)}
                >
                  <div
                    className="flex items-center gap-2 text-xs"
                    tabIndex={0}
                    role="button"
                    aria-label={`${bucket.exception_class} in ${bucket.source}, ${bucket.count} occurrences`}
                  >
                    <span className="text-red-400 font-semibold truncate max-w-[40%]" title={bucket.exception_class}>
                      {bucket.exception_class}
                    </span>
                    <span className="text-neutral-500 font-mono text-[10px] truncate flex-1" title={bucket.source}>
                      {bucket.source}
                    </span>
                    <span className="text-neutral-300 shrink-0">
                      {bucket.count}&times; in last hour
                    </span>
                    <span className="text-[10px] text-neutral-500 shrink-0">
                      {timeAgo(bucket.last_seen_iso)}
                    </span>
                  </div>
                  {isHovered && bucket.sample_message && (
                    <pre className="mt-2 text-[10px] text-neutral-400 bg-neutral-950/60 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all max-h-32">
                      {bucket.sample_message}
                    </pre>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </section>
  );
}
