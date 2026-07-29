import { describe, expect, it } from 'vitest';
import {
  CALL_MEDIA_TOPICS,
  CallMediaSession,
  type CallAudioFrame,
  type RtcAudioReader,
  type RtcNodeBindings,
  type RtcRemoteParticipant,
  type RtcRemoteTrackPublication,
  type RtcRoom,
} from './livekit-call-media.js';

type Handler = (...args: unknown[]) => void;
type FakeTranscription = {
  participantIdentity: string;
  trackSid: string;
  segments: Array<{ id: string; text: string; startTime: bigint; endTime: bigint; language: string; final: boolean }>;
};

class FakeReader implements RtcAudioReader {
  constructor(private readonly frames: unknown[]) {}
  cancelled = false;

  async read(): Promise<{ done: boolean; value?: unknown }> {
    const value = this.frames.shift();
    return value === undefined ? { done: true } : { done: false, value };
  }

  async cancel(): Promise<void> { this.cancelled = true; }
}

class FakeAudioSource {
  captured: unknown[] = [];
  cleared = 0;
  closed = false;
  async captureFrame(frame: unknown): Promise<void> { this.captured.push(frame); }
  clearQueue(): void { this.cleared += 1; }
  async close(): Promise<void> { this.closed = true; }
}

class FakeRoom implements RtcRoom {
  readonly remoteParticipants = new Map<string, RtcRemoteParticipant>();
  readonly handlers = new Map<unknown, Handler[]>();
  readonly publishedData: Array<{ data: Uint8Array; options: { reliable?: boolean; destination_identities?: string[]; topic?: string } }> = [];
  readonly publishedTracks: unknown[] = [];
  readonly transcriptions: FakeTranscription[] = [];
  localParticipant = {
    publishData: async (data: Uint8Array, options: { reliable?: boolean; destination_identities?: string[]; topic?: string }) => {
      this.publishedData.push({ data, options });
    },
    publishTrack: async (track: unknown) => { this.publishedTracks.push(track); return { sid: 'TR_output' }; },
    publishTranscription: async (transcription: FakeTranscription) => {
      this.transcriptions.push(transcription);
    },
  };
  name = 'bp-call-call-a-3';
  connectCalls: Array<{ url: string; token: string }> = [];
  disconnected = false;
  get isConnected(): boolean { return this.connectCalls.length > 0 && !this.disconnected; }

  on(event: unknown, handler: Handler): void {
    this.handlers.set(event, [...(this.handlers.get(event) ?? []), handler]);
  }
  async connect(url: string, token: string): Promise<void> {
    this.disconnected = false;
    this.connectCalls.push({ url, token });
  }
  async disconnect(): Promise<void> { this.disconnected = true; }
  emit(event: unknown, ...args: unknown[]): void {
    if (event === 'disconnected') this.disconnected = true;
    for (const handler of this.handlers.get(event) ?? []) handler(...args);
  }
}

function fixture() {
  const rooms: FakeRoom[] = [];
  const readers: FakeReader[] = [];
  const sources: FakeAudioSource[] = [];
  const sourceFormats: Array<{ sampleRate: number; channels: number }> = [];
  const inputFormats: Array<{ sampleRate: number; numChannels: number; frameSizeMs: number }> = [];
  const bindings: RtcNodeBindings = {
    createRoom: () => {
      const room = new FakeRoom();
      rooms.push(room);
      return room;
    },
    createAudioStream: (_track, options) => {
      inputFormats.push(options);
      const reader = new FakeReader([{ data: new Int16Array([1, 2]), sampleRate: 16_000, channels: 1, samplesPerChannel: 2 }]);
      readers.push(reader);
      return { getReader: () => reader };
    },
    createAudioSource: (sampleRate, channels) => {
      sourceFormats.push({ sampleRate, channels });
      const source = new FakeAudioSource();
      sources.push(source);
      return source;
    },
    createLocalAudioTrack: (_name, source) => ({ source }),
    createAudioFrame: (frame) => frame,
    createTrackPublishOptions: () => ({ source: undefined }),
    setMicrophoneSource: (options) => { (options as { source?: string }).source = 'microphone'; },
    isAudioKind: (kind) => kind === 'audio',
    events: {
      trackPublished: 'trackPublished', trackSubscribed: 'trackSubscribed', dataReceived: 'dataReceived',
      participantConnected: 'participantConnected', participantDisconnected: 'participantDisconnected',
      reconnecting: 'reconnecting', reconnected: 'reconnected', disconnected: 'disconnected',
    },
  };
  const lease = { callId: 'call-a', generation: 3 };
  const transport = {
    serverUrl: 'wss://livekit.example.test', participantToken: 'not-logged-token', room: 'bp-call-call-a-3',
    participantIdentity: 'coordinator-identity', operatorIdentity: 'browser-identity',
  };
  return { bindings, rooms, readers, sources, sourceFormats, inputFormats, lease, transport };
}

function participant(identity: string): RtcRemoteParticipant {
  return { identity, trackPublications: new Map() };
}

function data(value: unknown): Uint8Array { return new TextEncoder().encode(JSON.stringify(value)); }

describe('CallMediaSession', () => {
  it('allows playback cancellation before call media connects', async () => {
    const f = fixture();
    const session = new CallMediaSession({ bindings: f.bindings });

    await expect(session.cancelPlayback()).resolves.toBe(1);
    expect(f.sources).toHaveLength(0);
  });

  it('accepts only committed-message ID wake hints from the exact browser and active generation', async () => {
    const f = fixture();
    const hints: unknown[] = [];
    const session = new CallMediaSession({ bindings: f.bindings, callbacks: { onCommittedMessageHint: (hint) => hints.push(hint) } });
    await session.connect(f.lease, f.transport);
    const room = f.rooms[0]!;
    const browser = participant('browser-identity');
    const forged = participant('other-browser');
    const base = { v: 1, type: 'blueprint_message_committed', call_id: 'call-a', generation: 3, message_id: 'msg-1', sequence: 9 };
    room.emit('dataReceived', data(base), forged, undefined, CALL_MEDIA_TOPICS.committedMessage);
    room.emit('dataReceived', data({ ...base, generation: 2 }), browser, undefined, CALL_MEDIA_TOPICS.committedMessage);
    room.emit('dataReceived', data({ ...base, text: 'untrusted content is ignored by this protocol' }), browser, undefined, CALL_MEDIA_TOPICS.committedMessage);
    expect(hints).toEqual([{ callId: 'call-a', generation: 3, messageId: 'msg-1', sequence: 9 }]);
    expect(session.metrics()).toMatchObject({ acceptedHints: 1, rejectedPackets: 2 });
  });

  it('deduplicates browser wake hints and does not subscribe to other participants audio', async () => {
    const f = fixture();
    const hints: unknown[] = [];
    const session = new CallMediaSession({ bindings: f.bindings, callbacks: { onCommittedMessageHint: (hint) => hints.push(hint) } });
    await session.connect(f.lease, f.transport);
    const room = f.rooms[0]!;
    const browser = participant('browser-identity');
    const intruder = participant('intruder');
    let browserSubscribed = 0;
    let intruderSubscribed = 0;
    const browserPublication: RtcRemoteTrackPublication = { kind: 'audio', track: {}, setSubscribed: () => { browserSubscribed += 1; } };
    const intruderPublication: RtcRemoteTrackPublication = { kind: 'audio', track: {}, setSubscribed: () => { intruderSubscribed += 1; } };
    room.emit('trackPublished', browserPublication, browser);
    room.emit('trackPublished', intruderPublication, intruder);
    expect(browserSubscribed).toBe(1);
    expect(intruderSubscribed).toBe(0);
    const hint = { v: 1, type: 'blueprint_message_committed', call_id: 'call-a', generation: 3, message_id: 'msg-1', sequence: 2 };
    room.emit('dataReceived', data(hint), browser, undefined, CALL_MEDIA_TOPICS.committedMessage);
    room.emit('dataReceived', data(hint), browser, undefined, CALL_MEDIA_TOPICS.committedMessage);
    expect(hints).toHaveLength(1);
    expect(session.metrics().duplicateHints).toBe(1);
    expect(await session.readInputFrame()).toMatchObject({ sampleRate: 16_000, channels: 1, samplesPerChannel: 2 });
    expect(f.inputFormats).toEqual([{ sampleRate: 16_000, numChannels: 1, frameSizeMs: 20 }]);
  });

  it('fences output packets, bounds them, and cancels queued audio on a browser barge-in', async () => {
    const f = fixture();
    const barges: unknown[] = [];
    const session = new CallMediaSession({ bindings: f.bindings, callbacks: { onBargeIn: (event) => barges.push(event) } });
    await session.connect(f.lease, f.transport);
    const frame: CallAudioFrame = { data: new Int16Array([1, 2]), sampleRate: 24_000, channels: 1, samplesPerChannel: 2 };
    await session.writeOutputFrame(frame);
    await session.publishDisplay({ callId: 'call-a', generation: 3, packetId: 'display-1', text: 'Let x equal y.', displayBlocks: [{ type: 'latex', content: 'x=y' }] });
    const room = f.rooms[0]!;
    expect(room.publishedData[0]!.options).toEqual({ reliable: true, destination_identities: ['browser-identity'], topic: CALL_MEDIA_TOPICS.display });
    expect(JSON.parse(new TextDecoder().decode(room.publishedData[0]!.data))).toMatchObject({
      v: 1, type: 'blueprint_call_display_blocks', call_id: 'call-a', generation: 3,
      display_blocks: [{ type: 'latex', content: 'x=y' }],
    });
    await session.publishTtsSourceLifecycle({
      callId: 'call-a', generation: 3, phraseId: 'phrase-1', event: 'started',
    });
    expect(room.publishedData[1]!.options).toEqual({
      reliable: true,
      destination_identities: ['browser-identity'],
      topic: CALL_MEDIA_TOPICS.ttsSource,
    });
    expect(JSON.parse(new TextDecoder().decode(room.publishedData[1]!.data))).toMatchObject({
      v: 1, type: 'blueprint_call_tts_source', call_id: 'call-a', generation: 3,
      phrase_id: 'phrase-1', event: 'started',
    });
    await expect(session.publishDisplay({ callId: 'call-a', generation: 2, packetId: 'stale', text: 'no', displayBlocks: [{ type: 'code', content: 'no' }] })).rejects.toThrow('stale');
    await expect(session.publishDisplay({ callId: 'call-a', generation: 3, packetId: 'large', text: 'x'.repeat(13_000), displayBlocks: [{ type: 'code', content: 'ok' }] })).rejects.toThrow('invalid text');
    const browser = participant('browser-identity');
    room.emit('dataReceived', data({ v: 1, type: 'blueprint_call_barge_in', call_id: 'call-a', generation: 3, playout_revision: 0, played_prefix_chars: 5 }), browser, undefined, CALL_MEDIA_TOPICS.bargeIn);
    expect(barges).toEqual([{ callId: 'call-a', generation: 3, playoutRevision: 0, playedPrefixChars: 5 }]);
    expect(f.sources[0]!.cleared).toBe(1);
    await session.cancelPlayback();
    expect(f.sources[0]!.cleared).toBe(2);
    expect(room.publishedData).toHaveLength(2);
    await session.publishCaption({ callId: 'call-a', generation: 3, segmentId: 'caption-1', text: 'The argument is local.', final: true, startTimeMs: 3, endTimeMs: 7 });
    expect(room.transcriptions).toEqual([{
      participantIdentity: 'coordinator-identity', trackSid: 'TR_output',
      segments: [{ id: 'caption-1', text: 'The argument is local.', startTime: 3n, endTime: 7n, language: 'en', final: true }],
    }]);
  });

  it('disconnects old RTC resources before a credential refresh and after call end', async () => {
    const f = fixture();
    const session = new CallMediaSession({ bindings: f.bindings });
    session.setOutputFormat({ sampleRate: 24_000, channels: 1 });
    await session.connect(f.lease, f.transport);
    await session.writeOutputFrame({
      data: new Int16Array([1, 2]),
      sampleRate: 24_000,
      channels: 1,
      samplesPerChannel: 2,
    });
    const firstRoom = f.rooms[0]!;
    await session.reconnect(f.lease, { ...f.transport, participantToken: 'new-short-lived-token' });
    expect(firstRoom.disconnected).toBe(true);
    expect(f.sources[0]!.closed).toBe(true);
    // Generation B may establish its provider formats only after A's published
    // output source/track were fully removed.
    expect(() => session.setOutputFormat({ sampleRate: 24_000, channels: 1 })).not.toThrow();
    expect(f.rooms).toHaveLength(2);
    await session.disconnect();
    expect(f.rooms[1]!.disconnected).toBe(true);
    expect(session.isConnected()).toBe(false);
    expect(session.metrics()).toMatchObject({ connects: 2, reconnects: 1, disconnects: 2 });
  });

  it('fully removes a published old-generation output track before configuring the replacement', async () => {
    const f = fixture();
    const session = new CallMediaSession({ bindings: f.bindings });
    session.setOutputFormat({ sampleRate: 24_000, channels: 1 });
    await session.connect(f.lease, f.transport);
    await session.writeOutputFrame({
      data: new Int16Array([1, 2]),
      sampleRate: 24_000,
      channels: 1,
      samplesPerChannel: 2,
    });
    await session.disconnect();
    expect(f.sources[0]!.closed).toBe(true);
    expect(() => session.setOutputFormat({ sampleRate: 24_000, channels: 1 })).not.toThrow();
    await session.connect(
      { callId: 'call-a', generation: 4 },
      {
        ...f.transport,
        participantToken: 'generation-b-token',
      },
    );
    expect(f.rooms).toHaveLength(2);
  });

  it('does not report a retained room as connected after a terminal RTC disconnect', async () => {
    const f = fixture();
    const session = new CallMediaSession({ bindings: f.bindings });
    await session.connect(f.lease, f.transport);
    expect(session.isConnected()).toBe(true);

    f.rooms[0]!.emit('disconnected');

    expect(session.isConnected()).toBe(false);
  });

  it('reports only the authorised operator presence, including initial absence and reconnect', async () => {
    const f = fixture();
    const presence: boolean[] = [];
    const session = new CallMediaSession({
      bindings: f.bindings,
      callbacks: { onOperatorPresenceChanged: value => presence.push(value) },
    });
    await session.connect(f.lease, f.transport);
    const room = f.rooms[0]!;
    expect(presence).toEqual([false]);
    room.emit('participantConnected', participant('intruder'));
    expect(presence).toEqual([false]);
    const browser = participant('browser-identity');
    room.remoteParticipants.set(browser.identity, browser);
    room.emit('participantConnected', browser);
    expect(presence).toEqual([false, true]);
    room.remoteParticipants.delete(browser.identity);
    room.emit('participantDisconnected', browser);
    expect(presence).toEqual([false, true, false]);
  });

  it('publishes a non-default configured output sample rate and rejects mismatched PCM', async () => {
    const f = fixture();
    const session = new CallMediaSession({
      bindings: f.bindings,
      outputFormat: { sampleRate: 16_000, channels: 1 },
    });
    await session.connect(f.lease, f.transport);
    await session.writeOutputFrame({
      data: new Int16Array([1, 2]), sampleRate: 16_000, channels: 1, samplesPerChannel: 2,
    });
    expect(f.sourceFormats).toEqual([{ sampleRate: 16_000, channels: 1 }]);
    await expect(session.writeOutputFrame({
      data: new Int16Array([1, 2]), sampleRate: 24_000, channels: 1, samplesPerChannel: 2,
    })).rejects.toThrow('does not match');
  });

  it('fences bounded operator speech captions to the exact active lease', async () => {
    const f = fixture();
    const session = new CallMediaSession({ bindings: f.bindings });
    await session.connect(f.lease, f.transport);
    await session.publishTransientTranscript({
      callId: 'call-a', generation: 3, segmentId: 'turn-1', text: 'Let epsilon be small.', final: false,
    });
    const room = f.rooms[0]!;
    expect(room.publishedData[0]!.options).toMatchObject({
      destination_identities: ['browser-identity'], topic: CALL_MEDIA_TOPICS.transcript,
    });
    expect(JSON.parse(new TextDecoder().decode(room.publishedData[0]!.data))).toMatchObject({
      type: 'blueprint_call_operator_caption', final: false,
    });
    await expect(session.publishTransientTranscript({
      callId: 'forged', generation: 3, segmentId: 'turn-2', text: 'no', final: false,
    })).rejects.toThrow('stale');
    await expect(session.publishTransientTranscript({
      callId: 'call-a', generation: 3, segmentId: 'turn-3', text: 'x'.repeat(4_001), final: false,
    })).rejects.toThrow('invalid transcript text');
  });

  it('announces readiness only after joining the exact issued coordinator lease', async () => {
    const f = fixture();
    const session = new CallMediaSession({ bindings: f.bindings });
    await expect(session.publishReady()).rejects.toThrow('not connected');
    await session.connect(f.lease, f.transport);
    await session.publishReady();
    const room = f.rooms[0]!;
    expect(room.publishedData[0]!.options).toMatchObject({
      destination_identities: ['browser-identity'], topic: CALL_MEDIA_TOPICS.readiness,
    });
    expect(JSON.parse(new TextDecoder().decode(room.publishedData[0]!.data))).toEqual({
      v: 1, type: 'blueprint_call_ready', call_id: 'call-a', generation: 3,
    });
    await session.disconnect();
    await expect(session.publishReady()).rejects.toThrow('not connected');
  });

  it('replays readiness for an authorised browser that joins after coordinator startup', async () => {
    const f = fixture();
    const session = new CallMediaSession({ bindings: f.bindings });
    await session.connect(f.lease, f.transport);
    await session.publishReady();
    const room = f.rooms[0]!;
    room.emit('participantConnected', participant('intruder'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(room.publishedData).toHaveLength(1);
    room.emit('participantConnected', participant('browser-identity'));
    await new Promise(resolve => setTimeout(resolve, 0));
    expect(room.publishedData).toHaveLength(2);
    expect(room.publishedData[1]!.options).toMatchObject({
      destination_identities: ['browser-identity'], topic: CALL_MEDIA_TOPICS.readiness,
    });
  });

  it('normalizes browser input to a configured Deepgram-compatible format', async () => {
    const f = fixture();
    const session = new CallMediaSession({
      bindings: f.bindings,
      inputFormat: { sampleRate: 16_000, channels: 1, frameSizeMs: 40 },
    });
    await session.connect(f.lease, f.transport);
    const browser = participant('browser-identity');
    const publication: RtcRemoteTrackPublication = { kind: 'audio', track: {}, setSubscribed: () => undefined };
    f.rooms[0]!.emit('trackPublished', publication, browser);
    expect(f.inputFormats).toEqual([{ sampleRate: 16_000, numChannels: 1, frameSizeMs: 40 }]);
    expect(await session.readInputFrame()).toMatchObject({ sampleRate: 16_000, channels: 1 });
  });
});
