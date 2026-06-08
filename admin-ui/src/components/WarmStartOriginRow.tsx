import type { WarmStartOriginBreakdown } from '../hooks/useEfficiencySnapshot';

// Pool-vs-chain-vs-backlog split of warm starts over the 24h window, plus the
// pool-origin share. Renders nothing when the backend omits the field (older
// deploy) or there were no warm starts to attribute (null). Colors match the
// in-flight claim conventions used elsewhere in the panel: pool=emerald,
// backlog=sky, chain=orange.
export function WarmStartOriginRow({ breakdown }: { breakdown?: WarmStartOriginBreakdown | null }) {
  if (!breakdown) return null;
  const share = Math.round(breakdown.pool_origin_share * 100);
  return (
    <span data-testid="warm-start-origin-breakdown" className="w-full flex flex-wrap gap-x-3 gap-y-0.5">
      <span className="text-neutral-500">warm by origin</span>:
      <span><span className="text-neutral-500">pool</span> <span className="font-mono text-emerald-400">{breakdown.warm_pool}</span></span>
      <span><span className="text-neutral-500">backlog</span> <span className="font-mono text-sky-400">{breakdown.backlog_reuse}</span></span>
      <span><span className="text-neutral-500">chain</span> <span className="font-mono text-orange-400">{breakdown.chain_continuity}</span></span>
      <span data-testid="warm-start-pool-share"><span className="text-neutral-500">pool share</span> <span className="font-mono text-emerald-400">{share}%</span></span>
    </span>
  );
}
