import { FileText } from 'lucide-react';
import { useCallback, useEffect, useRef, useState } from 'react';
import {
  getMessageAttachmentDownloadUrl,
  invalidateMessageAttachmentDownloadUrl,
} from '../strategicStateApi';
import type { MessageAttachment } from '../types/messages';

interface MessageAttachmentChipProps {
  attachment: MessageAttachment;
  projectId: string;
  messageId: string;
  onPreview: (attachment: MessageAttachment) => void;
}

function MessageAttachmentChip({ attachment, projectId, messageId, onPreview }: MessageAttachmentChipProps) {
  const [resolved, setResolved] = useState<{ url: string; expiresAt: number } | null>(
    attachment.url ? { url: attachment.url, expiresAt: Number.POSITIVE_INFINITY } : null,
  );
  const [resolutionFailed, setResolutionFailed] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const image = attachment.mime_type?.startsWith('image/');

  const resolve = useCallback(async (): Promise<string | null> => {
    if (resolved && resolved.expiresAt > Date.now()) return resolved.url;
    if (!attachment.attachment_id || messageId.startsWith('optimistic:')) return null;
    try {
      const result = await getMessageAttachmentDownloadUrl(projectId, messageId, attachment.attachment_id);
      setResolved({ url: result.downloadUrl, expiresAt: result.expiresAt });
      setResolutionFailed(false);
      return result.downloadUrl;
    } catch {
      setResolutionFailed(true);
      return null;
    }
  }, [attachment.attachment_id, messageId, projectId, resolved]);

  useEffect(() => {
    if (!image || (resolved && resolved.expiresAt > Date.now()) || !attachment.attachment_id || messageId.startsWith('optimistic:') || !triggerRef.current) return;
    if (typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {
        observer.disconnect();
        void resolve();
      }
    }, { rootMargin: '120px' });
    observer.observe(triggerRef.current);
    return () => observer.disconnect();
  }, [attachment.attachment_id, image, messageId, resolve, resolved]);

  const invalidate = useCallback(() => {
    if (attachment.attachment_id) {
      invalidateMessageAttachmentDownloadUrl(projectId, messageId, attachment.attachment_id);
    }
    setResolved(null);
    setResolutionFailed(true);
  }, [attachment.attachment_id, messageId, projectId]);

  const preview = useCallback(() => {
    if (
      attachment.data
      || !attachment.attachment_id
      || messageId.startsWith('optimistic:')
      || (resolved && resolved.expiresAt > Date.now())
    ) {
      onPreview(resolved ? { ...attachment, url: resolved.url } : attachment);
      return;
    }
    void resolve().then(url => {
      onPreview(url ? { ...attachment, url } : attachment);
    });
  }, [attachment, messageId, onPreview, resolve, resolved]);

  const imageSrc = image
    ? (attachment.data ? `data:${attachment.mime_type};base64,${attachment.data}` : resolved?.url ?? null)
    : null;
  const errorLabel = resolutionFailed ? ' (preview unavailable; retry)' : '';
  if (imageSrc) {
    return (
      <button
        ref={triggerRef}
        type="button"
        onClick={preview}
        className="cursor-pointer border-0 bg-transparent p-0 leading-none"
        aria-label={`Preview ${attachment.name || 'image'}${errorLabel}`}
        data-viewport-allow-small-target
      >
        <img
          src={imageSrc}
          onError={invalidate}
          alt={attachment.name || 'Image attachment'}
          className="max-h-48 max-w-full rounded border border-neutral-700/60 object-contain"
        />
      </button>
    );
  }
  return (
    <button
      ref={triggerRef}
      type="button"
      onClick={preview}
      className="inline-flex min-h-[44px] cursor-pointer items-center gap-1.5 rounded border border-neutral-700/60 bg-neutral-800/40 px-2.5 py-1.5 text-xs text-neutral-300 hover:border-neutral-500/60 transition-colors"
      aria-label={`Preview ${attachment.name || attachment.mime_type || 'File'}${errorLabel}`}
    >
      <FileText className="h-3.5 w-3.5 shrink-0 text-neutral-500" />
      <span className="max-w-[200px] truncate">{attachment.name || attachment.mime_type || 'File'}</span>
      {attachment.size_bytes != null && (
        <span className="text-neutral-500 shrink-0">({attachment.size_bytes < 1024 ? '<1' : (attachment.size_bytes / 1024).toFixed(0)} KB)</span>
      )}
    </button>
  );
}

export function MessageAttachments({
  attachments,
  onPreview,
  projectId,
  messageId,
  resolutionVersion = 0,
}: {
  attachments: MessageAttachment[];
  onPreview: (attachment: MessageAttachment) => void;
  projectId: string;
  messageId: string;
  resolutionVersion?: number;
}) {
  if (!attachments.length) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {attachments.map((attachment, index) => (
        <MessageAttachmentChip
          key={`${attachment.attachment_id ?? attachment._attachmentKey ?? `${attachment.name ?? 'attachment'}:${index}`}:${resolutionVersion}`}
          attachment={attachment}
          projectId={projectId}
          messageId={messageId}
          onPreview={onPreview}
        />
      ))}
    </div>
  );
}
