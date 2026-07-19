import { memo, useRef, useState, type RefObject } from 'react';
import { Command, X, Send, Reply, Paperclip, FileText, Image } from 'lucide-react';
import { MessageTypeDropdown } from './MessageTypeDropdown';
import { type ArmingOutcome, composerHintCopy } from './messageArming';
import type { StrategicContextDescriptorModel } from '../utils/strategicContextDescriptor';
import { StrategicContextDescriptor } from './StrategicContextDescriptor';
import { AttachmentPreviewModal } from './AttachmentPreviewModal';
import type { MessageAttachment } from '../types/messages';
import { MESSAGE_CONTENT_MAX } from '../api';

export interface PendingAttachment {
  id: string;
  file: File;
  previewUrl: string | null;
  mime_type: string;
  name: string;
  size_bytes: number;
}

export interface ComposerArtifactRef {
  id?: string;
  artifact_ref_id?: string;
  kind?: string;
  title?: string;
  label?: string;
  revision?: number | string;
}

export type ComposerSourceRef = ComposerArtifactRef;

export interface MessagesComposerProps {
  textareaRef: RefObject<HTMLTextAreaElement | null>;
  newType: string;
  newContent: string;
  submitting: boolean;
  sendLocked?: boolean;
  attachmentLocked?: boolean;
  sendState?: 'sending' | 'accepted' | 'waiting' | 'needs_retry' | null;
  wakeupState?: 'queued' | 'coalesced' | 'paused' | 'waiting_for_capacity' | null;
  /** Freeze the pending payload while its durable outcome is unresolved. */
  editingLocked?: boolean;
  replyTo: { id: string; content: string; from: string; message_type: string; status: string } | null;
  /** Outcome the server reported for the last send, or null before any send. */
  lastArming?: ArmingOutcome | null;
  /** Whether a strategic cycle is currently running (live WS-driven signal). */
  strategistBusy?: boolean;
  strategicContext?: StrategicContextDescriptorModel | null;
  starterPrompts?: string[];
  capabilityHint?: string | null;
  disabledReason?: string | null;
  pendingAttachments?: PendingAttachment[];
  retainedAttachmentCount?: number;
  sourceRefs?: ComposerSourceRef[];
  artifactRefs?: ComposerArtifactRef[];
  uploading?: boolean;
  onPickStarterPrompt?: (value: string) => void;
  onOpenStrategicContext?: () => void;
  onOpenCommandPalette?: () => void;
  onChangeType: (value: string) => void;
  onChangeContent: (value: string) => void;
  onCancelReply: () => void;
  onAbandonPending?: () => void;
  onSubmit: () => void;
  onAddAttachment?: (file: File) => void | Promise<void>;
  onRemoveAttachment?: (id: string) => void;
  onRemoveArtifactRef?: (id: string) => void;
  /**
   * Swap the File backing a pending attachment — used by the pre-send preview
   * for canvas-edited (rotate/crop) results and for picker-based replace. When
   * omitted the preview stays read-only (no edit controls).
   */
  onReplaceAttachmentFile?: (id: string, file: File) => void;
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
export const MessagesComposer = memo(function MessagesComposer({
  textareaRef,
  newType,
  newContent,
  submitting,
  sendLocked = false,
  attachmentLocked = false,
  sendState,
  wakeupState,
  editingLocked,
  replyTo,
  lastArming,
  strategistBusy,
  strategicContext,
  starterPrompts,
  capabilityHint,
  disabledReason,
  pendingAttachments,
  retainedAttachmentCount = 0,
  sourceRefs = [],
  artifactRefs = [],
  uploading,
  onOpenStrategicContext,
  onOpenCommandPalette,
  onChangeType,
  onChangeContent,
  onCancelReply,
  onAbandonPending,
  onSubmit,
  onPickStarterPrompt,
  onAddAttachment,
  onRemoveAttachment,
  onRemoveArtifactRef,
  onReplaceAttachmentFile,
  chrome = 'full',
}: MessagesComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const replaceInputRef = useRef<HTMLInputElement>(null);
  const replaceTargetRef = useRef<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  const [previewId, setPreviewId] = useState<string | null>(null);
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
  const showHint = Boolean(disabledReason) || !minimal || Boolean(strategistBusy) || lastArming === 'not_armed' || sendState != null;
  const sendStateHint = sendState === 'sending'
    ? 'Sending…'
    : sendState === 'needs_retry'
      ? 'Send not confirmed. Retry will first check whether it was accepted.'
      : sendState === 'waiting'
        ? (wakeupState === 'paused' ? 'Message accepted. Resident is paused.' : 'Message accepted. Resident is waiting for capacity.')
        : sendState === 'accepted'
          ? 'Message accepted.'
          : null;

  const displayedSourceRefs = [
    ...sourceRefs,
    ...artifactRefs.filter(artifact => {
      const identity = String(artifact.artifact_ref_id || artifact.id || '');
      return !identity || !sourceRefs.some(source => String(source.artifact_ref_id || source.id || '') === identity);
    }),
  ];
  const hasAttachments = (pendingAttachments?.length ?? 0) > 0 || retainedAttachmentCount > 0;

  // The pending attachment whose full-size preview is open (if any). If it was
  // removed while open, this resolves to undefined and the modal unmounts.
  const previewAttachment = previewId != null
    ? pendingAttachments?.find(a => a.id === previewId)
    : undefined;
  // Adapt the PendingAttachment (File + object-URL) to the MessageAttachment
  // shape the preview modal consumes: its object URL is passed as the remote
  // `url`, which the modal already renders for both <img> and download.
  const previewMessageAttachment: MessageAttachment | null = previewAttachment
    ? {
      type: previewAttachment.mime_type.startsWith('image/') ? 'image' : 'attachment',
      mime_type: previewAttachment.mime_type,
      name: previewAttachment.name,
      size_bytes: previewAttachment.size_bytes,
      url: previewAttachment.previewUrl ?? undefined,
    }
    : null;

  // ── File handlers ──────────────────────────────────────────────────────────
  async function handleFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const files = e.target.files;
    if (!files || !onAddAttachment || attachmentLocked) return;
    for (let i = 0; i < files.length; i++) await onAddAttachment(files[i]);
    e.target.value = '';
  }

  function handleReplaceFileChange(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    const targetId = replaceTargetRef.current;
    replaceTargetRef.current = null;
    if (file && targetId && onReplaceAttachmentFile) {
      onReplaceAttachmentFile(targetId, file);
    }
    e.target.value = '';
  }

  async function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    if (!onAddAttachment || attachmentLocked) return;
    const items = e.clipboardData?.items;
    if (!items) return;
    const files: File[] = [];
    for (let i = 0; i < items.length; i++) {
      const item = items[i];
      if (item.kind === 'file') {
        const file = item.getAsFile();
        if (file) files.push(file);
      }
    }
    if (files.length === 0) return;
    e.preventDefault();
    for (const file of files) await onAddAttachment(file);
  }

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(true);
  }
  function handleDragLeave(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    if (e.currentTarget.contains(e.relatedTarget as Node)) return;
    setDragOver(false);
  }
  async function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    e.stopPropagation();
    setDragOver(false);
    if (!onAddAttachment || attachmentLocked) return;
    const files = e.dataTransfer?.files;
    if (!files) return;
    for (let i = 0; i < files.length; i++) await onAddAttachment(files[i]);
  }

  const attHasPreview = (att: PendingAttachment): string | null =>
    att.previewUrl;


  return (
    <div className={minimal
      ? 'shrink-0 border-t border-workbench-border bg-workbench-base px-2 pb-2 pt-1.5 md:px-2.5'
      : 'px-4 md:px-6 py-3 border-t border-neutral-800 shrink-0 bg-neutral-950/50'
    }>
      {minimal && strategicContext && (
        <StrategicContextDescriptor model={strategicContext} onOpen={onOpenStrategicContext} />
      )}
      {minimal && starterPrompts && starterPrompts.length > 0 && !newContent && !editingLocked && (
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
          <button
            type="button"
            onClick={onCancelReply}
            disabled={editingLocked}
            aria-label="Cancel reply"
            className="text-neutral-500 hover:text-neutral-300 p-0.5 shrink-0 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center disabled:opacity-40"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      )}
      {/* Hidden file input */}
      <input
        ref={fileInputRef}
        type="file"
        multiple
        onChange={handleFileChange}
        className="hidden"
        tabIndex={-1}
        accept="image/*,.pdf,.txt,.csv,.json,.xml,.md,.log,.yaml,.yml,.env,.cfg,.ini,.toml,.js,.ts,.py,.go,.rs,.java,.kt,.swift,.sql,.sh,.bat,.ps1"
      />
      {/* Hidden input for replacing a pending image from the preview editor */}
      <input
        ref={replaceInputRef}
        type="file"
        onChange={handleReplaceFileChange}
        className="hidden"
        tabIndex={-1}
        accept="image/*"
      />
      {previewAttachment && (
        <AttachmentPreviewModal
          open
          attachment={previewMessageAttachment as MessageAttachment}
          onClose={() => setPreviewId(null)}
          edit={onReplaceAttachmentFile && !attachmentLocked ? {
            sourceFile: previewAttachment.file,
            onApply: file => onReplaceAttachmentFile(previewAttachment.id, file),
            onRemove: () => { onRemoveAttachment?.(previewAttachment.id); setPreviewId(null); },
            onReplace: () => { replaceTargetRef.current = previewAttachment.id; replaceInputRef.current?.click(); },
          } : undefined}
        />
      )}

      {/* Attachment preview strip */}
      {hasAttachments && pendingAttachments && (
        <div className={`${minimal ? minimalColumnClass : ''} mb-1.5 flex flex-wrap gap-2 px-1`}>
          {pendingAttachments.map(att => {
            const preview = attHasPreview(att);
            const sizeLabel = att.size_bytes != null
              ? `(${att.size_bytes < 1024 ? '<1' : (att.size_bytes / 1024).toFixed(0)} KB)`
              : null;
            return (
              <div key={att.id} className="group relative flex items-stretch rounded-md border border-neutral-700/60 bg-neutral-800/40 pr-7 text-xs text-neutral-300">
                {preview ? (
                  // Image chip is clickable to open the full-size preview/editor.
                  <button
                    type="button"
                    onClick={() => setPreviewId(att.id)}
                    aria-label={`Open ${att.name} preview`}
                    className="flex min-h-[44px] items-center gap-1.5 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-neutral-700/40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-neutral-500"
                  >
                    <img src={preview} alt={att.name} className="h-8 w-8 rounded object-cover" />
                    <span className="max-w-[120px] truncate">{att.name}</span>
                    {sizeLabel && <span className="text-neutral-500 shrink-0">{sizeLabel}</span>}
                  </button>
                ) : (
                  <div className="flex items-center gap-1.5 px-2 py-1.5">
                    {att.mime_type?.startsWith('image/') ? (
                      <Image className="h-4 w-4 shrink-0 text-neutral-500" />
                    ) : (
                      <FileText className="h-4 w-4 shrink-0 text-neutral-500" />
                    )}
                    <span className="max-w-[120px] truncate">{att.name}</span>
                    {sizeLabel && <span className="text-neutral-500 shrink-0">{sizeLabel}</span>}
                  </div>
                )}
                <button
                  type="button"
                  onClick={() => onRemoveAttachment?.(att.id)}
                  disabled={editingLocked || attachmentLocked}
                  className="absolute right-0 top-1/2 flex min-h-[44px] min-w-[44px] -translate-y-1/2 items-center justify-center rounded text-neutral-500 opacity-0 transition-opacity hover:text-neutral-200 focus-visible:opacity-100 group-hover:opacity-100"
                  aria-label={`Remove ${att.name}`}
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            );
          })}
        </div>
      )}

      {displayedSourceRefs.length > 0 && (
        <div className={`${minimal ? minimalColumnClass : ''} mb-1.5 flex flex-wrap gap-2 px-1`} aria-label="Attached Resident sources" role="list">
          {displayedSourceRefs.map((ref, index) => {
            const identity = String(ref.artifact_ref_id || ref.id || `source-${index}`);
            const fallbackKind = ref.kind ? ref.kind.replace(/_/g, ' ') : 'Source';
            const kind = ref.title
              || ref.label
              || (ref.kind === 'whiteboard' ? 'Whiteboard' : ref.kind === 'notes' ? 'Notes' : fallbackKind);
            const revision = ref.revision == null ? '' : ` revision ${String(ref.revision)}`;
            const label = `${kind}${revision}`;
            return (
              <div key={identity} className="group flex min-h-[44px] items-center gap-1.5 rounded-full border border-sky-700/50 bg-sky-950/35 pl-2.5 pr-1 text-xs text-sky-200" role="listitem">
                <span>{label}</span>
                <button
                  type="button"
                  onClick={() => onRemoveArtifactRef?.(identity)}
                  disabled={editingLocked}
                  aria-label={`Remove ${label}`}
                  className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-full text-sky-400 hover:bg-sky-900/60 hover:text-sky-100 disabled:opacity-40"
                >
                  <X className="h-3 w-3" />
                </button>
              </div>
            );
          })}
        </div>
      )}

      <div
        data-testid={minimal ? 'strategic-composer-box' : undefined}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        className={`${minimal
          ? `${minimalColumnClass} flex items-end gap-1 rounded-md border border-workbench-border-strong/80 bg-workbench-card p-1 shadow-[0_10px_28px_rgba(0,0,0,0.20),inset_0_1px_0_rgba(255,255,255,0.035)]`
          : 'flex gap-2 items-end'
        } ${dragOver ? 'ring-2 ring-emerald-500/50 border-emerald-500/60' : ''}`}>
        {!minimal && (
          <MessageTypeDropdown value={newType} onChange={onChangeType} compact={minimal} />
        )}
        {minimal && (
          <>
            {onOpenCommandPalette && (
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
              disabled={composerDisabled || editingLocked || attachmentLocked}
              title="Attach file"
              aria-label="Attach file"
              className="flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md border border-neutral-800/80 bg-neutral-950/35 px-2 text-neutral-500 transition-colors hover:border-neutral-700 hover:bg-neutral-900/80 hover:text-neutral-300 disabled:opacity-40"
            >
              <Paperclip className="h-3.5 w-3.5" />
            </button>
          </>
        )}
        {!minimal && (
          <button
            type="button"
            onClick={() => fileInputRef.current?.click()}
            disabled={composerDisabled || editingLocked || attachmentLocked}
            title="Attach file"
            aria-label="Attach file"
            className="flex shrink-0 items-center justify-center rounded-xl bg-neutral-900/50 border border-neutral-800/50 p-2.5 text-neutral-500 transition-colors hover:bg-neutral-800 hover:text-neutral-300 disabled:opacity-40 min-h-[44px] min-w-[44px]"
          >
            <Paperclip className="w-4 h-4" />
          </button>
        )}
        <div className="relative flex-1">
          <textarea
            ref={textareaRef}
            value={newContent}
            onChange={e => { onChangeContent(e.target.value); handleTextareaInput(); }}
            onPaste={handlePaste}
            onKeyDown={e => {
              if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); if (!composerDisabled && !sendLocked && !overLimit) onSubmit(); }
            }}
            placeholder={disabledReason ?? (minimal ? 'Message Strategic' : 'Type a message...')}
            rows={1}
            disabled={composerDisabled}
            readOnly={editingLocked}
            aria-readonly={editingLocked || undefined}
            aria-describedby={sendState ? 'message-send-status' : undefined}
            className={minimal
              ? 'min-h-[44px] w-full resize-none rounded border-0 bg-transparent px-3 py-2 text-sm leading-relaxed text-neutral-100 outline-none placeholder:text-neutral-600 disabled:cursor-not-allowed disabled:text-neutral-500 md:min-h-8 md:py-1.5'
              : 'w-full bg-neutral-900/50 border border-neutral-800/50 text-neutral-100 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 placeholder-neutral-600 resize-none min-h-[44px] leading-relaxed disabled:cursor-not-allowed disabled:text-neutral-500'
            }
          />
        </div>
        <button
          type="button"
          onClick={onSubmit}
          disabled={composerDisabled || submitting || sendLocked || uploading || (!newContent.trim() && !hasAttachments) || overLimit}
          aria-label={sendState === 'needs_retry' ? 'Retry message' : 'Send message'}
          title={sendState === 'needs_retry' ? 'Retry message' : 'Send message'}
          data-send-state={sendState ?? undefined}
          className={minimal
            ? 'flex min-h-[44px] min-w-[44px] shrink-0 items-center justify-center rounded-md bg-emerald-500 text-neutral-950 transition-colors hover:bg-emerald-400 disabled:bg-neutral-800 disabled:text-neutral-500 disabled:opacity-100'
            : 'bg-blue-600 hover:bg-blue-500 text-white p-2.5 rounded-xl text-xs font-medium transition-colors disabled:opacity-40 shrink-0 min-h-[44px] min-w-[44px] flex items-center justify-center'
          }
        >
          <Send className="w-4 h-4" />
        </button>
      </div>
      {/* Steering hint: grounded in the last send's real outcome when available,
          otherwise predicted from the selected type. */}
      {(showHint || nearLimit) && (
        <div className={minimal ? `${minimalColumnClass} mt-1.5 px-1` : ''}>
          {showHint && (
            <p
              id={sendState ? 'message-send-status' : undefined}
              role={sendState ? 'status' : undefined}
              aria-live={sendState ? 'polite' : undefined}
              className={`${minimal ? 'text-ui-md text-neutral-600' : 'text-ui-md text-neutral-500'} leading-snug ${minimal ? '' : 'mt-1.5 px-0.5'}`}
            >
              {disabledReason ?? sendStateHint ?? hintText}
            </p>
          )}
          {sendState === 'needs_retry' && onAbandonPending && (
            <button
              type="button"
              onClick={onAbandonPending}
              disabled={submitting || uploading}
              className="mt-1 min-h-11 rounded px-2 text-ui-md text-neutral-500 underline decoration-neutral-700 underline-offset-2 hover:text-neutral-300 disabled:opacity-40"
            >
              Abandon pending message
            </button>
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
});
