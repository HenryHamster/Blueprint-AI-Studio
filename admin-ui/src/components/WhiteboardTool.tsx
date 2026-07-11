import { useCallback, useEffect, useRef, useState } from 'react';
import type { ToolComponentProps } from '../toolRegistry';
import { fetchToolContent, saveToolContent, ConflictError, type ToolContent } from '../api/projectTools';

// ---------------------------------------------------------------------------
// Types
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

interface WhiteboardContent {
  strokes: Stroke[];
}

const PRESET_COLORS = ['#e2e8f0', '#f87171', '#60a5fa', '#4ade80', '#facc15'];
const PRESET_WIDTHS = [2, 4, 8, 14];
const DEFAULT_COLOR = PRESET_COLORS[0];
const DEFAULT_WIDTH = PRESET_WIDTHS[1]; // 4px

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

type State =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'empty' }
  | { status: 'ok'; strokes: Stroke[]; revision: number };

export default function WhiteboardTool({ projectId, tool }: ToolComponentProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [state, setState] = useState<State>({ status: 'loading' });
  const [color, setColor] = useState(DEFAULT_COLOR);
  const [lineWidth, setLineWidth] = useState(DEFAULT_WIDTH);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  // Latest strokes/revision refs so event handlers always read fresh values
  // without re-binding on every render.
  const strokesRef = useRef<Stroke[]>([]);
  const revisionRef = useRef(0);
  const drawingRef = useRef(false);
  const currentStrokeRef = useRef<Stroke | null>(null);

  // Keep refs in sync with state.
  useEffect(() => {
    if (state.status === 'ok') {
      strokesRef.current = state.strokes;
      revisionRef.current = state.revision;
    }
  }, [state]);

  // -----------------------------------------------------------------------
  // Load content on mount
  // -----------------------------------------------------------------------

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const data: ToolContent<WhiteboardContent> = await fetchToolContent<WhiteboardContent>(
          projectId,
          tool.id,
        );
        if (cancelled) return;
        const strokes = data.content?.strokes;
        if (!strokes || strokes.length === 0) {
          setState({ status: 'empty' });
        } else {
          setState({ status: 'ok', strokes, revision: data.content_revision });
        }
      } catch (err) {
        if (cancelled) return;
        setState({
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
  // Drawing
  // -----------------------------------------------------------------------

  const redraw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    // Respect device pixel ratio for sharp rendering on high-DPI screens.
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = rect.width;
    const h = rect.height;
    if (canvas.width !== w * dpr || canvas.height !== h * dpr) {
      canvas.width = w * dpr;
      canvas.height = h * dpr;
    }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

    // Background
    ctx.fillStyle = '#1a1a2e';
    ctx.fillRect(0, 0, w, h);

    for (const stroke of strokesRef.current) {
      if (stroke.points.length < 2) continue;
      ctx.beginPath();
      ctx.strokeStyle = stroke.color;
      ctx.lineWidth = stroke.width;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.moveTo(stroke.points[0].x, stroke.points[0].y);
      for (let i = 1; i < stroke.points.length; i++) {
        ctx.lineTo(stroke.points[i].x, stroke.points[i].y);
      }
      ctx.stroke();
    }

    // Also draw the in-progress stroke (not yet committed to strokesRef).
    const liveStroke = currentStrokeRef.current;
    if (liveStroke && liveStroke.points.length >= 2) {
      ctx.beginPath();
      ctx.strokeStyle = liveStroke.color;
      ctx.lineWidth = liveStroke.width;
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.moveTo(liveStroke.points[0].x, liveStroke.points[0].y);
      for (let i = 1; i < liveStroke.points.length; i++) {
        ctx.lineTo(liveStroke.points[i].x, liveStroke.points[i].y);
      }
      ctx.stroke();
    }
  }, []);

  // Redraw whenever strokes change or on resize.
  useEffect(() => {
    if (state.status !== 'ok' && state.status !== 'empty') return;
    redraw();

    const onResize = () => redraw();
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, [state, redraw]);

  const getCanvasPoint = useCallback(
    (clientX: number, clientY: number): Point => {
      const canvas = canvasRef.current;
      if (!canvas) return { x: 0, y: 0 };
      const rect = canvas.getBoundingClientRect();
      return { x: clientX - rect.left, y: clientY - rect.top };
    },
    [],
  );

  const startDrawing = useCallback(
    (e: React.MouseEvent | React.TouchEvent) => {
      if (state.status !== 'ok' && state.status !== 'empty') return;
      e.preventDefault();

      // Promote empty → ok so drawing can begin.
      if (state.status === 'empty') {
        setState({ status: 'ok', strokes: [], revision: 0 });
        strokesRef.current = [];
        revisionRef.current = 0;
      }

      drawingRef.current = true;
      const clientX = 'touches' in e ? e.touches[0].clientX : (e as React.MouseEvent).clientX;
      const clientY = 'touches' in e ? e.touches[0].clientY : (e as React.MouseEvent).clientY;
      const point = getCanvasPoint(clientX, clientY);
      currentStrokeRef.current = { color, width: lineWidth, points: [point] };
    },
    [state.status, color, lineWidth, getCanvasPoint],
  );

  const draw = useCallback(
    (e: React.MouseEvent | React.TouchEvent) => {
      if (!drawingRef.current || !currentStrokeRef.current) return;
      e.preventDefault();
      const clientX = 'touches' in e ? e.touches[0].clientX : (e as React.MouseEvent).clientX;
      const clientY = 'touches' in e ? e.touches[0].clientY : (e as React.MouseEvent).clientY;
      const point = getCanvasPoint(clientX, clientY);
      currentStrokeRef.current = {
        ...currentStrokeRef.current,
        points: [...currentStrokeRef.current.points, point],
      };
      // Render the updated in-progress stroke on canvas.  The stroke is NOT
      // flushed to strokesRef until endDrawing — appending on every mousemove
      // would accumulate N duplicate copies of the same stroke.
      redraw();
    },
    [getCanvasPoint, redraw],
  );

  const endDrawing = useCallback(() => {
    if (!drawingRef.current || !currentStrokeRef.current) return;
    drawingRef.current = false;
    const finalStroke = currentStrokeRef.current;
    currentStrokeRef.current = null;
    if (finalStroke.points.length >= 2) {
      strokesRef.current = [...strokesRef.current, finalStroke];
      setState(prev => {
        if (prev.status !== 'ok') return prev;
        return { ...prev, strokes: strokesRef.current };
      });
    }
    // Redraw to remove the in-progress stroke; the completed stroke renders
    // from strokesRef instead.
    redraw();
  }, [redraw]);

  // -----------------------------------------------------------------------
  // Save
  // -----------------------------------------------------------------------

  const doSave = useCallback(async () => {
    if (state.status !== 'ok') return;
    setSaving(true);
    setSaveError(null);
    try {
      const result = await saveToolContent(projectId, tool.id, { strokes: strokesRef.current }, revisionRef.current);
      revisionRef.current = result.content_revision;
      setState(prev => (prev.status === 'ok' ? { ...prev, revision: result.content_revision } : prev));
    } catch (err) {
      const msg = err instanceof Error ? err.message : 'Save failed';
      setSaveError(msg);
      // Reload on conflict to get the latest revision.
      if (err instanceof ConflictError) {
        try {
          const data: ToolContent<WhiteboardContent> = await fetchToolContent<WhiteboardContent>(projectId, tool.id);
          strokesRef.current = data.content?.strokes ?? [];
          revisionRef.current = data.content_revision;
          setState({ status: 'ok', strokes: strokesRef.current, revision: data.content_revision });
          redraw();
        } catch {
          // If reload fails too, keep the existing state.
        }
      }
    } finally {
      setSaving(false);
    }
  }, [state.status, projectId, tool.id, redraw]);

  // Auto-save after a pause in drawing (debounced).
  useEffect(() => {
    if (state.status !== 'ok') return;
    const timer = setTimeout(() => {
      void doSave();
    }, 1500);
    return () => clearTimeout(timer);
  }, [state, doSave]);

  const handleClear = useCallback(() => {
    strokesRef.current = [];
    currentStrokeRef.current = null;
    drawingRef.current = false;
    setState(prev => (prev.status === 'ok' ? { ...prev, strokes: [] } : { status: 'empty' }));
    redraw();
  }, [redraw]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  if (state.status === 'loading') {
    return (
      <div className="flex items-center gap-2 text-sm text-neutral-500 py-4">
        <span className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-neutral-600 border-t-neutral-300" />
        Loading whiteboard…
      </div>
    );
  }

  if (state.status === 'error') {
    return (
      <div className="rounded border border-red-500/30 bg-red-500/10 p-3 text-sm text-red-400">
        {state.message}
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-1.5" data-viewport-allow-overflow data-viewport-allow-small-target>
        {/* Color palette */}
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

        <span className="mx-0.5 h-5 w-px bg-neutral-700" aria-hidden="true" />

        {/* Stroke width */}
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
                w === lineWidth
                  ? 'border-white bg-neutral-700'
                  : 'border-neutral-600 hover:border-neutral-400'
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

        {/* Clear */}
        <button
          type="button"
          onClick={handleClear}
          className="rounded border border-neutral-600 px-2 py-1 text-xs text-neutral-400 hover:border-red-500/50 hover:text-red-400 transition-colors min-h-[44px]"
        >
          Clear
        </button>

        {/* Save status */}
        <span className="ml-auto text-xs text-neutral-500">
          {saving ? 'Saving…' : saveError ? 'Save failed' : 'Saved'}
        </span>
      </div>

      {/* Canvas */}
      <canvas
        ref={canvasRef}
        className="w-full rounded border border-neutral-700 bg-[#1a1a2e] touch-none"
        style={{ aspectRatio: '16 / 9' }}
        onMouseDown={startDrawing}
        onMouseMove={draw}
        onMouseUp={endDrawing}
        onMouseLeave={endDrawing}
        onTouchStart={startDrawing}
        onTouchMove={draw}
        onTouchEnd={endDrawing}
      />

      {/* Save error detail */}
      {saveError && (
        <p className="text-xs text-amber-400">
          {saveError}
          <button
            type="button"
            onClick={() => { setSaveError(null); void doSave(); }}
            className="ml-1 underline hover:text-amber-300"
          >
            Retry
          </button>
        </p>
      )}
    </div>
  );
}
