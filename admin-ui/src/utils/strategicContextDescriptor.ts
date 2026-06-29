import type { ProjectMessageContext } from '../types/messages';
import type { StrategicStateResponse, StrategicWorkbenchState } from '../strategicStateApi';

interface StrategicContextRefDescriptor {
  id: string;
  kind: string;
  label: string;
  status?: string | null;
}

export interface StrategicContextDescriptorModel {
  modeLabel: string;
  surfaceLabel: string;
  contextLabel: string | null;
  contextTone: 'ready' | 'building' | 'stale' | 'neutral';
  refCount: number;
  refs: StrategicContextRefDescriptor[];
  capturedAt?: string | null;
}

type StrategicSessionLike = {
  interaction_mode?: string | null;
  active_surface?: string | null;
  session_started_at?: string | null;
  context_package_id?: string | null;
  selected_refs?: StrategicWorkbenchState['selected_refs'];
  selected_ref_descriptors?: Array<StrategicContextRefDescriptor>;
  captured_at?: string | null;
};

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

function fallbackRefLabel(kind: string, id: string, label?: string): string {
  if (label?.trim()) return label.trim();
  return `${kind.replace(/_/g, ' ')} ${id.slice(0, 8)}`;
}

function contextTone(status?: string | null): StrategicContextDescriptorModel['contextTone'] {
  if (status === 'building') return 'building';
  if (status === 'stale') return 'stale';
  if (status === 'ready') return 'ready';
  return 'neutral';
}

function contextLabel(status?: string | null, hasContextPackage?: boolean): string | null {
  if (status === 'building') return 'Building context';
  if (status === 'stale') return 'Stale context';
  if (status === 'ready') return 'Context ready';
  return hasContextPackage ? 'Context captured' : null;
}

function hasStrategicSignal(session: StrategicSessionLike): boolean {
  return Boolean(
    (session.selected_refs?.length ?? 0) > 0 ||
    session.interaction_mode && session.interaction_mode !== 'chat' ||
    session.active_surface && session.active_surface !== 'strategic' ||
    session.session_started_at ||
    session.context_package_id,
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
        status: descriptor?.status,
      };
    });
  }
  return descriptors.map(ref => ({ ...ref, id: `${ref.kind}:${ref.id}` }));
}

function buildDescriptor(
  session: StrategicSessionLike,
  contextStatus?: string | null,
): StrategicContextDescriptorModel | null {
  if (!hasStrategicSignal(session)) return null;
  const refs = buildRefs(session, session.selected_ref_descriptors);
  return {
    modeLabel: modeLabel(session.interaction_mode),
    surfaceLabel: surfaceLabel(session.active_surface),
    contextLabel: contextLabel(contextStatus, Boolean(session.context_package_id)),
    contextTone: contextTone(contextStatus),
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
  return buildDescriptor(
    {
      interaction_mode: workbench?.interaction_mode ?? session?.mode ?? 'chat',
      active_surface: workbench?.active_surface ?? session?.active_surface ?? 'strategic',
      session_started_at: workbench?.session_started_at ?? session?.session_started_at ?? null,
      context_package_id: session?.context_package_id ?? state?.context_package?.id ?? null,
      selected_refs: selectedRefs,
      selected_ref_descriptors: state?.work_session?.selected_ref_descriptors ?? [],
    },
    state?.context_package?.status ?? null,
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
      status: asString(record?.status),
    }];
  });
}

export function buildMessageStrategicContextDescriptor(
  context: ProjectMessageContext,
): StrategicContextDescriptorModel | null {
  const snapshot = asRecord(context.strategic_session);
  if (!snapshot) return null;
  return buildDescriptor({
    interaction_mode: asString(snapshot.interaction_mode) ?? 'chat',
    active_surface: asString(snapshot.active_surface) ?? 'strategic',
    session_started_at: asString(snapshot.session_started_at),
    context_package_id: asString(snapshot.context_package_id),
    selected_refs: parseSelectedRefs(snapshot.selected_refs),
    selected_ref_descriptors: parseRefDescriptors(snapshot.selected_ref_descriptors),
    captured_at: asString(snapshot.captured_at),
  });
}
