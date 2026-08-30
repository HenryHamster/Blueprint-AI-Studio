/** Transcript-free, tool-less strategic successor proof. */

import { createHash, randomUUID } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import type WebSocket from 'ws';
import { apiGet, apiPost, sanitize, sendLog } from '../index.js';
import {
  RuntimeUnavailableError,
  runIsolatedTextOnlyMemory,
} from '../providers/runners.js';

const MAX_PAYLOAD_BYTES = 190 * 1024;
const SHA256_RE = /^[0-9a-f]{64}$/;

type JsonObject = Record<string, unknown>;
type AttestationFailureDisposition = 'confirmed_pending' | 'ambiguous' | 'conflict';

class AttestationDeliveryError extends Error {
  constructor(
    readonly disposition: AttestationFailureDisposition,
    message: string,
    readonly cause?: unknown,
  ) {
    super(message);
    this.name = 'AttestationDeliveryError';
  }
}

interface ValidatedColdSuccessor {
  projectId: string;
  sessionId: string;
  workerGeneration: number;
  core: JsonObject;
  problemModel: string;
  runningUpdate: string | null;
  strategicMemory: string;
  hashes: {
    core: string;
    problemModel: string;
    runningUpdate: string;
    strategicMemory: string;
  };
}

function object(value: unknown, field: string): JsonObject {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error(`cold successor ${field} must be an object`);
  }
  return value as JsonObject;
}

function exactKeys(value: JsonObject, expected: string[], field: string): void {
  const actual = Object.keys(value).sort();
  const wanted = [...expected].sort();
  if (actual.length !== wanted.length || actual.some((key, index) => key !== wanted[index])) {
    throw new Error(`cold successor ${field} has unexpected fields`);
  }
}

function text(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.length === 0) {
    throw new Error(`cold successor ${field} must be non-empty text`);
  }
  return value;
}

function errorFromUnknown(value: unknown, fallback: string): Error {
  if (value instanceof Error) return value;
  if (typeof value === 'string' && value.length > 0) return new Error(value);
  return new Error(fallback);
}

function errorMessage(value: unknown): string {
  if (value instanceof Error) return value.message;
  if (typeof value === 'string') return value;
  return 'unknown error';
}

function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(',')}]`;
  if (value && typeof value === 'object') {
    const source = value as JsonObject;
    return `{${Object.keys(source).sort().map((key) => (
      `${JSON.stringify(key)}:${canonicalJson(source[key])}`
    )).join(',')}}`;
  }
  const encoded = JSON.stringify(value);
  return encoded;
}

function sha256(value: string): string {
  return createHash('sha256').update(value, 'utf8').digest('hex');
}

function validateSection(
  raw: unknown,
  field: string,
  expectedEncoding: string,
  exactBytes: string,
): string {
  const section = object(raw, `manifest.${field}`);
  const expectedKeys = field === 'strategic_memory'
    ? ['encoding', 'byte_count', 'sha256', 'document_count']
    : ['encoding', 'byte_count', 'sha256'];
  exactKeys(section, expectedKeys, `manifest.${field}`);
  if (section.encoding !== expectedEncoding) {
    throw new Error(`cold successor manifest.${field}.encoding is invalid`);
  }
  if (!Number.isInteger(section.byte_count) || section.byte_count !== Buffer.byteLength(exactBytes, 'utf8')) {
    throw new Error(`cold successor manifest.${field}.byte_count is invalid`);
  }
  if (typeof section.sha256 !== 'string' || !SHA256_RE.test(section.sha256)
      || section.sha256 !== sha256(exactBytes)) {
    throw new Error(`cold successor manifest.${field}.sha256 is invalid`);
  }
  return section.sha256;
}

export function classifyColdSuccessorClaim(
  payload: JsonObject,
  task: JsonObject,
): 'ordinary' | 'cold' {
  const context = task.context && typeof task.context === 'object' && !Array.isArray(task.context)
    ? task.context as JsonObject
    : {};
  const payloadHas = Object.hasOwn(payload, 'cold_successor_contract');
  const contextHas = Object.hasOwn(context, '_cold_successor');
  if (!payloadHas && !contextHas) return 'ordinary';
  if (
    payload.type !== 'strategic_review'
    || !payload.cold_successor_contract
    || typeof payload.cold_successor_contract !== 'object'
    || Array.isArray(payload.cold_successor_contract)
    || context._cold_successor !== true
  ) {
    throw new Error('cold successor claim markers are incomplete or malformed');
  }
  return 'cold';
}

export function preflightColdSuccessorClaim(
  payload: JsonObject,
  task: JsonObject,
): ValidatedColdSuccessor | null {
  if (classifyColdSuccessorClaim(payload, task) === 'ordinary') return null;
  exactKeys(task, [
    'task_id', 'payloads', 'context', 'project_metadata', 'task_capability',
  ], 'claim response');
  text(task.task_id, 'task id');
  if (!Array.isArray(task.payloads) || task.payloads.length !== 1) {
    throw new Error('cold successor claim must contain one exact payload');
  }
  const claimedPayload = object(task.payloads[0], 'claim payload');
  if (canonicalJson(claimedPayload) !== canonicalJson(payload)) {
    throw new Error('cold successor claim payload identity is inconsistent');
  }
  exactKeys(payload, [
    'type', 'cold_successor_contract', 'requires_browser',
  ], 'claim payload');
  if (payload.requires_browser !== false) {
    throw new Error('cold successor claim must deny browser access');
  }
  if (Buffer.byteLength(JSON.stringify(payload), 'utf8') > MAX_PAYLOAD_BYTES) {
    throw new Error('cold successor payload exceeds the exact byte limit');
  }
  const context = object(task.context, 'task context');
  exactKeys(context, [
    '_cold_successor', 'strategic_session_id', '_resident_worker_generation',
  ], 'task context');
  const capability = object(task.task_capability, 'task capability');
  exactKeys(capability, ['workspace_requirement'], 'task capability');
  if (capability.workspace_requirement !== 'none') {
    throw new Error('cold successor capability must deny workspace access');
  }
  const contract = object(payload.cold_successor_contract, 'contract');
  exactKeys(contract, ['core', 'manifest'], 'contract');
  const core = object(contract.core, 'core');
  exactKeys(core, [
    'schema_version', 'project_id', 'safety', 'problem_model_canonical_json',
    'running_update', 'strategic_memory_canonical_json',
  ], 'core');
  if (core.schema_version !== 1) throw new Error('cold successor schema_version is invalid');
  const projectId = text(core.project_id, 'project_id');
  const projectMetadata = object(task.project_metadata, 'project metadata');
  exactKeys(projectMetadata, ['project_id'], 'project metadata');
  if (projectMetadata.project_id !== projectId) {
    throw new Error('cold successor project identity is inconsistent');
  }
  const safety = object(core.safety, 'safety');
  exactKeys(safety, ['paused', 'autonomous_enabled', 'archived'], 'safety');
  if (safety.paused !== true || safety.autonomous_enabled !== false || safety.archived !== false) {
    throw new Error('cold successor safety fence is not exact');
  }
  const problemModel = text(core.problem_model_canonical_json, 'problem model');
  const strategicMemory = text(core.strategic_memory_canonical_json, 'strategic memory');
  let parsedProblemModel: unknown;
  let parsedMemory: unknown;
  try {
    parsedProblemModel = JSON.parse(problemModel);
    parsedMemory = JSON.parse(strategicMemory);
  } catch {
    throw new Error('cold successor canonical source JSON is invalid');
  }
  if (!parsedProblemModel || typeof parsedProblemModel !== 'object' || Array.isArray(parsedProblemModel)) {
    throw new Error('cold successor problem model source is invalid');
  }
  if (!Array.isArray(parsedMemory)) {
    throw new Error('cold successor strategic memory source is invalid');
  }
  if (problemModel !== canonicalJson(parsedProblemModel)
      || strategicMemory !== canonicalJson(parsedMemory)) {
    throw new Error('cold successor canonical source JSON is not canonical');
  }
  if (parsedMemory.length !== 1) {
    throw new Error('cold successor strategic memory must contain one handoff document');
  }
  const handoffMemory = object(parsedMemory[0], 'strategic memory document');
  exactKeys(handoffMemory, [
    'id', 'title', 'summary', 'body', 'topic', 'direction_id', 'tags', 'created_at',
  ], 'strategic memory document');
  if (
    typeof handoffMemory.id !== 'string'
    || typeof handoffMemory.title !== 'string'
    || typeof handoffMemory.summary !== 'string'
    || typeof handoffMemory.body !== 'string'
    || (handoffMemory.topic !== null && typeof handoffMemory.topic !== 'string')
    || (handoffMemory.direction_id !== null && typeof handoffMemory.direction_id !== 'string')
    || typeof handoffMemory.created_at !== 'string'
    || !Array.isArray(handoffMemory.tags)
    || handoffMemory.tags.some(tag => typeof tag !== 'string')
    || !handoffMemory.tags.includes('handoff')
  ) {
    throw new Error('cold successor strategic memory handoff document is invalid');
  }
  const running = object(core.running_update, 'running update');
  exactKeys(running, ['present', 'text'], 'running update');
  if (typeof running.present !== 'boolean'
      || (running.present && typeof running.text !== 'string')
      || (!running.present && running.text !== null)) {
    throw new Error('cold successor running update null/empty identity is invalid');
  }
  const runningUpdate = running.text as string | null;
  const manifest = object(contract.manifest, 'manifest');
  exactKeys(manifest, ['problem_model', 'running_update', 'strategic_memory', 'core'], 'manifest');
  const problemModelHash = validateSection(
    manifest.problem_model, 'problem_model', 'canonical-json', problemModel,
  );
  const runningUpdateHash = validateSection(
    manifest.running_update,
    'running_update',
    running.present ? 'utf-8' : 'json-null',
    running.present ? runningUpdate as string : 'null',
  );
  const strategicMemoryHash = validateSection(
    manifest.strategic_memory, 'strategic_memory', 'canonical-json', strategicMemory,
  );
  const memorySection = object(manifest.strategic_memory, 'manifest.strategic_memory');
  if (!Number.isInteger(memorySection.document_count)
      || memorySection.document_count !== parsedMemory.length) {
    throw new Error('cold successor strategic memory count is invalid');
  }
  const coreHash = validateSection(manifest.core, 'core', 'canonical-json', canonicalJson(core));
  const sessionId = text(context.strategic_session_id, 'session id');
  const workerGeneration = context._resident_worker_generation;
  if (!Number.isInteger(workerGeneration) || (workerGeneration as number) < 1) {
    throw new Error('cold successor worker generation is invalid');
  }
  return Object.freeze({
    projectId,
    sessionId,
    workerGeneration: workerGeneration as number,
    core: Object.freeze({ ...core }),
    problemModel,
    runningUpdate,
    strategicMemory,
    hashes: Object.freeze({
      core: coreHash,
      problemModel: problemModelHash,
      runningUpdate: runningUpdateHash,
      strategicMemory: strategicMemoryHash,
    }),
  });
}

function parseSemanticResult(raw: string): string {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    throw new Error('cold successor provider result is not exact JSON');
  }
  const result = object(parsed, 'provider result');
  exactKeys(result, ['schema_version', 'handoff_state', 'semantic_result'], 'provider result');
  if (result.schema_version !== 1 || result.handoff_state !== 'loaded') {
    throw new Error('cold successor provider did not attest loaded state');
  }
  const semanticResult = text(result.semantic_result, 'semantic result');
  if (semanticResult.length > 4000) throw new Error('cold successor semantic result is too large');
  return semanticResult;
}

type ColdFailureReason =
  | 'provider_terminal_failed'
  | 'provider_result_invalid'
  | 'authority_renewal_failed'
  | 'attestation_failed'
  | 'internal_error';

interface ColdReceipt {
  receiptState: 'pending' | 'attested' | 'terminal';
  completionTerminalEffectId: string;
  attestation: JsonObject | null;
  terminal: JsonObject | null;
}

function exactField(value: JsonObject, key: string, expected: unknown): boolean {
  return Object.hasOwn(value, key) && value[key] === expected;
}

function parseColdReceipt(
  raw: unknown,
  identity: JsonObject,
): ColdReceipt {
  const receipt = object(raw, 'durable receipt');
  exactKeys(receipt, [
    'schema_version', 'receipt_state', 'session_id', 'task_id',
    'worker_generation', 'task_status', 'completion_terminal_effect_id',
    'attestation', 'terminal',
  ], 'durable receipt');
  if (receipt.schema_version !== 1
      || !exactField(receipt, 'session_id', identity.session_id)
      || !exactField(receipt, 'task_id', identity.task_id)
      || !exactField(receipt, 'worker_generation', identity.worker_generation)
      || typeof receipt.completion_terminal_effect_id !== 'string'
      || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
        .test(receipt.completion_terminal_effect_id)) {
    throw new Error('cold successor durable receipt identity is invalid');
  }
  if (!['pending', 'attested', 'terminal'].includes(String(receipt.receipt_state))) {
    throw new Error('cold successor durable receipt state is invalid');
  }
  const attestation = receipt.attestation === null
    ? null : object(receipt.attestation, 'durable receipt attestation');
  const terminal = receipt.terminal === null
    ? null : object(receipt.terminal, 'durable receipt terminal');
  if (attestation) {
    exactKeys(attestation, [
      'schema_version', 'session_id', 'task_id', 'worker_generation',
      'resident_incarnation_id', 'core_sha256', 'problem_model_sha256',
      'running_update_sha256', 'strategic_memory_sha256',
      'provider_terminal_reason', 'provider_output_sha256', 'semantic_result',
      'completion_terminal_effect_id', 'attested_at',
    ], 'durable receipt attestation');
  }
  if (terminal) {
    exactKeys(terminal, [
      'schema_version', 'terminal_effect_id', 'session_id', 'task_id',
      'worker_generation', 'resident_incarnation_id', 'status',
      'terminal_reason', 'diagnostic_sha256', 'recorded_at',
    ], 'durable receipt terminal');
  }
  if (attestation && (
    attestation.schema_version !== 1
    || attestation.session_id !== identity.session_id
    || attestation.task_id !== identity.task_id
    || attestation.worker_generation !== identity.worker_generation
    || typeof attestation.resident_incarnation_id !== 'string'
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
      .test(attestation.resident_incarnation_id)
  )) {
    throw new Error('cold successor durable attestation identity is invalid');
  }
  if (terminal && (
    terminal.schema_version !== 1
    || terminal.session_id !== identity.session_id
    || terminal.task_id !== identity.task_id
    || terminal.worker_generation !== identity.worker_generation
    || typeof terminal.resident_incarnation_id !== 'string'
    || !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
      .test(terminal.resident_incarnation_id)
  )) {
    throw new Error('cold successor durable terminal identity is invalid');
  }
  if ((receipt.receipt_state === 'pending' && (attestation !== null || terminal !== null))
      || (receipt.receipt_state === 'attested' && (attestation === null || terminal !== null))
      || (receipt.receipt_state === 'terminal' && terminal === null)
      || (receipt.receipt_state !== 'terminal' && receipt.task_status !== 'IN_PROGRESS')
      || (receipt.receipt_state === 'terminal' && terminal?.status !== receipt.task_status)) {
    throw new Error('cold successor durable receipt payload is inconsistent');
  }
  return {
    receiptState: receipt.receipt_state as ColdReceipt['receiptState'],
    completionTerminalEffectId: receipt.completion_terminal_effect_id,
    attestation,
    terminal,
  };
}

function attestationMatches(
  receipt: ColdReceipt,
  effect: JsonObject,
): boolean {
  const attestation = receipt.attestation;
  if (!attestation || receipt.receiptState !== 'attested') return false;
  return Object.entries(effect).every(([key, value]) => exactField(attestation, key, value))
    && exactField(
      attestation,
      'completion_terminal_effect_id',
      receipt.completionTerminalEffectId,
    )
    && typeof attestation.attested_at === 'string'
    && attestation.attested_at.length > 0;
}

function attestationContractFieldsMatch(
  receipt: ColdReceipt,
  validated: ValidatedColdSuccessor,
): boolean {
  const attestation = receipt.attestation;
  if (!attestation) return false;
  return attestation.schema_version === 1
    && attestation.session_id === validated.sessionId
    && typeof attestation.task_id === 'string'
    && attestation.worker_generation === validated.workerGeneration
    && typeof attestation.resident_incarnation_id === 'string'
    && attestation.core_sha256 === validated.hashes.core
    && attestation.problem_model_sha256 === validated.hashes.problemModel
    && attestation.running_update_sha256 === validated.hashes.runningUpdate
    && attestation.strategic_memory_sha256 === validated.hashes.strategicMemory
    && attestation.provider_terminal_reason === 'completed'
    && typeof attestation.provider_output_sha256 === 'string'
    && SHA256_RE.test(attestation.provider_output_sha256)
    && typeof attestation.semantic_result === 'string'
    && attestation.semantic_result.trim().length > 0
    && attestation.semantic_result.length <= 4000
    && attestation.completion_terminal_effect_id === receipt.completionTerminalEffectId
    && typeof attestation.attested_at === 'string'
    && attestation.attested_at.length > 0;
}

function recoveredAttestationMatchesContract(
  receipt: ColdReceipt,
  validated: ValidatedColdSuccessor,
): boolean {
  return receipt.receiptState === 'attested'
    && attestationContractFieldsMatch(receipt, validated);
}

function terminalMatches(receipt: ColdReceipt, effect: JsonObject): boolean {
  const terminal = receipt.terminal;
  if (!terminal || receipt.receiptState !== 'terminal') return false;
  return Object.entries(effect).every(([key, value]) => exactField(terminal, key, value))
    && typeof terminal.recorded_at === 'string'
    && terminal.recorded_at.length > 0;
}

function recoveredTerminalStatus(
  receipt: ColdReceipt,
  validated: ValidatedColdSuccessor,
): 'COMPLETED' | 'FAILED' | null {
  const terminal = receipt.terminal;
  if (!terminal || receipt.receiptState !== 'terminal') return null;
  const common = terminal.schema_version === 1
    && typeof terminal.terminal_effect_id === 'string'
    && typeof terminal.resident_incarnation_id === 'string'
    && typeof terminal.recorded_at === 'string'
    && terminal.recorded_at.length > 0;
  if (!common) return null;
  if (terminal.status === 'COMPLETED') {
    return terminal.terminal_reason === 'completed'
      && terminal.diagnostic_sha256 === null
      && terminal.terminal_effect_id === receipt.completionTerminalEffectId
      && attestationContractFieldsMatch(receipt, validated)
      ? 'COMPLETED' : null;
  }
  return terminal.status === 'FAILED'
    && terminal.terminal_reason !== 'completed'
    && typeof terminal.diagnostic_sha256 === 'string'
    && SHA256_RE.test(terminal.diagnostic_sha256)
    ? 'FAILED' : null;
}

function assertLeaseHeld(lease: JsonObject): void {
  if (lease.authority_held !== true || lease.resident_incarnation_held !== true
      || lease.stop_requested === true) {
    throw new Error('cold successor proof authority was not granted');
  }
}

export async function handleStrategicColdSuccessor(
  taskId: string, payload: JsonObject, task: JsonObject,
  _ghToken: string, _owner: string, _repo: string, ws: WebSocket | null,
  statusRef: { value: string }, model?: string, effort?: string,
): Promise<void> {
  const validated = preflightColdSuccessorClaim(payload, task);
  if (!validated) throw new Error('cold successor handler received an ordinary task');
  const incarnation = randomUUID();
  const failureTerminalEffectId = randomUUID();
  const identity = {
    session_id: validated.sessionId,
    task_id: taskId,
    worker_generation: validated.workerGeneration,
    resident_incarnation_id: incarnation,
  };
  const heartbeatPath = (
    `/api/agent/projects/${validated.projectId}/strategic-session/cold-successor/heartbeat`
  );
  const terminalPath = (
    `/api/agent/projects/${validated.projectId}/strategic-session/cold-successor/terminal`
  );
  const receiptQuery = new URLSearchParams({
    session_id: validated.sessionId,
    task_id: taskId,
    worker_generation: String(validated.workerGeneration),
    resident_incarnation_id: incarnation,
  });
  const receiptPath = (
    `/api/agent/projects/${validated.projectId}/strategic-session/cold-successor/receipt?`
    + receiptQuery.toString()
  );
  let authorityHeld = false;
  let renewalTimer: NodeJS.Timeout | null = null;
  let renewalInFlight: Promise<void> | null = null;
  let renewalError: unknown = null;
  let attestationDeliveryAttempted = false;
  let attestationDurable = false;
  let terminalDeliveryStarted = false;
  let failureReason: ColdFailureReason = 'internal_error';
  const awaitRenewalHealthy = async (): Promise<void> => {
    if (renewalInFlight) await renewalInFlight;
    if (renewalError !== null) {
      throw errorFromUnknown(renewalError, 'cold successor renewal failed');
    }
  };
  const readReceipt = async (): Promise<ColdReceipt> => (
    parseColdReceipt(await apiGet(receiptPath, 1), identity)
  );
  const deliverAttestation = async (effect: JsonObject): Promise<ColdReceipt> => {
    let ambiguousSeen = false;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      let postError: unknown = null;
      try {
        await apiPost(
          `/api/agent/projects/${validated.projectId}/strategic-session/cold-successor/attest`,
          effect,
          1,
        );
      } catch (error) {
        postError = error;
        if (!(/\(4\d\d\)/.test(String(error)) && !/\(429\)/.test(String(error)))) {
          ambiguousSeen = true;
        }
      }
      let receipt: ColdReceipt;
      try {
        receipt = await readReceipt();
      } catch (error) {
        throw new AttestationDeliveryError(
          'ambiguous',
          'cold successor attestation receipt read is ambiguous',
          error,
        );
      }
      if (attestationMatches(receipt, effect)) return receipt;
      if (receipt.receiptState !== 'pending') {
        throw new AttestationDeliveryError(
          'conflict',
          'cold successor attestation receipt conflicts with the exact effect',
          postError,
        );
      }
      const authoritativeRejection = postError !== null
        && /\(4\d\d\)/.test(errorMessage(postError))
        && !/\(429\)/.test(errorMessage(postError));
      if (authoritativeRejection && !ambiguousSeen) {
        throw new AttestationDeliveryError(
          'confirmed_pending',
          'cold successor attestation was rejected before commit',
          postError,
        );
      }
      if (postError === null) ambiguousSeen = true;
      if (attempt === 1) {
        throw new AttestationDeliveryError(
          'ambiguous',
          'cold successor attestation delivery remains ambiguous',
          postError,
        );
      }
    }
    throw new AttestationDeliveryError(
      'ambiguous',
      'cold successor attestation delivery did not settle',
    );
  };
  const deliverTerminal = async (effect: JsonObject): Promise<ColdReceipt> => {
    let lastError: unknown = null;
    for (let attempt = 0; attempt < 2; attempt += 1) {
      try {
        await apiPost(terminalPath, effect, 1);
        lastError = null;
      } catch (error) {
        lastError = error;
      }
      const receipt = await readReceipt();
      if (terminalMatches(receipt, effect)) return receipt;
      const replayable = receipt.receiptState === 'pending'
        || (receipt.receiptState === 'attested' && effect.status === 'COMPLETED');
      if (!replayable || attempt === 1) {
        if (lastError !== null) {
          throw errorFromUnknown(lastError, 'cold successor terminal delivery failed');
        }
        throw new Error('cold successor terminal receipt did not match the exact effect');
      }
    }
    throw lastError === null
      ? new Error('cold successor terminal delivery did not settle')
      : errorFromUnknown(lastError, 'cold successor terminal delivery failed');
  };
  try {
    statusRef.value = 'Claiming cold-successor proof authority...';
    let initialReceipt: ColdReceipt;
    try {
      const lease = await apiPost(heartbeatPath, identity, 1) as JsonObject;
      assertLeaseHeld(lease);
      initialReceipt = await readReceipt();
    } catch (heartbeatError) {
      try {
        initialReceipt = await readReceipt();
      } catch {
        throw heartbeatError;
      }
    }
    authorityHeld = true;
    if (initialReceipt.receiptState === 'terminal') {
      const recoveredStatus = recoveredTerminalStatus(initialReceipt, validated);
      if (!recoveredStatus) {
        throw new Error('cold successor recovered terminal receipt is malformed');
      }
      task._cold_successor_terminal_status = recoveredStatus;
      sendLog(ws, `Cold successor ${recoveredStatus.toLowerCase()} receipt recovered.\n`);
      return;
    }
    renewalTimer = setInterval(() => {
      if (renewalInFlight || renewalError !== null) return;
      renewalInFlight = (async () => {
        try {
          assertLeaseHeld(await apiPost(heartbeatPath, identity, 1) as JsonObject);
        } catch (error) {
          try {
            const receipt = await readReceipt();
            if (receipt.receiptState === 'terminal') throw error;
          } catch {
            renewalError = error;
          }
        } finally {
          renewalInFlight = null;
        }
      })();
    }, 45_000);
    renewalTimer.unref();
    let completionReceipt = initialReceipt;
    if (initialReceipt.receiptState === 'attested') {
      if (!recoveredAttestationMatchesContract(initialReceipt, validated)) {
        throw new Error('cold successor recovered attestation is malformed');
      }
      attestationDurable = true;
      statusRef.value = 'Resuming from exact durable attestation...';
    } else {
      const prompt = `You are a transcript-free strategic successor proof. You have no tools, no repository, no web access, no conversation history, and no mutation authority. Read only the three durable sources in this exact JSON envelope and return exactly {"schema_version":1,"handoff_state":"loaded","semantic_result":"a concise statement of the current strategic checkpoint"}.\n\n${JSON.stringify({
        problem_model_canonical_json: validated.problemModel,
        running_update: { present: validated.runningUpdate !== null, text: validated.runningUpdate },
        strategic_memory_canonical_json: validated.strategicMemory,
      })}`;
      const scratch = fs.mkdtempSync(path.join(os.tmpdir(), 'bp-cold-successor-'));
      fs.chmodSync(scratch, 0o700);
      let output;
      try {
        failureReason = 'provider_terminal_failed';
        statusRef.value = 'Loading exact durable strategic authority...';
        output = await runIsolatedTextOnlyMemory(prompt, ws, statusRef, model, effort, {
          cwd: scratch,
          timeoutMs: 180_000,
          maxOutputBytes: 32 * 1024,
        });
      } finally {
        try { fs.rmSync(scratch, { recursive: true, force: true }); } catch { /* best effort */ }
      }
      await awaitRenewalHealthy();
      if (output.exitCode !== 0 || output.providerResultEventSeen !== true
          || output.providerResultIsError !== false
          || output.providerResultSubtype !== 'success'
          || output.providerTerminalReason !== 'completed'
          || output.providerApiErrorStatus !== undefined) {
        throw new Error(
          `cold successor provider terminal predicate failed (exit=${output.exitCode}, `
          + `result=${String(output.providerResultEventSeen)}, `
          + `is_error=${String(output.providerResultIsError)}, `
          + `subtype=${String(output.providerResultSubtype)}, `
          + `reason=${String(output.providerTerminalReason)}, `
          + `api_status=${String(output.providerApiErrorStatus)})`,
        );
      }
      failureReason = 'authority_renewal_failed';
      const providerOutput = output.lastResultText;
      failureReason = 'provider_result_invalid';
      const semanticResult = parseSemanticResult(providerOutput);
      failureReason = 'attestation_failed';
      const attestationEffect = {
        ...identity,
        core_sha256: validated.hashes.core,
        problem_model_sha256: validated.hashes.problemModel,
        running_update_sha256: validated.hashes.runningUpdate,
        strategic_memory_sha256: validated.hashes.strategicMemory,
        provider_terminal_reason: 'completed',
        provider_output_sha256: sha256(providerOutput),
        semantic_result: semanticResult,
      };
      attestationDeliveryAttempted = true;
      completionReceipt = await deliverAttestation(attestationEffect);
      attestationDurable = true;
    }
    failureReason = 'authority_renewal_failed';
    await awaitRenewalHealthy();
    terminalDeliveryStarted = true;
    await deliverTerminal({
      ...identity,
      terminal_effect_id: completionReceipt.completionTerminalEffectId,
      status: 'COMPLETED',
      terminal_reason: 'completed',
      diagnostic_sha256: null,
    });
    clearInterval(renewalTimer);
    renewalTimer = null;
    task._cold_successor_terminal_status = 'COMPLETED';
    sendLog(ws, 'Cold successor durable-authority attestation completed.\n');
  } catch (error) {
    await Promise.resolve(renewalInFlight);
    if (renewalError !== null) failureReason = 'authority_renewal_failed';
    if (error instanceof RuntimeUnavailableError) throw error;
    const confirmedPendingAttestation = error instanceof AttestationDeliveryError
      && error.disposition === 'confirmed_pending';
    if (
      !authorityHeld
      || attestationDurable
      || terminalDeliveryStarted
      || (attestationDeliveryAttempted && !confirmedPendingAttestation)
    ) throw error;
    const message = error instanceof Error ? error.message : String(error);
    statusRef.value = `[ERROR] ${sanitize(message).slice(0, 200)}`;
    terminalDeliveryStarted = true;
    await deliverTerminal({
      ...identity,
      terminal_effect_id: failureTerminalEffectId,
      status: 'FAILED',
      terminal_reason: failureReason,
      diagnostic_sha256: sha256(sanitize(message)),
    });
    task._cold_successor_terminal_status = 'FAILED';
  } finally {
    if (renewalTimer) clearInterval(renewalTimer);
    await Promise.resolve(renewalInFlight);
  }
}
