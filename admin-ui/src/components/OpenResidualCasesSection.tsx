import { timeAgo } from '../utils/time';
import type { DigestData } from './DashboardHome';

export function OpenResidualCasesSection({ digest }: { digest: DigestData | null }) {
  const groups = digest?.remediation_funnel?.residual_remediation_funnel?.open_cases_by_error_class ?? [];
  if (!groups.length) return null;
  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Open Residual Cases</h2>
      <div className="space-y-2">
        {groups.map((group) => (
          <div key={group.error_class} className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-2">
            <div className="flex items-center justify-between mb-1 text-xs">
              <span className="font-semibold text-neutral-300">{group.error_class} · {group.count}</span>
              <span className="text-neutral-500">{group.most_recent_at ? `latest ${timeAgo(group.most_recent_at)}` : 'no timestamp'}</span>
            </div>
            <div className="space-y-1">
              {group.cases.slice(0, 8).map((c) => (
                <div key={c.fingerprint} className="text-ui-md text-neutral-400 border border-neutral-800/60 rounded px-2 py-1">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-mono truncate min-w-0">{c.fingerprint.slice(0, 18)}…</span>
                    <span className="text-neutral-500 shrink-0">{c.status} · {c.hit_count} hits · {c.last_seen_at ? timeAgo(c.last_seen_at) : 'unknown age'}</span>
                  </div>
                  {c.sample_message && <div className="truncate text-neutral-500">{c.sample_message}</div>}
                </div>
              ))}
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
