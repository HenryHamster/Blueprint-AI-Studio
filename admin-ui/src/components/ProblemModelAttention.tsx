import { useState } from 'react';
import { Lock, Shield, Target, HelpCircle, ChevronDown, ChevronRight, X, Check } from 'lucide-react';
import { timeAgo } from '../utils/time';
import type { PendingConfirmation, PendingConfirmationType } from '../types';

function PendingTypeIcon({ type }: { type: PendingConfirmationType }) {
  switch (type) {
    case 'credentials_needed': return <Lock size={12} />;
    case 'access_needed': return <Shield size={12} />;
    case 'intent_change': return <Target size={12} />;
    case 'major_decision': return <HelpCircle size={12} />;
    default: return <HelpCircle size={12} />;
  }
}

const TYPE_LABELS: Record<PendingConfirmationType, string> = {
  credentials_needed: 'Credentials needed',
  access_needed: 'Access needed',
  intent_change: 'Intent change',
  major_decision: 'Major decision',
};

// Agent-blocking asks (credentials/access) sort above steering asks
const TYPE_ORDER: PendingConfirmationType[] = [
  'credentials_needed',
  'access_needed',
  'intent_change',
  'major_decision',
];

const VISIBLE_CAP = 3;

function PendingItemRow({
  pending,
  onDismiss,
  onAccept,
  readOnly = false,
}: {
  pending: PendingConfirmation;
  onDismiss?: (note?: string) => Promise<void>;
  onAccept?: (answer?: string) => Promise<void>;
  /** When true the row is a passive listing — no accept/dismiss affordances.
      Used by the problem-model view so acting lives in a single place. */
  readOnly?: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const [dismissing, setDismissing] = useState(false);
  const [accepting, setAccepting] = useState(false);
  const [answer, setAnswer] = useState('');
  const label = TYPE_LABELS[pending.type] ?? pending.type;

  const handleAccept = async (answerText?: string) => {
    if (!onAccept) return;
    setAccepting(true);
    try { await onAccept(answerText); } finally { setAccepting(false); }
  };

  const acceptLabel =
    pending.type === 'intent_change' ? 'Accept intent' :
    pending.type === 'major_decision' ? 'Answer' :
    'Mark provided';

  return (
    <div className="border-l-2 border-amber-500/40 pl-3">
      <div className="flex items-center gap-2 min-h-[44px] min-w-0">
        <span className="text-neutral-400 shrink-0" title={`${label} — id: ${pending.id}`}>
          <PendingTypeIcon type={pending.type} />
        </span>
        {/* Type label yields space (shrink + truncate) so a narrow rail row of
            icon + label + proposal + timestamp + action buttons never forces
            horizontal overflow; the icon title keeps the full type discoverable. */}
        <span className="text-xs text-neutral-400 shrink truncate min-w-0 max-w-[45%]" title={label}>{label}</span>
        <span className="text-neutral-700 shrink-0 select-none">—</span>
        <p className="text-xs text-neutral-200 flex-1 truncate min-w-0" title={pending.proposal}>
          {pending.proposal}
        </p>
        {pending.created_at && (
          <span className="text-ui-sm text-neutral-600 shrink-0">
            {timeAgo(pending.created_at)}
          </span>
        )}
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="shrink-0 p-1 min-h-[44px] md:min-h-0 rounded text-neutral-600 hover:text-neutral-300 transition"
          style={{ minWidth: 44 }}
          title={expanded ? 'Collapse' : 'Show details'}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        </button>
        {!readOnly && pending.type !== 'major_decision' && onAccept && (
          <button
            type="button"
            disabled={accepting || dismissing}
            onClick={() => handleAccept()}
            className="shrink-0 inline-flex items-center gap-1 px-2 py-1 min-h-[44px] md:min-h-0 text-xs font-medium rounded border border-emerald-800 bg-emerald-950/50 text-emerald-400 hover:bg-emerald-900/40 hover:text-emerald-300 transition disabled:opacity-50"
            style={{ minWidth: 44 }}
            title={acceptLabel}
          >
            <Check size={11} />
            {/* Label collapses to the icon on narrow viewports so the action
                row fits 375px; the title tooltip keeps it discoverable. */}
            <span className="hidden sm:inline">{acceptLabel}</span>
          </button>
        )}
        {!readOnly && onDismiss && (
          <button
            type="button"
            disabled={dismissing || accepting}
            onClick={async () => {
              setDismissing(true);
              try { await onDismiss(answer.trim() || undefined); } finally { setDismissing(false); }
            }}
            className="shrink-0 inline-flex items-center gap-1 px-2 py-1 min-h-[44px] md:min-h-0 text-xs font-medium rounded border border-neutral-700 bg-workbench-panel/50 text-neutral-400 hover:bg-workbench-hover hover:text-neutral-100 transition disabled:opacity-50"
            style={{ minWidth: 44 }}
            title="Mark as resolved without action"
          >
            <X size={11} />
            <span className="hidden sm:inline">Resolve</span>
          </button>
        )}
      </div>
      {expanded && (
        <div className="pb-2 pr-2 space-y-2">
          <p className="text-sm text-neutral-200">{pending.proposal}</p>
          {pending.rationale && (
            <p className="text-xs text-neutral-400">{pending.rationale}</p>
          )}
          {!readOnly && onAccept && (
            <div className="flex gap-2 items-start pt-1">
              <textarea
                value={answer}
                onChange={(e) => setAnswer(e.target.value)}
                placeholder={pending.type === 'major_decision' ? 'Enter your answer…' : 'Add a note (optional)…'}
                rows={2}
                className="flex-1 text-xs bg-workbench-panel border border-neutral-700 rounded px-2 py-1.5 text-neutral-200 placeholder-neutral-600 resize-none workbench-focus"
              />
              <button
                type="button"
                disabled={accepting || !answer.trim()}
                onClick={() => handleAccept(answer.trim())}
                className="shrink-0 inline-flex items-center gap-1 px-2 py-1.5 min-h-[44px] md:min-h-0 text-xs font-medium rounded border border-emerald-800 bg-emerald-950/50 text-emerald-400 hover:bg-emerald-900/40 hover:text-emerald-300 transition disabled:opacity-50"
                style={{ minWidth: 44 }}
              >
                <Check size={11} />
                {acceptLabel}
              </button>
            </div>
          )}
          <p className="text-ui-sm font-mono text-neutral-700">{pending.id}</p>
        </div>
      )}
    </div>
  );
}

interface PendingConfirmationListProps {
  pending: PendingConfirmation[];
  onDismiss?: (id: string, note?: string) => Promise<void>;
  onAccept?: (id: string, answer?: string) => Promise<void>;
  /** Render as a passive listing (no accept/dismiss buttons). */
  readOnly?: boolean;
}

export function PendingConfirmationList({ pending, onDismiss, onAccept, readOnly = false }: PendingConfirmationListProps) {
  const [showAll, setShowAll] = useState(false);

  const sorted = [...pending].sort((a, b) => {
    const ai = TYPE_ORDER.indexOf(a.type);
    const bi = TYPE_ORDER.indexOf(b.type);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  const visible = showAll ? sorted : sorted.slice(0, VISIBLE_CAP);
  const overflow = sorted.length - VISIBLE_CAP;

  return (
    <div className="space-y-1">
      {visible.map((p) => (
        <PendingItemRow
          key={p.id}
          pending={p}
          readOnly={readOnly}
          onDismiss={onDismiss ? (note) => onDismiss(p.id, note) : undefined}
          onAccept={onAccept ? (answer) => onAccept(p.id, answer) : undefined}
        />
      ))}
      {!showAll && overflow > 0 && (
        <button
          type="button"
          onClick={() => setShowAll(true)}
          className="pl-3 text-xs text-neutral-500 hover:text-neutral-300 transition"
        >
          Show {overflow} more...
        </button>
      )}
    </div>
  );
}
