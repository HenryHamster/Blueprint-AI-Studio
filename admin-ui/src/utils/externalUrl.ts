/**
 * Allowlist http(s) URLs before using them as external hrefs.
 *
 * Returns the normalized href for values that parse to an `http:`/`https:` URL,
 * and `null` for anything else (unparseable strings, or unsafe schemes such as
 * `javascript:`, `data:`, `file:`). Callers should skip rendering the link when
 * this returns `null`.
 *
 * Shared by the task sideboard and the task context rail so both surfaces apply
 * identical scheme sanitization to backend-supplied PR URLs.
 */
export function safeExternalUrl(value?: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}
