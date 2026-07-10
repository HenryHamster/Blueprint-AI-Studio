import type { ReactNode } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Skeleton } from './DashboardMetricCardsPrimitives';
import { snapshotFreshness } from '../utils/time';
import type { AgentServingShasResponse, FleetUtilizationResponse } from '../api';

export type { FleetUtilizationResponse };

const SNAPSHOT_STALE_AFTER_SECONDS = 120;
const STALE_CLASSES = 'opacity-60 saturate-50';

const _SAT_STYLE: Record<string, { badge: string; label: string }> = {
  healthy:   { badge: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400', label: 'Healthy' },
  busy:      { badge: 'bg-amber-500/10 border-amber-500/30 text-amber-400',       label: 'Busy' },
  saturated: { badge: 'bg-red-500/10 border-red-500/30 text-red-400',             label: 'Saturated' },
};
const _SAT_RANK: Record<NonNullable<FleetUtilizationResponse['saturation']>, number> = {
  healthy: 0,
  busy: 1,
  saturated: 2,
};

function pct(v: number | null | undefined): string {
  if (v == null) return '—';
  return `${Math.round(v * 100)}%`;
}

/** Render a VM count: amber "unknown" when null, the number when known (including 0). */
function renderCount(v: number | null | undefined): ReactNode {
  if (v == null) return <span className="text-amber-400">unknown</span>;
  return <>{v}</>;
}

function shortSha(sha: string | null | undefined): string {
  return sha ? sha.slice(0, 12) : 'unknown';
}

function formatBuildDate(epochSeconds: number | null | undefined): string {
  if (epochSeconds == null) return 'unknown build';
  return new Date(epochSeconds * 1000).toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  });
}

export function FleetCapacityCard({
  data,
  agentServingShas,
  loading,
}: {
  data: FleetUtilizationResponse | null;
  agentServingShas?: AgentServingShasResponse | null;
  loading: boolean;
}) {
  const satStyle = data?.saturation ? (_SAT_STYLE[data.saturation] ?? _SAT_STYLE.healthy) : null;
  const vmSatStyle = data?.vm_saturation ? (_SAT_STYLE[data.vm_saturation] ?? _SAT_STYLE.healthy) : null;
  const headerSatStyle = data
    ? (
        data.vm_saturation && (!data.saturation || _SAT_RANK[data.vm_saturation] >= _SAT_RANK[data.saturation])
          ? vmSatStyle
          : satStyle
      )
    : null;
  const cooldownNote = data && data.cooled_accounts > 0
    ? `${data.cooled_accounts} account${data.cooled_accounts > 1 ? 's' : ''} rate-limited`
    : null;
  const shaBuckets = agentServingShas?.fleet.buckets ?? [];
  const unknownCount = agentServingShas?.fleet.unknown_count ?? 0;
  const { asOfLabel, stale: freshnessStale } = snapshotFreshness(
    data?.precomputed_at,
    data?.precompute_age_seconds,
    SNAPSHOT_STALE_AFTER_SECONDS,
  );
  const stale = freshnessStale || data?.served_stale === true;

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">Fleet Capacity</h2>
        {!loading && data && headerSatStyle && (
          <span className={`text-ui-sm font-semibold px-2 py-0.5 rounded border ${headerSatStyle.badge}`}>
            {headerSatStyle.label}
          </span>
        )}
        {!loading && data && data.saturation == null && (
          <span className="text-ui-sm font-semibold px-2 py-0.5 rounded border bg-neutral-800/50 border-neutral-700/50 text-neutral-500">
            Unknown
          </span>
        )}
        {!loading && data && asOfLabel && (
          <span
            className={`text-ui-sm ${stale ? 'text-amber-400 font-semibold' : 'text-neutral-500'}`}
            title={stale ? 'Cached snapshot is stale — may not reflect current fleet capacity' : 'When this fleet-capacity snapshot was computed'}
          >
            {stale && '⚠ '}as of {asOfLabel}{stale && ' · stale'}
          </span>
        )}
      </div>
      {loading ? (
        <div className="space-y-2"><Skeleton className="h-10 w-full" /></div>
      ) : !data ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 flex items-center gap-2 text-neutral-400 text-xs">
          <AlertTriangle className="w-4 h-4" />
          Fleet capacity data unavailable
        </div>
      ) : (
        <div className={`bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 space-y-2 ${stale ? STALE_CLASSES : ''}`}>
          <div className="flex flex-wrap gap-2">
            <div className="flex items-center gap-1.5 text-xs">
              <span className="text-neutral-500">Active</span>
              <span className="font-mono font-bold text-neutral-200">
                {data.active_now}
                {data.nominal_capacity != null ? ` / ${data.nominal_capacity}` : ''}
              </span>
            </div>
            {data.usable_capacity != null && data.cooled_accounts > 0 && (
              <div className="flex items-center gap-1.5 text-xs">
                <span className="text-neutral-500">Usable</span>
                <span className="font-mono text-amber-400" title={`${data.cooled_accounts} account(s) cooling down`}>
                  {data.usable_capacity}
                </span>
              </div>
            )}
            <div className="flex items-center gap-1.5 text-xs">
              <span className="text-neutral-500">Avg util</span>
              <span className="font-mono text-neutral-300">{pct(data.util_avg)}</span>
            </div>
            <div className="flex items-center gap-1.5 text-xs">
              <span className="text-neutral-500">Now</span>
              <span className="font-mono text-neutral-300">{pct(data.util_now)}</span>
            </div>
          </div>
          {cooldownNote && (
            <p className="text-ui-sm text-amber-400/80">{cooldownNote} — usable capacity is lower than nominal</p>
          )}
          {data.vm_cap != null && (
            <div className="border-t border-neutral-800/70 pt-2 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <h3 className="text-ui-sm font-bold text-neutral-500 uppercase tracking-widest">VM Slots</h3>
                {vmSatStyle && (
                  <span className={`text-ui-sm font-semibold px-1.5 py-0.5 rounded border ${vmSatStyle.badge}`}>
                    {vmSatStyle.label}
                  </span>
                )}
              </div>
              <div className="grid gap-x-3 gap-y-1 text-ui-sm sm:grid-cols-2">
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">VM cap</span>
                  <span className="font-mono text-neutral-300 text-right">{data.vm_cap ?? '—'}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">Active task VMs</span>
                  <span className="font-mono text-neutral-300 text-right">{renderCount(data.active_task_vms)}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">Managed VM inventory</span>
                  <span className="font-mono text-neutral-300 text-right">{renderCount(data.managed_vm_count)}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">Idle pool adjustment</span>
                  <span className="font-mono text-neutral-300 text-right">{renderCount(data.idle_pool_vms)}</span>
                </div>
                <div className="flex justify-between gap-2 sm:col-span-2">
                  <span className="text-neutral-500">Slots available</span>
                  <span className="font-mono text-neutral-300 text-right">
                    {renderCount(data.vm_slots_available)}
                    {data.vm_saturation ? ` · ${_SAT_STYLE[data.vm_saturation]?.label ?? 'Unknown'}` : ''}
                  </span>
                </div>
              </div>
              {data.vm_saturation == null && (
                <p className="text-ui-sm text-amber-400/80">VM measurements partially unavailable — slot saturation unknown</p>
              )}
            </div>
          )}
          {data.saturation == null && (
            <p className="text-ui-sm text-neutral-500">No enabled accounts — capacity unknown</p>
          )}
          {agentServingShas && (
            <div className="border-t border-neutral-800/70 pt-2 space-y-2">
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center gap-2">
                  <h3 className="text-ui-sm font-bold text-neutral-500 uppercase tracking-widest">Agent Image</h3>
                  {agentServingShas.fleet.mixed && (
                    <span className="text-ui-sm font-semibold px-1.5 py-0.5 rounded border bg-amber-500/10 border-amber-500/30 text-amber-400">
                      Mixed rollover
                    </span>
                  )}
                </div>
                <span className="text-ui-sm text-neutral-500">
                  {agentServingShas.fleet.total_observed} observed
                </span>
              </div>
              <div className="grid gap-1.5 sm:grid-cols-2">
                {shaBuckets.length === 0 ? (
                  <p className="text-ui-sm text-neutral-500 sm:col-span-2">No agent image heartbeat data yet</p>
                ) : shaBuckets.map(bucket => (
                  <div
                    key={bucket.source_sha ?? 'unknown'}
                    className="rounded border border-neutral-800/70 bg-neutral-950/30 px-2 py-1.5"
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className={`font-mono text-ui-md ${bucket.source_sha ? 'text-neutral-200' : 'text-amber-400'}`}>
                        {shortSha(bucket.source_sha)}
                      </span>
                      <span className="text-ui-sm text-neutral-400">{bucket.count} VM{bucket.count === 1 ? '' : 's'}</span>
                    </div>
                    <div className="mt-1 flex flex-wrap gap-1 text-ui-xs">
                      {bucket.matches_backend_served && <span className="text-emerald-400">backend</span>}
                      {bucket.matches_current_image && <span className="text-sky-400">image</span>}
                      {bucket.matches_active_candidate && <span className="text-violet-400">candidate</span>}
                      {bucket.matches_integration_head && <span className="text-neutral-300">{agentServingShas.integration_branch ?? 'head'}</span>}
                      {!bucket.source_sha && <span className="text-amber-400">pre-marker</span>}
                    </div>
                  </div>
                ))}
              </div>
              <div className="grid gap-x-3 gap-y-1 text-ui-sm sm:grid-cols-2">
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">Current image</span>
                  <span className="font-mono text-neutral-300 text-right">
                    {shortSha(agentServingShas.current_image?.source_git_sha)}
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">Image bake</span>
                  <span className="text-neutral-300 text-right">
                    {formatBuildDate(agentServingShas.current_image?.build_ts)}
                  </span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">Backend served</span>
                  <span className="font-mono text-neutral-300 text-right">{shortSha(agentServingShas.backend_served_sha)}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">Candidate</span>
                  <span className="font-mono text-neutral-300 text-right">{shortSha(agentServingShas.active_candidate_sha)}</span>
                </div>
                <div className="flex justify-between gap-2">
                  <span className="text-neutral-500">{agentServingShas.integration_branch ?? 'Integration'}</span>
                  <span className="font-mono text-neutral-300 text-right">{shortSha(agentServingShas.integration_head_sha)}</span>
                </div>
                {unknownCount > 0 && (
                  <div className="flex justify-between gap-2">
                    <span className="text-neutral-500">Unknown</span>
                    <span className="text-amber-400 text-right">{unknownCount} VM{unknownCount === 1 ? '' : 's'}</span>
                  </div>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
