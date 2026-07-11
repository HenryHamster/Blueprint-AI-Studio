import { useState } from 'react';
import { Pencil, ChevronDown, ChevronRight, AlertCircle, Target, Box, CheckCircle2, Circle, Clock, Flag, GitBranch, Plus } from 'lucide-react';
import { timeAgo, formatAbsolute } from '../utils/time';
import { MarkdownContent } from './MarkdownContent';
import { ProblemModelEditor } from './ProblemModelEditor';
import { PendingConfirmationList } from './ProblemModelAttention';
import { dismissPendingConfirmation, acceptPendingConfirmation } from '../api';
import type { CurrentVersion } from '../api';
import type {
  ProblemModel,
  Gap,
  Capability,
  Friction,
  ProblemModelPatch,
  Source,
  SelfReflectionEntry,
} from '../types';
import type { CapabilityProofRecord, EvidenceRef } from '../types/problemModel';

interface ProblemModelViewerProps {
  projectId: string;
  problemModel: ProblemModel;
  selfReflectionLog?: SelfReflectionEntry[];
  currentVersion?: CurrentVersion | null;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onUpdated: (patched: ProblemModel) => void;
  // Callbacks wired from DirectionsPanel to make gap cards actionable.
  onSpawnDirection?: (gapId: string, gapName: string) => void;
  onLinkDirection?: (directionId: string, gapId: string) => void;
  directions?: Array<{ id: string; text: string }>;
}

const capabilityBarColors: Record<string, string> = {
  working: 'bg-emerald-500',
  partial: 'bg-amber-500',
  planned: 'bg-neutral-600',
};

const capabilityStatusLabels: Record<string, string> = {
  working: 'Working',
  partial: 'Partial',
  planned: 'Planned',
};

const priorityColors: Record<string, string> = {
  high: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
  medium: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  low: 'bg-neutral-700/40 text-neutral-400 border-neutral-700',
};

const sizeColors: Record<string, string> = {
  small: 'text-neutral-500',
  medium: 'text-neutral-400',
  large: 'text-neutral-300',
};

const proofStatusLabels: Record<string, string> = {
  proven: 'Proven',
  path_proven: 'Path-backed',
  unproven: 'Claimed only',
  exempt: 'Exempt',
};

const proofStatusColors: Record<string, string> = {
  proven: 'text-emerald-400',
  path_proven: 'text-amber-400',
  unproven: 'text-neutral-500',
};

const proofKindColors: Record<string, string> = {
  backing_path: 'border-amber-500/30 bg-amber-500/8',
  named_test: 'border-emerald-500/20 bg-emerald-500/5',
  trial_adoption: 'border-emerald-500/20 bg-emerald-500/5',
  downstream_artifact: 'border-emerald-500/20 bg-emerald-500/5',
  visual: 'border-emerald-500/20 bg-emerald-500/5',
  observable_behavior: 'border-emerald-500/20 bg-emerald-500/5',
};

function SourceBadge({ source }: { source: Source }) {
  // The message short ref is exactly the first 8 hex chars of the message UUID —
  // the canonical, resolvable reference the backend short-ref resolver matches
  // (MIN_MESSAGE_REF_HEXLEN = 8). Keep this slice length in lockstep with the
  // backend resolver so the displayed ref is always usable as-is via
  // `./fetch message <ref>` / `./messages resolve <ref>` — do NOT shorten it.
  const messageRef = source.message_id ? source.message_id.slice(0, 8) : null;
  const tail: string[] = [];
  if (source.pr_number) tail.push(`PR #${source.pr_number}`);
  if (source.direction_id) tail.push(`dir ${source.direction_id.slice(0, 8)}`);
  if (source.friction_id) tail.push(`friction ${source.friction_id.slice(0, 8)}`);
  return (
    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 text-ui-sm font-medium rounded bg-neutral-800/70 text-neutral-400 border border-neutral-800">
      {source.type.replace(/_/g, ' ')}
      {messageRef && (
        <span
          title={`Message ref ${messageRef} — resolvable short ref (fetch message ${messageRef} / messages resolve ${messageRef}); matches the backend short-ref resolver, no full UUID needed`}
        >
          {' · '}msg {messageRef}
        </span>
      )}
      {tail.length > 0 && ` · ${tail.join(' · ')}`}
    </span>
  );
}

function SectionHeader({ children, icon, count, amberCount }: { children: React.ReactNode; icon?: React.ReactNode; count?: number; amberCount?: number }) {
  return (
    <div className="flex items-center gap-2 mb-2 mt-4 first:mt-0">
      {icon && <span className="text-neutral-500">{icon}</span>}
      <h3 className="text-sm font-semibold text-neutral-200 uppercase tracking-wide">{children}</h3>
      {typeof amberCount === 'number' && (
        <span className="ml-1 px-1.5 py-0.5 text-xs font-semibold rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">
          {amberCount}
        </span>
      )}
      {typeof count === 'number' && (
        <span className="text-xs text-neutral-500">({count})</span>
      )}
    </div>
  );
}

function CapabilityBar({ capability }: { capability: Capability }) {
  const status = capability.status || 'planned';
  const proofRecords = Array.isArray(capability.proof_records) ? capability.proof_records : [];
  const evidenceRefs = Array.isArray(capability.evidence_refs) ? capability.evidence_refs : [];
  const isExempt = !!capability.proof_exempt;
  // Exempt working capabilities show "Exempt" instead of "Claimed only"
  // — they are exempt from proof requirements and do not count as unproven.
  // Derive proofStatus from the backend-enriched proof_status field;
  // fall back to the old (proof-records-exist → proven) heuristic for
  // payloads that haven't been through the backend enricher yet.
  const proofStatus: string = capability.proof_status
    || (proofRecords.length > 0 ? 'proven' : 'unproven');
  const proofColor = proofStatusColors[proofStatus] || 'text-neutral-500';

  function proofKindColor(kind: string): string {
    return proofKindColors[kind] || 'border-neutral-500/20 bg-neutral-500/5';
  }
  return (
    <div className="flex items-center gap-3 py-1.5">
      <div className={`w-2 h-2 rounded-full ${capabilityBarColors[status] || capabilityBarColors.planned}`} />
      <div className="flex-1 min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="text-sm text-neutral-200 truncate">{capability.name}</span>
          <span className="text-xs text-neutral-500">{capabilityStatusLabels[status] || status}</span>
          {isExempt ? (
            <span className="text-ui-sm font-medium uppercase tracking-wide text-neutral-500 border border-dashed border-neutral-600 rounded px-1 py-px">
              Exempt
            </span>
          ) : (
            <span className={`text-ui-sm font-medium uppercase tracking-wide ${proofColor}`}>
              {proofStatusLabels[proofStatus] || proofStatus}
            </span>
          )}
        </div>
        {capability.notes && (
          <p className="text-xs text-neutral-500 truncate">{capability.notes}</p>
        )}
        {evidenceRefs.length > 0 && (
          <div className="mt-1 space-y-0.5">
            {evidenceRefs.map((ref: EvidenceRef, idx: number) => (
              <div
                key={`evidence-${ref.source_id || idx}-${idx}`}
                className={`rounded border px-2 py-0.5 text-ui-md text-neutral-300 ${proofKindColor(ref.kind)}`}
              >
                <span className="text-neutral-500">{ref.kind}</span>{' '}
                <span className="break-all">{ref.ref}</span>
                {ref.proven ? (
                  <span className="ml-1 text-emerald-400">✓</span>
                ) : (
                  <span className="ml-1 text-neutral-600">—</span>
                )}
              </div>
            ))}
          </div>
        )}
        {proofRecords.length > 0 && (
          <div className="mt-1 space-y-1">
            {proofRecords.map((record: CapabilityProofRecord, index: number) => (
              <div
                key={`${record.task_id}-${record.proof_ref}-${index}`}
                className={`rounded border px-2 py-1 text-ui-md text-neutral-300 ${proofKindColor(record.proof_kind)}`}
              >
                <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                  <span>
                    <span className="text-neutral-500">kind</span> {record.proof_kind}
                  </span>
                  <span className="break-all">
                    <span className="text-neutral-500">ref</span> {record.proof_ref}
                  </span>
                  <span className="font-mono">
                    <span className="text-neutral-500 font-sans">commit</span> {record.commit_sha.slice(0, 12) || 'n/a'}
                  </span>
                  <span title={formatAbsolute(record.proved_at)}>
                    <span className="text-neutral-500">proved</span> {timeAgo(record.proved_at)}
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
      {capability.source && <SourceBadge source={capability.source} />}
    </div>
  );
}

function GapLinkSelect({
  gapId,
  directions,
  onLink,
}: {
  gapId: string;
  directions: Array<{ id: string; text: string }>;
  onLink: (directionId: string, gapId: string) => void;
}) {
  const [value, setValue] = useState('');
  return (
    <select
      value={value}
      onChange={e => {
        const id = e.target.value;
        if (id) {
          onLink(id, gapId);
          setValue('');
        }
      }}
      className="text-xs bg-neutral-900 border border-neutral-700 text-neutral-400 rounded px-2 py-1 min-w-0 max-w-full focus:outline-none focus:ring-1 focus:ring-neutral-600"
    >
      <option value="">Link direction…</option>
      {directions.map(d => (
        <option key={d.id} value={d.id}>
          {d.text.length > 50 ? `${d.text.slice(0, 50)}…` : d.text}
        </option>
      ))}
    </select>
  );
}

function GapCard({
  gap,
  onSpawnDirection,
  onLinkDirection,
  directions,
}: {
  gap: Gap;
  onSpawnDirection?: (gapId: string, gapName: string) => void;
  onLinkDirection?: (directionId: string, gapId: string) => void;
  directions?: Array<{ id: string; text: string }>;
}) {
  const activeMoves = gap.active_moves ?? [];
  const pastMoves = gap.past_moves ?? [];
  const hasActions = onSpawnDirection || (onLinkDirection && directions && directions.length > 0);
  return (
    <div className="border border-neutral-800 rounded-md p-3 bg-neutral-900/40">
      <div className="flex items-start justify-between gap-2 mb-1">
        <div className="flex-1 min-w-0">
          <div className="flex flex-wrap items-center gap-1.5 mb-1">
            <span className={`inline-flex items-center px-1.5 py-0.5 text-ui-sm font-medium rounded border ${priorityColors[gap.priority] || priorityColors.medium}`}>
              {gap.priority}
            </span>
            <span className={`text-ui-sm uppercase tracking-wide ${sizeColors[gap.size] || sizeColors.medium}`}>
              {gap.size}
            </span>
            {gap.tag && (
              <span className="inline-flex items-center px-1.5 py-0.5 text-ui-sm font-medium rounded bg-cyan-500/15 text-cyan-300 border border-cyan-500/30">
                {gap.tag}
              </span>
            )}
            <span className="text-ui-sm font-mono text-neutral-600">{gap.id}</span>
          </div>
          <h4 className="text-sm font-medium text-neutral-100">{gap.name}</h4>
        </div>
        {gap.source && <SourceBadge source={gap.source} />}
      </div>
      {gap.description && (
        <p className="text-sm text-neutral-300 mt-1.5">{gap.description}</p>
      )}
      {gap.evidence && (
        <p className="text-xs text-neutral-500 mt-1 italic">Evidence: {gap.evidence}</p>
      )}
      {(activeMoves.length > 0 || pastMoves.length > 0) && (
        <div className="mt-2 text-xs text-neutral-500 flex flex-wrap gap-2">
          {activeMoves.length > 0 && (
            <span><span className="text-neutral-400">Active:</span> {activeMoves.length} direction{activeMoves.length === 1 ? '' : 's'}</span>
          )}
          {pastMoves.length > 0 && (
            <span><span className="text-neutral-400">Past:</span> {pastMoves.length}</span>
          )}
        </div>
      )}
      {hasActions && (
        <div className="mt-2 pt-2 border-t border-neutral-800/60 flex items-center gap-2 flex-wrap">
          {onSpawnDirection && (
            <button
              type="button"
              onClick={() => onSpawnDirection(gap.id, gap.name)}
              className="inline-flex items-center gap-1 px-2 py-1 min-h-[44px] md:min-h-auto text-xs rounded border border-neutral-700 bg-neutral-800/50 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-100 transition"
            >
              <Plus size={10} />
              Spawn direction
            </button>
          )}
          {onLinkDirection && directions && directions.length > 0 && (
            <GapLinkSelect gapId={gap.id} directions={directions} onLink={onLinkDirection} />
          )}
        </div>
      )}
    </div>
  );
}

function ArchivedGapRow({ gap }: { gap: Gap }) {
  return (
    <div className="flex items-baseline gap-2 py-1 text-sm">
      <span className="text-neutral-600 text-xs font-mono shrink-0">{gap.id}</span>
      <span className="text-neutral-300 truncate flex-1">{gap.name}</span>
      <span className={`text-ui-sm uppercase px-1.5 py-0.5 rounded ${gap.status === 'closed' ? 'bg-emerald-500/10 text-emerald-400' : 'bg-neutral-700/40 text-neutral-500'}`}>
        {gap.status}
      </span>
      {gap.closed && <span className="text-xs text-neutral-500">{timeAgo(gap.closed)}</span>}
    </div>
  );
}

function FrictionRow({ friction }: { friction: Friction }) {
  const statusColor = friction.status === 'resolved'
    ? 'text-emerald-400'
    : friction.status === 'mitigated' ? 'text-amber-400' : 'text-rose-400';
  return (
    <div className="border border-neutral-800 rounded-md p-2.5 bg-neutral-900/30">
      <div className="flex items-baseline gap-2 mb-0.5">
        <span className={`text-xs uppercase tracking-wide font-semibold ${statusColor}`}>
          {friction.status}
        </span>
        <span className="text-sm text-neutral-200 flex-1">{friction.name}</span>
        {friction.source && <SourceBadge source={friction.source} />}
      </div>
      {friction.description && (
        <p className="text-xs text-neutral-500 mt-0.5">{friction.description}</p>
      )}
      {friction.seen_in.length > 0 && (
        <p className="text-xs text-neutral-600 mt-1">
          Seen in {friction.seen_in.length} direction{friction.seen_in.length === 1 ? '' : 's'}
        </p>
      )}
      {friction.promoted_to_gap_id && (
        <p className="text-xs text-cyan-400 mt-1">
          Promoted to gap {friction.promoted_to_gap_id}
        </p>
      )}
    </div>
  );
}

function SelfReflectionCard({ entry }: { entry: SelfReflectionEntry }) {
  return (
    <div className="border border-neutral-800 rounded-md p-3 bg-neutral-900/30 space-y-2">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-ui-sm text-neutral-500">{timeAgo(entry.ts)}</span>
        {entry.cycle_task_id && (
          <span className="text-ui-sm font-mono text-neutral-600">{entry.cycle_task_id.slice(0, 8)}</span>
        )}
      </div>
      {entry.what_was_hard && (
        <div>
          <h4 className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wide mb-0.5">What was hard</h4>
          <p className="text-xs text-neutral-300">{entry.what_was_hard}</p>
        </div>
      )}
      {entry.missing_context && (
        <div>
          <h4 className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wide mb-0.5">Missing context</h4>
          <p className="text-xs text-neutral-300">{entry.missing_context}</p>
        </div>
      )}
      {entry.proposed_self_improvement && (
        <div>
          <h4 className="text-ui-sm font-semibold text-amber-600/80 uppercase tracking-wide mb-0.5">Proposed improvement</h4>
          <p className="text-xs text-neutral-200">{entry.proposed_self_improvement}</p>
        </div>
      )}
    </div>
  );
}

export default function ProblemModelViewer({
  projectId,
  problemModel,
  selfReflectionLog = [],
  currentVersion,
  addToast,
  onUpdated,
  onSpawnDirection,
  onLinkDirection,
  directions,
}: ProblemModelViewerProps) {
  const [showEditor, setShowEditor] = useState(false);
  const [showArchived, setShowArchived] = useState(false);

  const intent = problemModel.intent ?? { purpose: '', current_focus: '' };
  const context = problemModel.context ?? {};
  const constraints = problemModel.constraints ?? [];
  const capabilities = problemModel.capabilities ?? [];
  const gaps = problemModel.gaps ?? [];
  const archivedGaps = problemModel.archived_gaps ?? [];
  const friction = problemModel.friction ?? [];
  const pending = problemModel.pending_user_confirmation ?? [];
  const revision = problemModel.revision ?? 0;
  const lastUpdated = problemModel.last_updated ?? '';
  const lastUpdatedBy = problemModel.last_updated_by ?? null;

  const isEmpty = !intent.purpose && !intent.user_purpose && !intent.current_focus
    && constraints.length === 0 && capabilities.length === 0
    && gaps.length === 0 && friction.length === 0;

  // Group capabilities by status for a cleaner visual grouping
  const capsByStatus = capabilities.reduce<Record<string, Capability[]>>((acc, cap) => {
    const s = cap.status || 'planned';
    (acc[s] ||= []).push(cap);
    return acc;
  }, {});

  // Sort gaps by priority (high > medium > low)
  const priorityRank: Record<string, number> = { high: 0, medium: 1, low: 2 };
  const sortedGaps = [...gaps].sort((a, b) =>
    (priorityRank[a.priority] ?? 3) - (priorityRank[b.priority] ?? 3)
  );

  const hasContextFields = Boolean(
    context.project_type || context.users || context.team || context.deploy_cadence || context.notes
  );

  const hasIntent = Boolean(intent.purpose || intent.user_purpose || intent.current_focus);

  return (
    <div className="flex flex-col h-full">
      {/* Header */}
      <div className="border-b border-neutral-800 px-4 py-3 flex items-start justify-between gap-3">
        <div className="flex-1 min-w-0">
          <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
            <Target size={14} className="text-neutral-400" />
            Problem Model
          </h2>
          <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-neutral-500 mt-1">
            <span>rev {revision}</span>
            {lastUpdated && <span>updated {timeAgo(lastUpdated)}</span>}
            {lastUpdatedBy && <span>by {lastUpdatedBy}</span>}
          </div>
        </div>
        <button
          type="button"
          onClick={() => setShowEditor(true)}
          className="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 text-xs font-medium rounded-md border border-neutral-700 bg-neutral-800/50 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-100 transition"
        >
          <Pencil size={11} />
          Edit intent
        </button>
      </div>

      <div className="flex-1 overflow-y-auto custom-scrollbar px-4 py-4 space-y-2">
        {isEmpty && (
          <div className="text-center py-12 text-neutral-500">
            <p className="text-sm">The problem model hasn't been populated yet.</p>
            <p className="text-xs mt-1 text-neutral-600">
              Edit intent to seed it, or run a strategic cycle to have the agent bootstrap it.
            </p>
          </div>
        )}

        {/* Pending confirmations — calm rows, amber used once per row (left accent) */}
        {pending.length > 0 && (
          <section>
            <SectionHeader icon={<AlertCircle size={14} />} amberCount={pending.length}>
              Needs your attention
            </SectionHeader>
            <PendingConfirmationList
              pending={pending}
              onDismiss={async (id, note) => {
                try {
                  await dismissPendingConfirmation(projectId, id, note);
                  onUpdated({
                    ...problemModel,
                    pending_user_confirmation: pending.filter((x) => x.id !== id),
                    revision: (problemModel.revision ?? 0) + 1,
                  });
                } catch {
                  addToast('Failed to dismiss — try again', 'error');
                }
              }}
              onAccept={async (id, answer) => {
                try {
                  const result = await acceptPendingConfirmation(projectId, id, answer);
                  const next: ProblemModel = {
                    ...problemModel,
                    pending_user_confirmation: pending.filter((x) => x.id !== id),
                    revision: (problemModel.revision ?? 0) + 1,
                  };
                  // For intent_change, reflect the accepted proposal in local state.
                  if (result.type === 'intent_change') {
                    const accepted = pending.find((x) => x.id === id);
                    if (accepted) {
                      const cur = problemModel.intent ?? { purpose: '', current_focus: '' };
                      next.intent = { ...cur, purpose: accepted.proposal };
                    }
                  }
                  onUpdated(next);
                } catch {
                  addToast('Failed to accept — try again', 'error');
                }
              }}
            />
          </section>
        )}

        {/* Intent */}
        {hasIntent && (
          <section>
            <SectionHeader icon={<Flag size={14} />}>Intent</SectionHeader>
            {intent.purpose && (
              <div className="mb-2">
                <h4 className="text-xs text-neutral-500 uppercase tracking-wide mb-0.5">Purpose</h4>
                <p className="text-sm text-neutral-200">{intent.purpose}</p>
              </div>
            )}
            {intent.user_purpose && (
              <div className="mb-2">
                <h4 className="text-xs text-neutral-500 uppercase tracking-wide mb-0.5">Operator purpose</h4>
                <p className="text-sm text-neutral-200">{intent.user_purpose}</p>
              </div>
            )}
            {intent.current_focus && (
              <div>
                <h4 className="text-xs text-neutral-500 uppercase tracking-wide mb-0.5">Current focus</h4>
                <p className="text-sm text-neutral-200">{intent.current_focus}</p>
              </div>
            )}
          </section>
        )}

        {/* Version goal strip — threaded from version-lifecycle, no new fetch */}
        {currentVersion != null && (
          <section>
            <SectionHeader icon={<GitBranch size={14} />}>Version goal</SectionHeader>
            {currentVersion.goal ? (
              <div className="flex flex-col gap-1">
                <p className="text-sm text-neutral-200" title={currentVersion.goal}>
                  {currentVersion.goal}
                </p>
                {currentVersion.goal_updated_at && (
                  <p className="text-ui-sm text-neutral-500">
                    {currentVersion.goal_editor_info && <span>{currentVersion.goal_editor_info} </span>}
                    {timeAgo(currentVersion.goal_updated_at)}
                  </p>
                )}
                {currentVersion.live_current_focus && currentVersion.live_current_focus !== currentVersion.goal && (
                  <div className="flex flex-col gap-1 pt-1 border-t border-amber-500/20">
                    <p className="text-ui-sm px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-400 border border-amber-500/20">
                      Goal has drifted from live focus
                    </p>
                    <p className="text-ui-sm text-neutral-400" title={currentVersion.live_current_focus}>
                      <span className="text-neutral-600">Live focus: </span>{currentVersion.live_current_focus}
                    </p>
                  </div>
                )}
              </div>
            ) : (
              <p className="text-xs text-neutral-600 italic">No version goal set yet.</p>
            )}
          </section>
        )}

        {/* Context */}
        {hasContextFields && (
          <section>
            <SectionHeader>Context</SectionHeader>
            <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
              {context.project_type && <><dt className="text-xs text-neutral-500">Type</dt><dd className="text-neutral-300">{context.project_type}</dd></>}
              {context.users && <><dt className="text-xs text-neutral-500">Users</dt><dd className="text-neutral-300">{context.users}</dd></>}
              {context.team && <><dt className="text-xs text-neutral-500">Team</dt><dd className="text-neutral-300">{context.team}</dd></>}
              {context.deploy_cadence && <><dt className="text-xs text-neutral-500">Cadence</dt><dd className="text-neutral-300">{context.deploy_cadence}</dd></>}
            </dl>
            {context.notes && (
              <div className="mt-2">
                <h4 className="text-xs text-neutral-500 uppercase tracking-wide mb-0.5">Notes</h4>
                <div className="text-sm text-neutral-300">
                  <MarkdownContent content={context.notes} />
                </div>
              </div>
            )}
          </section>
        )}

        {/* Constraints */}
        {constraints.length > 0 && (
          <section>
            <SectionHeader count={constraints.length}>Constraints</SectionHeader>
            <ul className="space-y-1">
              {constraints.map((c, i) => (
                <li key={i} className="text-sm text-neutral-300 pl-3 border-l-2 border-neutral-800">{c}</li>
              ))}
            </ul>
          </section>
        )}

        {/* Capabilities */}
        {capabilities.length > 0 && (
          <section>
            <SectionHeader icon={<Box size={14} />} count={capabilities.length}>Capabilities</SectionHeader>
            <div className="space-y-3">
              {(['working', 'partial', 'planned'] as const).map((status) => {
                const caps = capsByStatus[status] ?? [];
                if (caps.length === 0) return null;
                return (
                  <div key={status}>
                    <h4 className="text-ui-md font-medium text-neutral-500 uppercase tracking-wide mb-1">
                      {capabilityStatusLabels[status]} ({caps.length})
                    </h4>
                    <div className="divide-y divide-neutral-800/70 border border-neutral-800/70 rounded-md">
                      {caps.map((c, i) => (
                        <div key={i} className="px-2"><CapabilityBar capability={c} /></div>
                      ))}
                    </div>
                  </div>
                );
              })}
            </div>
          </section>
        )}

        {/* Gaps (active) */}
        {sortedGaps.length > 0 && (
          <section>
            <SectionHeader icon={<Circle size={14} />} count={sortedGaps.length}>Active gaps</SectionHeader>
            <div className="space-y-2">
              {sortedGaps.map((g, i) => (
                <GapCard
                  key={g.id ?? `gap-${i}`}
                  gap={g}
                  onSpawnDirection={onSpawnDirection}
                  onLinkDirection={onLinkDirection}
                  directions={directions}
                />
              ))}
            </div>
          </section>
        )}

        {/* Friction */}
        {friction.length > 0 && (
          <section>
            <SectionHeader count={friction.length}>Friction</SectionHeader>
            <div className="space-y-2">
              {friction.map((f) => <FrictionRow key={f.id} friction={f} />)}
            </div>
          </section>
        )}

        {/* Self-reflection log */}
        {selfReflectionLog.length > 0 && (
          <section>
            <SectionHeader count={selfReflectionLog.length}>Self-reflection</SectionHeader>
            <div className="space-y-2">
              {selfReflectionLog.map((entry, i) => (
                <SelfReflectionCard key={entry.cycle_task_id ?? `sr-${i}`} entry={entry} />
              ))}
            </div>
          </section>
        )}

        {/* Archived gaps */}
        {archivedGaps.length > 0 && (
          <section>
            <button
              type="button"
              onClick={() => setShowArchived((v) => !v)}
              className="flex items-center gap-1.5 text-xs text-neutral-500 hover:text-neutral-300 transition mt-4"
            >
              {showArchived ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              <CheckCircle2 size={12} />
              Archived gaps ({archivedGaps.length})
            </button>
            {showArchived && (
              <div className="mt-2 pl-4 border-l border-neutral-800 space-y-1">
                {archivedGaps.map((g) => <ArchivedGapRow key={g.id} gap={g} />)}
              </div>
            )}
          </section>
        )}

        {/* Revision hint at bottom */}
        {lastUpdated && (
          <div className="pt-4 text-xs text-neutral-600 text-center">
            <Clock size={10} className="inline mr-1" />
            rev {revision} — {timeAgo(lastUpdated)} by {lastUpdatedBy || 'unknown'}
          </div>
        )}
      </div>

      {showEditor && (
        <ProblemModelEditor
          projectId={projectId}
          problemModel={problemModel}
          onClose={() => setShowEditor(false)}
          onSaved={(patched) => {
            setShowEditor(false);
            onUpdated(patched);
          }}
          addToast={addToast}
        />
      )}
    </div>
  );
}

// Re-export for convenience
export type { ProblemModelPatch };
