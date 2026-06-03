import { type RefObject } from 'react';
import { X, Send, Reply } from 'lucide-react';
import { MessageTypeDropdown } from './MessageTypeDropdown';
import { type ArmingOutcome, composerHintCopy } from './messageArming';

export interface MessagesComposerProps {
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  newType: string;
  newContent: string;
  submitting: boolean;
  replyTo: { id: string; content: string; from: string } | null;
  /** Outcome the server reported for the last send, or null before any send. */
  lastArming?: ArmingOutcome | null;
  /** Whether a strategic cycle is currently running (live WS-driven signal). */
  strategistBusy?: boolean;
  onChangeType: (value: string) => void;
  onChangeContent: (value: string) => void;
  onCancelReply: () => void;
  onSubmit: () => void;
}

/**
 * Bottom compose area for MessagesPanel: the reply-quote bar, the message-type
 * dropdown, the auto-resizing textarea, and the send button. Purely
 * presentational — all message state, the textarea ref, and the submit handler
 * stay owned by MessagesPanel and are wired through props. Behaviour is
 * identical to the previously-inline markup (Enter submits, Shift+Enter inserts
 * a newline, the textarea auto-resizes up to ~3 rows / 96px).
 */
export function MessagesComposer({
  textareaRef,
  newType,
  newContent,
  submitting,
  replyTo,
  lastArming,
  strategistBusy,
  onChangeType,
  onChangeContent,
  onCancelReply,
  onSubmit,
}: MessagesComposerProps) {
  // Auto-resize textarea — max 3 rows (~96px).
  function handleTextareaInput() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 96)}px`;
  }

  // Hint reflects the server's real decision for the last send when available
  // (the only source that accounts for routine/system event exclusions),
  // falling back to the type-based prediction before any message has been sent —
  // and, when the strategist is mid-cycle, flagging that a steer is queued rather
  // than acting immediately.
  const hintText = composerHintCopy(newType, lastArming, strategistBusy);

  return (
    <div className="px-4 md:px-6 py-3 border-t border-neutral-800 shrink-0 bg-neutral-950/50">
      {/* Reply quote bar */}
      {replyTo && (
        <div className="flex items-center gap-2 mb-2 px-3 py-1.5 bg-neutral-800/60 border-l-2 border-blue-500/50 rounded-r-lg">
          <Reply className="w-3 h-3 text-blue-400 shrink-0" />
          <span className="text-xs text-neutral-400 truncate flex-1">
            Replying to <span className="text-neutral-300">{replyTo.from}</span>: {replyTo.content.slice(0, 80)}{replyTo.content.length > 80 ? '...' : ''}
          </span>
          <button onClick={onCancelReply} className="text-neutral-500 hover:text-neutral-300 p-0.5 shrink-0 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center">
            <X className="w-3 h-3" />
          </button>
        </div>
      )}
      <div className="flex gap-2 items-end">
        <MessageTypeDropdown value={newType} onChange={onChangeType} />
        <div className="flex-1 relative">
          <textarea
            ref={textareaRef}
            value={newContent}
            onChange={e => { onChangeContent(e.target.value); handleTextareaInput(); }}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSubmit(); }
            }}
            placeholder="Type a message..."
            rows={1}
            className="w-full bg-neutral-900/50 border border-neutral-800/50 text-neutral-100 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 placeholder-neutral-600 resize-none min-h-[44px] leading-relaxed"
          />
        </div>
        <button
          onClick={onSubmit}
          disabled={submitting || !newContent.trim()}
          className="bg-blue-600 hover:bg-blue-500 text-white p-2.5 rounded-xl text-xs font-medium transition-colors disabled:opacity-40 shrink-0 min-h-[44px] min-w-[44px] flex items-center justify-center"
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
      {/* Steering hint: grounded in the last send's real outcome when available,
          otherwise predicted from the selected type. */}
      <p className="text-[11px] leading-snug text-neutral-500 mt-1.5 px-0.5">
        {hintText}
      </p>
    </div>
  );
}
