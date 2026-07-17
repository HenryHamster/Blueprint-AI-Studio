export interface WhiteboardArtifactPoint {
  x: number;
  y: number;
}

export interface WhiteboardArtifactStroke {
  color: string;
  width: number;
  points: WhiteboardArtifactPoint[];
}

export interface WhiteboardArtifactNode {
  type: 'text' | 'image' | 'document';
  x: number;
  y: number;
  w: number;
  h: number;
  text?: string;
  color?: string;
  dataUrl?: string;
  name?: string;
}

export interface WhiteboardArtifactContent {
  strokes: WhiteboardArtifactStroke[];
  nodes: WhiteboardArtifactNode[];
}

export interface RenderedWhiteboardArtifact {
  file: File;
  checksum: string;
  width: number;
  height: number;
}

const WIDTH = 1600;
const HEIGHT = 900;
const PADDING = 56;
const MAX_POINTS = 200_000;
const MAX_NODES = 2_000;
const MAX_IMAGES = 64;
const MAX_IMAGE_SOURCE_BYTES = 32 * 1024 * 1024;
const MAX_NODE_TEXT_CHARS = 2 * 1024 * 1024;
const IMAGE_LOAD_CONCURRENCY = 4;
const IMAGE_LOAD_TIMEOUT_MS = 10_000;
const MAX_RENDER_MS = 15_000;
const DRAW_YIELD_INTERVAL = 5_000;

function validateContentSize(content: WhiteboardArtifactContent): void {
  let points = 0;
  let images = 0;
  let imageBytes = 0;
  let nodeTextChars = 0;
  for (const stroke of content.strokes) {
    points += stroke.points.length;
    if (points > MAX_POINTS) throw new Error(`Whiteboard has more than ${MAX_POINTS.toLocaleString()} points and cannot be shared.`);
  }
  if (content.nodes.length > MAX_NODES) throw new Error(`Whiteboard has more than ${MAX_NODES.toLocaleString()} nodes and cannot be shared.`);
  for (const node of content.nodes) {
    nodeTextChars += (node.text?.length ?? 0) + (node.name?.length ?? 0);
    if (nodeTextChars > MAX_NODE_TEXT_CHARS) throw new Error('Whiteboard node text exceeds the 2 MB sharing limit.');
    if (node.type !== 'image' || !node.dataUrl) continue;
    images += 1;
    imageBytes += node.dataUrl.length;
    if (images > MAX_IMAGES) throw new Error(`Whiteboard has more than ${MAX_IMAGES} images and cannot be shared.`);
    if (imageBytes > MAX_IMAGE_SOURCE_BYTES) throw new Error('Whiteboard embedded images exceed the 32 MB sharing limit.');
  }
}

function contentBounds(content: WhiteboardArtifactContent) {
  let left = Number.POSITIVE_INFINITY;
  let top = Number.POSITIVE_INFINITY;
  let right = Number.NEGATIVE_INFINITY;
  let bottom = Number.NEGATIVE_INFINITY;
  const include = (x: number, y: number) => {
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    left = Math.min(left, x);
    top = Math.min(top, y);
    right = Math.max(right, x);
    bottom = Math.max(bottom, y);
  };
  for (const stroke of content.strokes) {
    for (const point of stroke.points) include(point.x, point.y);
  }
  for (const node of content.nodes) {
    include(node.x, node.y);
    include(node.x + node.w, node.y + node.h);
  }
  if (!Number.isFinite(left)) return { left: 0, top: 0, width: WIDTH, height: HEIGHT };
  return {
    left,
    top,
    width: Math.max(1, right - left),
    height: Math.max(1, bottom - top),
  };
}

function wrapText(ctx: CanvasRenderingContext2D, text: string, width: number, maxLines: number) {
  const words = text.trim().split(/\s+/).filter(Boolean);
  const lines: string[] = [];
  let line = '';
  for (const word of words) {
    const next = line ? `${line} ${word}` : word;
    if (line && ctx.measureText(next).width > width) {
      lines.push(line);
      line = word;
      if (lines.length >= maxLines) break;
    } else {
      line = next;
    }
  }
  if (lines.length < maxLines && line) lines.push(line);
  return lines;
}

function loadImage(source: string, deadlineAt: number): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const image = new Image();
    const remaining = deadlineAt - Date.now();
    if (remaining <= 0) {
      reject(new Error('Whiteboard rendering exceeded the 15 second deadline.'));
      return;
    }
    const deadlineLimited = remaining <= IMAGE_LOAD_TIMEOUT_MS;
    const timer = window.setTimeout(() => {
      image.onload = null;
      image.onerror = null;
      reject(new Error(deadlineLimited
        ? 'Whiteboard rendering exceeded the 15 second deadline.'
        : 'A Whiteboard image timed out while rendering.'));
    }, Math.min(IMAGE_LOAD_TIMEOUT_MS, remaining));
    image.onload = () => {
      window.clearTimeout(timer);
      resolve(image);
    };
    image.onerror = () => {
      window.clearTimeout(timer);
      reject(new Error('A Whiteboard image could not be rendered.'));
    };
    image.src = source;
  });
}

async function loadNodeImages(nodes: WhiteboardArtifactNode[], deadlineAt: number): Promise<Array<HTMLImageElement | null>> {
  const images: Array<HTMLImageElement | null> = Array(nodes.length).fill(null);
  const indexes = nodes.flatMap((node, index) => (
    node.type === 'image' && node.dataUrl ? [index] : []
  ));
  let cursor = 0;
  const worker = async () => {
    while (cursor < indexes.length) {
      const index = indexes[cursor];
      cursor += 1;
      images[index] = await loadImage(nodes[index].dataUrl as string, deadlineAt);
    }
  };
  await Promise.all(Array.from(
    { length: Math.min(IMAGE_LOAD_CONCURRENCY, indexes.length) },
    () => worker(),
  ));
  return images;
}

async function yieldToBrowser(deadlineAt: number): Promise<void> {
  await new Promise<void>(resolve => window.setTimeout(resolve, 0));
  if (Date.now() >= deadlineAt) throw new Error('Whiteboard rendering exceeded the 15 second deadline.');
}

function withinRenderDeadline<T>(work: Promise<T>, deadlineAt: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const remaining = deadlineAt - Date.now();
    if (remaining <= 0) {
      reject(new Error('Whiteboard rendering exceeded the 15 second deadline.'));
      return;
    }
    const timer = window.setTimeout(
      () => reject(new Error('Whiteboard rendering exceeded the 15 second deadline.')),
      remaining,
    );
    work.then(
      value => { window.clearTimeout(timer); resolve(value); },
      error => { window.clearTimeout(timer); reject(error); },
    );
  });
}

function drawNode(
  ctx: CanvasRenderingContext2D,
  node: WhiteboardArtifactNode,
  image?: HTMLImageElement | null,
) {
  ctx.fillStyle = '#202338';
  ctx.strokeStyle = '#606783';
  ctx.lineWidth = 1.5;
  ctx.beginPath();
  ctx.roundRect(node.x, node.y, node.w, node.h, 8);
  ctx.fill();
  ctx.stroke();

  if (node.type === 'image' && image) {
    const scale = Math.min(node.w / image.width, node.h / image.height);
    const width = image.width * scale;
    const height = image.height * scale;
    ctx.drawImage(image, node.x + (node.w - width) / 2, node.y + (node.h - height) / 2, width, height);
    return;
  }

  ctx.fillStyle = node.color || '#e2e8f0';
  ctx.font = '16px system-ui, sans-serif';
  ctx.textBaseline = 'top';
  const label = node.type === 'document' ? node.name || 'Document' : node.text || '';
  const lines = wrapText(ctx, label, Math.max(8, node.w - 24), Math.max(1, Math.floor((node.h - 24) / 21)));
  lines.forEach((line, index) => ctx.fillText(line, node.x + 12, node.y + 12 + index * 21));
}

export async function renderWhiteboardArtifact(
  content: WhiteboardArtifactContent,
  revision: number,
): Promise<RenderedWhiteboardArtifact> {
  const deadlineAt = Date.now() + MAX_RENDER_MS;
  validateContentSize(content);
  const canvas = document.createElement('canvas');
  canvas.width = WIDTH;
  canvas.height = HEIGHT;
  const ctx = canvas.getContext('2d');
  if (!ctx) throw new Error('Whiteboard rendering is unavailable in this browser.');

  // Image decode/network work is independent. Start every load together, then
  // draw in node order after all assets are ready so overlap semantics stay exact.
  const nodeImages = await loadNodeImages(content.nodes, deadlineAt);

  ctx.fillStyle = '#1a1a2e';
  ctx.fillRect(0, 0, WIDTH, HEIGHT);
  const bounds = contentBounds(content);
  const scale = Math.min(
    (WIDTH - PADDING * 2) / bounds.width,
    (HEIGHT - PADDING * 2) / bounds.height,
    2,
  );
  const offsetX = (WIDTH - bounds.width * scale) / 2 - bounds.left * scale;
  const offsetY = (HEIGHT - bounds.height * scale) / 2 - bounds.top * scale;
  ctx.save();
  ctx.setTransform(scale, 0, 0, scale, offsetX, offsetY);
  let pointsSinceYield = 0;
  for (const stroke of content.strokes) {
    if (stroke.points.length < 2) continue;
    ctx.beginPath();
    ctx.strokeStyle = stroke.color;
    ctx.lineWidth = stroke.width;
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.moveTo(stroke.points[0].x, stroke.points[0].y);
    for (let index = 1; index < stroke.points.length; index += 1) {
      const point = stroke.points[index];
      ctx.lineTo(point.x, point.y);
      pointsSinceYield += 1;
      if (pointsSinceYield >= DRAW_YIELD_INTERVAL) {
        pointsSinceYield = 0;
        await yieldToBrowser(deadlineAt);
      }
    }
    ctx.stroke();
  }
  for (let index = 0; index < content.nodes.length; index += 1) {
    drawNode(ctx, content.nodes[index], nodeImages[index]);
    if ((index + 1) % 100 === 0) await yieldToBrowser(deadlineAt);
  }
  ctx.restore();

  if (Date.now() >= deadlineAt) throw new Error('Whiteboard rendering exceeded the 15 second deadline.');

  const blob = await withinRenderDeadline(new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(value => value ? resolve(value) : reject(new Error('Whiteboard rendering failed.')), 'image/png');
  }), deadlineAt);
  const bytes = await withinRenderDeadline(blob.arrayBuffer(), deadlineAt);
  const digest = await withinRenderDeadline(crypto.subtle.digest('SHA-256', bytes), deadlineAt);
  const checksum = [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
  return {
    file: new File([blob], `whiteboard-r${revision}.png`, { type: 'image/png' }),
    checksum,
    width: WIDTH,
    height: HEIGHT,
  };
}
