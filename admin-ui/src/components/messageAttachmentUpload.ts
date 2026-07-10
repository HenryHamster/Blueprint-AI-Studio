import type { MessageAttachment } from '../types/messages';
import type { PendingAttachment } from './MessagesComposer';
import { getMessageAttachmentUploadUrl } from '../strategicStateApi';

/** Largest file for which we generate an in-composer object-URL preview. */
const MAX_PREVIEW_BYTES = 10_000_000;

/** Build a PendingAttachment from a picked file, with an image preview when cheap. */
export function createPendingAttachment(file: File): PendingAttachment {
  const id = `att_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
  let previewUrl: string | null = null;
  if (file.type?.startsWith('image/') && file.size < MAX_PREVIEW_BYTES && typeof URL.createObjectURL === 'function') {
    try {
      previewUrl = URL.createObjectURL(file);
    } catch {
      previewUrl = null;
    }
  }
  return {
    id,
    file,
    previewUrl,
    mime_type: file.type || 'application/octet-stream',
    name: file.name,
    size_bytes: file.size,
  };
}

/** Remove one pending attachment by id, revoking its preview object URL. */
export function removePendingAttachment(attachments: PendingAttachment[], id: string): PendingAttachment[] {
  const att = attachments.find(a => a.id === id);
  if (att?.previewUrl && typeof URL.revokeObjectURL === 'function') {
    URL.revokeObjectURL(att.previewUrl);
  }
  return attachments.filter(a => a.id !== id);
}

export function revokePendingAttachmentPreviews(attachments: PendingAttachment[]): void {
  for (const attachment of attachments) {
    if (attachment.previewUrl && typeof URL.revokeObjectURL === 'function') {
      URL.revokeObjectURL(attachment.previewUrl);
    }
  }
}

export async function uploadPendingMessageAttachments(
  projectId: string,
  attachments: PendingAttachment[],
): Promise<MessageAttachment[]> {
  const uploadedAttachments: MessageAttachment[] = [];
  for (const att of attachments) {
    try {
      const { upload_url, download_url } = await getMessageAttachmentUploadUrl(
        projectId,
        att.name,
        att.mime_type,
      );
      const uploadRes = await fetch(upload_url, {
        method: 'PUT',
        headers: { 'Content-Type': att.mime_type },
        body: att.file,
      });
      if (!uploadRes.ok) throw new Error(`Upload failed: ${uploadRes.status}`);
      uploadedAttachments.push({
        type: att.mime_type?.startsWith('image/') ? 'image' : 'attachment',
        mime_type: att.mime_type,
        name: att.name,
        size_bytes: att.size_bytes,
        url: download_url,
      });
    } catch {
      throw new Error(`Failed to upload ${att.name}`);
    }
  }
  return uploadedAttachments;
}
