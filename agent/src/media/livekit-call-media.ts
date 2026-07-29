/**
 * LiveKit call-media boundary.
 *
 * @livekit/rtc-node is a developer-preview SDK.  Keep its API inside this
 * module: callers interact only with CallMediaSession and can replace the
 * bindings when the SDK stabilises.  The coordinator receives a short-lived
 * participant token from the backend; it must never receive an API secret.
 */
import {
  AudioFrame,
  AudioSource,
  AudioStream,
  LocalAudioTrack,
  Room,
  RoomEvent,
  TrackKind,
  TrackPublishOptions,
  TrackSource,
} from '@livekit/rtc-node';

export const CALL_MEDIA_PROTOCOL_VERSION = 1 as const;
export const CALL_MEDIA_MAX_PACKET_BYTES = 16 * 1024;
export const CALL_MEDIA_MAX_TEXT_CHARS = 12_000;
export const CALL_MEDIA_MAX_DEDUPE_ENTRIES = 1_024;

export const CALL_MEDIA_TOPICS = {
  committedMessage: 'blueprint.call.wake',
  bargeIn: 'blueprint.call.barge-in.v1',
  readiness: 'blueprint.call.readiness.v1',
  display: 'blueprint.call.display.v1',
  transcript: 'blueprint.call.operator-caption.v1',
  ttsSource: 'blueprint.call.tts-source.v1',
} as const;

export interface CallMediaLease {
  callId: string;
  generation: number;
}

/** Backend-issued, short-lived join credential.  It is safe only in memory. */
export interface CallTransportCredential {
  serverUrl: string;
  participantToken: string;
  room: string;
  participantIdentity: string;
  /** The one browser identity to which this coordinator may subscribe or send. */
  operatorIdentity: string;
}

export interface CallCommittedMessageHint {
  callId: string;
  generation: number;
  messageId: string;
  sequence: number;
}

export interface CallBargeIn {
  callId: string;
  generation: number;
  playoutRevision: number;
  playedPrefixChars: number;
}

export interface CallDisplayPacket {
  callId: string;
  generation: number;
  packetId: string;
  text?: string;
  displayBlocks?: Array<{ type: 'latex' | 'code' | 'table' | 'artifact_ref'; content: string }>;
}

export interface CallTtsSourcePacket {
  callId: string;
  generation: number;
  phraseId: string;
  event: 'started' | 'completed' | 'interrupted';
}

export interface CallCaption {
  callId: string;
  generation: number;
  segmentId: string;
  text: string;
  final: boolean;
  startTimeMs?: number;
  endTimeMs?: number;
}

/** Ephemeral STT UI projection. It is never a durable message or model input. */
export interface CallTransientTranscript {
  callId: string;
  generation: number;
  segmentId: string;
  text: string;
  final: boolean;
}

export interface CallAudioFrame {
  data: Int16Array;
  sampleRate: number;
  channels: number;
  samplesPerChannel: number;
}

/** Fixed for one published output track; changing it requires a reconnect. */
export interface CallOutputAudioFormat {
  sampleRate: number;
  channels: number;
}

export interface CallInputAudioFormat {
  sampleRate: number;
  channels: number;
  /** Bounded normalization frame; avoids high-latency giant browser frames. */
  frameSizeMs: number;
}

export interface CallMediaMetrics {
  connects: number;
  disconnects: number;
  reconnects: number;
  rejectedPackets: number;
  acceptedHints: number;
  duplicateHints: number;
  inputFrames: number;
  outputFrames: number;
  bargeIns: number;
}

type EventHandler = (...args: unknown[]) => void;

export interface RtcRemoteTrackPublication {
  kind?: unknown;
  track?: unknown;
  setSubscribed(subscribed: boolean): void;
}

export interface RtcRemoteParticipant {
  identity: string;
  trackPublications: Map<string, RtcRemoteTrackPublication>;
}

export interface RtcLocalParticipant {
  publishData(data: Uint8Array, options: { reliable?: boolean; destination_identities?: string[]; topic?: string }): Promise<void>;
  publishTrack(track: unknown, options: unknown): Promise<{ sid?: string }>;
  publishTranscription(transcription: {
    participantIdentity: string;
    trackSid: string;
    segments: Array<{ id: string; text: string; startTime: bigint; endTime: bigint; language: string; final: boolean }>;
  }): Promise<void>;
}

export interface RtcRoom {
  connect(url: string, token: string, options: { autoSubscribe: boolean; dynacast: boolean }): Promise<void>;
  disconnect(): Promise<void>;
  on(event: unknown, handler: EventHandler): unknown;
  off?(event: unknown, handler: EventHandler): unknown;
  remoteParticipants: Map<string, RtcRemoteParticipant>;
  localParticipant?: RtcLocalParticipant;
  name?: string;
  isConnected?: boolean;
}

interface RtcAudioSource {
  captureFrame(frame: unknown): Promise<void>;
  clearQueue(): void;
  close(): Promise<void>;
}

export interface RtcAudioReader {
  read(): Promise<{ done: boolean; value?: unknown }>;
  cancel(reason?: unknown): Promise<void>;
}

export interface RtcNodeBindings {
  createRoom(): RtcRoom;
  createAudioStream(track: unknown, options: { sampleRate: number; numChannels: number; frameSizeMs: number }): { getReader(): RtcAudioReader };
  createAudioSource(sampleRate: number, channels: number): RtcAudioSource;
  createLocalAudioTrack(name: string, source: RtcAudioSource): unknown;
  createAudioFrame(frame: CallAudioFrame): unknown;
  createTrackPublishOptions(): unknown;
  setMicrophoneSource(options: unknown): void;
  isAudioKind(kind: unknown): boolean;
  events: {
    trackPublished: unknown;
    trackSubscribed: unknown;
    dataReceived: unknown;
    participantConnected: unknown;
    participantDisconnected: unknown;
    reconnecting: unknown;
    reconnected: unknown;
    disconnected: unknown;
  };
}

/** Actual rtc-node binding; tests use a deterministic fake instead. */
export function createRtcNodeBindings(): RtcNodeBindings {
  return {
    createRoom: () => new Room(),
    createAudioStream: (track, options) => new AudioStream(track as never, options),
    createAudioSource: (sampleRate, channels) => new AudioSource(sampleRate, channels),
    createLocalAudioTrack: (name, source) => LocalAudioTrack.createAudioTrack(name, source as AudioSource),
    createAudioFrame: (frame) => new AudioFrame(
      frame.data,
      frame.sampleRate,
      frame.channels,
      frame.samplesPerChannel,
    ),
    createTrackPublishOptions: () => new TrackPublishOptions(),
    setMicrophoneSource: (options) => { (options as TrackPublishOptions).source = TrackSource.SOURCE_MICROPHONE; },
    isAudioKind: (kind) => kind === TrackKind.KIND_AUDIO,
    events: {
      trackPublished: RoomEvent.TrackPublished,
      trackSubscribed: RoomEvent.TrackSubscribed,
      dataReceived: RoomEvent.DataReceived,
      participantConnected: RoomEvent.ParticipantConnected,
      participantDisconnected: RoomEvent.ParticipantDisconnected,
      reconnecting: RoomEvent.Reconnecting,
      reconnected: RoomEvent.Reconnected,
      disconnected: RoomEvent.Disconnected,
    },
  };
}

export interface CallMediaCallbacks {
  onCommittedMessageHint?(hint: CallCommittedMessageHint): void;
  onBargeIn?(event: CallBargeIn): void;
  onOperatorPresenceChanged?(present: boolean): void;
  onConnectionState?(state: 'connecting' | 'connected' | 'reconnecting' | 'disconnected'): void;
}

export interface CallMediaSessionOptions {
  bindings?: RtcNodeBindings;
  callbacks?: CallMediaCallbacks;
  outputFormat?: CallOutputAudioFormat;
  inputFormat?: CallInputAudioFormat;
}

function requireText(value: unknown, field: string, max = CALL_MEDIA_MAX_TEXT_CHARS): string {
  if (typeof value !== 'string' || !value.trim() || value.length > max) throw new Error(`invalid ${field}`);
  return value;
}

function requireInteger(value: unknown, field: string, min = 0): number {
  if (!Number.isSafeInteger(value) || (value as number) < min) throw new Error(`invalid ${field}`);
  return value as number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function encodedSize(value: unknown): number {
  return new TextEncoder().encode(JSON.stringify(value)).byteLength;
}

function trimSeen(seen: Set<string>): void {
  while (seen.size > CALL_MEDIA_MAX_DEDUPE_ENTRIES) {
    const first = seen.values().next().value;
    if (typeof first !== 'string') return;
    seen.delete(first);
  }
}

/**
 * Owns only media transport and trust boundaries.  It never fetches a message,
 * invokes a model, or logs a credential/payload.  Browser data is a wake hint;
 * callers must fetch the canonical durable message before acting on it.
 */
export class CallMediaSession {
  private readonly bindings: RtcNodeBindings;
  private readonly callbacks: CallMediaCallbacks;
  private outputFormat: CallOutputAudioFormat;
  private inputFormat: CallInputAudioFormat;
  private room: RtcRoom | null = null;
  private lease: CallMediaLease | null = null;
  private transport: CallTransportCredential | null = null;
  private inputReader: RtcAudioReader | null = null;
  private outputSource: RtcAudioSource | null = null;
  private outputTrack: unknown = null;
  private outputTrackSid: string | null = null;
  private readyForBrowser = false;
  private operatorPresent: boolean | null = null;
  private playoutRevision = 0;
  private readonly seenHints = new Set<string>();
  private readonly metricsState: CallMediaMetrics = {
    connects: 0, disconnects: 0, reconnects: 0, rejectedPackets: 0,
    acceptedHints: 0, duplicateHints: 0, inputFrames: 0, outputFrames: 0, bargeIns: 0,
  };

  constructor(options: CallMediaSessionOptions = {}) {
    this.bindings = options.bindings ?? createRtcNodeBindings();
    this.callbacks = options.callbacks ?? {};
    this.outputFormat = options.outputFormat ?? { sampleRate: 24_000, channels: 1 };
    validateOutputFormat(this.outputFormat);
    this.inputFormat = options.inputFormat ?? { sampleRate: 16_000, channels: 1, frameSizeMs: 20 };
    validateInputFormat(this.inputFormat);
  }

  metrics(): CallMediaMetrics { return { ...this.metricsState }; }
  isConnected(): boolean {
    // rtc-node retains the Room object after a terminal disconnect.  Honor its
    // connection getter so the next coordinator reconciliation can rejoin with
    // a fresh token instead of treating a dead same-generation room as healthy.
    // Older test bindings may omit the optional getter.
    return this.room !== null && this.room.isConnected !== false;
  }
  currentPlayoutRevision(): number { return this.playoutRevision; }

  /** Configure TTS PCM before the local output track is published. */
  setOutputFormat(format: CallOutputAudioFormat): void {
    validateOutputFormat(format);
    if (this.outputSource || this.outputTrack) throw new Error('call output format is already published');
    this.outputFormat = { ...format };
  }

  /** Configure RTC resampling before a browser microphone is subscribed. */
  setInputFormat(format: CallInputAudioFormat): void {
    validateInputFormat(format);
    if (this.inputReader || this.room) throw new Error('call input format must be set before connecting media');
    this.inputFormat = { ...format };
  }

  async connect(lease: CallMediaLease, transport: CallTransportCredential): Promise<void> {
    this.validateTransport(lease, transport);
    await this.disconnect();
    this.lease = { ...lease };
    this.transport = { ...transport };
    this.playoutRevision = 0;
    this.readyForBrowser = false;
    this.operatorPresent = null;
    this.seenHints.clear();
    const room = this.bindings.createRoom();
    this.room = room;
    this.registerRoomHandlers(room);
    this.callbacks.onConnectionState?.('connecting');
    try {
      // Registering handlers before connect avoids losing handshake-time events.
      await room.connect(transport.serverUrl, transport.participantToken, { autoSubscribe: false, dynacast: true });
      if (room.name !== undefined && room.name !== transport.room) throw new Error('LiveKit connected to unexpected room');
      this.subscribeExistingBrowserAudio(room);
      this.updateOperatorPresence(room.remoteParticipants.has(transport.operatorIdentity));
      this.metricsState.connects += 1;
      this.callbacks.onConnectionState?.('connected');
    } catch (error) {
      await this.disconnect();
      throw error;
    }
  }

  /** Rejoins only with a newly-issued short-lived credential for this exact lease. */
  async reconnect(lease: CallMediaLease, transport: CallTransportCredential): Promise<void> {
    if (!sameLease(this.lease, lease)) throw new Error('cannot reconnect a different call lease');
    this.metricsState.reconnects += 1;
    await this.connect(lease, transport);
  }

  async disconnect(): Promise<void> {
    const room = this.room;
    this.room = null;
    this.lease = null;
    this.transport = null;
    this.operatorPresent = null;
    this.seenHints.clear();
    if (this.inputReader) {
      try { await this.inputReader.cancel(); } catch { /* best-effort local cleanup */ }
      this.inputReader = null;
    }
    if (this.outputSource) {
      try { this.outputSource.clearQueue(); } catch { /* best-effort local cleanup */ }
      try { await this.outputSource.close(); } catch { /* best-effort local cleanup */ }
      this.outputSource = null;
    }
    this.outputTrack = null;
    this.outputTrackSid = null;
    this.readyForBrowser = false;
    if (room) {
      try { await room.disconnect(); } finally { this.metricsState.disconnects += 1; }
    }
    this.callbacks.onConnectionState?.('disconnected');
  }

  /** Wait for exactly the authorised browser microphone's next audio frame. */
  async readInputFrame(): Promise<CallAudioFrame | null> {
    const reader = this.inputReader;
    const room = this.room;
    const lease = this.lease;
    if (!reader || !room || !lease) return null;
    const result = await reader.read();
    if (result.done || !result.value || this.room !== room || this.lease !== lease) return null;
    const frame = result.value as AudioFrame;
    if (!(frame.data instanceof Int16Array) || !Number.isSafeInteger(frame.sampleRate)
      || !Number.isSafeInteger(frame.channels) || !Number.isSafeInteger(frame.samplesPerChannel)) {
      this.metricsState.rejectedPackets += 1;
      return null;
    }
    this.metricsState.inputFrames += 1;
    return {
      data: frame.data,
      sampleRate: frame.sampleRate,
      channels: frame.channels,
      samplesPerChannel: frame.samplesPerChannel,
    };
  }

  /** Publish a PCM frame to the coordinator's microphone track.  No TTS lives here. */
  async writeOutputFrame(frame: CallAudioFrame): Promise<void> {
    if (!this.room || !this.lease) throw new Error('call media is not connected');
    validateAudioFrame(frame);
    if (frame.sampleRate !== this.outputFormat.sampleRate || frame.channels !== this.outputFormat.channels) {
      throw new Error('audio frame format does not match the published call output track');
    }
    await this.ensureOutputTrack();
    const source = this.outputSource;
    if (!source) throw new Error('LiveKit output source is unavailable');
    await source.captureFrame(this.bindings.createAudioFrame(frame));
    this.metricsState.outputFrames += 1;
  }

  async publishDisplay(packet: CallDisplayPacket): Promise<void> {
    this.requireCurrentPacket(packet.callId, packet.generation);
    const payload = {
      v: CALL_MEDIA_PROTOCOL_VERSION,
      type: 'blueprint_call_display_blocks',
      call_id: packet.callId,
      generation: packet.generation,
      packet_id: requireText(packet.packetId, 'packet_id', 256),
      ...(packet.text ? { text: requireText(packet.text, 'text') } : {}),
      display_blocks: validateDisplayBlocks(packet.displayBlocks),
    };
    await this.publishTrusted(CALL_MEDIA_TOPICS.display, payload);
  }

  async publishTransientTranscript(transcript: CallTransientTranscript): Promise<void> {
    this.requireCurrentPacket(transcript.callId, transcript.generation);
    await this.publishTrusted(CALL_MEDIA_TOPICS.transcript, {
      v: CALL_MEDIA_PROTOCOL_VERSION,
      type: 'blueprint_call_operator_caption',
      call_id: transcript.callId,
      generation: transcript.generation,
      segment_id: requireText(transcript.segmentId, 'transcript segment_id', 256),
      text: requireText(transcript.text, 'transcript text', 4_000),
      final: transcript.final,
    });
  }

  /**
   * Report the Coordinator-side TTS source lifecycle. This is deliberately not
   * named or persisted as browser playout: provider audio production does not
   * prove that the remote browser rendered it audibly.
   */
  async publishTtsSourceLifecycle(packet: CallTtsSourcePacket): Promise<void> {
    this.requireCurrentPacket(packet.callId, packet.generation);
    const payload = {
      v: CALL_MEDIA_PROTOCOL_VERSION,
      type: 'blueprint_call_tts_source',
      call_id: packet.callId,
      generation: packet.generation,
      phrase_id: requireText(packet.phraseId, 'phrase_id', 256),
      event: packet.event,
    };
    await this.publishTrusted(CALL_MEDIA_TOPICS.ttsSource, payload);
  }

  /**
   * The browser transport being connected is not proof that the coordinator
   * can hear, think, and speak.  Publish this only after the restricted Fast
   * process, STT/TTS sessions, and this exact RTC lease are all usable.
   * It is destination-scoped and generation-fenced like every other semantic
   * coordinator packet, so a browser participant cannot forge readiness.
   */
  async publishReady(): Promise<void> {
    const lease = this.lease;
    if (!lease) throw new Error('call media is not connected');
    this.readyForBrowser = true;
    await this.replayReady();
  }

  /** Reliable data is not replayed by LiveKit, so resend after an authorised
   * browser reconnect/join.  It remains an exact participant + lease fence. */
  private async replayReady(): Promise<void> {
    const lease = this.lease;
    if (!lease || !this.readyForBrowser) return;
    await this.publishTrusted(CALL_MEDIA_TOPICS.readiness, {
      v: CALL_MEDIA_PROTOCOL_VERSION,
      type: 'blueprint_call_ready',
      call_id: lease.callId,
      generation: lease.generation,
    });
  }

  /**
   * Publish a resident caption through LiveKit's native transcription channel.
   * The browser already renders this as `RoomEvent.TranscriptionReceived`; it
   * is not duplicated as a lossy/unauthenticated data packet.
   */
  async publishCaption(caption: CallCaption): Promise<void> {
    this.requireCurrentPacket(caption.callId, caption.generation);
    await this.ensureOutputTrack();
    const localParticipant = this.room?.localParticipant;
    const transport = this.transport;
    const trackSid = this.outputTrackSid;
    if (!localParticipant || !transport || !trackSid) throw new Error('LiveKit caption track is unavailable');
    const now = Date.now();
    const startTimeMs = caption.startTimeMs ?? now;
    const endTimeMs = caption.endTimeMs ?? startTimeMs;
    if (!Number.isSafeInteger(startTimeMs) || !Number.isSafeInteger(endTimeMs) || endTimeMs < startTimeMs) {
      throw new Error('invalid caption timing');
    }
    await localParticipant.publishTranscription({
      participantIdentity: transport.participantIdentity,
      trackSid,
      segments: [{
        id: requireText(caption.segmentId, 'caption segment_id', 256),
        text: requireText(caption.text, 'caption text', 4_000),
        startTime: BigInt(startTimeMs),
        endTime: BigInt(endTimeMs),
        language: 'en',
        final: caption.final,
      }],
    });
  }

  /** Cancel queued local audio and advance the barge-in fencing epoch. */
  cancelPlayback(): Promise<number> {
    if (!this.lease) throw new Error('call media is not connected');
    this.playoutRevision += 1;
    this.outputSource?.clearQueue();
    return Promise.resolve(this.playoutRevision);
  }

  private registerRoomHandlers(room: RtcRoom): void {
    room.on(this.bindings.events.trackPublished, (...args) => {
      const [publication, participant] = args as [RtcRemoteTrackPublication, RtcRemoteParticipant];
      this.subscribeBrowserAudio(publication, participant);
    });
    room.on(this.bindings.events.trackSubscribed, (...args) => {
      const [track, , participant] = args as [unknown, RtcRemoteTrackPublication, RtcRemoteParticipant];
      if (this.isAuthorisedBrowser(participant)) this.attachInputTrack(track);
    });
    room.on(this.bindings.events.dataReceived, (...args) => {
      const [payload, participant, , topic] = args as [Uint8Array, RtcRemoteParticipant | undefined, unknown, string | undefined];
      this.handleBrowserData(payload, participant, topic);
    });
    room.on(this.bindings.events.participantConnected, (...args) => {
      const [participant] = args as [RtcRemoteParticipant];
      if (this.isAuthorisedBrowser(participant)) {
        this.updateOperatorPresence(true);
        void this.replayReady().catch(() => undefined);
      }
    });
    room.on(this.bindings.events.participantDisconnected, (...args) => {
      const [participant] = args as [RtcRemoteParticipant];
      if (this.isAuthorisedBrowser(participant)) this.updateOperatorPresence(false);
    });
    room.on(this.bindings.events.reconnecting, () => this.callbacks.onConnectionState?.('reconnecting'));
    room.on(this.bindings.events.reconnected, () => {
      this.metricsState.reconnects += 1;
      this.subscribeExistingBrowserAudio(room);
      const operatorIdentity = this.transport?.operatorIdentity;
      if (operatorIdentity) this.updateOperatorPresence(room.remoteParticipants.has(operatorIdentity));
      void this.replayReady().catch(() => undefined);
      this.callbacks.onConnectionState?.('connected');
    });
    room.on(this.bindings.events.disconnected, () => this.callbacks.onConnectionState?.('disconnected'));
  }

  private subscribeExistingBrowserAudio(room: RtcRoom): void {
    for (const participant of room.remoteParticipants.values()) {
      for (const publication of participant.trackPublications.values()) this.subscribeBrowserAudio(publication, participant);
    }
  }

  private subscribeBrowserAudio(publication: RtcRemoteTrackPublication, participant: RtcRemoteParticipant): void {
    if (!this.isAuthorisedBrowser(participant) || !this.bindings.isAudioKind(publication.kind)) return;
    publication.setSubscribed(true);
    if (publication.track) this.attachInputTrack(publication.track);
  }

  private attachInputTrack(track: unknown): void {
    // A browser may republish its microphone after reconnecting.  The current
    // reader is then stale and must never feed audio from the prior track.
    void this.inputReader?.cancel();
    this.inputReader = this.bindings.createAudioStream(track, {
      sampleRate: this.inputFormat.sampleRate,
      numChannels: this.inputFormat.channels,
      frameSizeMs: this.inputFormat.frameSizeMs,
    }).getReader();
  }

  private handleBrowserData(payload: Uint8Array, participant: RtcRemoteParticipant | undefined, topic: string | undefined): void {
    if (!this.isAuthorisedBrowser(participant) || !topic || payload.byteLength > CALL_MEDIA_MAX_PACKET_BYTES) {
      this.metricsState.rejectedPackets += 1;
      return;
    }
    let value: Record<string, unknown>;
    try {
      const decoded = new TextDecoder().decode(payload);
      const parsed: unknown = JSON.parse(decoded);
      if (!isRecord(parsed)) throw new Error('data packet must be an object');
      value = parsed;
    } catch {
      this.metricsState.rejectedPackets += 1;
      return;
    }
    if (topic === CALL_MEDIA_TOPICS.committedMessage) this.handleCommittedHint(value);
    else if (topic === CALL_MEDIA_TOPICS.bargeIn) this.handleBargeIn(value);
    else this.metricsState.rejectedPackets += 1;
  }

  private handleCommittedHint(value: Record<string, unknown>): void {
    try {
      if (value.type !== 'blueprint_message_committed') throw new Error('invalid wake type');
      const lease = this.requirePayloadLease(value);
      const hint: CallCommittedMessageHint = {
        callId: lease.callId,
        generation: lease.generation,
        messageId: requireText(value.message_id, 'message_id', 256),
        sequence: requireInteger(value.sequence, 'sequence', 0),
      };
      const id = `${hint.messageId}:${hint.sequence}`;
      if (this.seenHints.has(id)) {
        this.metricsState.duplicateHints += 1;
        return;
      }
      this.seenHints.add(id);
      trimSeen(this.seenHints);
      this.metricsState.acceptedHints += 1;
      this.callbacks.onCommittedMessageHint?.(hint);
    } catch {
      this.metricsState.rejectedPackets += 1;
    }
  }

  private handleBargeIn(value: Record<string, unknown>): void {
    try {
      if (value.type !== 'blueprint_call_barge_in') throw new Error('invalid barge-in type');
      const lease = this.requirePayloadLease(value);
      const event: CallBargeIn = {
        callId: lease.callId,
        generation: lease.generation,
        playoutRevision: requireInteger(value.playout_revision, 'playout_revision', 0),
        playedPrefixChars: requireInteger(value.played_prefix_chars, 'played_prefix_chars', 0),
      };
      this.playoutRevision = Math.max(this.playoutRevision, event.playoutRevision + 1);
      this.outputSource?.clearQueue();
      this.metricsState.bargeIns += 1;
      this.callbacks.onBargeIn?.(event);
    } catch {
      this.metricsState.rejectedPackets += 1;
    }
  }

  private requirePayloadLease(value: Record<string, unknown>): CallMediaLease {
    if (value.v !== CALL_MEDIA_PROTOCOL_VERSION || !this.lease) throw new Error('invalid media protocol or inactive call');
    const callId = requireText(value.call_id, 'call_id', 256);
    const generation = requireInteger(value.generation, 'generation', 1);
    if (callId !== this.lease.callId || generation !== this.lease.generation) throw new Error('stale call payload');
    return this.lease;
  }

  private isAuthorisedBrowser(participant: RtcRemoteParticipant | undefined): boolean {
    return participant !== undefined && this.transport !== null && participant.identity === this.transport.operatorIdentity;
  }

  private updateOperatorPresence(present: boolean): void {
    if (this.operatorPresent === present) return;
    this.operatorPresent = present;
    this.callbacks.onOperatorPresenceChanged?.(present);
  }

  private async ensureOutputTrack(): Promise<void> {
    if (this.outputSource && this.outputTrack) return;
    const room = this.room;
    const localParticipant = room?.localParticipant;
    if (!room || !localParticipant || !this.lease) throw new Error('LiveKit local participant is unavailable');
    const source = this.bindings.createAudioSource(this.outputFormat.sampleRate, this.outputFormat.channels);
    const track = this.bindings.createLocalAudioTrack(`bp-call-${this.lease.callId}`, source);
    const options = this.bindings.createTrackPublishOptions();
    this.bindings.setMicrophoneSource(options);
    const publication = await localParticipant.publishTrack(track, options);
    if (!publication.sid) throw new Error('LiveKit did not assign an output track SID');
    this.outputSource = source;
    this.outputTrack = track;
    this.outputTrackSid = publication.sid;
  }

  private async publishTrusted(topic: string, payload: Record<string, unknown>): Promise<void> {
    const room = this.room;
    const transport = this.transport;
    if (!room?.localParticipant || !transport) throw new Error('LiveKit local participant is unavailable');
    if (encodedSize(payload) > CALL_MEDIA_MAX_PACKET_BYTES) throw new Error('call media packet exceeds size limit');
    await room.localParticipant.publishData(new TextEncoder().encode(JSON.stringify(payload)), {
      reliable: true,
      destination_identities: [transport.operatorIdentity],
      topic,
    });
  }

  private requireCurrentPacket(callId: string, generation: number): void {
    if (!this.lease || this.lease.callId !== callId || this.lease.generation !== generation) {
      throw new Error('stale call output packet');
    }
  }

  private validateTransport(lease: CallMediaLease, transport: CallTransportCredential): void {
    requireText(lease.callId, 'call_id', 256);
    requireInteger(lease.generation, 'call_generation', 1);
    if (!/^wss:\/\//i.test(transport.serverUrl) && !/^https:\/\//i.test(transport.serverUrl)) throw new Error('invalid LiveKit URL');
    requireText(transport.participantToken, 'participant_token', 8_192);
    requireText(transport.room, 'room', 512);
    requireText(transport.participantIdentity, 'participant_identity', 512);
    requireText(transport.operatorIdentity, 'operator_identity', 512);
    if (transport.participantIdentity === transport.operatorIdentity) throw new Error('coordinator and browser identities must differ');
  }
}

function sameLease(left: CallMediaLease | null, right: CallMediaLease): boolean {
  return left !== null && left.callId === right.callId && left.generation === right.generation;
}

function validateAudioFrame(frame: CallAudioFrame): void {
  if (!(frame.data instanceof Int16Array) || !Number.isSafeInteger(frame.sampleRate) || frame.sampleRate < 8_000
    || !Number.isSafeInteger(frame.channels) || frame.channels < 1 || frame.channels > 2
    || !Number.isSafeInteger(frame.samplesPerChannel) || frame.samplesPerChannel < 1
    || frame.data.length !== frame.samplesPerChannel * frame.channels) {
    throw new Error('invalid audio frame');
  }
}

function validateOutputFormat(format: CallOutputAudioFormat): void {
  if (!Number.isSafeInteger(format.sampleRate) || format.sampleRate < 8_000 || format.sampleRate > 48_000
    || !Number.isSafeInteger(format.channels) || format.channels < 1 || format.channels > 2) {
    throw new Error('invalid call output audio format');
  }
}

function validateInputFormat(format: CallInputAudioFormat): void {
  if (!Number.isSafeInteger(format.sampleRate) || format.sampleRate < 8_000 || format.sampleRate > 48_000
    || !Number.isSafeInteger(format.channels) || format.channels < 1 || format.channels > 2
    || !Number.isSafeInteger(format.frameSizeMs) || format.frameSizeMs < 10 || format.frameSizeMs > 80) {
    throw new Error('invalid call input audio format');
  }
}

function validateDisplayBlocks(value: CallDisplayPacket['displayBlocks']): Array<{ type: string; content: string }> {
  if (!value || value.length > 8) throw new Error('invalid display blocks');
  return value.map((block) => ({
    type: block.type,
    content: requireText(block.content, 'display block', 8_000),
  }));
}
