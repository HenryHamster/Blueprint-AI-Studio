import type WebSocket from 'ws';
import { sanitize, sendLog, sendWsFrame } from '../index.js';
import {
  FrameCoalescer,
  type StreamFrameKind,
  type StreamFrameStatus,
} from './strategic-resident-lib.js';
import { WsFrameBudget } from './ws-frame-budget.js';

// Coalesce streamed assistant deltas to at most ~10 WebSocket frames/sec so the
// live transcript stays well under the backend's 20 msg/s close threshold
// (routers/projects.py) — the same discipline as replayLogBufferRateLimited.
const STREAM_FLUSH_MS = 100;
// A one-shot Chat handler is followed immediately by the role runner's
// `ws.close()`. Give its ordered queue a short, rate-limited opportunity to
// finish, then reserve a final lifecycle frame before that close can race it.
// At most ten queued frames can leave during this window, so the forced final
// frame remains comfortably below the backend's 20-frame/s socket threshold.
const TERMINAL_DRAIN_MAX_MS = 900;

/** The `kind` vocabulary the backend `strategic_session` delta accepts
 *  (strategic_events.py `_STRATEGIC_SESSION_KINDS`) — reused from the lib so the
 *  emit + parse sides can never drift. */
export type SessionDeltaKind = StreamFrameKind;

/**
 * Streams a resident turn's assistant text to the operator as a typed
 * `strategic_session` WebSocket frame:
 *
 *   { type: "strategic_session",
 *     payload: { session_id, task_id, seq, kind, text, ts } }
 *
 * The backend agent WS receiver (routers/projects.py) maps this frame to
 * `emit_strategic_session_delta`, which secret-sanitizes `text`, bounds the
 * payload under the NOTIFY ceiling, and broadcasts a `strategic_session` event
 * the UI's transcript branch (useLogViewer.ts) renders. `seq` is monotonic per
 * session so the UI can order/dedupe deltas. `kind` is one of reasoning /
 * action / tool / status / message; assistant text streams as `message`, tool
 * calls as `tool`, and lifecycle/error lines as `status`.
 *
 * ALL frame kinds — assistant text deltas, tool calls, and status lines — share
 * ONE rate budget: they are enqueued in order (text coalescing into a single
 * frame; a tool/status frame sealing the open text before it) and drained at
 * most once every `STREAM_FLUSH_MS` (≤10/s), so even a burst of tool_use blocks
 * in a single assistant message stays well under the backend's 20 msg/s close
 * threshold. When no live viewer socket is attached, each frame falls back to
 * the buffered `sendLog` transcript so nothing is lost.
 */
export class SessionStreamer {
  private readonly queue = new FrameCoalescer();
  private timer: NodeJS.Timeout | null = null;
  private seq = 0;
  private readonly budget: WsFrameBudget;
  constructor(
    private readonly ws: WebSocket | null,
    private readonly sessionId: string,
    private readonly taskId: string,
    /** Pass the SOCKET's shared budget whenever another streamer writes to the
     *  same websocket (typed Fast runs this transcript and the conversation
     *  reply preview concurrently). Omitted, this streamer owns the whole
     *  <=10 frames/s on its own, which is correct when it is the only writer. */
    budget?: WsFrameBudget,
  ) {
    this.budget = budget ?? new WsFrameBudget(STREAM_FLUSH_MS);
  }

  private emit(kind: SessionDeltaKind, text: string, status?: StreamFrameStatus): void {
    const clean = sanitize(text);
    // A lifecycle frame (responding/done) is allowed to carry empty text; every
    // other frame with no text after sanitizing is dropped.
    if (!clean && !status) return;
    const payload: Record<string, unknown> = {
      session_id: this.sessionId,
      task_id: this.taskId,
      seq: this.seq++,
      kind,
      text: clean,
      ts: new Date().toISOString(),
    };
    if (status) payload.status = status;
    const frame = { type: 'strategic_session', payload };
    // Prefer the typed frame on a live socket; otherwise keep the delta in the
    // buffered agent-log transcript so a later-attaching viewer still sees it.
    if (!sendWsFrame(this.ws, frame)) {
      if (clean) sendLog(this.ws, clean.endsWith('\n') ? clean : `${clean}\n`);
    }
  }

  // Schedule the drain so it emits at most one queued frame per STREAM_FLUSH_MS
  // window (measured from the last real emit on the SHARED socket budget, so a
  // concurrent conversation-preview streamer counts against the same ≤10/s).
  private scheduleDrain(): void {
    if (this.timer || this.queue.isEmpty) return;
    const wait = this.budget.delayMs();
    this.timer = setTimeout(() => {
      this.timer = null;
      const item = this.queue.next();
      if (item) { this.emit(item.kind, item.text, item.status); this.budget.consume(); }
      this.scheduleDrain();
    }, wait);
  }

  delta(text: string): void {
    this.queue.pushText(text);
    this.scheduleDrain();
  }

  // Turn/session boundary flush. Seals the ending turn's open text item (so its
  // tail can't merge into the next turn's first delta) and hands the residual to
  // the SAME rate-limited drain the hot path uses. It deliberately does NOT
  // synchronously empty the queue: a turn that produced frames faster than the
  // ≤10/s drain (e.g. a burst of tool_use blocks in one assistant message) can
  // leave a large residual, and emitting it all in one tick here would blow past
  // the backend's 20 msg/s WS-close threshold (routers/projects.py) — the very
  // cap scheduleDrain exists to respect. Ordering and delivery are preserved:
  // scheduleDrain emits every queued frame, in order, one per STREAM_FLUSH_MS.
  flush(): void {
    this.queue.seal();
    this.scheduleDrain();
  }

  /**
   * Complete a one-shot stream before its owning role closes the WebSocket.
   *
   * The normal path preserves every queued frame and the shared <=10/s budget.
   * A pathological tool burst cannot hold role teardown indefinitely: after the
   * bounded drain window, discard its stale residual and send the terminal
   * `done` frame directly. That final frame is what releases the UI's live
   * preview; it is still rate-safe because the bounded window emits at most ten
   * frames before the one terminal send.
   *
   * Persistent Resident callers deliberately keep using `flush()` plus
   * `done()` — they own a long-lived socket and must retain their entire queue.
   */
  async finish(): Promise<void> {
    this.done();
    this.flush();

    // With no socket there is no close race: scheduleDrain's sendLog fallback
    // preserves the buffered transcript, and waiting would needlessly delay
    // every non-interactive Strategic task.
    if (!this.ws) return;

    const fullyDrained = await new Promise<boolean>((resolve) => {
      const startedAt = Date.now();
      const check = (): void => {
        if (this.queue.isEmpty) {
          resolve(true);
          return;
        }
        const remaining = TERMINAL_DRAIN_MAX_MS - (Date.now() - startedAt);
        if (remaining <= 0) {
          resolve(false);
          return;
        }
        setTimeout(check, Math.min(STREAM_FLUSH_MS, remaining));
      };
      check();
    });
    if (fullyDrained) return;

    // Do not leave a scheduled residual able to send after `done`, especially
    // once the role runner has closed the socket. seq is assigned at emit time,
    // so dropping these unsent items cannot create a sequence gap.
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.queue.drain();
    this.emit('status', '[strategic-session] done', 'done');
  }

  status(text: string): void {
    this.queue.pushFrame('status', `[strategic-session] ${text}`);
    this.scheduleDrain();
  }

  tool(name: string | undefined, command: string | undefined): void {
    const cmd = command ? ` ${command.slice(0, 160)}` : '';
    this.queue.pushFrame('tool', `[tool: ${name || 'tool'}]${cmd}`);
    this.scheduleDrain();
  }

  /**
   * A LIVE, EPHEMERAL activity one-liner for the operator's Workday activity
   * strip — a Bash command, file edit, read, spawned sub-agent, or tool result.
   * Carried on the same rate-limited queue as text so a burst of tool_use blocks
   * in one assistant message stays under the backend WS-close cap. NEVER
   * persisted (only the assistant's `./messages send` reply lands in the durable
   * transcript, as a ProjectMessage).
   */
  activity(kind: StreamFrameKind, text: string): void {
    this.queue.pushFrame(kind, text);
    this.scheduleDrain();
  }

  /** Mark the START of an operator turn — drives the UI "Workday's on it" state. */
  responding(): void {
    this.queue.pushFrame('status', '[strategic-session] responding', 'responding');
    this.scheduleDrain();
  }

  /** Mark the START of an autonomous turn — drives the UI "Workday: advancing" state. */
  working(): void {
    this.queue.pushFrame('status', '[strategic-session] working', 'working');
    this.scheduleDrain();
  }

  /** Mark the END of any turn — clears the UI activity strip. Ordered after all of
   *  the turn's text/activity frames (the queue drains in order). */
  done(): void {
    this.queue.pushFrame('status', '[strategic-session] done', 'done');
    this.scheduleDrain();
  }
}
