import { cleanup, fireEvent, render, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, test, vi } from 'vitest';
import { MessageAttachments } from './MessageAttachments';

const { getMessageAttachmentDownloadUrl, invalidateMessageAttachmentDownloadUrl } = vi.hoisted(() => ({
  getMessageAttachmentDownloadUrl: vi.fn(),
  invalidateMessageAttachmentDownloadUrl: vi.fn(),
}));
vi.mock('../strategicStateApi', () => ({ getMessageAttachmentDownloadUrl, invalidateMessageAttachmentDownloadUrl }));

describe('MessageAttachments lazy resolution', () => {
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  test('offscreen images and non-images make no resolver calls until the file is opened', async () => {
    getMessageAttachmentDownloadUrl.mockResolvedValue({ downloadUrl: 'https://signed.example/file', expiresAt: Date.now() + 60_000 });
    const onPreview = vi.fn();
    const { getByRole } = render(
      <MessageAttachments
        projectId="project-1"
        messageId="message-1"
        onPreview={onPreview}
        attachments={[
          { type: 'image', mime_type: 'image/png', attachment_id: 'image-1', name: 'offscreen.png' },
          { type: 'attachment', mime_type: 'text/plain', attachment_id: 'file-1', name: 'run.txt' },
        ]}
      />,
    );
    expect(getMessageAttachmentDownloadUrl).not.toHaveBeenCalled();
    fireEvent.click(getByRole('button', { name: 'Preview run.txt' }));
    await waitFor(() => expect(getMessageAttachmentDownloadUrl).toHaveBeenCalledWith(
      'project-1', 'message-1', 'file-1',
    ));
    expect(getMessageAttachmentDownloadUrl).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(onPreview).toHaveBeenCalledWith(expect.objectContaining({
      attachment_id: 'file-1', url: 'https://signed.example/file',
    })));
  });

  test('keeps the descriptor and exposes retryable failure state on resolver failure', async () => {
    getMessageAttachmentDownloadUrl.mockRejectedValue(new Error('503'));
    const { getByRole } = render(
      <MessageAttachments
        projectId="project-1"
        messageId="message-1"
        onPreview={vi.fn()}
        attachments={[
          { type: 'attachment', mime_type: 'text/plain', attachment_id: 'file-1', name: 'run.txt' },
        ]}
      />,
    );
    fireEvent.click(getByRole('button', { name: 'Preview run.txt' }));
    await waitFor(() => expect(getByRole('button', {
      name: 'Preview run.txt (preview unavailable; retry)',
    })).toBeTruthy());
  });

  test('invalidates a failed image URL so the next preview re-resolves it', async () => {
    getMessageAttachmentDownloadUrl.mockResolvedValue({ downloadUrl: 'https://signed.example/fresh', expiresAt: Date.now() + 60_000 });
    const { getByRole } = render(
      <MessageAttachments
        projectId="project-1"
        messageId="message-1"
        onPreview={vi.fn()}
        attachments={[{ type: 'image', mime_type: 'image/png', attachment_id: 'image-1', name: 'image.png', url: 'https://signed.example/stale' }]}
      />,
    );
    fireEvent.error(getByRole('img', { name: 'image.png' }));
    expect(invalidateMessageAttachmentDownloadUrl).toHaveBeenCalledWith('project-1', 'message-1', 'image-1');
    fireEvent.click(getByRole('button', { name: /preview image.png/i }));
    await waitFor(() => expect(getMessageAttachmentDownloadUrl).toHaveBeenCalledTimes(1));
  });
});
