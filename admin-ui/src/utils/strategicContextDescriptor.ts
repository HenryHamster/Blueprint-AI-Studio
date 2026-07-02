import type { ProjectMessageContext } from '../types/messages';
import type { StrategicStateResponse, StrategicWorkbenchState } from '../strategicStateApi';
import { parseTimestamp } from './time';

export const SESSION_IDLE_TTL_MS = 14_400_000; // 4 hours — mirror of backend SESSION_IDLE_TTL_SECONDS

interface StrategicContextRefDescriptor {
  id: string;
  kind: string;
  label: string;
  summary?: string | null;
  status?: string | null;
}

interface StrategicContextPackageSnapshotDescriptor {
  id: string;
  label?: string | null;
  summary?: string | null;
  status?: string | null;
}

export interface StrategicContextDescriptorModel {
  modeLabel: string;
  surfaceLabel: string;
  packageTitle?: string | null;
  contextLabel: string | null;
  contextTone: 'ready' | 'building' | 'stale' | 'neutral';
  receiptLabel?: string | null;
  itemCount?: number | null;
  canvas?: {
    nodeCount: number;
    edgeCount: number;
  } | null;
  refCount: number;
  refs: StrategicContextRefDescriptor[];
  capturedAt?: string | null;
}

type StrategicSessionLike = {
  interaction_mode?: string | null;
  active_surface?: string | null;
  session_started_at?: string | null;
  last_interaction_at?: string | null;
  context_package_id?: string | null;
  context_package_descriptor?: StrategicContextPackageSnapshotDescriptor | null;
  selected_refs?: StrategicWorkbenchState['selected_refs'];
  selected_ref_descriptors?: Array<StrategicContextRefDescriptor>;
  captured_at?: string | null;
};

function isSessionFresh(session: StrategicSessionLike): boolean {
  const ts = session.last_interaction_at ?? session.session_started_at;
  if (!ts) return false;
  const parsed = parseTimestamp(ts);
  if (isNaN(parsed.getTime())) return false;
  return Date.now() - parsed.getTime() <= SESSION_IDLE_TTL_MS;
}

function modeLabel(mode?: string | null): string {
  if (mode === 'check_in') return 'Check-in';
  if (mode === 'monitor') return 'Monitor';
  if (mode === 'continuous') return 'Workday';
  return 'Chat';
}

function surfaceLabel(surface?: string | null): string {
  if (surface === 'pipeline') return 'Pipeline';
  if (surface === 'project') return 'Project';
  if (surface === 'runtime') return 'Runtime';
  return 'Strategic';
}

function isNonDefaultMode(mode?: string | null): boolean {
  return Boolean(mode && mode !== 'chat');
}

function isNonDefaultSurface(surface?: string | null): boolean {
  return Boolean(surface && surface !== 'strategic');
}

function fallbackRefLabel(kind: string, id: string, label?: string): string {
  if (label?.trim()) return label.trim();
  return `${kind.replace(/_/g, ' ')} ${id.slice(0, 8)}`;
}

function normalizedStatus(status?: string | null): string | null {
  const normalized = status?.trim().toLowerCase();
  return normalized || null;
}

function contextTone(status?: string | null): StrategicContextDescriptorModel['contextTone'] {
  const normalized = normalizedStatus(status);
  if (normalized === 'building') return 'building';
  if (normalized === 'stale') return 'stale';
  if (normalized === 'ready') return 'ready';
  return 'neutral';
}

function contextLabel(status?: string | null, hasContextPackage?: boolean): string | null {
  const normalized = normalizedStatus(status);
  if (normalized === 'building') return 'Building context';
  if (normalized === 'stale') return 'Stale context';
  if (normalized === 'ready') return 'Context ready';
  return hasContextPackage ? 'Context captured' : null;
}

function humanStatusLabel(status: string): string {
  const label = status.replace(/[_-]+/g, ' ').trim();
  return label ? label.charAt(0).toUpperCase() + label.slice(1) : 'Context';
}

function capturedContextLabel(status?: string | null): string | null {
  const normalized = normalizedStatus(status);
  if (!normalized || normalized === 'ready') return null;
  if (normalized === 'building') return 'Building context';
  if (normalized === 'stale') return 'Stale context';
  if (normalized === 'actionable') return 'Actionable context';
  return `${humanStatusLabel(normalized)} context`;
}

function isUsefulContextStatus(status?: string | null): boolean {
  return capturedContextLabel(status) !== null;
}

export function isSessionStartedAtStale(session: StrategicSessionLike): boolean {
  const ts = session.last_interaction_at ?? session.session_started_at;
  if (!ts) return false;
  return !isSessionFresh(session);
}

function hasStrategicSignal(session: StrategicSessionLike): boolean {
  const fresh = isSessionFresh(session);
  return Boolean(
    session.context_package_id ||
    (fresh && (
      (session.selected_refs?.length ?? 0) > 0 ||
      session.interaction_mode && session.interaction_mode !== 'chat' ||
      session.active_surface && session.active_surface !== 'strategic' ||
      Boolean(session.session_started_at) ||
      Boolean(session.last_interaction_at)
    )),
  );
}

function buildRefs(
  session: StrategicSessionLike,
  descriptors: StrategicContextRefDescriptor[] = [],
): StrategicContextRefDescriptor[] {
  const descriptorByRef = new Map(descriptors.map(ref => [`${ref.kind}:${ref.id}`, ref]));
  const selectedRefs = session.selected_refs ?? [];
  if (selectedRefs.length > 0) {
    return selectedRefs.map(ref => {
      const descriptor = descriptorByRef.get(`${ref.kind}:${ref.id}`);
      return {
        id: `${ref.kind}:${ref.id}`,
        kind: ref.kind,
        label: descriptor?.label ?? fallbackRefLabel(ref.kind, ref.id, ref.label),
        summary: descriptor?.summary,
        status: descriptor?.status,
      };
    });
  }
  return descriptors.map(ref => ({ ...ref, id: `${ref.kind}:${ref.id}` }));
}

function buildDescriptor(
  session: StrategicSessionLike,
  contextStatus?: string | null,
  packageDetails?: Pick<StrategicContextDescriptorModel, 'packageTitle' | 'itemCount' | 'canvas'>,
): StrategicContextDescriptorModel | null {
  if (!hasStrategicSignal(session)) return null;
  const fresh = isSessionFresh(session);
  const refs = fresh ? buildRefs(session, session.selected_ref_descriptors) : [];
  return {
    modeLabel: fresh ? modeLabel(session.interaction_mode) : 'Chat',
    surfaceLabel: fresh ? surfaceLabel(session.active_surface) : 'Strategic',
    packageTitle: packageDetails?.packageTitle ?? null,
    contextLabel: contextLabel(contextStatus, Boolean(session.context_package_id)),
    contextTone: contextTone(contextStatus),
    itemCount: packageDetails?.itemCount ?? null,
    canvas: packageDetails?.canvas ?? null,
    refCount: refs.length,
    refs: refs.slice(0, 3),
    capturedAt: session.captured_at ?? null,
  };
}

function hasCapturedStrategicSignal(session: StrategicSessionLike): boolean {
  const refs = buildRefs(session, session.selected_ref_descriptors);
  const contextStatus = session.context_package_descriptor?.status;
  const hasExplicitAttachment = Boolean(
    session.context_package_descriptor?.summary ||
    isUsefulContextStatus(contextStatus),
  );
  return Boolean(
    refs.length > 0 ||
    isNonDefaultMode(session.interaction_mode) ||
    isNonDefaultSurface(session.active_surface) ||
    isUsefulContextStatus(contextStatus) ||
    hasExplicitAttachment
  );
}

function capturedReceiptLabel(
  session: StrategicSessionLike,
  refs: StrategicContextRefDescriptor[],
  contextLabelValue: string | null,
): string | null {
  if (refs.length > 0) return 'Refs attached';
  if (isNonDefaultSurface(session.active_surface)) return `${surfaceLabel(session.active_surface)} context`;
  if (contextLabelValue) return contextLabelValue;
  if (isNonDefaultMode(session.interaction_mode)) return `${modeLabel(session.interaction_mode)} context`;
  if (session.context_package_descriptor?.summary) return 'Context attached';
  return null;
}

function buildCapturedDescriptor(
  session: StrategicSessionLike,
): StrategicContextDescriptorModel | null {
  if (!hasCapturedStrategicSignal(session)) return null;
  const refs = buildRefs(session, session.selected_ref_descriptors);
  const packageDescriptor = session.context_package_descriptor ?? null;
  const label = capturedContextLabel(packageDescriptor?.status);
  return {
    modeLabel: modeLabel(session.interaction_mode),
    surfaceLabel: surfaceLabel(session.active_surface),
    packageTitle: packageDescriptor?.label ?? null,
    contextLabel: label,
    contextTone: contextTone(packageDescriptor?.status),
    receiptLabel: capturedReceiptLabel(session, refs, label),
    itemCount: null,
    canvas: null,
    refCount: refs.length,
    refs: refs.slice(0, 3),
    capturedAt: session.captured_at ?? null,
  };
}

export function buildStrategicContextDescriptor(
  state?: StrategicStateResponse | null,
  workbenchOverride?: StrategicWorkbenchState | null,
): StrategicContextDescriptorModel | null {
  const session = state?.work_session ?? null;
  const workbench = workbenchOverride ?? state?.workbench ?? null;
  const selectedRefs = workbench?.selected_refs ?? session?.selected_refs ?? [];
  const packageCanvas = state?.context_package?.canvas;
  return buildDescriptor(
    {
      interaction_mode: workbench?.interaction_mode ?? session?.mode ?? 'chat',
      active_surface: workbench?.active_surface ?? session?.active_surface ?? 'strategic',
      session_started_at: workbench?.session_started_at ?? session?.session_started_at ?? null,
      last_interaction_at: workbench?.last_interaction_at ?? session?.last_interaction_at ?? null,
      context_package_id: session?.context_package_id ?? state?.context_package?.id ?? null,
      selected_refs: selectedRefs,
      selected_ref_descriptors: state?.work_session?.selected_ref_descriptors ?? [],
    },
    state?.context_package?.status ?? null,
    {
      packageTitle: state?.context_package?.title ?? null,
      itemCount: state?.context_package?.item_count ?? null,
      canvas: packageCanvas
        ? {
          nodeCount: packageCanvas.node_count ?? packageCanvas.nodes?.length ?? 0,
          edgeCount: packageCanvas.edge_count ?? packageCanvas.edges?.length ?? 0,
        }
        : null,
    },
  );
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value.trim() : null;
}

function parseSelectedRefs(value: unknown): StrategicWorkbenchState['selected_refs'] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    const record = asRecord(entry);
    const kind = asString(record?.kind);
    const id = asString(record?.id);
    if (!kind || !id) return [];
    const label = asString(record?.label);
    return [{ kind, id, ...(label ? { label } : {}) }];
  });
}

function parseRefDescriptors(value: unknown): StrategicContextRefDescriptor[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((entry) => {
    const record = asRecord(entry);
    const kind = asString(record?.kind);
    const id = asString(record?.id);
    const label = asString(record?.label);
    if (!kind || !id || !label) return [];
    return [{
      id,
      kind,
      label,
      summary: asString(record?.summary),
      status: asString(record?.status),
    }];
  });
}

function parseContextPackageDescriptor(value: unknown): StrategicContextPackageSnapshotDescriptor | null {
  const record = asRecord(value);
  if (!record) return null;
  const id = asString(record.id) ?? asString(record.context_package_id);
  if (!id) return null;
  return {
    id,
    label: asString(record.label) ?? asString(record.title),
    summary: asString(record.summary),
    status: asString(record.status),
  };
}

export function buildMessageStrategicContextDescriptor(
  context: ProjectMessageContext,
): StrategicContextDescriptorModel | null {
  const snapshot = asRecord(context.strategic_session);
  if (!snapshot) return null;
  return buildCapturedDescriptor({
    interaction_mode: asString(snapshot.interaction_mode) ?? 'chat',
    active_surface: asString(snapshot.active_surface) ?? 'strategic',
    session_started_at: asString(snapshot.session_started_at),
    last_interaction_at: asString(snapshot.last_interaction_at),
    context_package_id: asString(snapshot.context_package_id),
    context_package_descriptor: parseContextPackageDescriptor(
      snapshot.context_package_descriptor ?? snapshot.context_package,
    ),
    selected_refs: parseSelectedRefs(snapshot.selected_refs),
    selected_ref_descriptors: parseRefDescriptors(snapshot.selected_ref_descriptors),
    captured_at: asString(snapshot.captured_at),
  });
}
