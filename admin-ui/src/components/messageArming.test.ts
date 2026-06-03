import { describe, test, expect } from 'vitest';
import {
  armingHintCopy,
  armingToastCopy,
  composerHintCopy,
  predictArming,
  parseArmingOutcome,
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
  test('missing outcome falls back to the generic confirmation', () => {
    expect(armingToastCopy(null)).toBe('Message sent');
    expect(armingToastCopy(undefined)).toBe('Message sent');
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
});

describe('composerHintCopy — busy-aware pre-send hint, server outcome wins post-send', () => {
  test('busy + non-NOTE pre-send: steer is queued for the next cycle', () => {
    const copy = composerHintCopy('FEEDBACK', null, true);
    expect(copy).toContain('queues for the next cycle');
    expect(copy).toContain('Strategist is working');
    // Must NOT make the immediate-reach promise while a cycle is running.
    expect(copy).not.toContain('reaches the strategist soon');
  });
  test('idle + non-NOTE pre-send: existing reaches-soon copy stands', () => {
    expect(composerHintCopy('FEEDBACK', null, false)).toContain('reaches the strategist soon');
    // Undefined busy (default idle) behaves the same.
    expect(composerHintCopy('FEEDBACK', null, undefined)).toContain('reaches the strategist soon');
  });
  test('NOTE + busy: still won’t-wake — busy state does not change passive copy', () => {
    const copy = composerHintCopy('NOTE', null, true);
    expect(copy).toContain('won’t wake the strategist');
    expect(copy).not.toContain('queues for the next cycle');
  });
  test('post-send: server outcome wins even while busy (override suppressed)', () => {
    // lastArming present → the real server outcome is reflected verbatim.
    expect(composerHintCopy('FEEDBACK', 'steering', true)).toBe(armingHintCopy('steering'));
    expect(composerHintCopy('FEEDBACK', 'routine_event', true)).toBe(armingHintCopy('routine_event'));
    expect(composerHintCopy('FEEDBACK', 'not_armed', true)).toBe(armingHintCopy('not_armed'));
  });
});

describe('predictArming — type-only prediction before any send', () => {
  test('NOTE predicts a passive note', () => {
    expect(predictArming('NOTE')).toBe('passive_note');
  });
  test('non-NOTE types predict steering', () => {
    expect(predictArming('FEEDBACK')).toBe('steering');
    expect(predictArming('BLOCKER')).toBe('steering');
  });
});

describe('parseArmingOutcome — narrows an untrusted response field', () => {
  test('accepts the four valid outcomes', () => {
    expect(parseArmingOutcome('steering')).toBe('steering');
    expect(parseArmingOutcome('passive_note')).toBe('passive_note');
    expect(parseArmingOutcome('routine_event')).toBe('routine_event');
    expect(parseArmingOutcome('not_armed')).toBe('not_armed');
  });
  test('rejects unknown / missing values', () => {
    expect(parseArmingOutcome(undefined)).toBeNull();
    expect(parseArmingOutcome(null)).toBeNull();
    expect(parseArmingOutcome('bogus')).toBeNull();
    expect(parseArmingOutcome(42)).toBeNull();
  });
});
