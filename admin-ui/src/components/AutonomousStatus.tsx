import { useState, useEffect, useCallback, useRef, type RefObject } from 'react';
import { Zap, ZapOff, AlertTriangle, RefreshCw } from 'lucide-react';
import { API_BASE_URL, authHeaders } from '../api';

interface ProjectAutonomousStatus {
  project_id: string;
  project_name: string;
  autonomous_enabled: boolean;
  consecutive_failures: number;
  last_research_at: string | null;
  tasks_spawned_last_hour: number;
  rate_limit: number;
}

interface AutonomousStatusProps {
  onRefreshTasks?: () => void;
  wsRef?: RefObject<WebSocket | null>;
}

export function AutonomousStatus({ onRefreshTasks, wsRef }: AutonomousStatusProps) {
  const [projects, setProjects] = useState<ProjectAutonomousStatus[]>([]);
  const [expanded, setExpanded] = useState(false);
  const [toggling, setToggling] = useState<string | null>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  // authHeaders is stable; fetchStatus is intentionally run only on mount
  const fetchStatus = useCallback(async () => {
    try {
      const headers = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/autonomous-status`, {
        headers: { Authorization: headers.Authorization },
      });
      if (res.ok) {
        const data = await res.json();
        setProjects(data.projects || []);
      }
    } catch {
      // silent
    }
  }, []);

  useEffect(() => {
    fetchStatus();
    const interval = setInterval(fetchStatus, 30000);
    return () => clearInterval(interval);
  }, [fetchStatus]);

  // Close dropdown on outside click (matches Settings pattern in App.tsx)
  useEffect(() => {
    if (!expanded) return;
    const handleClick = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setExpanded(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [expanded]);

  // Subscribe to WebSocket autonomous events for real-time updates.
  // We poll wsRef.current on an interval because ref mutations don't trigger
  // re-renders, so a dependency on wsRef.current would miss reconnections.
  useEffect(() => {
    if (!wsRef) return;
    let attachedWs: WebSocket | null = null;

    const handleMessage = (event: MessageEvent) => {
      try {
        const msg = JSON.parse(event.data);
        if (msg.event === 'autonomous_activity' || msg.event === 'autonomous_paused') {
          fetchStatus();
        }
      } catch {
        // ignore parse errors
      }
    };

    const attach = () => {
      const currentWs = wsRef.current;
      if (currentWs === attachedWs) return;
      if (attachedWs) {
        attachedWs.removeEventListener('message', handleMessage);
      }
      attachedWs = currentWs;
      if (currentWs) {
        currentWs.addEventListener('message', handleMessage);
      }
    };

    attach();
    const interval = setInterval(attach, 5000);

    return () => {
      clearInterval(interval);
      if (attachedWs) {
        attachedWs.removeEventListener('message', handleMessage);
      }
    };
  }, [wsRef, fetchStatus]);

  const toggleAutonomy = async (projectId: string, enabled: boolean) => {
    setToggling(projectId);
    try {
      const headers = await authHeaders();
      await fetch(`${API_BASE_URL}/api/admin/projects/${projectId}/autonomous`, {
        method: 'PATCH',
        headers: { ...headers, 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      });
      await fetchStatus();
      onRefreshTasks?.();
    } catch {
      // silent
    } finally {
      setToggling(null);
    }
  };

  const anyEnabled = projects.some(p => p.autonomous_enabled);
  const anyCircuitBreaker = projects.some(p => p.consecutive_failures >= 2);

  const formatTimeAgo = (iso: string) => {
    const diff = Date.now() - new Date(iso).getTime();
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return `${hrs}h ago`;
    return `${Math.floor(hrs / 24)}d ago`;
  };

  return (
    <div className="relative" ref={containerRef}>
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex flex-col items-center cursor-pointer group"
        title="Autonomous Pipeline Status"
      >
        <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Autonomy</span>
        <span className="text-xl font-bold font-mono leading-none flex items-center gap-1.5">
          {anyEnabled ? (
            <>
              <Zap className={`w-4 h-4 ${anyCircuitBreaker ? 'text-amber-400' : 'text-emerald-400'}`} />
              <span className={anyCircuitBreaker ? 'text-amber-400' : 'text-emerald-400'}>
                {anyCircuitBreaker ? 'Warn' : 'Active'}
              </span>
            </>
          ) : (
            <>
              <ZapOff className="w-4 h-4 text-neutral-500" />
              <span className="text-neutral-500">Off</span>
            </>
          )}
        </span>
      </button>

      {expanded && (
        <div className="absolute top-full mt-2 right-0 w-80 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl p-4 z-50 max-h-[60vh] overflow-y-auto custom-scrollbar">
          <div className="flex items-center justify-between mb-3">
            <h3 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider">Autonomous Pipeline</h3>
            <button onClick={fetchStatus} className="text-neutral-500 hover:text-neutral-300 transition-colors">
              <RefreshCw className="w-3 h-3" />
            </button>
          </div>

          {projects.length === 0 && (
            <p className="text-xs text-neutral-600">No projects found</p>
          )}

          {projects.map(p => (
            <div key={p.project_id} className="mb-3 last:mb-0 bg-neutral-950 border border-neutral-800 rounded-lg p-3">
              <div className="flex items-center justify-between mb-2">
                <span className="text-xs font-medium text-neutral-200 truncate max-w-[180px]">{p.project_name}</span>
                <button
                  onClick={() => toggleAutonomy(p.project_id, !p.autonomous_enabled)}
                  disabled={toggling === p.project_id}
                  className={`text-[10px] font-semibold uppercase tracking-wider px-2 py-0.5 rounded transition-colors ${
                    p.autonomous_enabled
                      ? 'bg-emerald-500/10 text-emerald-400 hover:bg-emerald-500/20'
                      : 'bg-neutral-800 text-neutral-500 hover:bg-neutral-700'
                  } ${toggling === p.project_id ? 'opacity-50' : ''}`}
                >
                  {p.autonomous_enabled ? 'Enabled' : 'Disabled'}
                </button>
              </div>

              <div className="space-y-1.5">
                {/* Circuit breaker */}
                {p.consecutive_failures > 0 && (
                  <div className="flex items-center gap-1.5 text-[11px]">
                    <AlertTriangle className={`w-3 h-3 shrink-0 ${p.consecutive_failures >= 3 ? 'text-red-400' : 'text-amber-400'}`} />
                    <span className={p.consecutive_failures >= 3 ? 'text-red-400' : 'text-amber-400'}>
                      {p.consecutive_failures} consecutive failure{p.consecutive_failures !== 1 ? 's' : ''}
                      {p.consecutive_failures >= 3 && ' — circuit breaker tripped'}
                    </span>
                  </div>
                )}

                {/* Last research */}
                <div className="flex items-center justify-between text-[11px] text-neutral-500">
                  <span>Last research</span>
                  <span className="font-mono">{p.last_research_at ? formatTimeAgo(p.last_research_at) : 'never'}</span>
                </div>

                {/* Rate limit usage */}
                <div className="flex items-center justify-between text-[11px] text-neutral-500">
                  <span>Tasks this hour</span>
                  <span className="font-mono">
                    <span className={p.tasks_spawned_last_hour >= p.rate_limit ? 'text-amber-400' : ''}>
                      {p.tasks_spawned_last_hour}
                    </span>
                    /{p.rate_limit}
                  </span>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
