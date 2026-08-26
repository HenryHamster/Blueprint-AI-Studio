import type WebSocket from 'ws';
import { sanitize, sendLog, sendWsFrame } from '../index.js';
import { WsFrameBudget } from './ws-frame-budget.js';

/** Hard wire cap the backend relay enforces on one preview chunk — keep in sync
 *  with `_STREAMING_PREVIEW_MAX_CHUNK_CHARS` in
 *  `backend/app/routers/projects.py`. A chunk over this is DROPPED server-side,
 *  so nothing may be put on the wire above it.
 *  `backend/testing/test_agent.py::test_streaming_preview_chunk_cap_matches_agent_constant`
 *  pins the two constants together. */
export const STREAM_DELTA_MAX_CHARS = 1_200;
/** Raw slice taken from the model text before redaction. Deliberately BELOW the
 *  wire cap: `sanitize()` normally shrinks text, but a few patterns can grow it
 *  slightly (`oauth_token:a` -> `oauth_token=***`), and a redacted chunk that
 *  overflowed the wire cap would be silently discarded by the relay. */
const STREAM_DELTA_RAW_SLICE_CHARS = 1_000;
/** How far back from a hard slice boundary to look for whitespace to break on,
 *  so a credential is never cut in half across two chunks (see
 *  {@link ConversationMessageStreamer.breakLength}). */
const STREAM_DELTA_BREAK_LOOKBACK_CHARS = 240;
/** Bounded window for `finish()` to drain its residual before the terminal
 *  frame — mirrors `SessionStreamer`'s `TERMINAL_DRAIN_MAX_MS`. The residual is
 *  discarded after it: the durable receipt-bound completion, not this preview,
 *  is the source of truth for the full reply. */
const TERMINAL_DRAIN_MAX_MS = 900;
export type StreamTerminal = 'failed' | 'abandoned';

/**
 * Sends bounded typed Fast reply chunks through the project-message event
 * contract. The UI appends ordered chunks locally; the final durable message
 * still replaces the preview by its receipt identity.
 *
 * Two invariants this class owns:
 *
 * - **Redaction.** Chunks are raw model output on an operator-facing channel.
 *   `sendWsFrame` does NOT sanitize (unlike `sendLog`), so every chunk goes
 *   through `sanitize()` here, exactly as `SessionStreamer.emit` does. The
 *   backend relay applies `_sanitize_log` again as defence in depth.
 * - **Rate.** Frames are spaced by the SHARED {@link WsFrameBudget} for this
 *   socket, so the reply preview and the `/strategic-session` transcript
 *   together stay under the backend's 20 msg/s close threshold instead of each
 *   assuming it owns half of it.
 */
export class ConversationMessageStreamer {
  private timer: NodeJS.Timeout | null = null;
  private content = '';
  private emittedLength = 0;
  private pending = false;
  private seq = 0;
  private attempt: number;
  private finished = false;
  private terminalSent = false;
  private readonly budget: WsFrameBudget;

  constructor(
    private readonly ws: WebSocket | null,
    private readonly sessionId: string,
    private readonly taskId: string,
    private readonly claimId: string,
    private readonly idempotencyKey: string,
    budget?: WsFrameBudget,
    attempt = 0,
  ) {
    this.budget = budget ?? new WsFrameBudget();
    this.attempt = attempt;
  }

  delta(text: string): void {
    if (!text || this.finished) return;
    this.content += text;
    this.pending = true;
    this.schedule();
  }

  /**
   * Retire the current attempt's preview and begin a fresh one.
   *
   * A provider crash followed by `replaceAfterCrash()` re-runs the SAME receipt
   * from the top, so attempt 2's text is a new answer, not a continuation of
   * attempt 1's partial. Keeping one preview identity across both attempts made
   * the client append attempt 2's tail onto attempt 1's partial (its
   * `seq <= current` guard swallows the restarted prefix), producing a
   * coherent-looking reply the model never wrote. Bumping the attempt gives the
   * retry its own preview id and tombstones the abandoned one.
   */
  newAttempt(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.emitTerminal('failed');
    this.attempt += 1;
    this.content = '';
    this.emittedLength = 0;
    this.pending = false;
    this.seq = 0;
    this.terminalSent = false;
    this.finished = false;
  }

  /**
   * Close the turn: drain what is left within a bounded, rate-limited window,
   * then send the terminal tombstone.
   *
   * The drain is deliberately NOT a synchronous `while` over the buffer. A long
   * reply can leave tens of 1,200-char chunks queued, and emitting them in one
   * tick trips the backend's 20 msg/s socket close — killing log and status
   * relay for the whole task, not just this stream.
   *
   * Idempotent for the terminal: calling `finish()` on the success path and
   * later `finish('failed')` (e.g. the completion POST threw, so no durable
   * reply exists) still emits exactly one tombstone.
   */
  async finish(terminal?: StreamTerminal): Promise<void> {
    if (this.finished && terminal === undefined) return;
    this.finished = true;
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    const deadline = Date.now() + TERMINAL_DRAIN_MAX_MS;
    while (this.pending) {
      const remaining = deadline - Date.now();
      if (remaining <= 0) break;
      const wait = Math.min(this.budget.delayMs(), remaining);
      if (wait > 0) await new Promise(resolve => setTimeout(resolve, wait));
      if (Date.now() >= deadline) break;
      this.emitPending();
    }
    // Whatever is still buffered is dropped on purpose: the durable completion
    // carries the full text, and holding the socket past the bounded window to
    // replay a preview is exactly what the frame budget exists to prevent.
    this.pending = false;
    if (terminal !== undefined) this.emitTerminal(terminal);
  }

  private schedule(): void {
    if (this.timer || !this.pending) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.emitPending();
      this.schedule();
    }, Math.max(this.budget.delayMs(), 1));
  }

  /**
   * Snap a hard slice length back to the last whitespace before it.
   *
   * `sanitize()` can only mask a credential it can see WHOLE. A boundary that
   * lands inside one splits it across two chunks: the leading half still
   * matches a token pattern and is masked, but the trailing half is a bare
   * alphanumeric run no pattern recognises, so the secret's tail goes out in
   * clear. Breaking on whitespace keeps tokens inside a single chunk.
   */
  private breakLength(rawLength: number, remaining: number): number {
    // The final chunk reaches the end of the buffer; nothing follows to split.
    if (rawLength >= remaining) return rawLength;
    const start = Math.max(1, rawLength - STREAM_DELTA_BREAK_LOOKBACK_CHARS);
    const window = this.content.slice(
      this.emittedLength + start,
      this.emittedLength + rawLength,
    );
    for (let i = window.length - 1; i >= 0; i -= 1) {
      // Keep the whitespace itself on this chunk so the join stays lossless.
      if (/\s/.test(window[i])) return start + i + 1;
    }
    // A single unbroken run longer than the lookback: nothing to snap to.
    return rawLength;
  }

  private emitPending(): void {
    if (!this.pending) return;
    const remaining = this.content.length - this.emittedLength;
    if (remaining <= 0) {
      this.pending = false;
      return;
    }
    let rawLength = this.breakLength(
      Math.min(STREAM_DELTA_RAW_SLICE_CHARS, remaining),
      remaining,
    );
    // Redact BEFORE the wire, then shrink the raw window on the rare occasion
    // redaction grew the text past the relay's cap, so a chunk carrying a
    // secret is never dropped-and-lost instead of sent-redacted.
    let chunk = sanitize(this.content.slice(this.emittedLength, this.emittedLength + rawLength));
    while (chunk.length > STREAM_DELTA_MAX_CHARS && rawLength > 1) {
      rawLength = Math.max(1, this.breakLength(Math.floor(rawLength / 2), remaining));
      chunk = sanitize(this.content.slice(this.emittedLength, this.emittedLength + rawLength));
    }
    this.emittedLength += rawLength;
    this.pending = this.emittedLength < this.content.length;
    // A slice that was entirely a redacted secret can sanitize to empty. There
    // is nothing left to show, but the cursor still advanced.
    if (!chunk) return;
    this.budget.consume();
    const sent = sendWsFrame(this.ws, {
      type: 'conversation_message_delta',
      payload: {
        session_id: this.sessionId,
        task_id: this.taskId,
        claim_id: this.claimId,
        idempotency_key: this.idempotencyKey,
        attempt: this.attempt,
        seq: this.seq++,
        content: chunk,
      },
    });
    // `sendLog` sanitizes again; `chunk` is already clean, so that pass is a
    // no-op and the buffered transcript can never carry the raw secret either.
    if (!sent) sendLog(this.ws, chunk.endsWith('\n') ? chunk : `${chunk}\n`);
  }

  private emitTerminal(terminal: StreamTerminal): void {
    if (this.terminalSent) return;
    this.terminalSent = true;
    this.budget.consume();
    sendWsFrame(this.ws, {
      type: 'conversation_message_delta',
      payload: {
        session_id: this.sessionId,
        task_id: this.taskId,
        claim_id: this.claimId,
        idempotency_key: this.idempotencyKey,
        attempt: this.attempt,
        seq: this.seq++,
        content: '',
        terminal,
      },
    });
  }
}
