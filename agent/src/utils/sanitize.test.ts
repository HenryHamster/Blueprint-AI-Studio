import { describe, expect, it } from 'vitest';

import { sanitize } from './sanitize.js';

describe('sanitize call speech credentials', () => {
  it.each([
    'CALL_DEEPGRAM_API_KEY',
    'CALL_CARTESIA_API_KEY',
    'X-API-Key',
  ])('redacts %s in assignment and JSON forms', (name) => {
    const secret = 'provider-secret-value-12345';

    expect(sanitize(`${name}=${secret}`)).toBe(`${name}=***`);
    expect(sanitize(`{"${name}":"${secret}"}`)).toBe(`{"${name}":***}`);
    expect(sanitize(`'${name}': '${secret}'`)).toBe(`'${name}': ***`);
  });
});
