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

/**
 * Extract the first JSON object from `text` that contains all `requiredKeys`.
 * Returns the parsed object, or null if none found.
 * Throws JsonExtractionError if matching candidates were present but malformed.
 */
export function extractJsonObject<T = Record<string, unknown>>(text: string, ...requiredKeys: string[]): T | null {
  // Quick bail: all keys must appear somewhere in the text
  for (const key of requiredKeys) {
    if (!text.includes(`"${key}"`)) return null;
  }
  let pos = 0;
  const parseErrors: string[] = [];
  while (pos < text.length) {
    const start = text.indexOf('{', pos);
    if (start === -1) break;
    const end = findMatchingClose(text, start, '{', '}');
    if (end === -1) break;
    const candidate = text.slice(start, end + 1);
    if (requiredKeys.every(k => candidate.includes(`"${k}"`))) {
      try {
        return JSON.parse(candidate) as T;
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
  return null;
}

/**
 * Extract the first JSON array from `text` that contains objects with all `requiredKeys`.
 * Returns the parsed array, or null if none found.
 * Throws JsonExtractionError if matching candidates were present but malformed.
 */
export function extractJsonArray<T = unknown[]>(text: string, ...requiredKeys: string[]): T | null {
  for (const key of requiredKeys) {
    if (!text.includes(`"${key}"`)) return null;
  }
  let pos = 0;
  const parseErrors: string[] = [];
  while (pos < text.length) {
    const start = text.indexOf('[', pos);
    if (start === -1) break;
    const end = findMatchingClose(text, start, '[', ']');
    if (end === -1) break;
    const candidate = text.slice(start, end + 1);
    if (requiredKeys.every(k => candidate.includes(`"${k}"`))) {
      try {
        return JSON.parse(candidate) as T;
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
  return null;
}
