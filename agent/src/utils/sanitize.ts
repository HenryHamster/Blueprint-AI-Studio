/**
 * Sanitize sensitive credentials and tokens from log output and error messages.
 * Removes or masks:
 * - GitHub personal access tokens (ghp_, ghs_, github_pat_)
 * - Anthropic API keys (sk-ant-...)
 * - OpenAI API keys (sk-..., sk-proj-...)
 * - Google API keys (AIza...)
 * - AWS IAM access keys (AKIA...)
 * - Stripe API keys (sk_live_, sk_test_, rk_live_) and webhook secrets (whsec_)
 * - Database connection strings with embedded passwords (postgresql://, postgres://,
 *   and SQLAlchemy +driver variants like postgresql+asyncpg://)
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
    .replace(/ghu_[A-Za-z0-9_]+/g, 'ghu_***')
    .replace(/gho_[A-Za-z0-9_]+/g, 'gho_***')
    .replace(/ghr_[A-Za-z0-9_]+/g, 'ghr_***')
    .replace(/github_pat_[A-Za-z0-9_]+/g, 'github_pat_***')
    .replace(/sk-ant-[A-Za-z0-9_-]+/g, 'sk-ant-***')
    .replace(/sk-proj-[A-Za-z0-9_-]{20,}/g, 'sk-proj-***')
    .replace(/sk-(?!ant-|proj-)[A-Za-z0-9_-]{20,}/g, 'sk-***')
    .replace(/AKIA[0-9A-Z]{16}/g, 'AKIA***')
    .replace(/sk_live_[A-Za-z0-9_-]+/g, 'sk_live_***')
    .replace(/sk_test_[A-Za-z0-9_-]+/g, 'sk_test_***')
    .replace(/rk_live_[A-Za-z0-9_-]+/g, 'rk_live_***')
    .replace(/whsec_[A-Za-z0-9_-]+/g, 'whsec_***')
    // Postgres DSNs, incl. SQLAlchemy +driver variants (postgresql+asyncpg://, etc.)
    .replace(/(postgres(?:ql)?(?:\+\w+)?):\/\/[^:/@]+:[^@]+@/g, '$1://***:***@')
    .replace(/AIza[A-Za-z0-9_-]{20,}/g, 'AIza***')
    .replace(/ya29\.[A-Za-z0-9_.-]+/g, 'ya29***')
    .replace(
      // keep in sync with backend/app/broadcast.py and backend/app/failure_classification.py
      // Value group uses [^'"\\]|\\. (unambiguous alternation — avoids ReDoS on backslash-heavy inputs)
      // Alternation handles quoted values (ReDoS-safe inner group) and unquoted values (8+ char floor)
      // \b guards prevent matching 'api_key' inside compound names like OPENAI_API_KEY
      /(['"]?\b(?:access_token|refresh_token|id_token|api_key|client_secret|private_key|token)\b['"]?\s*[:=]\s*)(?:(['"])((?:[^'"\\]|\\.)+)\2|([A-Za-z0-9_.+=\-/@]{8,}))/gi,
      (_match, prefix: string, quote: string | undefined) =>
        quote ? `${prefix}${quote}***${quote}` : `${prefix}***`,
    )
    .replace(/eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, 'eyJ***')
    .replace(/Bearer\s+[^\s,'"]+/gi, 'Bearer ***')
    .replace(/oauth_token[=:]\s*[^\s&'"]+/gi, 'oauth_token=***')
    .replace(/-----BEGIN[A-Z\s]+PRIVATE\s+KEY-----[\s\S]*?-----END[A-Z\s]+PRIVATE\s+KEY-----/g, '***PRIVATE KEY***')
    // Known secret env-var names and HTTP header names — redact the value regardless of format
    // Covers cases where the value is not a recognized token pattern (e.g. non-JWT AGENT_SECRET_TOKEN)
    .replace(
      /(['"]?\b(?:AGENT_SECRET_TOKEN|ANTHROPIC_API_KEY|ANTHROPIC_AUTH_TOKEN|OPENAI_API_KEY|CODEX_API_KEY|GEMINI_API_KEY|ZAI_API_KEY|DEEPSEEK_API_KEY|CLAUDE_CODE_OAUTH_TOKEN|CALL_DEEPGRAM_API_KEY|CALL_CARTESIA_API_KEY|X-Agent-Token|X-API-Key)\b['"]?\s*[:=]\s*)(?:(['"])(?:[^'"\\]|\\.)+\2|[^\s,'";&\])\n]{4,})/gi,
      '$1***',
    );
}

/**
 * Extract the message from a caught unknown value and sanitize it.
 * Use this at the error-extraction site so every downstream consumer
 * (sendLog, persistAndPostStatus, string interpolation) receives
 * an already-redacted value rather than relying on each call site
 * to remember the outer sanitize() wrap.
 */
export function sanitizeError(e: unknown): string {
  return sanitize(e instanceof Error ? e.message : String(e));
}
