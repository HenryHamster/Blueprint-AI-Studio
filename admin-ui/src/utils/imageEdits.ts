// Small client-side image edits (rotate / crop) applied to a pending chat
// attachment before it is uploaded. The canvas re-encode produces a brand-new
// `File` so the edited bytes are what actually get sent — not just a visual
// transform. The pure helpers (mime/extension selection, crop-region math) are
// unit-tested; the canvas orchestration guards against a missing 2D context so
// it degrades cleanly in environments without a real canvas.

const MIME_TO_EXT: Record<string, string> = {
  'image/jpeg': 'jpg',
  'image/webp': 'webp',
  'image/png': 'png',
};

/**
 * Choose the output MIME for a canvas re-encode. Canvas `toBlob` only reliably
 * emits png/jpeg/webp, so anything else (gif, svg, bmp, …) falls back to png.
 * jpeg is preserved (lossy but far smaller for photos); png is the safe default.
 */
export function outputMimeForEdit(mime: string | null | undefined): string {
  const m = (mime || '').toLowerCase();
  if (m === 'image/jpeg' || m === 'image/jpg') return 'image/jpeg';
  if (m === 'image/webp') return 'image/webp';
  return 'image/png';
}

/**
 * Keep the original filename when its extension already matches the output MIME
 * (treating .jpg/.jpeg as equivalent), otherwise swap in the correct extension
 * so the uploaded name is not misleading after a format change (e.g. gif→png).
 */
export function renameForMime(name: string, mime: string): string {
  const ext = MIME_TO_EXT[mime] ?? 'png';
  const dot = name.lastIndexOf('.');
  const base = dot > 0 ? name.slice(0, dot) : name;
  const curExt = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  if (mime === 'image/jpeg' && (curExt === 'jpg' || curExt === 'jpeg')) return name;
  if (curExt === ext) return name;
  return `${base || 'image'}.${ext}`;
}

export interface CropSelection {
  /** Selection origin/size in displayed (CSS) pixels relative to the image box. */
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface CropRegion {
  /** Crop origin/size in the image's natural (source) pixels. */
  sx: number;
  sy: number;
  sw: number;
  sh: number;
}

function clampInt(value: number, min: number, max: number): number {
  const v = Math.round(value);
  if (v < min) return min;
  if (v > max) return max;
  return v;
}

/**
 * Map a selection rectangle drawn over the *displayed* image (which is sized by
 * max-width/max-height only, so its client box equals the rendered content with
 * no letterboxing) onto the image's natural pixel grid, clamped to bounds and
 * guaranteed at least 1×1. Degenerate inputs (zero display/natural size) fall
 * back to the full image so a crop can never produce an empty canvas.
 */
export function computeCropRegion(
  sel: CropSelection,
  displayW: number,
  displayH: number,
  naturalW: number,
  naturalH: number,
): CropRegion {
  if (displayW <= 0 || displayH <= 0 || naturalW <= 0 || naturalH <= 0) {
    return { sx: 0, sy: 0, sw: Math.max(1, naturalW), sh: Math.max(1, naturalH) };
  }
  const scaleX = naturalW / displayW;
  const scaleY = naturalH / displayH;
  const sx = clampInt(sel.x * scaleX, 0, naturalW - 1);
  const sy = clampInt(sel.y * scaleY, 0, naturalH - 1);
  const sw = clampInt(sel.w * scaleX, 1, naturalW - sx);
  const sh = clampInt(sel.h * scaleY, 1, naturalH - sy);
  return { sx, sy, sw, sh };
}

function loadImageFromFile(file: File): Promise<{ img: HTMLImageElement; url: string }> {
  return new Promise((resolve, reject) => {
    if (typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function') {
      reject(new Error('Object URLs are unavailable in this environment'));
      return;
    }
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => resolve({ img, url });
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error('Failed to load image for editing'));
    };
    img.src = url;
  });
}

async function canvasToFile(canvas: HTMLCanvasElement, srcFile: File): Promise<File> {
  const mime = outputMimeForEdit(srcFile.type);
  const blob = await new Promise<Blob | null>(resolve => {
    canvas.toBlob(resolve, mime, mime === 'image/jpeg' ? 0.92 : undefined);
  });
  if (!blob) throw new Error('Failed to encode edited image');
  return new File([blob], renameForMime(srcFile.name || 'image', mime), { type: mime });
}

/**
 * Rotate an image file 90° clockwise, returning a re-encoded File. The output
 * canvas swaps width/height because a quarter turn transposes the dimensions.
 */
export async function rotateImageClockwise(file: File): Promise<File> {
  const { img, url } = await loadImageFromFile(file);
  try {
    const w = img.naturalWidth || img.width;
    const h = img.naturalHeight || img.height;
    const canvas = document.createElement('canvas');
    canvas.width = h;
    canvas.height = w;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas 2D context unavailable');
    ctx.translate(h / 2, w / 2);
    ctx.rotate(Math.PI / 2);
    ctx.drawImage(img, -w / 2, -h / 2);
    return await canvasToFile(canvas, file);
  } finally {
    URL.revokeObjectURL(url);
  }
}

/**
 * Crop an image file to `region` (natural pixels), returning a re-encoded File.
 */
export async function cropImageFile(file: File, region: CropRegion): Promise<File> {
  const { img, url } = await loadImageFromFile(file);
  try {
    const canvas = document.createElement('canvas');
    canvas.width = region.sw;
    canvas.height = region.sh;
    const ctx = canvas.getContext('2d');
    if (!ctx) throw new Error('Canvas 2D context unavailable');
    ctx.drawImage(
      img,
      region.sx, region.sy, region.sw, region.sh,
      0, 0, region.sw, region.sh,
    );
    return await canvasToFile(canvas, file);
  } finally {
    URL.revokeObjectURL(url);
  }
}
