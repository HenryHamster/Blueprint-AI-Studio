import { describe, it, expect } from 'vitest';
import {
  describeJsonExtractionResult,
  JsonExtractionError,
  extractJsonObject,
  extractJsonArray,
  extractJsonObjectDetailed,
  extractJsonArrayDetailed,
} from './json-extract.js';

describe('extractJsonObject', () => {
  it('extracts a simple object with required keys', () => {
    const result = extractJsonObject<{ a: number; b: string }>('{"a": 1, "b": "x"}', 'a', 'b');
    expect(result).toEqual({ a: 1, b: 'x' });
  });

  it('extracts an object embedded in surrounding prose', () => {
    const text = 'Here is the plan: {"name": "task", "priority": 2}. End.';
    expect(extractJsonObject(text, 'name')).toEqual({ name: 'task', priority: 2 });
  });

  it('returns null when a required key is absent from the text (early bail)', () => {
    expect(extractJsonObject('{"a": 1}', 'missing')).toBeNull();
  });

  it('skips candidates that do not contain all required keys', () => {
    const text = '{"other": "nope"} {"key": "value"}';
    expect(extractJsonObject(text, 'key')).toEqual({ key: 'value' });
  });

  it('tries the next candidate when the first contains required keys but is invalid JSON', () => {
    const text = '{"key": invalid} {"key": "good"}';
    expect(extractJsonObject<{ key: string }>(text, 'key')).toEqual({ key: 'good' });
  });

  it('throws when required-key candidates are present but none parse', () => {
    expect(() => extractJsonObject('{"key": invalid}', 'key')).toThrow(JsonExtractionError);
  });

  it('handles braces inside string values (code snippets)', () => {
    const text = '{"code": "if (x) { return y; }", "type": "snippet"}';
    expect(extractJsonObject<{ code: string; type: string }>(text, 'code', 'type'))
      .toEqual({ code: 'if (x) { return y; }', type: 'snippet' });
  });

  it('handles escaped quotes inside string values', () => {
    const text = '{"msg": "he said \\"hi\\"", "id": 1}';
    expect(extractJsonObject<{ msg: string; id: number }>(text, 'msg'))
      .toEqual({ msg: 'he said "hi"', id: 1 });
  });

  it('returns null for empty input', () => {
    expect(extractJsonObject('', 'anything')).toBeNull();
    expect(extractJsonObject('')).toBeNull();
  });

  it('returns null when there is no object at all', () => {
    expect(extractJsonObject('just some text', 'key')).toBeNull();
  });

  it('surfaces a typed no-match result for callers that need diagnostics', () => {
    const result = extractJsonObjectDetailed('just some text', 'key');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason).toBe('missing_required_key');
      expect(describeJsonExtractionResult(result)).toContain('missing_required_key');
    }
  });

  it('returns null when a { has no matching close', () => {
    expect(extractJsonObject('{ "key": "value"', 'key')).toBeNull();
  });

  it('parses deeply nested objects (4 levels)', () => {
    const text = 'pre {"a": {"b": {"c": {"d": "deep"}}}} post';
    expect(extractJsonObject(text, 'a')).toEqual({ a: { b: { c: { d: 'deep' } } } });
  });

  it('picks the second of two consecutive objects when only it has the required key', () => {
    const text = '{"a": 1} {"b": 2, "key": "found"}';
    expect(extractJsonObject(text, 'key')).toEqual({ b: 2, key: 'found' });
  });

  it('returns the first valid candidate when multiple objects contain the key', () => {
    const text = '{"key": "first"} {"key": "second"}';
    expect(extractJsonObject<{ key: string }>(text, 'key')).toEqual({ key: 'first' });
  });
});

describe('extractJsonArray', () => {
  it('extracts a simple array of objects containing required keys', () => {
    const text = '[{"severity": "high", "msg": "a"}, {"severity": "low", "msg": "b"}]';
    expect(extractJsonArray(text, 'severity')).toEqual([
      { severity: 'high', msg: 'a' },
      { severity: 'low', msg: 'b' },
    ]);
  });

  it('extracts an array embedded in prose', () => {
    const text = 'findings: [{"severity": "med"}] -- done';
    expect(extractJsonArray(text, 'severity')).toEqual([{ severity: 'med' }]);
  });

  it('returns null when required key is absent from the text (early bail)', () => {
    expect(extractJsonArray('[{"a": 1}]', 'severity')).toBeNull();
  });

  it('returns null for empty input', () => {
    expect(extractJsonArray('')).toBeNull();
  });

  it('returns null when there is no array', () => {
    expect(extractJsonArray('just text', 'k')).toBeNull();
  });

  it('surfaces a typed array no-match result for callers that need diagnostics', () => {
    const result = extractJsonArrayDetailed('just text with "k"', 'k');

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason).toBe('no_json_start');
      expect(describeJsonExtractionResult(result)).toContain('no_json_start');
    }
  });

  it('handles nested arrays containing objects with required keys', () => {
    const text = '[[1, 2], [3, {"key": "v"}]]';
    expect(extractJsonArray(text, 'key')).toEqual([[1, 2], [3, { key: 'v' }]]);
  });

  it('handles brackets inside string values', () => {
    const text = '[{"code": "arr[0] = [1,2]", "id": 1}]';
    expect(extractJsonArray<Array<{ code: string; id: number }>>(text, 'code'))
      .toEqual([{ code: 'arr[0] = [1,2]', id: 1 }]);
  });

  it('skips candidate arrays whose content does not include required keys', () => {
    const text = '[1, 2, 3] [{"severity": "high"}]';
    expect(extractJsonArray(text, 'severity')).toEqual([{ severity: 'high' }]);
  });

  it('tries the next array when the first contains the key string but is invalid JSON', () => {
    const text = '["severity": broken] [{"severity": "ok"}]';
    expect(extractJsonArray(text, 'severity')).toEqual([{ severity: 'ok' }]);
  });

  it('throws when required-key arrays are present but none parse', () => {
    expect(() => extractJsonArray('["severity": broken]', 'severity')).toThrow(JsonExtractionError);
  });
});
