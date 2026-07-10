import type { MessageAttachment } from '../types/messages';

/**
 * Read a File object via FileReader and produce a MessageAttachment
 * with a base64-encoded data payload (the portion after the data URL
 * comma).  Used by MessagesPanel.handleAttachFiles.
 */
export function fileToMessageAttachment(file: File): Promise<MessageAttachment> {
  return new Promise<MessageAttachment>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const mime_type = file.type || 'application/octet-stream';
      resolve({
        type: mime_type.startsWith('image/') ? 'image' : 'file',
        mime_type,
        data: (reader.result as string).split(',')[1],
        name: file.name,
        size_bytes: file.size,
      });
    };
    reader.onerror = () => reject(reader.error ?? new Error('FileReader error'));
    reader.readAsDataURL(file);
  });
}
