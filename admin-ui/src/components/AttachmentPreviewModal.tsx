import { X, Download, ExternalLink } from 'lucide-react';
import { useEffect, useRef, useState, useCallback, type KeyboardEvent } from 'react';
import type { MessageAttachment } from '../types/messages';

interface AttachmentPreviewModalProps {
  attachment: MessageAttachment;
  open: boolean;
  onClose: () => void;
}

function base64ToBlob(base64: string, mimeType: string): Blob {
  const byteChars = atob(base64);
  const chunks: BlobPart[] = [];
  for (let offset = 0; offset < byteChars.length; offset += 1024) {
    const slice = byteChars.slice(offset, offset + 1024);
    const bytes = new Uint8Array(slice.length);
    for (let i = 0; i < slice.length; i++) {
      bytes[i] = slice.charCodeAt(i);
    }
    chunks.push(bytes);
  }
  return new Blob(chunks, { type: mimeType });
}

function triggerDownload(blob: Blob, fileName: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = fileName;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function AttachmentPreviewModal({ attachment, open, onClose }: AttachmentPreviewModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);

  const mimeType = attachment.mime_type || '';
  const isImage = mimeType.startsWith('image/');
  const isPdf = mimeType === 'application/pdf';
  const displayName = attachment.name || attachment.mime_type || 'File';

  // Persisted chat/message attachments carry a signed GCS `url` rather than an
  // inline base64 `data` payload (only locally-constructed attachments carry
  // `data`). Both forms must preview, so we prefer the local blob when present
  // and otherwise fall back to the remote signed URL.
  const remoteUrl = attachment.url || null;

  // Create object URL for PDF preview from inline base64 data — revoke on
  // close/unmount. URL.createObjectURL is a browser platform API that requires
  // effect synchronization; setObjectUrl is needed to trigger re-render with
  // the new src on the <iframe>. When only a remote `url` is present there is
  // no blob to build, so this effect no-ops and the remote URL is used directly.
  useEffect(() => {
    if (!open || !isPdf || !attachment.data) return;
    const blob = base64ToBlob(attachment.data, mimeType);
    const url = URL.createObjectURL(blob);
    /* eslint-disable react-hooks/set-state-in-effect */
    setObjectUrl(url);
    return () => {
      URL.revokeObjectURL(url);
      setObjectUrl(null);
    };
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [open, isPdf, attachment.data, mimeType]);

  // Focus management: restore focus to trigger element on close
  useEffect(() => {
    if (!open) return;
    restoreFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const timer = window.setTimeout(() => closeButtonRef.current?.focus(), 0);
    return () => {
      window.clearTimeout(timer);
      restoreFocusRef.current?.focus();
      restoreFocusRef.current = null;
    };
  }, [open]);

  const focusableElements = useCallback((): HTMLElement[] => {
    if (!panelRef.current) return [];
    return Array.from(
      panelRef.current.querySelectorAll<HTMLElement>(
        'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
      ),
    ).filter(element => !element.hasAttribute('disabled'));
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    event.stopPropagation();
    event.nativeEvent.stopImmediatePropagation?.();

    if (event.key === 'Escape') {
      event.preventDefault();
      onClose();
      return;
    }

    if (event.key !== 'Tab') return;

    const focusables = focusableElements();
    if (focusables.length === 0) return;
    const currentIndex = focusables.indexOf(document.activeElement as HTMLElement);
    const nextIndex = event.shiftKey
      ? (currentIndex <= 0 ? focusables.length - 1 : currentIndex - 1)
      : (currentIndex === -1 || currentIndex === focusables.length - 1 ? 0 : currentIndex + 1);
    event.preventDefault();
    focusables[nextIndex]?.focus();
  }

  if (!open) return null;

  const attachmentData = attachment.data;
  const downloadBlob = attachmentData
    ? base64ToBlob(attachmentData, mimeType || 'application/octet-stream')
    : null;

  // Source used by the embedded viewers. Prefer a locally-built blob URL (data
  // path); otherwise use the remote signed URL directly.
  const imageSrc = attachmentData ? `data:${mimeType};base64,${attachmentData}` : remoteUrl;
  const pdfSrc = objectUrl || remoteUrl;
  // "Open in new tab" works for any renderable source (blob or remote URL).
  const openInTabUrl = (isImage && imageSrc) || (isPdf && pdfSrc) || remoteUrl || null;
  const canDownload = downloadBlob != null || remoteUrl != null;

  return (
    <div
      className="fixed inset-0 z-[85] flex items-center justify-center bg-black/70 backdrop-blur-sm px-4 py-4"
      onMouseDown={event => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        role="dialog"
        aria-modal="true"
        aria-label={`Preview: ${displayName}`}
        className="flex max-h-[calc(100vh-32px)] max-w-[min(90vw,960px)] flex-col overflow-hidden rounded-lg border border-neutral-700 bg-neutral-900 shadow-2xl shadow-black/50"
        onKeyDown={handleKeyDown}
      >
        {/* Header */}
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-neutral-700/60 px-4 py-2.5">
          <span className="truncate text-sm font-medium text-neutral-200">
            {displayName}
          </span>
          <button
            ref={closeButtonRef}
            type="button"
            onClick={onClose}
            aria-label="Close preview"
            className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md border border-neutral-700 bg-neutral-800 text-neutral-400 transition-colors hover:border-neutral-600 hover:text-neutral-100 md:min-h-8 md:min-w-8"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {/* Content */}
        <div className="min-h-0 flex-1 overflow-auto flex items-center justify-center">
          {isImage && imageSrc ? (
            <img
              src={imageSrc}
              alt={displayName}
              className="max-h-[calc(100vh-160px)] max-w-full object-contain"
            />
          ) : isPdf && pdfSrc ? (
            <iframe
              src={pdfSrc}
              title={displayName}
              className="h-[calc(100vh-160px)] w-full border-0"
            />
          ) : (
            <div className="flex flex-col items-center gap-4 px-6 py-12">
              <p className="text-sm text-neutral-400">
                {isPdf
                  ? 'Unable to load PDF data'
                  : `No preview available for ${mimeType || 'this file type'}`}
              </p>
            </div>
          )}
        </div>

        {/* Footer — always-visible open/download fallback */}
        {(openInTabUrl || canDownload) && (
          <div className="flex shrink-0 items-center justify-end gap-2 border-t border-neutral-700/60 px-4 py-2.5">
            {openInTabUrl && (
              <a
                href={openInTabUrl}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-md border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-neutral-700"
              >
                <ExternalLink className="h-4 w-4" />
                Open in new tab
              </a>
            )}
            {downloadBlob ? (
              <button
                type="button"
                onClick={() => triggerDownload(downloadBlob, displayName)}
                className="inline-flex items-center gap-2 rounded-md border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-neutral-700"
              >
                <Download className="h-4 w-4" />
                Download
              </button>
            ) : remoteUrl ? (
              <a
                href={remoteUrl}
                download={displayName}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-2 rounded-md border border-neutral-700 bg-neutral-800 px-3 py-1.5 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-neutral-700"
              >
                <Download className="h-4 w-4" />
                Download
              </a>
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}
