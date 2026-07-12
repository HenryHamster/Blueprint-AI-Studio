import { useState, useEffect, useCallback } from 'react';
import { X, Search, BookOpen, ArrowLeft, ExternalLink, Copy, Check, CheckCircle, XCircle, RefreshCw, FileCode, Globe, HelpCircle, Cpu, Shield, ShieldCheck, ShieldAlert, AlertTriangle, Download } from 'lucide-react';
import { ResolvedReferenceLink, isResolvableStorageUri, isSafeUrl } from './TaskCard';
import { MarkdownContent } from './MarkdownContent';
import { ReleaseEvidencePanel } from './ReleaseEvidence';
import { AcceptanceGradeBadge } from './AcceptanceGradeBadge';
import { CriterionVerdictBadge } from './CriterionVerdictBadge';
import { EvidenceTierBadge } from './EvidenceTierBadge';
import { computeJobExecutionLocation } from './ComputeJobsPanel.utils';
import {
  fetchKnowledgeDocuments,
  fetchKnowledgeDocument,
  resolveArtifactDownloadUrl,
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
  initialDirectionId?: string;
  initialDocumentId?: string;
}

interface KnowledgeQuery {
  search: string;
  topicFilter: string | null;
  tagFilter: string | null;
  directionFilter: string | null;
  sourceKindFilter: string | null;
  challengeStateFilter: string | null;
  lifecycleFilter: string | null;
  sweepFilter: string | null;
  offset: number;
  routedDirectionId: string | null;
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
      className={`${style.bg} ${style.text} border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-1 px-1.5 py-0.5`}
      title={`Origin: ${label}`}
    >
      <Icon className="w-2.5 h-2.5 shrink-0" />
      {label}
    </span>
  );
}


/**
 * Origin-challenge badge for a knowledge document spawned from a research finding.
 * The backend (app.knowledge_query.serialize_origin_provenance) records whether the
 * producing design / web_research task came from a research finding and the
 * second-opinion decision that allowed or rejected its dispatch. We surface the
 * resolved challenge outcome — confirmed, overturned, indeterminate/fail-closed, or
 * unchallenged — so an operator can tell at a glance whether a finding-derived
 * document survived an independent adversarial pass.
 *
 * Renders nothing for documents that are not research-finding-derived
 * (`not_research_finding_derived` / `legacy_unknown`) or whose backend response
 * predates the provenance contract (`origin_provenance` absent), so legacy documents
 * stay clean in the list. The detail view still surfaces those states via
 * OriginProvenanceDetail below.
 */
const CHALLENGE_STATE_STYLES: Record<string, { bg: string; text: string; icon: typeof Shield; label: string; tooltip: string }> = {
  unchallenged: { bg: 'bg-slate-500/10 border-slate-500/20', text: 'text-slate-300', icon: Shield, label: 'Unchallenged', tooltip: 'Spawned from a research finding that was not independently challenged.' },
  challenger_confirmed: { bg: 'bg-emerald-500/10 border-emerald-500/20', text: 'text-emerald-300', icon: ShieldCheck, label: 'Confirmed', tooltip: 'An independent second opinion agreed with the originating research finding.' },
  overturned: { bg: 'bg-red-500/10 border-red-500/20', text: 'text-red-300', icon: ShieldAlert, label: 'Overturned', tooltip: 'An independent second opinion disagreed with the originating research finding.' },
  indeterminate_fail_closed: { bg: 'bg-amber-500/10 border-amber-500/20', text: 'text-amber-300', icon: AlertTriangle, label: 'Indeterminate', tooltip: 'The second opinion was indeterminate; dispatch was allowed fail-closed.' },
};

/** Selectable challenge states for the bounded filter control (order matters). */
const CHALLENGE_STATE_OPTIONS = ['unchallenged', 'challenger_confirmed', 'overturned', 'indeterminate_fail_closed'] as const;

/**
 * Lifecycle-status badge for the strategist follow-up workflow.
 * New documents start as "pending" (needs attention), the strategist marks
 * them "consumed" (read + acted on) or "dismissed" (not actionable).
 */
const LIFECYCLE_STYLES: Record<string, { bg: string; text: string; label: string; tooltip: string }> = {
  pending: { bg: 'bg-amber-500/10 border-amber-500/20', text: 'text-amber-300', label: 'Pending', tooltip: 'New document awaiting strategist review.' },
  consumed: { bg: 'bg-emerald-500/10 border-emerald-500/20', text: 'text-emerald-300', label: 'Consumed', tooltip: 'Read and acted on by the strategist.' },
  dismissed: { bg: 'bg-slate-500/10 border-slate-500/20', text: 'text-slate-400', label: 'Dismissed', tooltip: 'Not actionable — dismissed by the strategist.' },
};

/** Selectable lifecycle states for the filter control (order matters). */
const LIFECYCLE_OPTIONS = ['pending', 'consumed', 'dismissed'] as const;

function LifecycleBadge({ status }: { status?: string | null }) {
  if (!status || status === 'pending') return null; // pending is the default; don't clutter display
  const style = LIFECYCLE_STYLES[status] ?? LIFECYCLE_STYLES.pending;
  return (
    <span
      className={`${style.bg} ${style.text} border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-1 px-1.5 py-0.5`}
      title={style.tooltip}
    >
      {style.label}
    </span>
  );
}

/** Human-readable labels for the origin-state axis surfaced in the detail block. */
const ORIGIN_STATE_LABELS: Record<string, string> = {
  research_finding: 'Research finding',
  not_research_finding_derived: 'Not research-finding derived',
  legacy_unknown: 'Legacy / unknown (predates provenance tracking)',
};

function OriginBadge({ doc }: { doc: Pick<KnowledgeDocument, 'origin_provenance'> }) {
  const prov = doc.origin_provenance;
  if (!prov || prov.origin_state !== 'research_finding') return null;
  const state = typeof prov.challenge_state === 'string' ? prov.challenge_state : 'unchallenged';
  const style = CHALLENGE_STATE_STYLES[state] ?? CHALLENGE_STATE_STYLES.unchallenged;
  const Icon = style.icon;
  return (
    <span
      className={`${style.bg} ${style.text} border text-ui-xs uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-1 px-1.5 py-0.5`}
      title={style.tooltip}
    >
      <Icon className="w-2.5 h-2.5 shrink-0" />
      {style.label}
    </span>
  );
}

/**
 * Detail-view block exposing the source research task / finding identity and the
 * second-opinion decision record for a knowledge document, derived from the backend
 * origin provenance. Renders whenever `origin_provenance` is present (so legacy /
 * not-finding-derived documents surface their state explicitly), and degrades to a
 * single state line when no finding identity or challenge record was recorded.
 * Renders nothing when the backend response omits provenance entirely (older
 * backends), keeping the existing detail view intact.
 */
function OriginProvenanceDetail({
  doc,
  onNavigateToTask,
}: {
  doc: Pick<KnowledgeDocument, 'origin_provenance'>;
  onNavigateToTask?: (taskId: string) => void;
}) {
  const prov = doc.origin_provenance;
  if (!prov) return null;
  const originState = typeof prov.origin_state === 'string' ? prov.origin_state : undefined;
  const stateLabel = (originState ? ORIGIN_STATE_LABELS[originState] : undefined) ?? originState ?? 'Unknown';
  const researchTaskId = typeof prov.research_task_id === 'string' ? prov.research_task_id : null;
  const parentTaskId = typeof prov.parent_research_task_id === 'string' ? prov.parent_research_task_id : null;
  const findingHash = typeof prov.finding_hash === 'string' ? prov.finding_hash : null;
  const findingIndex = typeof prov.finding_index === 'number' ? prov.finding_index : null;
  const challenge = prov.finding_challenge && typeof prov.finding_challenge === 'object' ? prov.finding_challenge : null;
  const hasIdentity = !!(researchTaskId || parentTaskId || findingHash || findingIndex !== null);
  const sidecarTaskId = challenge && typeof challenge.sidecar_task_id === 'string' ? challenge.sidecar_task_id : null;

  return (
    <div className="rounded border border-neutral-800 bg-neutral-900/40 px-2 py-1.5 text-ui-md space-y-1.5">
      <div className="flex items-center gap-1.5 flex-wrap">
        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Finding origin</div>
        <OriginBadge doc={doc} />
      </div>
      <div className="text-neutral-300">
        <span className="text-neutral-500">State:</span> {stateLabel}
      </div>
      {hasIdentity && (
        <div className="space-y-0.5">
          {researchTaskId && (
            onNavigateToTask ? (
              // reason: research-task provenance link in compact detail block; text-ui-md inline control
              <button
                data-viewport-allow-small-target
                onClick={() => onNavigateToTask(researchTaskId)}
                className="inline-flex items-center gap-1 text-blue-400 hover:text-blue-300 transition-colors"
              >
                <ExternalLink className="w-3 h-3" /> research task
                <span className="font-mono text-ui-sm text-neutral-500">{researchTaskId.slice(0, 8)}…</span>
              </button>
            ) : (
              <div className="text-neutral-400 break-all"><span className="text-neutral-500">Research task:</span> <span className="font-mono">{researchTaskId}</span></div>
            )
          )}
          {parentTaskId && parentTaskId !== researchTaskId && (
            <div className="text-neutral-400 break-all"><span className="text-neutral-500">Parent research task:</span> <span className="font-mono">{parentTaskId}</span></div>
          )}
          {findingIndex !== null && (
            <div className="text-neutral-400"><span className="text-neutral-500">Finding index:</span> <span className="font-mono">#{findingIndex}</span></div>
          )}
          {findingHash && (
            <div className="text-neutral-400 break-all"><span className="text-neutral-500">Finding hash:</span> <span className="font-mono">{findingHash}</span></div>
          )}
        </div>
      )}
      {challenge && (
        <div className="border-t border-neutral-800/70 pt-1.5 space-y-0.5">
          <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Second opinion</div>
          {typeof challenge.challenged_boundary === 'string' && challenge.challenged_boundary && (
            <div className="text-neutral-400 break-all"><span className="text-neutral-500">Boundary:</span> {challenge.challenged_boundary}</div>
          )}
          {(typeof challenge.first_verdict === 'string' || typeof challenge.second_opinion_verdict === 'string') && (
            <div className="text-neutral-400">
              <span className="text-neutral-500">Verdict:</span>{' '}
              <span className="font-mono">{typeof challenge.first_verdict === 'string' ? challenge.first_verdict : '—'}</span>
              {' → '}
              <span className="font-mono">{typeof challenge.second_opinion_verdict === 'string' ? challenge.second_opinion_verdict : '—'}</span>
            </div>
          )}
          {typeof challenge.agreement === 'string' && challenge.agreement && (
            <div className="text-neutral-400"><span className="text-neutral-500">Agreement:</span> {challenge.agreement}</div>
          )}
          {typeof challenge.reason === 'string' && challenge.reason && (
            <p className="text-neutral-400 leading-snug break-words">{challenge.reason}</p>
          )}
          {sidecarTaskId && (
            onNavigateToTask ? (
              // reason: challenge-task provenance link in compact detail block; text-ui-md inline control
              <button
                data-viewport-allow-small-target
                onClick={() => onNavigateToTask(sidecarTaskId)}
                className="inline-flex items-center gap-1 text-blue-400 hover:text-blue-300 transition-colors"
              >
                <ExternalLink className="w-3 h-3" /> challenge task
                <span className="font-mono text-ui-sm text-neutral-500">{sidecarTaskId.slice(0, 8)}…</span>
              </button>
            ) : (
              <div className="text-neutral-400 break-all"><span className="text-neutral-500">Challenge task:</span> <span className="font-mono">{sidecarTaskId}</span></div>
            )
          )}
        </div>
      )}
    </div>
  );
}

function VerdictBadge({ verdict }: { verdict: KnowledgeForwardQualityVerdict | null }) {
  if (!verdict) return null;
  const passed = verdict.passed === true;
  const reason = typeof verdict.reason === 'string' ? verdict.reason : undefined;
  const strategy = typeof verdict.strategy === 'string' ? verdict.strategy : undefined;
  return (
    <div className={`rounded border px-2 py-1.5 text-ui-md ${passed ? 'border-emerald-900/50 bg-emerald-950/30' : 'border-red-900/50 bg-red-950/30'}`}>
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
    <div className="rounded border border-neutral-800 bg-neutral-900/40 px-2 py-1.5 text-ui-md space-y-1.5">
      <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Acceptance probes</div>
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
              <div className="text-ui-sm text-neutral-500">Unresolved:</div>
              <ul className="space-y-0.5">
                {grounding.unresolved.map((u, i) => (
                  <li key={i} className="font-mono text-ui-sm text-amber-300/80 break-all">{u}</li>
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

/**
 * Execution provenance panel for compute_experiment knowledge documents.
 * Reads terminal_diagnostics from the forward_quality_verdict (stored by the
 * backend when the compute job has a terminal_reason) and surfaces where the
 * run executed and the evidence tier (earned from criterion_met, not hardcoded).
 */
function ComputeRunProvenanceDetail({
  doc,
}: {
  doc: Pick<KnowledgeDocument, 'source_kind' | 'forward_quality_verdict' | 'evidence_tier'>;
}) {
  if (doc.source_kind !== 'compute_experiment') return null;
  const v = doc.forward_quality_verdict;
  const tdRaw = v && typeof v.terminal_diagnostics === 'object' && v.terminal_diagnostics !== null
    ? v.terminal_diagnostics as Record<string, unknown>
    : null;
  const backend = typeof tdRaw?.executing_backend === 'string' ? tdRaw.executing_backend : null;
  const runnerId = typeof tdRaw?.runner_target_id === 'string' ? tdRaw.runner_target_id : null;
  const runnerHostname = typeof tdRaw?.runner_hostname === 'string' ? tdRaw.runner_hostname : null;
  if (!backend && !runnerId && !runnerHostname) return null;
  const location = backend ? computeJobExecutionLocation(backend) : null;

  return (
    <div className="rounded border border-neutral-800 bg-neutral-900/40 px-2 py-1.5 text-ui-md space-y-1">
      <div className="flex items-center gap-1.5 flex-wrap">
        <div className="text-ui-sm uppercase tracking-wide text-neutral-500">Run provenance</div>
        <EvidenceTierBadge tier={doc.evidence_tier} />
      </div>
      {location && (
        <div className="flex items-center gap-1.5">
          <Cpu className="w-3 h-3 shrink-0 text-neutral-400" />
          <span className={location.isOperatorHardware ? 'text-amber-300' : 'text-sky-300'}>{location.label}</span>
        </div>
      )}
      {(runnerHostname ?? runnerId) && (
        <div className="text-neutral-400 text-ui-sm font-mono">
          <span className="text-neutral-500">runner: </span>
          {runnerHostname ?? (runnerId ? runnerId.slice(0, 8) + '…' : null)}
        </div>
      )}
    </div>
  );
}

/** Compact chip showing execution location (operator hardware vs cloud) for compute_experiment list cards. */
function ExecutionLocationBadge({
  doc,
}: {
  doc: Pick<KnowledgeDocument, 'source_kind' | 'forward_quality_verdict'>;
}) {
  if (doc.source_kind !== 'compute_experiment') return null;
  const v = doc.forward_quality_verdict;
  const tdRaw = v && typeof v.terminal_diagnostics === 'object' && v.terminal_diagnostics !== null
    ? v.terminal_diagnostics as Record<string, unknown>
    : null;
  const backend = typeof tdRaw?.executing_backend === 'string' ? tdRaw.executing_backend : null;
  if (!backend) return null;
  const location = computeJobExecutionLocation(backend);
  return (
    <span className={`text-ui-xs px-1.5 py-0.5 rounded font-medium ${location.isOperatorHardware ? 'bg-amber-950/50 text-amber-300' : 'bg-sky-950/50 text-sky-300'}`}>
      {location.label}
    </span>
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
    // reason: copy-id affordance in compact detail header; text-ui-sm inline control
    <button
      data-viewport-allow-small-target
      onClick={copy}
      title="Copy document id for citation"
      className="inline-flex items-center gap-1 font-mono text-ui-sm text-neutral-500 hover:text-neutral-300 transition-colors"
    >
      {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
      {id.slice(0, 8)}…
    </button>
  );
}

function ExportMarkdown({ body }: { body?: string }) {
  const [copied, setCopied] = useState(false);
  const export_text = useCallback(() => {
    const markdown = body || '';
    navigator.clipboard?.writeText(markdown).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {/* clipboard unavailable */});
  }, [body]);
  if (!body) return null;
  return (
    <button
      data-viewport-allow-small-target
      onClick={export_text}
      title="Copy markdown to clipboard"
      className="inline-flex items-center gap-1 text-ui-sm text-neutral-500 hover:text-neutral-300 transition-colors"
    >
      {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Download className="w-3 h-3" />}
      export
    </button>
  );
}

const PAGE_SIZE = 50;

/** File extensions we can render inline as images or videos. NO svg — an SVG
 *  opened from a signed URL is a full document context and can execute script. */
const VIEWABLE_IMAGE_EXTS = ['.png', '.jpg', '.jpeg', '.gif', '.webp'];
const VIEWABLE_VIDEO_EXTS = ['.mp4', '.webm'];

function viewableMediaKind(uri: string | null | undefined): 'image' | 'video' | null {
  if (!uri) return null;
  const lower = uri.toLowerCase();
  if (VIEWABLE_IMAGE_EXTS.some(ext => lower.endsWith(ext))) return 'image';
  if (VIEWABLE_VIDEO_EXTS.some(ext => lower.endsWith(ext))) return 'video';
  return null;
}

/**
 * Renders a single viewable-media citation (image or video) inline after
 * resolving its `gs://` storage URI to a signed HTTPS URL. Shows a loading
 * indicator while resolving, and falls back to a plain download link on error.
 */
function ViewableMediaInline({
  projectId,
  storageUri,
  label,
  kind,
}: {
  projectId: string;
  storageUri: string;
  label: string;
  kind: 'image' | 'video';
}) {
  const [signedUrl, setSignedUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    resolveArtifactDownloadUrl(projectId, storageUri)
      .then(url => { if (!cancelled) setSignedUrl(url); })
      .catch(() => { if (!cancelled) setError('Failed to load preview.'); });
    return () => { cancelled = true; };
  }, [projectId, storageUri]);

  if (error) {
    return (
      <a
        href={storageUri}
        target="_blank"
        rel="noopener noreferrer"
        className="text-blue-400 hover:text-blue-300 break-all text-ui-sm"
      >
        {label} — {error} (open directly)
      </a>
    );
  }

  if (!signedUrl) {
    return <div className="text-ui-sm text-neutral-600">Loading preview…</div>;
  }

  if (kind === 'video') {
    return (
      <video controls className="max-h-48 w-full rounded border border-neutral-800 bg-black" aria-label={label}>
        <source src={signedUrl} />
        Your browser does not support the video element.
      </video>
    );
  }

  return (
    <img
      src={signedUrl}
      alt={label}
      className="max-h-48 w-full rounded border border-neutral-800 bg-neutral-950/45 object-contain"
    />
  );
}

export function KnowledgeBrowser({ selectedProjectId, onClose, onNavigateToTask, onLaunchWebResearchTask, initialDirectionId, initialDocumentId }: KnowledgeBrowserProps) {
  const routedDirectionId = initialDirectionId ?? null;
  const [query, setQuery] = useState<KnowledgeQuery>({
    search: '',
    topicFilter: null,
    tagFilter: null,
    directionFilter: routedDirectionId,
    sourceKindFilter: null,
    challengeStateFilter: null,
    lifecycleFilter: null,
    sweepFilter: null,
    offset: 0,
    routedDirectionId,
  });
  const effectiveQuery = query.routedDirectionId === routedDirectionId
    ? query
    : { ...query, directionFilter: routedDirectionId, offset: 0, routedDirectionId };
  const { search, topicFilter, tagFilter, directionFilter, sourceKindFilter, challengeStateFilter, lifecycleFilter, sweepFilter, offset } = effectiveQuery;

  const [docs, setDocs] = useState<KnowledgeDocument[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<KnowledgeDocument | null>(null);
  const [docError, setDocError] = useState<string | null>(null);

  const syncRoute = useCallback((prev: KnowledgeQuery): KnowledgeQuery => (
    prev.routedDirectionId === routedDirectionId
      ? prev
      : { ...prev, directionFilter: routedDirectionId, offset: 0, routedDirectionId }
  ), [routedDirectionId]);

  const setFilter = useCallback(<K extends 'search' | 'topicFilter' | 'tagFilter' | 'directionFilter' | 'sourceKindFilter' | 'challengeStateFilter' | 'lifecycleFilter' | 'sweepFilter'>(
    key: K,
    value: KnowledgeQuery[K],
  ) => {
    setQuery(prev => ({ ...syncRoute(prev), [key]: value, offset: 0 }));
  }, [syncRoute]);

  const loadMore = useCallback(() => {
    setQuery(prev => {
      const current = syncRoute(prev);
      return { ...current, offset: current.offset + PAGE_SIZE };
    });
  }, [syncRoute]);

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
        challenge_state: challengeStateFilter ?? undefined,
        lifecycle_status: lifecycleFilter ?? undefined,
        sweep: sweepFilter ?? undefined,
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
  }, [selectedProjectId, search, topicFilter, tagFilter, directionFilter, sourceKindFilter, challengeStateFilter, lifecycleFilter, sweepFilter, offset]);

  const openDoc = useCallback((doc: KnowledgeDocument) => {
    if (!selectedProjectId) return;
    setSelected(doc); // optimistic (no body yet)
    setDocError(null);
    fetchKnowledgeDocument(selectedProjectId, doc.id)
      .then(full => setSelected(full))
      .catch(() => { setDocError('Failed to load document body.'); });
  }, [selectedProjectId]);

  // When a specific document id is provided (e.g. from the compute jobs panel),
  // open it directly in the detail view once the panel mounts or the project changes.
  useEffect(() => {
    if (!initialDocumentId || !selectedProjectId) return;
    let cancelled = false;
    fetchKnowledgeDocument(selectedProjectId, initialDocumentId)
      .then(full => { if (!cancelled) { setDocError(null); setSelected(full); } })
      .catch(() => { if (!cancelled) setDocError('Failed to load document body.'); });
    return () => { cancelled = true; };
  }, [initialDocumentId, selectedProjectId]);

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
                Knowledge library
              </h3>
              {!selected && (
                <p className="text-ui-sm text-neutral-500 mt-0.5">
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
              <div className="flex items-center gap-3 flex-wrap text-ui-sm text-neutral-500">
                <CopyableId id={selected.id} />
                <ExportMarkdown body={selected.body} />
                <SourceKindBadge kind={selected.source_kind} />
                <CriterionVerdictBadge doc={selected} />
                <AcceptanceGradeBadge doc={selected} />
                <EvidenceTierBadge tier={selected.evidence_tier} />
                <OriginBadge doc={selected} />
                <LifecycleBadge status={selected.lifecycle_status} />
                {selected.authoring_branch && selected.head_sha && (
                  <span className="px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 font-mono">
                    {selected.authoring_branch}@{selected.head_sha}
                  </span>
                )}
                {selected.direction_id && <span className="px-1.5 py-0.5 rounded bg-blue-950/40 text-blue-300">{selected.direction_id}</span>}
                {selected.topic && <span className="px-1.5 py-0.5 rounded bg-violet-950/40 text-violet-300">{selected.topic}</span>}
                {selected.sweep_id && (
                  // reason: sweep filter chip in compact detail meta row; text-ui-xs inline control
                  <button
                    data-viewport-allow-small-target
                    onClick={e => { e.stopPropagation(); setFilter('sweepFilter', selected.sweep_id ?? null); }}
                    className="text-ui-xs px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-300 hover:bg-amber-900/40 transition-colors font-mono"
                    title={`Sweep ${selected.sweep_id}${selected.arm_label ? ` / ${selected.arm_label}` : ' (aggregate)'}`}
                  >{selected.sweep_id.slice(0, 8)}…{selected.arm_label ? ` / ${selected.arm_label}` : ' agg'}</button>
                )}
                {selected.created_at && <span>{relativeDate(selected.created_at)}</span>}
                {selected.source_task_id && onNavigateToTask && (() => {
                  const srcTaskId = selected.source_task_id;
                  return (
                    // reason: provenance task link in compact detail meta row; text-ui-sm inline control
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
                    <span key={t} className="text-ui-sm px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400">#{t}</span>
                  ))}
                </div>
              )}
            </div>

            {selected.source_kind === 'compute_experiment' && (
              <ReleaseEvidencePanel evidence={selected.release_evidence} />
            )}

            <ComputeRunProvenanceDetail doc={selected} />
            <VerdictBadge verdict={selected.forward_quality_verdict} />
            <ProbeDetail verdict={selected.forward_quality_verdict} />
            <OriginProvenanceDetail doc={selected} onNavigateToTask={onNavigateToTask} />

            {selected.summary &&<p className="text-xs text-neutral-400 leading-relaxed italic">{selected.summary}</p>}

            {docError && (
              <p data-testid="doc-error" className="text-xs text-red-400">{docError}</p>
            )}
            {selected.body !== undefined ? (
              <div className="border-t border-neutral-800 pt-3 text-sm text-neutral-200">
                <MarkdownContent content={selected.body} />
              </div>
            ) : (
              !docError && <p className="text-xs text-neutral-600">Loading document…</p>
            )}

            {(selected.source_citations ?? []).length > 0 && (
              <div className="border-t border-neutral-800 pt-3">
                <div className="text-ui-sm uppercase tracking-wide text-neutral-500 mb-1">Citations</div>
                <ul className="space-y-1 text-ui-md">
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
                        {excerpt && <div className="ml-3 italic text-ui-sm text-neutral-500">{excerpt}</div>}
                      </li>
                    );
                  })}
                </ul>
              </div>
            )}

            {/* Inline viewable media artifacts — image/video citations rendered inline
                instead of as click-to-download links. Grouped by kind for visual clarity. */}
            {selectedProjectId && (selected.source_citations ?? []).length > 0 && (() => {
              const viewableItems: Array<{ reference: string; label: string; kind: 'image' | 'video' }> = [];
              for (const c of selected.source_citations) {
                const { reference, label } = citationParts(c);
                if (!reference) continue;
                const kind = viewableMediaKind(reference);
                if (kind) viewableItems.push({ reference, label, kind });
              }
              if (viewableItems.length === 0) return null;
              return (
                <div className="border-t border-neutral-800 pt-3">
                  <div className="text-ui-sm uppercase tracking-wide text-neutral-500 mb-2">Artifacts</div>
                  <div className="space-y-2">
                    {viewableItems.map((item, i) => (
                      <ViewableMediaInline
                        key={i}
                        projectId={selectedProjectId}
                        storageUri={item.reference}
                        label={item.label}
                        kind={item.kind}
                      />
                    ))}
                  </div>
                </div>
              );
            })()}

          </div>
        ) : (
          /* List view */
          <>
            <div className="px-4 py-2 border-b border-neutral-800/50 shrink-0 space-y-2">
              {(topicFilter || tagFilter || directionFilter || sweepFilter || lifecycleFilter) && (
                <div className="flex items-center gap-1.5 flex-wrap">
                  {directionFilter && (
                    <span className="flex items-center gap-1 text-ui-sm bg-blue-950/40 border border-blue-900/50 text-blue-300 px-2 py-0.5 rounded">
                      direction: {directionFilter}
                      {/* reason: clear-direction-filter chip control; w-2.5 h-2.5 icon inline in chip */}
                      <button data-viewport-allow-small-target onClick={() => setFilter('directionFilter', null)} className="text-blue-500 hover:text-blue-200"><X className="w-2.5 h-2.5" /></button>
                    </span>
                  )}
                  {topicFilter && (
                    <span className="flex items-center gap-1 text-ui-sm bg-violet-950/40 border border-violet-900/50 text-violet-300 px-2 py-0.5 rounded">
                      topic: {topicFilter}
                      {/* reason: clear-topic-filter chip control; w-2.5 h-2.5 icon inline in chip */}
                      <button data-viewport-allow-small-target onClick={() => setFilter('topicFilter', null)} className="text-violet-500 hover:text-violet-200"><X className="w-2.5 h-2.5" /></button>
                    </span>
                  )}
                  {tagFilter && (
                    <span className="flex items-center gap-1 text-ui-sm bg-neutral-800 border border-neutral-700 text-neutral-300 px-2 py-0.5 rounded">
                      #{tagFilter}
                      {/* reason: clear-tag-filter chip control; w-2.5 h-2.5 icon inline in chip */}
                      <button data-viewport-allow-small-target onClick={() => setFilter('tagFilter', null)} className="text-neutral-500 hover:text-neutral-200"><X className="w-2.5 h-2.5" /></button>
                    </span>
                  )}
                  {sweepFilter && (
                    <span className="flex items-center gap-1 text-ui-sm bg-amber-950/40 border border-amber-900/50 text-amber-300 px-2 py-0.5 rounded font-mono">
                      sweep: {sweepFilter.slice(0, 8)}…
                      {/* reason: clear-sweep-filter chip control; w-2.5 h-2.5 icon inline in chip */}
                      <button data-viewport-allow-small-target onClick={() => setFilter('sweepFilter', null)} className="text-amber-500 hover:text-amber-200"><X className="w-2.5 h-2.5" /></button>
                    </span>
                  )}
                  {lifecycleFilter && (
                    <span className="flex items-center gap-1 text-ui-sm bg-neutral-800 border border-neutral-700 text-neutral-300 px-2 py-0.5 rounded">
                      lifecycle: {lifecycleFilter}
                      {/* reason: clear-lifecycle-filter chip control; w-2.5 h-2.5 icon inline in chip */}
                      <button data-viewport-allow-small-target onClick={() => setFilter('lifecycleFilter', null)} className="text-neutral-500 hover:text-neutral-200"><X className="w-2.5 h-2.5" /></button>
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
                <span className="text-ui-xs uppercase tracking-wide text-neutral-600 mr-0.5">origin</span>
                {/* reason: origin filter segment; text-ui-xs inline control matching card chip density */}
                <button
                  data-viewport-allow-small-target
                  onClick={() => setFilter('sourceKindFilter', null)}
                  className={`text-ui-xs uppercase tracking-wider px-1.5 py-0.5 rounded border transition-colors ${sourceKindFilter === null ? 'bg-neutral-700 border-neutral-600 text-neutral-100' : 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300'}`}
                >All</button>
                {SOURCE_KIND_OPTIONS.map(kind => {
                  const style = SOURCE_KIND_STYLES[kind];
                  const active = sourceKindFilter === kind;
                  const Icon = style.icon;
                  return (
                    // reason: origin filter segment; text-ui-xs inline control matching card chip density
                    <button
                      data-viewport-allow-small-target
                      key={kind}
                      onClick={() => setFilter('sourceKindFilter', active ? null : kind)}
                      className={`text-ui-xs uppercase tracking-wider px-1.5 py-0.5 rounded border inline-flex items-center gap-1 transition-colors ${active ? `${style.bg} ${style.text} border-current` : 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300'}`}
                    >
                      <Icon className="w-2.5 h-2.5 shrink-0" />
                      {style.label}
                    </button>
                  );
                })}
              </div>
              {/* Challenge-state filter — scope to a research-finding origin's second-opinion outcome. */}
              <div className="flex items-center gap-1 flex-wrap">
                <span className="text-ui-xs uppercase tracking-wide text-neutral-600 mr-0.5">challenge</span>
                {/* reason: challenge filter segment; text-ui-xs inline control matching card chip density */}
                <button
                  data-viewport-allow-small-target
                  onClick={() => setFilter('challengeStateFilter', null)}
                  className={`text-ui-xs uppercase tracking-wider px-1.5 py-0.5 rounded border transition-colors ${challengeStateFilter === null ? 'bg-neutral-700 border-neutral-600 text-neutral-100' : 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300'}`}
                >All</button>
                {CHALLENGE_STATE_OPTIONS.map(state => {
                  const style = CHALLENGE_STATE_STYLES[state];
                  const active = challengeStateFilter === state;
                  const Icon = style.icon;
                  return (
                    // reason: challenge filter segment; text-ui-xs inline control matching card chip density
                    <button
                      data-viewport-allow-small-target
                      key={state}
                      onClick={() => setFilter('challengeStateFilter', active ? null : state)}
                      className={`text-ui-xs uppercase tracking-wider px-1.5 py-0.5 rounded border inline-flex items-center gap-1 transition-colors ${active ? `${style.bg} ${style.text} border-current` : 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300'}`}
                      title={style.tooltip}
                    >
                      <Icon className="w-2.5 h-2.5 shrink-0" />
                      {style.label}
                    </button>
                  );
                })}
              </div>
              {/* Lifecycle-status filter — scope to a single follow-up state. */}
              <div className="flex items-center gap-1 flex-wrap">
                <span className="text-ui-xs uppercase tracking-wide text-neutral-600 mr-0.5">lifecycle</span>
                {/* reason: lifecycle filter segment; text-ui-xs inline control matching card chip density */}
                <button
                  data-viewport-allow-small-target
                  onClick={() => setFilter('lifecycleFilter', null)}
                  className={`text-ui-xs uppercase tracking-wider px-1.5 py-0.5 rounded border transition-colors ${lifecycleFilter === null ? 'bg-neutral-700 border-neutral-600 text-neutral-100' : 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300'}`}
                >All</button>
                {LIFECYCLE_OPTIONS.map(state => {
                  const style = LIFECYCLE_STYLES[state];
                  const active = lifecycleFilter === state;
                  return (
                    // reason: lifecycle filter segment; text-ui-xs inline control matching card chip density
                    <button
                      data-viewport-allow-small-target
                      key={state}
                      onClick={() => setFilter('lifecycleFilter', active ? null : state)}
                      className={`text-ui-xs uppercase tracking-wider px-1.5 py-0.5 rounded border inline-flex items-center gap-1 transition-colors ${active ? `${style.bg} ${style.text} border-current` : 'bg-neutral-900 border-neutral-800 text-neutral-500 hover:text-neutral-300'}`}
                      title={style.tooltip}
                    >
                      {style.label}
                    </button>
                  );
                })}
              </div>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-2">
              {loading && docs.length === 0 && <div className="text-neutral-600 text-center py-12 text-sm" data-testid="loading-initial">Loading…</div>}
              {error && <div className="text-red-400 text-center py-12 text-sm">{error}</div>}
              {/* Surfaces initialDocumentId body-load failures, which never reach the detail view. */}
              {docError && <div data-testid="doc-error" className="text-red-400 text-center py-2 text-xs">{docError}</div>}
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
                    <span className="text-ui-sm text-neutral-600 shrink-0">{relativeDate(doc.created_at)}</span>
                  </div>
                  {doc.summary && <p className="text-ui-md text-neutral-500 mt-1 line-clamp-2 leading-snug">{doc.summary}</p>}
                  <div className="flex items-center gap-1.5 flex-wrap mt-1.5">
                    <SourceKindBadge kind={doc.source_kind} />
                    <CriterionVerdictBadge doc={doc} />
                    <AcceptanceGradeBadge doc={doc} />
                    <EvidenceTierBadge tier={doc.evidence_tier} />
                    <ExecutionLocationBadge doc={doc} />
                    <OriginBadge doc={doc} />
                    <LifecycleBadge status={doc.lifecycle_status} />
                    {doc.direction_id && (
                      // reason: direction filter chip in compact card footer; text-ui-xs inline control
                      <button
                        data-viewport-allow-small-target
                        onClick={e => { e.stopPropagation(); setFilter('directionFilter', doc.direction_id); }}
                        className="text-ui-xs px-1.5 py-0.5 rounded bg-blue-950/40 text-blue-300 hover:bg-blue-900/40 transition-colors"
                      >{doc.direction_id}</button>
                    )}
                    {doc.topic && (
                      // reason: topic filter chip in compact card footer; text-ui-xs inline control
                      <button
                        data-viewport-allow-small-target
                        onClick={e => { e.stopPropagation(); setFilter('topicFilter', doc.topic ?? null); }}
                        className="text-ui-xs px-1.5 py-0.5 rounded bg-violet-950/40 text-violet-300 hover:bg-violet-900/40 transition-colors"
                      >{doc.topic}</button>
                    )}
                    {doc.sweep_id && (
                      // reason: sweep filter chip in compact card footer; text-ui-xs inline control
                      <button
                        data-viewport-allow-small-target
                        onClick={e => { e.stopPropagation(); setFilter('sweepFilter', doc.sweep_id ?? null); }}
                        className="text-ui-xs px-1.5 py-0.5 rounded bg-amber-950/40 text-amber-300 hover:bg-amber-900/40 transition-colors font-mono"
                        title={`Sweep ${doc.sweep_id}${doc.arm_label ? ` / ${doc.arm_label}` : ' (aggregate)'}`}
                      >{doc.sweep_id.slice(0, 8)}…{doc.arm_label ? ` / ${doc.arm_label}` : ' agg'}</button>
                    )}
                    {(doc.tags ?? []).slice(0, 4).map(t => (
                      // reason: tag filter chip in compact card footer; text-ui-xs inline control
                    <button
                      data-viewport-allow-small-target
                      key={t}
                      onClick={e => { e.stopPropagation(); setFilter('tagFilter', t); }}
                      className="text-ui-xs px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 hover:bg-neutral-700 transition-colors"
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
