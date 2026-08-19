/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useState } from 'react';
import { RotateCcw } from 'lucide-react';
import { fetchProjectAlertDomains, updateProjectAlertDomains } from '../api';
import type { ProjectAlertDomain } from '../types/alert';

interface ProjectAlertDomainsSectionProps {
  projectId: string | null;
  canSave: boolean;
}

export function ProjectAlertDomainsSection({ projectId, canSave }: ProjectAlertDomainsSectionProps) {
  const [domains, setDomains] = useState<ProjectAlertDomain[]>([]);
  const [loading, setLoading] = useState(false);
  const [savingDomain, setSavingDomain] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      setDomains([]);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    fetchProjectAlertDomains(projectId, controller.signal)
      .then(data => setDomains(data.domains ?? []))
      .catch(err => {
        if (err instanceof Error && err.name === 'AbortError') return;
        setError(err instanceof Error ? err.message : 'Failed to load alert domains');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [projectId]);

  const applyUpdate = async (domain: string, enabled: boolean | null) => {
    if (!projectId) return;
    const previous = domains;
    setSavingDomain(domain);
    setError(null);
    if (enabled !== null) {
      setDomains(prev => prev.map(row => (
        row.domain === domain ? { ...row, enabled, configured: true } : row
      )));
    }
    try {
      const data = await updateProjectAlertDomains(
        projectId,
        { [domain]: enabled === null ? null : { enabled } },
      );
      setDomains(data.domains ?? []);
    } catch (err) {
      setDomains(previous);
      setError(err instanceof Error ? err.message : 'Failed to update alert domain');
    } finally {
      setSavingDomain(null);
    }
  };

  if (!projectId) {
    return <p className="text-xs text-neutral-500">Select a project to configure alert domains.</p>;
  }

  return (
    <div className="space-y-5">
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-2">Alert Domains</h3>
        <p className="text-ui-md text-neutral-600">
          Control which optional project alert domains can create operator alerts. Core alerts stay enabled.
        </p>
      </div>

      {loading && <p className="text-xs text-neutral-500">Loading alert domains...</p>}
      {error && (
        <div role="alert" className="text-ui-md text-red-400 bg-red-500/10 border border-red-500/30 rounded-md px-3 py-2">
          {error}
        </div>
      )}

      {!loading && domains.length === 0 && !error && (
        <p className="text-xs text-neutral-500">No alert domains are available for this project.</p>
      )}

      <div className="space-y-2">
        {domains.map(domain => {
          const busy = savingDomain === domain.domain;
          const locked = domain.core || !canSave || busy;
          return (
            <div key={domain.domain} className="bg-workbench-base border border-neutral-800 rounded-lg px-4 py-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-xs font-medium text-neutral-200">{domain.label}</span>
                    <span className="font-mono text-ui-sm text-neutral-500">{domain.domain}</span>
                    {domain.core && <span className="rounded border border-cyan-500/30 bg-cyan-500/10 px-1.5 py-0.5 text-ui-sm text-cyan-400">core</span>}
                    {!domain.configured && (
                      <span className="rounded border border-neutral-700 bg-workbench-shell px-1.5 py-0.5 text-ui-sm text-neutral-500">
                        default {domain.default_enabled ? 'on' : 'off'}
                      </span>
                    )}
                  </div>
                  <p className="mt-1 text-ui-md text-neutral-500">{domain.description}</p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {domain.configured && !domain.core && (
                    <button
                      data-viewport-allow-small-target
                      type="button"
                      onClick={() => void applyUpdate(domain.domain, null)}
                      disabled={!canSave || busy}
                      className="text-neutral-500 hover:text-neutral-300 disabled:opacity-40 transition-colors"
                      title="Reset to project default"
                    >
                      <RotateCcw className="h-3.5 w-3.5" />
                    </button>
                  )}
                  <button
                    data-viewport-allow-small-target
                    type="button"
                    onClick={() => void applyUpdate(domain.domain, !domain.enabled)}
                    disabled={locked}
                    className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 disabled:opacity-50 ${domain.enabled ? 'bg-emerald-600' : 'bg-workbench-layer'}`}
                    title={domain.core ? 'Core alert domains cannot be disabled' : undefined}
                    aria-label={`${domain.enabled ? 'Disable' : 'Enable'} ${domain.label}`}
                  >
                    <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${domain.enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
                  </button>
                </div>
              </div>
            </div>
          );
        })}
      </div>

      {!canSave && (
        <p className="text-xs text-neutral-500">Only project owners can change alert-domain settings.</p>
      )}
    </div>
  );
}
