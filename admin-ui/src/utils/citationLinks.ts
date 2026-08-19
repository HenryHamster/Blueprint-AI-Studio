// Recognises the identifier shapes this project's strategist/agents already emit in
// free-text message content — direction ids (`d-xxxxxxxx`), PR references (`#NNNNN`),
// and task ids (full UUID, or an 8-hex short id anchored by the word "task" so a bare
// hex string is never guessed at) — and rewrites them to markdown links using an
// internal `citation://<kind>/<id>` href. MarkdownContentInner's `a` renderer resolves
// that href to an in-app navigation call, or degrades to the original literal text
// when it cannot be resolved (e.g. no PR repo configured). The stored message content
// itself is never mutated — this only runs on the string handed to ReactMarkdown.

export type CitationKind = 'direction' | 'task' | 'pr';

export interface CitationTarget {
  kind: CitationKind;
  id: string;
}

const CITATION_SCHEME = 'citation://';
export const CITATION_URL_PREFIX = CITATION_SCHEME;

export function toCitationHref(kind: CitationKind, id: string): string {
  return `${CITATION_SCHEME}${kind}/${encodeURIComponent(id)}`;
}

export function parseCitationHref(href: string): CitationTarget | null {
  if (!href.startsWith(CITATION_SCHEME)) return null;
  const rest = href.slice(CITATION_SCHEME.length);
  const slash = rest.indexOf('/');
  if (slash < 0) return null;
  const kind = rest.slice(0, slash);
  const id = decodeURIComponent(rest.slice(slash + 1));
  if (!id) return null;
  if (kind === 'direction' || kind === 'task' || kind === 'pr') return { kind, id };
  return null;
}

const UUID_RE = /\b([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\b/g;
const DIRECTION_RE = /\bd-([0-9a-fA-F]{8})\b/g;
const PR_RE = /(?<![\w/])#(\d{2,7})\b/g;
// A bare 8-hex string is ambiguous (could be a git sha, a color, anything) — only
// treat it as a task reference when the word "task" anchors it, mirroring how task
// short ids are already introduced in this app's own UI copy ("Task {id}").
const SHORT_TASK_RE = /\btask[ :]+([0-9a-fA-F]{8})(?!-[0-9a-fA-F])\b/gi;

interface Span {
  start: number;
  end: number;
  replacement: string;
}

function collectSpans(segment: string): Span[] {
  const spans: Span[] = [];
  const push = (m: RegExpExecArray, id: string, kind: CitationKind, label: string) => {
    spans.push({ start: m.index, end: m.index + m[0].length, replacement: `[${label}](${toCitationHref(kind, id)})` });
  };
  let m: RegExpExecArray | null;
  UUID_RE.lastIndex = 0;
  while ((m = UUID_RE.exec(segment))) push(m, m[1], 'task', m[1]);
  DIRECTION_RE.lastIndex = 0;
  while ((m = DIRECTION_RE.exec(segment))) push(m, m[1], 'direction', `d-${m[1]}`);
  PR_RE.lastIndex = 0;
  while ((m = PR_RE.exec(segment))) push(m, m[1], 'pr', `#${m[1]}`);
  SHORT_TASK_RE.lastIndex = 0;
  while ((m = SHORT_TASK_RE.exec(segment))) push(m, m[1], 'task', m[0]);

  spans.sort((a, b) => a.start - b.start || b.end - a.end);
  const deduped: Span[] = [];
  let lastEnd = -1;
  for (const span of spans) {
    if (span.start < lastEnd) continue; // drop overlaps, keep the earlier/longer match
    deduped.push(span);
    lastEnd = span.end;
  }
  return deduped;
}

function linkifySegment(segment: string): string {
  const spans = collectSpans(segment);
  if (spans.length === 0) return segment;
  let out = '';
  let cursor = 0;
  for (const span of spans) {
    out += segment.slice(cursor, span.start);
    out += span.replacement;
    cursor = span.end;
  }
  out += segment.slice(cursor);
  return out;
}

/**
 * Rewrites recognised identifiers to markdown citation links. Fence/code-span aware
 * (mirrors utils/mathContent.ts's normalizeMathDelimiters split) so identifiers inside
 * ```code``` blocks or `inline code` are left untouched, since those are usually shas
 * or literal values, not conversational references.
 */
export function linkifyCitations(content: string): string {
  const parts = content.split(/(```[\s\S]*?```|`[^`\n]+`)/g);
  return parts.map((part, i) => (i % 2 === 1 ? part : linkifySegment(part))).join('');
}
