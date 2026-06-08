import { useState, useEffect, useCallback } from 'react';
import { X, Search, BookOpen, ArrowLeft, ExternalLink, Copy, Check, CheckCircle, XCircle, RefreshCw, FileCode, Globe, HelpCircle, Cpu } from 'lucide-react';
import { ResolvedReferenceLink, isResolvableStorageUri, isSafeUrl } from './TaskCard';
import { MarkdownContent } from './MarkdownContent';
import {
  fetchKnowledgeDocuments,
  fetchKnowledgeDocument,
  type KnowledgeCitation,
  type KnowledgeDocument,
  type KnowledgeForwardQualityVerdict,
} from '../api';
import { parseTimestamp } from '../utils/time';

interface KnowledgeBrowserProps {
  selectedProjectId: string | null;
  onClose: () => void;
  onNavigateToTask?: (taskId: string) => void;
  onLaunchWebResearchTask?: (topic?: string) => void;
}

function relativeDate(dateStr: string | null): string {
  if (!dateStr) return '';
  const diffMs = Date.now() - parseTimestamp(dateStr).getTime();
  const diffMin = Math.floor(diffMs / 60000);
  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

/** A citation can be a bare URL string or an object with ref/url/title/excerpt. */
function citationParts(c: KnowledgeCitation | null | undefined): { reference: string | null; label: string; excerpt?: string } {
  if (typeof c === 'string') return { reference: c, label: c };
  if (!c || typeof c !== 'object') {
    const label = JSON.stringify(c) ?? String(c);
    return { reference: null, label };
  }
  const record = c as Partial<Record<'ref' | 'url' | 'title' | 'excerpt', unknown>>;
  const href = typeof record.url === 'string' ? record.url : typeof record.ref === 'string' ? record.ref : null;
  const label = typeof record.title === 'string' && record.title.length > 0 ? record.title : href ?? JSON.stringify(c) ?? 'Unknown citation';
  const excerpt = typeof record.excerpt === 'string' ? record.excerpt : undefined;
  return { reference: href, label, excerpt };
}

/**
 * Origin badge for a knowledge document, mirroring TaskTypeBadge's static
 * style-map language (slate for design, cyan for web research, neutral for
 * unknown/legacy docs without a recorded source kind).
 */
const SOURCE_KIND_STYLES: Record<string, { bg: string; text: string; icon: typeof FileCode; label: string }> = {
  design: { bg: 'bg-slate-500/10 border-slate-500/20', text: 'text-slate-300', icon: FileCode, label: 'Design' },
  web_research: { bg: 'bg-cyan-500/10 border-cyan-500/20', text: 'text-cyan-300', icon: Globe, label: 'Web Research' },
  compute_experiment: { bg: 'bg-amber-500/10 border-amber-500/20', text: 'text-amber-300', icon: Cpu, label: 'Compute' },
  unknown: { bg: 'bg-neutral-500/10 border-neutral-500/20', text: 'text-neutral-400', icon: HelpCircle, label: 'Unknown' },
};

/** Selectable origins for the filter control (order matters for the segmented control). */
const SOURCE_KIND_OPTIONS = ['design', 'web_research', 'compute_experiment', 'unknown'] as const;

function SourceKindBadge({ kind }: { kind?: string }) {
  if (!kind) return null;
  const style = SOURCE_KIND_STYLES[kind] ?? SOURCE_KIND_STYLES.unknown;
  const Icon = style.icon;
  const label = SOURCE_KIND_STYLES[kind]?.label ?? kind;
  return (
    <span
      className={`${style.bg} ${style.text} border text-[9px] uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-1 px-1.5 py-0.5`}
      title={`Origin: ${label}`}
    >
      <Icon className="w-2.5 h-2.5 shrink-0" />
      {label}
    </span>
  );
}

function VerdictBadge({ verdict }: { verdict: KnowledgeForwardQualityVerdict | null }) {
  if (!verdict) return null;
  const passed = verdict.passed === true;
  const reason = typeof verdict.reason === 'string' ? verdict.reason : undefined;
  const strategy = typeof verdict.strategy === 'string' ? verdict.strategy : undefined;
  return (
    <div className={`rounded border px-2 py-1.5 text-[11px] ${passed ? 'border-emerald-900/50 bg-emerald-950/30' : 'border-red-900/50 bg-red-950/30'}`}>
      <div className="flex items-center gap-1.5 font-medium">
        {passed ? <CheckCircle className="w-3 h-3 text-emerald-400" /> : <XCircle className="w-3 h-3 text-red-400" />}
        <span className={passed ? 'text-emerald-400' : 'text-red-400'}>Forward quality: {passed ? 'passed' : 'failed'}</span>
        {strategy && <span className="text-neutral-500 ml-auto">{strategy}</span>}
      </div>
      {reason && <p className="text-neutral-400 mt-1 leading-snug">{reason}</p>}
    </div>
  );
}

/**
 * Acceptance-probe detail behind the verdict badge: how well the document is
 * grounded in its citations (resolved file references for design, reachable URLs
 * for web_research) and whether it cleared the topic-responsiveness probe.
 * Purely observational — degrades to nothing when no probe detail is persisted
 * (older / un-probed documents lack the whole blob).
 */
function ProbeDetail({ verdict }: { verdict: KnowledgeForwardQualityVerdict | null }) {
  const probes = verdict?.probes;
  if (!probes) return null;
  const grounding = probes.grounding;
  const topic = probes.topic_responsiveness;
  if (!grounding && !topic) return null;

  // A document is weakly grounded when it has citations but few resolve
  // (unresolved file references for design, unreachable URLs for web_research).
  const weaklyGrounded =
    !!grounding && grounding.file_citations > 0 && grounding.grounded < grounding.file_citations;

  return (
    <div className="rounded border border-neutral-800 bg-neutral-900/40 px-2 py-1.5 text-[11px] space-y-1.5">
      <div className="text-[10px] uppercase tracking-wide text-neutral-500">Acceptance probes</div>
      {grounding && grounding.file_citations > 0 && (
        <div className="space-y-1">
          <div className="flex items-center gap-1.5">
            {weaklyGrounded ? <XCircle className="w-3 h-3 text-amber-400 shrink-0" /> : <CheckCircle className="w-3 h-3 text-emerald-400 shrink-0" />}
            <span className={weaklyGrounded ? 'text-amber-400 font-medium' : 'text-neutral-300'}>
              Grounded {grounding.grounded} of {grounding.file_citations} citation{grounding.file_citations === 1 ? '' : 's'}
            </span>
          </div>
          {grounding.unresolved.length > 0 && (
            <div className="ml-[18px] space-y-0.5">
              <div className="text-[10px] text-neutral-500">Unresolved:</div>
              <ul className="space-y-0.5">
                {grounding.unresolved.map((u, i) => (
                  <li key={i} className="font-mono text-[10px] text-amber-300/80 break-all">{u}</li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
      {topic && (
        <div className="flex items-center gap-1.5">
          {topic.passed ? <CheckCircle className="w-3 h-3 text-emerald-400 shrink-0" /> : <XCircle className="w-3 h-3 text-red-400 shrink-0" />}
          <span className={topic.passed ? 'text-neutral-300' : 'text-red-400 font-medium'}>
            Topic responsiveness {topic.passed ? 'passed' : 'failed'}
          </span>
          {topic.total > 0 && (
            <span className="text-neutral-500">({topic.overlap}/{topic.total} keywords)</span>
          )}
        </div>
      )}
    </div>
  );
}

function CopyableId({ id }: { id: string }) {
  const [copied, setCopied] = useState(false);
  const copy = useCallback(() => {
    navigator.clipboard?.writeText(id).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {/* clipboard unavailable */});
  }, [id]);
  return (
    // reason: copy-id affordance in compact detail header; text-[10px] inline control
    <button
      data-viewport-allow-small-target
      onClick={copy}
      title="Copy document id for citation"
      className="inline-flex items-center gap-1 font-mono text-[10px] text-neutral-500 hover:text-neutral-300 transition-colors"
    >
      {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
      {id.slice(0, 8)}…
    </button>
  );
}

const PAGE_SIZE = 50;

export function KnowledgeBrowser({ selectedProjectId, onClose, onNavigateToTask, onLaunchWebResearchTask }: KnowledgeBrowserProps) {
  const [query, setQuery] = useState({
    search: '',
    topicFilter: null as string | null,
    tagFilter: null as string | null,
    directionFilter: null as string | null,
    sourceKindFilter: null as string | null,
    offset: 0,
  });
  const { search, topicFilter, tagFilter, directionFilter, sourceKindFilter, offset } = query;

  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<KnowledgeDocument | null>(null);

  const setFilter = useCallback(<K extends 'search' | 'topicFilter' | 'tagFilter' | 'directionFilter' | 'sourceKindFilter'>(
    key: K,
    value: typeof query[K],
  ) => {
    setQuery(prev => ({ ...prev, [key]: value, offset: 0 }));
  }, [query]);

  const loadMore = useCallback(() => {
    setQuery(prev => ({ ...prev, offset: prev.offset + PAGE_SIZE }));
  }, []);

  // Debounced list/search fetch driven by backend full-text/tag/topic search.
  useEffect(() => {
    if (!selectedProjectId) return;
    let cancelled = false;
    const handle = setTimeout(() => {
      setLoading(true);
      setError(null);
      fetchKnowledgeDocuments(selectedProjectId, {
        q: search.trim() || undefined,
        topic: topicFilter ?? undefined,
        tag: tagFilter ?? undefined,
        direction: directionFilter ?? undefined,
        source_kind: sourceKindFilter ?? undefined,
        limit: PAGE_SIZE,
        offset,
      })
        .then(data => {
          if (cancelled) return;
          if (offset === 0) {
            setDocs(data.documents);
          } else {
            setDocs(prev => [...prev, ...data.documents]);
          }
          setTotal(data.total);
        })
        .catch(() => { if (!cancelled) setError('Failed to load knowledge documents.'); })
        .finally(() => { if (!cancelled) setLoading(false); });
    }, 250);
    return () => { cancelled = true; clearTimeout(handle); };
  }, [selectedProjectId, search, topicFilter, tagFilter, directionFilter, sourceKindFilter, offset]);

  const openDoc = useCallback((doc: KnowledgeDocument) => {
    if (!selectedProjectId) return;
    setSelected(doc); // optimistic (no body yet)
    fetchKnowledgeDocument(selectedProjectId, doc.id)
      .then(full => setSelected(full))
      .catch(() => {/* keep summary view */});
  }, [selectedProjectId]);

  const launchWebResearch = useCallback((topic?: string) => {
    onLaunchWebResearchTask?.(topic);
    onClose();
  }, [onClose, onLaunchWebResearchTask]);

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-full max-w-[600px] bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl">
        {/* Header */}
        <div className="flex justify-between items-center p-4 border-b border-neutral-800 shrink-0">
          <div className="flex items-center gap-2 min-w-0">
            {selected && (
              // reason: back-to-list control in detail header; w-4 h-4 icon matches header density
              <button data-viewport-allow-small-target onClick={() => setSelected(null)} className="text-neutral-500 hover:text-neutral-300 transition-colors shrink-0">
                <ArrowLeft className="w-4 h-4" />
              </button>
            )}
            <div className="min-w-0">
              <h3 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
                <BookOpen className="w-4 h-4 text-violet-400 shrink-0" />
                Knowledge Library
              </h3>
              {!selected && (
                <p className="text-[10px] text-neutral-500 mt-0.5">
                  {docs.length < total ? `showing ${docs.length} of ${total}` : `${total} document${total !== 1 ? 's' : ''}`}
                </p>
              )}
            </div>
          </div>
          {/* reason: icon-only close button in panel header; w-4 h-4 icon matches panel header density */}
          <button data-viewport-allow-small-target onClick={onClose} className="text-neutral-500 hover:text-neutral-300 transition-colors shrink-0">
            <X className="w-4 h-4" />
          </button>
        </div>

        {selected ? (
          /* Detail view */
          <div className="flex-1 overflow-y-auto p-4 space-y-3">
            <div className="space-y-1.5">
              <h2 className="text-base font-semibold text-neutral-100 leading-snug">{selected.title}</h2>
              <div className="flex items-center gap-3 flex-wrap text-[10px] text-neutral-500">
                <CopyableId id={selected.id} />
                <SourceKindBadge kind={selected.source_kind} />
                {selected.authoring_branch && selected.head_sha && (
                  <span className="px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 font-mono">
                    {selected.authoring_branch}@{selected.head_sha}
                  </span>
                )}
                {selected.direction_id && <span className="px-1.5 py-0.5 rounded bg-blue-950/40 text-blue-300">{selected.direction_id}</span>}
                {selected.topic && <span className="px-1.5 py-0.5 rounded bg-violet-950/40 text-violet-300">{selected.topic}</span>}
                {selected.created_at && <span>{relativeDate(selected.created_at)}</span>}
                {selected.source_task_id && onNavigateToTask && (() => {
                  const srcTaskId = selected.source_task_id;
                  return (
                    // reason: provenance task link in compact detail meta row; text-[10px] inline control
                    <button
                      data-viewport-allow-small-target
                      onClick={() => onNavigateToTask(srcTaskId)}
                      className="inline-flex items-center gap-1 text-blue-400 hover:text-blue-300 transition-colors"
                    >
                      <ExternalLink className="w-3 h-3" /> source task
                    </button>
                  );
                })()}
                {selected.topic && onLaunchWebResearchTask && (
                  <button
                    data-viewport-allow-small-target
                    onClick={() => launchWebResearch(selected.topic ?? undefined)}
                    className="inline-flex min-h-[44px] items-center gap-1 text-emerald-400 hover:text-emerald-300 transition-colors"
                  >
                    <RefreshCw className="w-3 h-3" /> refresh research
                  </button>
                )}
              </div>
              {(selected.tags ?? []).length > 0 && (
                <div className="flex flex-wrap gap-1">
                  {selected.tags.map(t => (
                    <span key={t} className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400">#{t}</span>
                  ))}
                </div>
              )}
            </div>

            <VerdictBadge verdict={selected.forward_quality_verdict} />
            <ProbeDetail verdict={selected.forward_quality_verdict} />

            {selected.summary &&<p className="text-xs text-neutral-400 leading-relaxed italic">{selected.summary}</p>}

            {selected.body !== undefined ? (
              <div className="border-t border-neutral-800 pt-3 text-sm text-neutral-200">
                <MarkdownContent content={selected.body} />
              </div>
            ) : (
              <p className="text-xs text-neutral-600">Loading document…</p>
            )}

            {(selected.source_citations ?? []).length > 0 && (
              <div className="border-t border-neutral-800 pt-3">
                <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-1">Citations</div>
                <ul className="space-y-1 text-[11px]">
                  {selected.source_citations.map((c, i) => {
                    const { reference, label, excerpt } = citationParts(c);
                    const resolvableReference = reference && (isSafeUrl(reference) || isResolvableStorageUri(reference))
                      ? reference
                      : null;
                    return (
                      <li key={i} className="leading-snug">
                        {resolvableReference ? (
                          <ResolvedReferenceLink
                            projectId={selectedProjectId}
                            reference={resolvableReference}
                            className="text-blue-400 hover:text-blue-300 break-all text-left"
                          >
                            {label}
                          </ResolvedReferenceLink>
                        ) : (
                          <span className="text-neutral-300 break-all">{label}</span>
                        )}
                        {excerpt && <div className="ml-3 italic text-[10px] text-neutral-500">{excerpt}</div>}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}
          </div>
        ) : (
          /* List view */
          <>
            <div className="px-4 py-2 border-b border-neutral-800/50 shrink-0 space-y-2">
              {(topicFilter || tagFilter || directionFilter) && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  {directionFilter && (
                    <span className="flex items-center gap-1 text-[10px] bg-blue-950/40 border border-blue-900/50 text-blue-300 px-2 py-0.5 rounded">
                      direction: {directionFilter}
                      {/* reason: clear-direction-filter chip control; w-2.5 h-2.5 icon inline in chip */}
                      <button data-viewport-allow-small-target onClick={() => setFilter('directionFilter', null)} className="text-blue-500 hover:text-blue-200"><X className="w-2.5 h-2.5" /></button>
                    </span>
                  )}
                  {topicFilter && (
                    <span className="flex items-center gap-1 text-[10px] bg-violet-950/40 border border-violet-900/50 text-violet-300 px-2 py-0.5 rounded">
                      topic: {topicFilter}
                      {/* reason: clear-topic-filter chip control; w-2.5 h-2.5 icon inline in chip */}
                      <button data-viewport-allow-small-target onClick={() => setFilter('topicFilter', null)} className="text-violet-500 hover:text-violet-200"><X className="w-2.5 h-2.5" /></button>
                    </span>
                  )}
                  {tagFilter && (
                    <span className="flex items-center gap-1 text-[10px] bg-neutral-800 border border-neutral-700 text-neutral-300 px-2 py-0.5 rounded">
                      #{tagFilter}
                      {/* reason: clear-tag-filter chip control; w-2.5 h-2.5 icon inline in chip */}
                      <button data-viewport-allow-small-target onClick={() => setFilter('tagFilter', null)} className="text-neutral-500 hover:text-neutral-200"><X className="w-2.5 h-2.5" /></button>
                    </span>
                  )}
                </div>
              )}
              <div className="relative">
                <Search className="w-3.5 h-3.5 text-neutral-500 absolute left-2.5 top-1/2 -translate-y-1/2" />
                <input
                  type="text"
                  value={search}
                  onChange={e => setFilter('search', e.target.value)}
                  placeholder="Search title, summary, body, topic, tags..."
                  className="w-full bg-neutral-900 border border-neutral-800 rounded pl-8 pr-3 py-1.5 text-xs text-neutral-200 placeholder:text-neutral-600 focus:outline-none focus:ring-1 focus:ring-neutral-700"
                />
              </div>
              {/* Source-kind (origin) filter — scope the library to a single producing task type. */}
              <div className="flex items-center gap-1 flex-wrap">
                <span className="text-[9px] uppercase tracking-wide text-neutral-600 mr-0.5">origin</span>
                {/* reason: origin filter segment; text-[9px] inline control matching card chip density */}
                <button
                  data-viewport-allow-small-target
                  onClick={() => setFilter('sourceKindFilter', null)}
                  className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded border transition-colors ${sourceKindFilter === null ? 'bg-neutral-700 border-neutral-600 text-neutral-100' : 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300'}`}
                >All</button>
                {SOURCE_KIND_OPTIONS.map(kind => {
                  const style = SOURCE_KIND_STYLES[kind];
                  const active = sourceKindFilter === kind;
                  const Icon = style.icon;
                  return (
                    // reason: origin filter segment; text-[9px] inline control matching card chip density
                    <button
                      data-viewport-allow-small-target
                      key={kind}
                      onClick={() => setFilter('sourceKindFilter', active ? null : kind)}
                      className={`text-[9px] uppercase tracking-wider px-1.5 py-0.5 rounded border inline-flex items-center gap-1 transition-colors ${active ? `${style.bg} ${style.text} border-current` : 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300'}`}
                    >
                      <Icon className="w-2.5 h-2.5 shrink-0" />
                      {style.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-2">
              {loading && docs.length === 0 && <div className="text-neutral-600 text-center py-12 text-sm" data-testid="loading-initial">Loading…</div>}
              {error && <div className="text-red-400 text-center py-12 text-sm">{error}</div>}
              {!loading && !error && docs.length === 0 && (
                <div className="text-neutral-600 text-center py-12 text-sm space-y-3">
                  <div>No knowledge documents{search.trim() ? ' match your search' : ' yet'}.</div>
                  {onLaunchWebResearchTask && (
                    <button
                      data-viewport-allow-small-target
                      onClick={() => launchWebResearch(search.trim() || undefined)}
                      className="inline-flex min-h-[44px] items-center justify-center rounded-lg border border-emerald-900/60 bg-emerald-950/30 px-3 py-2 text-xs font-medium text-emerald-300 hover:bg-emerald-950/50 hover:border-emerald-800 transition-colors"
                    >
                      {search.trim() ? `Launch web research for "${search.trim()}"` : 'Start web research'}
                    </button>
                  )}
                </div>
              )}
              {docs.map(doc => (
                // reason: document card opener in compact results list; px-3 py-2.5 row matches list density
                <div
                  key={doc.id}
                  role="button"
                  tabIndex={0}
                  data-viewport-allow-small-target
                  onClick={() => openDoc(doc)}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      openDoc(doc);
                    }
                  }}
                  className="w-full cursor-pointer text-left bg-neutral-900/50 border border-neutral-800 rounded-lg px-3 py-2.5 hover:bg-neutral-800/40 transition-colors"
                >
                  <div className="flex items-center gap-2">
                    <span className="text-xs font-medium text-neutral-200 flex-1 truncate">{doc.title}</span>
                    {doc.forward_quality_verdict?.passed === true && <CheckCircle className="w-3 h-3 text-emerald-400 shrink-0" />}
                    <span className="text-[10px] text-neutral-600 shrink-0">{relativeDate(doc.created_at)}</span>
                  </div>
                  {doc.summary && <p className="text-[11px] text-neutral-500 mt-1 line-clamp-2 leading-snug">{doc.summary}</p>}
                  <div className="flex items-center gap-1.5 flex-wrap mt-1.5">
                    <SourceKindBadge kind={doc.source_kind} />
                    {doc.direction_id && (
                      // reason: direction filter chip in compact card footer; text-[9px] inline control
                      <button
                        data-viewport-allow-small-target
                        onClick={e => { e.stopPropagation(); setFilter('directionFilter', doc.direction_id); }}
                        className="text-[9px] px-1.5 py-0.5 rounded bg-blue-950/40 text-blue-300 hover:bg-blue-900/40 transition-colors"
                      >{doc.direction_id}</button>
                    )}
                    {doc.topic && (
                      // reason: topic filter chip in compact card footer; text-[9px] inline control
                      <button
                        data-viewport-allow-small-target
                        onClick={e => { e.stopPropagation(); setFilter('topicFilter', doc.topic ?? null); }}
                        className="text-[9px] px-1.5 py-0.5 rounded bg-violet-950/40 text-violet-300 hover:bg-violet-900/40 transition-colors"
                      >{doc.topic}</button>
                    )}
                    {(doc.tags ?? []).slice(0, 4).map(t => (
                      // reason: tag filter chip in compact card footer; text-[9px] inline control
                    <button
                      data-viewport-allow-small-target
                      key={t}
                      onClick={e => { e.stopPropagation(); setFilter('tagFilter', t); }}
                      className="text-[9px] px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 hover:bg-neutral-700 transition-colors"
                    >#{t}</button>
                  ))}
                </div>
                </div>
              ))}
              {docs.length < total && !loading && (
                <button
                  onClick={loadMore}
                  className="w-full min-h-[44px] text-center py-2 text-xs text-neutral-400 hover:text-neutral-200 border border-neutral-800 rounded-lg transition-colors"
                >
                  Load more
                </button>
              )}
              {loading && docs.length > 0 && (
                <div className="text-neutral-600 text-center py-4 text-sm">Loading…</div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}
