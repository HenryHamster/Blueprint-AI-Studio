import { X, Check, AlertTriangle, HelpCircle, Bug, StickyNote, MessageSquare, PlusCircle, Trash2, CheckSquare, Reply, Copy } from 'lucide-react';
import { useState, useCallback } from 'react';
import { parseTimestamp } from '../utils/time';
import { ESCALATION_BADGE_LABELS, HIGH_SIGNAL_NOTE_BADGE_LABELS } from './eventPrefs';

function CopyableMessageRef({ msgId }: { msgId: string }) {
  const [copied, setCopied] = useState(false);
  const shortRef = msgId.slice(0, 8);
  const copy = useCallback(() => {
    navigator.clipboard?.writeText(msgId).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    }).catch(() => {/* clipboard unavailable */});
  }, [msgId]);
  return (
    <button
      onClick={copy}
      title="Copy message ref"
      className="inline-flex items-center gap-1 font-mono text-[10px] text-neutral-600 hover:text-neutral-400 transition-colors"
    >
      {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
      {shortRef}
    </button>
  );
}

export interface ProjectMessage {
  id: string;
  project_id: string;
  message_type: string;
  from_type: string;
  from_task_id: string | null;
  content: string;
  context: Record<string, unknown>;
  status: string;
  resolution: string | null;
  resolved_at: string | null;
  created_at: string | null;
}

// eslint-disable-next-line react-refresh/only-export-components
export const TYPE_CONFIG: Record<string, { icon: typeof MessageSquare; color: string; label: string; bubbleBg: string }> = {
  QUESTION:   { icon: HelpCircle,    color: 'text-blue-400',    label: 'Question',   bubbleBg: 'bg-blue-950/40 border-blue-800/40' },
  FEEDBACK:   { icon: MessageSquare,  color: 'text-purple-400',  label: 'Feedback',   bubbleBg: 'bg-purple-950/40 border-purple-800/40' },
  BUG_REPORT: { icon: Bug,           color: 'text-red-400',     label: 'Bug',        bubbleBg: 'bg-red-950/50 border-red-800/50' },
  BLOCKER:    { icon: AlertTriangle,  color: 'text-amber-400',   label: 'Blocker',    bubbleBg: 'bg-amber-950/50 border-amber-700/50' },
  NOTE:       { icon: StickyNote,     color: 'text-neutral-400',  label: 'Note',       bubbleBg: 'bg-neutral-900/60 border-neutral-800/50' },
};

// eslint-disable-next-line react-refresh/only-export-components
export function formatAge(isoString: string | null): string {
  if (!isoString) return '';
  const ms = Date.now() - parseTimestamp(isoString).getTime();
  const mins = Math.floor(ms / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatFromType(msg: ProjectMessage): string {
  const ft = (msg.from_type || '').toLowerCase();
  if (ft === 'agent') {
    const taskSlice = msg.from_task_id ? ` (${msg.from_task_id.slice(0, 8)})` : '';
    return `Agent${taskSlice}`;
  }
  if (ft === 'strategic') return 'Strategic Review';
  if (ft === 'system') return 'System';
  return 'You';
}

// eslint-disable-next-line react-refresh/only-export-components
export function isUserMessage(msg: ProjectMessage): boolean {
  return (msg.from_type || '').toLowerCase() === 'user';
}

function ContextBadges({ context, fromType }: { context: Record<string, unknown>; fromType: string }) {
  if ((fromType || '').toLowerCase() === 'user') return null;
  const badges: { label: string; className: string }[] = [];

  const severity = context.severity as string | undefined;
  if (severity === 'high') badges.push({ label: 'High', className: 'text-red-400 bg-red-500/10 border-red-500/30' });
  else if (severity === 'medium') badges.push({ label: 'Medium', className: 'text-amber-400 bg-amber-500/10 border-amber-500/30' });
  else if (severity === 'low') badges.push({ label: 'Low', className: 'text-neutral-500 bg-neutral-500/10 border-neutral-600/30' });

  const annotationType = context.annotation_type as string | undefined;
  if (annotationType === 'vision') badges.push({ label: 'Vision', className: 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30' });
  else if (annotationType === 'issue') badges.push({ label: 'Issue', className: 'text-red-400 bg-red-500/10 border-red-500/30' });
  else if (annotationType === 'comment') badges.push({ label: 'Comment', className: 'text-neutral-400 bg-neutral-500/10 border-neutral-600/30' });

  const taskType = context.task_type as string | undefined;
  if (taskType) badges.push({ label: taskType, className: 'text-neutral-500 bg-neutral-500/10 border-neutral-600/30' });

  const source = context.source as string | undefined;
  if (source === 'research_zero_children') badges.push({ label: 'No findings', className: 'text-amber-400 bg-amber-500/10 border-amber-500/30' });

  const eventType = context.event_type as string | undefined;
  if (eventType && ESCALATION_BADGE_LABELS[eventType]) {
    badges.push({ label: ESCALATION_BADGE_LABELS[eventType], className: 'text-red-400 bg-red-500/10 border-red-500/30 font-semibold' });
  }
  else if (eventType === 'pr_merged') badges.push({ label: 'PR Merged', className: 'text-green-400 bg-green-500/10 border-green-500/30' });
  else if (eventType === 'direction_milestone') badges.push({ label: (context.milestone as string) || 'Milestone', className: 'text-cyan-400 bg-cyan-500/10 border-cyan-500/30' });
  else if (eventType === 'cycle_summary') badges.push({ label: 'Cycle Summary', className: 'text-blue-400 bg-blue-500/10 border-blue-500/30' });
  else if (eventType && HIGH_SIGNAL_NOTE_BADGE_LABELS[eventType]) badges.push({ label: HIGH_SIGNAL_NOTE_BADGE_LABELS[eventType], className: 'text-red-400 bg-red-500/10 border-red-500/30 font-semibold' });
  else if (eventType === 'feedback_reply') badges.push({ label: 'Feedback Reply', className: 'text-purple-400 bg-purple-500/10 border-purple-500/30' });

  if (badges.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1 mt-1.5">
      {badges.map((b, i) => (
        <span key={i} className={`text-[11px] px-1.5 py-0.5 rounded border ${b.className}`}>{b.label}</span>
      ))}
    </div>
  );
}

export interface MessageBubbleProps {
  msg: ProjectMessage;
  faded?: boolean;
  readOnly?: boolean;
  allMessages?: ProjectMessage[];
  resolvingId: string | null;
  resolveInput: string;
  deletingIds: Set<string>;
  creatingDirectionIds: Set<string>;
  investigatingId: string | null;
  investigatingIds: Set<string>;
  selectMode: boolean;
  selectedIds: Set<string>;
  onOpenTask?: (taskId: string) => void;
  onToggleSelect: (msgId: string) => void;
  onCreateDirection: (msg: ProjectMessage) => void;
  onCreateInvestigation: (msg: ProjectMessage) => void;
  onResolve: (msgId: string, resolution: string) => void;
  onDelete: (msgId: string) => void;
  onStartResolve: (msgId: string) => void;
  onCancelResolve: () => void;
  onSetResolveInput: (value: string) => void;
  onStartInvestigate: (msgId: string) => void;
  onCancelInvestigate: () => void;
  onReply: (replyTo: { id: string; content: string; from: string }) => void;
}

export function MessageBubble({
  msg,
  faded = false,
  readOnly = false,
  allMessages = [],
  resolvingId,
  resolveInput,
  deletingIds,
  creatingDirectionIds,
  investigatingId,
  investigatingIds,
  selectMode,
  selectedIds,
  onOpenTask,
  onToggleSelect,
  onCreateDirection,
  onCreateInvestigation,
  onResolve,
  onDelete,
  onStartResolve,
  onCancelResolve,
  onSetResolveInput,
  onStartInvestigate,
  onCancelInvestigate,
  onReply,
}: MessageBubbleProps) {
  const config = TYPE_CONFIG[msg.message_type] || TYPE_CONFIG.NOTE;
  const Icon = config.icon;
  const userMsg = isUserMessage(msg);
  const isResolving = resolvingId === msg.id;

  let replyContextMsg: ProjectMessage | null = null;
  if (msg.message_type === 'NOTE' &&
      msg.context.event_type === 'feedback_acknowledgment' &&
      typeof msg.context.reply_to === 'string') {
    replyContextMsg = allMessages.find(m => m.id === msg.context.reply_to) || null;
  }

  return (
    <div data-testid="message-bubble" className={`flex ${userMsg ? 'justify-end' : 'justify-start'} ${faded ? 'opacity-75' : ''}`}>
      <div className={`max-w-[85%] md:max-w-[70%] ${userMsg ? 'items-end' : 'items-start'} flex flex-col`}>
        {/* Bubble */}
        <div className={`rounded-2xl border px-4 py-3 ${config.bubbleBg} ${
          msg.message_type === 'BLOCKER' ? 'border-amber-600/60 ring-1 ring-amber-500/20' :
          msg.message_type === 'BUG_REPORT' ? 'border-red-700/60' : ''
        }`}>
          {/* Header line */}
          <div className="flex items-center gap-2 mb-1">
            <Icon className={`w-3.5 h-3.5 shrink-0 ${config.color}`} />
            <span className={`text-[11px] font-semibold ${config.color}`}>{config.label}</span>
            <span className="text-[11px] text-neutral-600">
              {formatFromType(msg)}
              {msg.from_task_id && onOpenTask && (
                <button
                  onClick={() => onOpenTask(msg.from_task_id!)}
                  className="text-blue-400 hover:text-blue-300 transition-colors ml-1"
                  title={`Open task ${msg.from_task_id}`}
                >
                  ({msg.from_task_id.slice(0, 8)})
                </button>
              )}
              <span> · {formatAge(msg.created_at)}</span>
            </span>
            <span className="flex-1" />
            <CopyableMessageRef msgId={msg.id} />
          </div>
          {/* Reply context for feedback acknowledgment */}
          {replyContextMsg && (
            <div className="mb-1.5 px-2 py-1 border-l-2 border-neutral-700/50 rounded-r bg-neutral-800/30">
              <p className="text-[11px] text-neutral-500 mb-0.5">In reply to your feedback:</p>
              <p className="text-[11px] text-neutral-400 truncate">{replyContextMsg.content}</p>
            </div>
          )}
          {/* Reply quote (if this message is a reply) */}
          {typeof msg.context.reply_preview === 'string' && (
            <div className="mb-1.5 px-2 py-1 border-l-2 border-neutral-700/50 rounded-r bg-neutral-800/30">
              <p className="text-[11px] text-neutral-500 truncate">{msg.context.reply_preview}</p>
            </div>
          )}
          {/* Content */}
          <p className="text-sm text-neutral-200 leading-relaxed whitespace-pre-wrap">{msg.content}</p>
          {/* Context badges */}
          <ContextBadges context={msg.context} fromType={msg.from_type} />
        </div>

        {/* Linked indicator (for linked messages) */}
        {msg.status === 'linked' && (
          <div className={`mt-1 ${userMsg ? 'mr-3' : 'ml-3'} px-3 py-1.5 rounded-lg bg-indigo-800/30 border border-indigo-700/40`}>
            <p className="text-[11px] text-neutral-400">
              <span className="text-indigo-400 font-medium">Being addressed</span>
              {typeof msg.context._convergence_link === 'object' && msg.context._convergence_link && (() => {
                const link = msg.context._convergence_link as Record<string, unknown>;
                const kind = link.kind as string | undefined;
                const id = link.id as string | undefined;
                const kindLabel = kind === 'direction' ? 'direction' : kind === 'task' ? 'task' : 'item';
                return id ? (
                  <>
                    {' in '}
                    <span className="text-indigo-300 font-medium">{kindLabel}</span>
                    {' '}
                    <code className="text-indigo-200 bg-indigo-950/50 px-1 py-0.5 rounded text-[10px]">
                      {id.slice(0, 8)}
                    </code>
                  </>
                ) : null;
              })()}
            </p>
          </div>
        )}

        {/* Resolution (for resolved messages) */}
        {msg.status === 'resolved' && msg.resolution && (
          <div className={`mt-1 ${userMsg ? 'mr-3' : 'ml-3'} px-3 py-1.5 rounded-lg bg-neutral-800/50 border border-neutral-700/30`}>
            <p className="text-[11px] text-neutral-500">
              <span className="text-emerald-500/70 font-medium">Resolved:</span> {msg.resolution}
            </p>
          </div>
        )}

        {/* Action buttons (open messages only, not faded, not read-only grouped rows, not linked) */}
        {!faded && !readOnly && msg.status === 'open' && (
          <div className={`flex items-center gap-1 mt-1 ${userMsg ? 'self-end mr-1' : 'self-start ml-1'}`}>
            {selectMode && msg.message_type === 'FEEDBACK' && (
              <button
                onClick={() => onToggleSelect(msg.id)}
                className={`p-2.5 md:p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center ${
                  selectedIds.has(msg.id) ? 'text-purple-400 bg-purple-500/20' : 'text-neutral-600 hover:text-neutral-400'
                }`}
              >
                <CheckSquare className="w-3.5 h-3.5" />
              </button>
            )}
            <button
              onClick={() => onReply({ id: msg.id, content: msg.content, from: formatFromType(msg) })}
              title="Reply"
              className="text-neutral-600 hover:text-blue-400 p-2.5 md:p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
            >
              <Reply className="w-3.5 h-3.5" />
            </button>
            {msg.message_type === 'FEEDBACK' && !selectMode && (
              <button
                onClick={() => onCreateDirection(msg)}
                disabled={creatingDirectionIds.has(msg.id)}
                title="Create Direction"
                className="text-purple-500/70 hover:text-purple-400 p-2.5 md:p-1.5 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <PlusCircle className="w-3.5 h-3.5" />
              </button>
            )}
            {(msg.message_type === 'BLOCKER' || msg.message_type === 'BUG_REPORT') && investigatingId !== msg.id && !investigatingIds.has(msg.id) && (
              <button
                onClick={() => onStartInvestigate(msg.id)}
                disabled={investigatingIds.has(msg.id)}
                title="Investigate"
                className="text-amber-500/70 hover:text-amber-400 p-2.5 md:p-1.5 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <AlertTriangle className="w-3.5 h-3.5" />
              </button>
            )}
            {investigatingId === msg.id && (
              <div className="flex items-center gap-1 bg-neutral-900 border border-neutral-700 rounded-lg px-2 py-1 ml-1">
                <span className="text-xs text-neutral-400 whitespace-nowrap">Spawn investigation?</span>
                <button
                  onClick={() => onCreateInvestigation(msg)}
                  disabled={investigatingIds.has(msg.id)}
                  className="text-amber-400 hover:text-amber-300 p-1 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                  title="Confirm investigation"
                >
                  <Check className="w-3 h-3" />
                </button>
                <button
                  onClick={onCancelInvestigate}
                  className="text-neutral-500 hover:text-neutral-400 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                  title="Cancel"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            )}
            {!isResolving ? (
              <button
                onClick={() => onStartResolve(msg.id)}
                title="Resolve"
                className="text-emerald-500/70 hover:text-emerald-400 p-2.5 md:p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <Check className="w-3.5 h-3.5" />
              </button>
            ) : (
              <div className="flex items-center gap-1 bg-neutral-900 border border-neutral-700 rounded-lg px-2 py-1 ml-1">
                <input
                  type="text"
                  value={resolveInput}
                  onChange={e => onSetResolveInput(e.target.value)}
                  onKeyDown={e => { if (e.key === 'Enter') onResolve(msg.id, resolveInput); if (e.key === 'Escape') onCancelResolve(); }}
                  placeholder="Note (optional)..."
                  className="bg-transparent text-neutral-300 text-xs focus:outline-none w-36 md:w-48 placeholder-neutral-600"
                  autoFocus
                />
                <button
                  onClick={() => onResolve(msg.id, resolveInput)}
                  className="text-emerald-400 hover:text-emerald-300 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                  title="Confirm resolve"
                >
                  <Check className="w-3 h-3" />
                </button>
                <button
                  onClick={onCancelResolve}
                  className="text-neutral-500 hover:text-neutral-400 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                  title="Cancel"
                >
                  <X className="w-3 h-3" />
                </button>
              </div>
            )}
            <button
              onClick={() => onDelete(msg.id)}
              disabled={deletingIds.has(msg.id)}
              title="Delete"
              className="text-neutral-600 hover:text-red-400 p-2.5 md:p-1.5 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
