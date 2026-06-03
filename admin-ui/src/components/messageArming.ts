/**
 * Operator-message arming outcomes and their operator-facing copy.
 *
 * Single source of truth shared by the MessagesPanel (post-send toast) and the
 * MessagesComposer (steering hint), mirroring the backend strategic_trigger
 * ARMING_* constants. `steering` armed the strategist; `passive_note` and
 * `routine_event` were recorded but do not wake it. Kept in its own module (not
 * the component file) so these helpers can be exported without tripping
 * react-refresh's only-export-components rule.
 */
export type ArmingOutcome = 'steering' | 'passive_note' | 'routine_event';

// Outcome → composer hint copy.
const ARMING_HINT: Record<ArmingOutcome, string> = {
  steering: 'Steering message — reaches the strategist soon.',
  passive_note: 'Recorded as a note — won’t wake the strategist.',
  routine_event: 'Routine event — won’t wake the strategist.',
};

// Outcome → post-send toast copy.
const ARMING_TOAST: Record<ArmingOutcome, string> = {
  steering: 'Message sent — steering the strategist',
  passive_note: 'Note recorded — won’t wake the strategist',
  routine_event: 'Recorded — routine event, won’t wake the strategist',
};

/** Composer hint copy for a given (real or predicted) arming outcome. */
export function armingHintCopy(outcome: ArmingOutcome): string {
  return ARMING_HINT[outcome];
}

/** Post-send toast copy for a server-reported arming outcome. */
export function armingToastCopy(outcome: ArmingOutcome | null | undefined): string {
  return outcome ? ARMING_TOAST[outcome] : 'Message sent';
}

/**
 * Predict the outcome from the selected message type alone (before any send).
 * Operator-selectable types carry no event_type, so the only passive outcome a
 * composed message can predict is NOTE; routine_event only arises server-side.
 */
export function predictArming(newType: string): ArmingOutcome {
  return newType === 'NOTE' ? 'passive_note' : 'steering';
}

/** Narrow an unknown response field to a valid ArmingOutcome, else null. */
export function parseArmingOutcome(value: unknown): ArmingOutcome | null {
  return value === 'steering' || value === 'passive_note' || value === 'routine_event'
    ? value
    : null;
}
