import { describe, it, expect } from 'vitest';
import { generateIdempotencyKey } from './idempotencyKey';

describe('generateIdempotencyKey', () => {
  it('returns a non-empty string', () => {
    const key = generateIdempotencyKey();
    expect(typeof key).toBe('string');
    expect(key.length).toBeGreaterThan(0);
  });

  it('returns unique values on repeated calls', () => {
    const keys = new Set(Array.from({ length: 20 }, () => generateIdempotencyKey()));
    expect(keys.size).toBe(20);
  });

  it('uses crypto.randomUUID when available (standard UUID format)', () => {
    const key = generateIdempotencyKey();
    // crypto.randomUUID is available in happy-dom — output should be a UUID
    const uuidPattern = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
    expect(key).toMatch(uuidPattern);
  });

  it('falls back to timestamp-prefixed key when crypto.randomUUID is unavailable', () => {
    const orig = globalThis.crypto;
    try {
      // Stub out randomUUID to simulate environments without it
      Object.defineProperty(globalThis, 'crypto', {
        value: { ...orig, randomUUID: undefined },
        configurable: true,
      });
      const key = generateIdempotencyKey();
      expect(key).toMatch(/^idem-/);
      expect(key.length).toBeGreaterThan(5);
    } finally {
      Object.defineProperty(globalThis, 'crypto', { value: orig, configurable: true });
    }
  });

  it('fallback key format includes alphanumeric segments after the prefix', () => {
    const orig = globalThis.crypto;
    try {
      Object.defineProperty(globalThis, 'crypto', {
        value: { ...orig, randomUUID: undefined },
        configurable: true,
      });
      const key = generateIdempotencyKey();
      // idem-<base36 timestamp>-<random base36>
      const parts = key.split('-');
      expect(parts[0]).toBe('idem');
      expect(parts[1]).toBeTruthy(); // timestamp base36
      expect(parts[2]).toBeTruthy(); // random base36
    } finally {
      Object.defineProperty(globalThis, 'crypto', { value: orig, configurable: true });
    }
  });
});
