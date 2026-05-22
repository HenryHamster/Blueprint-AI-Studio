import { useState, useEffect, useCallback } from 'react';
import { BookOpen, ChevronDown, ChevronUp, ThumbsUp, ThumbsDown, Archive, AlertTriangle } from 'lucide-react';
import { apiFetch, API_BASE_URL } from '../api';

interface PlaybookEntry {
  key: string;
  content: string;
  effectiveness_score: number;
  confirmed_count: number;
  reference_count: number;
  age_days: number;
  stale: boolean;
  applicable_types: string[];
  source: string;
}

interface PlaybookPanelProps {
  selectedProjectId: string | null;
}

const SOURCE_LABELS: Record<string, string> = {
  'auto-generated': 'auto',
  'success-pattern': 'success',
  'review-extracted': 'review',
  'first-attempt-pattern': 'first-attempt',
};

function EffectivenessBar({ score }: { score: number }) {
  const pct = Math.round(score * 100);
  const color = score >= 0.5 ? 'bg-emerald-500' : score >= 0.2 ? 'bg-amber-500' : 'bg-neutral-600';
  return (
    <div className="flex items-center gap-1.5 min-w-0">
      <div className="w-16 h-1.5 bg-neutral-800 rounded-full overflow-hidden shrink-0">
        <div className={`h-full rounded-full ${color}`} style={{ width: `${Math.min(pct, 100)}%` }} />
      </div>
      <span className="text-xs text-neutral-400 tabular-nums">{pct}%</span>
    </div>
  );
}

export function PlaybookPanel({ selectedProjectId }: PlaybookPanelProps) {
  const [collapsed, setCollapsed] = useState(() =>
    localStorage.getItem('blueprint-admin:playbookPanelCollapsed') !== 'false'
  );
  const [entries, setEntries] = useState<PlaybookEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingKey, setPendingKey] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!selectedProjectId) return;
    setLoading(true);
    setError(null);
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${selectedProjectId}/playbook`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      // API returns {entries: {key: {...}}, count: N}
      const dict: Record<string, Omit<PlaybookEntry, 'key'>> = data.entries ?? {};
      const list: PlaybookEntry[] = Object.entries(dict).map(([k, v]) => ({ key: k, ...v }));
      list.sort((a, b) => b.effectiveness_score - a.effectiveness_score || b.confirmed_count - a.confirmed_count);
      setEntries(list);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load playbook');
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId]);

  useEffect(() => {
    if (!collapsed && selectedProjectId) load();
  }, [collapsed, selectedProjectId, load]);

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem('blueprint-admin:playbookPanelCollapsed', String(next));
  };

  const doAction = async (key: string, action: 'promote' | 'demote' | 'archive') => {
    if (!selectedProjectId || pendingKey) return;
    setPendingKey(key);
    try {
      const encodedKey = encodeURIComponent(key);
      const res = await apiFetch(
        `${API_BASE_URL}/api/admin/projects/${selectedProjectId}/playbook/${encodedKey}`,
        { method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) }
      );
      if (!res.ok) {
        const msg = await res.text();
        throw new Error(msg || `HTTP ${res.status}`);
      }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Action failed');
    } finally {
      setPendingKey(null);
    }
  };

  if (!selectedProjectId) return null;

  const staleCount = entries.filter(e => e.stale).length;
  const ineffectiveCount = entries.filter(e => e.reference_count >= 10 && e.confirmed_count === 0).length;

  return (
    <div className="mb-4 bg-neutral-950 border border-neutral-800/60 rounded-xl overflow-hidden shrink-0">
      <button
        onClick={toggle}
        className="w-full flex items-center gap-2 px-4 py-3 text-left hover:bg-neutral-900/50 transition-colors"
      >
        <BookOpen className="w-4 h-4 text-violet-400 shrink-0" />
        <span className="font-medium text-sm text-neutral-200">Playbook Inspector</span>
        {entries.length > 0 && (
          <span className="ml-1 text-xs text-neutral-500">{entries.length} entries</span>
        )}
        {ineffectiveCount > 0 && (
          <span className="flex items-center gap-1 ml-1 text-xs text-amber-400">
            <AlertTriangle className="w-3 h-3" />{ineffectiveCount} ineffective
          </span>
        )}
        {staleCount > 0 && (
          <span className="ml-1 text-xs text-neutral-500">{staleCount} stale</span>
        )}
        <div className="flex-1" />
        {collapsed ? <ChevronDown className="w-4 h-4 text-neutral-500" /> : <ChevronUp className="w-4 h-4 text-neutral-500" />}
      </button>

      {!collapsed && (
        <div className="border-t border-neutral-800/60">
          {loading && entries.length === 0 && (
            <div className="px-4 py-6 text-center text-sm text-neutral-500">Loading…</div>
          )}
          {error && (
            <div className="px-4 py-2 text-xs text-red-400 bg-red-950/30">{error}</div>
          )}
          {!loading && entries.length === 0 && !error && (
            <div className="px-4 py-6 text-center text-sm text-neutral-500">No playbook entries yet.</div>
          )}
          {entries.length > 0 && (
            <div className="divide-y divide-neutral-800/40">
              {entries.map(entry => {
                const isIneffective = entry.reference_count >= 10 && entry.confirmed_count === 0;
                const isPending = pendingKey === entry.key;
                return (
                  <div key={entry.key} className={`px-4 py-3 ${entry.stale ? 'opacity-60' : ''} ${isIneffective ? 'bg-amber-950/10' : ''}`}>
                    <div className="flex items-start gap-3 min-w-0">
                      <div className="flex-1 min-w-0">
                        {/* Key + badges */}
                        <div className="flex items-center gap-2 flex-wrap mb-1">
                          <code className="text-xs font-mono text-violet-300 truncate max-w-[280px]">{entry.key}</code>
                          {entry.stale && (
                            <span className="text-xs px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400">stale</span>
                          )}
                          {isIneffective && !entry.stale && (
                            <span className="text-xs px-1.5 py-0.5 rounded bg-amber-900/50 text-amber-400">ineffective</span>
                          )}
                          {entry.source && SOURCE_LABELS[entry.source] && (
                            <span className="text-xs px-1.5 py-0.5 rounded bg-neutral-800/80 text-neutral-500">{SOURCE_LABELS[entry.source]}</span>
                          )}
                          {entry.applicable_types.length > 0 && (
                            <span className="text-xs text-neutral-500">{entry.applicable_types.join(', ')}</span>
                          )}
                        </div>
                        {/* Content snippet — intentionally clamped to 2 lines for compact layout */}
                        <p className="text-xs text-neutral-400 leading-relaxed line-clamp-2 mb-2" data-viewport-allow-truncation>{entry.content}</p>
                        {/* Stats row */}
                        <div className="flex items-center gap-4 flex-wrap">
                          <div className="flex items-center gap-1.5">
                            <span className="text-xs text-neutral-500">effectiveness</span>
                            <EffectivenessBar score={entry.effectiveness_score} />
                          </div>
                          <span className="text-xs text-neutral-500">
                            <span className="text-neutral-300">{entry.confirmed_count}</span> confirmed /&nbsp;
                            <span className="text-neutral-300">{entry.reference_count}</span> referenced
                          </span>
                          <span className="text-xs text-neutral-500">{entry.age_days}d old</span>
                        </div>
                      </div>
                      {/* Actions */}
                      <div className="flex items-center gap-1.5 shrink-0 mt-0.5">
                        <button
                          disabled={isPending}
                          onClick={() => doAction(entry.key, 'promote')}
                          title="Promote — increment confirmed count"
                          className="p-3 rounded hover:bg-emerald-900/40 text-neutral-500 hover:text-emerald-400 transition-colors disabled:opacity-40"
                        >
                          <ThumbsUp className="w-5 h-5" />
                        </button>
                        <button
                          disabled={isPending}
                          onClick={() => doAction(entry.key, 'demote')}
                          title="Demote — mark as stale/ineffective"
                          className="p-3 rounded hover:bg-red-900/40 text-neutral-500 hover:text-red-400 transition-colors disabled:opacity-40"
                        >
                          <ThumbsDown className="w-5 h-5" />
                        </button>
                        <button
                          disabled={isPending}
                          onClick={() => doAction(entry.key, 'archive')}
                          title="Archive — remove from active playbook"
                          className="p-3 rounded hover:bg-neutral-800 text-neutral-500 hover:text-neutral-300 transition-colors disabled:opacity-40"
                        >
                          <Archive className="w-5 h-5" />
                        </button>
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
