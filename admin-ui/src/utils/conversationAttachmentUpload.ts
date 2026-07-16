import { deleteMessageAttachmentUpload, getMessageAttachmentUploadUrl } from '../strategicStateApi';
import type { MessageAttachment } from '../types/messages';

export interface UploadableConversationAttachment {
  file: File;
  mime_type: string;
  name: string;
}

export class ConversationAttachmentUploadError extends Error {
  readonly attachmentName: string;

  constructor(attachmentName: string, options?: ErrorOptions) {
    super(`Failed to upload ${attachmentName}`, options);
    this.name = 'ConversationAttachmentUploadError';
    this.attachmentName = attachmentName;
  }
}

export async function uploadConversationAttachments(
  projectId: string,
  attachments: UploadableConversationAttachment[],
): Promise<{ uploaded: MessageAttachment[]; visible: MessageAttachment[] }> {
  const uploaded: MessageAttachment[] = [];
  const visible: MessageAttachment[] = [];
  const createdIds: string[] = [];
  let attachmentName = 'attachment';
  try {
    for (const attachment of attachments) {
      attachmentName = attachment.name;
      const receipt = await getMessageAttachmentUploadUrl(
        projectId,
        attachment.name,
        attachment.mime_type,
        attachment.file.size,
      );
      if (receipt.size_bytes !== attachment.file.size) throw new Error('Upload receipt size mismatch');
      createdIds.push(receipt.attachment_id);
      const response = await fetch(receipt.upload_url, {
        method: 'PUT',
        headers: { 'Content-Type': attachment.mime_type },
        body: attachment.file,
      });
      if (!response.ok) throw new Error(`Upload failed: ${response.status}`);
      const descriptor: MessageAttachment = {
        type: attachment.mime_type.startsWith('image/') ? 'image' : 'attachment',
        mime_type: attachment.mime_type,
        attachment_id: receipt.attachment_id,
        storage_uri: receipt.storage_uri,
        name: attachment.name,
        size_bytes: attachment.file.size,
      };
      uploaded.push(descriptor);
      visible.push({ ...descriptor, url: receipt.download_url });
    }
    return { uploaded, visible };
  } catch (error) {
    await Promise.allSettled(createdIds.map(id => deleteMessageAttachmentUpload(projectId, id)));
    throw new ConversationAttachmentUploadError(attachmentName, { cause: error });
  }
}
