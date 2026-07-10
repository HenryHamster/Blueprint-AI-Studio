import { describe, it, expect, vi } from 'vitest';
import { fileToMessageAttachment } from './attachments';

describe('fileToMessageAttachment', () => {
  it('reads a text file and produces a correctly-shaped MessageAttachment', async () => {
    const content = 'Hello, world!';
    const file = new File([content], 'greeting.txt', { type: 'text/plain' });

    const att = await fileToMessageAttachment(file);

    expect(att).toEqual({
      type: 'file',
      mime_type: 'text/plain',
      data: expect.any(String),
      name: 'greeting.txt',
      size_bytes: content.length,
    });
    // Verify the base64 data round-trips correctly
    expect(atob(att.data as string)).toBe(content);
  });

  it('reads a small binary payload (image)', async () => {
    // A minimal 1×1 PNG (valid but trivial)
    const pngBytes = new Uint8Array([
      0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, // PNG signature
      0x00, 0x00, 0x00, 0x0D, 0x49, 0x48, 0x44, 0x52, // IHDR chunk
      0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, // 1×1
      0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53, // 8-bit grayscale
      0xDE, 0x00, 0x00, 0x00, 0x0C, 0x49, 0x44, 0x41, // IDAT chunk
      0x54, 0x08, 0xD7, 0x63, 0x68, 0x60, 0x00, 0x00, // (compressed data)
      0x00, 0x01, 0x00, 0x01, 0x00, 0x18, 0x2F, 0x7C, // …
      0xF3, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4E, // IEND chunk
      0x44, 0xAE, 0x42, 0x60, 0x82,
    ]);
    const file = new File([pngBytes], 'icon.png', { type: 'image/png' });

    const att = await fileToMessageAttachment(file);

    expect(att).toMatchObject({
      type: 'image',
      mime_type: 'image/png',
      name: 'icon.png',
      size_bytes: pngBytes.length,
    });
    expect(att.data).toEqual(expect.any(String));
  });

  it('defaults mime_type to application/octet-stream when type is empty', async () => {
    const file = new File(['data'], 'unknown.bin', { type: '' });

    const att = await fileToMessageAttachment(file);

    expect(att.mime_type).toBe('application/octet-stream');
  });

  it('includes the file name and size_bytes', async () => {
    const file = new File([new Uint8Array(65536)], 'large.bin', { type: 'application/octet-stream' });

    const att = await fileToMessageAttachment(file);

    expect(att.name).toBe('large.bin');
    expect(att.size_bytes).toBe(65536);
  });

  it('rejects when FileReader errors', async () => {
    // Mock FileReader to trigger the error path for this test only.
    const mock = vi.spyOn(FileReader.prototype, 'readAsDataURL')
      .mockImplementationOnce(function (this: FileReader) {
        // The onerror handler has already been assigned by fileToMessageAttachment
        // at this point; call it immediately to simulate a runtime error.
        if (this.onerror) {
          this.onerror(new ProgressEvent('error') as ProgressEvent<FileReader>);
        }
      });

    await expect(
      fileToMessageAttachment(new File(['x'], 'x.txt')),
    ).rejects.toThrow('FileReader error');

    mock.mockRestore();
  });
});
