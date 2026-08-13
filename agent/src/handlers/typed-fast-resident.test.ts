import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ResidentSession } from '../providers/resident-session.js';
import type { ForegroundResidentAdapter } from '../providers/foreground-resident.js';
import {
  _typedFastInternals,
  ForegroundTypedController,
  MAX_TYPED_WORKDAY_REQUESTS_PER_TURN,
  newTypedConversationReceipt,
  typedFastAbandonDisposition,
  typedFastAbandonDispositionWithConfirmation,
  typedFastFailureBackoffMs,
} from './typed-fast-resident.js';

afterEach(() => { vi.useRealTimers(); });

describe('typed Fast local receipt fence', () => {
  it('closes and rejects a readiness probe that never settles', async () => {
    vi.useFakeTimers();
    const close = vi.fn();
    const adapter: ForegroundResidentAdapter = {
      id: 'never-ready',
      validation: 'qualified',
      start: () => ({
        isAlive: () => true,
        sendTurn: () => new Promise<never>(() => {}),
        close,
      } as unknown as ResidentSession),
    };
    const controller = new ForegroundTypedController({ adapter });
    try {
      const assertion = expect(controller.ensureReady()).rejects.toThrow(/controller deadline/);
      await vi.advanceTimersByTimeAsync(15_000);
      await assertion;
      expect(close).toHaveBeenCalledTimes(1);
      expect(controller.isAlive()).toBe(false);
    } finally {
      controller.close();
    }
  });

  it('binds a public completion to the exact claimed message set', () => {
    const receipt = {
      claim_id: 'claim-1', message_ids: ['message-a', 'message-b'], idempotency_key: 'key-1',
    };
    const completion = _typedFastInternals.parseCompletion({
      receipt, content: 'Here is the direct answer.',
    });
    expect(_typedFastInternals.sameReceipt(completion.receipt, receipt)).toBe(true);
    expect(_typedFastInternals.sameReceipt(
      { ...completion.receipt, message_ids: ['message-b', 'message-a'] }, receipt,
    )).toBe(false);
  });

  it('does not accept malformed staged output as a consultation or reply', () => {
    expect(() => _typedFastInternals.parseCompletion({
      receipt: { claim_id: 'c', message_ids: [], idempotency_key: 'k' }, content: 'x',
    })).toThrow(/message_ids/);
    expect(() => _typedFastInternals.parseWorkdayRequest({
      receipt: { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' },
      delivery_key: '', prompt: 'please investigate',
    })).toThrow(/delivery key/);
  });

  it('creates a unique server-idempotency key without trusting model output', () => {
    const first = newTypedConversationReceipt('claim-1', ['message-a']);
    const second = newTypedConversationReceipt('claim-1', ['message-a']);
    expect(first.idempotency_key).not.toBe(second.idempotency_key);
    expect(first.message_ids).toEqual(['message-a']);
  });

  it('falls back only after the server confirms that the Fast claim is released', () => {
    expect(typedFastAbandonDisposition('abandoned')).toBe('fallback');
    expect(typedFastAbandonDisposition('already_abandoned')).toBe('fallback');
    expect(typedFastAbandonDisposition('already_completed')).toBe('completed');
    expect(typedFastAbandonDisposition(undefined)).toBe('deferred');
    expect(typedFastAbandonDispositionWithConfirmation('abandoned', false)).toBe('deferred');
    expect(typedFastAbandonDispositionWithConfirmation('already_abandoned', undefined)).toBe('deferred');
    expect(typedFastAbandonDispositionWithConfirmation('abandoned', true)).toBe('fallback');
    expect(typedFastAbandonDispositionWithConfirmation('already_completed', false)).toBe('completed');
  });

  it('uses a bounded exponential circuit delay', () => {
    expect(typedFastFailureBackoffMs(0)).toBe(0);
    expect(typedFastFailureBackoffMs(1)).toBe(1_000);
    expect(typedFastFailureBackoffMs(4)).toBe(8_000);
    expect(typedFastFailureBackoffMs(100)).toBe(60_000);
  });

  it('keeps the private Workday fan-out to one request per Fast turn', () => {
    expect(MAX_TYPED_WORKDAY_REQUESTS_PER_TURN).toBe(1);
    const receipt = { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' };
    const request = _typedFastInternals.parseWorkdayRequest({
      receipt, delivery_key: 'only-one', prompt: 'Check the derivation.',
    });
    expect(_typedFastInternals.validateStagedTypedIntents([], [request])).toMatchObject({
      workdayRequests: [{ delivery_key: 'only-one' }],
    });
    expect(() => _typedFastInternals.validateStagedTypedIntents([], [
      request,
      { ...request, delivery_key: 'second' },
    ])).toThrow(/too many Workday requests/);
  });

  it('bounds the atomic completion Workday payload to the coordinator contract', () => {
    const receipt = { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' };
    expect(() => _typedFastInternals.parseWorkdayRequest({
      receipt, delivery_key: 'd'.repeat(129), prompt: 'Check the derivation.',
    })).toThrow(/delivery key/);
    expect(() => _typedFastInternals.parseWorkdayRequest({
      receipt, delivery_key: 'delivery-key', prompt: 'q'.repeat(20_001),
    })).toThrow(/Workday prompt/);
  });

  it('rejects multiple public completions instead of choosing one arbitrarily', () => {
    const receipt = { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' };
    const completion = _typedFastInternals.parseCompletion({ receipt, content: 'One.' });
    expect(() => _typedFastInternals.validateStagedTypedIntents(
      [completion, { ...completion, content: 'Two.' }], [],
    )).toThrow(/multiple public completions/);
  });

  it('rejects direct execution and couples delegation promises to a Workday request', () => {
    const receipt = { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' };
    const directClaims = [
      "I'll remove the knob entries and delete the branches.",
      'I will edit the configuration now.',
      'Let me deploy the service change.',
      "I'll fast-forward main now.",
    ];
    for (const content of directClaims) {
      const completion = _typedFastInternals.parseCompletion({ receipt, content });
      expect(() => _typedFastInternals.validateStagedTypedIntents(
        [completion], [],
      )).toThrow(/delegate through Workday task tooling/);
    }

    for (const content of [
      'We can change the mode after checking the tradeoff.',
      'A Blueprint task could make the change if you want.',
      "I'll create a task list explaining the deployment options.",
    ]) {
      const proposal = _typedFastInternals.parseCompletion({ receipt, content });
      expect(_typedFastInternals.validateStagedTypedIntents([proposal], [])).toMatchObject({
        completion: proposal,
        workdayRequests: [],
      });
    }

    const request = _typedFastInternals.parseWorkdayRequest({
      receipt,
      delivery_key: 'managed-state-change',
      prompt: 'Use supported Blueprint task tooling to make the requested change.',
    });
    for (const content of [
      "I'll route a Blueprint task to change it and report the durable outcome.",
      "I'll create a Blueprint task to change the configuration.",
      "I'll create a task to deploy the service.",
      "I'll hand this off to Workday to update the repository.",
    ]) {
      const delegated = _typedFastInternals.parseCompletion({ receipt, content });
      expect(() => _typedFastInternals.validateStagedTypedIntents(
        [delegated], [],
      )).toThrow(/without staging a Workday request/);
      expect(_typedFastInternals.validateStagedTypedIntents([delegated], [request])).toMatchObject({
        completion: delegated,
        workdayRequests: [request],
      });
    }
  });
});
