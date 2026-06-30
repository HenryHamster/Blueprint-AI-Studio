import { type RefObject } from 'react';
import { Command, X, Send, Reply } from 'lucide-react';
import { MessageTypeDropdown } from './MessageTypeDropdown';
import { type ArmingOutcome, composerHintCopy } from './messageArming';
import type { StrategicContextDescriptorModel } from '../utils/strategicContextDescriptor';
import { StrategicContextDescriptor } from './StrategicContextDescriptor';

export interface MessagesComposerProps {
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  newType: string;
  newContent: string;
  submitting: boolean;
  replyTo: { id: string; content: string; from: string; message_type: string; status: string } | null;
  /** Outcome the server reported for the last send, or null before any send. */
  lastArming?: ArmingOutcome | null;
  /** Whether a strategic cycle is currently running (live WS-driven signal). */
  strategistBusy?: boolean;
  strategicContext?: StrategicContextDescriptorModel | null;
  onOpenStrategicContext?: () => void;
  onOpenCommandPalette?: () => void;
  onChangeType: (value: string) => void;
  onChangeContent: (value: string) => void;
  onCancelReply: () => void;
  onSubmit: () => void;
  chrome?: 'full' | 'minimal';
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
  strategicContext,
  onOpenStrategicContext,
  onOpenCommandPalette,
  onChangeType,
  onChangeContent,
  onCancelReply,
  onSubmit,
  chrome = 'full',
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
  const minimal = chrome === 'minimal';
  const minimalColumnClass = 'mx-auto w-full max-w-[940px] lg:mx-0';
  const hintArming = minimal && strategistBusy && lastArming !== 'not_armed' ? null : lastArming;
  const hintText = composerHintCopy(newType, hintArming, strategistBusy);
  const showHint = !minimal || Boolean(strategistBusy) || lastArming === 'not_armed';

  return (
    <div className={minimal
      ? 'shrink-0 border-t border-neutral-800 bg-[#05080c] px-2.5 py-2 md:px-3'
      : 'px-4 md:px-6 py-3 border-t border-neutral-800 shrink-0 bg-neutral-950/50'
    }>
      {minimal && strategicContext && (
        <StrategicContextDescriptor model={strategicContext} onOpen={onOpenStrategicContext} />
      )}
      {/* Reply quote bar */}
      {replyTo && (
        <div className={`${minimal ? minimalColumnClass : ''} mb-2 flex items-center gap-2 rounded-r-lg border-l-2 border-blue-500/50 bg-neutral-800/60 px-3 py-1.5`}>
          <Reply className="w-3 h-3 text-blue-400 shrink-0" />
          <span className="text-xs text-neutral-400 truncate flex-1">
            Replying to <span className="text-neutral-300">{replyTo.from}</span>: {replyTo.content.slice(0, 80)}{replyTo.content.length > 80 ? '...' : ''}
          </span>
          <button onClick={onCancelReply} className="text-neutral-500 hover:text-neutral-300 p-0.5 shrink-0 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center">
            <X className="w-3 h-3" />
          </button>
        </div>
      )}
      <div
        data-testid={minimal ? 'strategic-composer-box' : undefined}
        className={minimal
        ? `${minimalColumnClass} flex items-end gap-1.5 rounded-md border border-neutral-800 bg-[#10151c] p-1.5 shadow-inner shadow-black/20`
        : 'flex gap-2 items-end'
      }>
        {!minimal && (
          <MessageTypeDropdown value={newType} onChange={onChangeType} compact={minimal} />
        )}
        {minimal && onOpenCommandPalette && (
          <button
            type="button"
            onClick={onOpenCommandPalette}
            title="Open command palette (Ctrl+K)"
            aria-label="Open command palette"
            className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center gap-1 rounded-md border border-neutral-800 bg-[#0b0f14] px-2 text-xs font-semibold text-neutral-400 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-100"
          >
            <Command className="h-3.5 w-3.5" />
            <kbd className="hidden rounded border border-neutral-700 bg-neutral-950 px-1 py-0.5 font-mono text-ui-sm text-neutral-500 sm:inline-block">
              Ctrl+K
            </kbd>
          </button>
        )}
        <div className="relative flex-1">
          <textarea
            ref={textareaRef}
            value={newContent}
            onChange={e => { onChangeContent(e.target.value); handleTextareaInput(); }}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); onSubmit(); }
            }}
            placeholder={minimal ? 'Message Strategic...' : 'Type a message...'}
            rows={1}
            className={minimal
              ? 'min-h-[44px] w-full resize-none rounded-md border-0 bg-transparent px-2.5 py-2 text-sm leading-relaxed text-neutral-100 placeholder-neutral-500 outline-none'
              : 'w-full bg-neutral-900/50 border border-neutral-800/50 text-neutral-100 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 placeholder-neutral-600 resize-none min-h-[44px] leading-relaxed'
            }
          />
        </div>
        <button
          type="button"
          onClick={onSubmit}
          disabled={submitting || !newContent.trim()}
          aria-label="Send message"
          title="Send message"
          className={minimal
            ? 'flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md bg-emerald-600 text-white transition-colors hover:bg-emerald-500 disabled:opacity-40'
            : 'bg-blue-600 hover:bg-blue-500 text-white p-2.5 rounded-xl text-xs font-medium transition-colors disabled:opacity-40 shrink-0 min-h-[44px] min-w-[44px] flex items-center justify-center'
          }
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
      {/* Steering hint: grounded in the last send's real outcome when available,
          otherwise predicted from the selected type. */}
      {showHint && (
        <div className={minimal ? `${minimalColumnClass} mt-1.5 px-1` : ''}>
          <p className={`${minimal ? 'text-ui-md text-neutral-600' : 'text-ui-md text-neutral-500'} leading-snug ${minimal ? '' : 'mt-1.5 px-0.5'}`}>
            {hintText}
          </p>
        </div>
      )}
    </div>
  );
}
