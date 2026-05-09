export interface LayerSummaryEntry {
  layer: string;
  count: number;
  distinct_fingerprints: number;
  last_observed_at: string;
  sample: string;
}

export interface LayerSummary {
  cross_layer: boolean;
  layers_affected: string[];
  window_hours: number;
  layers: LayerSummaryEntry[];
  layer_mismatch?: boolean | null;
  prior_fix_layers?: string[] | null;
  current_symptom_layers?: string[] | null;
}

interface Props {
  summary?: LayerSummary | null;
}

// eslint-disable-next-line react-refresh/only-export-components
export const LAYER_SHORT_LABELS: Record<string, string> = {
  CI_WORKFLOW: 'CI',
  AGENT_VM: 'VM',
  AGENT_VERB: 'VERB',
  BASELINE_GATE: 'BASELINE',
  REVIEWER_PASS: 'REVIEWER',
};

// eslint-disable-next-line react-refresh/only-export-components
export const CANONICAL_LAYERS = ['CI_WORKFLOW', 'AGENT_VM', 'AGENT_VERB', 'BASELINE_GATE', 'REVIEWER_PASS'] as const;
export type CanonicalLayer = typeof CANONICAL_LAYERS[number];

export interface LayerCoverageCombo { layers: string[]; count: number; }
export interface LayerCoverageEntry { total_fixes_with_layers: number; combinations: LayerCoverageCombo[]; }
export type LayerCoverageByClass = Record<string, LayerCoverageEntry>;

function shortLabel(layer: string): string {
  return LAYER_SHORT_LABELS[layer] ?? layer;
}

interface LayerMismatchProps {
  layer_mismatch?: boolean | null;
  prior_fix_layers?: string[] | null;
  current_symptom_layers?: string[] | null;
}

interface UntargetedLayersProps {
  untargeted_layers?: string[] | null;
}

export function UntargetedLayersBadge({ untargeted_layers }: UntargetedLayersProps) {
  const layers = untargeted_layers ?? [];
  // Suppress when all layers are untargeted (no fix history yet) or none are
  // (full coverage) — both extremes are noise.
  if (layers.length === 0 || layers.length >= CANONICAL_LAYERS.length) return null;
  const labels = layers.map((l) => shortLabel(l));
  return (
    <span
      data-testid="untargeted-layers-badge"
      className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-500/10 text-neutral-400 border border-neutral-500/20"
      title={`Layers no prior fix has targeted: ${layers.join(', ')}`}
    >
      never defended in: {labels.join(', ')}
    </span>
  );
}

export function LayerMismatchBadge({ layer_mismatch, prior_fix_layers, current_symptom_layers }: LayerMismatchProps) {
  if (!layer_mismatch) return null;
  const fmt = (layers: string[] | null | undefined) =>
    (layers ?? []).map((l) => shortLabel(l)).join(', ') || '?';
  const tip = `prior fixes targeted ${fmt(prior_fix_layers)}; current symptoms on ${fmt(current_symptom_layers)}`;
  return (
    <span
      data-testid="layer-mismatch-badge"
      className="text-[10px] px-1.5 py-0.5 rounded bg-orange-500/10 text-orange-300 border border-orange-500/20"
      title={tip}
    >
      layer mismatch
    </span>
  );
}

export function LayersAffectedBadges({ summary }: Props) {
  if (!summary) return null;
  const entries = (summary.layers ?? []).filter((e) => e && e.layer);
  if (entries.length === 0) return null;

  return (
    <span data-testid="layers-affected-badges" className="inline-flex flex-wrap items-center gap-1">
      {entries.map((entry) => {
        const titleParts: string[] = [];
        titleParts.push(`${entry.layer}: ${entry.count} obs / ${entry.distinct_fingerprints} fp`);
        titleParts.push(`last seen ${entry.last_observed_at}`);
        if (entry.sample) titleParts.push(`sample: ${entry.sample}`);
        return (
          <span
            key={entry.layer}
            data-testid={`layer-badge-${entry.layer}`}
            className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800/60 text-neutral-300 border border-neutral-700 font-mono"
            title={titleParts.join(' • ')}
          >
            {shortLabel(entry.layer)} · {entry.count}
          </span>
        );
      })}
      {summary.cross_layer && (
        <span
          data-testid="cross-layer-badge"
          className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/30"
          title={`Symptoms observed across ${summary.layers_affected.length} layers in the last ${summary.window_hours}h`}
        >
          cross-layer
        </span>
      )}
    </span>
  );
}
