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
}

interface ViewportDebtResponse {
  rows: ViewportDebtRow[];
  scanned_at: string;
}

export function ViewportDebtPanel({ projectId }: { projectId: string }) {
  const [data, setData] = useState<ViewportDebtResponse | null>(null);

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

  return (
    <section data-testid="viewport-debt-panel" className="mt-4">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Viewport Debt
        <span className="ml-2 bg-neutral-700/40 text-neutral-300 text-[10px] font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
          {data.rows.length}
        </span>
      </h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
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
              {data.rows.map((row) => {
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
