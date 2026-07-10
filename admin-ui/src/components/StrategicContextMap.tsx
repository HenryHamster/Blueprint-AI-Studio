/* eslint-disable react-refresh/only-export-components */

import { useId, useMemo } from 'react';
import { ArrowUpRight } from 'lucide-react';
import type {
  StrategicContextCanvasEdge,
  StrategicContextCanvasNode,
  StrategicContextPackageDescriptor,
} from '../strategicStateApi';

type StrategicContextCanvas = NonNullable<StrategicContextPackageDescriptor['canvas']>;
type StrategicContextTarget = NonNullable<StrategicContextCanvasNode['target']>;

interface StrategicContextMapProps {
  canvas?: StrategicContextCanvas | null;
  onOpenTarget?: (target?: StrategicContextTarget | null) => void;
}

export interface PositionedNode {
  node: StrategicContextCanvasNode;
  index: number;
  rawLayer: number;
  column: number;
  x: number;
  xPercent: number;
  y: number;
}

export interface PositionedEdge {
  edge: StrategicContextCanvasEdge;
  source: PositionedNode;
  target: PositionedNode;
  path: string;
  labelX: number;
  labelY: number;
}

export const VIEWBOX_WIDTH = 1000;
const NODE_WIDTH = 168;
const NODE_HEIGHT = 88;
const ROW_GAP = 118;
const SINGLE_COLUMN = [50];
const DOUBLE_COLUMN = [28, 72];
const TRIPLE_COLUMN = [18, 50, 82];

function formatCompactNumber(value: number | null | undefined): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '0';
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

function formatUnitCount(value: number, singular: string): string {
  return `${formatCompactNumber(value)} ${singular}${value === 1 ? '' : 's'}`;
}

function contextKindLabel(kind?: string | null): string {
  if (kind === 'canvas') return 'context map';
  if (kind === 'knowledge_document') return 'knowledge';
  if (kind === 'project_section') return 'project';
  return (kind || 'context').replace(/_/g, ' ');
}

function fallbackLayerForNode(node: StrategicContextCanvasNode): number {
  const kind = node.kind.toLowerCase();
  if (kind === 'project' || kind === 'project_section' || kind === 'canvas') return 0;
  if (kind === 'task' || kind === 'artifact' || kind === 'message' || kind === 'runtime') return 2;
  return 1;
}

function statusToneClass(status?: string | null): string {
  const normalized = status?.toLowerCase() ?? '';
  if (['blocked', 'failed', 'error', 'critical'].some(term => normalized.includes(term))) {
    return 'border-rose-500/30 bg-rose-500/10 text-rose-300';
  }
  if (['active', 'running', 'building', 'pending'].some(term => normalized.includes(term))) {
    return 'border-cyan-500/30 bg-cyan-500/10 text-cyan-300';
  }
  if (['ready', 'done', 'complete', 'resolved'].some(term => normalized.includes(term))) {
    return 'border-emerald-500/30 bg-emerald-500/10 text-emerald-300';
  }
  return 'border-neutral-700 bg-neutral-900/80 text-neutral-400';
}

function truncateEdgeLabel(label: string): string {
  return label.length > 28 ? `${label.slice(0, 25)}...` : label;
}

function edgeKey(edge: StrategicContextCanvasEdge, index: number): string {
  return `${edge.source}:${edge.target}:${edge.label ?? 'link'}:${index}`;
}

export function computeLayout(
  nodes: StrategicContextCanvasNode[],
  edges: StrategicContextCanvasEdge[],
): { positionedNodes: PositionedNode[]; positionedEdges: PositionedEdge[]; height: number; visibleEdges: StrategicContextCanvasEdge[] } {
  const indexById = new Map(nodes.map((node, index) => [node.id, index]));
  const validEdges = edges.filter(edge => indexById.has(edge.source) && indexById.has(edge.target));
  const outgoing = new Map<string, StrategicContextCanvasEdge[]>();
  const indegree = new Map(nodes.map(node => [node.id, 0]));
  const rawLayers = new Map(nodes.map(node => [node.id, 0]));

  validEdges.forEach(edge => {
    const list = outgoing.get(edge.source) ?? [];
    list.push(edge);
    outgoing.set(edge.source, list);
    indegree.set(edge.target, (indegree.get(edge.target) ?? 0) + 1);
  });

  if (validEdges.length === 0) {
    nodes.forEach(node => rawLayers.set(node.id, fallbackLayerForNode(node)));
  } else {
    const queue = nodes
      .filter(node => (indegree.get(node.id) ?? 0) === 0)
      .sort((a, b) => (indexById.get(a.id) ?? 0) - (indexById.get(b.id) ?? 0));
    const visited = new Set<string>();

    while (queue.length > 0) {
      const node = queue.shift();
      if (!node) continue;
      visited.add(node.id);
      const sourceLayer = rawLayers.get(node.id) ?? 0;

      (outgoing.get(node.id) ?? []).forEach(edge => {
        rawLayers.set(edge.target, Math.max(rawLayers.get(edge.target) ?? 0, sourceLayer + 1));
        const nextIndegree = (indegree.get(edge.target) ?? 0) - 1;
        indegree.set(edge.target, nextIndegree);
        if (nextIndegree === 0) {
          const target = nodes[indexById.get(edge.target) ?? -1];
          if (target) queue.push(target);
          queue.sort((a, b) => (indexById.get(a.id) ?? 0) - (indexById.get(b.id) ?? 0));
        }
      });
    }

    nodes.forEach(node => {
      if (!visited.has(node.id)) {
        rawLayers.set(node.id, Math.max(rawLayers.get(node.id) ?? 0, fallbackLayerForNode(node)));
      }
    });
  }

  const maxRawLayer = Math.max(0, ...nodes.map(node => rawLayers.get(node.id) ?? 0));
  const columnCount = maxRawLayer <= 0 ? 1 : maxRawLayer === 1 ? 2 : 3;
  const xPercents = columnCount === 1 ? SINGLE_COLUMN : columnCount === 2 ? DOUBLE_COLUMN : TRIPLE_COLUMN;
  const collapseColumn = (rawLayer: number): number => {
    if (columnCount === 1) return 0;
    if (columnCount === 2) return rawLayer <= 0 ? 0 : 1;
    if (rawLayer <= 0) return 0;
    if (rawLayer >= maxRawLayer) return 2;
    return 1;
  };

  const columns = Array.from({ length: columnCount }, () => [] as StrategicContextCanvasNode[]);
  nodes.forEach(node => {
    columns[collapseColumn(rawLayers.get(node.id) ?? 0)].push(node);
  });
  columns.forEach(column => {
    column.sort((a, b) => {
      const layerDelta = (rawLayers.get(a.id) ?? 0) - (rawLayers.get(b.id) ?? 0);
      if (layerDelta !== 0) return layerDelta;
      return (indexById.get(a.id) ?? 0) - (indexById.get(b.id) ?? 0);
    });
  });

  const maxColumnRows = Math.max(1, ...columns.map(column => column.length));
  const height = Math.max(220, 92 + maxColumnRows * ROW_GAP);
  const topInset = 54;
  const bottomInset = 54;
  const yForRow = (row: number, rowCount: number): number => {
    if (rowCount <= 1) return height / 2;
    return topInset + ((height - topInset - bottomInset) * row) / (rowCount - 1);
  };

  const positionedNodes = columns.flatMap((column, columnIndex) => (
    column.map((node, rowIndex) => {
      const xPercent = xPercents[columnIndex];
      return {
        node,
        index: indexById.get(node.id) ?? rowIndex,
        rawLayer: rawLayers.get(node.id) ?? 0,
        column: columnIndex,
        xPercent,
        x: (xPercent / 100) * VIEWBOX_WIDTH,
        y: yForRow(rowIndex, column.length),
      };
    })
  )).sort((a, b) => a.index - b.index);

  const positionById = new Map(positionedNodes.map(node => [node.node.id, node]));
  const positionedEdges = validEdges.flatMap((edge, edgeIndex) => {
    const source = positionById.get(edge.source);
    const target = positionById.get(edge.target);
    if (!source || !target) return [];

    const sameColumn = source.column === target.column;
    const direction = target.x >= source.x ? 1 : -1;
    const sourceX = sameColumn ? source.x : source.x + direction * (NODE_WIDTH / 2 - 14);
    const targetX = sameColumn ? target.x : target.x - direction * (NODE_WIDTH / 2 - 14);
    const sourceY = source.y;
    const targetY = target.y;
    const bend = sameColumn ? (edgeIndex % 2 === 0 ? 92 : -92) : Math.max(96, Math.abs(targetX - sourceX) * 0.42);
    const path = sameColumn
      ? `M ${sourceX} ${sourceY} C ${sourceX + bend} ${sourceY + 38}, ${targetX + bend} ${targetY - 38}, ${targetX} ${targetY}`
      : `M ${sourceX} ${sourceY} C ${sourceX + direction * bend} ${sourceY}, ${targetX - direction * bend} ${targetY}, ${targetX} ${targetY}`;

    return [{
      edge,
      source,
      target,
      path,
      labelX: (source.x + target.x) / 2,
      labelY: (source.y + target.y) / 2 - 10,
    }];
  });

  return { positionedNodes, positionedEdges, height, visibleEdges: validEdges };
}

function NodeCard({
  positionedNode,
  onOpenTarget,
}: {
  positionedNode: PositionedNode;
  onOpenTarget?: (target?: StrategicContextTarget | null) => void;
}) {
  const { node, xPercent, y } = positionedNode;
  const canOpen = Boolean(node.target && onOpenTarget);
  const meta = [
    contextKindLabel(node.kind),
    node.count != null ? formatCompactNumber(node.count) : null,
  ].filter(Boolean).join(' / ');
  const className = [
    'absolute flex h-[88px] w-[clamp(6rem,22vw,11.5rem)] -translate-x-1/2 flex-col overflow-hidden rounded-md border px-2.5 py-2 text-left shadow-lg shadow-black/20 ring-1 ring-white/[0.03]',
    canOpen
      ? 'border-neutral-700/90 bg-[#0d141d]/95 transition-colors hover:border-cyan-500/50 hover:bg-[#111b25] focus:outline-none focus-visible:ring-2 focus-visible:ring-cyan-500/60'
      : 'border-neutral-800 bg-[#0b1118]/95',
  ].join(' ');
  const content = (
    <>
      <span className="flex min-w-0 items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate font-mono text-ui-sm font-semibold uppercase text-neutral-500">
          {meta}
        </span>
        {node.status && (
          <span className={`max-w-[48%] shrink-0 truncate rounded border px-1 text-ui-sm font-semibold uppercase ${statusToneClass(node.status)}`}>
            {node.status}
          </span>
        )}
      </span>
      <span className="mt-1 line-clamp-2 min-w-0 break-words text-sm font-semibold leading-tight text-neutral-100">
        {node.label}
      </span>
      {canOpen && (
        <ArrowUpRight className="absolute bottom-1.5 right-1.5 h-3.5 w-3.5 text-neutral-500" aria-hidden="true" />
      )}
    </>
  );

  if (canOpen) {
    return (
      <button
        type="button"
        className={className}
        style={{ left: `${xPercent}%`, top: y - NODE_HEIGHT / 2 }}
        onClick={() => onOpenTarget?.(node.target)}
        title={`Open ${node.label}`}
      >
        {content}
      </button>
    );
  }

  return (
    <div
      className={className}
      style={{ left: `${xPercent}%`, top: y - NODE_HEIGHT / 2 }}
      title={node.label}
    >
      {content}
    </div>
  );
}

export function StrategicContextMap({ canvas, onOpenTarget }: StrategicContextMapProps) {
  const markerId = useId().replace(/:/g, '');
  const nodes = canvas?.nodes ?? [];
  const edges = canvas?.edges ?? [];
  const layout = useMemo(() => computeLayout(nodes, edges), [nodes, edges]);
  const hiddenEdgeCount = Math.max(0, edges.length - layout.visibleEdges.length);

  if (nodes.length === 0) {
    return (
      <section aria-label="Strategic context map overview" data-testid="strategic-context-map">
        <div className="rounded-md border border-dashed border-neutral-800 bg-neutral-950/40 px-3 py-4 text-sm text-neutral-500">
          No map nodes are available yet. Resources and evidence below can still carry the discussion context.
        </div>
      </section>
    );
  }

  return (
    <section aria-label="Strategic context map overview" data-testid="strategic-context-map">
      <div
        className="max-h-[520px] min-w-0 overflow-auto rounded-md border border-neutral-800 bg-[#080d13] custom-scrollbar"
        role="group"
        aria-label={`Strategic context map with ${formatUnitCount(nodes.length, 'node')} and ${formatUnitCount(layout.visibleEdges.length, 'link')}`}
      >
        <div className="relative min-w-0 overflow-hidden" style={{ height: layout.height }}>
          <svg
            className="absolute inset-0 h-full w-full"
            viewBox={`0 0 ${VIEWBOX_WIDTH} ${layout.height}`}
            preserveAspectRatio="none"
            aria-hidden="true"
          >
            <defs>
              <marker
                id={markerId}
                markerWidth="8"
                markerHeight="8"
                refX="7"
                refY="4"
                orient="auto"
                markerUnits="strokeWidth"
              >
                <path d="M 0 0 L 8 4 L 0 8 z" fill="#525252" />
              </marker>
            </defs>
            {layout.positionedEdges.map((edge, index) => (
              <g key={edgeKey(edge.edge, index)}>
                <path
                  d={edge.path}
                  fill="none"
                  stroke="#3f3f46"
                  strokeWidth="1.5"
                  strokeLinecap="round"
                  markerEnd={`url(#${markerId})`}
                />
                {edge.edge.label && (
                  <text
                    x={edge.labelX}
                    y={edge.labelY}
                    textAnchor="middle"
                    fill="#a3a3a3"
                    fontSize="10"
                    fontWeight="600"
                    paintOrder="stroke"
                    stroke="#080d13"
                    strokeWidth="6"
                    strokeLinejoin="round"
                  >
                    {truncateEdgeLabel(edge.edge.label)}
                  </text>
                )}
              </g>
            ))}
          </svg>

          {layout.positionedNodes.map(positionedNode => (
            <NodeCard
              key={positionedNode.node.id}
              positionedNode={positionedNode}
              onOpenTarget={onOpenTarget}
            />
          ))}
        </div>
      </div>

      <ul className="sr-only">
        {layout.positionedEdges.map((edge, index) => (
          <li key={edgeKey(edge.edge, index)}>
            {edge.source.node.label} to {edge.target.node.label}
            {edge.edge.label ? `, ${edge.edge.label}` : ''}
          </li>
        ))}
      </ul>

      <div className="mt-2 flex min-w-0 flex-wrap items-center gap-1.5 text-ui-sm font-semibold uppercase text-neutral-500">
        <span className="rounded border border-neutral-800 bg-neutral-950/50 px-1.5 py-0.5">
          {formatUnitCount(nodes.length, 'node')}
        </span>
        <span className="rounded border border-neutral-800 bg-neutral-950/50 px-1.5 py-0.5">
          {formatUnitCount(layout.visibleEdges.length, 'link')}
        </span>
        {hiddenEdgeCount > 0 && (
          <span className="rounded border border-neutral-800 bg-neutral-950/50 px-1.5 py-0.5 text-neutral-600">
            {formatCompactNumber(hiddenEdgeCount)} unresolved
          </span>
        )}
        {layout.visibleEdges.length === 0 && (
          <span className="normal-case text-neutral-600">No links in this map yet.</span>
        )}
      </div>
    </section>
  );
}
