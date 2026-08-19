import { describe, test, expect } from 'vitest';
import {
  armingHintCopy,
  armingToastCopy,
  parseArmingOutcome,
  conversationAcceptanceToastCopy,
  workbenchArmingToastCopy,
} from './messageArming';

describe('armingToastCopy — post-send toast grounded in the real outcome', () => {
  test('steering outcome confirms the strategist is being steered', () => {
    expect(armingToastCopy('steering')).toBe('Message sent — steering the strategist');
  });
  test('passive_note outcome states it won’t wake the strategist', () => {
    expect(armingToastCopy('passive_note')).toContain('won’t wake the strategist');
  });
  test('routine_event outcome states it won’t wake the strategist', () => {
    expect(armingToastCopy('routine_event')).toContain('won’t wake the strategist');
  });
  test('not_armed outcome is truthful: saved but the strategist wasn’t woken', () => {
    const copy = armingToastCopy('not_armed');
    expect(copy).toContain('wasn’t woken');
    expect(copy).toContain('resending');
    // Must NOT make the false steering promise.
    expect(copy).not.toContain('steering the strategist');
  });
  test('duplicate outcome confirms the existing steer is already armed', () => {
    expect(armingToastCopy('duplicate')).toBe('Message already sent — steer already armed');
  });
  test('missing outcome falls back to the generic confirmation', () => {
    expect(armingToastCopy(null)).toBe('Message sent');
    expect(armingToastCopy(undefined)).toBe('Message sent');
  });
});
describe('conversationAcceptanceToastCopy — canonical conversation receipt wins', () => {
  test('queued overrides a contradictory legacy not_armed outcome', () => {
    const receipt = conversationAcceptanceToastCopy('queued', 'not_armed');
    expect(receipt).toEqual({
      message: 'Message accepted — queued',
      severity: 'success',
    });
    expect(receipt.message).not.toContain('wasn’t woken');
  });

  test('capacity and paused receipts stay truthful', () => {
    expect(conversationAcceptanceToastCopy('waiting_for_capacity', 'steering').message)
      .toContain('waiting to start');
    expect(conversationAcceptanceToastCopy('paused', 'steering').severity).toBe('info');
  });
});


describe('armingHintCopy — composer hint per outcome', () => {
  test('steering hint reaches the strategist', () => {
    expect(armingHintCopy('steering')).toContain('reaches the strategist soon');
  });
  test('passive_note hint won’t wake', () => {
    expect(armingHintCopy('passive_note')).toContain('won’t wake the strategist');
  });
  test('routine_event hint won’t wake', () => {
    expect(armingHintCopy('routine_event')).toContain('won’t wake the strategist');
  });
  test('not_armed hint admits the wake failed and suggests resending', () => {
    const copy = armingHintCopy('not_armed');
    expect(copy).toContain('Couldn’t wake the strategist');
    expect(copy).toContain('resending');
  });
  test('duplicate hint reflects the idempotent replay', () => {
    expect(armingHintCopy('duplicate')).toBe('Message already sent — steer already armed.');
  });
});

describe('workbenchArmingToastCopy — mode-change wake receipts', () => {
  test('steering confirms the strategist is being woken', () => {
    expect(workbenchArmingToastCopy('steering')).toBe('Strategic mode saved — waking the strategist');
  });

  test('not_armed is truthful about the failed wake', () => {
    const copy = workbenchArmingToastCopy('not_armed');
    expect(copy).toContain('wasn’t woken');
    expect(copy).not.toContain('waking the strategist');
  });

  test('passive and missing outcomes stay quiet', () => {
    expect(workbenchArmingToastCopy('passive_note')).toBeNull();
    expect(workbenchArmingToastCopy('routine_event')).toBeNull();
    expect(workbenchArmingToastCopy(null)).toBeNull();
  });
});

describe('parseArmingOutcome — narrows an untrusted response field', () => {
  function backendLikeCreateResponse(
    arming: 'steering' | 'passive_note' | 'routine_event' | 'not_armed' | 'duplicate',
  ) {
    const messageId = `m-${arming}`;
    return {
      id: messageId,
      status: arming === 'duplicate' ? 'duplicate' : 'created',
      arming,
      message: {
        id: messageId,
        project_id: 'proj-1',
        message_type: 'FEEDBACK',
        from_type: 'user',
        from_task_id: null,
        content: 'Operator message',
        context: {},
        status: 'open',
        resolution: null,
        resolved_at: null,
        created_at: '2026-07-01T12:00:00Z',
      },
    };
  }

  const backendLikeArmingFixtures = [
    { response: backendLikeCreateResponse('steering'), expected: 'steering' },
    { response: backendLikeCreateResponse('passive_note'), expected: 'passive_note' },
    { response: backendLikeCreateResponse('routine_event'), expected: 'routine_event' },
    { response: backendLikeCreateResponse('not_armed'), expected: 'not_armed' },
    { response: backendLikeCreateResponse('duplicate'), expected: 'duplicate' },
  ] as const;

  test.each(backendLikeArmingFixtures)(
    'accepts backend-like response arming=$expected',
    ({ response, expected }) => {
      expect(parseArmingOutcome(response.arming)).toBe(expected);
    },
  );

  test('accepted outcomes have backend-like toast and hint copy', () => {
    for (const { response, expected } of backendLikeArmingFixtures) {
      const outcome = parseArmingOutcome(response.arming);
      expect(outcome).toBe(expected);
      expect(armingToastCopy(outcome)).toEqual(expect.any(String));
      expect(armingHintCopy(outcome!)).toEqual(expect.any(String));
    }
  });

  test('rejects unknown / missing values', () => {
    expect(parseArmingOutcome(undefined)).toBeNull();
    expect(parseArmingOutcome(null)).toBeNull();
    expect(parseArmingOutcome('bogus')).toBeNull();
    expect(parseArmingOutcome(42)).toBeNull();
  });
});
