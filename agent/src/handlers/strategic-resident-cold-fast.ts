import { randomUUID } from 'crypto';
import type WebSocket from 'ws';
import {
  apiGet, apiPost, sanitize, sendLog,
} from '../index.js';
import { HEARTBEAT_API_MAX_RETRIES, HEARTBEAT_API_TIMEOUT } from '../constants.js';
import { typedFastRuntimeConfig } from '../providers/foreground-resident.js';
import type { ResidentSessionEvent } from '../providers/resident-session.js';
import {
  advanceCursorFromMessages,
  admitMessageBatchPage,
  applyOpenMessageCursorFloor,
  BoundedSeenSet,
  combineOperatorMessages,
  initialCursor,
  materializeContentBlocks,
  MAX_IMAGES_PER_TURN,
  MAX_POLL_PAGES,
  MESSAGE_POLL_LIMIT,
  OPERATOR_MESSAGE_BATCH_CAP,
  residentConfig,
  type MessagesPollResponse,
  type StreamFrameKind,
  type ResidentInboundMessage,
  type SessionHeartbeatResponse,
} from './strategic-resident-lib.js';
import {
  callLeaseIsPubliclyActive,
  parseCallLeaseProjection,
} from './live-conversation-resident.js';
import {
  buildFastResidentContextFrame,
  verifyStrategicContextEnvelope,
  type StrategicContextEnvelopeTransport,
} from './strategic-context-envelope.js';
import {
  ForegroundTypedController,
  newTypedConversationReceipt,
  typedFastAbandonDispositionWithConfirmation,
  typedFastFailureBackoffMs,
} from './typed-fast-resident.js';
import { monitorClaimBoundWork } from './strategic-resident-claim-monitor.js';
import { SessionStreamer } from './session-streamer.js';
import { ConversationMessageStreamer } from './conversation-message-streamer.js';
import { WsFrameBudget } from './ws-frame-budget.js';

const COORDINATOR_API_ATTEMPTS = 1;
const COORDINATOR_API_TIMEOUT_MS = 5_000;
const CLAIM_LEASE_SECONDS = 90;
const CLAIM_RENEW_MS = 15_000;
const TYPED_LEASE_TTL_SECONDS = 300;
const TYPED_PROVISION_RETRY_MS = 5_000;
const RESPONDING_STATUS = 'Responding to operator...';
type WorkspacePreparationResult =
  | { status: 'ready' }
  | { status: 'failed'; error: unknown };
type TypedFastLease = { leaseId: string; generation: number };
type TypedClaimResponse = {
  claim_id?: string;
  owner?: string;
  message_ids?: string[];
  envelope?: StrategicContextEnvelopeTransport;
};
type TypedAbandonResponse = {
  status?: 'abandoned' | 'already_abandoned' | 'already_completed';
  legacy_fallback_admissible?: boolean;
};
type PendingClaimRequest = {
  clientClaimId: string;
  idempotencyKey: string;
  messageIds: string[];
  messages: ResidentInboundMessage[];
};
export interface CheckoutIndependentFastStartupOptions {
  taskId: string;
  payload: Record<string, unknown>;
  task: Record<string, unknown>;
  projectId: string;
  sessionId: string;
  workerGeneration: number;
  residentIncarnationId: string;
  workspacePreparation: Promise<void>;
  initialAdmission: SessionHeartbeatResponse;
  initialAdmissionAt: number;
  ws: WebSocket | null;
  statusRef: { value: string };
}

export interface CheckoutIndependentFastStartupResult {
  workspace: WorkspacePreparationResult | null;
  stopRequested: boolean;
  handoffSafe: boolean;
  latestAdmission: SessionHeartbeatResponse;
  latestAdmissionAt: number;
}
/**
 * Preserve an unknown-outcome Fast claim's exact input while a later poll adds
 * newer messages (or omits the leased input). Both the normal Resident loop
 * and the checkout-independent startup lane use this immutable-turn rule.
 */
export function selectTypedFastClaimMessages(
  messages: ResidentInboundMessage[],
  pending?: Pick<PendingClaimRequest, 'messageIds' | 'messages'> | null,
): ResidentInboundMessage[] | null {
  if (!pending) {
    const oldestClaimKey = messages.find(message => (
      typeof message.context?._conversation_claim_idempotency_key === 'string'
      && message.context._conversation_claim_idempotency_key.trim().length > 0
    ))?.context?._conversation_claim_idempotency_key;
    if (typeof oldestClaimKey === 'string') {
      const claimedMessages = messages.filter(message => (
        message.context?._conversation_claim_idempotency_key === oldestClaimKey
      ));
      const groupContext = claimedMessages[0]?.context;
      const expectedCount = groupContext?._conversation_claim_member_count;
      const expectedIds = groupContext?._conversation_claim_message_ids;
      if (groupContext?._conversation_claim_group_complete === false) return null;
      if (
        typeof expectedCount === 'number'
        && (!Number.isInteger(expectedCount) || expectedCount !== claimedMessages.length)
      ) return null;
      if (Array.isArray(expectedIds)) {
        const visibleIds = new Set(claimedMessages.map(message => message.id));
        if (
          expectedIds.length !== claimedMessages.length
          || expectedIds.some(id => typeof id !== 'string' || !visibleIds.has(id))
        ) return null;
      }
      return claimedMessages;
    }
    return messages;
  }
  const pageById = new Map(messages.map(message => [
    typeof message.id === 'string' ? message.id.trim() : '',
    message,
  ]));
  const selected = pending.messageIds.map(messageId => (
    pageById.get(messageId)
    ?? pending.messages.find(message => (
      typeof message.id === 'string' && message.id.trim() === messageId
    ))
  ));
  return selected.every((message): message is ResidentInboundMessage => message !== undefined)
    ? selected
    : null;
}

/** Build the exact restricted Fast prompt without consulting the checkout. */
export async function buildTypedFastDispatchPrompt(
  verified: ReturnType<typeof verifyStrategicContextEnvelope>,
  selectedMessages: ResidentInboundMessage[],
  transcriptSequence: number,
): Promise<string | Array<Record<string, unknown>>> {
  // The signed envelope already carries the terminal message text.
  const attachmentOnlyMessages = selectedMessages.map(message => ({ ...message, content: '' }));
  const attachmentBlocks = await materializeContentBlocks(
    combineOperatorMessages(attachmentOnlyMessages),
  );
  const artifactDeliveryDeferred = selectedMessages.some(message => (
    message.context?._server_artifact_delivery_deferred === true
  ));
  const promptText = [
    buildFastResidentContextFrame(verified, transcriptSequence),
    '',
    '## Active operator turn',
    `The canonical context ends at the active operator input (sequence ${verified.envelope.transcript_sequence}).`,
    attachmentBlocks.length > 0
      ? 'The model input following this text contains the active turn attachments.'
      : 'There are no separate attachment blocks for this turn.',
    ...(artifactDeliveryDeferred ? [
      'Recovery notice: attachments from this interrupted turn exceeded the safe delivery limit and were not included. Ask the operator to resend the relevant artifact if it is needed to answer accurately.',
    ] : []),
    '',
    'You MUST use mcp__call__complete_turn exactly once with the natural public reply.',
    'If fresh investigation is genuinely necessary, also use mcp__call__request_workday with a concise private request, but still complete this turn naturally. Do not narrate Workday queue, delegation, or progress status in the reply: the server renders the authoritative Workday requested receipt only after the private request commits atomically. Do not emit a competing public answer in plain text.',
    'If the operator requests code, configuration, branch, deployment, or other managed-state execution, request Workday to route it through supported Blueprint task tooling. Never promise that Fast will perform the mutation itself.',
    'Do not make a public promise to route, create, or hand off a Blueprint/Workday task. Stage the exact request with mcp__call__request_workday and let the server-owned receipt communicate that it was requested.',
  ].join('\n');
  if (attachmentBlocks.length === 0) return promptText;
  return [
    { type: 'text', text: promptText },
    ...attachmentBlocks.map(block => block as unknown as Record<string, unknown>),
  ];
}

function exactIds(left: string[], right: string[]): boolean {
  return [...left].sort().join('\n') === [...right].sort().join('\n');
}

function projectFastMessages(response: MessagesPollResponse): ResidentInboundMessage[] {
  const fastIds = new Set(
    (Array.isArray(response.fast_message_ids) ? response.fast_message_ids : [])
      .filter((id): id is string => typeof id === 'string' && id.length > 0),
  );
  const claimGroupByMessageId = new Map<string, {
    idempotencyKey: string;
    messageIds: string[];
    memberCount: number;
    complete: boolean;
    artifactsDegraded: boolean;
  }>();
  for (const group of Array.isArray(response.fast_claim_groups) ? response.fast_claim_groups : []) {
    const idempotencyKey = typeof group.idempotency_key === 'string'
      ? group.idempotency_key.trim() : '';
    const messageIds = Array.isArray(group.message_ids)
      ? group.message_ids.filter((id): id is string => typeof id === 'string' && id.length > 0)
      : [];
    const memberCount = typeof group.member_count === 'number'
      ? group.member_count : messageIds.length;
    if (!idempotencyKey || messageIds.length === 0) continue;
    for (const messageId of messageIds) {
      claimGroupByMessageId.set(messageId, {
        idempotencyKey,
        messageIds,
        memberCount,
        complete: group.complete === true,
        artifactsDegraded: group.artifacts_degraded === true,
      });
    }
  }
  return (Array.isArray(response.messages) ? response.messages : [])
    .filter(message => (
      message.from_type === 'user'
      && typeof message.id === 'string'
      && fastIds.has(message.id)
    ))
    .map(message => {
      const claimGroup = claimGroupByMessageId.get(message.id as string);
      return {
        ...message,
        context: {
          ...(message.context ?? {}),
          _typed_fast_eligible: true,
          _server_artifact_delivery_deferred: claimGroup?.artifactsDegraded === true,
          ...(claimGroup ? {
            _conversation_claim_idempotency_key: claimGroup.idempotencyKey,
            _conversation_claim_message_ids: claimGroup.messageIds,
            _conversation_claim_member_count: claimGroup.memberCount,
            _conversation_claim_group_complete: claimGroup.complete,
          } : {}),
        },
      };
    });
}

/**
 * Run the restricted typed lane while the repository clone is unresolved, or
 * drain one already-open Fast turn when preparation settled before this lane
 * received control. The function returns only after every known public claim
 * has a server-confirmed terminal disposition, so the later Workday process
 * cannot race it.
 */
export async function runCheckoutIndependentFastStartup(
  options: CheckoutIndependentFastStartupOptions,
): Promise<CheckoutIndependentFastStartupResult> {
  const {
    taskId, payload, task, projectId, sessionId, workerGeneration,
    residentIncarnationId, workspacePreparation, initialAdmission,
    initialAdmissionAt, ws, statusRef,
  } = options;
  const config = typedFastRuntimeConfig({
    enabled: payload.typed_fast_enabled === true,
    models: (
      payload.typed_fast_models
      && typeof payload.typed_fast_models === 'object'
      && !Array.isArray(payload.typed_fast_models)
    ) ? payload.typed_fast_models as Record<string, string> : undefined,
    effort: typeof payload.typed_fast_effort === 'string'
      ? payload.typed_fast_effort : undefined,
    qualification: typeof payload.typed_fast_qualification === 'string'
      ? payload.typed_fast_qualification : undefined,
  });
  const cfg = residentConfig(payload);
  let workspaceResult: WorkspacePreparationResult | null = null;
  const currentWorkspaceResult = (): WorkspacePreparationResult | null => workspaceResult;
  const observedWorkspace = workspacePreparation.then(
    () => { workspaceResult = { status: 'ready' }; },
    (error: unknown) => { workspaceResult = { status: 'failed', error }; },
  );
  // A failed preparation still fences all model work. Successful synchronous
  // warm prep gets one bounded Fast poll below instead of skipping input that
  // arrived after the task payload was created. The
  // public workspace helper is an async wrapper around its implementation, so
  // allow both its continuation and this observer to drain without waiting on
  // genuinely asynchronous clone work.
  await Promise.resolve();
  await Promise.resolve();
  const immediateWorkspace = currentWorkspaceResult();
  const drainSettledReadyWorkspace = immediateWorkspace?.status === 'ready';
  if (immediateWorkspace?.status === 'failed') {
    return {
      workspace: immediateWorkspace,
      stopRequested: false,
      handoffSafe: true,
      latestAdmission: initialAdmission,
      latestAdmissionAt: initialAdmissionAt,
    };
  }

  let latestAdmission = initialAdmission;
  let latestAdmissionAt = initialAdmissionAt;
  let callLease = parseCallLeaseProjection(initialAdmission.call);
  let stopRequested = false;
  let handoffSafe = true;
  let controller: ForegroundTypedController | null = null;
  let lease: TypedFastLease | null = null;
  let ready = false;
  let activeClaimId: string | null = null;
  let pendingClaim: PendingClaimRequest | null = null;
  let abandonAttempt: Promise<'fallback' | 'completed' | 'deferred'> | null = null;
  let transcriptSequence = 0;
  let previousStatus: string | null = null;
  let nextProvisionAt = 0;
  let failureCount = 0;
  // Accessors avoid stale control-flow narrowing across async callbacks.
  const currentController = (): ForegroundTypedController | null => controller;
  const currentLease = (): TypedFastLease | null => lease;
  const currentPendingClaim = (): PendingClaimRequest | null => pendingClaim;
  const isReady = (): boolean => ready;
  const isStopRequested = (): boolean => stopRequested;
  const isHandoffSafe = (): boolean => handoffSafe;
  const isCallActive = (): boolean => callLeaseIsPubliclyActive(callLease);

  // The operator's chat surface has no incremental feedback between send and the
  // durable reply commit. Drive the SAME rate-limited emitter the warm Resident
  // lane uses so a cold-start Fast turn streams too. Observational ONLY: nothing
  // here may change what is claimed, persisted, retried, or abandoned.
  // ONE budget for the socket. The live transcript and the conversation reply
  // preview stream the same turn concurrently; each self-budgeting at <=10/s
  // would put 20/s plus `log` frames against the backend's 20 msg/s close.
  const wsFrameBudget = new WsFrameBudget();
  const streamer = new SessionStreamer(ws, sessionId, taskId, wsFrameBudget);
  let conversationStreamer: ConversationMessageStreamer | null = null;
  let streamTurnOpen = false;
  const openStreamTurn = (claimId: string, idempotencyKey: string): void => {
    if (streamTurnOpen) return;
    streamTurnOpen = true;
    conversationStreamer = new ConversationMessageStreamer(
      ws, sessionId, taskId, claimId, idempotencyKey, wsFrameBudget,
    );
    streamer.responding();
  };
  /** Terminate an in-flight turn on EVERY exit path (success, provider failure,
   *  interrupt, claim loss, stop request) so no consumer is left with a stuck
   *  in-flight turn. Idempotent: a second call after `done` emits nothing. */
  const closeStreamTurn = (terminal?: 'failed' | 'abandoned'): void => {
    if (!streamTurnOpen) return;
    streamTurnOpen = false;
    // Bounded, rate-limited drain plus tombstone. Deliberately not awaited:
    // every caller is a synchronous teardown path, and the terminal frame is
    // no longer gated on the claim still being live (routers/projects.py).
    void conversationStreamer?.finish(terminal);
    conversationStreamer = null;
    streamer.flush();
    streamer.done();
  };
  const streamSessionEvent = (ev: ResidentSessionEvent): void => {
    if (ev.kind === 'text' && ev.text) {
      // Assistant text goes to BOTH surfaces. The `/strategic-session` live
      // transcript is protected supported surface (CLAUDE.md) and must render
      // exactly as it did before streaming existed — including every window
      // where no conversation streamer is bound (READY probe, post-crash
      // replacement, between a failed turn and the next dispatch). The
      // cumulative canonical message preview is purely additive; the durable
      // reply remains authored exclusively through the staged receipt-bound
      // completion.
      streamer.delta(ev.text);
      conversationStreamer?.delta(ev.text);
    } else if (ev.kind === 'tool' || ev.kind === 'activity') {
      if (ev.activity) {
        streamer.activity(ev.activity.kind as StreamFrameKind, ev.activity.text);
      } else if (ev.kind === 'tool') {
        streamer.tool(ev.toolName, ev.text);
      }
    } else if (ev.kind === 'error' && ev.text) {
      streamer.status(`error: ${ev.text}`);
    }
  };

  const restoreStatus = (): void => {
    if (previousStatus !== null && statusRef.value === RESPONDING_STATUS) {
      statusRef.value = previousStatus;
    }
    previousStatus = null;
  };

  const releaseLease = async (reason: string): Promise<void> => {
    const exactLease = lease;
    if (exactLease === null) return;
    lease = null;
    try {
      await apiPost(
        `/api/agent/projects/${projectId}/strategic-session/typed-fast-lease/release`,
        {
          task_id: taskId,
          worker_generation: workerGeneration,
          resident_incarnation_id: residentIncarnationId,
          lease_id: exactLease.leaseId,
          generation: exactLease.generation,
          reason,
        },
        HEARTBEAT_API_MAX_RETRIES,
        HEARTBEAT_API_TIMEOUT,
      );
    } catch (error) {
      sendLog(ws, `[resident] cold-start Fast slot release deferred: ${sanitize(String(error))}\n`);
    }
  };

  const closeController = async (reason: string): Promise<void> => {
    ready = false;
    try {
      controller?.close();
    } catch (error) {
      sendLog(ws, `[resident] cold-start Fast controller close failed: ${sanitize(String(error))}\n`);
    }
    controller = null;
    await releaseLease(reason);
  };

  const interruptController = (): void => {
    try {
      controller?.interrupt();
    } catch (error) {
      sendLog(ws, `[resident] cold-start Fast interrupt failed: ${sanitize(String(error))}\n`);
    }
  };

  const abandonActiveClaim = async (): Promise<'fallback' | 'completed' | 'deferred'> => {
    // Retract the ephemeral preview BEFORE the early returns. A stranded
    // preview renders as a delivered answer, so the tombstone must not be
    // conditional on this being the first abandon or on a claim still being
    // held. `closeStreamTurn` is idempotent when no turn is open.
    closeStreamTurn('abandoned');
    if (activeClaimId === null) return Promise.resolve('fallback');
    if (abandonAttempt !== null) return await abandonAttempt;
    const claimId = activeClaimId;
    const attempt = (async () => {
      try {
        const response = await apiPost(
          `/api/agent/projects/${projectId}/conversation/turns/abandon`,
          {
            task_id: taskId,
            session_id: sessionId,
            worker_generation: workerGeneration,
            incarnation_id: residentIncarnationId,
            claim_id: claimId,
          },
          COORDINATOR_API_ATTEMPTS,
          COORDINATOR_API_TIMEOUT_MS,
        ) as TypedAbandonResponse;
        const disposition = typedFastAbandonDispositionWithConfirmation(
          response.status,
          response.legacy_fallback_admissible,
        );
        if (disposition !== 'deferred') {
          activeClaimId = null;
          pendingClaim = null;
          restoreStatus();
        }
        return disposition;
      } catch (error) {
        sendLog(ws, `[resident] cold-start Fast claim release deferred: ${sanitize(String(error))}\n`);
        return 'deferred';
      }
    })();
    abandonAttempt = attempt;
    try {
      return await attempt;
    } finally {
      // Permit a later idempotent retry after this single-flight attempt settles.
      if (abandonAttempt === attempt) abandonAttempt = null;
    }
  };

  const heartbeat = async (): Promise<'continue' | 'claim_lost' | 'stop'> => {
    const leaseSnapshot = lease === null ? null : { ...lease };
    const claimSnapshot = activeClaimId;
    const body: Record<string, unknown> = {
      session_id: sessionId,
      task_id: taskId,
      progress: false,
      worker_generation: workerGeneration,
      resident_incarnation_id: residentIncarnationId,
      message_cursor: initialCursor(task, payload),
      ...(claimSnapshot === null ? {} : {
        conversation_claim_id: claimSnapshot,
        conversation_claim_incarnation: residentIncarnationId,
        conversation_claim_lease_seconds: CLAIM_LEASE_SECONDS,
      }),
      ...(leaseSnapshot === null ? {} : {
        typed_fast_lease_id: leaseSnapshot.leaseId,
        typed_fast_generation: leaseSnapshot.generation,
        typed_fast_ttl_seconds: TYPED_LEASE_TTL_SECONDS,
      }),
    };
    try {
      const response = await apiPost(
        `/api/agent/projects/${projectId}/strategic-session/heartbeat`,
        body,
        HEARTBEAT_API_MAX_RETRIES,
        HEARTBEAT_API_TIMEOUT,
      ) as SessionHeartbeatResponse;
      const priorAdmission = latestAdmission;
      const callProjectionPresent = Object.prototype.hasOwnProperty.call(response, 'call');
      const callTransportPresent = Object.prototype.hasOwnProperty.call(response, 'call_transport');
      latestAdmission = {
        ...response,
        ...(!callProjectionPresent && Object.prototype.hasOwnProperty.call(priorAdmission, 'call')
          ? { call: priorAdmission.call }
          : {}),
        ...(!callTransportPresent && Object.prototype.hasOwnProperty.call(priorAdmission, 'call_transport')
          ? { call_transport: priorAdmission.call_transport }
          : {}),
      };
      latestAdmissionAt = Date.now();
      if (response.stop_requested || response.resident_incarnation_held === false) {
        stopRequested = true;
        return 'stop';
      }
      // Omission is not revocation; explicit null/inactive remains authoritative.
      if (callProjectionPresent) callLease = parseCallLeaseProjection(response.call);
      const currentLeaseMatches = leaseSnapshot !== null && lease !== null
        && leaseSnapshot.leaseId === lease.leaseId
        && leaseSnapshot.generation === lease.generation;
      if (currentLeaseMatches && Object.prototype.hasOwnProperty.call(response, 'typed_fast')) {
        const projected = (response as SessionHeartbeatResponse & { typed_fast?: unknown }).typed_fast;
        const active = projected !== null
          && typeof projected === 'object'
          && !Array.isArray(projected)
          && (projected as Record<string, unknown>).status === 'active'
          && (projected as Record<string, unknown>).lease_id === leaseSnapshot.leaseId
          && Number((projected as Record<string, unknown>).generation) === leaseSnapshot.generation;
        if (!active) {
          lease = null;
          ready = false;
          controller?.close();
          controller = null;
          return claimSnapshot === null ? 'continue' : 'claim_lost';
        }
      }
      if (claimSnapshot !== null && response.conversation_claim_held === false) {
        return 'claim_lost';
      }
      // Chat survives authority lapse, but an active nested call pre-empts Fast.
      if (claimSnapshot !== null && isCallActive()) return 'claim_lost';
      return 'continue';
    } catch (error) {
      const message = String(error);
      if (/\((401|409)\)/.test(message) || /no longer active/i.test(message)) {
        stopRequested = true;
        return 'stop';
      }
      sendLog(ws, `[resident] cold-start heartbeat failed (non-fatal): ${sanitize(message)}\n`);
      return 'continue';
    }
  };

  const heartbeatIfDue = async (): Promise<void> => {
    if (
      workspaceResult === null
      && !stopRequested
      && Date.now() - latestAdmissionAt >= CLAIM_RENEW_MS
      && await heartbeat() === 'stop'
    ) stopRequested = true;
  };

  const pollFastMessages = async (): Promise<ResidentInboundMessage[]> => {
    let cursor = initialCursor(task, payload);
    let traversedFloor: string | null = null;
    const seen = new BoundedSeenSet();
    const collected = new Map<string, ResidentInboundMessage>();
    let requiredImages = 0;
    for (let page = 0; page < MAX_POLL_PAGES; page++) {
      const previousCursor = cursor;
      const sessionScope = sessionId
        ? `&session_id=${encodeURIComponent(sessionId)}&worker_generation=${workerGeneration}`
        : '';
      const response = await apiGet(
        `/api/agent/projects/${projectId}/messages?since=${encodeURIComponent(cursor)}&from_type=user&status=open&limit=${MESSAGE_POLL_LIMIT}${sessionScope}`,
        COORDINATOR_API_ATTEMPTS,
        COORDINATOR_API_TIMEOUT_MS,
      ) as MessagesPollResponse;
      const floor = applyOpenMessageCursorFloor(
        cursor,
        response.open_message_cursor_floor,
        traversedFloor,
      );
      traversedFloor = floor.traversedFloor;
      if (floor.rewound) {
        cursor = floor.cursor;
        continue;
      }
      const raw = Array.isArray(response.messages) ? response.messages : [];
      const projected = projectFastMessages(response);
      const admission = admitMessageBatchPage(
        projected,
        seen,
        OPERATOR_MESSAGE_BATCH_CAP - collected.size,
        MAX_IMAGES_PER_TURN - requiredImages,
      );
      for (const message of admission.admitted) {
        if (typeof message.id === 'string' && !collected.has(message.id)) {
          collected.set(message.id, message);
        }
      }
      requiredImages += admission.requiredImagesAdmitted;
      cursor = advanceCursorFromMessages(raw, cursor);
      if (collected.size >= OPERATOR_MESSAGE_BATCH_CAP) break;
      if (!admission.fullyAdmitted) break;
      if (raw.length < MESSAGE_POLL_LIMIT || cursor === previousCursor) break;
    }
    return [...collected.values()];
  };

  const monitorProvisioning = async (allowReadyWorkspace = false): Promise<void> => {
    if (!config.activated || Date.now() < nextProvisionAt) return;
    nextProvisionAt = Date.now() + TYPED_PROVISION_RETRY_MS;
    const workspaceFencesProvisioning = (): boolean => {
      const result = currentWorkspaceResult();
      return result !== null && (!allowReadyWorkspace || result.status === 'failed');
    };
    let settled = false;
    const provisioning = (async () => {
      try {
        const response = await apiPost(
          `/api/agent/projects/${projectId}/strategic-session/typed-fast-lease/acquire`,
          {
            task_id: taskId,
            worker_generation: workerGeneration,
            resident_incarnation_id: residentIncarnationId,
          },
          HEARTBEAT_API_MAX_RETRIES,
          HEARTBEAT_API_TIMEOUT,
        ) as { acquired?: unknown; reason?: unknown; lease?: unknown };
        const rawLease = response.lease;
        if (
          response.acquired !== true
          || rawLease === null
          || typeof rawLease !== 'object'
          || Array.isArray(rawLease)
          || typeof (rawLease as Record<string, unknown>).lease_id !== 'string'
          || !Number.isInteger((rawLease as Record<string, unknown>).generation)
        ) {
          const reason = typeof response.reason === 'string'
            ? response.reason : 'lease was not admitted';
          throw new Error(`typed Fast capacity unavailable: ${reason}`);
        }
        lease = {
          leaseId: (rawLease as Record<string, unknown>).lease_id as string,
          generation: (rawLease as Record<string, unknown>).generation as number,
        };
        if (workspaceFencesProvisioning() || isStopRequested()) return;
        controller = new ForegroundTypedController({
          adapter: config.adapter,
          model: config.model,
          effort: config.effort,
        });
        // Registered before the first session exists; the controller re-attaches
        // it to every session it starts, including a mid-turn crash replacement.
        controller.onEvent(streamSessionEvent);
        await controller.ensureReady();
        if (workspaceFencesProvisioning() || isStopRequested()) return;
        ready = controller.isAlive();
        if (!ready) throw new Error('typed Fast readiness child exited');
        nextProvisionAt = 0;
        failureCount = 0;
        sendLog(
          ws,
          `[resident] checkout-independent Fast foreground ready (${config.model}/${config.effort})\n`,
        );
      } catch (error) {
        ready = false;
        sendLog(
          ws,
          `[resident] checkout-independent Fast unavailable: ${sanitize(String(error))}\n`,
        );
      } finally {
        settled = true;
      }
    })();
    const provisioningSettled = (): boolean => settled;
    while (!provisioningSettled()) {
      await Promise.race([
        provisioning,
        ...(allowReadyWorkspace ? [] : [observedWorkspace]),
        new Promise<void>(resolve => setTimeout(resolve, CLAIM_RENEW_MS)),
      ]);
      if (workspaceFencesProvisioning() || isStopRequested()) {
        controller?.close();
        await provisioning;
        break;
      }
      if (!provisioningSettled() && await heartbeat() === 'stop') {
        controller?.close();
        await provisioning;
        break;
      }
    }
    if (!ready && lease !== null) await closeController('cold_start_readiness_incomplete');
  };

  const reconcileUnknownClaimForHandoff = async (): Promise<void> => {
    if (pendingClaim === null || activeClaimId !== null) return;
    try {
      const claimed = await apiPost(
        `/api/agent/projects/${projectId}/conversation/turns/claim`,
        {
          task_id: taskId,
          session_id: sessionId,
          worker_generation: workerGeneration,
          incarnation_id: residentIncarnationId,
          message_ids: pendingClaim.messageIds,
          owner: 'fast',
          lease_seconds: CLAIM_LEASE_SECONDS,
          client_claim_id: pendingClaim.clientClaimId,
          idempotency_key: pendingClaim.idempotencyKey,
        },
        COORDINATOR_API_ATTEMPTS,
        COORDINATOR_API_TIMEOUT_MS,
      ) as TypedClaimResponse;
      const claimedIds = Array.isArray(claimed.message_ids)
        ? claimed.message_ids.filter((id): id is string => typeof id === 'string') : [];
      if (
        typeof claimed.claim_id !== 'string'
        || !claimed.claim_id
        || claimed.owner !== 'fast'
        || !exactIds(claimedIds, pendingClaim.messageIds)
      ) {
        throw new Error('unknown cold-start Fast claim did not reconcile exactly');
      }
      activeClaimId = claimed.claim_id;
    } catch (error) {
      handoffSafe = false;
      sendLog(
        ws,
        `[resident] cold-start Fast claim outcome remains unknown; refusing Workday handoff: ${sanitize(String(error))}\n`,
      );
    }
  };

  const finishForWorkspaceOrStop = async (reason: string): Promise<void> => {
    await reconcileUnknownClaimForHandoff();
    if (activeClaimId !== null) {
      interruptController();
      const disposition = await abandonActiveClaim();
      if (disposition === 'deferred') handoffSafe = false;
    }
    await closeController(reason);
    restoreStatus();
  };

  const dispatchClaim = async (
    messages: ResidentInboundMessage[],
  ): Promise<'completed' | 'released' | 'deferred' | 'workspace'> => {
    let outcome: 'completed' | 'released' | 'deferred' | 'workspace' | null = null;
    try {
      outcome = await dispatchClaimTurn(messages);
      return outcome;
    } finally {
      // Only 'completed' proves a durable reply landed to replace the preview.
      // Every other exit, thrown errors included, must retract it — otherwise
      // the operator keeps a complete-looking answer that does not exist
      // server-side and vanishes on remount.
      closeStreamTurn(outcome === 'completed' ? undefined : 'failed');
    }
  };

  const dispatchClaimTurn = async (
    messages: ResidentInboundMessage[],
  ): Promise<'completed' | 'released' | 'deferred' | 'workspace'> => {
    const selectedMessages = selectTypedFastClaimMessages(messages, pendingClaim);
    if (selectedMessages === null) return 'deferred';
    const messageIds = selectedMessages.map(message => (
      typeof message.id === 'string' ? message.id.trim() : ''
    ));
    if (messageIds.length === 0 || messageIds.some(id => !id)) return 'deferred';
    pendingClaim ??= {
      clientClaimId: randomUUID(),
      idempotencyKey: `typed-fast-claim:${randomUUID()}`,
      messageIds: [...messageIds],
      messages: [...selectedMessages],
    };
    let claimed: TypedClaimResponse;
    try {
      claimed = await apiPost(
        `/api/agent/projects/${projectId}/conversation/turns/claim`,
        {
          task_id: taskId,
          session_id: sessionId,
          worker_generation: workerGeneration,
          incarnation_id: residentIncarnationId,
          message_ids: pendingClaim.messageIds,
          owner: 'fast',
          lease_seconds: CLAIM_LEASE_SECONDS,
          client_claim_id: pendingClaim.clientClaimId,
          idempotency_key: pendingClaim.idempotencyKey,
        },
        COORDINATOR_API_ATTEMPTS,
        COORDINATOR_API_TIMEOUT_MS,
      ) as TypedClaimResponse;
    } catch (error) {
      sendLog(
        ws,
        `[resident] checkout-independent Fast claim deferred: ${sanitize(String(error))}\n`,
      );
      return 'deferred';
    }
    const claimedIds = Array.isArray(claimed.message_ids)
      ? claimed.message_ids.filter((id): id is string => typeof id === 'string') : [];
    activeClaimId = typeof claimed.claim_id === 'string' && claimed.claim_id
      ? claimed.claim_id : null;
    abandonAttempt = null;
    if (
      activeClaimId === null
      || claimed.owner !== 'fast'
      || !exactIds(claimedIds, pendingClaim.messageIds)
      || !claimed.envelope
    ) {
      if (activeClaimId !== null && await abandonActiveClaim() === 'deferred') handoffSafe = false;
      return handoffSafe ? 'released' : 'deferred';
    }
    const claimedEnvelope = claimed.envelope;
    try {
      let verified: ReturnType<typeof verifyStrategicContextEnvelope>;
      try {
      verified = verifyStrategicContextEnvelope(claimed.envelope, {
        contextAuthorityGeneration: claimed.envelope.context_authority_generation,
        privacyGeneration: claimed.envelope.privacy_generation,
        minimumTranscriptSequence: claimed.envelope.transcript_sequence,
      });
      } catch (error) {
        sendLog(ws, `[resident] checkout-independent Fast envelope rejected: ${sanitize(String(error))}\n`);
        if (await abandonActiveClaim() === 'deferred') handoffSafe = false;
        return handoffSafe ? 'released' : 'deferred';
      }
      const receipt = newTypedConversationReceipt(activeClaimId, pendingClaim.messageIds);
      const monitorColdClaimWork = <T>(
        work: Promise<T>,
        onFence?: () => void,
        finishAfterWorkspaceReady = false,
      ) => monitorClaimBoundWork({
        work,
        renew: async () => await heartbeat() === 'continue',
        renewEveryMs: CLAIM_RENEW_MS,
        externalFence: observedWorkspace,
        shouldFenceExternal: () => (
          !finishAfterWorkspaceReady || currentWorkspaceResult()?.status !== 'ready'
        ),
        onFence,
      });
      const promptPromise = buildTypedFastDispatchPrompt(
        verified,
        selectedMessages,
        transcriptSequence,
      );
      // Finish an already-claimed checkout-independent response when clone succeeds;
      // only failure/stop/claim loss/call activation fences materialization.
      const promptAttempt = await monitorColdClaimWork(promptPromise, undefined, true);
      if (promptAttempt.fenced) {
        if (await abandonActiveClaim() === 'deferred') handoffSafe = false;
        return currentWorkspaceResult() !== null || isStopRequested() ? 'workspace' : 'deferred';
      }
      if (promptAttempt.result.status === 'rejected') throw promptAttempt.result.reason;
      const prompt = promptAttempt.result.value;
      if (workspaceResult?.status === 'failed' || stopRequested || isCallActive()) {
        if (await abandonActiveClaim() === 'deferred') handoffSafe = false;
        return 'workspace';
      }
      const exactController = controller;
      if (!ready || exactController === null || !exactController.isAlive()) {
        if (await abandonActiveClaim() === 'deferred') handoffSafe = false;
        return handoffSafe ? 'released' : 'deferred';
      }

      if (previousStatus === null) previousStatus = statusRef.value;
      // Pending poll/readiness/claim is not yet a response in progress.
      statusRef.value = RESPONDING_STATUS;
      openStreamTurn(activeClaimId, receipt.idempotency_key);
      const dispatchOnce = () => exactController.dispatch(receipt, prompt).then(
        value => ({ status: 'fulfilled' as const, value }),
        (reason: unknown) => ({ status: 'rejected' as const, reason }),
      );
      let turnPromise = dispatchOnce();
      let turnAttempt = await monitorColdClaimWork(
        turnPromise,
        () => { exactController.interrupt(); },
        true,
      );
      if (turnAttempt.fenced) {
        if (await abandonActiveClaim() === 'deferred') handoffSafe = false;
        return currentWorkspaceResult() !== null || isStopRequested() ? 'workspace' : 'deferred';
      }
      if (turnAttempt.result.status === 'rejected') throw turnAttempt.result.reason;
      let dispatched = turnAttempt.result.value;
      if (
        dispatched.status === 'rejected'
        || dispatched.value.result.processGone
        || dispatched.value.completion === null
      ) {
        exactController.replaceAfterCrash();
        transcriptSequence = 0;
        // The retry re-runs the same receipt from the top, so its text is a new
        // answer. Give it a distinct preview identity and tombstone attempt 1's
        // partial rather than welding the two together.
        conversationStreamer?.newAttempt();
        turnPromise = dispatchOnce();
        turnAttempt = await monitorColdClaimWork(
          turnPromise,
          () => { exactController.interrupt(); },
          true,
        );
        if (turnAttempt.fenced) {
          if (await abandonActiveClaim() === 'deferred') handoffSafe = false;
          return currentWorkspaceResult() !== null || isStopRequested() ? 'workspace' : 'deferred';
        }
        if (turnAttempt.result.status === 'rejected') throw turnAttempt.result.reason;
        dispatched = turnAttempt.result.value;
      }
      if (
        dispatched.status === 'rejected'
        || dispatched.value.result.processGone
        || dispatched.value.completion === null
      ) {
        await closeController('cold_start_receipt_protocol_failure');
        if (await abandonActiveClaim() === 'deferred') handoffSafe = false;
        return handoffSafe ? 'released' : 'deferred';
      }
      const turn = dispatched.value;
      const completion = turn.completion;
      if (completion === null) {
        throw new Error('cold-start Fast completion vanished after validation');
      }
      const workdayRequest = turn.workdayRequests[0];
      try {
        await apiPost(
          `/api/agent/projects/${projectId}/conversation/turns/complete`,
          {
            task_id: taskId,
            worker_generation: workerGeneration,
            incarnation_id: residentIncarnationId,
            claim_id: activeClaimId,
            owner: 'fast',
            content: completion.content,
            idempotency_key: receipt.idempotency_key,
            envelope: claimedEnvelope,
            ...(turn.workdayRequests.length > 0 ? {
              workday_request: {
                delivery_key: workdayRequest.delivery_key,
                question: workdayRequest.prompt,
              },
            } : {}),
          },
          COORDINATOR_API_ATTEMPTS,
          COORDINATOR_API_TIMEOUT_MS,
        );
      } catch (error) {
        sendLog(ws, `[resident] checkout-independent Fast completion uncertain: ${sanitize(String(error))}\n`);
        const disposition = await abandonActiveClaim();
        if (disposition === 'deferred') handoffSafe = false;
        return disposition === 'completed'
          ? 'completed'
          : disposition === 'fallback' ? 'released' : 'deferred';
      }
      transcriptSequence = verified.envelope.transcript_sequence;
      activeClaimId = null;
      pendingClaim = null;
      abandonAttempt = null;
      restoreStatus();
      return 'completed';
    } catch (error) {
      sendLog(
        ws,
        `[resident] checkout-independent Fast failed after exact claim: ${sanitize(String(error))}\n`,
      );
      interruptController();
      await closeController('cold_start_post_claim_failure');
      const disposition = await abandonActiveClaim();
      if (disposition === 'deferred') handoffSafe = false;
      return disposition === 'completed'
        ? 'completed'
        : disposition === 'fallback' ? 'released' : 'deferred';
    }
  };

  const waitForRetry = async (delayMs: number): Promise<void> => {
    const deadline = Date.now() + Math.max(1, delayMs);
    while (
      currentWorkspaceResult() === null
      && !isStopRequested()
      && isHandoffSafe()
      && Date.now() < deadline
    ) {
      const heartbeatDueIn = Math.max(
        1,
        CLAIM_RENEW_MS - (Date.now() - latestAdmissionAt),
      );
      await Promise.race([
        observedWorkspace,
        new Promise<void>(resolve => setTimeout(
          resolve,
          Math.min(deadline - Date.now(), heartbeatDueIn),
        )),
      ]);
      await heartbeatIfDue();
    }
  };

  if (
    drainSettledReadyWorkspace
    && !isStopRequested()
    && isHandoffSafe()
    && !isCallActive()
    && config.activated
  ) {
    let messages: ResidentInboundMessage[] = [];
    try {
      // Poll before provisioning so a quick workspace with no waiting Fast
      // turn pays no foreground-process startup cost.
      messages = await pollFastMessages();
    } catch (error) {
      sendLog(ws, `[resident] settled-workspace Fast poll failed: ${sanitize(String(error))}\n`);
    }
    if (messages.length > 0) {
      await monitorProvisioning(true);
      const exactController = currentController();
      if (
        isReady()
        && exactController !== null
        && currentLease() !== null
        && exactController.isAlive()
        && !isStopRequested()
        && !isCallActive()
      ) {
        await dispatchClaim(messages);
      }
    }
  }

  while (
    !drainSettledReadyWorkspace
    && currentWorkspaceResult() === null
    && !isStopRequested()
    && isHandoffSafe()
  ) {
    if (isCallActive()) {
      if (currentController() !== null || currentLease() !== null) {
        await closeController('cold_start_nested_call_active');
      }
      const heartbeatDueIn = Math.max(1, CLAIM_RENEW_MS - (Date.now() - latestAdmissionAt));
      await Promise.race([
        observedWorkspace,
        new Promise<void>(resolve => setTimeout(
          resolve,
          Math.min(cfg.messagePollMs, heartbeatDueIn),
        )),
      ]);
      await heartbeatIfDue();
      continue;
    }
    if (!config.activated) {
      await Promise.race([
        observedWorkspace,
        new Promise<void>(resolve => setTimeout(resolve, CLAIM_RENEW_MS)),
      ]);
      await heartbeatIfDue();
      continue;
    }
    const loopController = currentController();
    if (!isReady() || loopController === null || currentLease() === null || !loopController.isAlive()) {
      if (loopController !== null && !loopController.isAlive()) {
        await closeController('cold_start_foreground_process_gone');
      }
      await monitorProvisioning();
      if (currentWorkspaceResult() !== null || isStopRequested()) break;
      if (!isReady()) {
        await Promise.race([
          observedWorkspace,
          new Promise<void>(resolve => setTimeout(resolve, Math.max(
            1,
            nextProvisionAt - Date.now(),
          ))),
        ]);
        await heartbeatIfDue();
        continue;
      }
    }
    let messages: ResidentInboundMessage[] = [];
    try {
      messages = await pollFastMessages();
    } catch (error) {
      sendLog(ws, `[resident] checkout-independent Fast poll failed: ${sanitize(String(error))}\n`);
    }
    if (currentWorkspaceResult() !== null || isStopRequested()) break;
    if (messages.length > 0 || currentPendingClaim() !== null) {
      const outcome = await dispatchClaim(messages);
      if (outcome === 'workspace' || !isHandoffSafe()) break;
      if (outcome === 'completed') {
        failureCount = 0;
      } else {
        failureCount += 1;
        await waitForRetry(Math.max(
          cfg.messagePollMs,
          typedFastFailureBackoffMs(failureCount),
        ));
      }
      await heartbeatIfDue();
      continue;
    }
    const heartbeatDueIn = Math.max(1, CLAIM_RENEW_MS - (Date.now() - latestAdmissionAt));
    await Promise.race([
      observedWorkspace,
      new Promise<void>(resolve => setTimeout(
        resolve,
        Math.min(cfg.messagePollMs, heartbeatDueIn),
      )),
    ]);
    await heartbeatIfDue();
  }

  const settledWorkspace = currentWorkspaceResult();
  await finishForWorkspaceOrStop(
    settledWorkspace?.status === 'failed'
      ? 'cold_start_workspace_failed'
      : isStopRequested()
        ? 'cold_start_stopped'
        : 'cold_start_workspace_ready',
  );
  // Clone is uncancellable; fence teardown until its shared-checkout mutation settles.
  if (currentWorkspaceResult() === null) await observedWorkspace;
  return {
    workspace: currentWorkspaceResult(),
    stopRequested: isStopRequested(),
    handoffSafe: isHandoffSafe(),
    latestAdmission,
    latestAdmissionAt,
  };
}
