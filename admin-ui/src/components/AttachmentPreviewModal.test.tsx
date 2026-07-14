import { describe, test, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, within } from '@testing-library/react';
import { AttachmentPreviewModal } from './AttachmentPreviewModal';
import type { MessageAttachment } from '../types/messages';

// happy-dom does not implement the Object URL APIs; the modal uses them to
// build a blob preview for inline base64 PDFs. Stub them so the type-branching
// under test is exercised deterministically.
let createObjectURL: ReturnType<typeof vi.fn>;
let revokeObjectURL: ReturnType<typeof vi.fn>;
let prevCreate: typeof URL.createObjectURL | undefined;
let prevRevoke: typeof URL.revokeObjectURL | undefined;

beforeEach(() => {
  // Stub only the Object-URL methods (happy-dom omits them) — leave the URL
  // constructor intact so iframe page-loading inside happy-dom keeps working.
  prevCreate = URL.createObjectURL;
  prevRevoke = URL.revokeObjectURL;
  createObjectURL = vi.fn(() => 'blob:mock-object-url');
  revokeObjectURL = vi.fn();
  URL.createObjectURL = createObjectURL as unknown as typeof URL.createObjectURL;
  URL.revokeObjectURL = revokeObjectURL as unknown as typeof URL.revokeObjectURL;
});

afterEach(() => {
  URL.createObjectURL = prevCreate as typeof URL.createObjectURL;
  URL.revokeObjectURL = prevRevoke as typeof URL.revokeObjectURL;
});

function renderModal(attachment: MessageAttachment) {
  return render(
    <AttachmentPreviewModal attachment={attachment} open onClose={() => {}} />,
  );
}

describe('AttachmentPreviewModal type branching', () => {
  test('renders nothing when closed', () => {
    const { container } = render(
      <AttachmentPreviewModal
        attachment={{ type: 'image', mime_type: 'image/png', data: 'AAAA' }}
        open={false}
        onClose={() => {}}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  test('image with inline base64 data renders <img> from a data URL', () => {
    renderModal({ type: 'image', mime_type: 'image/png', data: 'iVBORw0KGgo=', name: 'shot.png' });
    const img = screen.getByAltText('shot.png') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('data:image/png;base64,iVBORw0KGgo=');
  });

  test('image with only a remote url renders <img> from that url', () => {
    renderModal({ type: 'image', mime_type: 'image/png', url: 'https://gcs/signed/img.png', name: 'remote.png' });
    const img = screen.getByAltText('remote.png') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('https://gcs/signed/img.png');
  });

  test('PDF with a remote url renders an iframe pointed at that url (the operator case)', () => {
    renderModal({ type: 'document', mime_type: 'application/pdf', url: 'https://gcs/signed/report.pdf', name: 'report.pdf' });
    const frame = screen.getByTitle('report.pdf') as HTMLIFrameElement;
    expect(frame.tagName).toBe('IFRAME');
    expect(frame.getAttribute('src')).toBe('https://gcs/signed/report.pdf');
    // Regression guard: url-only PDFs must not hit the "Unable to load" branch.
    expect(screen.queryByText('Unable to load PDF data')).toBeNull();
  });

  test('PDF with inline base64 data renders an iframe from a blob object URL', () => {
    renderModal({ type: 'document', mime_type: 'application/pdf', data: 'JVBERi0=', name: 'inline.pdf' });
    const frame = screen.getByTitle('inline.pdf') as HTMLIFrameElement;
    expect(frame.getAttribute('src')).toBe('blob:mock-object-url');
    expect(createObjectURL).toHaveBeenCalled();
  });

  test('PDF with neither data nor url shows the unavailable message', () => {
    renderModal({ type: 'document', mime_type: 'application/pdf', name: 'empty.pdf' });
    expect(screen.getByText('Unable to load PDF data')).toBeTruthy();
    expect(screen.queryByTitle('empty.pdf')).toBeNull();
  });

  test('non-previewable type shows the no-preview message, not a viewer', () => {
    renderModal({ type: 'binary', mime_type: 'application/octet-stream', data: 'AAAA', name: 'data.bin' });
    expect(screen.getByText(/No preview available/)).toBeTruthy();
  });

  test('remote-url attachment exposes always-visible Open-in-new-tab and Download fallbacks', () => {
    renderModal({ type: 'document', mime_type: 'application/pdf', url: 'https://gcs/signed/report.pdf', name: 'report.pdf' });
    const dialog = screen.getByRole('dialog');
    const openLink = within(dialog).getByText('Open in new tab').closest('a') as HTMLAnchorElement;
    expect(openLink.getAttribute('href')).toBe('https://gcs/signed/report.pdf');
    expect(openLink.getAttribute('target')).toBe('_blank');
    const downloadLink = within(dialog).getByText('Download').closest('a') as HTMLAnchorElement;
    expect(downloadLink.getAttribute('href')).toBe('https://gcs/signed/report.pdf');
    expect(downloadLink.getAttribute('download')).toBe('report.pdf');
  });

  test('inline-data attachment exposes a Download button (blob path)', () => {
    renderModal({ type: 'binary', mime_type: 'application/octet-stream', data: 'AAAA', name: 'data.bin' });
    const dialog = screen.getByRole('dialog');
    const downloadBtn = within(dialog).getByText('Download').closest('button');
    expect(downloadBtn).toBeTruthy();
  });

  test('Escape key invokes onClose', () => {
    const onClose = vi.fn();
    render(
      <AttachmentPreviewModal
        attachment={{ type: 'document', mime_type: 'application/pdf', url: 'https://gcs/x.pdf', name: 'x.pdf' }}
        open
        onClose={onClose}
      />,
    );
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape', code: 'Escape' });
    expect(onClose).toHaveBeenCalled();
  });
});
