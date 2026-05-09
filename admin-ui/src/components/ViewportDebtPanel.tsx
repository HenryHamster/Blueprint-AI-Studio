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
  has_viewport_test: boolean;
  regression_case_count: number;
  source_url: string | null;
  introducing_pr_number?: number | null;
  introducing_commit_sha?: string | null;
}

interface CoverageSummary {
  total_components: number;
  untested_count: number;
  untested_components: string[];
}

interface ViewportDebtResponse {
  rows: ViewportDebtRow[];
  scanned_at: string;
  coverage_summary?: CoverageSummary;
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

  if (!data || data.rows.length === 0) return null;

  const visibleRows = showUntestedOnly
    ? data.rows.filter((r) => !r.has_viewport_test)
    : data.rows;
  const untestedHiddenFromTopRows =
    showUntestedOnly &&
    visibleRows.length === 0 &&
    (data.coverage_summary?.untested_count ?? 0) > 0;

  return (
    <section data-testid="viewport-debt-panel" className="mt-4">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Viewport Debt
        <span className="ml-2 bg-neutral-700/40 text-neutral-300 text-[10px] font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
          {data.rows.length}
        </span>
      </h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {data.coverage_summary && (
          <div className="flex flex-wrap items-center gap-2 px-3 py-2 border-b border-neutral-800/50">
            <span className="text-[11px] text-neutral-400 flex-1 min-w-0">
              Components without viewport test:{' '}
              <span className="text-white font-semibold">{data.coverage_summary.untested_count}</span>
              {' '}of{' '}
              <span className="text-white font-semibold">{data.coverage_summary.total_components}</span>
            </span>
            <button
              onClick={() => setShowUntestedOnly((v) => !v)}
              className={`min-h-[44px] min-w-[44px] px-3 text-[10px] font-medium rounded transition-colors ${
                showUntestedOnly
                  ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                  : 'bg-neutral-800/50 text-neutral-400 border border-neutral-700/50 hover:text-neutral-200'
              }`}
              aria-label="Toggle untested-only filter"
            >
              {showUntestedOnly ? 'Untested only' : 'Show all'}
            </button>
          </div>
        )}
        {untestedHiddenFromTopRows && (
          <div className="px-3 py-2 text-[11px] text-neutral-400 border-b border-neutral-800/50">
            All top-{data.rows.length} components by opt-out debt have viewport tests.{' '}
            <span className="text-white font-semibold">
              {data.coverage_summary?.untested_count}
            </span>
            {' '}untested components are outside this view
            {data.coverage_summary?.untested_components.length ? (
              <>
                {': '}
                <span className="font-mono text-neutral-300">
                  {data.coverage_summary.untested_components
                    .map((c) =>
                      c.replace(/^admin-ui\/src\/components\//, '').replace(/\.tsx$/, '')
                    )
                    .join(', ')}
                </span>
              </>
            ) : (
              '.'
            )}
          </div>
        )}
        <div className="overflow-x-auto" data-viewport-allow-overflow>
          {/* eslint-disable-next-line viewport/no-fixed-width -- intentional scrollable table on mobile; overflow contained by parent overflow-x-auto */}
          <table className="w-full text-[10px] min-w-[520px]">
            <thead>
              <tr className="border-b border-neutral-800/50 text-neutral-500">
                <th className="text-left px-3 py-1.5 font-medium">Component</th>
                <th className="text-right px-2 py-1.5 font-medium" title="eslint-disable viewport/min-touch-target lines">eslint</th>
                <th className="text-right px-2 py-1.5 font-medium" title="data-viewport-allow-small-target attributes">target</th>
                <th className="text-right px-2 py-1.5 font-medium" title="data-viewport-allow-overflow attributes">overflow</th>
                <th className="text-right px-2 py-1.5 font-medium" title="data-viewport-allow-truncation attributes">trunc</th>
                <th className="text-right px-2 py-1.5 font-medium" title="Total opt-out count">total</th>
                <th className="text-center px-2 py-1.5 font-medium">test</th>
                <th className="text-right px-2 py-1.5 font-medium" title="Viewport regression dispatcher cases (30d)">cases</th>
                <th className="px-2 py-1.5"></th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-800/30">
              {visibleRows.map((row) => {
                const shortName = row.component.replace(/^admin-ui\/src\/components\//, '').replace(/\.tsx$/, '');
                return (
                  <tr key={row.component} className="hover:bg-neutral-800/20 transition-colors">
                    <td className="px-3 py-1.5 font-mono text-neutral-300 min-w-0 max-w-[160px] truncate" title={row.component}>
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
                    <td className="px-2 py-1.5 text-right font-semibold text-white">
                      {row.total_opt_outs}
                    </td>
                    <td className="px-2 py-1.5 text-center">
                      {row.has_viewport_test
                        ? <CheckCircle className="w-3 h-3 text-emerald-400 inline-block" />
                        : <XCircle className="w-3 h-3 text-neutral-600 inline-block" />
                      }
                    </td>
                    <td className="px-2 py-1.5 text-right">
                      <span className={row.regression_case_count > 0 ? 'text-red-400 font-medium' : 'text-neutral-600'}>
                        {row.regression_case_count > 0 ? row.regression_case_count : '—'}
                      </span>
                      {row.regression_case_count > 0 && row.introducing_pr_number != null && (
                        <span
                          className="block text-[9px] text-neutral-500 leading-tight"
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
              })}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}
