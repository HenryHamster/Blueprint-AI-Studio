import { X, Check, AlertTriangle, HelpCircle, Bug, StickyNote, MessageSquare, PlusCircle, Trash2, CheckSquare, Reply, Copy, FileText } from 'lucide-react';
import { useState, useCallback, memo } from 'react';
import { parseTimestamp } from '../utils/time';
import { ESCALATION_BADGE_LABELS, HIGH_SIGNAL_NOTE_BADGE_LABELS } from './eventPrefs';
import { MarkdownContent } from './MarkdownContent';
import type { CitationState, MessageAttachment, ProjectMessage, ProjectMessageContext, ProvenanceWarnings } from '../types/messages';
import { buildMessageStrategicContextDescriptor } from '../utils/strategicContextDescriptor';
import { StrategicContextDescriptor } from './StrategicContextDescriptor';
import { AttachmentPreviewModal } from './AttachmentPreviewModal';

export type { ProjectMessage, ProjectMessageContext } from '../types/messages';

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
      // Compact secondary affordance in the dense message-metadata header row;
      // a 44px touch target would inflate every message's header on mobile.
      data-viewport-allow-small-target
      className="inline-flex items-center gap-1 font-mono text-ui-sm text-neutral-600 hover:text-neutral-400 transition-colors"
    >
      {copied ? <Check className="w-3 h-3 text-emerald-400" /> : <Copy className="w-3 h-3" />}
      {shortRef}
    </button>
  );
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
  if (ft === 'strategic' || ft === 'strategist') return 'Strategic Review';
  if (ft === 'system') return 'System';
  return 'You';
}

function formatChatFromType(msg: ProjectMessage): string {
  const ft = (msg.from_type || '').toLowerCase();
  if (ft === 'strategic' || ft === 'strategist') return 'Strategic';
  if (ft === 'system') return 'System';
  if (ft === 'agent') return 'Agent';
  return 'You';
}

// eslint-disable-next-line react-refresh/only-export-components
export function isUserMessage(msg: ProjectMessage): boolean {
  const ft = (msg.from_type || '').toLowerCase();
  return ft === 'user' || ft === 'operator';
}

function asStringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((entry): entry is string => typeof entry === 'string' && entry.length > 0) : [];
}

function getProvenanceWarnings(context: ProjectMessageContext): ProvenanceWarnings | null {
  const raw = context._provenance_warnings;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const citationState = raw.citation_state;
  return {
    citation_state: citationState === 'absent' || citationState === 'present' || citationState === 'indeterminate' ? citationState : undefined,
    broken_refs: asStringArray(raw.broken_refs),
    ungrounded_refs: asStringArray(raw.ungrounded_refs),
  };
}

// Per-state presentation for the citation provenance badge. Each state mirrors the
// reproduce-on-HEAD tri-state semantics from classify_reproduction:
//   absent        → nothing cited survives on HEAD (fully stale)
//   present       → at least one citation survives but some refs are broken/ungrounded (partially grounded)
//   indeterminate → no validation backend could verify the citations (unverifiable)
const CITATION_BADGE_CONFIG: Record<CitationState, { label: string; container: string; labelText: string; refText: string }> = {
  absent: {
    label: 'cited code missing on HEAD',
    container: 'border-amber-500/30 bg-amber-500/10',
    labelText: 'text-amber-300',
    refText: 'text-amber-100/80',
  },
  present: {
    label: 'some cited code missing on HEAD',
    container: 'border-amber-500/20 bg-amber-500/5',
    labelText: 'text-amber-200',
    refText: 'text-amber-100/70',
  },
  indeterminate: {
    label: 'citations could not be verified',
    container: 'border-neutral-500/30 bg-neutral-500/10',
    labelText: 'text-neutral-300',
    refText: 'text-neutral-200/80',
  },
};

function AttachmentThumbnails({ attachments, onPreview }: { attachments: MessageAttachment[]; onPreview: (att: MessageAttachment) => void }) {
  if (!attachments.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {attachments.map((att, i) => {
        if (att.mime_type?.startsWith('image/') && att.data) {
          return (
            <button
              key={i}
              type="button"
              onClick={() => onPreview(att)}
              className="cursor-pointer border-0 bg-transparent p-0 leading-none"
              aria-label={`Preview ${att.name || 'image'}`}
              data-viewport-allow-small-target
            >
              <img
                src={`data:${att.mime_type};base64,${att.data}`}
                alt={att.name || 'Image attachment'}
                className="max-h-48 max-w-full rounded border border-neutral-700/60 object-contain"
              />
            </button>
          );
        }
        return (
          <button
            key={i}
            type="button"
            onClick={() => onPreview(att)}
            className="inline-flex cursor-pointer items-center gap-1.5 rounded border border-neutral-700/60 bg-neutral-800/40 px-2.5 py-1.5 text-xs text-neutral-300 hover:border-neutral-500/60 transition-colors"
            aria-label={`Preview ${att.name || att.mime_type || 'File'}`}
          >
            <FileText className="h-3.5 w-3.5 shrink-0 text-neutral-500" />
            <span className="max-w-[200px] truncate">{att.name || att.mime_type || 'File'}</span>
            {att.size_bytes != null && (
              <span className="text-neutral-500 shrink-0">({att.size_bytes < 1024 ? '<1' : (att.size_bytes / 1024).toFixed(0)} KB)</span>
            )}
          </button>
        );
      })}
    </div>
  );
}

function StaleCitationBadge({ msg }: { msg: ProjectMessage }) {
  if (msg.message_type !== 'BUG_REPORT' && msg.message_type !== 'BLOCKER') return null;
  const warnings = getProvenanceWarnings(msg.context);
  const state = warnings?.citation_state;
  if (!state) return null;

  const refs = [...new Set([...(warnings.broken_refs ?? []), ...(warnings.ungrounded_refs ?? [])])];
  const refSummary = refs.length > 0 ? refs.join(', ') : null;

  // A partially grounded report only warrants a badge when there is something
  // broken/ungrounded to inspect; a clean 'present' state stays quiet.
  if (state === 'present' && !refSummary) return null;

  const cfg = CITATION_BADGE_CONFIG[state];
  return (
    <div className={`mt-2 inline-flex max-w-full flex-col gap-1 rounded-xl border px-2.5 py-2 ${cfg.container}`}>
      <span className={`inline-flex items-center gap-1 text-ui-md font-semibold ${cfg.labelText}`}>
        <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
        {cfg.label}
      </span>
      {refSummary && (
        <span className={`text-ui-md leading-relaxed break-words ${cfg.refText}`}>
          {refSummary}
        </span>
      )}
    </div>
  );
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
        <span key={i} className={`text-ui-md px-1.5 py-0.5 rounded border ${b.className}`}>{b.label}</span>
      ))}
    </div>
  );
}

export interface MessageBubbleProps {
  msg: ProjectMessage;
  faded?: boolean;
  readOnly?: boolean;
  allMessages?: ProjectMessage[];
  presentation?: 'triage' | 'chat';
  highlighted?: boolean;
  actionMode?: 'full' | 'reply-only' | 'none';
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
  onReply: (replyTo: { id: string; content: string; from: string; message_type: string; status: string }) => void;
  onOpenStrategicContext?: () => void;
}

export const MessageBubble = memo(function MessageBubble({
  msg,
  faded = false,
  readOnly = false,
  allMessages = [],
  presentation = 'triage',
  highlighted = false,
  actionMode = 'full',
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
  onOpenStrategicContext,
}: MessageBubbleProps) {
  const [previewAttachment, setPreviewAttachment] = useState<MessageAttachment | null>(null);
  const config = TYPE_CONFIG[msg.message_type] || TYPE_CONFIG.NOTE;
  const Icon = config.icon;
  const userMsg = isUserMessage(msg);
  const chatMode = presentation === 'chat';
  const showOpsChrome = !chatMode;
  const fromType = (msg.from_type || '').toLowerCase();
  const replyableChatAuthor = fromType === 'agent' || fromType === 'strategic' || fromType === 'strategist';
  const canChatReply = chatMode && replyableChatAuthor && !faded && !readOnly && msg.status === 'open' && actionMode !== 'none';
  const isResolving = resolvingId === msg.id;
  const taskId = msg.from_task_id;
  const strategicContextReceipt = chatMode && userMsg
    ? buildMessageStrategicContextDescriptor(msg.context)
    : null;

  let replyContextMsg: ProjectMessage | null = null;
  let replyContextType: 'feedback' | 'question' | null = null;
  if (msg.message_type === 'NOTE' &&
      typeof msg.context.reply_to === 'string') {
    if (msg.context.event_type === 'feedback_acknowledgment') {
      replyContextMsg = allMessages.find(m => m.id === msg.context.reply_to) || null;
      replyContextType = replyContextMsg ? 'feedback' : null;
    } else if (msg.context.event_type === 'question_answer') {
      replyContextMsg = allMessages.find(m => m.id === msg.context.reply_to) || null;
      replyContextType = replyContextMsg ? 'question' : null;
    }
  }

  return (
    <>
    <div
      data-testid="message-bubble"
      data-message-id={msg.id}
      className={`group flex scroll-mt-24 rounded-md transition-colors ${userMsg ? 'justify-end' : 'justify-start'} ${faded ? 'opacity-75' : ''} ${highlighted ? 'bg-amber-400/5 ring-1 ring-amber-300/35' : ''}`}
    >
      <div className={`${
        chatMode
          ? userMsg
            ? 'max-w-[86%] md:max-w-[72%]'
            : 'w-full max-w-[92%] md:max-w-[76%]'
          : 'max-w-[85%] md:max-w-[70%]'
      } ${userMsg ? 'items-end' : 'items-start'} flex flex-col`}>
        {chatMode && !userMsg && (
          <div className="relative mb-0.5 flex items-center gap-1.5 px-0.5 text-ui-md text-neutral-500">
            <span className="font-medium text-neutral-400">{formatChatFromType(msg)}</span>
            <span aria-hidden="true">·</span>
            <span>{formatAge(msg.created_at)}</span>
            {taskId && (
              <>
                <span aria-hidden="true">·</span>
                <span
                  className="font-mono text-ui-md text-neutral-600"
                  title={`Task ${taskId}`}
                >
                  {taskId.slice(0, 8)}
                </span>
              </>
            )}
            {canChatReply && (
              <button
                onClick={() => onReply({ id: msg.id, content: msg.content, from: formatChatFromType(msg), message_type: msg.message_type, status: msg.status })}
                title="Reply"
                className="absolute -right-1 -top-3 flex min-h-[44px] min-w-[44px] items-center justify-center rounded p-2 text-neutral-600 opacity-40 transition-colors hover:text-neutral-300 focus-visible:opacity-100 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-neutral-600 md:opacity-0 md:group-hover:opacity-100"
              >
                <Reply className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
        )}
        {/* Bubble */}
        <div className={chatMode
          ? userMsg
            ? 'rounded-md border border-neutral-700/60 bg-[#10161f] px-3 py-2 shadow-[0_1px_0_rgba(255,255,255,0.035)]'
            : 'rounded-md border border-transparent bg-transparent px-0.5 py-0.5'
          : `rounded-2xl border px-4 py-3 ${config.bubbleBg} ${
            msg.message_type === 'BLOCKER' ? 'border-amber-600/60 ring-1 ring-amber-500/20' :
            msg.message_type === 'BUG_REPORT' ? 'border-red-700/60' : ''
          }`
        }>
          {/* Header line */}
          {showOpsChrome && (
          <div className="flex items-center gap-2 mb-1">
            <Icon className={`w-3.5 h-3.5 shrink-0 ${config.color}`} />
            <span className={`text-ui-md font-semibold ${config.color}`}>{config.label}</span>
            <span className="text-ui-md text-neutral-600">
              {formatFromType(msg)}
              {taskId && onOpenTask && (
                <button
                  onClick={() => onOpenTask(taskId)}
                  className="text-blue-400 hover:text-blue-300 transition-colors ml-1"
                  title={`Open task ${taskId}`}
                >
                  ({taskId.slice(0, 8)})
                </button>
              )}
              <span> · {formatAge(msg.created_at)}</span>
            </span>
            <span className="flex-1" />
            {actionMode === 'full' && <CopyableMessageRef msgId={msg.id} />}
          </div>
          )}
          {/* Reply context for feedback acknowledgment and question answer */}
          {replyContextMsg && (
            <div className="mb-1 px-2 py-1 border-l-2 border-neutral-700/50 rounded-r bg-neutral-800/30">
              <p className="text-ui-md text-neutral-500 mb-0.5">
                {replyContextType === 'question' ? 'Answering your question:' : 'In reply to your feedback:'}
              </p>
              <p className="text-ui-md text-neutral-400 truncate">{replyContextMsg.content}</p>
            </div>
          )}
          {/* Reply quote (if this message is a reply) */}
          {typeof msg.context.reply_preview === 'string' && (
            <div className="mb-1 px-2 py-1 border-l-2 border-neutral-700/50 rounded-r bg-neutral-800/30">
              <p className="text-ui-md text-neutral-500 truncate">{msg.context.reply_preview}</p>
            </div>
          )}
          {/* Content */}
          {userMsg ? (
            <div className={chatMode ? 'text-sm text-neutral-100 leading-relaxed' : 'text-sm text-neutral-200 leading-relaxed'}>
              <MarkdownContent content={msg.content} />
            </div>
          ) : (
            <div className={chatMode ? 'text-sm text-neutral-100 leading-relaxed' : 'text-sm text-neutral-200 leading-relaxed'}>
              <MarkdownContent content={msg.content} />
            </div>
          )}
          {msg.context.attachments && Array.isArray(msg.context.attachments) && msg.context.attachments.length > 0 && (
            <AttachmentThumbnails attachments={msg.context.attachments} onPreview={setPreviewAttachment} />
          )}
          {showOpsChrome && <StaleCitationBadge msg={msg} />}
          {/* Context badges */}
          {showOpsChrome && <ContextBadges context={msg.context} fromType={msg.from_type} />}
          {strategicContextReceipt && (
            <div className="mt-1.5 border-t border-neutral-700/50 pt-1.5">
              <StrategicContextDescriptor
                model={strategicContextReceipt}
                variant="receipt"
                onOpen={onOpenStrategicContext}
              />
            </div>
          )}
        </div>

        {/* Linked indicator (for linked messages) */}
        {showOpsChrome && msg.status === 'linked' && (
          <div className={`mt-1 ${userMsg ? 'mr-3' : 'ml-3'} px-3 py-1.5 rounded-lg bg-indigo-800/30 border border-indigo-700/40`}>
            <p className="text-ui-md text-neutral-400">
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
                    <code className="text-indigo-200 bg-indigo-950/50 px-1 py-0.5 rounded text-ui-sm">
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
          <div className={`mt-1 ${userMsg ? 'mr-2' : 'ml-2'} px-2.5 py-1 ${chatMode ? 'rounded border border-neutral-800/70 bg-neutral-900/40' : 'rounded-lg bg-neutral-800/50 border border-neutral-700/30'}`}>
            <p className="text-ui-md text-neutral-500">
              <span className="text-emerald-500/70 font-medium">Resolved:</span> {msg.resolution}
            </p>
          </div>
        )}

        {/* Action buttons (open messages only, not faded, not read-only grouped rows, not linked) */}
        {!chatMode && !faded && !readOnly && msg.status === 'open' && actionMode !== 'none' && (
          <div className={`flex items-center gap-1 mt-1 ${userMsg ? 'self-end mr-1' : 'self-start ml-1'}`}>
            {selectMode && actionMode === 'full' && msg.message_type === 'FEEDBACK' && (
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
              onClick={() => onReply({ id: msg.id, content: msg.content, from: formatFromType(msg), message_type: msg.message_type, status: msg.status })}
              title="Reply"
              className="text-neutral-600 hover:text-blue-400 p-2.5 md:p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
            >
              <Reply className="w-3.5 h-3.5" />
            </button>
            {actionMode === 'full' && msg.message_type === 'FEEDBACK' && !selectMode && (
              <button
                onClick={() => onCreateDirection(msg)}
                disabled={creatingDirectionIds.has(msg.id)}
                title="Create Direction"
                className="text-purple-500/70 hover:text-purple-400 p-2.5 md:p-1.5 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <PlusCircle className="w-3.5 h-3.5" />
              </button>
            )}
            {actionMode === 'full' && (msg.message_type === 'BLOCKER' || msg.message_type === 'BUG_REPORT') && investigatingId !== msg.id && !investigatingIds.has(msg.id) && (
              <button
                onClick={() => onStartInvestigate(msg.id)}
                disabled={investigatingIds.has(msg.id)}
                title="Investigate"
                className="text-amber-500/70 hover:text-amber-400 p-2.5 md:p-1.5 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <AlertTriangle className="w-3.5 h-3.5" />
              </button>
            )}
            {actionMode === 'full' && investigatingId === msg.id && (
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
            {actionMode === 'full' && (!isResolving ? (
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
                  onKeyDown={e => { if (e.key === 'Enter' && (msg.message_type !== 'QUESTION' || resolveInput.trim())) onResolve(msg.id, resolveInput); if (e.key === 'Escape') onCancelResolve(); }}
                  placeholder={msg.message_type === 'QUESTION' ? 'Your answer (required)...' : 'Note (optional)...'}
                  className="bg-transparent text-neutral-300 text-xs focus:outline-none w-36 md:w-48 placeholder-neutral-600"
                  autoFocus
                />
                <button
                  onClick={() => onResolve(msg.id, resolveInput)}
                  disabled={msg.message_type === 'QUESTION' && !resolveInput.trim()}
                  className="text-emerald-400 hover:text-emerald-300 p-1 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
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
            ))}
            {actionMode === 'full' && (
            <button
              onClick={() => onDelete(msg.id)}
              disabled={deletingIds.has(msg.id)}
              title="Delete"
              className="text-neutral-600 hover:text-red-400 p-2.5 md:p-1.5 rounded transition-colors disabled:opacity-30 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
            >
              <Trash2 className="w-3.5 h-3.5" />
            </button>
            )}
          </div>
        )}
      </div>
    </div>
    {previewAttachment && (
      <AttachmentPreviewModal
        attachment={previewAttachment}
        open={true}
        onClose={() => setPreviewAttachment(null)}
      />
    )}
    </>
  );
});
