import { useState } from 'react';
import { apiFetch } from '../api';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { parseTimestamp, timeAgo } from '../utils/time';
import type { AccountHealthEntry } from './PoolAccountHealthPanel';

interface QuarantineStatus {
  kind: string;
  started_at: string | null;
  account_rate: number | null;
  project_median: number | null;
  sample_size: number | null;
}

interface AccountBreakdown {
  account_id: string;
  total_tasks: number;
  success_count: number;
  success_rate: number;
  below_median: boolean;
  quarantine_status: QuarantineStatus | null;
}

interface BreakdownRow {
  provider: string;
  model: string;
  total_tasks: number;
  success_count: number;
  success_rate: number;
  distinct_accounts: number;
  project_median_success_rate: number;
  accounts?: AccountBreakdown[];
}

function srColor(rate: number | null) {
  if (rate == null) return 'text-neutral-500';
  if (rate >= 0.8) return 'text-emerald-400';
  if (rate >= 0.5) return 'text-amber-400';
  return 'text-red-400';
}

function fmtPct(rate: number | null) {
  return rate != null ? `${Math.round(rate * 100)}%` : '—';
}

function fmtDate(iso: string | null) {
  if (!iso) return '—';
  try {
    return parseTimestamp(iso).toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch {
    return iso;
  }
}

function ModelSummary({ rows }: { rows: BreakdownRow[] }) {
  const totalTasks = rows.reduce((sum, row) => sum + row.total_tasks, 0);
  const totalAccounts = rows.reduce((sum, row) => sum + row.distinct_accounts, 0);
  const quarantinedAccounts = rows.reduce(
    (sum, row) => sum + (row.accounts ?? []).filter(account => account.quarantine_status).length,
    0,
  );
  const belowMedianAccounts = rows.reduce(
    (sum, row) => sum + (row.accounts ?? []).filter(account => account.below_median).length,
    0,
  );

  return (
    <div
      data-testid="provider-model-summary"
      className="mb-2 grid grid-cols-2 overflow-hidden rounded-md border border-neutral-800/60 bg-workbench-shell/30 sm:grid-cols-4"
    >
      <div className="border-r border-b border-neutral-800/60 px-2.5 py-2 sm:border-b-0">
        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Rows</div>
        <div className="mt-0.5 font-mono text-xs font-semibold text-neutral-200">{rows.length}</div>
      </div>
      <div className="border-r-0 border-b border-neutral-800/60 px-2.5 py-2 sm:border-r sm:border-b-0">
        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Tasks</div>
        <div className="mt-0.5 font-mono text-xs font-semibold text-neutral-200">{totalTasks}</div>
      </div>
      <div className="border-r border-neutral-800/60 px-2.5 py-2">
        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Accounts</div>
        <div className="mt-0.5 font-mono text-xs font-semibold text-neutral-200">{totalAccounts}</div>
        <div className="mt-0.5 truncate text-ui-sm text-neutral-600">{belowMedianAccounts} under median</div>
      </div>
      <div className="px-2.5 py-2">
        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Quarantine</div>
        <div className="mt-0.5 font-mono text-xs font-semibold text-amber-300">{quarantinedAccounts}</div>
      </div>
    </div>
  );
}

function QuarantineBadge({
  qs,
  accountId,
  provider,
  model,
  onCleared,
}: {
  qs: QuarantineStatus;
  accountId: string;
  provider: string;
  model: string;
  onCleared: () => void;
}) {
  const [confirming, setConfirming] = useState(false);
  const [clearing, setClearing] = useState(false);

  const doClear = async () => {
    setClearing(true);
    try {
      const params = new URLSearchParams({ provider, model });
      await apiFetch(
        `/api/admin/max-plan-accounts/${accountId}/regression-quarantine/clear?${params}`,
        { method: 'POST' },
      );
      onCleared();
    } finally {
      setClearing(false);
      setConfirming(false);
    }
  };

  return (
    <div className="flex items-center gap-1.5 flex-wrap">
      <span className="text-ui-xs font-semibold px-1.5 py-0.5 rounded border bg-orange-500/20 text-orange-400 border-orange-500/30 uppercase">
        quarantine:{qs.kind}
      </span>
      {qs.started_at && (
        <span className="text-ui-xs text-neutral-500">{fmtDate(qs.started_at)}</span>
      )}
      {qs.account_rate != null && qs.project_median != null && (
        <span className="text-ui-xs text-neutral-500 font-mono">
          {fmtPct(qs.account_rate)} vs {fmtPct(qs.project_median)} median
        </span>
      )}
      {qs.kind === 'regression' && (
        confirming ? (
          <span className="flex items-center gap-1">
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button
              onClick={doClear}
              disabled={clearing}
              className="text-ui-xs px-1.5 py-0.5 rounded bg-orange-500/30 text-orange-300 hover:bg-orange-500/50 transition-colors disabled:opacity-50"
            >
              {clearing ? 'Clearing…' : 'Confirm clear'}
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button
              onClick={() => setConfirming(false)}
              className="text-ui-xs px-1.5 py-0.5 rounded bg-workbench-layer/50 text-neutral-400 hover:bg-workbench-hover transition-colors"
            >
              Cancel
            </button>
          </span>
        ) : (
          // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
          <button
            onClick={() => setConfirming(true)}
            className="text-ui-xs px-1.5 py-0.5 rounded bg-workbench-layer/50 text-neutral-400 hover:text-orange-300 hover:bg-orange-500/20 transition-colors"
          >
            Clear quarantine
          </button>
        )
      )}
    </div>
  );
}

export function ProviderModelBreakdownPanel({
  selectedProjectId,
  accounts: healthAccounts,
}: {
  selectedProjectId: string | null;
  accounts?: AccountHealthEntry[] | null;
}) {
  const [rows, setRows] = useState<BreakdownRow[]>([]);
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  const [hideSingleAccount, setHideSingleAccount] = useState(false);
  const [loading, setLoading] = useState(true);
  // Poll-failure / response-staleness tracking. Previously load() swallowed
  // failures silently, so a persistent outage left the last-good rows rendered
  // as current indefinitely and an initial failure unmounted the panel.
  const [pollError, setPollError] = useState(false);
  const [hasSucceeded, setHasSucceeded] = useState(false);
  const [servedStale, setServedStale] = useState(false);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);

  const labelFor = (accountId: string) =>
    healthAccounts?.find(a => a.account_id === accountId)?.label ?? accountId.slice(0, 12);

  const load = async () => {
    try {
      const params = new URLSearchParams({ expand_accounts: 'true' });
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`/api/admin/max-plan-accounts/provider-model-breakdown?${params}`);
      if (res.ok) {
        const json = await res.json();
        setRows(json.rows ?? []);
        setServedStale(json.served_stale === true);
        setGeneratedAt(typeof json.generated_at === 'string' ? json.generated_at : null);
        setPollError(false);
        setHasSucceeded(true);
      } else {
        setPollError(true);
      }
    } catch {
      setPollError(true);
    } finally {
      setLoading(false);
    }
  };

  usePoller(load, { intervalMs: DEFAULT_POLL_MS, deps: [selectedProjectId] });

  // Initial fetch still in flight (no success and no failure yet) — render nothing.
  if (loading && !hasSucceeded && !pollError) return null;

  // Initial fetch FAILED before any success — surface an explicit error instead
  // of silently unmounting, so the operator sees the panel is not just empty.
  if (!hasSucceeded && pollError) {
    return (
      <section>
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
          Provider / Model Breakdown
        </h2>
        <div className="bg-workbench-base/50 border border-red-500/30 rounded-lg px-3 py-2 text-ui-md text-red-300">
          Could not load the provider/model breakdown — the endpoint is unreachable. Retrying…
        </div>
      </section>
    );
  }

  // Succeeded at least once but the data set is genuinely empty — nothing to show.
  if (rows.length === 0) return null;

  const visible = hideSingleAccount ? rows.filter(r => r.distinct_accounts >= 2) : rows;

  // A response served from the cache fallback, OR a poll that has started
  // failing after a prior success, means the rows below are last-good — not a
  // live reading. Mark them so the success-rate / quarantine rows are not
  // presented as current.
  const showStale = servedStale || pollError;
  const asOfLabel = generatedAt ? timeAgo(generatedAt) : null;

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <div className="flex items-center gap-2">
          <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">
            Provider / Model Breakdown
          </h2>
          {showStale && (
            <span
              className="text-ui-xs font-semibold px-1.5 py-0.5 rounded border border-amber-500/40 bg-amber-500/15 text-amber-300"
              title={
                pollError
                  ? `Polling is failing — these rows are the last-good reading${asOfLabel ? ` from ${asOfLabel}` : ''}`
                  : `Served from cache fallback — these rows are not a live reading${asOfLabel ? `; data is from ${asOfLabel}` : ''}`
              }
            >
              stale{asOfLabel ? ` · as of ${asOfLabel}` : ''}
            </span>
          )}
        </div>
        {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
        <button
          onClick={() => setHideSingleAccount(v => !v)}
          className={`text-ui-xs px-1.5 py-0.5 rounded border transition-colors ${
            hideSingleAccount
              ? 'bg-workbench-layer/50 text-neutral-300 border-neutral-600/50'
              : 'text-neutral-500 border-neutral-800/50 hover:text-neutral-300'
          }`}
        >
          {hideSingleAccount ? 'Show all' : 'Hide single-account'}
        </button>
      </div>
      <ModelSummary rows={visible} />
      {visible.length === 0 ? (
        <div
          data-testid="provider-model-empty-filter"
          className="rounded-lg border border-neutral-800/50 bg-workbench-base/50 px-3 py-4 text-center text-xs text-neutral-500"
        >
          No multi-account provider/model rows in this window.
        </div>
      ) : (
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {visible.map(row => {
            const key = `${row.provider}::${row.model}`;
            const expanded = expandedKey === key;
            return (
              <div key={key}>
                <div
                  className="px-3 py-2 text-ui-sm cursor-pointer"
                  onClick={() => setExpandedKey(expanded ? null : key)}
                >
                  <div className="flex items-start justify-between gap-2 mb-1">
                    <span className="font-medium text-neutral-200 break-words">{row.model}</span>
                    <span className="text-neutral-500 shrink-0">{row.provider}</span>
                  </div>
                  <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                    <span>Tasks: <span className="text-neutral-300 tabular-nums">{row.total_tasks}</span></span>
                    <span>Rate: <span className={`tabular-nums ${srColor(row.success_rate)}`}>{fmtPct(row.success_rate)}</span></span>
                    <span>Median: <span className={`tabular-nums ${srColor(row.project_median_success_rate)}`}>{fmtPct(row.project_median_success_rate)}</span></span>
                    <span>Accts: <span className="text-neutral-400 tabular-nums">{row.distinct_accounts}</span></span>
                  </div>
                </div>
                {expanded && row.accounts && row.accounts.length > 0 && (
                  <div className="px-3 pb-3 pt-1 bg-workbench-shell/40">
                    <div className="space-y-1.5">
                      {row.accounts.map(acct => (
                        <div key={acct.account_id} className="rounded border border-neutral-800/50 bg-workbench-base/60 px-2.5 py-1.5">
                          <div className="flex items-center gap-2 flex-wrap">
                            <span className="text-ui-sm font-medium text-neutral-300">
                              {labelFor(acct.account_id)}
                            </span>
                            <span className="text-ui-xs text-neutral-500 font-mono tabular-nums">
                              {acct.total_tasks} tasks
                            </span>
                            <span className={`text-ui-sm font-mono font-bold tabular-nums ${srColor(acct.success_rate)}`}>
                              {fmtPct(acct.success_rate)}
                            </span>
                            {acct.below_median && (
                              <span className="text-ui-xs font-semibold px-1.5 py-0.5 rounded border bg-amber-500/20 text-amber-400 border-amber-500/30 uppercase">
                                below median
                              </span>
                            )}
                          </div>
                          {acct.quarantine_status && (
                            <div className="mt-1">
                              <QuarantineBadge
                                qs={acct.quarantine_status}
                                accountId={acct.account_id}
                                provider={row.provider}
                                model={row.model}
                                onCleared={load}
                              />
                            </div>
                          )}
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            );
          })}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-xs">
          <thead>
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">Provider / Model</th>
              <th className="text-right px-3 py-2 font-medium">Tasks</th>
              <th className="text-right px-3 py-2 font-medium">Rate</th>
              <th className="text-right px-3 py-2 font-medium">Median</th>
              <th className="text-right px-3 py-2 font-medium">Accts</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {visible.map(row => {
              const key = `${row.provider}::${row.model}`;
              const expanded = expandedKey === key;
              return [
                <tr
                  key={key}
                  className="cursor-pointer hover:bg-workbench-hover/20 transition-colors"
                  onClick={() => setExpandedKey(expanded ? null : key)}
                >
                  <td className="px-3 py-2">
                    <span className="font-medium text-neutral-200">{row.model}</span>
                    <span className="text-neutral-500 ml-1.5 text-ui-sm">{row.provider}</span>
                  </td>
                  <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{row.total_tasks}</td>
                  <td className={`text-right px-3 py-2 tabular-nums ${srColor(row.success_rate)}`}>
                    {fmtPct(row.success_rate)}
                  </td>
                  <td className={`text-right px-3 py-2 tabular-nums ${srColor(row.project_median_success_rate)}`}>
                    {fmtPct(row.project_median_success_rate)}
                  </td>
                  <td className="text-right px-3 py-2 text-neutral-400 tabular-nums">{row.distinct_accounts}</td>
                </tr>,
                expanded && row.accounts && row.accounts.length > 0 && (
                  <tr key={`${key}__expand`}>
                    <td colSpan={5} className="px-3 pb-3 pt-1 bg-workbench-shell/40">
                      <div className="space-y-1.5">
                        {row.accounts.map(acct => (
                          <div key={acct.account_id} className="rounded border border-neutral-800/50 bg-workbench-base/60 px-2.5 py-1.5">
                            <div className="flex items-center gap-2 flex-wrap">
                              <span className="text-ui-sm font-medium text-neutral-300">
                                {labelFor(acct.account_id)}
                              </span>
                              <span className="text-ui-xs text-neutral-500 font-mono tabular-nums">
                                {acct.total_tasks} tasks
                              </span>
                              <span className={`text-ui-sm font-mono font-bold tabular-nums ${srColor(acct.success_rate)}`}>
                                {fmtPct(acct.success_rate)}
                              </span>
                              {acct.below_median && (
                                <span className="text-ui-xs font-semibold px-1.5 py-0.5 rounded border bg-amber-500/20 text-amber-400 border-amber-500/30 uppercase">
                                  below median
                                </span>
                              )}
                            </div>
                            {acct.quarantine_status && (
                              <div className="mt-1">
                                <QuarantineBadge
                                  qs={acct.quarantine_status}
                                  accountId={acct.account_id}
                                  provider={row.provider}
                                  model={row.model}
                                  onCleared={load}
                                />
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    </td>
                  </tr>
                ),
              ];
            })}
          </tbody>
        </table>
      </div>
      )}
    </section>
  );
}
