import { useState } from 'react';
import { Lock, Shield, Target, HelpCircle, ChevronDown, ChevronRight, X } from 'lucide-react';
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
}: {
  pending: PendingConfirmation;
  onDismiss: () => Promise<void>;
}) {
  const [expanded, setExpanded] = useState(false);
  const [dismissing, setDismissing] = useState(false);
  const label = TYPE_LABELS[pending.type] ?? pending.type;

  return (
    <div className="border-l-2 border-amber-500/40 pl-3">
      <div className="flex items-center gap-2 min-h-[44px]">
        <span className="text-neutral-400 shrink-0" title={`${label} — id: ${pending.id}`}>
          <PendingTypeIcon type={pending.type} />
        </span>
        <span className="text-xs text-neutral-400 shrink-0">{label}</span>
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
          title={expanded ? 'Collapse' : 'Show details'}
          aria-expanded={expanded}
        >
          {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        </button>
        <button
          type="button"
          disabled={dismissing}
          onClick={async () => {
            setDismissing(true);
            try { await onDismiss(); } finally { setDismissing(false); }
          }}
          className="shrink-0 inline-flex items-center gap-1 px-2 py-1 min-h-[44px] md:min-h-0 text-xs font-medium rounded border border-neutral-700 bg-neutral-800/50 text-neutral-400 hover:bg-neutral-800 hover:text-neutral-100 transition disabled:opacity-50"
          title="Mark as resolved"
        >
          <X size={11} />
          Resolve
        </button>
      </div>
      {expanded && (
        <div className="pb-2 pr-2 space-y-1">
          <p className="text-sm text-neutral-200">{pending.proposal}</p>
          {pending.rationale && (
            <p className="text-xs text-neutral-400">{pending.rationale}</p>
          )}
          <p className="text-ui-sm font-mono text-neutral-700">{pending.id}</p>
        </div>
      )}
    </div>
  );
}

interface PendingConfirmationListProps {
  pending: PendingConfirmation[];
  onDismiss: (id: string) => Promise<void>;
}

export function PendingConfirmationList({ pending, onDismiss }: PendingConfirmationListProps) {
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
          onDismiss={() => onDismiss(p.id)}
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
