/**
 * RailMiniCanvas — a compact inline SVG visualization for the right-rail context
 * section. Follows the same node/edge rendering pattern as StrategicContextMap
 * (computeLayout, SVG paths, colored circles) but at a smaller scale and with
 * pointer-events disabled.
 *
 * Three visual states:
 *  1. Has canvas data → render the laid-out nodes/edges (existing behavior).
 *  2. No canvas data, strategist active → show a minimal "working" indicator.
 *  3. No canvas data, idle → show a quiet placeholder.
 */
import { useMemo } from 'react';
import { computeLayout, VIEWBOX_WIDTH } from './StrategicContextMap';
import type { StrategicContextCanvasNode, StrategicContextCanvasEdge } from '../strategicStateApi';

export interface RailMiniCanvasProps {
  canvas?: { nodes?: StrategicContextCanvasNode[]; edges?: StrategicContextCanvasEdge[] } | null;
  strategistActive?: boolean;
  activeTaskLabel?: string | null;
  activeDirectionLabel?: string | null;
}

/** Resolve a fill color from a node's status string (mirrors StrategicContextMap). */
function miniFill(s?: string | null): string {
  const c = s?.toLowerCase() ?? '';
  if (/active|running|building|pending/.test(c)) return '#22d3ee';
  if (/blocked|failed|error|critical/.test(c)) return '#fb7185';
  if (/done|ready|complete|resolved/.test(c)) return '#34d399';
  return '#525252';
}

/** Stack of small colored dots showing the strategist is actively working. */
function WorkingIndicator({ activeTaskLabel, activeDirectionLabel }: {
  activeTaskLabel?: string | null;
  activeDirectionLabel?: string | null;
}) {
  return (
    <div
      className="flex items-center gap-3 rounded-md border border-cyan-500/20 bg-cyan-500/5 px-3 py-2"
      data-testid="rail-mini-canvas-working"
    >
      <div className="flex shrink-0 gap-1" aria-hidden="true">
        <span className="h-2 w-2 animate-pulse rounded-full bg-cyan-400" />
        <span className="h-2 w-2 animate-pulse rounded-full bg-cyan-400/60" style={{ animationDelay: '300ms' }} />
        <span className="h-2 w-2 animate-pulse rounded-full bg-cyan-400/30" style={{ animationDelay: '600ms' }} />
      </div>
      <div className="min-w-0 text-xs leading-relaxed text-neutral-400">
        {activeTaskLabel && (
          <span className="block truncate font-medium text-neutral-200">{activeTaskLabel}</span>
        )}
        {activeDirectionLabel && (
          <span className="block truncate text-cyan-400/70">{activeDirectionLabel}</span>
        )}
        {!activeTaskLabel && !activeDirectionLabel && (
          <span className="text-cyan-400/70">Working…</span>
        )}
      </div>
    </div>
  );
}

/** An idle-state placeholder — very quiet, no motion. */
function IdlePlaceholder() {
  return (
    <div
      className="flex items-center gap-2 rounded-md border border-neutral-800/60 bg-neutral-950/30 px-3 py-2"
      data-testid="rail-mini-canvas-idle"
    >
      <div className="flex shrink-0 gap-1" aria-hidden="true">
        <span className="h-2 w-2 rounded-full bg-neutral-700" />
        <span className="h-2 w-2 rounded-full bg-neutral-700/60" />
        <span className="h-2 w-2 rounded-full bg-neutral-700/30" />
      </div>
      <span className="truncate text-xs text-neutral-600">No map data</span>
    </div>
  );
}

export function RailMiniCanvas({
  canvas,
  strategistActive = false,
  activeTaskLabel,
  activeDirectionLabel,
}: RailMiniCanvasProps) {
  const layout = useMemo(() => {
    const innerNodes = canvas?.nodes ?? [];
    const innerEdges = canvas?.edges ?? [];
    return computeLayout(innerNodes, innerEdges);
  }, [canvas?.nodes, canvas?.edges]);

  // State 1 — canvas nodes exist: render the SVG layout (existing pattern).
  if (layout.positionedNodes.length > 0) {
    return (
      <div
        className="mb-2 overflow-hidden rounded border border-neutral-800 bg-[#080d13]"
        style={{ maxHeight: 120 }}
        data-testid="rail-mini-canvas"
      >
        <svg
          className="w-full"
          viewBox={`0 0 ${VIEWBOX_WIDTH} ${layout.height}`}
          preserveAspectRatio="xMidYMin meet"
          style={{ pointerEvents: 'none' }}
          aria-hidden="true"
        >
          {layout.positionedEdges.map((e, i) => (
            <path key={i} d={e.path} fill="none" stroke="#3f3f46" strokeWidth="1" />
          ))}
          {layout.positionedNodes.map(pn => (
            <circle key={pn.node.id} cx={pn.x} cy={pn.y} r={5} fill={miniFill(pn.node.status)} />
          ))}
        </svg>
      </div>
    );
  }

  // State 2 — no canvas data, strategist active: show a working indicator.
  if (strategistActive) {
    return (
      <WorkingIndicator
        activeTaskLabel={activeTaskLabel}
        activeDirectionLabel={activeDirectionLabel}
      />
    );
  }

  // State 3 — idle / no data: show a quiet placeholder.
  return <IdlePlaceholder />;
}
