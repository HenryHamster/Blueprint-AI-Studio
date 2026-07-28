import { createHash, randomUUID } from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';
import type { ResidentSession } from '../providers/resident-session.js';
import {
  createForegroundResidentProfile,
  type ForegroundResidentAdapter,
  type ForegroundResidentProfile,
} from '../providers/foreground-resident.js';
import { provisionCallResidentTool, type ProvisionedCallResidentTool } from '../providers/call-resident-tool.js';

export const CALL_PACKET_VERSION = 1 as const;
export const MAX_CALL_PACKET_BYTES = 64 * 1024;
export const MAX_CALL_DISPLAY_BLOCKS = 8;
export const MAX_CALL_SPOKEN_TEXT_CHARS = 24_000;

export type CallLeaseStatus = 'starting' | 'pausing' | 'active' | 'ending' | 'ended' | 'failed';

export interface CallLease {
  callId: string;
  generation: number;
  status: CallLeaseStatus;
  expiresAt: string | null;
  inputRevision: number;
  adapter: string | null;
}

/** Short-lived, coordinator-only transport projection.  No API secret belongs here. */
export interface CallTransportProjection {
  serverUrl: string;
  participantToken: string;
  room: string;
  participantIdentity: string;
  /** Exact browser/operator identity; never infer this from room participants. */
  operatorIdentity: string;
}

export interface CallDisplayBlock {
  type: 'latex' | 'code' | 'table' | 'artifact_ref';
  content: string;
}

export interface CallWorkPacket {
  version: typeof CALL_PACKET_VERSION;
  packet_id: string;
  project_id: string;
  call_session_id: string;
  call_generation: number;
  request_id: string;
  originating_user_turn_ids: string[];
  input_revision: number;
  context_sequence: number;
  kind: 'progress' | 'final' | 'blocked';
  spoken_text: string;
  exact_text?: string;
  display_blocks?: CallDisplayBlock[];
  evidence_refs?: string[];
  supersedes_packet_id?: string;
}

export interface CallTurnReceipt {
  call_id: string;
  call_generation: number;
  input_revision: number;
  public_input_message_id: string;
  idempotency_key: string;
}

export interface CallCompletion {
  spoken_text: string;
  exact_text?: string;
  display_blocks?: CallDisplayBlock[];
  durable: boolean;
}

/** Intent staged by the only model-visible fast MCP server. */
export interface StagedCallCompletion extends CallCompletion {
  receipt: CallTurnReceipt;
}

export interface StagedWorkdayRequest {
  request_id: string;
  prompt: string;
  receipt: CallTurnReceipt;
}

export interface StagedDisplayBlocks {
  receipt: CallTurnReceipt;
  display_blocks: CallDisplayBlock[];
}

export interface ForegroundCallTurn {
  receipt: CallTurnReceipt;
  result: Awaited<ReturnType<ResidentSession['sendTurn']>>;
  completion: StagedCallCompletion | null;
  workdayRequests: StagedWorkdayRequest[];
  displayBlocks: StagedDisplayBlocks[];
}

export interface ForegroundCallControllerOptions {
  adapter: ForegroundResidentAdapter;
  /** Kept outside the repository and removed once the call ends. */
  runtimeRoot?: string;
  model?: string;
  effort?: string;
  environment?: NodeJS.ProcessEnv;
  /** Test-only override. Real calls require a measured qualified adapter. */
  allowUnvalidatedAdapter?: boolean;
}

type JsonRecord = Record<string, unknown>;

function nonEmptyString(value: unknown, name: string, max = 24_000): string {
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    throw new Error(`invalid ${name}`);
  }
  return value;
}

function integer(value: unknown, name: string, min = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < min) throw new Error(`invalid ${name}`);
  return value as number;
}

function record(value: unknown, name: string): JsonRecord {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error(`invalid ${name}`);
  return value as JsonRecord;
}

/**
 * The backend deliberately projects only non-sensitive lease fields to the
 * resident.  Treat a malformed projection as inactive: an agent must never
 * guess that it owns public call delivery from task bootstrap data.
 */
export function parseCallLeaseProjection(value: unknown, nowMs = Date.now()): CallLease | null {
  if (value === null || value === undefined) return null;
  try {
    const raw = record(value, 'call lease');
    const callId = nonEmptyString(raw.call_id, 'call_id', 256);
    const generation = integer(raw.generation, 'generation', 1);
    const statusRaw = nonEmptyString(raw.status, 'status', 32);
    if (!['starting', 'pausing', 'active', 'ending', 'ended', 'failed'].includes(statusRaw)) {
      throw new Error('invalid status');
    }
    const inputRevision = integer(raw.input_revision, 'input_revision', 0);
    const expiresAt = raw.expires_at === null || raw.expires_at === undefined
      ? null : nonEmptyString(raw.expires_at, 'expires_at', 128);
    // An expired projection is never an active call, even if a delayed heartbeat
    // still says active.  This closes the stale-worker public-owner window.
    if (expiresAt !== null && Number.isFinite(Date.parse(expiresAt)) && Date.parse(expiresAt) <= nowMs) return null;
    return {
      callId,
      generation,
      status: statusRaw as CallLeaseStatus,
      expiresAt,
      inputRevision,
      adapter: typeof raw.adapter === 'string' && raw.adapter.trim() ? raw.adapter : null,
    };
  } catch {
    return null;
  }
}

export function callLeaseIsPubliclyActive(lease: CallLease | null): boolean {
  if (lease === null || !['starting', 'pausing', 'active'].includes(lease.status)) return false;
  // A projection can outlive the last successful heartbeat (for example during
  // a backend partition).  Re-check wall-clock expiry at every authority use,
  // not merely while parsing the projection, so a stale coordinator cannot
  // keep speaking/receiving media after the server-owned call lease lapses.
  if (lease.expiresAt === null) return false;
  const expiresAt = Date.parse(lease.expiresAt);
  return Number.isFinite(expiresAt) && expiresAt > Date.now();
}

export function callLeaseKey(lease: Pick<CallLease, 'callId' | 'generation'> | null): string | null {
  return lease === null ? null : `${lease.callId}:${lease.generation}`;
}

/**
 * Fence every callback emitted by a speech-provider pipeline to the exact call
 * generation that created it. Provider cancellation is asynchronous, so this
 * check remains required even after the old pipeline has been closed.
 */
export function speechOriginIsCurrent(
  originKey: string,
  currentLease: CallLease | null,
  activeCallKey: string | null,
): boolean {
  return callLeaseIsPubliclyActive(currentLease)
    && callLeaseKey(currentLease) === originKey
    && activeCallKey === originKey;
}

export function parseCallTransportProjection(value: unknown): CallTransportProjection | null {
  if (value === null || value === undefined) return null;
  try {
    const raw = record(value, 'call transport');
    const serverUrl = nonEmptyString(raw.server_url, 'server_url', 2_048);
    // Keep the protocol narrow now even though Phase 1 does not join a room.
    if (!/^wss:\/\//i.test(serverUrl) && !/^https:\/\//i.test(serverUrl)) throw new Error('invalid server_url');
    return {
      serverUrl,
      participantToken: nonEmptyString(raw.participant_token, 'participant_token', 8_192),
      room: nonEmptyString(raw.room, 'room', 512),
      participantIdentity: nonEmptyString(raw.participant_identity, 'participant_identity', 512),
      operatorIdentity: nonEmptyString(raw.operator_identity, 'operator_identity', 512),
    };
  } catch { return null; }
}

export function validateCallWorkPacket(value: unknown): CallWorkPacket {
  const raw = record(value, 'call packet');
  if (raw.version !== CALL_PACKET_VERSION) throw new Error('unsupported call packet version');
  const kind = nonEmptyString(raw.kind, 'kind', 16);
  if (kind !== 'progress' && kind !== 'final' && kind !== 'blocked') throw new Error('invalid packet kind');
  const originating = Array.isArray(raw.originating_user_turn_ids) ? raw.originating_user_turn_ids : null;
  if (!originating || originating.length === 0 || originating.length > 32) throw new Error('invalid originating_user_turn_ids');
  const originatingUserTurnIds = originating.map((id) => nonEmptyString(id, 'originating_user_turn_id', 256));
  const displayBlocks = raw.display_blocks === undefined ? undefined : validateDisplayBlocks(raw.display_blocks);
  const evidenceRefs = raw.evidence_refs === undefined ? undefined : validateEvidenceRefs(raw.evidence_refs);
  const spokenText = nonEmptyString(raw.spoken_text, 'spoken_text', MAX_CALL_SPOKEN_TEXT_CHARS);
  const exactText = raw.exact_text === undefined ? undefined : nonEmptyString(raw.exact_text, 'exact_text', MAX_CALL_SPOKEN_TEXT_CHARS);
  return {
    version: CALL_PACKET_VERSION,
    packet_id: nonEmptyString(raw.packet_id, 'packet_id', 256),
    project_id: nonEmptyString(raw.project_id, 'project_id', 256),
    call_session_id: nonEmptyString(raw.call_session_id, 'call_session_id', 256),
    call_generation: integer(raw.call_generation, 'call_generation', 1),
    request_id: nonEmptyString(raw.request_id, 'request_id', 256),
    originating_user_turn_ids: originatingUserTurnIds,
    input_revision: integer(raw.input_revision, 'input_revision', 0),
    context_sequence: integer(raw.context_sequence, 'context_sequence', 0),
    kind,
    spoken_text: spokenText,
    exact_text: exactText,
    display_blocks: displayBlocks,
    evidence_refs: evidenceRefs,
    supersedes_packet_id: raw.supersedes_packet_id === undefined
      ? undefined : nonEmptyString(raw.supersedes_packet_id, 'supersedes_packet_id', 256),
  };
}

function validateDisplayBlocks(value: unknown): CallDisplayBlock[] {
  if (!Array.isArray(value) || value.length > MAX_CALL_DISPLAY_BLOCKS) throw new Error('invalid display_blocks');
  return value.map((block) => {
    const raw = record(block, 'display block');
    const type = nonEmptyString(raw.type, 'display block type', 32);
    if (type !== 'latex' && type !== 'code' && type !== 'table' && type !== 'artifact_ref') {
      throw new Error('invalid display block type');
    }
    return { type, content: nonEmptyString(raw.content, 'display block content', 16_000) };
  });
}

function validateEvidenceRefs(value: unknown): string[] {
  if (!Array.isArray(value) || value.length > 32) throw new Error('invalid evidence_refs');
  return value.map((ref) => {
    const text = nonEmptyString(ref, 'evidence_ref', 512);
    if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)) throw new Error('evidence_ref URLs are not allowed');
    return text;
  });
}

/** Call-owned state only.  It is deliberately independent from model output. */
export class CallCoordinatorState {
  private lease: CallLease | null = null;
  private transport: CallTransportProjection | null = null;
  private readonly seenPacketIds = new Set<string>();
  private readonly supersededRequests = new Set<string>();
  private playoutRevision = 0;
  private heardPrefix = '';

  updateLease(lease: CallLease | null): void {
    const changed = this.lease?.callId !== lease?.callId
      || this.lease?.generation !== lease?.generation
      || this.lease?.inputRevision !== lease?.inputRevision;
    this.lease = lease;
    if (changed) {
      this.seenPacketIds.clear();
      this.supersededRequests.clear();
      this.playoutRevision = 0;
      this.heardPrefix = '';
    }
  }

  updateControlProjection(lease: CallLease | null, transport: CallTransportProjection | null): void {
    this.updateLease(lease);
    // Transport is intentionally retained only while the matching live lease
    // exists.  It is opaque to the text-only Coordinator and must never be
    // logged or forwarded to a model process.
    this.transport = callLeaseIsPubliclyActive(lease) ? transport : null;
  }

  currentLease(): CallLease | null {
    // Never hand a stale in-memory lease back to a caller.  A heartbeat is a
    // refresh mechanism, not the sole expiry mechanism: a network partition
    // must not preserve call authority until the next successful response.
    return callLeaseIsPubliclyActive(this.lease) ? this.lease : null;
  }
  currentTransport(): CallTransportProjection | null {
    return this.currentLease() === null ? null : this.transport;
  }
  suppressOrdinaryWorkday(): boolean { return this.currentLease() !== null; }
  suppressAutonomy(): boolean { return this.suppressOrdinaryWorkday(); }

  beginPublicTurn(publicInputMessageId: string): CallTurnReceipt {
    const lease = this.currentLease();
    if (!lease) throw new Error('no active call lease');
    return {
      call_id: lease.callId,
      call_generation: lease.generation,
      input_revision: lease.inputRevision,
      public_input_message_id: nonEmptyString(publicInputMessageId, 'public_input_message_id', 256),
      idempotency_key: `call-complete:${lease.callId}:${lease.generation}:${lease.inputRevision}:${randomUUID()}`,
    };
  }

  acceptWorkPacket(value: unknown, projectId: string): CallWorkPacket | null {
    const packet = validateCallWorkPacket(value);
    const lease = this.currentLease();
    if (!lease) return null;
    if (packet.project_id !== projectId || packet.call_session_id !== lease.callId || packet.call_generation !== lease.generation) return null;
    if (packet.input_revision !== lease.inputRevision || this.seenPacketIds.has(packet.packet_id)) return null;
    if (this.supersededRequests.has(packet.request_id)) return null;
    if (packet.supersedes_packet_id) this.seenPacketIds.add(packet.supersedes_packet_id);
    this.seenPacketIds.add(packet.packet_id);
    return packet;
  }

  supersedeRequest(requestId: string): void { this.supersededRequests.add(nonEmptyString(requestId, 'request_id', 256)); }

  interruptPlayback(playedPrefix: string): number {
    this.playoutRevision += 1;
    this.heardPrefix = playedPrefix.slice(0, MAX_CALL_SPOKEN_TEXT_CHARS);
    return this.playoutRevision;
  }

  playbackState(): { revision: number; heardPrefix: string } {
    return { revision: this.playoutRevision, heardPrefix: this.heardPrefix };
  }

  completionIsCurrent(receipt: CallTurnReceipt): boolean {
    const lease = this.currentLease();
    return lease !== null
      && lease.callId === receipt.call_id
      && lease.generation === receipt.call_generation
      && lease.inputRevision === receipt.input_revision;
  }
}

function removePathQuietly(target: string | undefined): void {
  if (!target) return;
  try { fs.rmSync(target, { recursive: true, force: true, maxRetries: 1 }); } catch { /* best effort cleanup */ }
}

function validateReceipt(value: unknown): CallTurnReceipt {
  const raw = record(value, 'call turn receipt');
  return {
    call_id: nonEmptyString(raw.call_id, 'call_id', 256),
    call_generation: integer(raw.call_generation, 'call_generation', 1),
    input_revision: integer(raw.input_revision, 'input_revision', 0),
    public_input_message_id: typeof raw.public_input_message_id === 'string'
      ? nonEmptyString(raw.public_input_message_id, 'public_input_message_id', 256) : '',
    idempotency_key: nonEmptyString(raw.idempotency_key, 'idempotency_key', 512),
  };
}

function sameReceipt(left: CallTurnReceipt, right: CallTurnReceipt): boolean {
  return left.call_id === right.call_id
    && left.call_generation === right.call_generation
    && left.input_revision === right.input_revision
    && left.idempotency_key === right.idempotency_key
    && left.public_input_message_id === right.public_input_message_id;
}

function completionFromStaged(value: unknown): StagedCallCompletion {
  const raw = record(value, 'call completion');
  return {
    receipt: validateReceipt(raw.receipt),
    spoken_text: nonEmptyString(raw.spoken_text, 'spoken_text', MAX_CALL_SPOKEN_TEXT_CHARS),
    ...(raw.exact_text === undefined ? {} : { exact_text: nonEmptyString(raw.exact_text, 'exact_text', MAX_CALL_SPOKEN_TEXT_CHARS) }),
    ...(raw.display_blocks === undefined ? {} : { display_blocks: validateDisplayBlocks(raw.display_blocks) }),
    durable: raw.durable !== false,
  };
}

function requestFromStaged(value: unknown): StagedWorkdayRequest {
  const raw = record(value, 'call Workday request');
  return {
    request_id: nonEmptyString(raw.request_id, 'request_id', 256),
    prompt: nonEmptyString(raw.prompt, 'prompt', MAX_CALL_SPOKEN_TEXT_CHARS),
    receipt: validateReceipt(raw.receipt),
  };
}

function displaysFromStaged(value: unknown): StagedDisplayBlocks {
  const raw = record(value, 'call display blocks');
  return { receipt: validateReceipt(raw.receipt), display_blocks: validateDisplayBlocks(raw.display_blocks) };
}

/**
 * Owns exactly one short-lived, restricted Fast Resident child for a call.
 *
 * Model output is never delivery.  The only accepted result is a local MCP
 * intent that matches the immutable receipt the Coordinator installed before
 * dispatch.  This keeps a crashed/replaced child from completing an old turn.
 */
export class ForegroundCallController {
  private readonly runtimeDir: string;
  private readonly stageDir: string;
  private readonly receiptPath: string;
  private profile: ForegroundResidentProfile | null = null;
  private tool: ProvisionedCallResidentTool | null = null;
  private session: ResidentSession | null = null;

  constructor(private readonly options: ForegroundCallControllerOptions) {
    this.runtimeDir = fs.mkdtempSync(path.join(options.runtimeRoot ?? os.tmpdir(), 'blueprint-call-coordinator-'));
    fs.chmodSync(this.runtimeDir, 0o700);
    this.stageDir = path.join(this.runtimeDir, 'fast-stage');
    this.receiptPath = path.join(this.runtimeDir, 'turn-receipt.json');
  }

  isAlive(): boolean { return this.session?.isAlive() === true; }

  /** STT StartOfTurn / browser barge-in pre-empts only the public Fast child. */
  interrupt(): void { this.session?.interrupt(); }

  /** Start lazily so an idle Workday does not consume a second provider slot. */
  ensureStarted(): void {
    if (this.session?.isAlive()) return;
    if (this.options.adapter.validation !== 'qualified' && !this.options.allowUnvalidatedAdapter) {
      throw new Error(`Fast adapter ${this.options.adapter.id} is not qualification-approved for calls`);
    }
    this.closeSessionOnly();
    this.profile = createForegroundResidentProfile(this.options.environment ?? process.env);
    this.tool = provisionCallResidentTool(this.profile.cwd, 'fast', {
      receiptPath: this.receiptPath,
      stageDir: this.stageDir,
    });
    this.session = this.options.adapter.start({
      profile: this.profile,
      mcpConfigPath: this.tool.configPath,
      model: this.options.model,
      effort: this.options.effort,
    });
  }

  /** Replaces only Fast; Workday state and staged final packets remain intact. */
  replaceAfterCrash(): void {
    this.closeSessionOnly();
    this.ensureStarted();
  }

  private writeReceipt(receipt: CallTurnReceipt): void {
    removePathQuietly(this.receiptPath);
    fs.writeFileSync(this.receiptPath, JSON.stringify(receipt), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
    fs.chmodSync(this.receiptPath, 0o600);
  }

  private drainStage<T>(prefix: string, parse: (value: unknown) => T, receipt: CallTurnReceipt): T[] {
    if (!fs.existsSync(this.stageDir)) return [];
    const output: T[] = [];
    for (const entry of fs.readdirSync(this.stageDir).sort()) {
      if (!entry.startsWith(prefix) || !entry.endsWith('.json')) continue;
      const staged = path.join(this.stageDir, entry);
      try {
        const stat = fs.lstatSync(staged);
        if (!stat.isFile() || stat.size > MAX_CALL_PACKET_BYTES) throw new Error('invalid staged local intent');
        const parsed = parse(JSON.parse(fs.readFileSync(staged, 'utf8')));
        const candidate = parsed as { receipt?: CallTurnReceipt };
        if (candidate.receipt && sameReceipt(candidate.receipt, receipt)) output.push(parsed);
      } catch {
        // A malformed model-owned staged file is ignored, not retried as a
        // public reply.  The caller can ask the current Fast child again.
      } finally {
        try { fs.unlinkSync(staged); } catch { /* removal failure is harmless: duplicate fence remains server-side */ }
      }
    }
    return output;
  }

  async dispatch(receipt: CallTurnReceipt, prompt: string): Promise<ForegroundCallTurn> {
    this.ensureStarted();
    this.writeReceipt(receipt);
    const current = this.session;
    if (!current) throw new Error('Fast Resident failed to start');
    const result = await current.sendTurn(prompt);
    const completions = this.drainStage('complete-', completionFromStaged, receipt);
    const requests = this.drainStage('request-', requestFromStaged, receipt);
    const displays = this.drainStage('display-', displaysFromStaged, receipt);
    // First staged completion wins. The local MCP server also enforces this,
    // but retaining a Coordinator-side rule makes duplicate prompt injection
    // unable to create two public owners.
    return { receipt, result, completion: completions[0] ?? null, workdayRequests: requests, displayBlocks: displays };
  }

  close(): void {
    this.closeSessionOnly();
    removePathQuietly(this.runtimeDir);
  }

  private closeSessionOnly(): void {
    try { this.session?.close(); } catch { /* worker replacement continues */ }
    this.session = null;
    if (this.profile) {
      removePathQuietly(this.profile.cwd);
      removePathQuietly(this.profile.env.HOME);
    }
    this.profile = null;
    this.tool = null;
  }
}

/**
 * Interruptible coordinator wait. RTC receives an authenticated *wake hint*,
 * while speech receives a final STT callback; both call notify() so voice does
 * not inherit the ordinary 750ms–1.5s Workday polling delay. Content still
 * comes exclusively from the canonical backend fetch.
 */
export class CallCoordinatorWakeGate {
  private pending = false;
  private resolver: (() => void) | null = null;
  private closed = false;

  notify(): void {
    if (this.closed) return;
    this.pending = true;
    const resolve = this.resolver;
    this.resolver = null;
    resolve?.();
  }

  async wait(timeoutMs: number): Promise<'wake' | 'timeout'> {
    if (this.closed) return 'timeout';
    if (this.pending) {
      this.pending = false;
      return 'wake';
    }
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.resolver = null;
        resolve('timeout');
      }, Math.max(0, timeoutMs));
      this.resolver = () => {
        clearTimeout(timer);
        this.pending = false;
        resolve('wake');
      };
    });
  }

  close(): void {
    this.closed = true;
    const resolve = this.resolver;
    this.resolver = null;
    resolve?.();
  }
}

/** Sentence boundary only; provider-specific theory pronunciation stays in TTS. */
export class FastSpeechTextBuffer {
  private pending = '';
  push(delta: string): string[] {
    this.pending = (this.pending + delta).slice(-MAX_CALL_SPOKEN_TEXT_CHARS);
    const phrases: string[] = [];
    for (;;) {
      const boundary = this.pending.search(/[.!?](?:\s|$)/);
      const hardBoundary = boundary < 0 && this.pending.length >= 280
        ? this.pending.lastIndexOf(' ', 240) : -1;
      const end = boundary >= 0 ? boundary + 1 : hardBoundary;
      if (end <= 0) break;
      const phrase = this.pending.slice(0, end).trim();
      this.pending = this.pending.slice(end).trimStart();
      if (phrase) phrases.push(phrase);
    }
    return phrases;
  }
  flush(): string | null {
    const output = this.pending.trim();
    this.pending = '';
    return output || null;
  }
  clear(): void { this.pending = ''; }
}

/**
 * Reconcile again immediately only when the lease changed while an async media
 * startup was in flight. Retrying the same failed lease here would create an
 * unbounded provider/credential retry loop; same-generation retries are paced
 * by the Resident heartbeat.
 */
export function shouldImmediatelyReconcileCallMedia(
  attemptedKey: string | null,
  latestKey: string | null,
  connectedKey: string | null,
): boolean {
  return latestKey !== attemptedKey && latestKey !== connectedKey;
}

/**
 * Filesystem staging is local IPC, never a public transport.  A final/blocked
 * packet survives a Fast process replacement until the Coordinator commits or
 * deliberately disposes it.  Progress remains ephemeral and is not staged.
 */
export class CallPacketStaging {
  constructor(private readonly directory: string) {}

  stage(packet: CallWorkPacket): string | null {
    if (packet.kind === 'progress') return null;
    fs.mkdirSync(this.directory, { recursive: true, mode: 0o700 });
    fs.chmodSync(this.directory, 0o700);
    const safeId = createHash('sha256').update(packet.packet_id).digest('hex');
    const target = path.join(this.directory, `${safeId}.json`);
    const encoded = JSON.stringify(packet);
    if (Buffer.byteLength(encoded, 'utf8') > MAX_CALL_PACKET_BYTES) throw new Error('call packet exceeds staging limit');
    const tmp = path.join(this.directory, `.${safeId}.${process.pid}.tmp`);
    try {
      fs.writeFileSync(tmp, encoded, { encoding: 'utf8', mode: 0o600, flag: 'wx' });
      fs.renameSync(tmp, target);
    } finally {
      try { fs.unlinkSync(tmp); } catch { /* renamed or never created */ }
    }
    return target;
  }

  read(stagedPath: string): CallWorkPacket {
    if (path.dirname(stagedPath) !== this.directory) throw new Error('staged packet is outside call staging directory');
    const stat = fs.lstatSync(stagedPath);
    if (!stat.isFile() || stat.size > MAX_CALL_PACKET_BYTES) throw new Error('invalid staged call packet');
    return validateCallWorkPacket(JSON.parse(fs.readFileSync(stagedPath, 'utf8')));
  }
}
