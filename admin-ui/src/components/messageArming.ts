/**
 * Operator-message arming outcomes and their operator-facing copy.
 *
 * Single source of truth shared by the MessagesPanel (post-send toast) and the
 * MessagesComposer (steering hint), mirroring the backend strategic_trigger
 * ARMING_* constants. `steering` armed the strategist; `passive_note` and
 * `routine_event` were recorded but do not wake it. `not_armed` is a genuine
 * steering message the server tried to arm but could NOT persist (the project
 * row was momentarily contended), so the strategist was NOT woken — surfaced
 * truthfully instead of a false `steering` promise. `duplicate` indicates the
 * message was already sent (same idempotency key), so the steer was already armed
 * and no new action is needed. Kept in its own module (not the component file)
 * so these helpers can be exported without tripping react-refresh's
 * only-export-components rule.
 */
export type ArmingOutcome = 'steering' | 'passive_note' | 'routine_event' | 'not_armed' | 'duplicate';

// Outcome → composer hint copy.
const ARMING_HINT: Record<ArmingOutcome, string> = {
  steering: 'Steering message — reaches the strategist soon.',
  passive_note: 'Recorded as a note — won’t wake the strategist.',
  routine_event: 'Routine event — won’t wake the strategist.',
  not_armed: 'Couldn’t wake the strategist — try resending in a moment.',
  duplicate: 'Message already sent — steer already armed.',
};

// Pre-send hint shown when a strategic cycle is already running: a steering
// message can't act immediately — it's coalesced into the pending refresh
// marker and queued behind the active cycle, then picked up on the next cycle.
const QUEUED_STEERING_HINT = 'Strategist is working — your steer queues for the next cycle.';
export const WORKDAY_CAPABILITY_HINT = 'Workday can research, plan, and spawn budgeted compute experiments from chat.';

// Outcome → post-send toast copy.
const ARMING_TOAST: Record<ArmingOutcome, string> = {
  steering: 'Message sent — steering the strategist',
  passive_note: 'Note recorded — won’t wake the strategist',
  routine_event: 'Recorded — routine event, won’t wake the strategist',
  not_armed: 'Message saved, but the strategist wasn’t woken — try resending',
  duplicate: 'Message already sent — steer already armed',
};

const WORKBENCH_ARMING_TOAST: Record<Extract<ArmingOutcome, 'steering' | 'not_armed'>, string> = {
  steering: 'Strategic mode saved — waking the strategist',
  not_armed: 'Strategic mode saved, but the strategist wasn’t woken — try again',
};

/** Composer hint copy for a given (real or predicted) arming outcome. */
export function armingHintCopy(outcome: ArmingOutcome): string {
  return ARMING_HINT[outcome];
}

/** Post-send toast copy for a server-reported arming outcome. */
export function armingToastCopy(outcome: ArmingOutcome | null | undefined): string {
  return outcome ? ARMING_TOAST[outcome] : 'Message sent';
}

export type ConversationWakeupState = 'queued' | 'coalesced' | 'paused' | 'waiting_for_capacity';

/** Conversation acceptance is authoritative; legacy arming cannot override it. */
export function conversationAcceptanceToastCopy(
  wakeup: ConversationWakeupState | null | undefined,
  arming: ArmingOutcome | null | undefined,
): { message: string; severity: 'success' | 'info' } {
  switch (wakeup) {
    case 'queued':
      return { message: 'Message accepted — queued', severity: 'success' };
    case 'coalesced':
      return { message: 'Message accepted — included in the next turn', severity: 'success' };
    case 'paused':
      return { message: 'Message accepted — background autonomy is paused', severity: 'info' };
    case 'waiting_for_capacity':
      return { message: 'Message accepted — waiting to start', severity: 'info' };
    default:
      return {
        message: armingToastCopy(arming),
        severity: arming === 'not_armed' ? 'info' : 'success',
      };
  }
}

/** Toast copy for Strategic workbench mode changes that wake or tried to wake Strategic. */
export function workbenchArmingToastCopy(outcome: ArmingOutcome | null | undefined): string | null {
  return outcome === 'steering' || outcome === 'not_armed' ? WORKBENCH_ARMING_TOAST[outcome] : null;
}

/**
 * Predict the outcome from the selected message type alone (before any send).
 * Operator-selectable types carry no event_type, so the only passive outcome a
 * composed message can predict is NOTE; routine_event only arises server-side.
 */
export function predictArming(newType: string): ArmingOutcome {
  return newType === 'NOTE' ? 'passive_note' : 'steering';
}

/**
 * Composer hint copy at compose time, accounting for whether the strategist is
 * currently working. The server's real outcome (`lastArming`) always wins once a
 * message has been sent. Before any send, the type-based prediction stands —
 * except a steering-type message composed while a cycle is already running is
 * queued for the next cycle rather than acting immediately, so its pre-send hint
 * says so. NOTE / passive / routine paths are untouched by the busy state.
 */
export function composerHintCopy(
  newType: string,
  lastArming: ArmingOutcome | null | undefined,
  strategistBusy?: boolean,
): string {
  if (lastArming) return armingHintCopy(lastArming);
  const predicted = predictArming(newType);
  if (predicted === 'steering' && strategistBusy) return QUEUED_STEERING_HINT;
  return armingHintCopy(predicted);
}

/** Narrow an unknown response field to a valid ArmingOutcome, else null. */
export function parseArmingOutcome(value: unknown): ArmingOutcome | null {
  return value === 'steering' ||
    value === 'passive_note' ||
    value === 'routine_event' ||
    value === 'not_armed' ||
    value === 'duplicate'
    ? value
    : null;
}
