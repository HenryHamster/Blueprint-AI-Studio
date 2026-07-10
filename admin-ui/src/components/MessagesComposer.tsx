import { type RefObject, useRef, useState } from 'react';
import { Command, X, Send, Reply, Paperclip, FileText } from 'lucide-react';
import { MessageTypeDropdown } from './MessageTypeDropdown';
import { type ArmingOutcome, composerHintCopy } from './messageArming';
import type { StrategicContextDescriptorModel } from '../utils/strategicContextDescriptor';
import { StrategicContextDescriptor } from './StrategicContextDescriptor';
import { MESSAGE_CONTENT_MAX } from '../api';
import type { MessageAttachment } from '../types/messages';

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
  starterPrompts?: string[];
  capabilityHint?: string | null;
  disabledReason?: string | null;
  onPickStarterPrompt?: (value: string) => void;
  onOpenStrategicContext?: () => void;
  onOpenCommandPalette?: () => void;
  onChangeType: (value: string) => void;
  onChangeContent: (value: string) => void;
  onCancelReply: () => void;
  onSubmit: () => void;
  /** Pending file attachments to show as preview chips before sending. */
  pendingAttachments?: MessageAttachment[];
  /** Called when the user picks, drops, or pastes files. */
  onAttachFiles?: (files: File[]) => void;
  /** Called when the user removes a pending attachment by index. */
  onRemoveAttachment?: (index: number) => void;
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
  starterPrompts,
  capabilityHint,
  disabledReason,
  onOpenStrategicContext,
  onOpenCommandPalette,
  onChangeType,
  onChangeContent,
  onCancelReply,
  onSubmit,
  onPickStarterPrompt,
  chrome = 'full',
  pendingAttachments = [],
  onAttachFiles,
  onRemoveAttachment,
}: MessagesComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragOver, setDragOver] = useState(false);

  function handleFileSelect(e: React.ChangeEvent<HTMLInputElement>) {
    if (e.target.files?.length && onAttachFiles) onAttachFiles(Array.from(e.target.files));
    e.target.value = '';
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    if (!onAttachFiles) return;
    const files = Array.from(e.clipboardData?.items || [])
      .filter(i => i.kind === 'file')
      .map(i => i.getAsFile())
      .filter(Boolean) as File[];
    if (files.length) { e.preventDefault(); onAttachFiles(files); }
  }

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(true);
  }

  function handleDragLeave(e: React.DragEvent) {
    // Only clear the drag-over highlight when the cursor actually leaves the
    // drop-zone container — not when it moves from the container to a child
    // element (textarea, button, etc.) inside it.
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setDragOver(false);
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    if (e.dataTransfer.files.length && onAttachFiles) {
      onAttachFiles(Array.from(e.dataTransfer.files));
    }
  }

  // Auto-resize textarea — max 3 rows (~96px).
  function handleTextareaInput() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 96)}px`;
  }

  const overLimit = newContent.length > MESSAGE_CONTENT_MAX;
  const nearLimit = newContent.length > MESSAGE_CONTENT_MAX * 0.8;

  // Hint reflects the server's real decision for the last send when available
  // (the only source that accounts for routine/system event exclusions),
  // falling back to the type-based prediction before any message has been sent —
  // and, when the strategist is mid-cycle, flagging that a steer is queued rather
  // than acting immediately.
  const minimal = chrome === 'minimal';
  const minimalColumnClass = 'mx-auto w-full max-w-[940px]';
  const composerDisabled = Boolean(disabledReason);
  const hintArming = minimal && strategistBusy && lastArming !== 'not_armed' ? null : lastArming;
  const hintText = composerHintCopy(newType, hintArming, strategistBusy);
  const showHint = Boolean(disabledReason) || !minimal || Boolean(strategistBusy) || lastArming === 'not_armed';

  return (
    <div className={minimal
      ? 'shrink-0 border-t border-workbench-border bg-workbench-base px-2 pb-2 pt-1.5 md:px-2.5'
      : 'px-4 md:px-6 py-3 border-t border-neutral-800 shrink-0 bg-neutral-950/50'
    }>
      {minimal && strategicContext && (
        <StrategicContextDescriptor model={strategicContext} onOpen={onOpenStrategicContext} />
      )}
      {minimal && starterPrompts && starterPrompts.length > 0 && !newContent && (
        <div className={`${minimalColumnClass} mb-1.5 flex flex-wrap gap-1.5 px-1`}>
          {starterPrompts.map(prompt => (
            <button
              key={prompt}
              type="button"
              onClick={() => onPickStarterPrompt?.(prompt)}
              className="rounded-full border border-neutral-800 bg-neutral-950/60 px-2.5 py-1 text-xs text-neutral-300 transition-colors hover:border-neutral-600 hover:bg-neutral-900"
            >
              {prompt}
            </button>
          ))}
        </div>
      )}
      {minimal && !newContent && capabilityHint && (
        <p className={`${minimalColumnClass} mb-1 px-1 text-ui-md text-neutral-600 leading-snug`}>
          {capabilityHint}
        </p>
      )}
      {/* Reply quote bar */}
      {replyTo && (
        <div className={`${minimal ? minimalColumnClass : ''} mb-1.5 flex items-center gap-2 rounded-r-md border-l-2 border-neutral-600 bg-neutral-900/75 px-2.5 py-1.5`}>
          <Reply className="w-3 h-3 text-neutral-400 shrink-0" />
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
        ? `${minimalColumnClass} flex items-end gap-1 rounded-md border ${dragOver ? 'border-emerald-500/60' : 'border-workbench-border-strong/80'} bg-workbench-card p-1 shadow-[0_10px_28px_rgba(0,0,0,0.20),inset_0_1px_0_rgba(255,255,255,0.035)]`
        : 'flex gap-2 items-end'
      }
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
      >
        <input ref={fileInputRef} type="file" multiple className="hidden" onChange={handleFileSelect} />
        {!minimal && (
          <MessageTypeDropdown value={newType} onChange={onChangeType} compact={minimal} />
        )}
        {minimal && onOpenCommandPalette && (
          <button
            type="button"
            onClick={onOpenCommandPalette}
            title="Open command palette (Ctrl+K)"
            aria-label="Open command palette (Ctrl+K)"
            className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center gap-1 rounded-md border border-neutral-800/80 bg-neutral-950/35 px-2.5 text-xs font-semibold text-neutral-500 transition-colors hover:border-neutral-700 hover:bg-neutral-900/80 hover:text-neutral-100 md:hidden"
          >
            <Command className="h-3.5 w-3.5" />
            <span className="hidden font-mono text-ui-sm text-neutral-500 sm:inline">Ctrl+K</span>
          </button>
        )}
        <button
          type="button"
          onClick={() => fileInputRef.current?.click()}
          title="Attach file"
          aria-label="Attach file"
          className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md text-neutral-500 transition-colors hover:text-neutral-300 md:min-h-8 md:min-w-8"
        >
          <Paperclip className="w-4 h-4" />
        </button>
        <div className="relative flex-1">
          <textarea
            ref={textareaRef}
            value={newContent}
            onChange={e => { onChangeContent(e.target.value); handleTextareaInput(); }}
            onPaste={handlePaste}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!composerDisabled && !overLimit) onSubmit(); }
            }}
            placeholder={disabledReason ?? (minimal ? 'Message Strategic' : 'Type a message...')}
            rows={1}
            disabled={composerDisabled}
            className={minimal
              ? 'min-h-[44px] w-full resize-none rounded border-0 bg-transparent px-3 py-2 text-sm leading-relaxed text-neutral-100 outline-none placeholder:text-neutral-600 disabled:cursor-not-allowed disabled:text-neutral-500 md:min-h-8 md:py-1.5'
              : 'w-full bg-neutral-900/50 border border-neutral-800/50 text-neutral-100 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 placeholder-neutral-600 resize-none min-h-[44px] leading-relaxed disabled:cursor-not-allowed disabled:text-neutral-500'
            }
          />
        </div>
        <button
          type="button"
          onClick={onSubmit}
          disabled={composerDisabled || submitting || !newContent.trim() || overLimit}
          aria-label="Send message"
          title="Send message"
          className={minimal
            ? 'flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md bg-emerald-500 text-neutral-950 transition-colors hover:bg-emerald-400 disabled:bg-neutral-800 disabled:text-neutral-500 disabled:opacity-100 md:min-h-8 md:min-w-8'
            : 'bg-blue-600 hover:bg-blue-500 text-white p-2.5 rounded-xl text-xs font-medium transition-colors disabled:opacity-40 shrink-0 min-h-[44px] min-w-[44px] flex items-center justify-center'
          }
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
      {/* Attachment preview strip */}
      {pendingAttachments.length > 0 && (
        <div className={`${minimal ? minimalColumnClass : ''} mt-2 flex flex-wrap gap-2`}>
          {pendingAttachments.map((att, i) => (
            <div key={att._attachmentKey ?? `${i}-${att.name || ''}-${att.size_bytes || 0}`} className="flex items-center gap-1.5 rounded border border-neutral-700/60 bg-neutral-800/40 px-2.5 py-1.5">
              {att.mime_type?.startsWith('image/') && att.data ? (
                <img src={`data:${att.mime_type};base64,${att.data}`} alt={att.name || ''} className="h-8 w-8 rounded object-cover" />
              ) : (
                <FileText className="h-4 w-4 shrink-0 text-neutral-500" />
              )}
              <span className="max-w-[120px] truncate text-xs text-neutral-300">{att.name || 'File'}</span>
              {att.size_bytes != null && (
                <span className="shrink-0 text-xs text-neutral-500">({(att.size_bytes / 1024).toFixed(0)} KB)</span>
              )}
              <button type="button" onClick={() => onRemoveAttachment?.(i)} className="text-neutral-500 hover:text-neutral-300 p-0.5 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto" aria-label={`Remove ${att.name || 'file'}`}>
                <X className="h-3 w-3" />
              </button>
            </div>
          ))}
        </div>
      )}
      {/* Steering hint: grounded in the last send's real outcome when available,
          otherwise predicted from the selected type. */}
      {(showHint || nearLimit) && (
        <div className={minimal ? `${minimalColumnClass} mt-1.5 px-1` : ''}>
          {showHint && (
            <p className={`${minimal ? 'text-ui-md text-neutral-600' : 'text-ui-md text-neutral-500'} leading-snug ${minimal ? '' : 'mt-1.5 px-0.5'}`}>
              {disabledReason ?? hintText}
            </p>
          )}
          {nearLimit && (
            <p className={`text-right text-xs leading-snug ${minimal ? '' : 'px-0.5'} ${showHint ? 'mt-0.5' : (minimal ? '' : 'mt-1.5')} ${overLimit ? 'text-red-400' : 'text-amber-500'}`}>
              {newContent.length.toLocaleString()} / {MESSAGE_CONTENT_MAX.toLocaleString()}
            </p>
          )}
        </div>
      )}
    </div>
  );
}
