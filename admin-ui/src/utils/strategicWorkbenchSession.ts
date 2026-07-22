import type { PrimaryTab } from '../hooks/useUIState';
import type { StrategicInteractionMode } from '../components/StrategicSessionControl';
import type { StrategicWorkbenchState } from '../strategicStateApi';
import { SESSION_IDLE_TTL_MS } from './strategicContextDescriptor';
import { parseTimestamp } from './time';

export const MAX_STRATEGIC_SELECTED_REFS = 12;
export type StrategicSelectedRef = StrategicWorkbenchState['selected_refs'][number];

export function normalizeStrategicInteractionMode(mode?: string | null): StrategicInteractionMode {
  // The vocabulary collapsed to Chat + Workday; a retired `check_in` / `monitor`
  // value (from old data or a stale client) coerces to `chat`.
  if (mode === 'continuous') return 'continuous';
  return 'chat';
}

export function workbenchSurfaceForTab(tab: PrimaryTab): StrategicWorkbenchState['active_surface'] | null {
  if (tab === 'chat') return 'strategic';
  if (tab === 'pipeline') return 'pipeline';
  if (tab === 'project') return 'project';
  if (tab === 'runtime') return 'runtime';
  // 'fleet' has no strategic workbench surface
  return null;
}

export function tabForWorkbenchSurface(surface?: string | null): PrimaryTab | null {
  if (surface === 'strategic') return 'chat';
  if (surface === 'pipeline' || surface === 'project' || surface === 'runtime') return surface;
  return null;
}

function compactRefString(value: unknown, maxChars: number): string | null {
  if (typeof value !== 'string') return null;
  const text = value.trim().replace(/\s+/g, ' ');
  if (!text) return null;
  return text.slice(0, maxChars);
}

export function normalizeStrategicSelectedRefs(refs: unknown): StrategicSelectedRef[] {
  if (!Array.isArray(refs)) return [];
  const seen = new Set<string>();
  const normalized: StrategicSelectedRef[] = [];
  for (const entry of refs) {
    if (!entry || typeof entry !== 'object') continue;
    const raw = entry as Partial<StrategicSelectedRef>;
    const kind = compactRefString(raw.kind, 40);
    const id = compactRefString(raw.id, 120);
    if (!kind || !id) continue;
    const key = `${kind}:${id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    const label = compactRefString(raw.label, 160);
    normalized.push(label ? { kind, id, label } : { kind, id });
    if (normalized.length >= MAX_STRATEGIC_SELECTED_REFS) break;
  }
  return normalized;
}

export function upsertStrategicSelectedRef(
  refs: StrategicSelectedRef[] | null | undefined,
  ref: StrategicSelectedRef,
): StrategicSelectedRef[] {
  const normalizedRef = normalizeStrategicSelectedRefs([ref])[0];
  if (!normalizedRef) return normalizeStrategicSelectedRefs(refs ?? []);
  const withoutRef = normalizeStrategicSelectedRefs(refs ?? [])
    .filter(existing => !(existing.kind === normalizedRef.kind && existing.id === normalizedRef.id));
  return normalizeStrategicSelectedRefs([normalizedRef, ...withoutRef]);
}

export function removeStrategicSelectedRef(
  refs: StrategicSelectedRef[] | null | undefined,
  ref: Pick<StrategicSelectedRef, 'kind' | 'id'>,
): StrategicSelectedRef[] {
  return normalizeStrategicSelectedRefs(refs ?? [])
    .filter(existing => !(existing.kind === ref.kind && existing.id === ref.id));
}

function isBaseSessionStale(base: StrategicWorkbenchState | null | undefined): boolean {
  const ts = base?.last_interaction_at ?? base?.session_started_at;
  if (!ts) return false;
  const parsed = parseTimestamp(ts);
  if (isNaN(parsed.getTime())) return false;
  return Date.now() - parsed.getTime() > SESSION_IDLE_TTL_MS;
}

export function buildOptimisticStrategicWorkbench(
  base: StrategicWorkbenchState | null | undefined,
  patch: Partial<StrategicWorkbenchState>,
): StrategicWorkbenchState {
  const now = new Date().toISOString();
  const baseSessionStartedAt = !isBaseSessionStale(base) ? (base?.session_started_at ?? null) : null;
  return {
    version: 1,
    interaction_mode: normalizeStrategicInteractionMode(
      typeof patch.interaction_mode === 'string' ? patch.interaction_mode : base?.interaction_mode,
    ),
    active_surface: typeof patch.active_surface === 'string' ? patch.active_surface : base?.active_surface ?? 'strategic',
    selected_refs: normalizeStrategicSelectedRefs(
      Array.isArray(patch.selected_refs) ? patch.selected_refs : base?.selected_refs ?? [],
    ),
    session_started_at: baseSessionStartedAt ?? now,
    last_interaction_at: now,
  };
}
