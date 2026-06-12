/**
 * Sanitize sensitive credentials and tokens from log output and error messages.
 * Removes or masks:
 * - GitHub personal access tokens (ghp_, ghs_, github_pat_)
 * - Anthropic API keys (sk-ant-...)
 * - OpenAI API keys (sk-..., sk-proj-...)
 * - Google API keys (AIza...)
 * - Bearer tokens and OAuth tokens
 * - JSON/key-value credential fields
 * - JWT tokens
 * - Private SSH keys
 * - Git URLs with embedded credentials
 *
 * @param text — Raw text containing potentially sensitive data
 * @returns Text with all sensitive patterns replaced with masked values
 */
export function sanitize(text: string): string {
  return text
    .replace(/https:\/\/x-access-token:[^@]+@/g, 'https://x-access-token:***@')
    .replace(/ghp_[A-Za-z0-9_]+/g, 'ghp_***')
    .replace(/ghs_[A-Za-z0-9_]+/g, 'ghs_***')
    .replace(/github_pat_[A-Za-z0-9_]+/g, 'github_pat_***')
    .replace(/sk-ant-[A-Za-z0-9_-]+/g, 'sk-ant-***')
    .replace(/sk-proj-[A-Za-z0-9_-]{20,}/g, 'sk-proj-***')
    .replace(/sk-(?!ant-|proj-)[A-Za-z0-9_-]{20,}/g, 'sk-***')
    .replace(/AIza[A-Za-z0-9_-]{20,}/g, 'AIza***')
    .replace(
      // keep in sync with backend/app/broadcast.py and backend/app/failure_classification.py
      // Value group uses [^'"\\]|\\. (unambiguous alternation — avoids ReDoS on backslash-heavy inputs)
      /(["']?)(access_token|refresh_token|id_token|api_key|client_secret|private_key|token)\1(\s*[:=]\s*)(["'])((?:[^'"\\]|\\.)+)\4/gi,
      (_match, keyQuote: string, key: string, separator: string, valueQuote: string) =>
        `${keyQuote}${key}${keyQuote}${separator}${valueQuote}***${valueQuote}`,
    )
    .replace(/eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, 'eyJ***')
    .replace(/Bearer\s+[^\s,'"]+/gi, 'Bearer ***')
    .replace(/oauth_token[=:]\s*[^\s&'"]+/gi, 'oauth_token=***')
    .replace(/-----BEGIN[A-Z\s]+PRIVATE\s+KEY-----[\s\S]*?-----END[A-Z\s]+PRIVATE\s+KEY-----/g, '***PRIVATE KEY***');
}
