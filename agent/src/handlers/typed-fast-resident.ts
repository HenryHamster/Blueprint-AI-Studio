import { randomUUID } from 'crypto';
import fs from 'fs';
import os from 'os';
import path from 'path';
import type { ResidentSession } from '../providers/resident-session.js';
import {
  createForegroundResidentProfile,
  FAST_TYPED_TOOL_NAMES,
  type ForegroundResidentAdapter,
  type ForegroundResidentProfile,
} from '../providers/foreground-resident.js';
import {
  provisionCallResidentTool,
  type ProvisionedCallResidentTool,
} from '../providers/call-resident-tool.js';

const MAX_TYPED_TURN_BYTES = 64 * 1024;
const MAX_TYPED_REPLY_CHARS = 20_000;
/** One question per public Fast turn keeps the private lane bounded and legible. */
export const MAX_TYPED_WORKDAY_REQUESTS_PER_TURN = 1;
const MAX_TYPED_WORKDAY_DELIVERY_KEY_CHARS = 128;
const MAX_TYPED_WORKDAY_QUESTION_CHARS = 20_000;

export interface TypedConversationTurnReceipt {
  claim_id: string;
  message_ids: string[];
  /** Server accepts this key exactly once for the claim. */
  idempotency_key: string;
}

export interface TypedFastCompletion {
  receipt: TypedConversationTurnReceipt;
  content: string;
}

export interface TypedWorkdayRequest {
  receipt: TypedConversationTurnReceipt;
  delivery_key: string;
  prompt: string;
}

export interface TypedFastTurn {
  receipt: TypedConversationTurnReceipt;
  result: Awaited<ReturnType<ResidentSession['sendTurn']>>;
  completion: TypedFastCompletion | null;
  workdayRequests: TypedWorkdayRequest[];
}

export interface ForegroundTypedControllerOptions {
  adapter: ForegroundResidentAdapter;
  runtimeRoot?: string;
  model?: string;
  effort?: string;
  environment?: NodeJS.ProcessEnv;
  /** Tests and typed rollout use the normal foreground qualification gate. */
  allowUnvalidatedAdapter?: boolean;
}

// A private READY probe exists only to prove that the restricted child can
// complete a provider turn.  It must never consume the normal 90-second Fast
// turn budget: provisioning runs beside the Resident loop, and a wedged CLI
// should be replaced quickly while operator input remains unclaimed.
const TYPED_FAST_READINESS_TIMEOUT_MS = 15_000;

function nonEmpty(value: unknown, name: string, max = MAX_TYPED_REPLY_CHARS): string {
  if (typeof value !== 'string' || !value.trim() || value.length > max) {
    throw new Error(`invalid typed Fast ${name}`);
  }
  return value.trim();
}

function receiptFrom(value: unknown): TypedConversationTurnReceipt {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new Error('invalid typed Fast receipt');
  }
  const raw = value as Record<string, unknown>;
  const messageIds = Array.isArray(raw.message_ids)
    ? raw.message_ids.map((id) => nonEmpty(id, 'message id', 256)) : [];
  if (messageIds.length === 0 || messageIds.length > 20) {
    throw new Error('invalid typed Fast receipt message_ids');
  }
  return {
    claim_id: nonEmpty(raw.claim_id, 'claim id', 256),
    message_ids: messageIds,
    idempotency_key: nonEmpty(raw.idempotency_key, 'idempotency key', 255),
  };
}

function sameReceipt(left: TypedConversationTurnReceipt, right: TypedConversationTurnReceipt): boolean {
  return left.claim_id === right.claim_id
    && left.idempotency_key === right.idempotency_key
    && left.message_ids.length === right.message_ids.length
    && left.message_ids.every((id, index) => id === right.message_ids[index]);
}

function removeQuietly(target: string | undefined): void {
  if (!target) return;
  try { fs.rmSync(target, { recursive: true, force: true, maxRetries: 1 }); } catch { /* cleanup only */ }
}

function parseCompletion(value: unknown): TypedFastCompletion {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid typed completion');
  const raw = value as Record<string, unknown>;
  return { receipt: receiptFrom(raw.receipt), content: nonEmpty(raw.content, 'completion') };
}

function parseWorkdayRequest(value: unknown): TypedWorkdayRequest {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid typed Workday request');
  const raw = value as Record<string, unknown>;
  return {
    receipt: receiptFrom(raw.receipt),
    // Match the coordinator's atomic completion contract exactly. Rejecting
    // oversize staged data locally avoids claiming a public turn we can never
    // complete with its private request attached.
    delivery_key: nonEmpty(raw.delivery_key, 'delivery key', MAX_TYPED_WORKDAY_DELIVERY_KEY_CHARS),
    prompt: nonEmpty(raw.prompt, 'Workday prompt', MAX_TYPED_WORKDAY_QUESTION_CHARS),
  };
}

function validateStagedTypedIntents(
  completions: TypedFastCompletion[],
  requests: TypedWorkdayRequest[],
): { completion: TypedFastCompletion | null; workdayRequests: TypedWorkdayRequest[] } {
  if (completions.length > 1) {
    throw new Error('Fast Resident staged multiple public completions for one receipt');
  }
  const uniqueRequests = new Map<string, TypedWorkdayRequest>();
  for (const request of requests) {
    const existing = uniqueRequests.get(request.delivery_key);
    if (existing && existing.prompt !== request.prompt) {
      throw new Error('Fast Resident reused a Workday delivery key with different content');
    }
    uniqueRequests.set(request.delivery_key, request);
  }
  if (uniqueRequests.size > MAX_TYPED_WORKDAY_REQUESTS_PER_TURN) {
    throw new Error('Fast Resident staged too many Workday requests for one turn');
  }
  return { completion: completions[0] ?? null, workdayRequests: [...uniqueRequests.values()] };
}

/**
 * One isolated, tool-restricted model child for normal typed conversation.
 * It has no Blueprint token or checkout. Plain model output is not delivery:
 * only a staged, receipt-bound local MCP intent can reach the Coordinator.
 */
export class ForegroundTypedController {
  private readonly runtimeDir: string;
  private stageDir: string;
  private receiptPath: string;
  private sessionGeneration = 0;
  private profile: ForegroundResidentProfile | null = null;
  private tool: ProvisionedCallResidentTool | null = null;
  private session: ResidentSession | null = null;

  constructor(private readonly options: ForegroundTypedControllerOptions) {
    this.runtimeDir = fs.mkdtempSync(path.join(options.runtimeRoot ?? os.tmpdir(), 'blueprint-typed-fast-'));
    fs.chmodSync(this.runtimeDir, 0o700);
    this.stageDir = path.join(this.runtimeDir, 'stage-0');
    this.receiptPath = path.join(this.runtimeDir, 'receipt-0.json');
  }

  isAlive(): boolean { return this.session?.isAlive() === true; }
  interrupt(): void { this.session?.interrupt(); }

  ensureStarted(): void {
    if (this.session?.isAlive()) return;
    if (this.options.adapter.validation !== 'qualified' && !this.options.allowUnvalidatedAdapter) {
      throw new Error(`Fast adapter ${this.options.adapter.id} is not qualification-approved`);
    }
    this.closeSessionOnly();
    this.sessionGeneration += 1;
    this.stageDir = path.join(this.runtimeDir, `stage-${this.sessionGeneration}`);
    this.receiptPath = path.join(this.runtimeDir, `receipt-${this.sessionGeneration}.json`);
    this.profile = createForegroundResidentProfile(
      this.options.environment ?? process.env,
      FAST_TYPED_TOOL_NAMES,
    );
    this.tool = provisionCallResidentTool(this.profile.cwd, 'typed', {
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

  /**
   * Prove that the already-spawned child can complete a real provider turn
   * before it is allowed to claim an operator message.  The probe has no
   * receipt, so even an errant tool call cannot stage a public completion.
   */
  async ensureReady(): Promise<void> {
    this.ensureStarted();
    const session = this.session;
    if (!session) throw new Error('Fast Resident failed to start');
    const result = await session.sendTurn(
      'Private startup health probe. Do not call any tools. Reply with READY only.',
      { hardTimeoutMs: TYPED_FAST_READINESS_TIMEOUT_MS },
    );
    if (
      result.processGone || result.timedOut || result.hardTimedOut
      || result.interrupted || result.providerFailed
    ) {
      this.closeSessionOnly();
      throw new Error('Fast Resident did not pass its private readiness probe');
    }
  }

  replaceAfterCrash(): void { this.closeSessionOnly(); this.ensureStarted(); }

  private writeReceipt(receipt: TypedConversationTurnReceipt): void {
    removeQuietly(this.receiptPath);
    fs.writeFileSync(this.receiptPath, JSON.stringify(receipt), {
      encoding: 'utf8', mode: 0o600, flag: 'wx',
    });
    fs.chmodSync(this.receiptPath, 0o600);
  }

  private drain<T>(prefix: string, parser: (value: unknown) => T, receipt: TypedConversationTurnReceipt): T[] {
    if (!fs.existsSync(this.stageDir)) return [];
    const accepted: T[] = [];
    for (const entry of fs.readdirSync(this.stageDir).sort()) {
      if (!entry.startsWith(prefix) || !entry.endsWith('.json')) continue;
      const staged = path.join(this.stageDir, entry);
      try {
        const stat = fs.lstatSync(staged);
        if (!stat.isFile() || stat.size > MAX_TYPED_TURN_BYTES) throw new Error('invalid staged typed intent');
        const parsed = parser(JSON.parse(fs.readFileSync(staged, 'utf8')));
        const candidate = parsed as { receipt?: TypedConversationTurnReceipt };
        if (candidate.receipt && sameReceipt(candidate.receipt, receipt)) accepted.push(parsed);
      } catch {
        // Invalid model-controlled local output cannot become a public reply.
      } finally {
        try { fs.unlinkSync(staged); } catch { /* server idempotency remains final fence */ }
      }
    }
    return accepted;
  }

  async dispatch(
    receipt: TypedConversationTurnReceipt,
    prompt: string | Array<Record<string, unknown>>,
  ): Promise<TypedFastTurn> {
    this.ensureStarted();
    this.writeReceipt(receipt);
    const session = this.session;
    if (!session) throw new Error('Fast Resident failed to start');
    const result = await session.sendTurn(prompt);
    if (result.interrupted || result.timedOut) {
      if (this.session === session) this.closeSessionOnly();
      return { receipt, result, completion: null, workdayRequests: [] };
    }
    const completions = this.drain('complete-', parseCompletion, receipt);
    const requests = this.drain('request-', parseWorkdayRequest, receipt);
    const accepted = validateStagedTypedIntents(completions, requests);
    return {
      receipt,
      result,
      completion: accepted.completion,
      workdayRequests: accepted.workdayRequests,
    };
  }

  close(): void { this.closeSessionOnly(); removeQuietly(this.runtimeDir); }

  private closeSessionOnly(): void {
    try { this.session?.close(); } catch { /* replacement proceeds */ }
    this.session = null;
    if (this.profile) {
      removeQuietly(this.profile.cwd);
      removeQuietly(this.profile.env.HOME);
    }
    this.profile = null;
    this.tool = null;
  }
}

export function newTypedConversationReceipt(claimId: string, messageIds: string[]): TypedConversationTurnReceipt {
  return {
    claim_id: nonEmpty(claimId, 'claim id', 256),
    message_ids: messageIds.map((id) => nonEmpty(id, 'message id', 256)),
    idempotency_key: `typed-fast:${claimId}:${randomUUID()}`,
  };
}

export type TypedFastAbandonStatus = 'abandoned' | 'already_abandoned' | 'already_completed';

export function typedFastFailureBackoffMs(failureCount: number): number {
  if (!Number.isSafeInteger(failureCount) || failureCount < 1) return 0;
  return Math.min(60_000, 1_000 * (2 ** Math.min(failureCount - 1, 6)));
}

export function typedFastAbandonDisposition(
  status: TypedFastAbandonStatus | undefined,
): 'fallback' | 'completed' | 'deferred' {
  if (status === 'already_completed') return 'completed';
  if (status === 'abandoned' || status === 'already_abandoned') return 'fallback';
  return 'deferred';
}

/** A status string is insufficient: only the Coordinator can prove fallback is safe. */
export function typedFastAbandonDispositionWithConfirmation(
  status: TypedFastAbandonStatus | undefined,
  legacyFallbackAdmissible: boolean | undefined,
): 'fallback' | 'completed' | 'deferred' {
  if (status === 'already_completed') return 'completed';
  return legacyFallbackAdmissible === true
    ? typedFastAbandonDisposition(status)
    : 'deferred';
}

export const _typedFastInternals = {
  receiptFrom, sameReceipt, parseCompletion, parseWorkdayRequest, validateStagedTypedIntents,
};
