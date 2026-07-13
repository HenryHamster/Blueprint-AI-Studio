import { AlertCircle, AlertTriangle, CheckCircle, HelpCircle, XCircle } from 'lucide-react';

type EvidenceTier = 'covered' | 'attested' | 'backfilled' | 'partial' | 'uncovered';

const TIER_CONFIG: Record<EvidenceTier, {
  label: string;
  cls: string;
  Icon: React.ComponentType<{ className?: string }>;
  tooltip: string;
}> = {
  covered: {
    label: 'Machine verified',
    cls: 'bg-emerald-950/30 border-emerald-900/50 text-emerald-400',
    Icon: CheckCircle,
    tooltip: 'Executed and verified on operator hardware',
  },
  attested: {
    label: 'Attested',
    cls: 'bg-amber-950/30 border-amber-900/50 text-amber-400',
    Icon: AlertCircle,
    tooltip: 'Executed on cloud infrastructure',
  },
  backfilled: {
    label: 'Backfilled',
    cls: 'bg-neutral-800 border-neutral-700 text-neutral-400',
    Icon: HelpCircle,
    tooltip: 'Metrics present without direct execution evidence',
  },
  partial: {
    label: 'Partial',
    cls: 'bg-amber-950/30 border-amber-900/50 text-amber-400',
    Icon: AlertCircle,
    tooltip: 'Execution attempted but did not complete',
  },
  uncovered: {
    label: 'Self-reported',
    cls: 'bg-neutral-800 border-neutral-700 text-neutral-500',
    Icon: XCircle,
    tooltip: 'No execution evidence — result is self-reported',
  },
};

/** Evidence-tier badge for compute_experiment knowledge documents.
 *  Shows the proof strength of a result: was it machine-verified on operator
 *  hardware, cloud-attested, backfilled, or self-reported? Returns null for
 *  unrecognized or absent tiers (non-compute docs, legacy docs). */
export function EvidenceTierBadge({ tier }: { tier: string | null | undefined }) {
  if (!tier || !(tier in TIER_CONFIG)) return null;
  const { label, cls, Icon, tooltip } = TIER_CONFIG[tier as EvidenceTier];
  return (
    <span
      className={`${cls} border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-1 px-1.5 py-0.5`}
      title={tooltip}
    >
      <Icon className="w-2.5 h-2.5 shrink-0" />
      {label}
    </span>
  );
}

/** Warning chip for a compute job that ran on a MUTABLE image tag rather than a
 *  digest-pinned reference. The images-first substrate promises run-to-run
 *  reproducibility via digest pinning; a mutable tag silently defeats it, so the
 *  operator must not believe such a run is bit-reproducible. Render only when the
 *  job's resolved image is known to be mutable (see `computeJobImagePinStatus`). */
export function MutableImageBadge() {
  return (
    <span
      className="bg-amber-950/30 border-amber-900/50 text-amber-400 border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-1 px-1.5 py-0.5"
      title="Runs on a mutable image tag — not digest-pinned; run-to-run reproducibility is not guaranteed"
    >
      <AlertTriangle className="w-2.5 h-2.5 shrink-0" />
      Mutable image
    </span>
  );
}
