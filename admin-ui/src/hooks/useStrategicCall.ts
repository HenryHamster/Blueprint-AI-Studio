import { useCallback, useEffect, useRef, useState } from 'react';
import { RemoteAudioTrack, Room, RoomEvent, Track } from 'livekit-client';
import {
  endStrategicCall,
  fetchStrategicCall,
  reportStrategicCallPlayout,
  startStrategicCall,
  type StrategicCallResponse,
  type StrategicCallState,
} from '../strategicStateApi';
import { BrowserPlayoutTracker, monitorBrowserAudio } from './browserPlayoutTracker';

export interface LiveCaption {
  id: string;
  text: string;
  final: boolean;
  speaker: 'operator' | 'resident';
}

export interface CallDisplayBlock {
  id: string;
  type: 'latex' | 'code' | 'table' | 'artifact_ref';
  content: string;
  title?: string;
}

export type StrategicCallConnectionState = 'idle' | 'starting' | 'connecting' | 'waiting_for_resident' | 'connected' | 'reconnecting' | 'error';

/** A media socket alone is not a useful call. Keep this bounded and explicit. */
export const CALL_COORDINATOR_READY_TIMEOUT_MS = 20_000;

interface CallDataPacket {
  v?: unknown;
  type?: unknown;
  call_id?: unknown;
  generation?: unknown;
  phrase_id?: unknown;
  event?: unknown;
  display_blocks?: unknown;
  segment_id?: unknown;
  text?: unknown;
  final?: unknown;
}

/** Room data is untrusted unless it comes from the issued agent identity and is
 * fenced to the active call generation. Browser tokens may publish data too. */
export function isTrustedCallPacket(
  packet: CallDataPacket,
  participantIdentity: string | undefined,
  call: StrategicCallState,
  agentIdentity: string | null | undefined,
): boolean {
  return Boolean(agentIdentity)
    && participantIdentity === agentIdentity
    && packet.v === 1
    && packet.call_id === call.call_id
    && packet.generation === call.generation;
}

/** Native LiveKit transcription is scoped to the active room/token; the UI
 * still admits captions only from the coordinator that the backend issued. */
export function isTrustedCallCaption(participantIdentity: string | undefined, agentIdentity: string | null | undefined): boolean {
  return Boolean(agentIdentity) && participantIdentity === agentIdentity;
}

/** The coordinator can announce readiness only from the backend-issued agent identity. */
export function isTrustedCallReadyPacket(
  packet: CallDataPacket,
  participantIdentity: string | undefined,
  call: StrategicCallState,
  agentIdentity: string | null | undefined,
): boolean {
  return isTrustedCallPacket(packet, participantIdentity, call, agentIdentity)
    && packet.type === 'blueprint_call_ready';
}

/**
 * STT partials use the data channel rather than becoming a durable message.
 * Parse them only after the caller applied isTrustedCallPacket: browser
 * participants can publish arbitrary room data under the same topic.
 */
export function asTrustedOperatorCaption(packet: CallDataPacket): LiveCaption | null {
  if (packet.type !== 'blueprint_call_operator_caption'
    || typeof packet.segment_id !== 'string' || !packet.segment_id.trim() || packet.segment_id.length > 256
    || typeof packet.text !== 'string' || !packet.text.trim() || packet.text.length > 4_000
    || typeof packet.final !== 'boolean') {
    return null;
  }
  return {
    // Namespace this separately from native agent captions, whose segment IDs
    // are provider controlled and may otherwise collide in the compact UI map.
    id: `operator:${packet.segment_id}`,
    text: packet.text.trim(),
    final: packet.final,
    speaker: 'operator',
  };
}

/** Complete admission check for the ephemeral operator-STT channel. */
export function parseTrustedOperatorCaption(
  packet: CallDataPacket,
  participantIdentity: string | undefined,
  call: StrategicCallState,
  agentIdentity: string | null | undefined,
): LiveCaption | null {
  if (!isTrustedCallPacket(packet, participantIdentity, call, agentIdentity)) return null;
  return asTrustedOperatorCaption(packet);
}

function connectionFields(response: StrategicCallResponse): { url: string; token: string } | null {
  const url = response.server_url ?? response.room_url ?? response.livekit_url ?? null;
  const token = response.participant_token ?? response.token ?? null;
  return url && token ? { url, token } : null;
}

function asDisplayBlocks(value: unknown): CallDisplayBlock[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((block, index) => {
    if (!block || typeof block !== 'object') return [];
    const candidate = block as Record<string, unknown>;
    const type = candidate.type;
    const content = candidate.content;
    if ((type !== 'latex' && type !== 'code' && type !== 'table' && type !== 'artifact_ref') || typeof content !== 'string') return [];
    return [{
      id: typeof candidate.id === 'string' ? candidate.id : `${type}:${index}:${content.slice(0, 24)}`,
      type,
      content,
      ...(typeof candidate.title === 'string' ? { title: candidate.title } : {}),
    }];
  });
}

/**
 * Browser-only transport state for the existing Workday conversation. Durable
 * messages remain in useConversation; LiveKit carries only media/captions and
 * an ID-only wake hint after that durable send has succeeded.
 */
export function useStrategicCall(projectId: string | null) {
  const roomRef = useRef<Room | null>(null);
  const remoteAudioTrackRef = useRef<RemoteAudioTrack | null>(null);
  const remoteAudioElementRef = useRef<HTMLMediaElement | null>(null);
  const stopAudioMonitorRef = useRef<(() => void) | null>(null);
  const playoutTrackerRef = useRef<BrowserPlayoutTracker | null>(null);
  const readinessTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const callRef = useRef<StrategicCallState | null>(null);
  const [call, setCall] = useState<StrategicCallState | null>(null);
  const [connectionState, setConnectionState] = useState<StrategicCallConnectionState>('idle');
  const [microphoneState, setMicrophoneState] = useState<'off' | 'requesting' | 'on' | 'denied'>('off');
  const [audioPlaybackState, setAudioPlaybackState] = useState<'unknown' | 'enabled' | 'blocked'>('unknown');
  const [captions, setCaptions] = useState<LiveCaption[]>([]);
  const [displayBlocks, setDisplayBlocks] = useState<CallDisplayBlock[]>([]);
  const [error, setError] = useState<string | null>(null);

  const clearReadinessTimeout = useCallback(() => {
    if (readinessTimeoutRef.current !== null) {
      clearTimeout(readinessTimeoutRef.current);
      readinessTimeoutRef.current = null;
    }
  }, []);

  const setCallSnapshot = useCallback((next: StrategicCallState | null) => {
    callRef.current = next;
    setCall(next);
  }, []);

  const detachRemoteAudio = useCallback(() => {
    const track = remoteAudioTrackRef.current;
    const element = remoteAudioElementRef.current;
    remoteAudioTrackRef.current = null;
    remoteAudioElementRef.current = null;
    stopAudioMonitorRef.current?.();
    stopAudioMonitorRef.current = null;
    playoutTrackerRef.current = null;
    if (track && element) {
      try { track.detach(element); } catch { /* best-effort media cleanup */ }
    }
    element?.remove();
  }, []);

  const disconnect = useCallback(() => {
    clearReadinessTimeout();
    detachRemoteAudio();
    const room = roomRef.current;
    roomRef.current = null;
    room?.disconnect();
  }, [clearReadinessTimeout, detachRemoteAudio]);

  const connect = useCallback(async (response: StrategicCallResponse) => {
    const connection = connectionFields(response);
    if (!connection) throw new Error('Call is starting, but media credentials are not ready yet.');
    const currentCall = response.call;
    if (!currentCall) throw new Error('Call is missing its identity.');
    if (!response.agent_identity) throw new Error('Call is missing the trusted agent identity.');
    disconnect();
    const room = new Room({ adaptiveStream: true, dynacast: true });
    roomRef.current = room;
    room.on(RoomEvent.TrackSubscribed, (track, _publication, participant) => {
      if (roomRef.current !== room
        || participant.identity !== response.agent_identity
        || track.kind !== Track.Kind.Audio
        || !(track instanceof RemoteAudioTrack)) return;
      detachRemoteAudio();
      const element = track.attach();
      element.autoplay = true;
      element.setAttribute('aria-hidden', 'true');
      element.dataset.blueprintCallAudio = 'resident';
      element.style.display = 'none';
      document.body.append(element);
      remoteAudioTrackRef.current = track;
      remoteAudioElementRef.current = element;
      const playoutTracker = new BrowserPlayoutTracker(receipt => {
        const active = callRef.current;
        if (!projectId || !active
          || active.call_id !== currentCall.call_id
          || active.generation !== currentCall.generation) return;
        void reportStrategicCallPlayout(projectId, {
          call_id: currentCall.call_id,
          generation: currentCall.generation,
          phrase_id: receipt.phraseId,
          event: receipt.event,
          at: receipt.at,
        }).catch(() => undefined);
      });
      playoutTrackerRef.current = playoutTracker;
      stopAudioMonitorRef.current = monitorBrowserAudio(
        track.mediaStreamTrack,
        (audible, atMs) => playoutTracker.audioLevel(audible, atMs),
      );
      // The element's playback state is the browser-side truth boundary. Do
      // not confuse receipt of an SCTP phrase marker with audible playout.
      element.addEventListener('playing', () => {
        if (roomRef.current === room) setError(null);
      });
    });
    room.on(RoomEvent.TrackUnsubscribed, (track, _publication, participant) => {
      if (participant.identity === response.agent_identity && track === remoteAudioTrackRef.current) {
        detachRemoteAudio();
      }
    });
    room.on(RoomEvent.ConnectionStateChanged, state => {
      if (roomRef.current !== room) return;
      if (state === 'connected') {
        // WebRTC connectivity only proves browser ↔ LiveKit.  Do not claim the
        // call is usable until the exact issued Coordinator confirms Fast +
        // STT/TTS + its RTC participant are ready.
        setConnectionState('waiting_for_resident');
        clearReadinessTimeout();
        readinessTimeoutRef.current = setTimeout(() => {
          if (roomRef.current !== room) return;
          setConnectionState(previous => previous === 'waiting_for_resident' ? 'error' : previous);
          const active = callRef.current;
          if (!projectId || !active) {
            setError('The Resident did not become ready. End the call before continuing by text.');
            return;
          }
          // A call that never reaches trusted coordinator readiness suppresses
          // ordinary Workday delivery. Release that lease rather than telling
          // the operator to keep typing into a conversation with no owner.
          void endStrategicCall(projectId, {
            call_id: active.call_id,
            generation: active.generation,
          }).then(response => {
            if (callRef.current?.call_id !== active.call_id || callRef.current?.generation !== active.generation) return;
            disconnect();
            setMicrophoneState('off');
            setCallSnapshot(response.call);
            setConnectionState('error');
            setError('The Resident did not become ready; the call was ended. Typed conversation is available again.');
          }).catch(() => {
            setError('The Resident did not become ready. End the call before continuing by text.');
          });
        }, CALL_COORDINATOR_READY_TIMEOUT_MS);
      } else if (state === 'reconnecting') {
        clearReadinessTimeout();
        setConnectionState('reconnecting');
      } else if (state === 'disconnected') {
        clearReadinessTimeout();
        setConnectionState('idle');
      }
      else setConnectionState('connecting');
    });
    room.on(RoomEvent.AudioPlaybackStatusChanged, () => {
      if (roomRef.current !== room) return;
      // `canPlaybackAudio` reflects browser autoplay policy; media transport can
      // remain connected even when the user must explicitly enable sound.
      setAudioPlaybackState(room.canPlaybackAudio ? 'enabled' : 'blocked');
      if (!room.canPlaybackAudio) setError('Browser audio playback is blocked. Select Enable audio to hear the Resident.');
    });
    room.on(RoomEvent.TranscriptionReceived, (segments, participant) => {
      if (roomRef.current !== room) return;
      if (!isTrustedCallCaption(participant?.identity, response.agent_identity)) return;
      const speaker: LiveCaption['speaker'] = 'resident';
      setCaptions(previous => {
        const next = new Map(previous.map(caption => [caption.id, caption]));
        for (const segment of segments) {
          next.set(segment.id, {
            id: segment.id,
            text: segment.text,
            final: segment.final,
            speaker,
          });
        }
        return [...next.values()].slice(-8);
      });
    });
    room.on(RoomEvent.DataReceived, (payload, participant) => {
      if (roomRef.current !== room) return;
      try {
        const packet = JSON.parse(new TextDecoder().decode(payload)) as CallDataPacket;
        const active = callRef.current;
        if (!active || !isTrustedCallPacket(packet, participant?.identity, active, response.agent_identity)) return;
        if (isTrustedCallReadyPacket(packet, participant?.identity, active, response.agent_identity)) {
          clearReadinessTimeout();
          setError(null);
          setConnectionState('connected');
          return;
        }
        if (packet.type === 'blueprint_call_operator_caption') {
          // `isTrustedCallPacket` above deliberately runs before this parser;
          // do not admit browser-published caption-shaped data.
          const caption = parseTrustedOperatorCaption(packet, participant?.identity, active, response.agent_identity);
          if (!caption) return;
          setCaptions(previous => {
            const next = new Map(previous.map(item => [item.id, item]));
            next.set(caption.id, caption);
            return [...next.values()].slice(-8);
          });
        }
        if (packet.type === 'blueprint_call_display_blocks') setDisplayBlocks(asDisplayBlocks(packet.display_blocks));
        if (packet.type === 'blueprint_call_tts_source'
          && typeof packet.phrase_id === 'string'
          && (packet.event === 'started' || packet.event === 'completed' || packet.event === 'interrupted')) {
          // The marker supplies phrase identity only. Browser audio analysis
          // above supplies the separate proof that rendered audio began/stopped.
          playoutTrackerRef.current?.source(packet.phrase_id, packet.event);
        }
      } catch {
        // Room data is untrusted transport input. Ignore unknown/malformed data.
      }
    });
    setConnectionState('connecting');
    await room.connect(connection.url, connection.token);
    // connect() is reached from Start/Reconnect, both explicit user gestures.
    // Resume browser audio here so autoplay policy cannot silently produce a
    // healthy-looking but inaudible call.
    try {
      await room.startAudio();
      setAudioPlaybackState('enabled');
    } catch {
      // Some browsers no longer preserve the original click activation after
      // the backend/RTC awaits. The visible Enable audio button below is a
      // direct user-gesture fallback and keeps the call recoverable.
      setAudioPlaybackState('blocked');
      setError('Browser audio playback is blocked. Select Enable audio to hear the Resident.');
    }
    setMicrophoneState('requesting');
    try {
      await room.localParticipant.setMicrophoneEnabled(true);
      setMicrophoneState('on');
    } catch (reason) {
      setMicrophoneState('denied');
      throw reason;
    }
  }, [clearReadinessTimeout, detachRemoteAudio, disconnect, projectId, setCallSnapshot]);

  const start = useCallback(async () => {
    if (!projectId) return;
    setError(null);
    setConnectionState('starting');
    // A successful start acquires a server-side reply-owner lease and Fast
    // capacity before the browser attempts RTC/microphone setup.  If that
    // setup fails, this exact newly acquired lease must be released so typed
    // Workday delivery is not left suppressed behind a local UI error.
    let acquired: StrategicCallState | null = null;
    try {
      const response = await startStrategicCall(projectId);
      setCallSnapshot(response.call);
      acquired = response.call;
      await connect(response);
    } catch (reason) {
      disconnect();
      if (acquired) {
        try {
          const released = await endStrategicCall(projectId, {
            call_id: acquired.call_id,
            generation: acquired.generation,
          });
          // Do not overwrite a newer user action if this failed start raced a
          // subsequent explicitly acquired generation.
          if (callRef.current?.call_id === acquired.call_id
            && callRef.current?.generation === acquired.generation) {
            setCallSnapshot(released.call);
          }
        } catch {
          // Preserve the original RTC/microphone error and keep the explicit
          // End call control available if the fenced release itself failed.
        }
      }
      setConnectionState('error');
      setError(reason instanceof Error ? reason.message : 'Unable to start call.');
    }
  }, [connect, disconnect, projectId, setCallSnapshot]);

  const reconnect = useCallback(async () => {
    if (!projectId) return;
    setError(null);
    setConnectionState('connecting');
    try {
      const response = await fetchStrategicCall(projectId);
      if (!response) throw new Error('No active call to reconnect.');
      setCallSnapshot(response.call);
      await connect(response);
    } catch (reason) {
      disconnect();
      setConnectionState('error');
      setError(reason instanceof Error ? reason.message : 'Unable to reconnect call.');
    }
  }, [connect, disconnect, projectId, setCallSnapshot]);

  const end = useCallback(async () => {
    if (!projectId || !callRef.current) return;
    const fence = { call_id: callRef.current.call_id, generation: callRef.current.generation };
    disconnect();
    setMicrophoneState('off');
    setConnectionState('idle');
    try {
      const response = await endStrategicCall(projectId, fence);
      setCallSnapshot(response.call);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Unable to end call.');
    }
  }, [disconnect, projectId, setCallSnapshot]);

  const toggleMicrophone = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;
    const enabled = microphoneState !== 'on';
    setMicrophoneState('requesting');
    try {
      await room.startAudio();
      await room.localParticipant.setMicrophoneEnabled(enabled);
      setMicrophoneState(enabled ? 'on' : 'off');
    } catch (reason) {
      setMicrophoneState('denied');
      setError(reason instanceof Error ? reason.message : 'Unable to change microphone state.');
    }
  }, [microphoneState]);

  const enableAudio = useCallback(async () => {
    const room = roomRef.current;
    if (!room) return;
    try {
      // Invoked directly from the button click; keep this first await at the
      // LiveKit autoplay boundary so browser activation is not lost.
      await room.startAudio();
      setAudioPlaybackState('enabled');
      setError(null);
    } catch (reason) {
      setAudioPlaybackState('blocked');
      setError(reason instanceof Error ? reason.message : 'Unable to enable browser audio playback.');
    }
  }, []);

  const publishCommittedMessage = useCallback(async (message: { id: string; sequence?: number | null }) => {
    const room = roomRef.current;
    const active = callRef.current;
    if (!room || !active || connectionState !== 'connected') return;
    // This is intentionally only a hint. The coordinator fetches and validates
    // the canonical Blueprint row; no operator text, artifact data, or secrets
    // are copied into the room packet.
    if (message.sequence == null) return;
    const payload = new TextEncoder().encode(JSON.stringify({
      v: 1,
      type: 'blueprint_message_committed',
      call_id: active.call_id,
      generation: active.generation,
      message_id: message.id,
      sequence: message.sequence,
    }));
    await room.localParticipant.publishData(payload, { reliable: true, topic: 'blueprint.call.wake' });
  }, [connectionState]);

  useEffect(() => () => disconnect(), [disconnect]);
  useEffect(() => {
    let current = true;
    queueMicrotask(() => {
      if (!current) return;
      setCallSnapshot(null);
      setCaptions([]);
      setDisplayBlocks([]);
      setError(null);
      setMicrophoneState('off');
      setAudioPlaybackState('unknown');
      setConnectionState('idle');
    });
    return () => { current = false; };
  }, [projectId, setCallSnapshot]);

  // A reload never reconstructs a second transcript or silently grabs the
  // microphone. It discovers an existing lease, then offers an explicit
  // reconnect which obtains fresh short-lived media credentials.
  useEffect(() => {
    if (!projectId) return;
    let cancelled = false;
    void fetchStrategicCall(projectId).then(response => {
      if (cancelled || !response?.call) return;
      setCallSnapshot(response.call);
      if (response.call.status !== 'ended' && response.call.status !== 'failed') {
        setConnectionState('reconnecting');
      }
    }).catch(() => {
      if (!cancelled) setError('Unable to check the existing call.');
    });
    return () => { cancelled = true; };
  }, [projectId, setCallSnapshot]);

  return {
    call,
    connectionState,
    microphoneState,
    audioPlaybackState,
    captions,
    displayBlocks,
    error,
    start,
    reconnect,
    end,
    toggleMicrophone,
    enableAudio,
    publishCommittedMessage,
  };
}
