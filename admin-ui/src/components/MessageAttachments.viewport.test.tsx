import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MessageAttachments } from './MessageAttachments';
import type { MessageAttachment } from '../types/messages';
import { expectNoOverflowAt, expectMinTouchTargetAt, stressStrings } from '../test/viewport';

vi.mock('../strategicStateApi', async () => {
  const { createStrategicStateApiMock } = await import('../test/strategic-state-api-mock-complete');
  return createStrategicStateApiMock({
    getMessageAttachmentDownloadUrl: vi.fn(async () => ({
      downloadUrl: 'https://example.com/file.pdf',
      expiresAt: Date.now() + 3600_000,
    })),
    invalidateMessageAttachmentDownloadUrl: vi.fn(),
  });
});

const fileAttachment: MessageAttachment = {
  type: 'file',
  mime_type: 'application/pdf',
  attachment_id: 'att-1',
  name: 'report.pdf',
  size_bytes: 204800,
};

const imageAttachment: MessageAttachment = {
  type: 'image',
  mime_type: 'image/png',
  attachment_id: 'att-2',
  name: 'screenshot.png',
  data: 'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==',
};

const longNameAttachment: MessageAttachment = {
  type: 'file',
  mime_type: 'text/plain',
  attachment_id: 'att-3',
  name: stressStrings.title,
  size_bytes: 512,
};

describe('MessageAttachments — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  async function render(attachments: MessageAttachment[]) {
    host = document.createElement('div');
    host.style.width = '375px';
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <MessageAttachments
          attachments={attachments}
          onPreview={() => { /* no-op */ }}
          projectId="proj-1"
          messageId="msg-1"
        />,
      );
    });
  }

  test('file chip has no horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      await render([fileAttachment]);
    });
  });

  test('long filename chip does not overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      await render([longNameAttachment]);
    });
  });

  test('multiple chips wrap without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      await render([fileAttachment, fileAttachment, longNameAttachment]);
    });
  });

  test('file chip meets minimum touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      await render([fileAttachment]);
    });
  });

  test('image chip (data URI) meets minimum touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      await render([imageAttachment]);
    });
  });
});
