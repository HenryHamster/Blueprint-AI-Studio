export function sanitize(text: string): string {
  return text
    .replace(/https:\/\/x-access-token:[^@]+@/g, 'https://x-access-token:***@')
    .replace(/ghp_[A-Za-z0-9_]+/g, 'ghp_***')
    .replace(/ghs_[A-Za-z0-9_]+/g, 'ghs_***')
    .replace(/github_pat_[A-Za-z0-9_]+/g, 'github_pat_***')
    .replace(/sk-ant-[A-Za-z0-9_-]+/g, 'sk-ant-***')
    .replace(/eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+/g, 'eyJ***')
    .replace(/Bearer\s+[^\s,'"]+/gi, 'Bearer ***')
    .replace(/oauth_token[=:]\s*[^\s&'"]+/gi, 'oauth_token=***')
    .replace(/-----BEGIN[A-Z\s]+PRIVATE\s+KEY-----[\s\S]*?-----END[A-Z\s]+PRIVATE\s+KEY-----/g, '***PRIVATE KEY***');
}
