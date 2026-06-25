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

const GATE_VERDICT_COLORS: Record<string, string> = {
  NO_DATA: 'text-neutral-500',
  DEGRADED: 'text-red-400',
  INSUFFICIENT: 'text-neutral-400',
  LOW_COVERAGE: 'text-neutral-400',
  PARTIAL: 'text-amber-400',
  HEALTHY: 'text-emerald-400',
};

function GateHealthBadge({ backendVerdict }: {
  backendVerdict?: { state: string; fail_open_rate: number | null; total_invocations: number; min_sample: number };
}) {
  if (!backendVerdict) {
    return <span data-testid="health-badge" className="text-neutral-500 font-mono text-ui-sm">NO DATA</span>;
  }
  const label = backendVerdict.state.replace(/_/g, ' ');
  return <span data-testid="health-badge" className={`${GATE_VERDICT_COLORS[backendVerdict.state] || 'text-neutral-400'} font-mono text-ui-sm`}>{label}</span>;
}

function PanelReviewView({ data }: { data: PanelReviewEffectiveness }) {
  const total = data.total_challenged;
  const failOpen = data.challenged_degraded ?? 0;
  const failOpenRate = data.fail_open_rate != null
    ? (data.fail_open_rate * 100).toFixed(1)
    : (total + failOpen > 0 ? ((failOpen / (total + failOpen)) * 100).toFixed(1) : null);
  const overturnRate = data.overturn_rate != null ? (data.overturn_rate * 100).toFixed(1) : null;
  const escalated = data.challenged_escalated ?? 0;
  const coverage = data.effective_coverage != null ? (data.effective_coverage * 100).toFixed(0) : null;

  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between">
        <span className="text-neutral-400 text-ui-sm">Gate health</span>
        <GateHealthBadge backendVerdict={data.gate_health} />
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
      {coverage != null && (
        <div className="text-ui-sm text-neutral-500">effective coverage: {coverage}%</div>
      )}
    </div>
  );
}

export function AdversarialQualityPanel({ projectId }: { projectId: string | null }) {
  const [axes, setAxes] = useState<EfficiencyAxesResponse | null>(null);
  const [loading, setLoading] = useState(!!projectId);

  useEffect(() => {
    if (!projectId) return;
    let mounted = true;
    (async () => {
      if (mounted) setLoading(true);
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
                  post-deploy: <GateHealthBadge backendVerdict={postDeploy.gate_health} />
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
