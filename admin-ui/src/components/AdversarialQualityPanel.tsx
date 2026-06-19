import { useEffect, useState } from 'react';
import { fetchEfficiencyAxes, type PanelReviewEffectiveness } from '../api';

function DistributionBar({ confirmed, revised, rejected }: { confirmed: number; revised: number; rejected: number }) {
  const total = confirmed + revised + rejected;
  if (total === 0) return null;
  const w = (n: number) => `${((n / total) * 100).toFixed(0)}%`;
  return (
    <div className="flex h-1.5 rounded overflow-hidden gap-px mt-1">
      {confirmed > 0 && <div data-testid="bar-confirmed" style={{ width: w(confirmed) }} className="bg-emerald-600" />}
      {revised > 0 && <div data-testid="bar-revised" style={{ width: w(revised) }} className="bg-amber-500" />}
      {rejected > 0 && <div data-testid="bar-rejected" style={{ width: w(rejected) }} className="bg-blue-500" />}
    </div>
  );
}

function GateHealthBadge({ failOpen, total }: { failOpen: number; total: number }) {
  const rate = total + failOpen > 0 ? failOpen / (total + failOpen) : 0;
  if (rate >= 0.3) return <span data-testid="health-badge" className="text-red-400 font-mono text-[10px]">DEGRADED</span>;
  if (rate >= 0.1) return <span data-testid="health-badge" className="text-amber-400 font-mono text-[10px]">PARTIAL</span>;
  return <span data-testid="health-badge" className="text-emerald-400 font-mono text-[10px]">HEALTHY</span>;
}

function PanelReviewView({ data }: { data: PanelReviewEffectiveness }) {
  const total = data.total_challenged;
  const failOpen = data.challenged_escalated ?? 0;
  const failOpenRate = total + failOpen > 0 ? ((failOpen / (total + failOpen)) * 100).toFixed(1) : null;
  const overturnRate = data.overturn_rate != null ? (data.overturn_rate * 100).toFixed(1) : null;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-neutral-400 text-[10px]">Gate health</span>
        <GateHealthBadge failOpen={failOpen} total={total} />
      </div>
      {total > 0 && (
        <>
          <DistributionBar
            confirmed={data.challenged_confirmed}
            revised={data.challenged_revised}
            rejected={data.challenged_rejected}
          />
          <div className="flex flex-wrap gap-x-3 text-[10px]">
            <span className="text-emerald-400/80">confirmed: {data.challenged_confirmed}</span>
            <span className="text-amber-400/80">revised: {data.challenged_revised}</span>
            <span className="text-blue-400/80">overturned: {data.challenged_rejected}</span>
            <span className="text-neutral-400">total: {total}</span>
            {overturnRate != null && <span className="text-neutral-400">overturn rate: {overturnRate}%</span>}
          </div>
        </>
      )}
      {failOpen > 0 && (
        <div data-testid="fail-open-row" className="text-[10px] text-red-400/80">
          fail-open: {failOpen}{failOpenRate != null ? ` (${failOpenRate}%)` : ''} — challenger failed, primary adopted
        </div>
      )}
      {data.cost_per_challenge_usd != null && (
        <div className="text-[10px] text-neutral-500">cost/challenge: ${data.cost_per_challenge_usd.toFixed(4)}</div>
      )}
    </div>
  );
}

export function AdversarialQualityPanel({ projectId }: { projectId: string | null }) {
  const [data, setData] = useState<PanelReviewEffectiveness | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!projectId) { setLoading(false); return; }
    let mounted = true;
    setLoading(true);
    (async () => {
      try {
        const axes = await fetchEfficiencyAxes(projectId);
        if (mounted) setData(axes.panel_review_effectiveness ?? null);
      } catch {
        // silent fail — panel is advisory
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => { mounted = false; };
  }, [projectId]);

  if (!projectId || (!loading && !data)) return null;

  return (
    <section data-testid="adversarial-quality-panel">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Challenge Gate Health
      </h2>
      <div className="rounded-lg bg-neutral-800/50 p-3 text-xs">
        {loading ? (
          <div data-testid="panel-loading" className="text-neutral-600 text-[10px]">Loading…</div>
        ) : (
          data && <PanelReviewView data={data} />
        )}
      </div>
    </section>
  );
}
