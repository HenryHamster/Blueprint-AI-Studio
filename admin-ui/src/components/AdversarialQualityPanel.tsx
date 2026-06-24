import { useEffect, useState } from 'react';
import { fetchEfficiencyAxes, type EfficiencyAxesResponse, type PanelReviewEffectiveness } from '../api';

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

const GATE_MIN_SAMPLE = 30;

function GateHealthBadge({ failOpen, total }: { failOpen: number; total: number }) {
  if (total + failOpen === 0) return <span data-testid="health-badge" className="text-neutral-500 font-mono text-ui-sm">NO DATA</span>;
  const rate = failOpen / (total + failOpen);
  if (rate >= 0.05) return <span data-testid="health-badge" className="text-red-400 font-mono text-ui-sm">DEGRADED</span>;
  if (total + failOpen < GATE_MIN_SAMPLE) return <span data-testid="health-badge" className="text-neutral-400 font-mono text-ui-sm">INSUFFICIENT</span>;
  if (rate >= 0.01) return <span data-testid="health-badge" className="text-amber-400 font-mono text-ui-sm">PARTIAL</span>;
  return <span data-testid="health-badge" className="text-emerald-400 font-mono text-ui-sm">HEALTHY</span>;
}

function PanelReviewView({ data }: { data: PanelReviewEffectiveness }) {
  const total = data.total_challenged;
  const failOpen = data.challenged_degraded ?? 0;
  const failOpenRate = data.fail_open_rate != null
    ? (data.fail_open_rate * 100).toFixed(1)
    : (total + failOpen > 0 ? ((failOpen / (total + failOpen)) * 100).toFixed(1) : null);
  const overturnRate = data.overturn_rate != null ? (data.overturn_rate * 100).toFixed(1) : null;
  const escalated = data.challenged_escalated ?? 0;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-neutral-400 text-ui-sm">Gate health</span>
        <GateHealthBadge failOpen={failOpen} total={total} />
      </div>
      {total > 0 && (
        <>
          <DistributionBar
            confirmed={data.challenged_confirmed}
            revised={data.challenged_revised}
            rejected={data.challenged_rejected}
          />
          <div className="flex flex-wrap gap-x-3 text-ui-sm">
            <span className="text-emerald-400/80">confirmed: {data.challenged_confirmed}</span>
            <span className="text-amber-400/80">revised: {data.challenged_revised}</span>
            <span className="text-blue-400/80">overturned: {data.challenged_rejected}</span>
            <span className="text-neutral-400">total: {total}</span>
            {overturnRate != null && <span className="text-neutral-400">overturn rate: {overturnRate}%</span>}
          </div>
        </>
      )}
      {failOpen > 0 && (
        <div data-testid="fail-open-row" className="text-ui-sm text-red-400/80">
          fail-open: {failOpen}{failOpenRate != null ? ` (${failOpenRate}%)` : ''} — silent merge (model degraded, primary adopted)
        </div>
      )}
      {escalated > 0 && (
        <div data-testid="escalated-row" className="text-ui-sm text-amber-400/80">
          escalated to manual review: {escalated}
        </div>
      )}
      {data.cost_per_challenge_usd != null && (
        <div className="text-ui-sm text-neutral-500">cost/challenge: ${data.cost_per_challenge_usd.toFixed(4)}</div>
      )}
    </div>
  );
}

export function AdversarialQualityPanel({ projectId }: { projectId: string | null }) {
  const [axes, setAxes] = useState<EfficiencyAxesResponse | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!projectId) { setLoading(false); return; }
    let mounted = true;
    setLoading(true);
    (async () => {
      try {
        const result = await fetchEfficiencyAxes(projectId);
        if (mounted) setAxes(result);
      } catch {
        // silent fail — panel is advisory
      } finally {
        if (mounted) setLoading(false);
      }
    })();
    return () => { mounted = false; };
  }, [projectId]);

  const data = axes?.panel_review_effectiveness ?? null;
  const postDeploy = axes?.panel_review_effectiveness_post_deploy ?? null;

  if (!projectId || (!loading && !data)) return null;

  return (
    <section data-testid="adversarial-quality-panel">
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Challenge Gate Health
      </h2>
      <div className="rounded-lg bg-neutral-800/50 p-3 text-xs">
        {loading ? (
          <div data-testid="panel-loading" className="text-neutral-600 text-ui-sm">Loading…</div>
        ) : (
          data && (
            <>
              <PanelReviewView data={data} />
              {postDeploy && (
                <div data-testid="post-deploy-row" className="mt-2 pt-2 border-t border-neutral-700/50 text-ui-sm text-neutral-500">
                  post-deploy: <GateHealthBadge failOpen={postDeploy.challenged_degraded ?? 0} total={postDeploy.total_challenged} />
                  {postDeploy.fail_open_rate != null && (
                    <span className="ml-1">({(postDeploy.fail_open_rate * 100).toFixed(1)}% fail-open, {postDeploy.total_challenged + (postDeploy.challenged_degraded ?? 0)} gate invocations)</span>
                  )}
                </div>
              )}
            </>
          )
        )}
      </div>
    </section>
  );
}
