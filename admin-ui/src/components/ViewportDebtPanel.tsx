import { useState, useEffect } from 'react';
import { ExternalLink, CheckCircle, XCircle } from 'lucide-react';
import { apiFetch } from '../api';
import { isSafeUrl } from './TaskCard';

export interface ViewportDebtRow {
  component: string;
  eslint_disable_count: number;
  data_viewport_allow_small_target: number;
  data_viewport_allow_overflow: number;
  data_viewport_allow_truncation: number;
  total_opt_outs: number;
  budget_cap?: number | null;
  headroom?: number | null;
  has_viewport_test: boolean;
  regression_case_count: number;
  regression_case_breakdown?: Record<string, number>;
  source_url: string | null;
  introducing_pr_number?: number | null;
  introducing_commit_sha?: string | null;
}

interface CoverageSummary {
  total_components: number;
  untested_count: number;
  untested_components: string[];
  total_opt_outs?: number;
  total_budget?: number | null;
  budget_headroom?: number | null;
}

interface ViewportDebtResponse {
  rows: ViewportDebtRow[];
  untested_rows?: ViewportDebtRow[];
  scanned_at: string;
  coverage_summary?: CoverageSummary;
}

function formatBreakdown(breakdown: Record<string, number>): string {
  return Object.entries(breakdown)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([type, count]) => count + ' ' + type)
    .join(', ');
}

function renderMobileDebtRow(row: ViewportDebtRow) {
  const shortName = row.component
    .replace(/^admin-ui\/src\/components\//, '')
    .replace(/\.tsx$/, '');
  return (
    <div key={row.component} className="px-3 py-2 text-ui-sm">
      <div className="flex items-start justify-between gap-2 mb-1">
        <span className="font-mono text-neutral-300 break-all" title={row.component}>{shortName}</span>
        <div className="flex items-center gap-1 shrink-0">
          {row.has_viewport_test ? (
            <CheckCircle className="w-3 h-3 text-emerald-400" />
          ) : (
            <XCircle className="w-3 h-3 text-red-400" />
          )}
          {row.source_url && isSafeUrl(row.source_url) && (
            <a href={row.source_url} target="_blank" rel="noopener noreferrer"
              className="text-neutral-500 hover:text-neutral-300 inline-flex items-center justify-center min-h-[44px] min-w-[44px]"
              aria-label={`Open source for ${shortName}`}>
              <ExternalLink className="w-3 h-3" />
            </a>
          )}
        </div>
      </div>
      <div className="grid grid-cols-3 gap-x-2 gap-y-0.5 text-neutral-500">
        <span>eslint: <span className={row.eslint_disable_count > 0 ? 'text-amber-400 font-medium' : 'text-neutral-600'}>{row.eslint_disable_count}</span></span>
        <span>target: <span className={row.data_viewport_allow_small_target > 0 ? 'text-amber-400 font-medium' : 'text-neutral-600'}>{row.data_viewport_allow_small_target}</span></span>
        <span>overflow: <span className={row.data_viewport_allow_overflow > 0 ? 'text-amber-400 font-medium' : 'text-neutral-600'}>{row.data_viewport_allow_overflow}</span></span>
        <span>trunc: <span className={row.data_viewport_allow_truncation > 0 ? 'text-amber-400 font-medium' : 'text-neutral-600'}>{row.data_viewport_allow_truncation}</span></span>
        <span className="col-span-2">total: {row.budget_cap != null ? (
          <span className={row.headroom != null && row.headroom <= 0 ? 'text-red-400 font-semibold' : row.headroom != null && row.headroom <= 2 ? 'text-amber-400 font-semibold' : 'text-white font-semibold'}>
            {row.total_opt_outs}/{row.budget_cap}
          </span>
        ) : (
          <span className={row.total_opt_outs > 0 ? 'text-white font-semibold' : 'text-neutral-600'}>{row.total_opt_outs} <span className="text-neutral-600">(uncapped)</span></span>
        )}
          {row.regression_case_count > 0 && <span className="ml-2 text-red-400">{row.regression_case_count} case{row.regression_case_count !== 1 ? 's' : ''}</span>}
        </span>
      </div>
    </div>
  );
}

function renderDebtRow(row: ViewportDebtRow) {
  const shortName = row.component
    .replace(/^admin-ui\/src\/components\//, '')
    .replace(/\.tsx$/, '');
  return (
    <tr key={row.component} className="hover:bg-workbench-hover/20 transition-colors">
      <td
        className="px-3 py-1.5 font-mono text-neutral-300 min-w-0 max-w-[160px] truncate"
        title={row.component}
      >
        {shortName}
      </td>
      <td className="px-2 py-1.5 text-right">
        <span className={row.eslint_disable_count > 0 ? 'text-amber-400 font-medium' : 'text-neutral-600'}>
          {row.eslint_disable_count}
        </span>
      </td>
      <td className="px-2 py-1.5 text-right">
        <span className={row.data_viewport_allow_small_target > 0 ? 'text-amber-400 font-medium' : 'text-neutral-600'}>
          {row.data_viewport_allow_small_target}
        </span>
      </td>
      <td className="px-2 py-1.5 text-right">
        <span className={row.data_viewport_allow_overflow > 0 ? 'text-amber-400 font-medium' : 'text-neutral-600'}>
          {row.data_viewport_allow_overflow}
        </span>
      </td>
      <td className="px-2 py-1.5 text-right">
        <span className={row.data_viewport_allow_truncation > 0 ? 'text-amber-400 font-medium' : 'text-neutral-600'}>
          {row.data_viewport_allow_truncation}
        </span>
      </td>
      <td className="px-2 py-1.5 text-right font-semibold">
        {row.budget_cap != null ? (
          <span className={row.headroom != null && row.headroom <= 0 ? 'text-red-400' : row.headroom != null && row.headroom <= 2 ? 'text-amber-400' : 'text-white'}>
            {row.total_opt_outs}/{row.budget_cap}
          </span>
        ) : (
          <span className={row.total_opt_outs > 0 ? 'text-neutral-400' : 'text-neutral-600'} title="No budget cap set">{row.total_opt_outs}</span>
        )}
      </td>
      <td className="px-2 py-1.5 text-center">
        {row.has_viewport_test ? (
          <CheckCircle className="w-3 h-3 text-emerald-400 inline-block" />
        ) : (
          <XCircle className="w-3 h-3 text-red-400 inline-block" />
        )}
      </td>
      <td className="px-2 py-1.5 text-right">
        <span className={row.regression_case_count > 0 ? 'text-red-400 font-medium' : 'text-neutral-600'}>
          {row.regression_case_count > 0 ? row.regression_case_count : '—'}
        </span>
        {row.regression_case_count > 0 && row.regression_case_breakdown && (
          <span className="block text-ui-xs text-neutral-500 leading-tight whitespace-nowrap">
            {formatBreakdown(row.regression_case_breakdown)}
          </span>
        )}
        {row.regression_case_count > 0 && row.introducing_pr_number != null && (
          <span
            className="block text-ui-xs text-neutral-500 leading-tight"
            title={row.introducing_commit_sha ?? undefined}
          >
            intro: #{row.introducing_pr_number}
          </span>
        )}
      </td>
      <td className="px-2 py-1.5">
        {row.source_url && isSafeUrl(row.source_url) && (
          <a
            href={row.source_url}
            target="_blank"
            rel="noopener noreferrer"
            className="text-neutral-500 hover:text-neutral-300 transition-colors inline-flex items-center justify-center min-h-[44px] min-w-[44px]"
            aria-label={`Open source for ${shortName}`}
          >
            <ExternalLink className="w-3 h-3" />
          </a>
        )}
      </td>
    </tr>
  );
}

function renderRowGroup(rows: ViewportDebtRow[]) {
  return rows.map((row) => renderDebtRow(row));
}

export function ViewportDebtPanel({ projectId }: { projectId: string }) {
  const [data, setData] = useState<ViewportDebtResponse | null>(null);
  const [showUntestedOnly, setShowUntestedOnly] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/viewport-debt`)
      .then(async (res) => {
        if (!cancelled && res.ok) setData(await res.json());
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [projectId]);

  const optOutRows = data?.rows ?? [];
  const untestedRows = data?.untested_rows ?? [];
  const totalRowCount = optOutRows.length + untestedRows.length;
  if (!data || totalRowCount === 0) return null;

  const visibleOptOutRows = showUntestedOnly
    ? optOutRows.filter((r) => !r.has_viewport_test)
    : optOutRows;
  const visibleUntestedRows = untestedRows;
  const renderedUntestedComponents = new Set(
    untestedRows.map((r) => r.component),
  );
  const remainingUntested = (data.coverage_summary?.untested_components ?? []).filter(
    (c) => !renderedUntestedComponents.has(c),
  );
  const untestedHiddenFromTopRows =
    showUntestedOnly &&
    visibleOptOutRows.length === 0 &&
    visibleUntestedRows.length === 0 &&
    (data.coverage_summary?.untested_count ?? 0) > 0;

  return (
    <section data-testid="viewport-debt-panel" className="mt-4">
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Viewport Debt
        <span className="ml-2 bg-workbench-layer/40 text-neutral-300 text-ui-sm font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
          {totalRowCount}
        </span>
      </h2>
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {data.coverage_summary && (
          <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-neutral-800/50">
            <span className="text-ui-md text-neutral-400 flex-1 min-w-0">
              {data.coverage_summary.total_budget != null ? (
                <>
                  Total opt-outs:{' '}
                  <span className={
                    data.coverage_summary.budget_headroom != null && data.coverage_summary.budget_headroom <= 0
                      ? 'text-red-400 font-semibold'
                      : data.coverage_summary.budget_headroom != null && data.coverage_summary.budget_headroom <= 5
                      ? 'text-amber-400 font-semibold'
                      : 'text-white font-semibold'
                  }>{data.coverage_summary.total_opt_outs ?? '?'}/{data.coverage_summary.total_budget}</span>
                  {data.coverage_summary.budget_headroom != null && (
                    <span className="ml-1 text-neutral-500">({data.coverage_summary.budget_headroom} remaining)</span>
                  )}
                  {' · '}
                </>
              ) : null}
              Untested:{' '}
              <span className="text-white font-semibold">{data.coverage_summary.untested_count}</span>
              {' '}of{' '}
              <span className="text-white font-semibold">{data.coverage_summary.total_components}</span>
            </span>
            <button
              onClick={() => setShowUntestedOnly((v) => !v)}
              className={`min-h-[44px] min-w-[44px] px-3 text-ui-sm font-medium rounded transition-colors ${
                showUntestedOnly
                  ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                  : 'bg-workbench-panel/50 text-neutral-400 border border-neutral-700/50 hover:text-neutral-200'
              }`}
              aria-label="Toggle untested-only filter"
            >
              {showUntestedOnly ? 'Untested only' : 'Show all'}
            </button>
          </div>
        )}
        {untestedHiddenFromTopRows && (
          <div className="px-3 py-2 text-ui-md text-neutral-400 border-b border-neutral-800/50">
            All top-{optOutRows.length} components by opt-out debt have viewport tests.
          </div>
        )}
        {remainingUntested.length > 0 && (
          <div className="px-3 py-2 text-ui-md text-neutral-400 border-b border-neutral-800/50">
            +{remainingUntested.length} more untested
            {' '}component{remainingUntested.length === 1 ? '' : 's'} not shown.
          </div>
        )}
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {visibleOptOutRows.map((row) => renderMobileDebtRow(row))}
          {visibleUntestedRows.length > 0 && (
            <>
              <div className="px-3 py-1.5 text-ui-sm uppercase tracking-wider text-neutral-500 bg-workbench-base/40 border-t border-neutral-800/50">
                Untested (no viewport test)
              </div>
              {visibleUntestedRows.map((row) => renderMobileDebtRow(row))}
            </>
          )}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-ui-sm">
          <thead>
            <tr className="border-b border-neutral-800/50 text-neutral-500">
              <th className="text-left px-3 py-1.5 font-medium">Component</th>
              <th className="text-right px-2 py-1.5 font-medium" title="viewport/* rule suppression count (eslint-disable lines)">eslint</th>
              <th className="text-right px-2 py-1.5 font-medium" title="viewport small-target override count">target</th>
              <th className="text-right px-2 py-1.5 font-medium" title="viewport overflow override count">overflow</th>
              <th className="text-right px-2 py-1.5 font-medium" title="viewport truncation override count">trunc</th>
              <th className="text-right px-2 py-1.5 font-medium" title="Total opt-out count / budget cap (red=at/over cap, amber=≤2 remaining)">total/cap</th>
              <th className="text-center px-2 py-1.5 font-medium">test</th>
              <th className="text-right px-2 py-1.5 font-medium" title="Viewport regression dispatcher cases (30d)">cases</th>
              <th className="px-2 py-1.5"></th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {renderRowGroup(visibleOptOutRows)}
          </tbody>
          {visibleUntestedRows.length > 0 && (
            <>
              <tbody>
                <tr className="border-t border-neutral-800/50">
                  <td
                    colSpan={9}
                    className="px-3 py-1.5 text-ui-sm uppercase tracking-wider text-neutral-500 bg-workbench-base/40"
                  >
                    Untested (no viewport test)
                  </td>
                </tr>
              </tbody>
              <tbody className="divide-y divide-neutral-800/30">
                {renderRowGroup(visibleUntestedRows)}
              </tbody>
            </>
          )}
        </table>
      </div>
    </section>
  );
}
