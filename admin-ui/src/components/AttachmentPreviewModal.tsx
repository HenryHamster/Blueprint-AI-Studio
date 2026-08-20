import { X, Download, ExternalLink, RotateCw, Crop, Check, RefreshCw, Trash2 } from 'lucide-react';
import { useEffect, useRef, useState, useCallback, type KeyboardEvent, type PointerEvent as ReactPointerEvent } from 'react';
import type { MessageAttachment } from '../types/messages';
import {
  computeCropRegion,
  cropImageFile,
  rotateImageClockwise,
  type CropSelection,
} from '../utils/imageEdits';

/**
 * Optional pre-send editing surface for a pending (not-yet-uploaded) image
 * attachment. When supplied, the modal shows rotate/crop/replace/remove
 * controls that operate on `sourceFile` and hand the re-encoded File back via
 * `onApply`. Sent (read-only) attachments omit this prop and the modal stays a
 * pure viewer.
 */
export interface AttachmentEditControls {
  /** The current pending File, used as the source for canvas re-encodes. */
  sourceFile: File;
  /** Called with the re-encoded File after a rotate or crop is applied. */
  onApply: (file: File) => void | Promise<void>;
  /** Discard this attachment entirely. */
  onRemove: () => void;
  /** Swap in a different file (opens the caller's file picker). */
  onReplace: () => void;
}

interface AttachmentPreviewModalProps {
  attachment: MessageAttachment;
  open: boolean;
  onClose: () => void;
  edit?: AttachmentEditControls;
  onPreviewError?: () => void;
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

export function AttachmentPreviewModal({ attachment, open, onClose, edit, onPreviewError }: AttachmentPreviewModalProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const restoreFocusRef = useRef<HTMLElement | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);

  const mimeType = attachment.mime_type || '';
  const isImage = mimeType.startsWith('image/');
  const isPdf = mimeType === 'application/pdf';
  const displayName = attachment.name || attachment.mime_type || 'File';

  // ── Pre-send editing state (only meaningful when `edit` + image) ────────────
  const editImgRef = useRef<HTMLImageElement>(null);
  const cropStartRef = useRef<{ x: number; y: number } | null>(null);
  const [cropMode, setCropMode] = useState(false);
  const [selection, setSelection] = useState<CropSelection | null>(null);
  const [editBusy, setEditBusy] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);
  const editable = Boolean(edit) && isImage;

  // Reset the transient editing UI whenever the modal opens or the underlying
  // image changes (e.g. after a rotate/crop/replace swaps in new bytes).
  const attachmentSrcKey = attachment.url || attachment.data || null;
  useEffect(() => {
    /* eslint-disable react-hooks/set-state-in-effect */
    setCropMode(false);
    setSelection(null);
    setEditBusy(false);
    setEditError(null);
    cropStartRef.current = null;
    /* eslint-enable react-hooks/set-state-in-effect */
  }, [open, attachmentSrcKey]);

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

  function clampToBox(value: number, max: number): number {
    if (value < 0) return 0;
    if (value > max) return max;
    return value;
  }

  function handleCropPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (!cropMode || editBusy || !editImgRef.current) return;
    const rect = editImgRef.current.getBoundingClientRect();
    const x = clampToBox(event.clientX - rect.left, rect.width);
    const y = clampToBox(event.clientY - rect.top, rect.height);
    cropStartRef.current = { x, y };
    setSelection({ x, y, w: 0, h: 0 });
    editImgRef.current.setPointerCapture?.(event.pointerId);
  }

  function handleCropPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const start = cropStartRef.current;
    if (!cropMode || !start || !editImgRef.current) return;
    const rect = editImgRef.current.getBoundingClientRect();
    const cx = clampToBox(event.clientX - rect.left, rect.width);
    const cy = clampToBox(event.clientY - rect.top, rect.height);
    setSelection({
      x: Math.min(start.x, cx),
      y: Math.min(start.y, cy),
      w: Math.abs(cx - start.x),
      h: Math.abs(cy - start.y),
    });
  }

  function handleCropPointerUp() {
    cropStartRef.current = null;
  }

  async function applyRotate() {
    if (!edit || editBusy) return;
    setEditBusy(true);
    setEditError(null);
    try {
      const rotated = await rotateImageClockwise(edit.sourceFile);
      await edit.onApply(rotated);
    } catch {
      setEditError('Could not rotate this image');
    } finally {
      setEditBusy(false);
    }
  }

  async function applyCrop() {
    const img = editImgRef.current;
    if (!edit || !img || !selection || selection.w < 4 || selection.h < 4) {
      setEditError('Drag a larger area to crop');
      return;
    }
    setEditBusy(true);
    setEditError(null);
    try {
      const region = computeCropRegion(
        selection,
        img.clientWidth,
        img.clientHeight,
        img.naturalWidth,
        img.naturalHeight,
      );
      const cropped = await cropImageFile(edit.sourceFile, region);
      await edit.onApply(cropped);
      setCropMode(false);
      setSelection(null);
    } catch {
      setEditError('Could not crop this image');
    } finally {
      setEditBusy(false);
    }
  }

  function toggleCropMode() {
    setEditError(null);
    setSelection(null);
    cropStartRef.current = null;
    setCropMode(prev => !prev);
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
        className="flex max-h-[calc(100vh-32px)] max-w-[min(90vw,960px)] flex-col overflow-hidden rounded-lg border border-neutral-700 bg-workbench-base shadow-2xl shadow-black/50"
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
            className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md border border-neutral-700 bg-workbench-panel text-neutral-400 transition-colors hover:border-neutral-600 hover:text-neutral-100 md:min-h-8 md:min-w-8"
          >
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>

        {/* Pre-send edit toolbar (image attachments in the composer only) */}
        {editable && (
          <div className="flex shrink-0 flex-wrap items-center gap-2 border-b border-neutral-700/60 px-4 py-2">
            <button
              type="button"
              onClick={applyRotate}
              disabled={editBusy || cropMode}
              aria-label="Rotate image 90 degrees"
              className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-700 bg-workbench-panel px-3 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-workbench-hover disabled:opacity-40"
            >
              <RotateCw className="h-4 w-4" aria-hidden="true" />
              Rotate
            </button>
            {!cropMode ? (
              <button
                type="button"
                onClick={toggleCropMode}
                disabled={editBusy}
                aria-label="Crop image"
                className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-700 bg-workbench-panel px-3 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-workbench-hover disabled:opacity-40"
              >
                <Crop className="h-4 w-4" aria-hidden="true" />
                Crop
              </button>
            ) : (
              <>
                <button
                  type="button"
                  onClick={applyCrop}
                  disabled={editBusy}
                  aria-label="Apply crop"
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-emerald-600/60 bg-emerald-600/20 px-3 text-sm text-emerald-200 transition-colors hover:bg-emerald-600/30 disabled:opacity-40"
                >
                  <Check className="h-4 w-4" aria-hidden="true" />
                  Apply crop
                </button>
                <button
                  type="button"
                  onClick={toggleCropMode}
                  disabled={editBusy}
                  aria-label="Cancel crop"
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-700 bg-workbench-panel px-3 text-sm text-neutral-300 transition-colors hover:border-neutral-600 hover:bg-workbench-hover disabled:opacity-40"
                >
                  <X className="h-4 w-4" aria-hidden="true" />
                  Cancel
                </button>
              </>
            )}
            <span className="flex-1" />
            <button
              type="button"
              onClick={() => edit?.onReplace()}
              disabled={editBusy}
              aria-label="Replace image"
              className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-700 bg-workbench-panel px-3 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-workbench-hover disabled:opacity-40"
            >
              <RefreshCw className="h-4 w-4" aria-hidden="true" />
              Replace
            </button>
            <button
              type="button"
              onClick={() => edit?.onRemove()}
              disabled={editBusy}
              aria-label="Remove image"
              className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-700 bg-workbench-panel px-3 text-sm text-neutral-300 transition-colors hover:border-red-700/60 hover:text-red-300 disabled:opacity-40"
            >
              <Trash2 className="h-4 w-4" aria-hidden="true" />
              Remove
            </button>
            {(editError || cropMode) && (
              <p className={`w-full text-ui-md leading-snug ${editError ? 'text-red-400' : 'text-neutral-500'}`}>
                {editError ?? 'Drag across the image to select a crop region, then Apply crop.'}
              </p>
            )}
          </div>
        )}

        {/* Content */}
        <div className="min-h-0 flex-1 overflow-auto flex items-center justify-center">
          {editable && imageSrc ? (
            <div
              className="relative inline-block leading-none"
              onPointerDown={handleCropPointerDown}
              onPointerMove={handleCropPointerMove}
              onPointerUp={handleCropPointerUp}
              style={cropMode ? { cursor: 'crosshair', touchAction: 'none' } : undefined}
            >
              <img
                ref={editImgRef}
                src={imageSrc}
                alt={displayName}
                draggable={false}
                className="max-h-[calc(100vh-220px)] max-w-full select-none"
                onError={onPreviewError}
              />
              {cropMode && selection && selection.w > 0 && selection.h > 0 && (
                <div
                  className="pointer-events-none absolute border-2 border-emerald-400 bg-emerald-400/10"
                  style={{ left: selection.x, top: selection.y, width: selection.w, height: selection.h }}
                />
              )}
            </div>
          ) : isImage && imageSrc ? (
            <img
              src={imageSrc}
              alt={displayName}
              className="max-h-[calc(100vh-160px)] max-w-full object-contain"
              onError={onPreviewError}
            />
          ) : isPdf && pdfSrc ? (
            <iframe
              src={pdfSrc}
              title={displayName}
              className="h-[calc(100vh-160px)] w-full border-0"
              onError={onPreviewError}
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
                className="inline-flex items-center gap-2 rounded-md border border-neutral-700 bg-workbench-panel px-3 py-1.5 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-workbench-hover"
              >
                <ExternalLink className="h-4 w-4" />
                Open in new tab
              </a>
            )}
            {downloadBlob ? (
              <button
                type="button"
                onClick={() => triggerDownload(downloadBlob, displayName)}
                className="inline-flex items-center gap-2 rounded-md border border-neutral-700 bg-workbench-panel px-3 py-1.5 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-workbench-hover"
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
                className="inline-flex items-center gap-2 rounded-md border border-neutral-700 bg-workbench-panel px-3 py-1.5 text-sm text-neutral-200 transition-colors hover:border-neutral-600 hover:bg-workbench-hover"
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
