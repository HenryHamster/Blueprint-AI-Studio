import { useEffect, useState } from 'react';
import { ChevronDown, ChevronUp, GitBranch, AlertTriangle } from 'lucide-react';
import { fetchNullBranchConfig, backfillProjectBranches, type NullBranchConfigEntry } from '../api';

interface NullBranchDiagnosticsPanelProps {
  onConfigure?: (projectId: string) => void;
}

export function NullBranchDiagnosticsPanel({ onConfigure }: NullBranchDiagnosticsPanelProps = {}) {
  const [projects, setProjects] = useState<NullBranchConfigEntry[]>([]);
  const [selfRepoSkipped, setSelfRepoSkipped] = useState<NullBranchConfigEntry[]>([]);
  const [loading, setLoading] = useState(true);
  const [selfRepoExpanded, setSelfRepoExpanded] = useState(false);
  const [backfilling, setBackfilling] = useState<Set<string>>(new Set());
  const [errors, setErrors] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const data = await fetchNullBranchConfig();
        if (!cancelled) {
          setProjects(data.projects);
          setSelfRepoSkipped(data.self_repo_skipped);
          setLoading(false);
        }
      } catch {
        if (!cancelled) setLoading(false);
      }
    };
    load();
    return () => { cancelled = true; };
  }, []);

  const handleBackfill = async (project: NullBranchConfigEntry) => {
    setBackfilling(prev => new Set(prev).add(project.id));
    setErrors(prev => Object.fromEntries(Object.entries(prev).filter(([k]) => k !== project.id)));
    try {
      await backfillProjectBranches(project.id);
      setProjects(prev => prev.filter(p => p.id !== project.id));
    } catch (e) {
      setErrors(prev => ({ ...prev, [project.id]: e instanceof Error ? e.message : 'Backfill failed' }));
    } finally {
      setBackfilling(prev => { const next = new Set(prev); next.delete(project.id); return next; });
    }
  };

  if (loading || (projects.length === 0 && selfRepoSkipped.length === 0)) return null;

  return (
    <div className="mb-3 shrink-0">
      <div className="flex items-center gap-2 text-ui-md font-bold text-neutral-400 uppercase tracking-widest px-1 py-1">
        <GitBranch className="w-3 h-3" />
        Branch Config
        {projects.length > 0 && (
          <span className="ml-1 bg-amber-500/20 border border-amber-500/40 text-amber-400 text-ui-sm font-bold px-1.5 py-0.5 rounded-full leading-none">
            {projects.length} need fix
          </span>
        )}
      </div>

      {projects.length > 0 && (
        <div className="mt-1 bg-workbench-base border border-amber-900/40 rounded-lg p-3 text-xs space-y-2">
          <div className="flex items-center gap-1.5 mb-2">
            <AlertTriangle className="w-3 h-3 text-amber-400" />
            <span className="text-amber-400 font-medium">
              {projects.length} project{projects.length !== 1 ? 's' : ''} missing branch config — falling back to &apos;main&apos;
            </span>
          </div>
          {projects.map(project => (
            <div key={project.id} className="flex flex-wrap items-center gap-2">
              <span className="text-neutral-300 font-medium">{project.name}</span>
              <span className="text-neutral-500 font-mono text-ui-sm">
                {project.github_owner}/{project.github_repo}
              </span>
              {errors[project.id] && (
                <span className="text-red-400 text-ui-sm">{errors[project.id]}</span>
              )}
              <div className="ml-auto flex items-center gap-1.5">
                {onConfigure && (
                  <button
                    onClick={() => onConfigure(project.id)}
                    className="px-2 min-h-[44px] rounded text-ui-sm font-medium bg-workbench-panel border border-neutral-700 text-neutral-300 hover:bg-workbench-hover transition-colors"
                  >
                    Configure
                  </button>
                )}
                <button
                  onClick={() => handleBackfill(project)}
                  disabled={backfilling.has(project.id)}
                  className="px-2 min-h-[44px] rounded text-ui-sm font-medium bg-blue-500/10 border border-blue-500/30 text-blue-400 hover:bg-blue-500/20 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                >
                  {backfilling.has(project.id) ? 'Backfilling…' : 'Backfill'}
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      {selfRepoSkipped.length > 0 && (
        <div className="mt-1">
          <button
            onClick={() => setSelfRepoExpanded(e => !e)}
            className="flex items-center gap-1.5 text-ui-sm text-neutral-500 hover:text-neutral-400 transition-colors px-1 min-h-[44px]"
          >
            {selfRepoExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
            {selfRepoSkipped.length} self-repo skipped (intentional)
          </button>
          {selfRepoExpanded && (
            <div className="mt-1 bg-workbench-base border border-neutral-800 rounded-lg p-2 text-xs space-y-1">
              {selfRepoSkipped.map(project => (
                <div key={project.id} className="flex items-center gap-2 text-neutral-500">
                  <span className="font-medium text-neutral-400">{project.name}</span>
                  <span className="font-mono text-ui-sm">{project.github_owner}/{project.github_repo}</span>
                  <span className="text-ui-sm text-neutral-600">LTS-managed</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
