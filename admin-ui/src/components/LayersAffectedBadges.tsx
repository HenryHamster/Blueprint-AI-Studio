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
}

interface Props {
  summary?: LayerSummary | null;
}

const LAYER_SHORT_LABELS: Record<string, string> = {
  CI_WORKFLOW: 'CI',
  AGENT_VM: 'VM',
  AGENT_VERB: 'VERB',
  BASELINE_GATE: 'BASELINE',
  REVIEWER_PASS: 'REVIEWER',
};

function shortLabel(layer: string): string {
  return LAYER_SHORT_LABELS[layer] ?? layer;
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
