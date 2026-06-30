import { useEffect, type ReactNode } from 'react';
import { ArrowUpRight, GitBranch, Link2, Network, Plus, X } from 'lucide-react';
import type {
  StrategicContextArtifactEvidence,
  StrategicContextCanvasNode,
  StrategicContextPackageDrilldown,
  StrategicContextResourceRef,
  StrategicStateResponse,
  StrategicWorkbenchState,
  StrategicWorkSessionRefDescriptor,
} from '../strategicStateApi';
import {
  MAX_STRATEGIC_SELECTED_REFS,
  removeStrategicSelectedRef,
  upsertStrategicSelectedRef,
  type StrategicSelectedRef,
} from '../utils/strategicWorkbenchSession';

type StrategicContextTarget = StrategicContextPackageDrilldown['target'];

interface StrategicContextWorkspaceProps {
  open: boolean;
  state: StrategicStateResponse | null;
  workbenchState?: StrategicWorkbenchState | null;
  savingSessionRefs?: boolean;
  onClose: () => void;
  onOpenTarget: (target?: StrategicContextTarget | null) => void;
  onUpdateSessionRefs?: (
    updateRefs: (refs: StrategicWorkbenchState['selected_refs']) => StrategicWorkbenchState['selected_refs']
  ) => void;
}

function formatCompactNumber(value: number | null | undefined): string {
  const n = Number(value ?? 0);
  if (!Number.isFinite(n)) return '0';
  if (Math.abs(n) >= 1000) return `${(n / 1000).toFixed(n >= 10_000 ? 0 : 1)}k`;
  return String(n);
}

function contextStatusTone(status?: string | null): { dot: string; text: string; label: string } {
  if (status === 'building') return { dot: 'bg-cyan-400', text: 'text-cyan-300', label: 'building' };
  if (status === 'stale') return { dot: 'bg-amber-300', text: 'text-amber-300', label: 'stale' };
  if (!status) return { dot: 'bg-neutral-600', text: 'text-neutral-500', label: 'empty' };
  return { dot: 'bg-emerald-400', text: 'text-emerald-300', label: 'ready' };
}

function contextKindLabel(kind?: string | null): string {
  if (kind === 'canvas') return 'map';
  if (kind === 'knowledge_document') return 'knowledge';
  if (kind === 'artifact') return 'artifact';
  if (kind === 'task') return 'task';
  return (kind || 'context').replace(/_/g, ' ');
}

function contextRefKey(ref: Pick<StrategicSelectedRef, 'kind' | 'id'>): string {
  return `${ref.kind}:${ref.id}`;
}

function selectedRefFromResource(resource: StrategicContextResourceRef): StrategicSelectedRef {
  return {
    kind: resource.kind,
    id: resource.id,
    label: resource.label,
  };
}

function selectedRefFromNode(node: StrategicContextCanvasNode): StrategicSelectedRef {
  return {
    kind: node.kind,
    id: node.id,
    label: node.label,
  };
}

function selectedRefFromKnowledgeEvidence(item: {
  id: string;
  label?: string | null;
  topic?: string | null;
}): StrategicSelectedRef {
  return {
    kind: 'knowledge_document',
    id: item.id.startsWith('knowledge:') ? item.id : `knowledge:${item.id}`,
    label: item.label ?? item.topic ?? 'Knowledge document',
  };
}

function selectedRefFromArtifactEvidence(item: StrategicContextArtifactEvidence): StrategicSelectedRef {
  return {
    kind: 'artifact',
    id: item.id.startsWith('artifact:') ? item.id : `artifact:${item.id}`,
    label: item.label ?? item.logical_key ?? item.artifact_type ?? 'Artifact',
  };
}

function fallbackRefDescriptor(ref: StrategicSelectedRef): StrategicWorkSessionRefDescriptor {
  return {
    kind: ref.kind,
    id: ref.id,
    label: ref.label || `${contextKindLabel(ref.kind)} ${ref.id.slice(0, 8)}`,
  };
}

function targetLabel(target?: StrategicContextTarget | null): string | null {
  if (!target) return null;
  const parts = [target.surface, target.section, target.task_id, target.document_id, target.artifact_id]
    .filter((part): part is string => typeof part === 'string' && part.trim().length > 0);
  return parts.join(' / ');
}

function Section({
  title,
  meta,
  children,
  testId,
}: {
  title: string;
  meta?: string | null;
  children: ReactNode;
  testId?: string;
}) {
  return (
    <section className="min-w-0 overflow-hidden rounded-md border border-neutral-800 bg-[#10151c]" data-testid={testId}>
      <div className="flex min-w-0 items-center justify-between gap-3 border-b border-neutral-800 px-3 py-2.5">
        <h3 className="truncate text-sm font-semibold text-neutral-100">{title}</h3>
        {meta && <span className="shrink-0 text-xs text-neutral-500">{meta}</span>}
      </div>
      <div className="min-w-0 p-2.5">{children}</div>
    </section>
  );
}

function EmptyState({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-neutral-800 px-3 py-4 text-sm text-neutral-500">
      {children}
    </div>
  );
}

function SessionRefAction({
  selected,
  disabled,
  label,
  onKeep,
  onRemove,
}: {
  selected: boolean;
  disabled?: boolean;
  label: string;
  onKeep?: () => void;
  onRemove?: () => void;
}) {
  if (!onKeep && !onRemove) return null;
  if (selected) {
    return (
      <button
        type="button"
        disabled={disabled}
        onClick={onRemove}
        className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-md border border-neutral-800 px-2 text-ui-sm font-semibold text-neutral-400 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 disabled:opacity-40"
        title={`Remove ${label} from session context`}
        aria-label={`Remove ${label} from session context`}
      >
        <X className="h-3.5 w-3.5" aria-hidden="true" />
        <span>Remove</span>
      </button>
    );
  }
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onKeep}
      className="inline-flex min-h-11 shrink-0 items-center gap-1 rounded-md border border-neutral-800 px-2 text-ui-sm font-semibold text-neutral-400 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100 disabled:opacity-40"
      title={`Keep ${label} in session context`}
      aria-label={`Keep ${label} in session context`}
    >
      <Plus className="h-3.5 w-3.5" aria-hidden="true" />
      <span>Keep</span>
    </button>
  );
}

function ContextRow({
  label,
  kind,
  detail,
  target,
  selected,
  saving,
  badge,
  onOpenTarget,
  onKeep,
  onRemove,
}: {
  label: string;
  kind: string;
  detail?: string | null;
  target?: StrategicContextTarget | null;
  selected?: boolean;
  saving?: boolean;
  badge?: ReactNode;
  onOpenTarget: (target?: StrategicContextTarget | null) => void;
  onKeep?: () => void;
  onRemove?: () => void;
}) {
  const canOpen = Boolean(target);
  return (
    <div className="flex min-w-0 items-center gap-2 border-t border-neutral-800/70 py-2 first:border-t-0">
      <button
        type="button"
        disabled={!canOpen}
        onClick={() => onOpenTarget(target)}
        className="min-h-11 min-w-0 flex-1 text-left disabled:cursor-default"
        title={canOpen ? `Open ${label}` : label}
      >
        <span className="flex min-w-0 items-center gap-2">
          <span className="truncate text-sm font-semibold text-neutral-100">{label}</span>
          {badge}
        </span>
        <span className="mt-0.5 block truncate text-ui-sm font-semibold uppercase text-neutral-500">
          {contextKindLabel(kind)}
          {targetLabel(target) ? ` / ${targetLabel(target)}` : ''}
        </span>
        {detail && <span className="mt-0.5 block line-clamp-2 text-xs leading-relaxed text-neutral-500">{detail}</span>}
      </button>
      {canOpen && <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />}
      <SessionRefAction
        selected={Boolean(selected)}
        disabled={saving}
        label={label}
        onKeep={onKeep}
        onRemove={onRemove}
      />
    </div>
  );
}

function EdgeRow({
  source,
  target,
  label,
}: {
  source: string;
  target: string;
  label?: string | null;
}) {
  return (
    <div className="flex min-w-0 items-center gap-2 border-t border-neutral-800/70 py-2 first:border-t-0">
      <Link2 className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
      <div className="min-w-0">
        <div className="truncate text-sm font-semibold text-neutral-200">
          {source} <span className="text-neutral-600">to</span> {target}
        </div>
        {label && <div className="mt-0.5 truncate text-xs text-neutral-500">{label}</div>}
      </div>
    </div>
  );
}

function StatusBadge({ status }: { status?: string | null }) {
  if (!status) return null;
  return (
    <span className="shrink-0 rounded border border-neutral-800 px-1 text-ui-sm font-semibold uppercase text-neutral-500">
      {status}
    </span>
  );
}

export function StrategicContextWorkspace({
  open,
  state,
  workbenchState,
  savingSessionRefs = false,
  onClose,
  onOpenTarget,
  onUpdateSessionRefs,
}: StrategicContextWorkspaceProps) {
  useEffect(() => {
    if (!open) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [onClose, open]);

  if (!open) return null;

  const contextPackage = state?.context_package ?? null;
  const selectedRefs = workbenchState?.selected_refs ?? state?.work_session?.selected_refs ?? state?.workbench?.selected_refs ?? [];
  const workSessionDescriptors = state?.work_session?.selected_ref_descriptors ?? [];
  const descriptorByRef = new Map(workSessionDescriptors.map(descriptor => [contextRefKey(descriptor), descriptor]));
  const selectedRefDescriptors = selectedRefs.map(ref => descriptorByRef.get(contextRefKey(ref)) ?? fallbackRefDescriptor(ref));
  const selectedKeys = new Set(selectedRefs.map(contextRefKey));
  const canvas = contextPackage?.canvas ?? null;
  const nodeLabelById = new Map((canvas?.nodes ?? []).map(node => [node.id, node.label]));
  const tone = contextStatusTone(contextPackage?.status);
  const resources = contextPackage?.resources ?? [];
  const nodes = canvas?.nodes ?? [];
  const edges = canvas?.edges ?? [];
  const drilldowns = [...(contextPackage?.drilldowns ?? []), ...(canvas?.drilldowns ?? [])];
  const knowledgeEvidence = contextPackage?.evidence?.knowledge?.items ?? [];
  const artifactEvidence = contextPackage?.evidence?.artifacts?.items ?? [];
  const itemCount = contextPackage?.item_count ?? 0;
  const tokenCount = contextPackage?.token_count ?? 0;
  const packageMeta = [
    itemCount > 0 ? `${formatCompactNumber(itemCount)} items` : null,
    tokenCount > 0 ? `${formatCompactNumber(tokenCount)} tokens` : null,
    contextPackage?.updated_at ? `updated ${contextPackage.updated_at}` : null,
  ].filter(Boolean).join(' / ');

  const handleOpenTarget = (target?: StrategicContextTarget | null): void => {
    if (!target) return;
    onClose();
    onOpenTarget(target);
  };
  const keepRef = (ref: StrategicSelectedRef): void => {
    onUpdateSessionRefs?.(refs => upsertStrategicSelectedRef(refs, ref));
  };
  const removeRef = (ref: Pick<StrategicSelectedRef, 'kind' | 'id'>): void => {
    onUpdateSessionRefs?.(refs => removeStrategicSelectedRef(refs, ref));
  };
  const renderKeepableRow = ({
    key,
    label,
    kind,
    detail,
    target,
    ref,
    badge,
  }: {
    key: string;
    label: string;
    kind: string;
    detail?: string | null;
    target?: StrategicContextTarget | null;
    ref: StrategicSelectedRef;
    badge?: ReactNode;
  }) => {
    const selected = selectedKeys.has(contextRefKey(ref));
    return (
      <ContextRow
        key={key}
        label={label}
        kind={kind}
        detail={detail}
        target={target}
        selected={selected}
        saving={savingSessionRefs}
        badge={badge}
        onOpenTarget={handleOpenTarget}
        onKeep={onUpdateSessionRefs && !selected ? () => keepRef(ref) : undefined}
        onRemove={onUpdateSessionRefs && selected ? () => removeRef(ref) : undefined}
      />
    );
  };

  return (
    <div className="fixed inset-0 z-[65] flex justify-end" role="dialog" aria-modal="true" aria-label="Strategic context">
      <button
        type="button"
        className="absolute inset-0 cursor-default bg-black/50"
        aria-label="Close Strategic context"
        onClick={onClose}
      />
      <div className="relative flex h-full w-full max-w-full flex-col overflow-hidden border-l border-neutral-800 bg-[#05080c] shadow-2xl md:max-w-3xl">
        <header className="flex min-w-0 shrink-0 items-center justify-between gap-3 border-b border-neutral-800 bg-[#05070a] px-3 py-2.5 md:px-4">
          <div className="min-w-0">
            <div className="flex min-w-0 items-center gap-2">
              <Network className="h-4 w-4 shrink-0 text-neutral-500" aria-hidden="true" />
              <h2 className="truncate text-lg font-semibold text-neutral-100">Strategic context</h2>
              <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${tone.dot}`} aria-hidden="true" />
              <span className={`shrink-0 text-ui-sm font-semibold uppercase ${tone.text}`}>{tone.label}</span>
            </div>
            {contextPackage && (
              <p className="mt-0.5 truncate text-xs text-neutral-500">
                {contextPackage.title}
                {packageMeta ? ` / ${packageMeta}` : ''}
              </p>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-neutral-900 hover:text-neutral-100"
            title="Close Strategic context"
            aria-label="Close Strategic context"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </header>

        <div className="min-h-0 flex-1 overflow-auto p-3 custom-scrollbar md:p-4">
          <div className="grid min-w-0 max-w-full grid-cols-1 gap-3">
            <Section
              title="Kept context"
              meta={`${selectedRefDescriptors.length}/${MAX_STRATEGIC_SELECTED_REFS}`}
              testId="strategic-context-workspace-kept"
            >
              {selectedRefDescriptors.length > 0 ? (
                <div className="rounded-md border border-neutral-800 bg-neutral-950/40 px-2.5">
                  {selectedRefDescriptors.map(ref => (
                    <ContextRow
                      key={contextRefKey(ref)}
                      label={ref.label}
                      kind={ref.kind}
                      detail={ref.summary ?? null}
                      target={ref.target}
                      selected
                      saving={savingSessionRefs}
                      badge={<StatusBadge status={ref.status} />}
                      onOpenTarget={handleOpenTarget}
                      onRemove={onUpdateSessionRefs ? () => removeRef(ref) : undefined}
                    />
                  ))}
                </div>
              ) : (
                <EmptyState>No context is pinned for this Strategic session.</EmptyState>
              )}
            </Section>

            {drilldowns.length > 0 && (
              <Section title="Open" meta={`${drilldowns.length}`} testId="strategic-context-workspace-drilldowns">
                <div className="grid gap-2 sm:grid-cols-2">
                  {drilldowns.map(drilldown => (
                    <button
                      key={`${drilldown.id}:${targetLabel(drilldown.target) ?? 'target'}`}
                      type="button"
                      onClick={() => handleOpenTarget(drilldown.target)}
                      className="flex min-h-11 min-w-0 items-center justify-between gap-2 rounded-md border border-neutral-800 bg-neutral-950/50 px-3 py-2 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-900"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-semibold text-neutral-100">{drilldown.label}</span>
                        <span className="mt-0.5 block truncate text-ui-sm font-semibold uppercase text-neutral-500">
                          {targetLabel(drilldown.target)}
                        </span>
                      </span>
                      <ArrowUpRight className="h-3.5 w-3.5 shrink-0 text-neutral-600" aria-hidden="true" />
                    </button>
                  ))}
                </div>
              </Section>
            )}

            <Section
              title={canvas?.title ?? 'Context map'}
              meta={`${formatCompactNumber(canvas?.node_count ?? nodes.length)} nodes / ${formatCompactNumber(canvas?.edge_count ?? edges.length)} links`}
              testId="strategic-context-workspace-map"
            >
              {nodes.length > 0 ? (
                <div className="rounded-md border border-neutral-800 bg-neutral-950/40 px-2.5">
                  {nodes.map(node => renderKeepableRow({
                    key: node.id,
                    label: node.label,
                    kind: node.kind,
                    detail: node.summary ?? null,
                    target: node.target,
                    ref: selectedRefFromNode(node),
                    badge: (
                      <>
                        {node.count != null && (
                          <span className="shrink-0 rounded border border-neutral-800 px-1 text-ui-sm font-semibold text-neutral-500">
                            {formatCompactNumber(node.count)}
                          </span>
                        )}
                        <StatusBadge status={node.status} />
                      </>
                    ),
                  }))}
                </div>
              ) : (
                <EmptyState>No map nodes are available.</EmptyState>
              )}
              {edges.length > 0 && (
                <div className="mt-3 rounded-md border border-neutral-800 bg-neutral-950/40 px-2.5" data-testid="strategic-context-workspace-edges">
                  {edges.map((edge, index) => (
                    <EdgeRow
                      key={`${edge.source}:${edge.target}:${edge.label ?? index}`}
                      source={nodeLabelById.get(edge.source) ?? edge.source}
                      target={nodeLabelById.get(edge.target) ?? edge.target}
                      label={edge.label}
                    />
                  ))}
                </div>
              )}
            </Section>

            <Section title="Resources" meta={`${resources.length}`} testId="strategic-context-workspace-resources">
              {resources.length > 0 ? (
                <div className="rounded-md border border-neutral-800 bg-neutral-950/40 px-2.5">
                  {resources.map(resource => renderKeepableRow({
                    key: `${resource.kind}:${resource.id}`,
                    label: resource.label,
                    kind: resource.kind,
                    detail: resource.summary,
                    target: resource.target,
                    ref: selectedRefFromResource(resource),
                  }))}
                </div>
              ) : (
                <EmptyState>No resources are attached to this package.</EmptyState>
              )}
            </Section>

            <Section
              title="Evidence"
              meta={`${knowledgeEvidence.length + artifactEvidence.length}`}
              testId="strategic-context-workspace-evidence"
            >
              {knowledgeEvidence.length === 0 && artifactEvidence.length === 0 ? (
                <EmptyState>No evidence is attached to this package.</EmptyState>
              ) : (
                <div className="grid gap-3 lg:grid-cols-2">
                  {knowledgeEvidence.length > 0 && (
                    <div className="rounded-md border border-neutral-800 bg-neutral-950/40 px-2.5">
                      {knowledgeEvidence.map(item => {
                        const ref = selectedRefFromKnowledgeEvidence(item);
                        return renderKeepableRow({
                          key: `knowledge:${item.id}`,
                          label: ref.label ?? 'Knowledge document',
                          kind: item.kind,
                          detail: item.summary ?? item.source_kind ?? null,
                          target: item.target,
                          ref,
                        });
                      })}
                    </div>
                  )}
                  {artifactEvidence.length > 0 && (
                    <div className="rounded-md border border-neutral-800 bg-neutral-950/40 px-2.5">
                      {artifactEvidence.map(item => {
                        const ref = selectedRefFromArtifactEvidence(item);
                        return renderKeepableRow({
                          key: `artifact:${item.id}`,
                          label: ref.label ?? 'Artifact',
                          kind: item.artifact_type ?? item.kind,
                          detail: item.task_title ?? item.task_status ?? null,
                          target: item.target,
                          ref,
                        });
                      })}
                    </div>
                  )}
                </div>
              )}
            </Section>

            {!contextPackage && (
              <Section title="Context package" testId="strategic-context-workspace-empty">
                <EmptyState>No Strategic context package is loaded for this project.</EmptyState>
              </Section>
            )}

            {contextPackage?.sources && contextPackage.sources.length > 0 && (
              <div className="flex min-w-0 flex-wrap gap-1.5 text-ui-sm text-neutral-500" data-testid="strategic-context-workspace-sources">
                <GitBranch className="h-3.5 w-3.5 text-neutral-600" aria-hidden="true" />
                {contextPackage.sources.map(source => (
                  <span key={source.kind} className="rounded border border-neutral-800 bg-neutral-950/60 px-1.5 py-0.5">
                    {source.kind.replace(/_/g, ' ')} {formatCompactNumber(source.count)}
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
