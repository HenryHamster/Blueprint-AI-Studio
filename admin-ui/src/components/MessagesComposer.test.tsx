import { afterEach, describe, test, expect, vi } from 'vitest';
import { render, screen, fireEvent, within, waitFor } from '@testing-library/react';
import { createRef } from 'react';
import { MessagesComposer, type PendingAttachment } from './MessagesComposer';

// Stub the Object-URL APIs happy-dom omits; the composer/modal build blob URLs.
const prevCreate = URL.createObjectURL;
const prevRevoke = URL.revokeObjectURL;
URL.createObjectURL = vi.fn(() => 'blob:mock') as unknown as typeof URL.createObjectURL;
URL.revokeObjectURL = vi.fn() as unknown as typeof URL.revokeObjectURL;

afterEach(() => {
  URL.createObjectURL = prevCreate;
  URL.revokeObjectURL = prevRevoke;
});

function imageAttachment(overrides: Partial<PendingAttachment> = {}): PendingAttachment {
  return {
    id: 'img-1',
    file: new File(['x'], 'shot.png', { type: 'image/png' }),
    previewUrl: 'blob:preview',
    mime_type: 'image/png',
    name: 'shot.png',
    size_bytes: 2048,
    ...overrides,
  };
}

function renderComposer(props: Partial<React.ComponentProps<typeof MessagesComposer>> = {}) {
  const ref = createRef<HTMLTextAreaElement>();
  return render(
    <MessagesComposer
      textareaRef={ref}
      newType="FEEDBACK"
      newContent=""
      submitting={false}
      replyTo={null}
      chrome="minimal"
      onChangeType={() => {}}
      onChangeContent={() => {}}
      onCancelReply={() => {}}
      onSubmit={() => {}}
      {...props}
    />,
  );
}

describe('MessagesComposer — live math preview', () => {
  test('shows a math preview strip with KaTeX when the draft contains $...$ delimiters', async () => {
    const { container } = renderComposer({ newContent: 'Energy $E = mc^2$ is famous.' });
    const preview = screen.getByLabelText('Math preview');
    expect(preview).toBeTruthy();
    await waitFor(() => {
      expect(container.querySelector('.katex')).toBeTruthy();
    });
  });

  test('does not render a preview strip for plain text without math delimiters', () => {
    renderComposer({ newContent: 'Hello, just a plain message.' });
    expect(screen.queryByLabelText('Math preview')).toBeNull();
  });

  test('does not render a preview strip when the draft is empty', () => {
    renderComposer({ newContent: '' });
    expect(screen.queryByLabelText('Math preview')).toBeNull();
  });
});

describe('MessagesComposer — insert equation', () => {
  test('renders an Insert equation button in minimal chrome', () => {
    renderComposer({ chrome: 'minimal' });
    expect(screen.getByLabelText('Insert equation')).toBeTruthy();
  });

  test('renders an Insert equation button in full chrome', () => {
    renderComposer({ chrome: 'full' });
    expect(screen.getByLabelText('Insert equation')).toBeTruthy();
  });

  test('clicking insert equation calls onChangeContent with $equation$ appended', () => {
    const onChangeContent = vi.fn();
    renderComposer({ newContent: 'Energy: ', onChangeContent });
    fireEvent.click(screen.getByLabelText('Insert equation'));
    expect(onChangeContent).toHaveBeenCalledWith('Energy: $equation$');
  });

  test('inserts $equation$ at cursor position when content is empty', () => {
    const onChangeContent = vi.fn();
    renderComposer({ newContent: '', onChangeContent });
    fireEvent.click(screen.getByLabelText('Insert equation'));
    expect(onChangeContent).toHaveBeenCalledWith('$equation$');
  });

  test('insert equation button is disabled when composerDisabled', () => {
    renderComposer({ disabledReason: 'Composer disabled', chrome: 'minimal' });
    const btn = screen.getByLabelText('Insert equation');
    expect((btn as HTMLButtonElement).disabled).toBe(true);
  });
});

describe('MessagesComposer — clickable pending image preview', () => {
  test('renders an exact-revision source chip and exposes explicit removal', () => {
    const onRemoveArtifactRef = vi.fn();
    renderComposer({
      artifactRefs: [{
        id: 'tool-ref-7', artifact_ref_id: 'tool-ref-7', kind: 'notes', revision: 7,
      }],
      onRemoveArtifactRef,
    });

    expect(screen.getByLabelText('Attached sources')).toBeTruthy();
    expect(screen.getByText('Notes revision 7')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Remove Notes revision 7'));
    expect(onRemoveArtifactRef).toHaveBeenCalledWith('tool-ref-7');
  });

  test('renders every message-scoped source ref, not only tool artifacts', () => {
    const onRemoveArtifactRef = vi.fn();
    renderComposer({
      sourceRefs: [{ id: 'task-7', kind: 'task', label: 'Runner investigation', revision: 7 }],
      onRemoveArtifactRef,
    });

    expect(screen.getByText('Runner investigation revision 7')).toBeTruthy();
    fireEvent.click(screen.getByLabelText('Remove Runner investigation revision 7'));
    expect(onRemoveArtifactRef).toHaveBeenCalledWith('task-7');
  });

  test('renders an open-preview button for a pending image with a preview url', () => {
    renderComposer({ pendingAttachments: [imageAttachment()] });
    expect(screen.getByLabelText('Open shot.png preview')).toBeTruthy();
  });

  test('a pending non-image (no preview url) is not clickable to open a preview', () => {
    renderComposer({
      pendingAttachments: [imageAttachment({ id: 'f', name: 'notes.txt', mime_type: 'text/plain', previewUrl: null })],
    });
    expect(screen.queryByLabelText('Open notes.txt preview')).toBeNull();
    // The chip still renders (filename visible) and remains removable.
    expect(screen.getByText('notes.txt')).toBeTruthy();
    expect(screen.getByLabelText('Remove notes.txt')).toBeTruthy();
  });

  test('clicking the chip opens the full-size preview modal at the object url', () => {
    renderComposer({ pendingAttachments: [imageAttachment()], onReplaceAttachmentFile: () => {} });
    fireEvent.click(screen.getByLabelText('Open shot.png preview'));
    const dialog = screen.getByRole('dialog');
    const img = within(dialog).getByAltText('shot.png') as HTMLImageElement;
    expect(img.getAttribute('src')).toBe('blob:preview');
  });

  test('edit controls appear only when onReplaceAttachmentFile is provided', () => {
    const { rerender } = renderComposer({ pendingAttachments: [imageAttachment()] });
    fireEvent.click(screen.getByLabelText('Open shot.png preview'));
    // No replace handler → read-only viewer, no edit toolbar.
    expect(screen.queryByLabelText('Rotate image 90 degrees')).toBeNull();

    const ref = createRef<HTMLTextAreaElement>();
    rerender(
      <MessagesComposer
        textareaRef={ref}
        newType="FEEDBACK"
        newContent=""
        submitting={false}
        replyTo={null}
        chrome="minimal"
        onChangeType={() => {}}
        onChangeContent={() => {}}
        onCancelReply={() => {}}
        onSubmit={() => {}}
        pendingAttachments={[imageAttachment()]}
        onReplaceAttachmentFile={() => {}}
      />,
    );
    fireEvent.click(screen.getByLabelText('Open shot.png preview'));
    expect(screen.getByLabelText('Rotate image 90 degrees')).toBeTruthy();
  });

  test('Remove in the preview editor discards the attachment and closes the modal', () => {
    const onRemoveAttachment = vi.fn();
    renderComposer({
      pendingAttachments: [imageAttachment()],
      onRemoveAttachment,
      onReplaceAttachmentFile: () => {},
    });
    fireEvent.click(screen.getByLabelText('Open shot.png preview'));
    fireEvent.click(screen.getByLabelText('Remove image'));
    expect(onRemoveAttachment).toHaveBeenCalledWith('img-1');
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  test('Replace opens the hidden replace file input', () => {
    const { container } = renderComposer({
      pendingAttachments: [imageAttachment()],
      onReplaceAttachmentFile: () => {},
    });
    const replaceInput = container.querySelector<HTMLInputElement>('input[type="file"][accept="image/*"]');
    expect(replaceInput).toBeTruthy();
    const clickSpy = vi.spyOn(replaceInput as HTMLInputElement, 'click');
    fireEvent.click(screen.getByLabelText('Open shot.png preview'));
    fireEvent.click(screen.getByLabelText('Replace image'));
    expect(clickSpy).toHaveBeenCalledOnce();
  });
});
