/** Pure helpers for the bounded Strategic Resident capability catalog. */

export type ResidentCapabilityMode = 'inspect' | 'mutate';

export function catalogAvailability(
  providerInput: Record<string, unknown>,
  capabilityId: string,
  mode: ResidentCapabilityMode,
): string | null {
  const capabilities = providerInput.capabilities;
  const catalog = capabilities && typeof capabilities === 'object'
    ? (capabilities as Record<string, unknown>).catalog
    : null;
  if (!Array.isArray(catalog)) return null;
  const entries: unknown[] = catalog;
  const entry: unknown = entries.find((candidate) => (
    candidate
    && typeof candidate === 'object'
    && (candidate as Record<string, unknown>).id === capabilityId
  ));
  if (!entry || typeof entry !== 'object') return null;
  const item = entry as Record<string, unknown>;
  return item.mode === mode && typeof item.availability === 'string'
    ? item.availability
    : null;
}

export function proposedCapabilityRejection(
  capabilityId: string,
  mode: ResidentCapabilityMode,
): Record<string, unknown> {
  return {
    capability_id: capabilityId,
    mode,
    status: 'rejected',
    error: 'Capability availability is propose_only; include it only as a proposed final operation.',
  };
}
