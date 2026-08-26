/**
 * ONE shared frame budget for every streamer writing to the SAME agent
 * WebSocket.
 *
 * The backend agent WS receiver closes the socket with code 1008
 * ("Rate limit exceeded", `backend/app/routers/projects.py`) once it counts
 * more than 20 inbound messages in a one-second window, and that counter is
 * shared by EVERY frame type the agent sends — `log`, `status`,
 * `strategic_session`, and `conversation_message_delta` alike. Losing the
 * socket does not merely drop a stream: it takes the running task's log and
 * status relay with it.
 *
 * Historically each streamer self-budgeted at <=10 frames/s on the assumption
 * that it owned half of the 20/s ceiling. That assumption breaks as soon as two
 * streamers run concurrently (the `/strategic-session` transcript and the
 * conversation reply preview both stream one typed Fast turn), before `log`
 * frames are counted at all. Handing both streamers the same `WsFrameBudget`
 * makes <=10 frames/s a property of the SOCKET rather than of each streamer, so
 * adding a streamer can never raise the aggregate rate.
 */

/** Minimum spacing between two frames on one socket — <=10 frames/second. */
export const WS_FRAME_MIN_INTERVAL_MS = 100;

export class WsFrameBudget {
  private lastEmitAt = 0;

  constructor(private readonly minIntervalMs: number = WS_FRAME_MIN_INTERVAL_MS) {}

  /** Milliseconds to wait before the next frame may go out; 0 when ready. */
  delayMs(now: number = Date.now()): number {
    const elapsed = now - this.lastEmitAt;
    return elapsed >= this.minIntervalMs ? 0 : this.minIntervalMs - elapsed;
  }

  /** Record that a frame just went out on the shared socket. */
  consume(now: number = Date.now()): void {
    this.lastEmitAt = now;
  }
}
