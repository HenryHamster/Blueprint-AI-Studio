import { describe, test, expect } from 'vitest';
import { linkifyCitations, parseCitationHref, toCitationHref } from './citationLinks';

describe('linkifyCitations', () => {
  test('links a direction id', () => {
    expect(linkifyCitations('see d-dec033c2 for context')).toBe(
      'see [d-dec033c2](citation://direction/dec033c2) for context',
    );
  });

  test('links a PR reference', () => {
    expect(linkifyCitations('landed in #17493 yesterday')).toBe(
      'landed in [#17493](citation://pr/17493) yesterday',
    );
  });

  test('links a full task uuid', () => {
    const id = '0e0689a0-1234-4abc-89ab-1234567890ab';
    expect(linkifyCitations(`task ${id} is stuck`)).toBe(
      `task [${id}](citation://task/${id}) is stuck`,
    );
  });

  test('links an 8-hex short task id anchored by the word "task"', () => {
    expect(linkifyCitations('blocked on task c0f26a99 review')).toBe(
      'blocked on [task c0f26a99](citation://task/c0f26a99) review',
    );
  });

  test('does not link a bare 8-hex string with no anchoring context', () => {
    const input = 'commit sha c0f26a99 landed';
    expect(linkifyCitations(input)).toBe(input);
  });

  test('does not link identifiers inside fenced code blocks', () => {
    const input = '```\nd-dec033c2 #17493\n```';
    expect(linkifyCitations(input)).toBe(input);
  });

  test('does not link identifiers inside inline code spans', () => {
    const input = 'ref `d-dec033c2` here';
    expect(linkifyCitations(input)).toBe(input);
  });

  test('links multiple distinct identifiers in one message', () => {
    const input = 'direction d-dec033c2 shipped via #17493';
    expect(linkifyCitations(input)).toBe(
      'direction [d-dec033c2](citation://direction/dec033c2) shipped via [#17493](citation://pr/17493)',
    );
  });

  test('passes through plain text unchanged', () => {
    const input = 'no identifiers in this message at all.';
    expect(linkifyCitations(input)).toBe(input);
  });
});

describe('parseCitationHref / toCitationHref', () => {
  test('round-trips a direction target', () => {
    const href = toCitationHref('direction', 'dec033c2');
    expect(parseCitationHref(href)).toEqual({ kind: 'direction', id: 'dec033c2' });
  });

  test('round-trips a pr target', () => {
    const href = toCitationHref('pr', '17493');
    expect(parseCitationHref(href)).toEqual({ kind: 'pr', id: '17493' });
  });

  test('returns null for a non-citation href', () => {
    expect(parseCitationHref('https://example.com')).toBeNull();
  });

  test('returns null for a malformed citation href', () => {
    expect(parseCitationHref('citation://task')).toBeNull();
    expect(parseCitationHref('citation://bogus-kind/abc')).toBeNull();
  });
});
