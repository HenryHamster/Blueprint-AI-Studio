import {
  STALE_RESULT_RETRY_BASE_MS, STALE_RESULT_RETRY_MAX_MS,
  STALE_RESULT_MAX_ATTEMPTS, STALE_RESULT_MAX_AGE_MS,
} from '../constants.js';
import { sanitize } from './sanitize.js';

/** Persist result payload to disk before POSTing status, then clean up on success.
 *  If the POST fails (network error, VM kill), the file survives for retry on next startup. */
export type StaleResultEnvelope = {
  payload: Record<string, unknown>;
  retry: {
    attempts: number;
    createdAt: number;
    nextAttemptAt: number;
    lastAttemptAt?: number;
    lastError?: string;
  };
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function createStaleResultEnvelope(
  payload: Record<string, unknown>,
  now = Date.now(),
): StaleResultEnvelope {
  return {
    payload,
    retry: {
      attempts: 0,
      createdAt: now,
      nextAttemptAt: 0,
    },
  };
}

export function parseStaleResultFile(raw: string, now = Date.now()): StaleResultEnvelope {
  const parsed = JSON.parse(raw) as unknown;
  if (!isRecord(parsed)) throw new Error('stale result file is not a JSON object');
  const payload = parsed.payload;
  const retry = parsed.retry;
  if (isRecord(payload) && isRecord(retry)) {
    return {
      payload,
      retry: {
        attempts: typeof retry.attempts === 'number' ? retry.attempts : 0,
        createdAt: typeof retry.createdAt === 'number' ? retry.createdAt : now,
        nextAttemptAt: typeof retry.nextAttemptAt === 'number' ? retry.nextAttemptAt : 0,
        lastAttemptAt: typeof retry.lastAttemptAt === 'number' ? retry.lastAttemptAt : undefined,
        lastError: typeof retry.lastError === 'string' ? retry.lastError : undefined,
      },
    };
  }
  return createStaleResultEnvelope(parsed, now);
}

export function staleResultBackoffMs(attempts: number): number {
  const exponent = Math.max(0, Math.min(attempts - 1, 10));
  return Math.min(STALE_RESULT_RETRY_BASE_MS * Math.pow(2, exponent), STALE_RESULT_RETRY_MAX_MS);
}

export function shouldAbandonStaleResult(envelope: StaleResultEnvelope, now = Date.now()): boolean {
  return envelope.retry.attempts >= STALE_RESULT_MAX_ATTEMPTS
    || now - envelope.retry.createdAt > STALE_RESULT_MAX_AGE_MS;
}

export function markStaleResultFailure(
  envelope: StaleResultEnvelope,
  error: unknown,
  now = Date.now(),
): StaleResultEnvelope {
  const attempts = envelope.retry.attempts + 1;
  const nextAttemptAt = now + staleResultBackoffMs(attempts);
  const lastError = error instanceof Error ? error.message : String(error);
  return {
    payload: envelope.payload,
    retry: {
      ...envelope.retry,
      attempts,
      lastAttemptAt: now,
      nextAttemptAt,
      lastError: sanitize(lastError).slice(0, 500),
    },
  };
}
