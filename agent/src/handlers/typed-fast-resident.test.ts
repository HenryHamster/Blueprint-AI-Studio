import { afterEach, describe, expect, it, vi } from 'vitest';
import fs from 'fs';
import type { ResidentSession, ResidentTurnResult } from '../providers/resident-session.js';
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
  it('poisons the child when direct managed-execution staging violates its role', async () => {
    let alive = true;
    const close = vi.fn(() => { alive = false; });
    const cleanResult: ResidentTurnResult = {
      resultText: '', accumulatedTurnText: '', interrupted: false,
      timedOut: false, hardTimedOut: false, appliedStrategicOp: false,
      sawToolUse: true, sentOperatorMessage: false, operatorReply: null,
      processGone: false, ttftMs: null, turnMs: 1, toolCallCount: 1,
      totalToolCallDurationMs: 1, meanInterTokenMs: null, inputTokens: 1,
      providerFailed: false, failureCategory: null,
    };
    const adapter: ForegroundResidentAdapter = {
      id: 'invalid-staging',
      validation: 'qualified',
      start: ({ mcpConfigPath }) => {
        const config = JSON.parse(fs.readFileSync(mcpConfigPath, 'utf8')) as {
          mcpServers: { call: { env: {
            CALL_LOCAL_STAGE_DIR: string;
            CALL_TURN_RECEIPT_PATH: string;
          } } };
        };
        const paths = config.mcpServers.call.env;
        return {
          sendTurn: () => {
            const receipt: unknown = JSON.parse(
              fs.readFileSync(paths.CALL_TURN_RECEIPT_PATH, 'utf8'),
            );
            fs.writeFileSync(
              `${paths.CALL_LOCAL_STAGE_DIR}/complete-invalid.json`,
              JSON.stringify({
                receipt,
                content: "I'll edit the configuration now.",
              }),
            );
            return Promise.resolve(cleanResult);
          },
          interrupt: () => undefined,
          onEvent: () => undefined,
          touch: () => undefined,
          waitForIdle: () => Promise.resolve(),
          close,
          sessionId: () => null,
          isAlive: () => alive,
          exitCode: () => null,
        } satisfies ResidentSession;
      },
    };
    const controller = new ForegroundTypedController({ adapter });
    const receipt = {
      claim_id: 'invalid-claim',
      message_ids: ['invalid-message'],
      idempotency_key: 'invalid-key',
    };
    try {
      await expect(controller.dispatch(receipt, 'Answer the operator.')).rejects.toThrow(
        /delegate through Workday task tooling/,
      );
      expect(close).toHaveBeenCalledTimes(1);
      expect(controller.isAlive()).toBe(false);
    } finally {
      controller.close();
    }
  });

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

  it('accepts one exact structured Workday request with its public completion', () => {
    expect(MAX_TYPED_WORKDAY_REQUESTS_PER_TURN).toBe(1);
    const receipt = { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' };
    const completion = _typedFastInternals.parseCompletion({
      receipt, content: 'The current evidence is incomplete, so this answer is bounded.',
    });
    const request = _typedFastInternals.parseWorkdayRequest({
      receipt, delivery_key: 'only-one', prompt: 'Check the derivation.',
    });
    expect(_typedFastInternals.validateStagedTypedIntents(
      [completion], [request],
    )).toEqual({ completion, workdayRequests: [request] });

    expect(() => _typedFastInternals.validateStagedTypedIntents([], [request])).toThrow(
      /without a public completion/,
    );
    expect(() => _typedFastInternals.validateStagedTypedIntents([completion], [
      { ...request, receipt: { ...receipt, message_ids: ['other'] } },
    ])).toThrow(/different receipt/);
    expect(() => _typedFastInternals.validateStagedTypedIntents([completion], [
      request,
      { ...request, delivery_key: 'second' },
    ])).toThrow(/too many Workday requests/);
    expect(() => _typedFastInternals.validateStagedTypedIntents(
      [completion, { ...completion, content: 'Second.' }], [],
    )).toThrow(/multiple public completions/);
    expect(() => _typedFastInternals.parseWorkdayRequest({
      receipt, delivery_key: 'd'.repeat(129), prompt: 'Check the derivation.',
    })).toThrow(/delivery key/);
    expect(() => _typedFastInternals.parseWorkdayRequest({
      receipt, delivery_key: 'delivery-key', prompt: 'q'.repeat(20_001),
    })).toThrow(/Workday prompt/);
  });

  it('rejects direct managed execution', () => {
    const receipt = { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' };
    for (const content of [
      "I'll remove the knob entries and delete the branches.",
      "I'll edit the configuration now.",
      "I'm going to edit the configuration now.",
      'I’m going to deploy the service now.',
      "I'm editing the configuration now.",
      'I’m deploying the service now.',
      "I'll fix the code now.",
      'Let me change the setting.',
      'I’m updating the config now.',
      "I'm configuring the service now.",
      "I've edited the configuration already.",
      'I’ve deployed the service already.',
      "I've implemented the repository change.",
      'I’ve fixed the code already.',
      'I have pushed the commit already.',
      'I just updated the config.',
      'I will update you about the issue. I will fix the code now.',
      "I'll deploy to production.",
      "I'll push to main.",
      "I'll merge into the default branch.",
      "I'll deploy it to production.",
      "I'll deploy it now.",
      "I'll push it now.",
      "I'll commit it.",
      "I've deployed it already.",
      "I'll deploy now.",
      "I'm deploying the service now.",
      "I'm deploying now.",
      "I've deployed already.",
      "I'll push shortly.",
      'I deployed it.',
      'I pushed to main.',
      'I committed the change.',
      "I'll remove it from the config.",
      "I'll commit and push the code.",
      "I'll quickly edit the code.",
      "I'll just deploy to production.",
      "I'll go ahead and push to main.",
      "I'll make the code change.",
      "I'll apply the patch.",
      "I'll open a pull request.",
      "I'll run the migration.",
      "I'll cherry-pick the commit.",
      "I'll take care of editing the code.",
      "I'll handle the code change.",
      "I'll go update the code.",
      "I'll be editing the code.",
      "I'll work on the code.",
      "I'll edit strategic-resident.ts.",
      "I'll update the codebase.",
      'Let me deploy the service change.',
      "I'll fast-forward main now.",
      'I’ll fast-forward main now.',
    ]) {
      const completion = _typedFastInternals.parseCompletion({ receipt, content });
      expect(() => _typedFastInternals.validateStagedTypedIntents(
        [completion], [],
      ), content).toThrow(/delegate through Workday task tooling/);
    }
  });

  it('accepts neutral strategist prose without body-status classification', () => {
    const receipt = { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' };
    for (const content of [
      'We can change the mode after checking the tradeoff.',
      'We are editing the direction together, not touching repository code.',
      "I've reviewed the deployment options and can route a task if you want.",
      'I will update you about the code.',
      "I'm updating you on the deployment status.",
      "I've changed my recommendation for the repository.",
      "I'll edit my recommendation after reviewing the code.",
      "I've rewritten this answer to explain the config.",
      "I'm resetting expectations around the deployment.",
      "I'll change the deployment recommendation.",
      "I'll rewrite the deployment plan.",
      "I'll edit the code example.",
      "I'll push back on that recommendation.",
      "I'll commit to explaining the tradeoff.",
      "I'll merge these ideas in the summary.",
      'I recommend pushing to main after the checks pass.',
      "I'll explain why we should not deploy yet.",
      'A Blueprint task could make the change if you want.',
      "I'll create a task list explaining the deployment options.",
      "I've created a task list to compare the deployment options.",
      "I've opened a job description to explain the role.",
      "I've started the task discussion in this answer.",
      "I've queued a request example for the documentation.",
      "I've routed the task wording into the summary.",
      'I have created a request for clarification below.',
      'I have opened a request for your feedback.',
      'The server badge renders the phrase "Workday requested" when its receipt proves it.',
      "I've queued a task-list example for this explanation.",
      'I will follow up by explaining the existing evidence in this reply.',
      "I've queued a task.",
      'Workday requested.',
      'Fast only.',
      'I will follow up with the result.',
      'I ran.',
      'I am running.',
      'I will run.',
      'I fixed.',
      'I will fix.',
    ]) {
      const completion = _typedFastInternals.parseCompletion({ receipt, content });
      expect(_typedFastInternals.validateStagedTypedIntents(
        [completion], [],
      ), content).toEqual({ completion, workdayRequests: [] });
    }
  });

  it('keeps the managed-execution fence bounded on 20k prose', () => {
    const receipt = { claim_id: 'c', message_ids: ['m'], idempotency_key: 'k' };
    const neutralContent = 'neutral architecture explanation, '.repeat(700).slice(0, 20_000);
    const managedContent = `${'x'.repeat(19_940)} I'll edit the configuration.`;
    const neutral = _typedFastInternals.parseCompletion({ receipt, content: neutralContent });
    const managed = _typedFastInternals.parseCompletion({ receipt, content: managedContent });
    const started = performance.now();
    for (let attempt = 0; attempt < 20; attempt += 1) {
      expect(_typedFastInternals.validateStagedTypedIntents(
        [neutral], [],
      )).toEqual({ completion: neutral, workdayRequests: [] });
      expect(() => _typedFastInternals.validateStagedTypedIntents(
        [managed], [],
      )).toThrow(/delegate through Workday task tooling/);
    }
    expect(performance.now() - started).toBeLessThan(250);
  });
});
