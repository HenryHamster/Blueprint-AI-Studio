import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ToolComponentProps } from '../toolRegistry';
import { fetchToolContent, saveToolContent, ConflictError, type ToolContent } from '../api/projectTools';

// ---------------------------------------------------------------------------
// Canvas content model
//
// The whiteboard is a real canvas: freehand strokes PLUS placed nodes (text
// blocks, pasted/uploaded images, document/PDF cards). All coordinates are in
// "world" space; the viewport applies a pan/zoom transform on top so the same
// stored content renders identically regardless of how the operator is looking
// at it.
//
// Persistence goes through the existing project-tool content API (PUT with
// base_revision optimistic locking). Content is a free-form JSONB blob on the
// backend, so no schema migration is required — we bump the payload to
// { version: 2, strokes, nodes } and stay backward-compatible with the legacy
// { strokes } shape on load.
// ---------------------------------------------------------------------------

interface Point {
  x: number;
  y: number;
}

interface Stroke {
  color: string;
  width: number;
  points: Point[];
}

interface BaseNode {
  id: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

interface TextNode extends BaseNode {
  type: 'text';
  text: string;
  color?: string;
}

interface ImageNode extends BaseNode {
  type: 'image';
  dataUrl: string;
  name?: string;
}

interface DocumentNode extends BaseNode {
  type: 'document';
  dataUrl: string;
  name: string;
  mime: string;
}

type CanvasNode = TextNode | ImageNode | DocumentNode;

interface CanvasContent {
  version: 2;
  strokes: Stroke[];
  nodes: CanvasNode[];
}

interface View {
  tx: number;
  ty: number;
  scale: number;
}

const PRESET_COLORS = ['#e2e8f0', '#f87171', '#60a5fa', '#4ade80', '#facc15'];
const PRESET_WIDTHS = [2, 4, 8, 14];
const DEFAULT_COLOR = PRESET_COLORS[0];
const DEFAULT_WIDTH = PRESET_WIDTHS[1]; // 4px
const MIN_SCALE = 0.2;
const MAX_SCALE = 4;
const MIN_NODE_SIZE = 40;
// Pasted/uploaded assets are stored inline as data URLs inside the tool
// content JSONB. Cap the size so a single huge file can't bloat the document
// (and blow past sane request limits). ~4 MiB of raw bytes ≈ ~5.4 MiB base64.
const MAX_ASSET_BYTES = 4 * 1024 * 1024;

// ---------------------------------------------------------------------------
// Content (de)serialization helpers
// ---------------------------------------------------------------------------

const EMPTY_CONTENT: CanvasContent = { version: 2, strokes: [], nodes: [] };

/** Normalize any stored content (legacy `{strokes}` or v2 `{strokes,nodes}`). */
function normalizeContent(raw: unknown): CanvasContent {
  if (!raw || typeof raw !== 'object') return { version: 2, strokes: [], nodes: [] };
  const obj = raw as Record<string, unknown>;
  const strokes = Array.isArray(obj.strokes) ? (obj.strokes as Stroke[]) : [];
  const nodes = Array.isArray(obj.nodes) ? (obj.nodes as CanvasNode[]) : [];
  return { version: 2, strokes, nodes };
}

/** Stable serialization used for dirty-tracking and conflict comparison. */
function serialize(content: CanvasContent): string {
  return JSON.stringify({ strokes: content.strokes, nodes: content.nodes });
}

function isEmpty(content: CanvasContent): boolean {
  return content.strokes.length === 0 && content.nodes.length === 0;
}

let nodeSeq = 0;
function newNodeId(): string {
  nodeSeq += 1;
  const rand = typeof crypto !== 'undefined' && 'randomUUID' in crypto ? crypto.randomUUID() : `${nodeSeq}`;
  return `n-${rand}`;
}

/** Read a File into a data URL, enforcing the size cap. */
function readFileAsDataUrl(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    if (file.size > MAX_ASSET_BYTES) {
      reject(new Error(`"${file.name}" is too large (${(file.size / (1024 * 1024)).toFixed(1)} MB). Max ${MAX_ASSET_BYTES / (1024 * 1024)} MB.`));
      return;
    }
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error(`Failed to read "${file.name}"`));
    reader.readAsDataURL(file);
  });
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

type LoadState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready' };

export default function WhiteboardTool({ projectId, tool }: ToolComponentProps) {
  const surfaceRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const imageInputRef = useRef<HTMLInputElement | null>(null);
  const docInputRef = useRef<HTMLInputElement | null>(null);

  const [loadState, setLoadState] = useState<LoadState>({ status: 'loading' });
  const [content, setContent] = useState<CanvasContent>(EMPTY_CONTENT);
  const [view, setView] = useState<View>({ tx: 0, ty: 0, scale: 1 });
  const [mode, setMode] = useState<'select' | 'draw'>('select');
  const [color, setColor] = useState(DEFAULT_COLOR);
  const [lineWidth, setLineWidth] = useState(DEFAULT_WIDTH);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [saveStatus, setSaveStatus] = useState<'idle' | 'saving' | 'error'>('idle');
  const [dirty, setDirty] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Refs mirror the latest values so async save handlers and pointer handlers
  // always read fresh state without re-binding.
  const contentRef = useRef<CanvasContent>(EMPTY_CONTENT);
  const revisionRef = useRef(0);
  const viewRef = useRef<View>(view);
  const lastSavedRef = useRef<string>('');
  const savingRef = useRef(false);
  const pendingRef = useRef(false);
  const mountedRef = useRef(true);

  // In-progress freehand stroke (world coords), not yet committed to content.
  const liveStrokeRef = useRef<Stroke | null>(null);
  const drawingRef = useRef(false);
  // Active pan / node-drag / node-resize gesture bookkeeping.
  const gestureRef = useRef<
    | null
    | { kind: 'pan'; startX: number; startY: number; startTx: number; startTy: number }
    | { kind: 'move'; id: string; startX: number; startY: number; origX: number; origY: number }
    | { kind: 'resize'; id: string; startX: number; startY: number; origW: number; origH: number }
  >(null);

  useEffect(() => {
    viewRef.current = view;
  }, [view]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // -----------------------------------------------------------------------
  // Load content on mount
  // -----------------------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data: ToolContent<unknown> = await fetchToolContent<unknown>(projectId, tool.id);
        if (cancelled) return;
        const normalized = normalizeContent(data.content);
        revisionRef.current = data.content_revision;
        contentRef.current = normalized;
        lastSavedRef.current = serialize(normalized);
        setContent(normalized);
        setDirty(false);
        setLoadState({ status: 'ready' });
      } catch (err) {
        if (cancelled) return;
        setLoadState({
          status: 'error',
          message: err instanceof Error ? err.message : 'Failed to load whiteboard',
        });
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectId, tool.id]);

  // -----------------------------------------------------------------------
  // Content mutation — the single funnel that keeps refs + dirty state in sync
  // -----------------------------------------------------------------------

  const applyContent = useCallback((next: CanvasContent) => {
    contentRef.current = next;
    setContent(next);
    setDirty(serialize(next) !== lastSavedRef.current);
  }, []);

  // -----------------------------------------------------------------------
  // Save path (fixes memo 5930a473: false "Saved" + dropped last edits)
  //
  // Correctness guarantees:
  //  - dirty state is derived from a content diff, never assumed; the status
  //    line shows "Saved" ONLY when the persisted snapshot matches current
  //    content.
  //  - a single-flight guard (savingRef) plus a pending flag means edits made
  //    while a save is in flight are never dropped — they trigger a follow-up
  //    save against the freshly-bumped revision.
  //  - unmount flushes any pending change instead of silently clearing the
  //    debounce timer.
  // -----------------------------------------------------------------------

  const mergeAfterConflict = useCallback(async () => {
    const data: ToolContent<unknown> = await fetchToolContent<unknown>(projectId, tool.id);
    const server = normalizeContent(data.content);
    const local = contentRef.current;
    // Strokes are additive: union, deduped by serialization.
    const seen = new Set(server.strokes.map(s => JSON.stringify(s)));
    const strokes: Stroke[] = [...server.strokes];
    for (const s of local.strokes) {
      const key = JSON.stringify(s);
      if (!seen.has(key)) {
        strokes.push(s);
        seen.add(key);
      }
    }
    // Nodes merge by id — the local edit wins for overlapping ids, and
    // server-only nodes are preserved so a concurrent writer's cards survive.
    const byId = new Map<string, CanvasNode>();
    for (const n of server.nodes) byId.set(n.id, n);
    for (const n of local.nodes) byId.set(n.id, n);
    const merged: CanvasContent = { version: 2, strokes, nodes: [...byId.values()] };
    revisionRef.current = data.content_revision;
    contentRef.current = merged;
    if (mountedRef.current) {
      setContent(merged);
      // The merged result still needs to be persisted against the adopted
      // server revision, so keep it dirty.
      setDirty(true);
    }
  }, [projectId, tool.id]);

  const flush = useCallback(async () => {
    if (savingRef.current) {
      pendingRef.current = true;
      return;
    }
    const serialized = serialize(contentRef.current);
    if (serialized === lastSavedRef.current) {
      if (mountedRef.current) setDirty(false);
      return;
    }
    savingRef.current = true;
    if (mountedRef.current) {
      setSaveStatus('saving');
      setSaveError(null);
    }
    try {
      const result = await saveToolContent(
        projectId,
        tool.id,
        { version: 2, strokes: contentRef.current.strokes, nodes: contentRef.current.nodes },
        revisionRef.current,
      );
      revisionRef.current = result.content_revision;
      lastSavedRef.current = serialized;
      if (mountedRef.current) {
        setSaveStatus('idle');
        setDirty(serialize(contentRef.current) !== lastSavedRef.current);
      }
    } catch (err) {
      if (err instanceof ConflictError) {
        try {
          await mergeAfterConflict();
          if (mountedRef.current) setSaveStatus('idle');
        } catch {
          if (mountedRef.current) {
            setSaveStatus('error');
            setSaveError('Save conflict — reload failed. Retry.');
          }
        }
      } else if (mountedRef.current) {
        setSaveStatus('error');
        setSaveError(err instanceof Error ? err.message : 'Save failed');
      }
    } finally {
      savingRef.current = false;
      if (pendingRef.current) {
        pendingRef.current = false;
        void flush();
      }
    }
  }, [projectId, tool.id, mergeAfterConflict]);

  // Debounced autosave. Re-armed on every content change while dirty; a bare
  // revision refresh (dirty=false) never schedules a save.
  useEffect(() => {
    if (loadState.status !== 'ready' || !dirty) return;
    const timer = setTimeout(() => {
      void flush();
    }, 1200);
    return () => clearTimeout(timer);
  }, [content, dirty, loadState.status, flush]);

  // Flush any pending change on unmount so switching away never drops edits.
  useEffect(() => {
    return () => {
      if (serialize(contentRef.current) !== lastSavedRef.current) {
        void flush();
      }
    };
  }, [flush]);

  // -----------------------------------------------------------------------
  // Rendering the freehand strokes onto the canvas (screen space).
  // -----------------------------------------------------------------------

  const redraw = useCallback(() => {
    const canvas = canvasRef.current;
    const surface = surfaceRef.current;
    if (!canvas || !surface) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const dpr = window.devicePixelRatio || 1;
    const rect = surface.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    const v = viewRef.current;
    const drawStroke = (stroke: Stroke) => {
      if (stroke.points.length < 2) return;
      ctx.beginPath();
      ctx.strokeStyle = stroke.color;
      ctx.lineWidth = Math.max(0.5, stroke.width * v.scale);
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      const p0 = stroke.points[0];
      ctx.moveTo(p0.x * v.scale + v.tx, p0.y * v.scale + v.ty);
      for (let i = 1; i < stroke.points.length; i++) {
        const p = stroke.points[i];
        ctx.lineTo(p.x * v.scale + v.tx, p.y * v.scale + v.ty);
      }
      ctx.stroke();
    };

    for (const stroke of contentRef.current.strokes) drawStroke(stroke);
    if (liveStrokeRef.current) drawStroke(liveStrokeRef.current);
  }, []);

  // Redraw whenever content or view changes, plus on resize.
  useEffect(() => {
    if (loadState.status !== 'ready') return;
    redraw();
    const onResize = () => redraw();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [content, view, loadState.status, redraw]);

  // -----------------------------------------------------------------------
  // Coordinate helpers
  // -----------------------------------------------------------------------

  const screenToWorld = useCallback((clientX: number, clientY: number): Point => {
    const surface = surfaceRef.current;
    const v = viewRef.current;
    if (!surface) return { x: 0, y: 0 };
    const rect = surface.getBoundingClientRect();
    return {
      x: (clientX - rect.left - v.tx) / v.scale,
      y: (clientY - rect.top - v.ty) / v.scale,
    };
  }, []);

  /** World coords of the current viewport center — used to place new nodes. */
  const viewportCenterWorld = useCallback((): Point => {
    const surface = surfaceRef.current;
    if (!surface) return { x: 100, y: 100 };
    const rect = surface.getBoundingClientRect();
    return screenToWorld(rect.left + rect.width / 2, rect.top + rect.height / 2);
  }, [screenToWorld]);

  // -----------------------------------------------------------------------
  // Surface pointer handling (pan in select mode, draw in draw mode)
  // -----------------------------------------------------------------------

  const onSurfacePointerDown = useCallback(
    (e: React.PointerEvent) => {
      if (loadState.status !== 'ready') return;
      // Only react to gestures that start on the empty surface — node handlers
      // stop propagation.
      setSelectedId(null);
      if (mode === 'draw') {
        drawingRef.current = true;
        const p = screenToWorld(e.clientX, e.clientY);
        liveStrokeRef.current = { color, width: lineWidth, points: [p] };
        (e.target as Element).setPointerCapture?.(e.pointerId);
      } else {
        const v = viewRef.current;
        gestureRef.current = {
          kind: 'pan',
          startX: e.clientX,
          startY: e.clientY,
          startTx: v.tx,
          startTy: v.ty,
        };
        (e.target as Element).setPointerCapture?.(e.pointerId);
      }
    },
    [loadState.status, mode, screenToWorld, color, lineWidth],
  );

  const onSurfacePointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (drawingRef.current && liveStrokeRef.current) {
        const p = screenToWorld(e.clientX, e.clientY);
        liveStrokeRef.current = { ...liveStrokeRef.current, points: [...liveStrokeRef.current.points, p] };
        redraw();
        return;
      }
      const g = gestureRef.current;
      if (!g) return;
      if (g.kind === 'pan') {
        const dx = e.clientX - g.startX;
        const dy = e.clientY - g.startY;
        setView(prev => ({ ...prev, tx: g.startTx + dx, ty: g.startTy + dy }));
      } else if (g.kind === 'move') {
        const scale = viewRef.current.scale;
        const dx = (e.clientX - g.startX) / scale;
        const dy = (e.clientY - g.startY) / scale;
        setContent(prev => {
          const next = {
            ...prev,
            nodes: prev.nodes.map(n =>
              n.id === g.id ? { ...n, x: g.origX + dx, y: g.origY + dy } : n,
            ),
          };
          contentRef.current = next;
          return next;
        });
      } else if (g.kind === 'resize') {
        const scale = viewRef.current.scale;
        const dw = (e.clientX - g.startX) / scale;
        const dh = (e.clientY - g.startY) / scale;
        setContent(prev => {
          const next = {
            ...prev,
            nodes: prev.nodes.map(n =>
              n.id === g.id
                ? { ...n, w: Math.max(MIN_NODE_SIZE, g.origW + dw), h: Math.max(MIN_NODE_SIZE, g.origH + dh) }
                : n,
            ),
          };
          contentRef.current = next;
          return next;
        });
      }
    },
    [screenToWorld, redraw],
  );

  const endGesture = useCallback(() => {
    if (drawingRef.current) {
      drawingRef.current = false;
      const stroke = liveStrokeRef.current;
      liveStrokeRef.current = null;
      if (stroke && stroke.points.length >= 2) {
        applyContent({ ...contentRef.current, strokes: [...contentRef.current.strokes, stroke] });
      } else {
        redraw();
      }
    }
    if (gestureRef.current) {
      const wasNode = gestureRef.current.kind !== 'pan';
      gestureRef.current = null;
      if (wasNode) {
        // Commit the moved/resized node position to the dirty tracker.
        applyContent(contentRef.current);
      }
    }
  }, [applyContent, redraw]);

  // -----------------------------------------------------------------------
  // Zoom (wheel, centered on cursor) + zoom buttons
  // -----------------------------------------------------------------------

  const onWheel = useCallback((e: React.WheelEvent) => {
    if (loadState.status !== 'ready') return;
    const surface = surfaceRef.current;
    if (!surface) return;
    const rect = surface.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    setView(prev => {
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
      const nextScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, prev.scale * factor));
      // Keep the world point under the cursor fixed while zooming.
      const wx = (cx - prev.tx) / prev.scale;
      const wy = (cy - prev.ty) / prev.scale;
      return { scale: nextScale, tx: cx - wx * nextScale, ty: cy - wy * nextScale };
    });
  }, [loadState.status]);

  const zoomBy = useCallback((factor: number) => {
    const surface = surfaceRef.current;
    const rect = surface?.getBoundingClientRect();
    const cx = rect ? rect.width / 2 : 0;
    const cy = rect ? rect.height / 2 : 0;
    setView(prev => {
      const nextScale = Math.min(MAX_SCALE, Math.max(MIN_SCALE, prev.scale * factor));
      const wx = (cx - prev.tx) / prev.scale;
      const wy = (cy - prev.ty) / prev.scale;
      return { scale: nextScale, tx: cx - wx * nextScale, ty: cy - wy * nextScale };
    });
  }, []);

  const resetView = useCallback(() => setView({ tx: 0, ty: 0, scale: 1 }), []);

  // -----------------------------------------------------------------------
  // Node creation / editing / deletion
  // -----------------------------------------------------------------------

  const addNode = useCallback(
    (node: CanvasNode) => {
      applyContent({ ...contentRef.current, nodes: [...contentRef.current.nodes, node] });
      setSelectedId(node.id);
    },
    [applyContent],
  );

  const addTextBlock = useCallback(() => {
    const c = viewportCenterWorld();
    addNode({
      id: newNodeId(),
      type: 'text',
      x: c.x - 90,
      y: c.y - 30,
      w: 180,
      h: 60,
      text: '',
      color: DEFAULT_COLOR,
    });
  }, [addNode, viewportCenterWorld]);

  const handleFiles = useCallback(
    async (files: FileList | File[], kind: 'image' | 'document') => {
      setActionError(null);
      const list = Array.from(files);
      const center = viewportCenterWorld();
      let offset = 0;
      for (const file of list) {
        try {
          const dataUrl = await readFileAsDataUrl(file);
          if (kind === 'image') {
            addNode({
              id: newNodeId(),
              type: 'image',
              x: center.x - 120 + offset,
              y: center.y - 90 + offset,
              w: 240,
              h: 180,
              dataUrl,
              name: file.name,
            });
          } else {
            addNode({
              id: newNodeId(),
              type: 'document',
              x: center.x - 130 + offset,
              y: center.y - 90 + offset,
              w: 260,
              h: 180,
              dataUrl,
              name: file.name,
              mime: file.type || 'application/octet-stream',
            });
          }
          offset += 24;
        } catch (err) {
          setActionError(err instanceof Error ? err.message : 'Failed to add file');
        }
      }
    },
    [addNode, viewportCenterWorld],
  );

  // Paste images directly onto the canvas.
  const onPaste = useCallback(
    (e: React.ClipboardEvent) => {
      if (loadState.status !== 'ready') return;
      const items = e.clipboardData?.items;
      if (!items) return;
      const images: File[] = [];
      for (const item of items) {
        if (item.kind === 'file' && item.type.startsWith('image/')) {
          const f = item.getAsFile();
          if (f) images.push(f);
        }
      }
      if (images.length > 0) {
        e.preventDefault();
        void handleFiles(images, 'image');
      }
    },
    [handleFiles, loadState.status],
  );

  const updateNode = useCallback(
    (id: string, patch: Partial<CanvasNode>) => {
      applyContent({
        ...contentRef.current,
        nodes: contentRef.current.nodes.map(n => (n.id === id ? ({ ...n, ...patch } as CanvasNode) : n)),
      });
    },
    [applyContent],
  );

  const deleteNode = useCallback(
    (id: string) => {
      applyContent({ ...contentRef.current, nodes: contentRef.current.nodes.filter(n => n.id !== id) });
      setSelectedId(prev => (prev === id ? null : prev));
    },
    [applyContent],
  );

  const clearAll = useCallback(() => {
    applyContent({ version: 2, strokes: [], nodes: [] });
    setSelectedId(null);
    liveStrokeRef.current = null;
    drawingRef.current = false;
  }, [applyContent]);

  // -----------------------------------------------------------------------
  // Node pointer handlers (move / resize) — only active in select mode
  // -----------------------------------------------------------------------

  const startNodeMove = useCallback(
    (e: React.PointerEvent, node: CanvasNode) => {
      if (mode !== 'select') return;
      e.stopPropagation();
      setSelectedId(node.id);
      gestureRef.current = {
        kind: 'move',
        id: node.id,
        startX: e.clientX,
        startY: e.clientY,
        origX: node.x,
        origY: node.y,
      };
      (e.target as Element).setPointerCapture?.(e.pointerId);
    },
    [mode],
  );

  const startNodeResize = useCallback((e: React.PointerEvent, node: CanvasNode) => {
    e.stopPropagation();
    gestureRef.current = {
      kind: 'resize',
      id: node.id,
      startX: e.clientX,
      startY: e.clientY,
      origW: node.w,
      origH: node.h,
    };
    (e.target as Element).setPointerCapture?.(e.pointerId);
  }, []);

  // -----------------------------------------------------------------------
  // Derived save-status label
  // -----------------------------------------------------------------------

  const statusLabel = useMemo(() => {
    if (saveStatus === 'saving') return 'Saving…';
    if (saveStatus === 'error') return 'Save failed';
    if (dirty) return 'Unsaved changes';
    return 'Saved';
  }, [saveStatus, dirty]);

  const worldTransform = `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`;

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (loadState.status === 'loading') {
    return (
      <div className="flex items-center gap-2 text-sm text-neutral-500 py-4">
        <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-neutral-600 border-t-neutral-300" />
        Loading whiteboard…
      </div>
    );
  }

  if (loadState.status === 'error') {
    return (
      <div className="rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
        {loadState.message}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {/* Toolbar */}
      <div
        className="flex flex-wrap items-center gap-1.5"
        data-viewport-allow-overflow
        data-viewport-allow-small-target
      >
        {/* Mode toggle */}
        <div className="flex items-center gap-0.5 rounded border border-neutral-700 p-0.5" role="radiogroup" aria-label="Tool mode">
          <button
            type="button"
            role="radio"
            aria-checked={mode === 'select'}
            onClick={() => setMode('select')}
            className={`min-h-[44px] rounded px-2 py-1 text-xs transition-colors ${mode === 'select' ? 'bg-neutral-700 text-neutral-100' : 'text-neutral-400 hover:text-neutral-200'}`}
          >
            Select
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={mode === 'draw'}
            onClick={() => setMode('draw')}
            className={`min-h-[44px] rounded px-2 py-1 text-xs transition-colors ${mode === 'draw' ? 'bg-neutral-700 text-neutral-100' : 'text-neutral-400 hover:text-neutral-200'}`}
          >
            Draw
          </button>
        </div>

        <span className="mx-0.5 h-5 w-px bg-neutral-700" aria-hidden="true" />

        {/* Color palette (draw) */}
        <div className="flex items-center gap-0.5" role="radiogroup" aria-label="Stroke color">
          {PRESET_COLORS.map(c => (
            <button
              key={c}
              type="button"
              aria-label={`Color ${c}`}
              aria-checked={c === color}
              role="radio"
              onClick={() => setColor(c)}
              className={`h-7 w-7 rounded-full border-2 transition-colors ${
                c === color ? 'border-white scale-110' : 'border-transparent hover:scale-105'
              }`}
              style={{ backgroundColor: c }}
            />
          ))}
        </div>

        {/* Stroke width (draw) */}
        <div className="flex items-center gap-0.5" role="radiogroup" aria-label="Stroke width">
          {PRESET_WIDTHS.map(w => (
            <button
              key={w}
              type="button"
              aria-label={`${w}px width`}
              aria-checked={w === lineWidth}
              role="radio"
              onClick={() => setLineWidth(w)}
              className={`flex h-7 w-7 items-center justify-center rounded border transition-colors ${
                w === lineWidth ? 'border-white bg-neutral-700' : 'border-neutral-600 hover:border-neutral-400'
              }`}
            >
              <span
                className="rounded-full"
                style={{ width: Math.min(w + 2, 14), height: Math.min(w + 2, 14), backgroundColor: color }}
              />
            </button>
          ))}
        </div>

        <span className="mx-0.5 h-5 w-px bg-neutral-700" aria-hidden="true" />

        {/* Insert actions */}
        <button
          type="button"
          onClick={addTextBlock}
          className="rounded border border-neutral-600 px-2 py-1 text-xs text-neutral-300 hover:border-neutral-400 hover:text-neutral-100 transition-colors min-h-[44px]"
        >
          + Text
        </button>
        <button
          type="button"
          onClick={() => imageInputRef.current?.click()}
          className="rounded border border-neutral-600 px-2 py-1 text-xs text-neutral-300 hover:border-neutral-400 hover:text-neutral-100 transition-colors min-h-[44px]"
        >
          + Image
        </button>
        <button
          type="button"
          onClick={() => docInputRef.current?.click()}
          className="rounded border border-neutral-600 px-2 py-1 text-xs text-neutral-300 hover:border-neutral-400 hover:text-neutral-100 transition-colors min-h-[44px]"
        >
          + Document
        </button>

        <span className="mx-0.5 h-5 w-px bg-neutral-700" aria-hidden="true" />

        {/* Zoom controls */}
        <div className="flex items-center gap-0.5" aria-label="Zoom controls">
          <button
            type="button"
            aria-label="Zoom out"
            onClick={() => zoomBy(1 / 1.2)}
            className="flex h-7 w-7 items-center justify-center rounded border border-neutral-600 text-xs text-neutral-300 hover:border-neutral-400"
          >
            −
          </button>
          <button
            type="button"
            onClick={resetView}
            className="min-h-[44px] rounded border border-neutral-600 px-1.5 py-1 text-xs tabular-nums text-neutral-400 hover:text-neutral-200"
            aria-label="Reset zoom"
          >
            {Math.round(view.scale * 100)}%
          </button>
          <button
            type="button"
            aria-label="Zoom in"
            onClick={() => zoomBy(1.2)}
            className="flex h-7 w-7 items-center justify-center rounded border border-neutral-600 text-xs text-neutral-300 hover:border-neutral-400"
          >
            +
          </button>
        </div>

        <span className="mx-0.5 h-5 w-px bg-neutral-700" aria-hidden="true" />

        {/* Clear */}
        <button
          type="button"
          onClick={clearAll}
          className="rounded border border-neutral-600 px-2 py-1 text-xs text-neutral-400 hover:border-red-500/50 hover:text-red-400 transition-colors min-h-[44px]"
        >
          Clear
        </button>

        {/* Save status */}
        <span
          className={`ml-auto text-xs ${saveStatus === 'error' ? 'text-red-400' : dirty ? 'text-amber-400' : 'text-neutral-500'}`}
          data-testid="whiteboard-save-status"
        >
          {statusLabel}
        </span>
      </div>

      {/* Hidden file inputs */}
      <input
        ref={imageInputRef}
        type="file"
        accept="image/*"
        multiple
        className="hidden"
        onChange={e => {
          if (e.target.files) void handleFiles(e.target.files, 'image');
          e.target.value = '';
        }}
      />
      <input
        ref={docInputRef}
        type="file"
        accept="application/pdf,.pdf"
        multiple
        className="hidden"
        onChange={e => {
          if (e.target.files) void handleFiles(e.target.files, 'document');
          e.target.value = '';
        }}
      />

      {actionError && <p className="text-xs text-amber-400">{actionError}</p>}

      {/* Canvas surface */}
      <div
        ref={surfaceRef}
        data-testid="whiteboard-surface"
        className={`relative w-full overflow-hidden rounded border border-neutral-700 bg-[#1a1a2e] ${mode === 'draw' ? 'cursor-crosshair' : 'cursor-grab'} touch-none`}
        style={{ aspectRatio: '16 / 9', minHeight: 240 }}
        onPointerDown={onSurfacePointerDown}
        onPointerMove={onSurfacePointerMove}
        onPointerUp={endGesture}
        onPointerLeave={endGesture}
        onPointerCancel={endGesture}
        onWheel={onWheel}
        onPaste={onPaste}
        tabIndex={0}
      >
        {/* Stroke layer (screen space) */}
        <canvas ref={canvasRef} className="pointer-events-none absolute inset-0 h-full w-full" />

        {/* Node layer (world space) */}
        <div
          className="pointer-events-none absolute left-0 top-0"
          style={{ transform: worldTransform, transformOrigin: '0 0' }}
        >
          {content.nodes.map(node => (
            <CanvasNodeView
              key={node.id}
              node={node}
              selected={selectedId === node.id}
              mode={mode}
              onPointerDownMove={startNodeMove}
              onPointerDownResize={startNodeResize}
              onChangeText={text => updateNode(node.id, { text } as Partial<CanvasNode>)}
              onDelete={() => deleteNode(node.id)}
              onSelect={() => setSelectedId(node.id)}
            />
          ))}
        </div>

        {isEmpty(content) && (
          <div className="pointer-events-none absolute inset-0 flex items-center justify-center px-4 text-center text-xs text-neutral-500">
            Draw, paste an image, or add a text block / document to start your canvas.
          </div>
        )}
      </div>

      {saveError && (
        <p className="text-xs text-amber-400">
          {saveError}
          <button
            type="button"
            onClick={() => {
              setSaveError(null);
              void flush();
            }}
            className="ml-1 underline hover:text-amber-300"
          >
            Retry
          </button>
        </p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Node view
// ---------------------------------------------------------------------------

interface CanvasNodeViewProps {
  node: CanvasNode;
  selected: boolean;
  mode: 'select' | 'draw';
  onPointerDownMove: (e: React.PointerEvent, node: CanvasNode) => void;
  onPointerDownResize: (e: React.PointerEvent, node: CanvasNode) => void;
  onChangeText: (text: string) => void;
  onDelete: () => void;
  onSelect: () => void;
}

function CanvasNodeView({
  node,
  selected,
  mode,
  onPointerDownMove,
  onPointerDownResize,
  onChangeText,
  onDelete,
  onSelect,
}: CanvasNodeViewProps) {
  const interactive = mode === 'select';
  return (
    <div
      className={`pointer-events-auto absolute flex flex-col overflow-hidden rounded border ${
        selected ? 'border-indigo-400 ring-1 ring-indigo-400/50' : 'border-neutral-600'
      } bg-neutral-900/90 shadow-lg`}
      style={{ left: node.x, top: node.y, width: node.w, height: node.h }}
      onPointerDown={e => {
        // Interacting with a node must never start a surface pan or clear the
        // selection, so swallow the event before it reaches the surface.
        e.stopPropagation();
        // Dragging the header (not an inner control) moves the node.
        if ((e.target as HTMLElement).dataset.nodeHandle === 'body') {
          onPointerDownMove(e, node);
        } else {
          onSelect();
        }
      }}
    >
      {/* Drag handle / header */}
      <div
        data-node-handle="body"
        className={`flex items-center justify-between gap-1 border-b border-neutral-700 bg-neutral-800/80 px-1.5 py-0.5 text-ui-sm uppercase tracking-wide text-neutral-400 ${
          interactive ? 'cursor-move' : ''
        }`}
      >
        <span data-node-handle="body" className="truncate">
          {node.type === 'text' ? 'Text' : node.type === 'image' ? node.name ?? 'Image' : node.name}
        </span>
        <button
          type="button"
          aria-label="Delete item"
          onClick={onDelete}
          className="shrink-0 text-neutral-500 hover:text-red-400"
        >
          ✕
        </button>
      </div>

      {/* Body */}
      <div className="min-h-0 flex-1">
        {node.type === 'text' && (
          <textarea
            value={node.text}
            onChange={e => onChangeText(e.target.value)}
            placeholder="Type here…"
            className="h-full w-full resize-none bg-transparent p-1.5 text-xs text-neutral-100 outline-none"
            style={{ color: node.color }}
            onPointerDown={e => e.stopPropagation()}
          />
        )}
        {node.type === 'image' && (
          <img
            src={node.dataUrl}
            alt={node.name ?? 'Pasted image'}
            className="h-full w-full object-contain"
            draggable={false}
          />
        )}
        {node.type === 'document' && (
          <DocumentPreview node={node} />
        )}
      </div>

      {/* Resize handle */}
      {interactive && (
        <div
          role="button"
          aria-label="Resize item"
          onPointerDown={e => onPointerDownResize(e, node)}
          className="absolute bottom-0 right-0 h-3 w-3 cursor-nwse-resize bg-indigo-400/70"
          style={{ borderTopLeftRadius: 3 }}
        />
      )}
    </div>
  );
}

function DocumentPreview({ node }: { node: DocumentNode }) {
  const isPdf = node.mime === 'application/pdf' || node.name.toLowerCase().endsWith('.pdf');
  if (isPdf) {
    return (
      <object data={node.dataUrl} type="application/pdf" className="h-full w-full" aria-label={node.name}>
        <a href={node.dataUrl} download={node.name} className="block min-h-[44px] p-2 text-xs text-indigo-300 underline">
          {node.name}
        </a>
      </object>
    );
  }
  return (
    <a
      href={node.dataUrl}
      download={node.name}
      className="flex h-full w-full items-center justify-center p-2 text-xs text-indigo-300 underline"
      onPointerDown={e => e.stopPropagation()}
    >
      {node.name}
    </a>
  );
}
