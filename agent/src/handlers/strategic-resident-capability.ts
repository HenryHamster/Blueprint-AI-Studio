/** Pure helpers for the bounded Strategic Resident capability catalog. */
const MUTATION_EVIDENCE_KEYS = [
  'kind', 'project_id', 'source_id', 'revision', 'checksum', 'inspected_at',
] as const;
const MUTATION_EVIDENCE_INPUT_KEYS = new Set([
  ...MUTATION_EVIDENCE_KEYS, 'evidence_id', 'summary', 'decisive', 'data',
]);
const INSPECTION_RECEIPT_UNAVAILABLE =
  'canonical inspection receipt is unavailable or inconsistent';

export class ResidentMutationEvidenceUnavailableError extends Error {
  constructor(index: number) {
    super(`Resident mutation evidence[${index}] is not backed by one server inspection`);
    this.name = 'ResidentMutationEvidenceUnavailableError';
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const entries = Object.entries(value as Record<string, unknown>)
      .sort(([left], [right]) => left.localeCompare(right));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(',')}}`;
  }
  if (typeof value === 'string') return JSON.stringify(value);
  if (typeof value === 'number') return Number.isFinite(value) ? String(value) : 'null';
  if (typeof value === 'boolean') return String(value);
  return 'null';
}

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
export function rejectedCapabilityOutcome(
  capabilityId: string,
  mode: ResidentCapabilityMode,
  reasonCode: 'invalid_mutation_evidence' | 'canonical_inspection_replay_unavailable',
): Record<string, unknown> {
  return {
    capability_id: capabilityId,
    mode,
    status: 'rejected',
    reason_code: reasonCode,
    effect: 'none',
    retryable: false,
  };
}

export function canonicalInspectionReceiptUnavailable(error: unknown): boolean {
  if (!(error instanceof Error)) return false;
  const prefix = 'POST /api/agent/resident-turn/capability failed (409): ';
  if (!error.message.startsWith(prefix)) return false;
  let body: unknown;
  try {
    body = JSON.parse(error.message.slice(prefix.length));
  } catch {
    return false;
  }
  if (!isRecord(body)) return false;
  if (body.detail === INSPECTION_RECEIPT_UNAVAILABLE) return true;
  const wrapped = body.error;
  return isRecord(wrapped)
    && wrapped.code === 409
    && wrapped.message === INSPECTION_RECEIPT_UNAVAILABLE;
}
export function canonicalMutationEvidence(
  items: unknown[],
  serverInspectionEvidence: Record<string, unknown>[],
  projectId: string,
): Record<string, unknown>[] {
  if (items.length > 50) {
    throw new Error('Resident mutation evidence exceeds 50 items');
  }
  const canonicalIdentities = new Set<string>();
  return items.map((value, index) => {
    if (!isRecord(value)) {
      throw new Error(`Resident mutation evidence[${index}] must be an object`);
    }
    const unknownKey = Object.keys(value).find(
      (key) => !MUTATION_EVIDENCE_INPUT_KEYS.has(key),
    );
    if (unknownKey !== undefined) {
      throw new Error(`Resident mutation evidence[${index}] contains an unknown field: ${unknownKey}`);
    }
    if (
      value.evidence_id !== undefined
      && (!nonEmptyString(value.evidence_id) || value.evidence_id.length > 120)
    ) {
      throw new Error(`Resident mutation evidence[${index}].evidence_id must be a bounded non-empty string`);
    }
    if (
      value.summary !== undefined
      && (!nonEmptyString(value.summary) || value.summary.length > 4_000)
    ) {
      throw new Error(`Resident mutation evidence[${index}].summary must be a bounded non-empty string`);
    }
    if (value.decisive !== undefined && typeof value.decisive !== 'boolean') {
      throw new Error(`Resident mutation evidence[${index}].decisive must be a boolean`);
    }
    for (const key of MUTATION_EVIDENCE_KEYS) {
      if (!nonEmptyString(value[key])) {
        throw new Error(`Resident mutation evidence[${index}].${key} must be a non-empty string`);
      }
    }
    if (value.project_id !== projectId) {
      throw new Error(`Resident mutation evidence[${index}] belongs to another project`);
    }
    const matches = serverInspectionEvidence.filter((candidate) => (
      MUTATION_EVIDENCE_KEYS.every((key) => (
        nonEmptyString(candidate[key]) && candidate[key] === value[key]
      ))
      && (
        value.data === undefined
        || (
          Object.prototype.hasOwnProperty.call(candidate, 'data')
          && stableJson(candidate.data) === stableJson(value.data)
        )
      )
    ));
    if (matches.length !== 1) {
      throw new ResidentMutationEvidenceUnavailableError(index);
    }
    const inspected = matches[0];
    const canonical = Object.fromEntries(
      MUTATION_EVIDENCE_KEYS.map((key) => [key, inspected[key]]),
    );
    const identity = stableJson(canonical);
    if (canonicalIdentities.has(identity)) {
      throw new Error('Resident mutation evidence contains a duplicate server inspection');
    }
    canonicalIdentities.add(identity);
    return canonical;
  });
}
