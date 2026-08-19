import type {
  StrategicResidentSessionAvailability,
  StrategicStateTaskFocus,
  StrategicStateTaskFocusItem,
} from '../strategicStateApi';
import type { ResidentActivityState } from './strategicSessionLive';
import type { ProjectMessage } from '../types/messages';

export type ConversationWorkdayState =
  | 'off'
  | 'ready'
  | 'starting'
  | 'working'
  | 'waiting'
  | 'consultation_queued'
  | 'consultation_running'
  | 'consultation_delivering'
  | 'paused'
  | 'retrying'
  | 'stopping'
  | 'unavailable'
  | 'needs_restart';

export interface ConversationWorkdayStatus {
  state: ConversationWorkdayState;
  label: string;
  compactLabel?: string;
  detail?: string;
  compactDetail?: string;
  title: string;
  /** Epoch ms of the last observed Resident action (executing start, or the
   *  frozen idle/dead heartbeat). Null when no Resident session is live, so
   *  callers can render "when it last acted" without hovering. */
  lastActivityMs?: number | null;
}

const TASK_DETAIL_MAX_CHARS = 96;
const CONSULTATION_REASON_CODES = new Set([
  'consultation_queued',
  'consultation_running',
  'consultation_delivering',
]);

function compactText(value: string | null | undefined, maxChars: number): string | null {
  const compact = value?.trim().replace(/\s+/g, ' ');
  if (!compact) return null;
  return compact.length <= maxChars ? compact : `${compact.slice(0, maxChars - 1)}…`;
}

function statusLabel(status: string): string {
  return status.trim().toLowerCase().replaceAll('_', ' ');
}

export interface ConversationWorkdayConsultationReceipts {
  requestedIds: Set<string>;
  resolvedIds: Set<string>;
  unresolvedIds: Set<string>;
}

/**
 * Reconcile only canonical, server-owned public receipts. The source Fast
 * reply's message id is the opaque consultation id; the exact Workday
 * continuation carries that same id. Counts stay correct when A and B are
 * queued concurrently and continuation A arrives before B finishes.
 */
export function deriveConversationWorkdayConsultations(
  conversationMessages: ProjectMessage[],
): ConversationWorkdayConsultationReceipts {
  const requestedIds = new Set<string>();
  const resolvedIds = new Set<string>();
  for (const message of conversationMessages) {
    const fromType = (message.from_type || '').toLowerCase();
    if (fromType !== 'strategic' && fromType !== 'strategist') continue;
    const context = message.context ?? {};
    if (
      context.message_role === 'operator_reply'
      && context._workday_consultation_requested === true
      && message.id
    ) {
      requestedIds.add(message.id);
      continue;
    }
    const consultationId = context._workday_consultation_atomic_reply_id;
    if (
      context.message_role === 'workday_consultation_continuation'
      && typeof context._workday_result_packet_id === 'string'
      && context._workday_result_packet_id.trim().length > 0
      && typeof consultationId === 'string'
      && consultationId.trim().length > 0
    ) {
      resolvedIds.add(consultationId.trim());
    }
  }
  return {
    requestedIds,
    resolvedIds,
    unresolvedIds: new Set(
      [...requestedIds].filter(id => !resolvedIds.has(id)),
    ),
  };
}

function highestPriorityTask(
  taskFocus: StrategicStateTaskFocus | null | undefined,
  residentTaskId: string | null | undefined,
): StrategicStateTaskFocusItem | null {
  const firstNonResident = (bucket: 'needs_judgment' | 'friction' | 'active') => (
    taskFocus?.buckets?.[bucket]?.items.find(task => task.id !== residentTaskId) ?? null
  );
  // Preserve the server's ordering inside each bucket while surfacing the
  // operator decision boundary before ordinary friction or active work.
  return firstNonResident('needs_judgment')
    ?? firstNonResident('friction')
    ?? firstNonResident('active');
}

function taskStatusDetail(task: StrategicStateTaskFocusItem | null): {
  detail?: string;
  compactDetail?: string;
  title?: string;
} {
  if (!task) return {};
  const status = statusLabel(task.status);
  const shortId = task.id.slice(0, 8);
  const title = compactText(task.title, 180) ?? `Task ${shortId}`;
  const reason = compactText(
    task.blocked_reason
      ?? task.detail
      ?? task.movement_explanation
      ?? task.latest_status_message,
    220,
  );
  return {
    detail: compactText(`${shortId} · ${status} · ${title}`, TASK_DETAIL_MAX_CHARS) ?? undefined,
    compactDetail: `${shortId} ${status}`,
    title: `${title} (${task.id}) — ${status}${reason ? `: ${reason}` : ''}`,
  };
}

export function buildConversationWorkdayStatus({
  interactionMode,
  residentActivity,
  residentAvailability,
  taskFocus,
  residentTaskId,
  conversationMessages = [],
}: {
  interactionMode: string | null | undefined;
  residentActivity: ResidentActivityState | null;
  residentAvailability?: StrategicResidentSessionAvailability | null;
  taskFocus?: StrategicStateTaskFocus | null;
  residentTaskId?: string | null;
  conversationMessages?: ProjectMessage[];
}): ConversationWorkdayStatus {
  const task = taskStatusDetail(highestPriorityTask(taskFocus, residentTaskId));
  const consultationReceipts = deriveConversationWorkdayConsultations(conversationMessages);
  const unresolvedCount = consultationReceipts.unresolvedIds.size;
  const activityDetail = compactText(
    residentActivity?.detail ?? residentActivity?.commandText ?? residentActivity?.label,
    220,
  );
  // Frozen idle/dead heartbeat when present, else the live executing start —
  // both are "when the Resident last actually did something" in epoch ms.
  const lastActivityMs = residentActivity?.endMs ?? residentActivity?.startMs ?? null;
  const withTask = (
    state: ConversationWorkdayState,
    label: string,
    baseTitle: string,
  ): ConversationWorkdayStatus => ({
    state,
    label,
    // The current activity is what the operator asked to see without hovering —
    // it must lead; the queued-task focus item is secondary context, not a
    // replacement for naming what the Resident is doing right now.
    detail: compactText(
      [activityDetail, task.detail].filter(Boolean).join(' · '),
      TASK_DETAIL_MAX_CHARS,
    ) ?? undefined,
    compactDetail: activityDetail
      ? compactText(activityDetail, 40) ?? undefined
      : task.compactDetail,
    title: [baseTitle, activityDetail, task.title].filter(Boolean).join(' · '),
    lastActivityMs,
  });
  const withConsultation = (
    state: ConversationWorkdayState,
    label: string,
    baseTitle: string,
    options: {
      compactLabel?: string;
      compactReason?: string | null;
      forceCount?: boolean;
      minimumCount?: number;
      visibleReason?: string | null;
    } = {},
  ): ConversationWorkdayStatus => {
    const queueCount = Math.max(unresolvedCount, options.minimumCount ?? 0);
    const queueDetail = queueCount > 1 || (options.forceCount && queueCount > 0)
      ? `${queueCount} open request${queueCount === 1 ? '' : 's'}`
      : undefined;
    const compactQueueDetail = queueDetail ? `${queueCount} open` : undefined;
    // The open-request count is secondary context; it must never displace the
    // substance of what the Resident is doing (pause reason, then live
    // activity, then the queued-task focus item) from the one visible slot.
    const substance = compactText(
      [options.visibleReason, activityDetail, task.detail].filter(Boolean).join(' · '),
      TASK_DETAIL_MAX_CHARS,
    ) ?? undefined;
    const visibleDetail = [queueDetail, substance].filter(Boolean).join(' · ') || undefined;
    const compactSubstance = options.compactReason
      ?? (activityDetail ? compactText(activityDetail, 40) ?? undefined : undefined);
    const compactVisibleDetail = [compactQueueDetail, compactSubstance]
      .filter(Boolean)
      .join(' · ') || undefined;
    return {
      state,
      label,
      compactLabel: options.compactLabel,
      detail: visibleDetail ?? task.detail,
      compactDetail: compactVisibleDetail ?? visibleDetail ?? task.compactDetail,
      title: [baseTitle, options.visibleReason, activityDetail, task.title]
        .filter(Boolean)
        .join(' · '),
      lastActivityMs,
    };
  };

  const structuredConsultationId = residentActivity?.consultationId?.trim() || null;
  const structuredConsultationResolved = structuredConsultationId !== null
    && consultationReceipts.resolvedIds.has(structuredConsultationId);
  const structuredConsultationCurrent = Boolean(
    residentActivity?.reasonCode
    && structuredConsultationId
    && (
      consultationReceipts.requestedIds.has(structuredConsultationId)
      || residentActivity.consultationVerified === true
    )
    && !structuredConsultationResolved
  );
  const openConsultationCount = Math.max(
    unresolvedCount,
    structuredConsultationCurrent ? 1 : 0,
  );
  const autonomyReason = residentActivity?.autonomyReasonCode;
  const consultationPaused = residentActivity?.autonomyState === 'paused'
    && !CONSULTATION_REASON_CODES.has(autonomyReason ?? '');
  const pauseLabel = autonomyReason === 'foreground_call' ? 'on call' : 'paused';
  const pauseReason = consultationPaused
    ? autonomyReason ? statusLabel(autonomyReason) : 'paused'
    : null;
  const compactPauseReason = autonomyReason === 'authority_not_held'
    ? 'no authority'
    : pauseReason;

  // A normal Stop/Chat transition retains an ended Resident audit snapshot.
  // Once every public consultation request is settled, the operator's current
  // Chat preference is authoritative: the retained snapshot is not a restart
  // request. Open requests still take precedence below so they cannot vanish.
  if (interactionMode !== 'continuous' && openConsultationCount === 0) {
    return withTask(
      'off',
      'Workday off',
      'Private Continuous Workday Resident autonomy is off in Chat mode; already-dispatched pipeline work may continue; other project autonomy may continue',
    );
  }

  if (residentActivity?.durablePhase === 'retrying') {
    if (openConsultationCount > 0) {
      return withConsultation(
        'retrying',
        'Workday requested · retrying',
        'A private Workday request is durable while the Resident waits to retry',
        {
          compactLabel: 'requested · retrying',
          forceCount: true,
          minimumCount: openConsultationCount,
        },
      );
    }
    return withTask(
      'retrying',
      'Workday retrying',
      'Private Continuous Workday Resident is waiting to retry',
    );
  }
  if (residentActivity?.durablePhase === 'stopping') {
    if (openConsultationCount > 0) {
      return withConsultation(
        'stopping',
        'Workday requested · stopping',
        'A private Workday request remains open while the Resident is stopping',
        {
          compactLabel: 'requested · stopping',
          forceCount: true,
          minimumCount: openConsultationCount,
        },
      );
    }
    return withTask(
      'stopping',
      'Workday stopping',
      'Private Continuous Workday Resident is stopping; already-dispatched pipeline work and other project autonomy may continue',
    );
  }
  if (residentActivity?.phase === 'dead') {
    if (openConsultationCount > 0) {
      return withConsultation(
        'needs_restart',
        'Workday requested · needs restart',
        'A private Workday request remains open, but the Resident is not live',
        {
          compactLabel: 'requested · restart',
          forceCount: true,
          minimumCount: openConsultationCount,
        },
      );
    }
    return withTask(
      'needs_restart',
      'Workday needs restart',
      'Private Continuous Workday Resident is not live; already-dispatched pipeline work may continue; other project autonomy may continue',
    );
  }

  if (structuredConsultationCurrent) {
    if (residentActivity?.reasonCode === 'consultation_running') {
      return withConsultation(
        'consultation_running',
        'Workday consulting',
        'Private Continuous Workday is actively investigating a Fast request',
      );
    }
    if (residentActivity?.reasonCode === 'consultation_delivering') {
      return withConsultation(
        'consultation_delivering',
        'Workday findings ready',
        'Workday finished; Fast is preparing the public continuation',
      );
    }
    if (residentActivity?.reasonCode === 'consultation_queued') {
      if (consultationPaused) {
        return withConsultation(
          'consultation_queued',
          `Workday requested · ${pauseLabel}`,
          'Fast requested a private Continuous Workday consultation, but the Resident cannot act now',
          {
            compactLabel: `requested · ${pauseLabel}`,
            compactReason: compactPauseReason,
            forceCount: true,
            minimumCount: 1,
            visibleReason: pauseReason,
          },
        );
      }
      return withConsultation(
        'consultation_queued',
        'Workday requested',
        'Fast requested a private Continuous Workday consultation',
      );
    }
  }

  // The public Fast reply is delivered realtime and can beat the Agent's first
  // queued heartbeat. Preserve that immediate, exactly-correlated truth until
  // every request id has a matching public continuation.
  if (unresolvedCount > 0) {
    if (
      !residentActivity
      && residentAvailability
      && !['active', 'starting', 'waiting', 'ready'].includes(residentAvailability.state)
    ) {
      return withConsultation(
        'unavailable',
        'Workday requested · unavailable',
        `A private Workday request remains open, but the Resident is unavailable${residentAvailability.reason ? `: ${residentAvailability.reason}` : ''}`,
        {
          compactLabel: 'requested · unavailable',
          forceCount: true,
          minimumCount: unresolvedCount,
        },
      );
    }
    if (consultationPaused) {
      return withConsultation(
        'consultation_queued',
        `Workday requested · ${pauseLabel}`,
        'Fast requested a private Continuous Workday consultation, but the Resident cannot act now',
        {
          compactLabel: `requested · ${pauseLabel}`,
          compactReason: compactPauseReason,
          forceCount: true,
          minimumCount: unresolvedCount,
          visibleReason: pauseReason,
        },
      );
    }
    return withConsultation(
      'consultation_queued',
      'Workday requested',
      'Fast requested a private Continuous Workday consultation',
    );
  }

  const staleStructuredConsultation = Boolean(
    residentActivity?.reasonCode && !structuredConsultationCurrent,
  );
  if (
    residentActivity?.autonomyState === 'paused'
    && autonomyReason !== 'consultation_queued'
    && autonomyReason !== 'consultation_running'
    && autonomyReason !== 'consultation_delivering'
  ) {
    const foregroundCall = autonomyReason === 'foreground_call';
    return withTask(
      'paused',
      foregroundCall ? 'Workday on call' : 'Workday paused',
      foregroundCall
        ? 'Private Continuous Workday autonomy is paused while the foreground call owns Strategic'
        : `Private Continuous Workday cannot take an autonomous turn now${autonomyReason ? `: ${statusLabel(autonomyReason)}` : ''}`,
    );
  }

  if (residentActivity?.phase === 'executing' && !staleStructuredConsultation) {
    return withTask(
      'working',
      'Workday working',
      'Private Continuous Workday Resident is actively working',
    );
  }

  if (residentActivity) {
    return withTask(
      'waiting',
      'Workday waiting',
      'Private Continuous Workday Resident is live but is not reporting active work',
    );
  }

  if (residentAvailability?.state === 'starting') {
    return withTask(
      'starting',
      'Workday starting',
      `Private Continuous Workday Resident is starting${residentAvailability.reason ? `: ${residentAvailability.reason}` : ''}`,
    );
  }
  if (residentAvailability?.state === 'active') {
    return withTask(
      'waiting',
      'Workday waiting',
      `Private Continuous Workday Resident is active but is not reporting active work${residentAvailability.reason ? `: ${residentAvailability.reason}` : ''}`,
    );
  }
  if (residentAvailability?.state === 'waiting') {
    return withTask(
      'retrying',
      'Workday retrying',
      `Private Continuous Workday Resident is waiting to retry${residentAvailability.reason ? `: ${residentAvailability.reason}` : ''}`,
    );
  }
  if (residentAvailability?.state === 'ready') {
    return withTask(
      'ready',
      'Workday ready',
      residentAvailability.reason
        ? `An eligible Workday runner is available, but no active Resident session is reporting activity yet: ${residentAvailability.reason}`
        : 'An eligible Workday runner is available, but no active Resident session is reporting activity yet',
    );
  }
  return withTask(
    'unavailable',
    'Workday unavailable',
    `Private Continuous Workday Resident is unavailable${residentAvailability?.reason ? `: ${residentAvailability.reason}` : ''}; already-dispatched pipeline work may continue; other project autonomy may continue`,
  );
}
