/**
 * String-aware JSON extraction utilities.
 *
 * Uses bracket-depth counting that skips braces inside JSON string values,
 * so code snippets or template literals in prompts don't break extraction.
 */

/**
 * Find the end index of a JSON value starting at `start` using string-aware
 * bracket counting.  Returns -1 if no balanced close is found.
 *
 * @param text   Full text to scan
 * @param start  Index of the opening bracket (`{` or `[`)
 * @param open   Opening bracket character
 * @param close  Closing bracket character
 */
function findMatchingClose(text: string, start: number, open: string, close: string): number {
  let depth = 0;
  let inString = false;
  let escapeNext = false;
  for (let i = start; i < text.length; i++) {
    const c = text[i];
    if (escapeNext) { escapeNext = false; continue; }
    if (c === '\\' && inString) { escapeNext = true; continue; }
    if (c === '"') { inString = !inString; continue; }
    if (inString) continue;
    if (c === open) depth++;
    else if (c === close) {
      depth--;
      if (depth === 0) return i;
    }
  }
  return -1;
}

export class JsonExtractionError extends Error {
  constructor(
    public readonly kind: 'object' | 'array',
    public readonly requiredKeys: readonly string[],
    message: string,
  ) {
    super(message);
    this.name = 'JsonExtractionError';
  }
}

export type JsonExtractionMissReason =
  | 'missing_required_key'
  | 'no_json_start'
  | 'unbalanced_json'
  | 'no_matching_candidate'
  | 'parsed_object_missing_key'
  | 'parsed_array_missing_key';

export type JsonExtractionResult<T> =
  | { ok: true; value: T }
  | {
      ok: false;
      kind: 'object' | 'array';
      reason: JsonExtractionMissReason;
      requiredKeys: readonly string[];
      detail: string;
    };

export function describeJsonExtractionResult(result: JsonExtractionResult<unknown>): string {
  if (result.ok) return 'matched';
  return `${result.kind} ${result.reason}: ${result.detail}`;
}

/**
 * Extract the first JSON object from `text` that contains all `requiredKeys`.
 * Returns the parsed object, or null if none found.
 * Throws JsonExtractionError if matching candidates were present but malformed.
 */
export function extractJsonObjectDetailed<T = Record<string, unknown>>(
  text: string,
  ...requiredKeys: string[]
): JsonExtractionResult<T> {
  for (const key of requiredKeys) {
    if (!text.includes(`"${key}"`)) {
      return {
        ok: false,
        kind: 'object',
        reason: 'missing_required_key',
        requiredKeys,
        detail: `required key "${key}" is absent from output`,
      };
    }
  }
  let pos = 0;
  const parseErrors: string[] = [];
  let sawStart = false;
  let sawUnbalanced = false;
  let sawMissingOwnKey = false;
  while (pos < text.length) {
    const start = text.indexOf('{', pos);
    if (start === -1) break;
    sawStart = true;
    const end = findMatchingClose(text, start, '{', '}');
    if (end === -1) {
      sawUnbalanced = true;
      break;
    }
    const candidate = text.slice(start, end + 1);
    if (requiredKeys.every(k => candidate.includes(`"${k}"`))) {
      try {
        const parsed = JSON.parse(candidate) as Record<string, unknown>;
        // Guard against envelope-wrapped output (e.g. Codex JSONL item.completed)
        // where required keys appear as substrings inside a nested string value
        // but are not own properties of the parsed object.
        const missingKeys = requiredKeys.filter(k => !(k in parsed));
        if (missingKeys.length > 0) {
          sawMissingOwnKey = true;
          pos = start + 1;
          continue;
        }
        return { ok: true, value: parsed as T };
      } catch (err: unknown) {
        parseErrors.push(err instanceof Error ? err.message : String(err));
      }
    }
    pos = start + 1;
  }
  if (requiredKeys.length > 0 && parseErrors.length > 0) {
    throw new JsonExtractionError(
      'object',
      requiredKeys,
      `Failed to parse JSON object containing required keys (${requiredKeys.join(', ')}): ${parseErrors[0]}`,
    );
  }
  if (!sawStart) {
    return {
      ok: false,
      kind: 'object',
      reason: 'no_json_start',
      requiredKeys,
      detail: 'no object opening brace found',
    };
  }
  if (sawMissingOwnKey) {
    return {
      ok: false,
      kind: 'object',
      reason: 'parsed_object_missing_key',
      requiredKeys,
      detail: `object candidate(s) parsed but required keys were not own properties (envelope-wrapping?)`,
    };
  }
  if (sawUnbalanced) {
    return {
      ok: false,
      kind: 'object',
      reason: 'unbalanced_json',
      requiredKeys,
      detail: 'object opening brace had no matching close',
    };
  }
  return {
    ok: false,
    kind: 'object',
    reason: 'no_matching_candidate',
    requiredKeys,
    detail: `no object contained all required keys (${requiredKeys.join(', ')})`,
  };
}

export function extractJsonObject(text: string, ...requiredKeys: string[]): Record<string, unknown> | null {
  const result = extractJsonObjectDetailed(text, ...requiredKeys);
  return result.ok ? result.value : null;
}

/**
 * Extract the first JSON array from `text` that contains objects with all `requiredKeys`.
 * Returns the parsed array, or null if none found.
 * Throws JsonExtractionError if matching candidates were present but malformed.
 */
export function extractJsonArrayDetailed<T = unknown[]>(
  text: string,
  ...requiredKeys: string[]
): JsonExtractionResult<T> {
  for (const key of requiredKeys) {
    if (!text.includes(`"${key}"`)) {
      return {
        ok: false,
        kind: 'array',
        reason: 'missing_required_key',
        requiredKeys,
        detail: `required key "${key}" is absent from output`,
      };
    }
  }
  let pos = 0;
  const parseErrors: string[] = [];
  let sawStart = false;
  let sawUnbalanced = false;
  let sawMissingOwnKey = false;
  while (pos < text.length) {
    const start = text.indexOf('[', pos);
    if (start === -1) break;
    sawStart = true;
    const end = findMatchingClose(text, start, '[', ']');
    if (end === -1) {
      sawUnbalanced = true;
      break;
    }
    const candidate = text.slice(start, end + 1);
    if (requiredKeys.every(k => candidate.includes(`"${k}"`))) {
      try {
        const parsed = JSON.parse(candidate) as unknown[];
        // For arrays with required keys, check that at least one top-level element
        // object actually has all required keys as own properties. This prevents
        // envelope arrays (where keys appear only in nested string values) from
        // being returned. If no top-level elements are plain objects, skip this check.
        if (requiredKeys.length > 0 && Array.isArray(parsed)) {
          const elementObjects = parsed.filter(
            (el): el is Record<string, unknown> =>
              el !== null && typeof el === 'object' && !Array.isArray(el),
          );
          if (
            elementObjects.length > 0 &&
            !elementObjects.some(el => requiredKeys.every(k => k in el))
          ) {
            sawMissingOwnKey = true;
            pos = start + 1;
            continue;
          }
        }
        return { ok: true, value: parsed as T };
      } catch (err: unknown) {
        parseErrors.push(err instanceof Error ? err.message : String(err));
      }
    }
    pos = start + 1;
  }
  if (requiredKeys.length > 0 && parseErrors.length > 0) {
    throw new JsonExtractionError(
      'array',
      requiredKeys,
      `Failed to parse JSON array containing required keys (${requiredKeys.join(', ')}): ${parseErrors[0]}`,
    );
  }
  if (!sawStart) {
    return {
      ok: false,
      kind: 'array',
      reason: 'no_json_start',
      requiredKeys,
      detail: 'no array opening bracket found',
    };
  }
  if (sawMissingOwnKey) {
    return {
      ok: false,
      kind: 'array',
      reason: 'parsed_array_missing_key',
      requiredKeys,
      detail: `array candidate(s) parsed but no element object had all required keys as own properties`,
    };
  }
  if (sawUnbalanced) {
    return {
      ok: false,
      kind: 'array',
      reason: 'unbalanced_json',
      requiredKeys,
      detail: 'array opening bracket had no matching close',
    };
  }
  return {
    ok: false,
    kind: 'array',
    reason: 'no_matching_candidate',
    requiredKeys,
    detail: `no array contained all required keys (${requiredKeys.join(', ')})`,
  };
}

export function extractJsonArray(text: string, ...requiredKeys: string[]): unknown[] | null {
  const result = extractJsonArrayDetailed(text, ...requiredKeys);
  return result.ok ? result.value : null;
}
